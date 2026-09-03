"""Context Compression Strategy V1: lossless structured-object virtualization."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextDelta
from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

from .amortized_planner import (
    BATCH_POLICY_DYNAMIC,
    BATCH_POLICY_FIXED,
    FLUSH_FIXED_BATCH_SIZE,
    AmortizedDecision,
    PendingDelta,
    plan_amortized_flush,
)
from .amortized_state import HotTailPartition, TailDelta, partition_hot_tail
from .cards import benefit_gate, build_card, render_card, render_retrieval_card
from .detection import (
    RETRIEVE_OBJECT_TOOL_NAME,
    detect_delta_objects,
    iter_text_parts,
    message_key,
)
from .extractors import deterministic_summary, extract_structure
from .models import (
    DeltaRecord,
    DeltaState,
    ObjectRecord,
    PendingLedgerRecord,
    RetrievalLease,
)
from .planner import (
    EMERGENCY_FLUSH,
    FLUSH_NET_POSITIVE,
    PROJECTION_FAILED_RAW_FALLBACK,
    SUMMARY_RECHECK_BELOW_THRESHOLD,
    WAIT_BELOW_THRESHOLD,
    WAIT_NO_BASELINE,
    EconomicDecision,
    PrefixFacts,
    PreparedCandidate,
    PricingWeights,
    plan_emergency_batch,
    plan_economic_batch,
    resolve_pricing_weights,
    round_reusable_prefix,
    score_exact_batch,
)
from .renderer import (
    apply_occurrence_cards,
    project_compressed_messages,
)
from .store import ObjectContextStore, canonical_json
from .summaries import BoundedSummaryGenerator


logger = logging.getLogger(__name__)

ENGINE_NAME = "object_context"
DEFAULT_OBJECT_PREFILTER_MIN_TOKENS = 256
DEFAULT_MIN_ABSOLUTE_SAVING_TOKENS = 128
DEFAULT_MIN_RELATIVE_SAVING_RATIO = 0.25
DEFAULT_CARD_SUMMARY_ENABLED = False
DEFAULT_SUMMARY_MAX_TOKENS = 64
DEFAULT_WM_GRACE_DELTAS = 20
DEFAULT_RECENT_RETRIEVAL_ACTIVE_DELTAS = 20
DEFAULT_RETRIEVAL_MAX_TOKENS_RATIO = 0.50
DEFAULT_MIN_RAW_EXPOSURES = 1
DEFAULT_ECONOMIC_MIN_NET_SAVING_TOKENS = 1000
DEFAULT_ECONOMIC_CACHE_READ_RATIO_FALLBACK = 0.10
DEFAULT_ECONOMIC_CACHE_WRITE_RATIO_FALLBACK = 1.00
DEFAULT_EMERGENCY_CONTEXT_RATIO = 0.90
DEFAULT_HOT_TAIL_MAX_INFERENCES = 4
DEFAULT_HOT_TAIL_MAX_TOKENS = 12_800
DEFAULT_AMORTIZED_CACHE_READ_WEIGHT = 0.10
DEFAULT_BATCH_POLICY = BATCH_POLICY_DYNAMIC
DEFAULT_FIXED_BATCH_SIZE = 4

_OBJECT_REF_SCAN_RE = re.compile(r"object://obj_[a-f0-9]{24}@v[1-9][0-9]*")
_ORIGIN_VALUE_MAX_CHARS = 240
_ORIGIN_TARGET_KEYS = (
    "path",
    "file_path",
    "filename",
    "name",
    "skill",
    "skill_name",
    "cwd",
    "workdir",
)
_ORIGIN_DEFAULT_OPERATIONS = {
    "patch": "modify",
    "write_file": "write",
    "read_file": "read",
    "terminal": "execute",
    "process": "inspect",
    "skills_list": "list",
    "skill_view": "view",
    "skill_manage": "manage",
    "browser_exec": "execute",
}


@dataclass(frozen=True)
class _PreparedProjection:
    delta: DeltaRecord
    cards: tuple[tuple[str, str, str, dict[str, Any]], ...]
    compressed_view: tuple[dict[str, Any], ...]
    occurrences: tuple[dict[str, Any], ...]
    raw_tokens: int
    projected_tokens: int
    known_object_refs: tuple[str, ...] = ()
    content_replacements: tuple[tuple[int, Any], ...] = ()

    @property
    def object_refs(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                [
                    *(str(row["object_ref"]) for row in self.occurrences),
                    *self.known_object_refs,
                ]
            )
        )


@dataclass(frozen=True)
class _RequestObservationSnapshot:
    """Immutable facts for one selected provider attempt.

    Pending gains are captured before send so a later configuration, parser or
    projection change cannot rewrite the cost of the request that actually ran.
    """

    request_attempt_id: str
    conversation_id: str
    selection_sequence: int
    raw_delta_ids: tuple[str, ...]
    pending_gains: tuple[tuple[str, int, int], ...]
    selected_view: tuple[dict[str, Any], ...]
    route_namespace_hash: str
    store_path: str


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_int(value: Any, default: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _safe_float(
    value: Any,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _safe_nonnegative_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, parsed)


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _bounded_origin_value(value: Any) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    text = " ".join(str(value).strip().split())
    if len(text) > _ORIGIN_VALUE_MAX_CHARS:
        return text[: _ORIGIN_VALUE_MAX_CHARS - 1].rstrip() + "…"
    return text


def _tool_call_args(
    delta: DeltaRecord, occurrence: dict[str, Any]
) -> dict[str, Any]:
    """Resolve the exact preceding call without copying its full arguments."""

    wanted_id = str(occurrence.get("tool_call_id") or "")
    wanted_name = str(occurrence.get("tool_name") or "")
    for message in delta.raw_view:
        if not isinstance(message, dict):
            continue
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or call.get("call_id") or "")
            function = call.get("function")
            function = function if isinstance(function, dict) else {}
            call_name = str(function.get("name") or call.get("name") or "")
            if wanted_id and call_id == wanted_id:
                selected = call
            elif not wanted_id and wanted_name and call_name == wanted_name:
                selected = call
            else:
                continue
            raw_args = function.get("arguments", selected.get("arguments"))
            if isinstance(raw_args, dict):
                return dict(raw_args)
            if isinstance(raw_args, str):
                try:
                    parsed = json.loads(raw_args)
                except (TypeError, ValueError):
                    return {}
                return dict(parsed) if isinstance(parsed, dict) else {}
            return {}
    return {}


def _card_origin(delta: DeltaRecord, occurrence: dict[str, Any]) -> dict[str, str]:
    """Build bounded, deterministic provenance for one Card."""

    origin: dict[str, str] = {}
    role = _bounded_origin_value(occurrence.get("source_role"))
    if role:
        origin["role"] = role
    tool_name = _bounded_origin_value(occurrence.get("tool_name"))
    if not tool_name:
        return origin
    origin["tool"] = tool_name
    args = _tool_call_args(delta, occurrence)
    operation = ""
    for key in ("mode", "action", "operation"):
        operation = _bounded_origin_value(args.get(key))
        if operation:
            break
    if not operation:
        operation = _ORIGIN_DEFAULT_OPERATIONS.get(tool_name, "")
    if operation:
        origin["operation"] = operation
    for key in _ORIGIN_TARGET_KEYS:
        target = _bounded_origin_value(args.get(key))
        if target:
            origin["target"] = target
            break
    return origin


def _load_engine_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        return _as_dict(load_config_readonly())
    except Exception:
        logger.debug("Object Context config read failed; using defaults", exc_info=True)
        return {}


def _is_summary_or_synthetic(message: dict[str, Any]) -> bool:
    try:
        from agent.context_compressor import (
            ContextCompressor,
            is_compaction_summary_message,
        )

        if is_compaction_summary_message(message) or (
            message.get("role") == "user"
            and ContextCompressor._is_synthetic_compression_user_turn(message)
        ):
            return True
    except Exception:
        pass
    return any(
        message.get(marker)
        for marker in (
            "_thinking_prefill",
            "_empty_recovery_synthetic",
            "_empty_terminal_sentinel",
            "_dropped_toolcall_nudge",
            "_verification_stop_synthetic",
            "_pre_verify_synthetic",
            "_kanban_stop_synthetic",
        )
    )


def _content_part_text(message: dict[str, Any], part_ordinal: int) -> str:
    for part in iter_text_parts(message.get("content")):
        if part.ordinal == part_ordinal:
            return part.text
    raise ValueError("registered object source part no longer exists")


class ObjectContextEngine(ContextCompressor):
    """V1 Object Context plus the independent whole-history summarizer."""

    emit_automatic_compaction_status = True
    # The exposure ledger must learn that a selected request succeeded even
    # when a provider omits its optional usage block.
    needs_success_notification_without_usage = True
    # Provider usage is observable before Hermes has validated the returned
    # assistant item.  Exposure/Hot-Tail/W state advances only at the later,
    # accepted inference-Delta choke point.
    defers_response_success_until_inference_commit = True
    # Object Context's cache-prefix facts and Raw exposure snapshot must be
    # taken from the same canonical message view that will reach the transport,
    # after orphan/thinking/whitespace/surrogate repair but before provider
    # cache-control decoration.
    select_after_message_sanitization = True

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        summary_generator: BoundedSummaryGenerator | None = None,
        archive_hook: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        cfg = _as_dict(config) if config is not None else _load_engine_config()
        compression_cfg = _as_dict(cfg.get("compression"))
        context_cfg = _as_dict(cfg.get("context"))
        object_cfg = _as_dict(context_cfg.get("object_context"))
        threshold_tokens = _safe_int(compression_cfg.get("threshold_tokens"), 0) or None
        model_thresholds = {
            str(key): float(value)
            for key, value in _as_dict(compression_cfg.get("model_thresholds")).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        super().__init__(
            model="",
            threshold_percent=_safe_float(compression_cfg.get("threshold"), 0.50),
            protect_first_n=_safe_int(compression_cfg.get("protect_first_n"), 3),
            protect_last_n=_safe_int(compression_cfg.get("protect_last_n"), 20),
            summary_target_ratio=_safe_float(
                compression_cfg.get("target_ratio"),
                0.20,
                minimum=0.10,
                maximum=0.80,
            ),
            quiet_mode=True,
            summary_model_override=str(compression_cfg.get("summary_model") or ""),
            abort_on_summary_failure=_truthy(
                compression_cfg.get("abort_on_summary_failure"), False
            ),
            model_thresholds=model_thresholds,
            threshold_tokens_cap=threshold_tokens,
            proactive_prune_tokens=0,
            min_tail_user_messages=_safe_int(
                compression_cfg.get("min_tail_user_messages"), 1, minimum=1
            ),
        )
        self.object_context_enabled = _truthy(object_cfg.get("enabled"), True)
        configured_scheduler = str(object_cfg.get("scheduler") or "economic").casefold()
        self.scheduler = (
            configured_scheduler
            if configured_scheduler in {"economic", "amortized_batch"}
            else "economic"
        )
        self.hot_tail_max_inferences = _safe_int(
            object_cfg.get("hot_tail_max_inferences"),
            DEFAULT_HOT_TAIL_MAX_INFERENCES,
            minimum=1,
        )
        self.hot_tail_max_tokens = _safe_int(
            object_cfg.get("hot_tail_max_tokens"),
            DEFAULT_HOT_TAIL_MAX_TOKENS,
            minimum=1,
        )
        self.pending_max_inferences = 2 * self.hot_tail_max_inferences
        self.pending_max_tokens = 2 * self.hot_tail_max_tokens
        self.amortized_cache_read_weight = _safe_float(
            object_cfg.get("amortized_cache_read_weight"),
            DEFAULT_AMORTIZED_CACHE_READ_WEIGHT,
        )
        configured_batch_policy = str(
            object_cfg.get("batch_policy") or DEFAULT_BATCH_POLICY
        ).casefold()
        self.batch_policy = (
            configured_batch_policy
            if configured_batch_policy in {BATCH_POLICY_DYNAMIC, BATCH_POLICY_FIXED}
            else DEFAULT_BATCH_POLICY
        )
        self.fixed_batch_size = _safe_int(
            object_cfg.get("fixed_batch_size"),
            DEFAULT_FIXED_BATCH_SIZE,
            minimum=1,
        )
        self.object_prefilter_min_tokens = _safe_int(
            object_cfg.get("object_prefilter_min_tokens"),
            DEFAULT_OBJECT_PREFILTER_MIN_TOKENS,
            minimum=1,
        )
        self.min_absolute_saving_tokens = _safe_int(
            object_cfg.get("min_absolute_saving_tokens"),
            DEFAULT_MIN_ABSOLUTE_SAVING_TOKENS,
        )
        self.min_relative_saving_ratio = _safe_float(
            object_cfg.get("min_relative_saving_ratio"),
            DEFAULT_MIN_RELATIVE_SAVING_RATIO,
        )
        self.card_summary_enabled = _truthy(
            object_cfg.get("card_summary_enabled"),
            DEFAULT_CARD_SUMMARY_ENABLED,
        )
        self.summary_max_tokens = _safe_int(
            object_cfg.get("summary_max_tokens"),
            DEFAULT_SUMMARY_MAX_TOKENS,
            minimum=8,
        )
        self.wm_grace_deltas = _safe_int(
            object_cfg.get("wm_grace_deltas"),
            DEFAULT_WM_GRACE_DELTAS,
        )
        self.recent_retrieval_active_deltas = _safe_int(
            object_cfg.get("recent_retrieval_active_deltas"),
            DEFAULT_RECENT_RETRIEVAL_ACTIVE_DELTAS,
        )
        self.retrieval_max_tokens_ratio = _safe_float(
            object_cfg.get("retrieval_max_tokens_ratio"),
            DEFAULT_RETRIEVAL_MAX_TOKENS_RATIO,
            minimum=0.05,
        )
        self.min_raw_exposures = max(
            1,
            _safe_int(
                object_cfg.get("min_raw_exposures"),
                DEFAULT_MIN_RAW_EXPOSURES,
            ),
        )
        self.economic_min_net_saving_tokens = _safe_int(
            object_cfg.get("economic_min_net_saving_tokens"),
            DEFAULT_ECONOMIC_MIN_NET_SAVING_TOKENS,
        )
        raw_usd_threshold = object_cfg.get("economic_min_net_saving_usd")
        self.economic_min_net_saving_usd = (
            max(0.0, float(raw_usd_threshold))
            if isinstance(raw_usd_threshold, (int, float))
            and not isinstance(raw_usd_threshold, bool)
            else None
        )
        self.economic_cache_read_ratio_fallback = _safe_float(
            object_cfg.get("economic_cache_read_ratio_fallback"),
            DEFAULT_ECONOMIC_CACHE_READ_RATIO_FALLBACK,
        )
        self.economic_cache_write_ratio_fallback = _safe_nonnegative_float(
            object_cfg.get("economic_cache_write_ratio_fallback"),
            DEFAULT_ECONOMIC_CACHE_WRITE_RATIO_FALLBACK,
        )
        self.emergency_context_ratio = _safe_float(
            object_cfg.get("emergency_context_ratio"),
            DEFAULT_EMERGENCY_CONTEXT_RATIO,
            minimum=0.10,
            maximum=1.0,
        )
        self._summary_generator = summary_generator or BoundedSummaryGenerator(
            max_tokens=self.summary_max_tokens
        )
        self._archive_hook = archive_hook
        self._store: ObjectContextStore | None = None
        self._store_path: Path | None = None
        self._object_session_id = ""
        self._conversation_id = ""
        self._active_turn_id = ""
        self._last_rendered_refs: set[str] = set()
        self._last_hot_tail_tokens = 0
        self._last_batch_size = 0
        self._last_failure = ""
        self._projection_sequence = 0
        self._cache_request_sequence = 0
        self._request_sequence = 0
        self._pending_raw_exposure: (
            _RequestObservationSnapshot | None
        ) = None
        self._unconfirmed_success_observations: list[
            _RequestObservationSnapshot
        ] = []
        self._active_pending_snapshot_ids: frozenset[str] = frozenset()
        self._planning_request_attempt_id = ""
        self._previous_successful_request_view: tuple[dict[str, Any], ...] | None = None
        self._cache_baseline_state = "unknown"
        self._cache_namespace_hash = ""
        self._last_success_sequence = 0
        self._last_economic_decision: EconomicDecision | None = None
        self._last_amortized_decision: AmortizedDecision | None = None
        self._last_hot_partition: HotTailPartition | None = None
        self._amortized_telemetry_context: (
            tuple[
                AmortizedDecision,
                HotTailPartition,
                Any | None,
                Any | None,
                PricingWeights,
                bool,
            ]
            | None
        ) = None
        # Immutable replacement snapshot for high-frequency status-bar reads.
        # Detailed status continues to aggregate SQLite; this mapping must
        # remain entirely in-memory so prompt-toolkit repaint never performs
        # storage I/O.
        self._status_bar_savings_snapshot: dict[str, int | float | bool] = {}

    @property
    def name(self) -> str:
        return ENGINE_NAME

    @staticmethod
    def _route_namespace_digest(
        *,
        model: str,
        provider: str,
        base_url: str,
        api_mode: str,
        api_key: Any,
    ) -> str:
        """Return a non-secret identity for the provider cache namespace."""

        credential_digest = hashlib.sha256(
            str(api_key or "").encode("utf-8", errors="replace")
        ).hexdigest()
        encoded = canonical_json({
            "model": str(model or ""),
            "provider": str(provider or ""),
            "base_url": str(base_url or ""),
            "api_mode": str(api_mode or ""),
            "credential_digest": credential_digest,
        })
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def update_model(
        self,
        model: str,
        context_length: int,
        base_url: str = "",
        api_key: Any = "",
        provider: str = "",
        api_mode: str = "",
        max_tokens: int | None = None,
    ) -> None:
        """Invalidate request-local economics when the serving route changes."""

        next_namespace = self._route_namespace_digest(
            model=model,
            provider=provider,
            base_url=base_url,
            api_mode=api_mode,
            api_key=api_key,
        )
        route_changed = next_namespace != getattr(self, "_cache_namespace_hash", "")
        super().update_model(
            model,
            context_length,
            base_url=base_url,
            api_key=api_key,
            provider=provider,
            api_mode=api_mode,
            max_tokens=max_tokens,
        )
        if route_changed:
            self._cache_namespace_hash = next_namespace
            self._pending_raw_exposure = None
            self._active_pending_snapshot_ids = frozenset()
            self._planning_request_attempt_id = ""
            self._previous_successful_request_view = None
            self._cache_baseline_state = "cold"
            self._last_economic_decision = None
            self._last_amortized_decision = None
            self._amortized_telemetry_context = None

    def _clear_status_bar_savings(self) -> None:
        self._status_bar_savings_snapshot = {}

    def _restore_status_bar_savings(self) -> None:
        store = self._store
        if store is None or not self._conversation_id:
            self._clear_status_bar_savings()
            return
        try:
            status = store.aggregate_status(self._conversation_id)
            self._projection_sequence = max(
                0, _safe_int(status.get("request_projection_count"), 0)
            )
            latest = _as_dict(status.get("last_request_metrics"))
            totals = _as_dict(status.get("request_metric_totals"))
            has_conversation_scope = (
                "raw_conversation_tokens" in latest
                and "conversation_tokens_saved" in latest
                and "raw_conversation_tokens" in totals
                and "conversation_tokens_saved" in totals
            )
            raw_name = (
                "raw_conversation_tokens"
                if has_conversation_scope
                else "raw_context_tokens"
            )
            saved_name = (
                "conversation_tokens_saved"
                if has_conversation_scope
                else "tokens_saved"
            )
            last_raw = max(0, _safe_int(latest.get(raw_name), 0))
            last_saved = max(0, _safe_int(latest.get(saved_name), 0))
            session_raw = max(0, _safe_int(totals.get(raw_name), 0))
            session_saved = max(0, _safe_int(totals.get(saved_name), 0))
            self._status_bar_savings_snapshot = {
                "object_context_active": True,
                "last_tokens_saved": last_saved,
                "last_reduction_percent": (
                    min(100.0, last_saved / last_raw * 100.0) if last_raw else 0.0
                ),
                "session_tokens_saved": session_saved,
                "session_reduction_percent": (
                    min(100.0, session_saved / session_raw * 100.0)
                    if session_raw
                    else 0.0
                ),
                "request_projection_count": max(
                    0, _safe_int(status.get("request_projection_count"), 0)
                ),
                "_session_raw_tokens": session_raw,
            }
        except Exception:
            self._projection_sequence = 0
            self._clear_status_bar_savings()
            logger.debug(
                "Object Context status-bar savings restore failed",
                exc_info=True,
            )

    def _remember_request_projection(
        self,
        *,
        raw_tokens: int,
        saved_tokens: int,
    ) -> None:
        previous = self._status_bar_savings_snapshot
        count = max(0, _safe_int(previous.get("request_projection_count"), 0)) + 1
        session_saved = max(
            0, _safe_int(previous.get("session_tokens_saved"), 0)
        ) + max(0, saved_tokens)
        # Session raw tokens are private to the snapshot because only the
        # reduction percentage needs them; do not expose another public field.
        session_raw = max(
            0, _safe_int(previous.get("_session_raw_tokens"), 0)
        ) + max(0, raw_tokens)
        self._status_bar_savings_snapshot = {
            "object_context_active": True,
            "last_tokens_saved": max(0, saved_tokens),
            "last_reduction_percent": (
                min(100.0, saved_tokens / raw_tokens * 100.0)
                if raw_tokens > 0
                else 0.0
            ),
            "session_tokens_saved": session_saved,
            "session_reduction_percent": (
                min(100.0, session_saved / session_raw * 100.0)
                if session_raw > 0
                else 0.0
            ),
            "request_projection_count": count,
            "_session_raw_tokens": session_raw,
        }

    def get_status_bar_metrics(self) -> dict[str, Any]:
        """Return an I/O-free immutable-copy view for CLI repaint."""

        return {
            key: value
            for key, value in self._status_bar_savings_snapshot.items()
            if not key.startswith("_")
        }

    @staticmethod
    def is_available() -> bool:
        return True

    def _flush_unconfirmed_success_observations(self) -> None:
        """Persist accepted request snapshots in order, retaining failures."""

        store = self._store
        if store is None:
            return
        while self._unconfirmed_success_observations:
            pending_exposure = self._unconfirmed_success_observations[0]
            target_store = store
            if str(store.path) != pending_exposure.store_path:
                try:
                    target_store = ObjectContextStore(
                        Path(pending_exposure.store_path)
                    )
                except Exception:
                    logger.warning(
                        "Object Context accepted-request outbox store reopen "
                        "failed; retaining the observation",
                        exc_info=True,
                    )
                    return
            try:
                observation = target_store.confirm_successful_request_observation(
                    conversation_id=pending_exposure.conversation_id,
                    request_attempt_id=pending_exposure.request_attempt_id,
                    raw_delta_ids=pending_exposure.raw_delta_ids,
                    pending_accruals=pending_exposure.pending_gains,
                    exposure_request_sequence=(
                        pending_exposure.selection_sequence
                    ),
                    min_raw_exposures=self.min_raw_exposures,
                    route_namespace_hash=pending_exposure.route_namespace_hash,
                )
            except Exception:
                logger.warning(
                    "Object Context successful-request confirmation failed; "
                    "the immutable observation remains queued for retry",
                    exc_info=True,
                )
                return
            self._unconfirmed_success_observations.pop(0)
            if pending_exposure.conversation_id == self._conversation_id:
                self._last_success_sequence = max(
                    self._last_success_sequence,
                    int(observation.success_sequence),
                )

    def confirm_response_accepted(self) -> None:
        """Advance Raw exposure/W only for an application-accepted inference."""

        pending_exposure = self._pending_raw_exposure
        self._pending_raw_exposure = None
        if pending_exposure is not None:
            self._previous_successful_request_view = tuple(
                copy.deepcopy(list(pending_exposure.selected_view))
            )
            self._cache_baseline_state = "known"
            self._unconfirmed_success_observations.append(pending_exposure)
        self._flush_unconfirmed_success_observations()

    def confirm_response_rejected(self) -> None:
        """Release one terminal failed/rejected attempt without earning credit."""

        self._pending_raw_exposure = None

    def observe_response_usage(self, usage: dict[str, Any]) -> None:
        """Record provider usage without treating the response as accepted."""

        self._record_response_usage(usage)

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Compatibility path for hosts that already imply accepted success."""

        self.confirm_response_accepted()
        self._record_response_usage(usage)

    def _record_response_usage(self, usage: dict[str, Any]) -> None:
        """Retain summarizer accounting and provider cache telemetry."""

        super().update_from_response(usage)
        # ``update_from_response({})`` is also used to consume a pending
        # compression verdict when a provider response has no usage block. It
        # is not a measured request and must not become a zero-hit datapoint.
        if not usage or not any(
            key in usage
            for key in (
                "prompt_tokens",
                "input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
            )
        ):
            return
        cache_read = max(0, _safe_int(usage.get("cache_read_tokens"), 0))
        cache_write = max(0, _safe_int(usage.get("cache_write_tokens"), 0))
        prompt_tokens = max(0, _safe_int(usage.get("prompt_tokens"), 0))
        uncached_input = max(
            0,
            _safe_int(
                usage.get("input_tokens"),
                max(0, prompt_tokens - cache_read - cache_write),
            ),
        )
        if prompt_tokens <= 0:
            prompt_tokens = uncached_input + cache_read + cache_write
        if prompt_tokens <= 0 or self._store is None or not self._conversation_id:
            return

        self._cache_request_sequence += 1
        sequence = self._cache_request_sequence
        metadata = {
            "event": "provider_cache_usage",
            "cache_request_id": (
                f"{self._object_session_id or 'session'}:cache:"
                f"{sequence}:{time.time_ns()}"
            ),
            "cache_request_sequence": sequence,
            "turn_id": self._active_turn_id,
            "session_id": self._object_session_id,
        }
        try:
            self._store.record_metrics(
                self._conversation_id,
                {
                    "prompt_tokens": prompt_tokens,
                    "uncached_input_tokens": uncached_input,
                    "cache_read_tokens": cache_read,
                    "cache_write_tokens": cache_write,
                    "prompt_cache_hit_ratio": min(
                        1.0, cache_read / prompt_tokens
                    ),
                },
                metadata=metadata,
            )
        except Exception:
            logger.debug(
                "Object Context provider cache telemetry write failed",
                exc_info=True,
            )

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        # A prior accepted request may have hit a transient SQLite error just
        # before its session boundary. Retry its UUID-idempotent outbox while
        # the old store/identity are still available; never silently erase it.
        self._flush_unconfirmed_success_observations()
        super().on_session_start(session_id, **kwargs)
        self._clear_status_bar_savings()
        self._projection_sequence = 0
        self._cache_request_sequence = 0
        self._request_sequence = 0
        self._pending_raw_exposure = None
        self._active_pending_snapshot_ids = frozenset()
        self._planning_request_attempt_id = ""
        self._previous_successful_request_view = None
        self._cache_baseline_state = "unknown"
        self._last_success_sequence = 0
        self._last_economic_decision = None
        self._last_amortized_decision = None
        self._last_hot_partition = None
        self._amortized_telemetry_context = None
        prior_conversation = self._conversation_id
        self._object_session_id = str(session_id or "")
        conversation_id = str(kwargs.get("conversation_id") or "")
        session_db = kwargs.get("session_db", getattr(self, "_session_db", None))
        if not conversation_id and session_db is not None and session_id:
            resolver = getattr(session_db, "get_conversation_root", None)
            if callable(resolver):
                try:
                    conversation_id = str(resolver(session_id) or "")
                except Exception:
                    logger.debug("Object Context lineage lookup failed", exc_info=True)
        if (
            not conversation_id
            and kwargs.get("boundary_reason") == "compression"
            and prior_conversation
        ):
            conversation_id = prior_conversation
        self._conversation_id = conversation_id or self._object_session_id
        if kwargs.get("boundary_reason") != "compression":
            self._active_turn_id = ""

        hermes_home = kwargs.get("hermes_home")
        if hermes_home:
            self._store_path = (
                Path(str(hermes_home)) / "context" / "object_context_v1.sqlite3"
            )
        if self._store_path is None:
            self._store = None
            logger.warning(
                "Object Context V1 disabled: profile storage path is unavailable"
            )
            return
        try:
            self._store = ObjectContextStore(self._store_path)
            self._reconcile_persisted_history()
            self._request_sequence = self._store.max_request_sequence(
                self._conversation_id
            )
            latest_success = getattr(self._store, "latest_success_sequence", None)
            if callable(latest_success):
                self._last_success_sequence = max(
                    0, int(latest_success(self._conversation_id) or 0)
                )
            # One storage aggregation per real/resumed session boundary. From
            # this point onward the frequently repainted CLI reads only the
            # immutable in-memory snapshot updated by select_context().
            self._restore_status_bar_savings()
        except Exception as exc:
            self._store = None
            self._clear_status_bar_savings()
            self._last_failure = f"store_initialization:{exc}"
            logger.warning(
                "Object Context V1 store initialization failed; raw context remains active",
                exc_info=True,
            )

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        self._flush_unconfirmed_success_observations()
        super().on_session_end(session_id, messages)
        self._object_session_id = ""
        self._conversation_id = ""
        self._active_turn_id = ""
        self._last_rendered_refs.clear()
        self._projection_sequence = 0
        self._cache_request_sequence = 0
        self._request_sequence = 0
        self._pending_raw_exposure = None
        self._active_pending_snapshot_ids = frozenset()
        self._planning_request_attempt_id = ""
        self._previous_successful_request_view = None
        self._cache_baseline_state = "unknown"
        self._last_success_sequence = 0
        self._last_economic_decision = None
        self._last_amortized_decision = None
        self._last_hot_partition = None
        self._amortized_telemetry_context = None
        self._clear_status_bar_savings()

    def on_session_reset(self) -> None:
        self._flush_unconfirmed_success_observations()
        super().on_session_reset()
        self._object_session_id = ""
        self._conversation_id = ""
        self._active_turn_id = ""
        self._last_rendered_refs.clear()
        self._projection_sequence = 0
        self._cache_request_sequence = 0
        self._request_sequence = 0
        self._pending_raw_exposure = None
        self._active_pending_snapshot_ids = frozenset()
        self._planning_request_attempt_id = ""
        self._previous_successful_request_view = None
        self._cache_baseline_state = "unknown"
        self._last_success_sequence = 0
        self._last_economic_decision = None
        self._last_amortized_decision = None
        self._last_hot_partition = None
        self._amortized_telemetry_context = None
        self._clear_status_bar_savings()

    def _record_metric(
        self,
        name: str,
        value: float,
        *,
        delta_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._store is None or not self._conversation_id:
            return
        try:
            self._store.record_metric(
                self._conversation_id,
                name,
                value,
                delta_id=delta_id,
                metadata=metadata,
            )
        except Exception:
            logger.debug("Object Context metric write failed", exc_info=True)

    @staticmethod
    def _reconciled_delta_id(
        conversation_id: str, messages: Sequence[dict[str, Any]]
    ) -> str:
        digest = hashlib.sha256(
            canonical_json(list(messages)).encode("utf-8", errors="surrogatepass")
        ).hexdigest()[:24]
        return f"reconcile:{conversation_id}:{digest}"

    def _history_deltas(self, history: Sequence[dict[str, Any]]) -> list[ContextDelta]:
        deltas: list[ContextDelta] = []
        current_turn = "reconcile:no-user"
        index = 0
        while index < len(history):
            original = history[index]
            if not isinstance(original, dict):
                index += 1
                continue
            message = {
                key: copy.deepcopy(value)
                for key, value in original.items()
                if key != "_row_id"
            }
            role = str(message.get("role") or "")
            if role == "system" or _is_summary_or_synthetic(message):
                index += 1
                continue
            row_identity = original.get("_row_id", message.get("timestamp", index))
            if role == "user":
                current_turn = f"reconcile-turn:{row_identity}"
                group = [message]
                kind = "user"
                index += 1
            elif role == "assistant":
                group = [message]
                index += 1
                while index < len(history):
                    candidate = history[index]
                    if (
                        not isinstance(candidate, dict)
                        or candidate.get("role") != "tool"
                    ):
                        break
                    group.append({
                        key: copy.deepcopy(value)
                        for key, value in candidate.items()
                        if key != "_row_id"
                    })
                    index += 1
                kind = "inference"
            elif role == "tool":
                group = [message]
                kind = "inference"
                index += 1
            else:
                index += 1
                continue
            delta_id = self._reconciled_delta_id(self._conversation_id, group)
            deltas.append(
                ContextDelta(
                    delta_id=delta_id,
                    kind=kind,
                    conversation_id=self._conversation_id,
                    session_id=self._object_session_id,
                    turn_id=current_turn,
                    sequence=len(deltas),
                    messages=tuple(group),
                    inference_id=(delta_id if kind == "inference" else ""),
                )
            )
        return deltas

    def _reconcile_persisted_history(self) -> None:
        store = self._store
        session_db = getattr(self, "_session_db", None)
        getter = getattr(session_db, "get_messages_as_conversation", None)
        if store is None or not self._object_session_id or not callable(getter):
            return
        try:
            history = getter(
                self._object_session_id,
                include_ancestors=True,
                include_inactive=True,
                include_row_ids=True,
            )
        except (TypeError, AttributeError):
            return
        except Exception:
            logger.warning("Object Context raw-trace reconciliation read failed")
            return
        observed_signatures = {
            tuple(message_key(message) for message in delta.raw_view)
            for delta in store.list_deltas(self._conversation_id)
        }
        for delta in self._history_deltas(history or []):
            signature = tuple(message_key(message) for message in delta.messages)
            if signature in observed_signatures:
                continue
            if (
                store.find_delta_by_raw_view(self._conversation_id, delta.messages)
                is not None
            ):
                continue
            self._ingest_delta(delta, schedule=False)
            observed_signatures.add(signature)

    def _ingest_delta(self, delta: ContextDelta, *, schedule: bool) -> None:
        store = self._store
        if store is None:
            return
        if delta.conversation_id:
            self._conversation_id = delta.conversation_id
        if delta.session_id:
            self._object_session_id = delta.session_id
        if delta.kind == "user":
            self._active_turn_id = delta.turn_id
        record = store.register_delta(
            delta_id=delta.delta_id,
            conversation_id=self._conversation_id,
            session_id=self._object_session_id,
            turn_id=delta.turn_id,
            kind=delta.kind,
            inference_id=delta.inference_id,
            turn_sequence=delta.sequence,
            raw_view=delta.messages,
        )
        self._register_detected_objects(delta, record)
        # V1.1 commit-time work ends after local registration. ``schedule`` is
        # retained only for call-site compatibility during the migration; no
        # projection decision is allowed before request assembly.
        del schedule

    def _register_detected_objects(
        self, delta: ContextDelta, record: DeltaRecord
    ) -> None:
        store = self._store
        if store is None:
            return
        try:
            detected = detect_delta_objects(delta, min_tokens=1)
        except Exception as exc:
            self._last_failure = f"detection:{exc}"
            self._record_metric("compression_failures", 1, delta_id=delta.delta_id)
            return
        self._record_metric("objects_detected", len(detected), delta_id=delta.delta_id)
        externalized = 0
        skipped = 0
        for item in detected:
            if estimate_tokens_rough(item.content) < self.object_prefilter_min_tokens:
                skipped += 1
                continue
            try:
                store.register_object(
                    conversation_id=self._conversation_id,
                    session_id=self._object_session_id,
                    delta=record,
                    detected=item,
                )
                externalized += 1
            except Exception as exc:
                self._last_failure = f"object_store:{exc}"
                self._record_metric(
                    "compression_failures",
                    1,
                    delta_id=delta.delta_id,
                    metadata={"stage": "object_store"},
                )
        self._record_metric(
            "objects_externalized", externalized, delta_id=delta.delta_id
        )
        self._record_metric("objects_skipped_small", skipped, delta_id=delta.delta_id)

    def on_delta_committed(self, delta: ContextDelta) -> None:
        if self._store is None:
            return
        if delta.kind == "user":
            # A new real user Delta is a definitive boundary after any prior
            # turn.  If an early-return failure path could not emit an explicit
            # rejection callback, it must not fence this new turn or receive
            # Raw exposure / waiting-area credit retroactively.
            self.confirm_response_rejected()
        try:
            self._ingest_delta(delta, schedule=False)
        except Exception as exc:
            self._last_failure = f"delta_ingest:{exc}"
            self._record_metric("compression_failures", 1, delta_id=delta.delta_id)
            logger.warning(
                "Object Context Delta ingestion failed; raw trace remains active",
                exc_info=True,
            )

    def _schedule_newly_cold(self) -> None:
        """Deprecated V1 seam: economic scheduling runs only per request."""

        self._last_hot_tail_tokens = 0

    def _ensure_delta_objects(self, delta: DeltaRecord) -> None:
        if self._store is None or self._store.occurrences_for_delta(delta.delta_id):
            return
        observed = ContextDelta(
            delta_id=delta.delta_id,
            kind=delta.kind,
            conversation_id=delta.conversation_id,
            session_id=delta.session_id,
            turn_id=delta.turn_id,
            sequence=delta.turn_sequence,
            messages=delta.raw_view,
            inference_id=delta.inference_id,
        )
        self._register_detected_objects(observed, delta)

    def _raw_span(self, delta: DeltaRecord, occurrence: dict[str, Any]) -> str:
        message_ordinal = int(occurrence["message_ordinal"])
        part_ordinal = int(occurrence["part_ordinal"])
        if message_ordinal < 0 or message_ordinal >= len(delta.raw_view):
            raise ValueError("registered message ordinal is invalid")
        text = _content_part_text(delta.raw_view[message_ordinal], part_ordinal)
        start = int(occurrence["span_start"])
        end = int(occurrence["span_end"])
        if start < 0 or end <= start or end > len(text):
            raise ValueError("registered object span is invalid")
        return text[start:end]

    def _successful_retrievals_for_delta(
        self, delta: DeltaRecord
    ) -> tuple[tuple[int, dict[str, Any]], ...]:
        """Return successful known-reference results without re-detecting data."""

        store = self._store
        if store is None:
            return ()
        successful: list[tuple[int, dict[str, Any]]] = []
        for ordinal, message in enumerate(delta.raw_view):
            if (
                not isinstance(message, dict)
                or message.get("role") != "tool"
                or str(message.get("tool_name") or message.get("name") or "")
                != RETRIEVE_OBJECT_TOOL_NAME
            ):
                continue
            tool_call_id = str(message.get("tool_call_id") or "")
            event = store.retrieval_event_for_tool_call(
                self._conversation_id, tool_call_id
            )
            if not event or str(event.get("status") or "") != "success":
                continue
            object_ref = str(event.get("object_ref") or "")
            raw_content = str(message.get("content") or "")
            if (
                store.parse_object_ref(object_ref) is None
                or object_ref not in raw_content
                or "retrieved_object" not in raw_content
            ):
                continue
            # Resolve once here so an absent/corrupt archive object fails open
            # as raw instead of publishing a receipt that cannot be reloaded.
            try:
                record = store.get_object(self._conversation_id, object_ref)
            except Exception:
                logger.warning(
                    "Object Context retrieval receipt target failed validation; "
                    "keeping the exact result raw",
                    exc_info=True,
                )
                continue
            if record is None:
                continue
            successful.append((ordinal, event))
        return tuple(successful)

    def _prepare_retrieval_projection(
        self, delta: DeltaRecord
    ) -> _PreparedProjection | None:
        retrievals = self._successful_retrievals_for_delta(delta)
        if not retrievals:
            return None
        compressed_view = copy.deepcopy(list(delta.raw_view))
        replacements: list[tuple[int, Any]] = []
        refs: list[str] = []
        for ordinal, event in retrievals:
            object_ref = str(event["object_ref"])
            receipt = render_retrieval_card(object_ref=object_ref, status="success")
            compressed_view[ordinal]["content"] = receipt
            replacements.append((ordinal, receipt))
            refs.append(object_ref)
        rendered_tokens = estimate_messages_tokens_rough(compressed_view)
        if rendered_tokens >= delta.raw_token_count:
            return None
        return _PreparedProjection(
            delta=delta,
            cards=(),
            compressed_view=tuple(compressed_view),
            occurrences=(),
            raw_tokens=delta.raw_token_count,
            projected_tokens=rendered_tokens,
            known_object_refs=tuple(dict.fromkeys(refs)),
            content_replacements=tuple(replacements),
        )

    def _prepare_delta_projection(
        self,
        delta: DeltaRecord,
        *,
        generate_summaries: bool,
    ) -> _PreparedProjection | None:
        """Build immutable candidate bytes without publishing scheduler state."""

        store = self._store
        if store is None:
            return None
        retrieval_projection = self._prepare_retrieval_projection(delta)
        if retrieval_projection is not None:
            return retrieval_projection
        self._ensure_delta_objects(delta)
        occurrences = store.occurrences_for_delta(delta.delta_id)
        accepted: set[str] = set()
        card_updates: list[tuple[str, str, str, dict[str, Any]]] = []
        rows_for_render: list[dict[str, Any]] = []
        for occurrence in occurrences:
            object_ref = str(occurrence["object_ref"])
            record = store.get_object(self._conversation_id, object_ref)
            if record is None:
                continue
            contains = extract_structure(record)
            origin = _card_origin(delta, occurrence)
            provisional = render_card(
                build_card(
                    record,
                    summary="",
                    contains=contains,
                    origin=origin,
                )
            )
            raw_span = self._raw_span(delta, occurrence)
            plausible, _, _ = benefit_gate(
                raw_span,
                provisional,
                min_absolute_saving_tokens=self.min_absolute_saving_tokens,
                min_relative_saving_ratio=self.min_relative_saving_ratio,
            )
            if not plausible:
                continue
            summary = ""
            used_fallback = False
            if generate_summaries:
                previous = (
                    store.get_object(self._conversation_id, record.supersedes)
                    if record.supersedes
                    else None
                )
                self._record_metric(
                    "card_summary_attempts", 1, delta_id=delta.delta_id
                )
                summary, used_fallback = self._summary_generator.generate(
                    engine=self,
                    record=record,
                    contains=contains,
                    previous=previous,
                )
            card_text = render_card(
                build_card(
                    record,
                    summary=summary,
                    contains=contains,
                    origin=origin,
                )
            )
            beneficial, _, _ = benefit_gate(
                raw_span,
                card_text,
                min_absolute_saving_tokens=self.min_absolute_saving_tokens,
                min_relative_saving_ratio=self.min_relative_saving_ratio,
            )
            if not beneficial:
                continue
            accepted.add(object_ref)
            row = dict(occurrence)
            row["card_text"] = card_text
            rows_for_render.append(row)
            card_updates.append((object_ref, summary, card_text, contains))
            if used_fallback:
                self._record_metric("summary_fallbacks", 1, delta_id=delta.delta_id)
        if not accepted:
            return None
        compressed_view = apply_occurrence_cards(
            delta.raw_view, rows_for_render, allowed_refs=accepted
        )
        rendered_tokens = estimate_messages_tokens_rough(compressed_view)
        if rendered_tokens >= delta.raw_token_count:
            return None
        return _PreparedProjection(
            delta=delta,
            cards=tuple(card_updates),
            compressed_view=tuple(compressed_view),
            occurrences=tuple(rows_for_render),
            raw_tokens=delta.raw_token_count,
            projected_tokens=rendered_tokens,
        )

    def _prepare_compressed_delta(
        self, delta: DeltaRecord
    ) -> (
        tuple[
            str,
            list[tuple[str, str, str, dict[str, Any]]],
            list[dict[str, Any]],
        ]
        | None
    ):
        """Compatibility wrapper for explicit/manual legacy call paths."""

        prepared = self._prepare_delta_projection(
            delta, generate_summaries=self.card_summary_enabled
        )
        if prepared is None:
            return None
        return (
            delta.delta_id,
            list(prepared.cards),
            list(prepared.compressed_view),
        )

    def _compress_delta_batch(self, deltas: Sequence[DeltaRecord]) -> None:
        store = self._store
        eligible = [
            delta
            for delta in deltas
            if delta.state
            in {DeltaState.COMPRESSION_ELIGIBLE, DeltaState.COMPRESSION_FAILED}
        ]
        if store is None or not eligible:
            return
        started_at = time.perf_counter()
        try:
            store.set_delta_states({
                delta.delta_id: DeltaState.COMPRESSING for delta in eligible
            })
            batch = []
            for delta in eligible:
                try:
                    prepared = self._prepare_compressed_delta(delta)
                    if prepared is not None:
                        batch.append(prepared)
                except Exception as exc:
                    store.mark_delta_failed(delta.delta_id, str(exc))
                    self._last_failure = f"compression:{exc}"
                    self._record_metric(
                        "compression_failures",
                        1,
                        delta_id=delta.delta_id,
                        metadata={"stage": "prepare_or_validate"},
                    )
            if not batch:
                self._last_batch_size = 0
                return
            try:
                store.publish_compressed_batch(batch)
                self._last_batch_size = len(batch)
                self.compression_count += len(batch)
                self._record_metric("prompt_prefix_rewrite_events", 1)
                self._record_metric("prompt_prefix_rewritten_deltas", len(batch))
                self._run_activity_gc()
            except Exception as exc:
                self._last_failure = f"compression_commit:{exc}"
                for delta_id, _, _ in batch:
                    store.mark_delta_failed(delta_id, str(exc))
                    self._record_metric(
                        "compression_failures",
                        1,
                        delta_id=delta_id,
                        metadata={"stage": "atomic_commit"},
                    )
        finally:
            self._record_metric(
                "compression_latency_ms",
                max(0.0, (time.perf_counter() - started_at) * 1000),
            )

    @staticmethod
    def _identity_match(
        request_message: dict[str, Any], raw_message: dict[str, Any]
    ) -> bool:
        if request_message.get("role") != raw_message.get("role"):
            return False
        matched_durable_field = False
        for key in ("timestamp", "tool_call_id"):
            request_value = request_message.get(key)
            raw_value = raw_message.get(key)
            if request_value is not None or raw_value is not None:
                if request_value != raw_value:
                    return False
                matched_durable_field = True
        request_calls = request_message.get("tool_calls")
        raw_calls = raw_message.get("tool_calls")
        if request_calls is not None or raw_calls is not None:
            if not isinstance(request_calls, list) or not isinstance(raw_calls, list):
                return False
            request_call_ids = tuple(
                str(call.get("id") or "")
                for call in request_calls
                if isinstance(call, dict)
            )
            raw_call_ids = tuple(
                str(call.get("id") or "")
                for call in raw_calls
                if isinstance(call, dict)
            )
            if (
                not request_call_ids
                or request_call_ids != raw_call_ids
                or len(request_call_ids) != len(request_calls)
                or len(raw_call_ids) != len(raw_calls)
            ):
                return False
            matched_durable_field = True
        request_name = request_message.get("tool_name") or request_message.get("name")
        raw_name = raw_message.get("tool_name") or raw_message.get("name")
        if request_name or raw_name:
            return request_name == raw_name and (
                matched_durable_field or bool(request_name)
            )
        return matched_durable_field

    def _project_views(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        identity_messages: Sequence[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]] | None]:
        """Project provider and persisted-history views from one store read."""

        store = self._store
        if store is None or not self._conversation_id or not messages:
            return None, None
        try:
            keys = [
                message_key(message)
                for message in messages
                if isinstance(message, dict)
            ]
            identity_list = [
                message
                for message in (identity_messages or [])
                if isinstance(message, dict)
            ]
            identity_keys = [message_key(message) for message in identity_list]
            compressed_deltas = [
                delta
                for delta in store.list_deltas(self._conversation_id)
                if delta.state == DeltaState.COMPRESSED
                and delta.compressed_view is not None
            ]
            compressed_raw_messages = [
                raw_message
                for delta in compressed_deltas
                for raw_message in delta.raw_view
                if isinstance(raw_message, dict)
            ]
            compressed_raw_keys = [
                message_key(message) for message in compressed_raw_messages
            ]
            occurrences = store.occurrence_cards_for_messages(
                self._conversation_id,
                [*keys, *identity_keys, *compressed_raw_keys],
            )
            # The provider request may substitute a persisted ``api_content``
            # sidecar into a user message. Its clean raw content remains the
            # immutable occurrence identity, while exact spans are still valid
            # because injected material is appended. Alias those rows onto the
            # request copy by durable timestamp/role/tool identity.
            for request_message in [*messages, *identity_list]:
                if not isinstance(request_message, dict):
                    continue
                request_key = message_key(request_message)
                if request_key in occurrences:
                    continue
                for raw_message in compressed_raw_messages:
                    if not self._raw_message_present(request_message, raw_message):
                        continue
                    raw_key = message_key(raw_message)
                    if raw_key in occurrences:
                        aligned = self._aligned_occurrence_rows(
                            request_message,
                            raw_message,
                            occurrences[raw_key],
                        )
                        if aligned is not None:
                            occurrences[request_key] = aligned
                    break
            def render(
                view: Sequence[dict[str, Any]],
            ) -> list[dict[str, Any]] | None:
                projected = project_compressed_messages(view, occurrences)
                for delta in compressed_deltas:
                    retrievals = self._successful_retrievals_for_delta(delta)
                    if not retrievals or delta.compressed_view is None:
                        continue
                    positions = self._matching_delta_positions(delta, view)
                    if positions is None:
                        continue
                    for ordinal, _ in retrievals:
                        if ordinal >= len(delta.compressed_view):
                            raise ValueError(
                                "retrieval projection ordinal is out of bounds"
                            )
                        target = positions[ordinal]
                        projected[target]["content"] = copy.deepcopy(
                            delta.compressed_view[ordinal].get("content")
                        )
                return None if projected == list(view) else projected

            return (
                render(messages),
                render(identity_list) if identity_messages is not None else None,
            )
        except Exception as exc:
            self._last_failure = f"renderer:{exc}"
            self._record_metric(
                "compression_failures", 1, metadata={"stage": "renderer"}
            )
            logger.warning(
                "Object Context render failed; using immutable raw context",
                exc_info=True,
            )
            return None, None

    def _project(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        identity_messages: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]] | None:
        projected, _ = self._project_views(
            messages, identity_messages=identity_messages
        )
        return projected

    @staticmethod
    def _normalized_tool_calls(
        calls: Any,
    ) -> tuple[tuple[str, str, str, str], ...] | None:
        """Normalize transport-only tool-call differences for raw exposure.

        Persisted provider rows may retain response item ids and spaced JSON,
        while the outbound chat-completions adapter sends only the callable
        fields and minifies the same arguments. Those are the same raw model
        decision; ids, order, type, function name, and argument value remain
        mandatory.
        """

        if not isinstance(calls, list):
            return None
        normalized: list[tuple[str, str, str, str]] = []
        for call in calls:
            if not isinstance(call, dict):
                return None
            function = call.get("function")
            if not isinstance(function, dict):
                return None
            arguments = function.get("arguments", "")
            if isinstance(arguments, str):
                try:
                    normalized_arguments = canonical_json(json.loads(arguments))
                except (TypeError, ValueError, json.JSONDecodeError):
                    normalized_arguments = arguments
            else:
                normalized_arguments = canonical_json(arguments)
            normalized.append(
                (
                    str(call.get("id") or call.get("call_id") or ""),
                    str(call.get("type") or "function"),
                    str(function.get("name") or ""),
                    normalized_arguments,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _raw_text_part_alignment(
        request_message: dict[str, Any],
        raw_message: dict[str, Any],
    ) -> dict[int, tuple[int, int, int]] | None:
        """Map durable Raw text coordinates onto the transport-facing copy.

        The host canonicalizes a top-level string with ``strip()`` before OC
        selection, while request-only sidecars may append text after the
        durable payload.  Occurrence coordinates remain anchored to the
        immutable Raw message, so presence and rendering must share an
        explicit mapping instead of assuming identical strings.  Every
        accepted mapping is a contiguous, byte-for-byte slice: either the
        complete Raw part or that part with outer whitespace removed.

        Values are ``(raw_visible_start, raw_visible_end, request_start)``.
        Returning ``None`` is conservative: the message is not treated as Raw
        when a transport transform changed payload bytes rather than merely
        trimming its outer whitespace or appending a sidecar.
        """

        raw_content = raw_message.get("content")
        request_content = request_message.get("content")
        if isinstance(raw_content, list):
            # The host does not top-level-strip multimodal/list content.  Keep
            # every durable non-text part and text carrier byte-identical so a
            # matching timestamp cannot alias a changed image, filename, MIME
            # type, or reordered part onto occurrence coordinates. Request-only
            # MoA guidance may append trailing *text* parts after that exact
            # prefix; a newly appended image/file is a payload change and must
            # fail closed rather than inherit the durable message identity.
            if (
                not isinstance(request_content, list)
                or len(request_content) < len(raw_content)
                or request_content[: len(raw_content)] != raw_content
            ):
                return None
            for sidecar in request_content[len(raw_content) :]:
                if isinstance(sidecar, str):
                    continue
                if (
                    isinstance(sidecar, dict)
                    and str(sidecar.get("type") or "") in {"text", "input_text"}
                    and isinstance(sidecar.get("text"), str)
                ):
                    continue
                return None
            return {
                part.ordinal: (0, len(part.text), 0)
                for part in iter_text_parts(raw_content)
            }
        if not isinstance(raw_content, str):
            return {} if request_content == raw_content else None
        if not isinstance(request_content, str):
            return None
        if request_content.startswith(raw_content):
            return {0: (0, len(raw_content), 0)}
        raw_left = len(raw_content) - len(raw_content.lstrip())
        raw_right = len(raw_content.rstrip())
        if raw_right <= raw_left:
            return None
        visible = raw_content[raw_left:raw_right]
        if not request_content.startswith(visible):
            return None
        return {0: (raw_left, raw_right, 0)}

    @classmethod
    def _aligned_occurrence_rows(
        cls,
        request_message: dict[str, Any],
        raw_message: dict[str, Any],
        rows: Sequence[dict[str, Any]],
    ) -> list[dict[str, Any]] | None:
        """Translate immutable occurrence spans into request coordinates."""

        alignment = cls._raw_text_part_alignment(request_message, raw_message)
        if alignment is None:
            return None
        raw_parts = {
            part.ordinal: part.text
            for part in iter_text_parts(raw_message.get("content"))
        }
        request_parts = {
            part.ordinal: part.text
            for part in iter_text_parts(request_message.get("content"))
        }
        translated: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            part_ordinal = int(row.get("part_ordinal") or 0)
            bounds = alignment.get(part_ordinal)
            if bounds is None:
                return None
            raw_left, raw_right, request_start = bounds
            raw_start = int(row.get("span_start") or 0)
            raw_end = int(row.get("span_end") or 0)
            visible_start = max(raw_start, raw_left)
            visible_end = min(raw_end, raw_right)
            if visible_end <= visible_start:
                return None
            mapped_start = request_start + visible_start - raw_left
            mapped_end = request_start + visible_end - raw_left
            raw_text = raw_parts.get(part_ordinal, "")
            request_text = request_parts.get(part_ordinal, "")
            if (
                mapped_start < 0
                or mapped_end > len(request_text)
                or request_text[mapped_start:mapped_end]
                != raw_text[visible_start:visible_end]
            ):
                return None
            row["span_start"] = mapped_start
            row["span_end"] = mapped_end
            translated.append(row)
        return translated

    @classmethod
    def _raw_message_present(
        cls,
        request_message: dict[str, Any],
        raw_message: dict[str, Any],
    ) -> bool:
        """Whether one immutable raw message survives in a selected request."""

        if message_key(request_message) == message_key(raw_message):
            return True
        if not cls._identity_match(request_message, raw_message):
            return False
        if cls._raw_text_part_alignment(request_message, raw_message) is None:
            return False
        if raw_message.get("tool_calls") is not None:
            request_calls = cls._normalized_tool_calls(
                request_message.get("tool_calls")
            )
            raw_calls = cls._normalized_tool_calls(raw_message.get("tool_calls"))
            if request_calls is None or request_calls != raw_calls:
                return False
        return True

    @classmethod
    def _raw_delta_present(
        cls,
        delta: DeltaRecord,
        request_messages: Sequence[dict[str, Any]],
    ) -> bool:
        """Require the Delta's entire causal message group in order and raw."""

        return cls._matching_delta_positions(delta, request_messages) is not None

    @classmethod
    def _matching_delta_positions(
        cls,
        delta: DeltaRecord,
        request_messages: Sequence[dict[str, Any]],
    ) -> tuple[int, ...] | None:
        """Locate a complete raw causal group inside one assembled request."""

        cursor = 0
        positions: list[int] = []
        for raw_message in delta.raw_view:
            for index in range(cursor, len(request_messages)):
                request_message = request_messages[index]
                if not isinstance(request_message, dict):
                    continue
                if cls._raw_message_present(request_message, raw_message):
                    positions.append(index)
                    cursor = index + 1
                    break
            else:
                return None
        return tuple(positions)

    def _snapshot_raw_exposure(
        self,
        selected_messages: Sequence[dict[str, Any]],
        *,
        request_attempt_id: str | None = None,
    ) -> None:
        """Replace the attempted-request snapshot used by the next verdict."""

        store = self._store
        if store is None or not self._conversation_id:
            self._pending_raw_exposure = None
            return
        self._request_sequence += 1
        attempt_id = (
            str(request_attempt_id) if request_attempt_id else str(uuid.uuid4())
        )
        try:
            raw_ids = tuple(
                delta.delta_id
                for delta in store.list_deltas(self._conversation_id)
                if delta.state != DeltaState.COMPRESSED
                and delta.compressed_view is None
                and not delta.projection_epoch_id
                and self._raw_delta_present(delta, selected_messages)
            )
        except Exception:
            raw_ids = ()
            logger.warning(
                "Object Context raw-exposure selection failed; "
                "request remains usable and no exposure is assumed; the "
                "attempt fence is retained",
                exc_info=True,
            )
        pending_gains: tuple[tuple[str, int, int], ...] = ()
        if self.scheduler == "amortized_batch":
            try:
                pending_gains = tuple(
                    (
                        ledger.delta_id,
                        ledger.gain_tokens,
                        ledger.ledger_generation,
                    )
                    for ledger in store.list_pending_ledgers(
                        self._conversation_id,
                        delta_ids=raw_ids,
                    )
                    if ledger.delta_id in self._active_pending_snapshot_ids
                )
            except Exception:
                logger.warning(
                    "Object Context Pending snapshot failed; this request will "
                    "not accrue confirmed waiting area",
                    exc_info=True,
                )
        self._pending_raw_exposure = _RequestObservationSnapshot(
            request_attempt_id=attempt_id,
            conversation_id=self._conversation_id,
            selection_sequence=self._request_sequence,
            raw_delta_ids=raw_ids,
            pending_gains=pending_gains,
            selected_view=tuple(copy.deepcopy(list(selected_messages))),
            route_namespace_hash=self._cache_namespace_hash,
            store_path=str(store.path),
        )

    def _cache_granularity_tokens(self) -> int:
        provider = str(getattr(self, "provider", "") or "").casefold()
        if provider in {"openai", "openai-api", "openai-codex"}:
            return 128
        if provider in {"anthropic", "bedrock"}:
            return 1024
        if provider in {"custom", "local"}:
            return 1
        # Unknown hosted routes must not overstate a partial cache block.
        return 1024

    def _rough_lcp_tokens(
        self,
        previous: Sequence[dict[str, Any]],
        current: Sequence[dict[str, Any]],
    ) -> int:
        previous_text = canonical_json(list(previous))
        current_text = canonical_json(list(current))
        limit = min(len(previous_text), len(current_text))
        cursor = 0
        while cursor < limit and previous_text[cursor] == current_text[cursor]:
            cursor += 1
        rough = min(
            estimate_tokens_rough(current_text[:cursor]),
            estimate_messages_tokens_rough(list(previous)),
            estimate_messages_tokens_rough(list(current)),
        )
        return round_reusable_prefix(
            rough,
            self._cache_granularity_tokens(),
        )

    def _economic_pricing_weights(self) -> PricingWeights:
        """Resolve technical cache ratios, falling back without zeroing work."""

        entry = None
        try:
            from agent.usage_pricing import get_pricing_entry

            provider = str(getattr(self, "provider", "") or "")
            lookup_provider = "openai" if provider == "openai-codex" else provider
            lookup_base_url = (
                str(getattr(self, "base_url", "") or "")
                if lookup_provider.casefold()
                in {"custom", "local", "openrouter", "nous"}
                else ""
            )
            entry = get_pricing_entry(
                str(getattr(self, "model", "") or ""),
                provider=lookup_provider,
                base_url=lookup_base_url,
                api_key=str(getattr(self, "api_key", "") or ""),
            )
        except Exception:
            logger.debug(
                "Object Context technical pricing lookup failed; using fallback",
                exc_info=True,
            )
        if entry is None:
            return resolve_pricing_weights(
                uncached_input_price=None,
                cache_read_price=None,
                cache_write_price=None,
                fallback_cache_read=self.economic_cache_read_ratio_fallback,
                fallback_cache_write=self.economic_cache_write_ratio_fallback,
            )
        return resolve_pricing_weights(
            uncached_input_price=(
                float(entry.input_cost_per_million)
                if entry.input_cost_per_million is not None
                else None
            ),
            cache_read_price=(
                float(entry.cache_read_cost_per_million)
                if entry.cache_read_cost_per_million is not None
                else None
            ),
            cache_write_price=(
                float(entry.cache_write_cost_per_million)
                if entry.cache_write_cost_per_million is not None
                else None
            ),
            source=str(entry.source or "pricing_entry"),
            version=str(entry.pricing_version or ""),
            fallback_cache_read=self.economic_cache_read_ratio_fallback,
            fallback_cache_write=self.economic_cache_write_ratio_fallback,
        )

    def _aux_card_summary_snapshot(self) -> dict[str, float]:
        getter = getattr(
            getattr(self, "_session_db", None),
            "auxiliary_usage_breakdown",
            None,
        )
        if not callable(getter) or not self._object_session_id:
            return {}
        try:
            rows = getter([self._object_session_id])
        except Exception:
            return {}
        for row in rows or []:
            if row.get("task") != "object_context_card_summary":
                continue
            return {
                key: float(row.get(key) or 0)
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "estimated_cost_usd",
                )
            }
        return {}

    @staticmethod
    def _summary_cost_equivalent_tokens(
        before: dict[str, float],
        after: dict[str, float],
        pricing: PricingWeights,
    ) -> float:
        cost_delta = max(
            0.0,
            after.get("estimated_cost_usd", 0.0)
            - before.get("estimated_cost_usd", 0.0),
        )
        if cost_delta and pricing.uncached_input_usd_per_token:
            return cost_delta / pricing.uncached_input_usd_per_token
        return max(
            0.0,
            after.get("input_tokens", 0.0) - before.get("input_tokens", 0.0),
        ) + max(
            0.0,
            after.get("cache_read_tokens", 0.0)
            - before.get("cache_read_tokens", 0.0),
        ) * pricing.cache_read + max(
            0.0,
            after.get("cache_write_tokens", 0.0)
            - before.get("cache_write_tokens", 0.0),
        ) * pricing.cache_write + max(
            0.0,
            after.get("output_tokens", 0.0) - before.get("output_tokens", 0.0),
        ) + max(
            0.0,
            after.get("reasoning_tokens", 0.0)
            - before.get("reasoning_tokens", 0.0),
        )

    def _earliest_prepared_offset(
        self,
        request_messages: Sequence[dict[str, Any]],
        prepared: _PreparedProjection,
    ) -> int | None:
        if prepared.occurrences:
            earliest = min(
                prepared.occurrences,
                key=lambda row: (
                    int(row.get("message_ordinal") or 0),
                    int(row.get("part_ordinal") or 0),
                    int(row.get("span_start") or 0),
                ),
            )
            message_ordinal = int(earliest.get("message_ordinal") or 0)
        elif prepared.content_replacements:
            message_ordinal = min(
                ordinal for ordinal, _ in prepared.content_replacements
            )
        else:
            return None
        raw_message = prepared.delta.raw_view[message_ordinal]
        for index, request_message in enumerate(request_messages):
            if isinstance(request_message, dict) and self._raw_message_present(
                request_message, raw_message
            ):
                # Message-boundary accounting is conservative: it never claims
                # the stable intra-message prefix before the changed object.
                return estimate_messages_tokens_rough(list(request_messages[:index]))
        return None

    def _render_prepared_batch(
        self,
        request_messages: Sequence[dict[str, Any]],
        prepared_batch: Sequence[_PreparedProjection],
    ) -> list[dict[str, Any]]:
        occurrences: dict[str, list[dict[str, Any]]] = {}
        for prepared in prepared_batch:
            for row in prepared.occurrences:
                raw_message = prepared.delta.raw_view[
                    int(row.get("message_ordinal") or 0)
                ]
                occurrences.setdefault(message_key(raw_message), []).append(dict(row))
        for request_message in request_messages:
            if not isinstance(request_message, dict):
                continue
            request_key = message_key(request_message)
            if request_key in occurrences:
                continue
            for prepared in prepared_batch:
                for raw_message in prepared.delta.raw_view:
                    if self._raw_message_present(request_message, raw_message):
                        raw_key = message_key(raw_message)
                        if raw_key in occurrences:
                            aligned = self._aligned_occurrence_rows(
                                request_message,
                                raw_message,
                                occurrences[raw_key],
                            )
                            if aligned is not None:
                                occurrences[request_key] = aligned
                        break
        rendered = project_compressed_messages(request_messages, occurrences)
        for prepared in prepared_batch:
            if not prepared.content_replacements:
                continue
            positions = self._matching_delta_positions(
                prepared.delta, request_messages
            )
            if positions is None:
                continue
            for ordinal, content in prepared.content_replacements:
                rendered[positions[ordinal]]["content"] = copy.deepcopy(content)
        return rendered

    def _discover_economic_candidates(
        self,
        baseline_request: Sequence[dict[str, Any]],
        *,
        include_skipped: bool = False,
    ) -> tuple[
        dict[str, _PreparedProjection],
        tuple[PreparedCandidate, ...],
        int,
    ]:
        store = self._store
        if store is None:
            return {}, (), 0
        prepared_by_id: dict[str, _PreparedProjection] = {}
        candidates: list[PreparedCandidate] = []
        unseen = 0
        allowed_states = {
            DeltaState.HOT,
            DeltaState.COMPRESSION_ELIGIBLE,
            DeltaState.COMPRESSION_FAILED,
        }
        if include_skipped:
            allowed_states.add(DeltaState.COMPRESSION_SKIPPED)
        for delta in store.list_deltas(self._conversation_id):
            known_retrieval = bool(self._successful_retrievals_for_delta(delta))
            if (
                delta.state not in allowed_states
                or delta.compressed_view is not None
                or delta.projection_epoch_id
                or (not delta.object_refs and not known_retrieval)
                or not self._raw_delta_present(delta, baseline_request)
            ):
                continue
            if delta.raw_seen_count < self.min_raw_exposures:
                unseen += 1
                continue
            try:
                prepared = self._prepare_delta_projection(
                    delta, generate_summaries=False
                )
            except Exception as exc:
                store.mark_delta_failed(delta.delta_id, str(exc))
                self._last_failure = f"economic_candidate_prepare:{exc}"
                self._record_metric(
                    "compression_failures",
                    1,
                    delta_id=delta.delta_id,
                    metadata={"stage": "economic_candidate_prepare"},
                )
                continue
            if prepared is None:
                continue
            offset = self._earliest_prepared_offset(baseline_request, prepared)
            if offset is None:
                continue
            prepared_by_id[delta.delta_id] = prepared
            candidates.append(
                PreparedCandidate(
                    delta_id=delta.delta_id,
                    sequence=delta.global_sequence,
                    raw_tokens=prepared.raw_tokens,
                    projected_tokens=prepared.projected_tokens,
                    earliest_change_token_offset=offset,
                    object_refs=prepared.object_refs,
                )
            )
        return prepared_by_id, tuple(candidates), unseen

    def _delta_start_token_offset(
        self,
        baseline_request: Sequence[dict[str, Any]],
        delta: DeltaRecord,
    ) -> int | None:
        positions = self._matching_delta_positions(delta, baseline_request)
        if not positions:
            return None
        return estimate_messages_tokens_rough(list(baseline_request[: positions[0]]))

    def _rebalance_amortized_pending(
        self,
        baseline_request: Sequence[dict[str, Any]],
    ) -> tuple[
        dict[str, _PreparedProjection],
        tuple[PreparedCandidate, ...],
        HotTailPartition,
        tuple[PendingLedgerRecord, ...],
        int,
    ]:
        """Rebuild the active Hot/Pending partition for one rendered Q0."""

        store = self._store
        baseline_tokens = estimate_messages_tokens_rough(list(baseline_request))
        empty_partition = partition_hot_tail(
            (),
            latest_success_sequence=max(0, self._last_success_sequence),
            baseline_prompt_tokens=baseline_tokens,
            hot_bucket_limit=self.hot_tail_max_inferences,
            hot_token_limit=self.hot_tail_max_tokens,
        )
        if store is None or not self._conversation_id:
            return {}, (), empty_partition, (), 0

        prepared_by_id, all_candidates, unseen = (
            self._discover_economic_candidates(
                baseline_request,
                include_skipped=True,
            )
        )
        try:
            self._last_success_sequence = max(
                self._last_success_sequence,
                store.latest_success_sequence(self._conversation_id),
            )
        except Exception:
            logger.warning(
                "Object Context V1.2 durable success-boundary refresh failed; "
                "using the conservative in-memory watermark",
                exc_info=True,
            )
        deltas = [
            delta
            for delta in store.list_deltas(self._conversation_id)
            if delta.state != DeltaState.COMPRESSED
            and delta.compressed_view is None
            and not delta.projection_epoch_id
            and self._raw_delta_present(delta, baseline_request)
        ]
        raw_ids = tuple(delta.delta_id for delta in deltas)
        existing_ledgers = {
            ledger.delta_id: ledger
            for ledger in store.list_pending_ledgers(
                self._conversation_id,
                delta_ids=raw_ids,
            )
        }
        # A configured exposure-threshold increase invalidates any older
        # eligibility boundary. Re-protect the Delta and delete its ledger in
        # one transaction; the next successful Raw carry that reaches the new
        # threshold assigns a fresh boundary.
        threshold_reconciled = False
        for delta in deltas:
            if delta.raw_seen_count >= self.min_raw_exposures:
                continue
            if (
                delta.eligibility_success_sequence is None
                and delta.delta_id not in existing_ledgers
            ):
                continue
            try:
                threshold_reconciled = (
                    store.reset_delta_eligibility_if_underexposed(
                        conversation_id=self._conversation_id,
                        delta_id=delta.delta_id,
                        min_raw_exposures=self.min_raw_exposures,
                    )
                    or threshold_reconciled
                )
            except Exception:
                logger.warning(
                    "Object Context V1.2 exposure-threshold reconciliation failed; "
                    "keeping the Delta protected",
                    exc_info=True,
                )
                existing_ledgers.pop(delta.delta_id, None)
        if threshold_reconciled:
            deltas = [
                delta
                for delta in store.list_deltas(self._conversation_id)
                if delta.state != DeltaState.COMPRESSED
                and delta.compressed_view is None
                and not delta.projection_epoch_id
                and self._raw_delta_present(delta, baseline_request)
            ]
            raw_ids = tuple(delta.delta_id for delta in deltas)
            existing_ledgers = {
                ledger.delta_id: ledger
                for ledger in store.list_pending_ledgers(
                    self._conversation_id,
                    delta_ids=raw_ids,
                )
            }
        # The V4 success clock is dense and independent of legacy request
        # watermarks. Migrated seen Deltas remain protected until their next
        # real V4 Raw observation assigns an eligibility boundary.
        effective_latest = self._last_success_sequence
        entries: list[TailDelta] = []
        delta_by_id = {delta.delta_id: delta for delta in deltas}
        for delta_id in tuple(existing_ledgers):
            delta = delta_by_id.get(delta_id)
            if delta is None:
                continue
            underexposed = delta.raw_seen_count < self.min_raw_exposures
            permanently_nonprojectable = (
                delta_id not in prepared_by_id
                and delta.state != DeltaState.COMPRESSION_FAILED
            )
            if not underexposed and not permanently_nonprojectable:
                continue
            try:
                store.retire_pending_ledger(
                    conversation_id=self._conversation_id,
                    delta_id=delta_id,
                )
                existing_ledgers.pop(delta_id, None)
            except Exception:
                logger.warning(
                    "Object Context V1.2 Pending retirement failed; "
                    "keeping the candidate out of this request snapshot",
                    exc_info=True,
                )
                prepared_by_id.pop(delta_id, None)
                existing_ledgers.pop(delta_id, None)
        for delta in deltas:
            offset = self._delta_start_token_offset(baseline_request, delta)
            if offset is None:
                continue
            eligibility = (
                None
                if (
                    delta.raw_seen_count < self.min_raw_exposures
                    or delta.eligibility_success_sequence is None
                )
                else delta.eligibility_success_sequence
            )
            entries.append(
                TailDelta(
                    delta_id=delta.delta_id,
                    global_sequence=delta.global_sequence,
                    start_token_offset=offset,
                    eligibility_success_sequence=eligibility,
                    projectable=delta.delta_id in prepared_by_id,
                    already_pending=delta.delta_id in existing_ledgers,
                )
            )
        partition = partition_hot_tail(
            entries,
            latest_success_sequence=effective_latest,
            baseline_prompt_tokens=baseline_tokens,
            hot_bucket_limit=self.hot_tail_max_inferences,
            hot_token_limit=self.hot_tail_max_tokens,
        )
        prospective = partition.prospective_success_sequence
        for delta_id in partition.promoted_delta_ids:
            prepared = prepared_by_id.get(delta_id)
            delta = delta_by_id.get(delta_id)
            if prepared is None or delta is None:
                continue
            eligibility = delta.eligibility_success_sequence
            if eligibility is None or effective_latest < 1:
                continue
            age_cold = prospective - eligibility >= self.hot_tail_max_inferences
            try:
                ledger = store.promote_pending_delta(
                    conversation_id=self._conversation_id,
                    delta_id=delta_id,
                    entered_success_sequence=effective_latest,
                    bucket_sequence=eligibility,
                    raw_tokens=prepared.raw_tokens,
                    projected_tokens=prepared.projected_tokens,
                    gain_tokens=max(
                        0, prepared.raw_tokens - prepared.projected_tokens
                    ),
                    estimator_version="rough_message_estimator:v1.2",
                    pending_reason=(
                        "hot_inference_limit" if age_cold else "hot_token_limit"
                    ),
                    min_raw_exposures=self.min_raw_exposures,
                )
                existing_ledgers[delta_id] = ledger
            except Exception:
                logger.warning(
                    "Object Context V1.2 Pending promotion failed; keeping Raw",
                    exc_info=True,
                )

        # A Pending estimate is a durable accounting contract.  Re-running the
        # deterministic projector should normally reproduce it byte-for-byte;
        # if a parser/configuration/retrieval change legitimately alters the
        # estimate, start a new ledger generation instead of applying a stale
        # gain to future successful requests.
        for delta_id, ledger in tuple(existing_ledgers.items()):
            prepared = prepared_by_id.get(delta_id)
            delta = delta_by_id.get(delta_id)
            if prepared is None or delta is None:
                continue
            gain = max(0, prepared.raw_tokens - prepared.projected_tokens)
            if (
                ledger.raw_tokens == prepared.raw_tokens
                and ledger.projected_tokens == prepared.projected_tokens
                and ledger.gain_tokens == gain
            ):
                continue
            try:
                existing_ledgers[delta_id] = store.upsert_pending_ledger(
                    conversation_id=self._conversation_id,
                    delta_id=delta_id,
                    entered_success_sequence=ledger.entered_success_sequence,
                    bucket_sequence=ledger.bucket_sequence,
                    raw_tokens=prepared.raw_tokens,
                    projected_tokens=prepared.projected_tokens,
                    gain_tokens=gain,
                    estimator_version="rough_message_estimator:v1.2",
                    pending_reason=ledger.pending_reason,
                    min_raw_exposures=self.min_raw_exposures,
                    reset_wait_area=True,
                )
            except Exception:
                logger.warning(
                    "Object Context V1.2 Pending estimate refresh failed; "
                    "excluding the stale candidate from this boundary",
                    exc_info=True,
                )
                prepared_by_id.pop(delta_id, None)

        active_ledgers = tuple(
            ledger
            for ledger in store.list_pending_ledgers(
                self._conversation_id,
                delta_ids=raw_ids,
            )
            if ledger.delta_id in prepared_by_id
        )
        self._active_pending_snapshot_ids = frozenset(
            ledger.delta_id for ledger in active_ledgers
        )
        self._last_hot_partition = partition
        self._last_hot_tail_tokens = partition.hot_tokens
        return (
            prepared_by_id,
            all_candidates,
            partition,
            active_ledgers,
            unseen,
        )

    def _amortized_pricing_weights(self) -> PricingWeights:
        """Return the fixed V1.2 policy weights in one stable unit."""

        return PricingWeights(
            cache_read=self.amortized_cache_read_weight,
            cache_write=1.0,
            source="amortized_fixed",
            version="1.2",
        )

    def _exact_threshold_crossed(self, score: Any) -> bool:
        if (
            score.net_saving_equivalent_tokens
            < self.economic_min_net_saving_tokens
        ):
            return False
        return not (
            self.economic_min_net_saving_usd is not None
            and score.net_saving_usd is not None
            and score.net_saving_usd < self.economic_min_net_saving_usd
        )

    @staticmethod
    def _batch_projection_became_durable(
        store: ObjectContextStore,
        delta_ids: Iterable[str],
    ) -> bool:
        """Whether a losing planner must discard its stale pre-CAS Q0 view.

        Another engine can publish the same conversation between our render
        and atomic commit.  A failed CAS is then not an ordinary local failure:
        sending the already-built Raw Q0 would ignore the winner's durable Card
        epoch.  Detect any newly projected member and force select_context() to
        render the latest store view before the provider request is sent.
        """

        try:
            for delta_id in delta_ids:
                delta = store.get_delta(str(delta_id))
                if delta is not None and (
                    delta.state == DeltaState.COMPRESSED
                    or delta.compressed_view is not None
                    or bool(delta.projection_epoch_id)
                ):
                    return True
        except Exception:
            logger.debug(
                "Object Context concurrent projection check failed",
                exc_info=True,
            )
        return False

    def _projection_decision_record(
        self,
        decision: EconomicDecision,
        pricing: PricingWeights,
        *,
        projection_epoch_id: str | None = None,
        card_or_receipt_tokens: int | None = None,
        decision_mode: str | None = None,
    ) -> dict[str, Any]:
        score = decision.winner
        record = {
            "projection_epoch_id": projection_epoch_id
            or f"decision_{uuid.uuid4().hex}",
            "conversation_id": self._conversation_id,
            "session_id": self._object_session_id,
            "request_sequence": self._request_sequence + 1,
            "decision_kind": decision.decision_kind,
            "decision_mode": decision_mode or (
                "emergency"
                if decision.decision_kind == "emergency"
                else "normal"
            ),
            "decision_reason": decision.decision_reason,
            "candidate_count": decision.candidate_count,
            "member_delta_ids": score.member_delta_ids if score else (),
            "member_object_refs": score.member_object_refs if score else (),
            "earliest_changed_delta_id": (
                score.earliest_changed_delta_id if score else ""
            ),
            "baseline_prompt_tokens": (
                score.baseline_prompt_tokens if score else None
            ),
            "candidate_prompt_tokens": (
                score.candidate_prompt_tokens if score else None
            ),
            "gross_tokens_removed": score.gross_tokens_removed if score else None,
            "card_or_receipt_tokens": (
                max(0, int(card_or_receipt_tokens))
                if card_or_receipt_tokens is not None
                else None
            ),
            "baseline_reusable_prefix_tokens": (
                score.baseline_reusable_prefix_tokens if score else None
            ),
            "candidate_reusable_prefix_tokens": (
                score.candidate_reusable_prefix_tokens if score else None
            ),
            "cache_tokens_invalidated": (
                score.cache_tokens_invalidated if score else None
            ),
            "cache_penalty_equivalent_tokens": (
                score.cache_penalty_equivalent_tokens if score else None
            ),
            "known_summary_cost_equivalent_tokens": (
                score.known_summary_cost_equivalent_tokens if score else None
            ),
            "net_saving_equivalent_tokens": (
                score.net_saving_equivalent_tokens if score else None
            ),
            "net_saving_usd": score.net_saving_usd if score else None,
            "cache_read_weight": pricing.cache_read,
            "cache_write_weight": pricing.cache_write,
            "pricing_source": pricing.source,
            "pricing_version": pricing.version,
            "estimator_source": decision.estimator_source,
            "request_attempt_id": (
                self._planning_request_attempt_id or None
            ),
        }
        # Rows written after the V1.2 schema upgrade are explicitly labelled;
        # this does not reinterpret historical V1.1 rows, whose nullable
        # version remains empty after migration.
        record.update({
            "policy_version": (
                "1.2" if self.scheduler == "amortized_batch" else "1.1"
            ),
            "batch_policy": (
                self.batch_policy
                if self.scheduler == "amortized_batch"
                else None
            ),
            "fixed_batch_size": (
                self.fixed_batch_size
                if self.scheduler == "amortized_batch"
                else None
            ),
            "baseline_state": self._cache_baseline_state,
            "emergency_triggered": (
                decision_mode == "emergency"
                or decision.decision_kind == "emergency"
            ),
            "cache_granularity_tokens": self._cache_granularity_tokens(),
        })
        amortized_context = self._amortized_telemetry_context
        if amortized_context is not None:
            (
                amortized_decision,
                partition,
                policy_score,
                immediate_score,
                immediate_pricing,
                immediate_crossed,
            ) = amortized_context
            policy_pricing = self._amortized_pricing_weights()
            fixed_score = None
            if score is not None:
                fixed_score = score_exact_batch(
                    member_delta_ids=score.member_delta_ids,
                    member_object_refs=score.member_object_refs,
                    earliest_changed_delta_id=score.earliest_changed_delta_id,
                    baseline_prompt_tokens=score.baseline_prompt_tokens,
                    candidate_prompt_tokens=score.candidate_prompt_tokens,
                    baseline_reusable_prefix_tokens=(
                        score.baseline_reusable_prefix_tokens
                    ),
                    candidate_reusable_prefix_tokens=(
                        score.candidate_reusable_prefix_tokens
                    ),
                    pricing=policy_pricing,
                    known_summary_cost_equivalent_tokens=(
                        score.known_summary_cost_equivalent_tokens
                    ),
                )
            elif policy_score is not None:
                # The emergency planner can legitimately report no legal
                # winner.  Persist the actual no-op request Q0→Q0 rather than
                # leaving a purported V1.2 row without recomputable score
                # facts.  The separate amortized_* fields below still describe
                # the all-Pending policy counterfactual Q0→Qc.
                fixed_score = score_exact_batch(
                    member_delta_ids=(),
                    member_object_refs=(),
                    earliest_changed_delta_id="",
                    baseline_prompt_tokens=policy_score.baseline_prompt_tokens,
                    candidate_prompt_tokens=policy_score.baseline_prompt_tokens,
                    baseline_reusable_prefix_tokens=(
                        policy_score.baseline_reusable_prefix_tokens
                    ),
                    candidate_reusable_prefix_tokens=(
                        policy_score.baseline_reusable_prefix_tokens
                    ),
                    pricing=policy_pricing,
                )
            if fixed_score is not None:
                record.update({
                    "member_delta_ids": fixed_score.member_delta_ids,
                    "member_object_refs": fixed_score.member_object_refs,
                    "earliest_changed_delta_id": (
                        fixed_score.earliest_changed_delta_id
                    ),
                    "baseline_prompt_tokens": fixed_score.baseline_prompt_tokens,
                    "candidate_prompt_tokens": fixed_score.candidate_prompt_tokens,
                    "gross_tokens_removed": fixed_score.gross_tokens_removed,
                    "baseline_reusable_prefix_tokens": (
                        fixed_score.baseline_reusable_prefix_tokens
                    ),
                    "candidate_reusable_prefix_tokens": (
                        fixed_score.candidate_reusable_prefix_tokens
                    ),
                    "cache_tokens_invalidated": (
                        fixed_score.cache_tokens_invalidated
                    ),
                    "cache_penalty_equivalent_tokens": (
                        fixed_score.cache_penalty_equivalent_tokens
                    ),
                    "net_saving_equivalent_tokens": (
                        fixed_score.net_saving_equivalent_tokens
                    ),
                    "net_saving_usd": fixed_score.net_saving_usd,
                    "known_summary_cost_equivalent_tokens": (
                        fixed_score.known_summary_cost_equivalent_tokens
                    ),
                })
            record.update({
                "cache_read_weight": policy_pricing.cache_read,
                "cache_write_weight": policy_pricing.cache_write,
                "pricing_source": policy_pricing.source,
                "pricing_version": policy_pricing.version,
                "amortized_baseline_prompt_tokens": (
                    policy_score.baseline_prompt_tokens
                    if policy_score is not None
                    else None
                ),
                "amortized_candidate_prompt_tokens": (
                    policy_score.candidate_prompt_tokens
                    if policy_score is not None
                    else None
                ),
                "amortized_baseline_reusable_prefix_tokens": (
                    policy_score.baseline_reusable_prefix_tokens
                    if policy_score is not None
                    else None
                ),
                "amortized_candidate_reusable_prefix_tokens": (
                    policy_score.candidate_reusable_prefix_tokens
                    if policy_score is not None
                    else None
                ),
                "immediate_cache_penalty_equivalent_tokens": (
                    immediate_score.cache_penalty_equivalent_tokens
                    if immediate_score is not None
                    else None
                ),
                "immediate_net_saving_equivalent_tokens": (
                    immediate_score.net_saving_equivalent_tokens
                    if immediate_score is not None
                    else None
                ),
                "immediate_net_saving_usd": (
                    immediate_score.net_saving_usd
                    if immediate_score is not None
                    else None
                ),
                "immediate_cache_read_weight": immediate_pricing.cache_read,
                "immediate_cache_write_weight": immediate_pricing.cache_write,
                "immediate_pricing_source": immediate_pricing.source,
                "immediate_pricing_version": immediate_pricing.version,
            })
            record.update(
                self._amortized_decision_telemetry_fields(
                    amortized_decision,
                    partition,
                    immediate_crossed=immediate_crossed,
                )
            )
        return record

    def _amortized_decision_telemetry_fields(
        self,
        decision: AmortizedDecision,
        partition: HotTailPartition,
        *,
        immediate_crossed: bool,
    ) -> dict[str, Any]:
        """Return the V1.2-only portion shared by every decision mode."""

        return {
            "policy_version": "1.2",
            "baseline_state": self._cache_baseline_state,
            "hot_underexposed_count": partition.underexposed_delta_count,
            "hot_seen_delta_count": partition.seen_hot_delta_count,
            "hot_seen_bucket_count": partition.seen_hot_bucket_count,
            "hot_tail_tokens": partition.hot_tokens,
            "hot_overflow_tokens": partition.hot_overflow_tokens,
            "hot_start_token_offset": partition.hot_start_token_offset,
            "pending_delta_count": decision.pending_delta_count,
            "pending_bucket_count": decision.pending_bucket_count,
            "pending_raw_tokens": decision.pending_raw_tokens,
            "pending_gain_tokens": decision.pending_gain_tokens,
            "wait_area_token_requests": decision.total_wait_area,
            "wait_loss_now": decision.wait_loss_now,
            "wait_loss_increment": decision.wait_loss_increment,
            "wait_loss_projected": decision.wait_loss_projected,
            "shared_cached_hot_tokens": decision.shared_cached_hot_tokens,
            "shared_overhead_equivalent_tokens": decision.shared_overhead,
            "crossing_margin": decision.crossing_margin,
            "emergency_triggered": decision.emergency_requested,
            "pending_count_over": decision.pending_count_over,
            "pending_tokens_over": decision.pending_tokens_over,
            "amortized_crossed": decision.amortized_crossed,
            "immediate_crossed": bool(immediate_crossed),
            "batch_policy": decision.batch_policy,
            "fixed_batch_size": decision.fixed_batch_size,
            "amortized_cache_read_weight": self.amortized_cache_read_weight,
            "cache_granularity_tokens": self._cache_granularity_tokens(),
        }

    def _amortized_projection_decision_record(
        self,
        decision: AmortizedDecision,
        partition: HotTailPartition,
        *,
        exact_score: Any | None,
        policy_score: Any | None,
        immediate_score: Any | None,
        immediate_pricing: PricingWeights,
        immediate_crossed: bool,
        projection_epoch_id: str | None = None,
        card_or_receipt_tokens: int | None = None,
        decision_mode: str = "amortized",
    ) -> dict[str, Any]:
        """Build one content-free V1.2 decision row.

        The primary score columns retain the fixed V1.2 accounting view used
        beside W/Q. Dedicated ``immediate_*`` columns preserve the independent
        V1.1 summary-free full-render counterfactual without conflating its
        route pricing with the fixed amortization weight.
        """

        immediate = EconomicDecision(
            decision_kind="flush" if decision.should_flush else "wait",
            decision_reason=decision.reason,
            candidate_count=decision.pending_delta_count,
            winner=exact_score,
            ranked_batches=(exact_score,) if exact_score is not None else (),
            pricing_source="amortized_fixed",
            pricing_version="1.2",
            estimator_source="rough_message_estimator",
        )
        record = self._projection_decision_record(
            immediate,
            self._amortized_pricing_weights(),
            projection_epoch_id=projection_epoch_id,
            card_or_receipt_tokens=card_or_receipt_tokens,
            decision_mode=decision_mode,
        )
        record.update({
            "amortized_baseline_prompt_tokens": (
                policy_score.baseline_prompt_tokens
                if policy_score is not None
                else None
            ),
            "amortized_candidate_prompt_tokens": (
                policy_score.candidate_prompt_tokens
                if policy_score is not None
                else None
            ),
            "amortized_baseline_reusable_prefix_tokens": (
                policy_score.baseline_reusable_prefix_tokens
                if policy_score is not None
                else None
            ),
            "amortized_candidate_reusable_prefix_tokens": (
                policy_score.candidate_reusable_prefix_tokens
                if policy_score is not None
                else None
            ),
            "immediate_cache_penalty_equivalent_tokens": (
                immediate_score.cache_penalty_equivalent_tokens
                if immediate_score is not None
                else None
            ),
            "immediate_net_saving_equivalent_tokens": (
                immediate_score.net_saving_equivalent_tokens
                if immediate_score is not None
                else None
            ),
            "immediate_net_saving_usd": (
                immediate_score.net_saving_usd
                if immediate_score is not None
                else None
            ),
            "immediate_cache_read_weight": immediate_pricing.cache_read,
            "immediate_cache_write_weight": immediate_pricing.cache_write,
            "immediate_pricing_source": immediate_pricing.source,
            "immediate_pricing_version": immediate_pricing.version,
        })
        record.update(
            self._amortized_decision_telemetry_fields(
                decision,
                partition,
                immediate_crossed=immediate_crossed,
            )
        )
        return record

    def _persist_wait_decision(
        self,
        decision: EconomicDecision,
        pricing: PricingWeights,
        *,
        card_or_receipt_tokens: int | None = None,
        decision_mode: str | None = None,
    ) -> None:
        if self._store is None:
            return
        try:
            self._store.record_projection_decision(
                self._projection_decision_record(
                    decision,
                    pricing,
                    card_or_receipt_tokens=card_or_receipt_tokens,
                    decision_mode=decision_mode,
                )
            )
        except Exception:
            logger.warning(
                "Object Context decision telemetry write failed; request remains usable",
                exc_info=True,
            )

    @staticmethod
    def _prepared_card_or_receipt_tokens(prepared: _PreparedProjection) -> int:
        return sum(
            estimate_tokens_rough(card_text)
            for _, _, card_text, _ in prepared.cards
        ) + sum(
            estimate_tokens_rough(str(content))
            for _, content in prepared.content_replacements
        )

    def _plan_and_publish_economic_batch(
        self,
        baseline_request: Sequence[dict[str, Any]],
        *,
        include_skipped: bool = False,
    ) -> bool:
        started_at = time.perf_counter()
        store = self._store
        if store is None or not self._conversation_id:
            return False
        prepared_by_id, candidates, unseen = self._discover_economic_candidates(
            baseline_request,
            include_skipped=include_skipped,
        )
        pricing = self._economic_pricing_weights()
        previous = self._previous_successful_request_view
        baseline_lcp = (
            self._rough_lcp_tokens(previous, baseline_request)
            if previous is not None
            else 0
        )
        prefix = PrefixFacts(
            baseline_prompt_tokens=estimate_messages_tokens_rough(
                list(baseline_request)
            ),
            baseline_reusable_prefix_tokens=baseline_lcp,
            previous_success_available=previous is not None,
            cache_granularity_tokens=self._cache_granularity_tokens(),
        )
        emergency_target = max(
            1,
            int(
                max(1, int(getattr(self, "context_length", 0) or 1))
                * self.emergency_context_ratio
            ),
        )
        emergency = prefix.baseline_prompt_tokens >= emergency_target
        if emergency:
            decision = plan_emergency_batch(
                candidates,
                prefix=prefix,
                pricing=pricing,
                target_prompt_tokens=emergency_target,
                unseen_candidate_count=unseen,
            )
        else:
            decision = plan_economic_batch(
                candidates,
                prefix=prefix,
                pricing=pricing,
                minimum_net_saving_tokens=self.economic_min_net_saving_tokens,
                minimum_net_saving_usd=self.economic_min_net_saving_usd,
                unseen_candidate_count=unseen,
            )
        self._last_economic_decision = decision
        decision_replacement_tokens = sum(
            self._prepared_card_or_receipt_tokens(prepared_by_id[delta_id])
            for delta_id in decision.member_delta_ids
            if delta_id in prepared_by_id
        )
        if decision.decision_kind not in {"flush", "emergency"}:
            self._persist_wait_decision(
                decision,
                pricing,
                card_or_receipt_tokens=decision_replacement_tokens,
                decision_mode="emergency" if emergency else "normal",
            )
            return False

        ranked = decision.ranked_batches
        if self.card_summary_enabled:
            # Summary inference is allowed only for the provisional winner.
            ranked = ranked[:1]
        for rough_score in ranked:
            if not emergency and (
                rough_score.net_saving_equivalent_tokens
                < self.economic_min_net_saving_tokens
            ):
                break
            batch = [
                prepared_by_id[delta_id]
                for delta_id in rough_score.member_delta_ids
            ]
            summary_cost = 0.0
            if self.card_summary_enabled:
                before = self._aux_card_summary_snapshot()
                summarized: list[_PreparedProjection] = []
                for prepared in batch:
                    exact = self._prepare_delta_projection(
                        prepared.delta, generate_summaries=True
                    )
                    if exact is None:
                        summarized = []
                        break
                    summarized.append(exact)
                if not summarized:
                    self._last_economic_decision = replace(
                        decision,
                        decision_kind="wait",
                        decision_reason=PROJECTION_FAILED_RAW_FALLBACK,
                        winner=rough_score,
                    )
                    self._persist_wait_decision(
                        self._last_economic_decision,
                        pricing,
                        card_or_receipt_tokens=decision_replacement_tokens,
                        decision_mode="emergency" if emergency else "normal",
                    )
                    return False
                batch = summarized
                summary_cost = self._summary_cost_equivalent_tokens(
                    before, self._aux_card_summary_snapshot(), pricing
                )
            candidate_request = self._render_prepared_batch(
                baseline_request, batch
            )
            exact_score = score_exact_batch(
                member_delta_ids=(item.delta.delta_id for item in batch),
                member_object_refs=(
                    object_ref for item in batch for object_ref in item.object_refs
                ),
                earliest_changed_delta_id=batch[0].delta.delta_id,
                baseline_prompt_tokens=prefix.baseline_prompt_tokens,
                candidate_prompt_tokens=estimate_messages_tokens_rough(
                    candidate_request
                ),
                baseline_reusable_prefix_tokens=baseline_lcp,
                candidate_reusable_prefix_tokens=self._rough_lcp_tokens(
                    previous, candidate_request
                ) if previous is not None else 0,
                pricing=pricing,
                known_summary_cost_equivalent_tokens=summary_cost,
            )
            decision_replacement_tokens = sum(
                self._prepared_card_or_receipt_tokens(item) for item in batch
            )
            exact_crossed = (
                exact_score.candidate_prompt_tokens <= emergency_target
                if emergency
                else self._exact_threshold_crossed(exact_score)
            )
            if not exact_crossed:
                self._last_economic_decision = replace(
                    decision,
                    decision_kind="wait",
                    decision_reason=(
                        SUMMARY_RECHECK_BELOW_THRESHOLD
                        if self.card_summary_enabled
                        else WAIT_BELOW_THRESHOLD
                    ),
                    winner=exact_score,
                )
                continue
            epoch_id = f"epoch_{uuid.uuid4().hex}"
            committed_decision = replace(
                decision,
                decision_kind="emergency" if emergency else "flush",
                decision_reason=(
                    EMERGENCY_FLUSH if emergency else FLUSH_NET_POSITIVE
                ),
                winner=exact_score,
            )
            exact_replacement_tokens = decision_replacement_tokens
            try:
                store.publish_compressed_batch(
                    [
                        (
                            item.delta.delta_id,
                            item.cards,
                            item.compressed_view,
                        )
                        for item in batch
                    ],
                    projection_epoch_id=epoch_id,
                    request_sequence=self._request_sequence + 1,
                    min_raw_exposures=self.min_raw_exposures,
                    known_object_refs_by_delta={
                        item.delta.delta_id: item.known_object_refs
                        for item in batch
                        if item.known_object_refs
                    },
                    projection_decision=self._projection_decision_record(
                        committed_decision,
                        pricing,
                        projection_epoch_id=epoch_id,
                        card_or_receipt_tokens=exact_replacement_tokens,
                        decision_mode="emergency" if emergency else "normal",
                    ),
                )
            except Exception as exc:
                if self._batch_projection_became_durable(
                    store, (item.delta.delta_id for item in batch)
                ):
                    self._last_failure = "economic_concurrent_projection_adopted"
                    return True
                self._last_failure = f"economic_projection_commit:{exc}"
                try:
                    store.mark_deltas_failed_if_unprojected(
                        (item.delta.delta_id for item in batch), str(exc)
                    )
                except Exception:
                    logger.debug(
                        "Object Context failed-state publication failed",
                        exc_info=True,
                    )
                self._record_metric(
                    "compression_failures",
                    1,
                    metadata={"stage": "economic_atomic_commit"},
                )
                self._last_economic_decision = replace(
                    decision,
                    decision_kind="wait",
                    decision_reason=PROJECTION_FAILED_RAW_FALLBACK,
                    winner=exact_score,
                )
                self._persist_wait_decision(
                    self._last_economic_decision,
                    pricing,
                    card_or_receipt_tokens=exact_replacement_tokens,
                    decision_mode="emergency" if emergency else "normal",
                )
                return False
            self._last_batch_size = len(batch)
            self.compression_count += len(batch)
            self._record_metric("prompt_prefix_rewrite_events", 1)
            self._record_metric("prompt_prefix_rewritten_deltas", len(batch))
            for item in batch:
                self._record_metric(
                    "raw_context_tokens",
                    item.raw_tokens,
                    delta_id=item.delta.delta_id,
                )
                self._record_metric(
                    "rendered_context_tokens",
                    item.projected_tokens,
                    delta_id=item.delta.delta_id,
                )
                self._record_metric(
                    "card_tokens",
                    self._prepared_card_or_receipt_tokens(item),
                    delta_id=item.delta.delta_id,
                )
                self._record_metric(
                    "tokens_saved",
                    max(0, item.raw_tokens - item.projected_tokens),
                    delta_id=item.delta.delta_id,
                )
            self._last_economic_decision = committed_decision
            self._record_metric(
                "compression_latency_ms",
                max(0.0, (time.perf_counter() - started_at) * 1000),
            )
            return True
        self._persist_wait_decision(
            self._last_economic_decision,
            pricing,
            card_or_receipt_tokens=decision_replacement_tokens,
            decision_mode="emergency" if emergency else "normal",
        )
        return False

    def _plan_and_publish_amortized_batch(
        self, baseline_request: Sequence[dict[str, Any]]
    ) -> bool:
        """Evaluate and, when due, atomically publish the V1.2 Pending pool."""

        started_at = time.perf_counter()
        store = self._store
        if store is None or not self._conversation_id:
            return False

        (
            prepared_by_id,
            _all_candidates,
            partition,
            ledgers,
            _unseen,
        ) = self._rebalance_amortized_pending(baseline_request)
        baseline_tokens = estimate_messages_tokens_rough(list(baseline_request))
        emergency_target = max(
            1,
            int(
                max(1, int(getattr(self, "context_length", 0) or 1))
                * self.emergency_context_ratio
            ),
        )
        emergency = baseline_tokens >= emergency_target

        ordered_ledgers = tuple(
            ledger for ledger in ledgers if ledger.delta_id in prepared_by_id
        )
        all_prepared_batch = tuple(
            prepared_by_id[ledger.delta_id] for ledger in ordered_ledgers
        )
        dynamic_candidate_request = (
            self._render_prepared_batch(baseline_request, all_prepared_batch)
            if all_prepared_batch
            else list(baseline_request)
        )
        previous = self._previous_successful_request_view
        baseline_known = (
            self._cache_baseline_state == "known" and previous is not None
        )
        baseline_cold = self._cache_baseline_state == "cold"
        baseline_lcp = (
            self._rough_lcp_tokens(previous, baseline_request)
            if baseline_known and previous is not None
            else 0
        )
        dynamic_candidate_lcp = (
            self._rough_lcp_tokens(previous, dynamic_candidate_request)
            if baseline_known and previous is not None
            else 0
        )
        hot_start = min(
            baseline_tokens,
            max(0, int(partition.hot_start_token_offset)),
        )
        shared_cached_hot = (
            max(0, baseline_lcp - max(dynamic_candidate_lcp, hot_start))
            if baseline_known
            else 0
        )
        pending = tuple(
            PendingDelta(
                delta_id=ledger.delta_id,
                success_sequence=ledger.bucket_sequence,
                raw_tokens=ledger.raw_tokens,
                gain_tokens=ledger.gain_tokens,
                wait_area=ledger.wait_area_token_requests,
            )
            for ledger in ordered_ledgers
        )
        decision = plan_amortized_flush(
            pending,
            cache_read_weight=self.amortized_cache_read_weight,
            shared_cached_hot_tokens=shared_cached_hot,
            hot_bucket_limit=self.hot_tail_max_inferences,
            hot_token_limit=self.hot_tail_max_tokens,
            emergency=emergency,
            batch_policy=self.batch_policy,
            fixed_batch_size=self.fixed_batch_size,
        )
        # A same-route resume has no trustworthy P bytes.  Capacity remains a
        # hard safety action, but a normal zero-Q crossing must wait until one
        # accepted request reconstructs the prefix baseline.  A definitively
        # new route (cold) legitimately has Q=0 and may cross immediately.
        if not baseline_known and not baseline_cold:
            # Q is unknown, not zero.  Never label a safety flush as an
            # economic crossing merely because the placeholder numeric value
            # is zero.
            decision = replace(decision, amortized_crossed=False)
            if (
                self.batch_policy == BATCH_POLICY_DYNAMIC
                and decision.pending_delta_count > 0
                and not decision.capacity_triggered
                and not decision.emergency_requested
            ):
                decision = replace(
                    decision,
                    action="wait",
                    reason=WAIT_NO_BASELINE,
                    member_delta_ids=(),
                    member_bucket_sequences=(),
                )

        all_member_delta_ids = tuple(
            item.delta.delta_id for item in all_prepared_batch
        )
        if decision.should_flush:
            prepared_batch = tuple(
                prepared_by_id[delta_id]
                for delta_id in decision.member_delta_ids
                if delta_id in prepared_by_id
            )
        elif self.batch_policy == BATCH_POLICY_FIXED:
            # A fixed-count wait has no legal partial publication candidate.
            # Score Q0→Q0 while the independent W/Q fields continue to expose
            # the full-Pending dynamic-policy counterfactual.
            prepared_batch = ()
        else:
            # Preserve the existing dynamic V1.2 wait telemetry: it scores the
            # complete Pending candidate even though it does not publish it.
            prepared_batch = all_prepared_batch
        candidate_request = (
            self._render_prepared_batch(baseline_request, prepared_batch)
            if prepared_batch
            else list(baseline_request)
        )
        candidate_lcp = (
            self._rough_lcp_tokens(previous, candidate_request)
            if baseline_known and previous is not None
            else 0
        )
        policy_pricing = self._amortized_pricing_weights()
        immediate_pricing = self._economic_pricing_weights()
        candidate_tokens = estimate_messages_tokens_rough(candidate_request)
        dynamic_candidate_tokens = estimate_messages_tokens_rough(
            dynamic_candidate_request
        )
        earliest = (
            min(
                prepared_batch,
                key=lambda item: (
                    item.delta.global_sequence,
                    item.delta.delta_id,
                ),
            )
            if prepared_batch
            else None
        )
        member_delta_ids = tuple(
            item.delta.delta_id for item in prepared_batch
        )
        member_object_refs = tuple(
            object_ref
            for item in prepared_batch
            for object_ref in item.object_refs
        )
        earliest_delta_id = earliest.delta.delta_id if earliest is not None else ""
        exact_score = score_exact_batch(
            member_delta_ids=member_delta_ids,
            member_object_refs=member_object_refs,
            earliest_changed_delta_id=earliest_delta_id,
            baseline_prompt_tokens=baseline_tokens,
            candidate_prompt_tokens=candidate_tokens,
            baseline_reusable_prefix_tokens=baseline_lcp,
            candidate_reusable_prefix_tokens=candidate_lcp,
            pricing=policy_pricing,
        )
        all_member_object_refs = tuple(
            object_ref
            for item in all_prepared_batch
            for object_ref in item.object_refs
        )
        all_earliest = (
            min(
                all_prepared_batch,
                key=lambda item: (
                    item.delta.global_sequence,
                    item.delta.delta_id,
                ),
            )
            if all_prepared_batch
            else None
        )
        all_earliest_delta_id = (
            all_earliest.delta.delta_id if all_earliest is not None else ""
        )
        policy_score = score_exact_batch(
            member_delta_ids=all_member_delta_ids,
            member_object_refs=all_member_object_refs,
            earliest_changed_delta_id=all_earliest_delta_id,
            baseline_prompt_tokens=baseline_tokens,
            candidate_prompt_tokens=dynamic_candidate_tokens,
            baseline_reusable_prefix_tokens=baseline_lcp,
            candidate_reusable_prefix_tokens=dynamic_candidate_lcp,
            pricing=policy_pricing,
        )
        # V1.1 shadow telemetry uses the real route/fallback pricing and USD
        # gate, but the same deterministic summary-free Q0/Qc bytes.  It never
        # calls the summary model and never triggers a V1.2 flush.
        immediate_score = score_exact_batch(
            member_delta_ids=all_member_delta_ids,
            member_object_refs=all_member_object_refs,
            earliest_changed_delta_id=all_earliest_delta_id,
            baseline_prompt_tokens=baseline_tokens,
            candidate_prompt_tokens=dynamic_candidate_tokens,
            baseline_reusable_prefix_tokens=baseline_lcp,
            candidate_reusable_prefix_tokens=dynamic_candidate_lcp,
            pricing=immediate_pricing,
        )
        replacement_tokens = sum(
            self._prepared_card_or_receipt_tokens(item)
            for item in prepared_batch
        )
        immediate_crossed = bool(
            all_prepared_batch
            and self._exact_threshold_crossed(immediate_score)
        )
        self._last_economic_decision = EconomicDecision(
            decision_kind="flush" if immediate_crossed else "wait",
            decision_reason=(
                FLUSH_NET_POSITIVE if immediate_crossed else WAIT_BELOW_THRESHOLD
            ),
            candidate_count=len(all_prepared_batch),
            winner=immediate_score,
            ranked_batches=(
                (immediate_score,) if immediate_score is not None else ()
            ),
            pricing_source=immediate_pricing.source,
            pricing_version=immediate_pricing.version,
            estimator_source="rough_message_estimator",
        )
        self._last_amortized_decision = decision
        if emergency:
            # Emergency deliberately retains the V1.1 viability planner over
            # every legal Raw-seen candidate.  It may cross Seen Hot but the
            # shared min_raw_exposures gate still protects fresh content.  The
            # transient context enriches that actual emergency epoch with the
            # same independent V1.2 Hot/Pending/W/Q facts as normal decisions.
            self._amortized_telemetry_context = (
                decision,
                partition,
                policy_score,
                immediate_score,
                immediate_pricing,
                immediate_crossed,
            )
            try:
                return self._plan_and_publish_economic_batch(
                    baseline_request,
                    include_skipped=True,
                )
            finally:
                self._amortized_telemetry_context = None
        mode = (
            "capacity"
            if decision.capacity_triggered
            else (
                "fixed"
                if self.batch_policy == BATCH_POLICY_FIXED
                else "amortized"
            )
        )

        if not decision.should_flush:
            try:
                store.record_projection_decision(
                    self._amortized_projection_decision_record(
                        decision,
                        partition,
                        exact_score=exact_score,
                        policy_score=policy_score,
                        immediate_score=immediate_score,
                        immediate_pricing=immediate_pricing,
                        immediate_crossed=immediate_crossed,
                        card_or_receipt_tokens=replacement_tokens,
                        decision_mode=mode,
                    )
                )
            except Exception:
                logger.warning(
                    "Object Context V1.2 wait telemetry write failed; "
                    "request remains Raw and usable",
                    exc_info=True,
                )
            return False

        # Dynamic and capacity V1.2 actions are deliberately flush-all. A
        # normal fixed action selects exactly the oldest configured N Deltas.
        # Derive the expected membership independently from the planner and
        # fail closed before any durable projection if those contracts diverge.
        expected_member_delta_ids = (
            all_member_delta_ids
            if decision.capacity_triggered
            or self.batch_policy == BATCH_POLICY_DYNAMIC
            else all_member_delta_ids[: self.fixed_batch_size]
        )
        if (
            decision.member_delta_ids != expected_member_delta_ids
            or decision.member_delta_ids
            != tuple(item.delta.delta_id for item in prepared_batch)
            or (
                self.batch_policy == BATCH_POLICY_FIXED
                and not decision.capacity_triggered
                and decision.reason != FLUSH_FIXED_BATCH_SIZE
            )
        ):
            self._last_failure = "amortized_membership_mismatch"
            logger.error(
                "Object Context V1.2 planner violated %s membership",
                self.batch_policy,
            )
            return False

        epoch_id = f"epoch_{uuid.uuid4().hex}"
        try:
            store.publish_compressed_batch(
                [
                    (
                        item.delta.delta_id,
                        item.cards,
                        item.compressed_view,
                    )
                    for item in prepared_batch
                ],
                projection_epoch_id=epoch_id,
                request_sequence=self._request_sequence + 1,
                min_raw_exposures=self.min_raw_exposures,
                known_object_refs_by_delta={
                    item.delta.delta_id: item.known_object_refs
                    for item in prepared_batch
                    if item.known_object_refs
                },
                projection_decision=self._amortized_projection_decision_record(
                    decision,
                    partition,
                    exact_score=exact_score,
                    policy_score=policy_score,
                    immediate_score=immediate_score,
                    immediate_pricing=immediate_pricing,
                    immediate_crossed=immediate_crossed,
                    projection_epoch_id=epoch_id,
                    card_or_receipt_tokens=replacement_tokens,
                    decision_mode=mode,
                ),
            )
        except Exception as exc:
            if self._batch_projection_became_durable(
                store, (item.delta.delta_id for item in prepared_batch)
            ):
                self._last_failure = "amortized_concurrent_projection_adopted"
                return True
            self._last_failure = f"amortized_projection_commit:{exc}"
            try:
                store.mark_deltas_failed_if_unprojected(
                    (item.delta.delta_id for item in prepared_batch), str(exc)
                )
            except Exception:
                logger.debug(
                    "Object Context V1.2 failed-state publication failed",
                    exc_info=True,
                )
            self._record_metric(
                "compression_failures",
                1,
                metadata={"stage": "amortized_atomic_commit"},
            )
            failed = replace(
                decision,
                action="wait",
                reason=PROJECTION_FAILED_RAW_FALLBACK,
                member_delta_ids=(),
                member_bucket_sequences=(),
            )
            self._last_amortized_decision = failed
            try:
                store.record_projection_decision(
                    self._amortized_projection_decision_record(
                        failed,
                        partition,
                        exact_score=exact_score,
                        policy_score=policy_score,
                        immediate_score=immediate_score,
                        immediate_pricing=immediate_pricing,
                        immediate_crossed=immediate_crossed,
                        card_or_receipt_tokens=replacement_tokens,
                        decision_mode=mode,
                    )
                )
            except Exception:
                logger.warning(
                    "Object Context V1.2 failure telemetry write failed",
                    exc_info=True,
                )
            return False

        self._last_batch_size = len(prepared_batch)
        self.compression_count += len(prepared_batch)
        self._record_metric("prompt_prefix_rewrite_events", 1)
        self._record_metric(
            "prompt_prefix_rewritten_deltas", len(prepared_batch)
        )
        for item in prepared_batch:
            self._record_metric(
                "raw_context_tokens",
                item.raw_tokens,
                delta_id=item.delta.delta_id,
            )
            self._record_metric(
                "rendered_context_tokens",
                item.projected_tokens,
                delta_id=item.delta.delta_id,
            )
            self._record_metric(
                "card_tokens",
                self._prepared_card_or_receipt_tokens(item),
                delta_id=item.delta.delta_id,
            )
            self._record_metric(
                "tokens_saved",
                max(0, item.raw_tokens - item.projected_tokens),
                delta_id=item.delta.delta_id,
            )
        self._record_metric(
            "compression_latency_ms",
            max(0.0, (time.perf_counter() - started_at) * 1000),
        )
        return True

    @staticmethod
    def _refs_in_messages(messages: Iterable[dict[str, Any]]) -> set[str]:
        refs: set[str] = set()
        for message in messages:
            if not isinstance(message, dict):
                continue
            try:
                encoded = canonical_json({
                    "content": message.get("content"),
                    "tool_calls": message.get("tool_calls"),
                    "artifact": message.get("artifact"),
                    "workspace": message.get("workspace"),
                })
            except Exception:
                continue
            refs.update(_OBJECT_REF_SCAN_RE.findall(encoded))
        return refs

    def select_context(
        self,
        request_messages: list[dict[str, Any]],
        *,
        conversation_messages: list[dict[str, Any]] | None = None,
        incoming_message: dict[str, Any] | None = None,
        budget_tokens: int = 0,
    ) -> list[dict[str, Any]] | None:
        del incoming_message, budget_tokens
        # A transient SQLite failure must not permanently discard an already
        # accepted request observation. Retry queued UUID-idempotent rows before
        # using the durable success watermark for this boundary.
        self._flush_unconfirmed_success_observations()
        started_at = time.perf_counter()
        request_attempt_id = str(uuid.uuid4())
        self._planning_request_attempt_id = request_attempt_id
        conversation_scope = (
            conversation_messages
            if conversation_messages is not None
            else [
                message
                for message in request_messages
                if isinstance(message, dict) and message.get("role") != "system"
            ]
        )
        projected, projected_conversation = self._project_views(
            request_messages, identity_messages=conversation_scope
        )
        baseline = projected if projected is not None else request_messages
        # An unresolved prior attempt is a provider-failure/retry boundary.
        # Any Cards already committed for that attempt are rendered below, but
        # V1.2 must not publish a second normal/capacity epoch until one attempt
        # receives a terminal successful notification (or a route reset clears
        # the fence explicitly).
        retry_fence = (
            self.scheduler == "amortized_batch"
            and self._pending_raw_exposure is not None
        )
        published = False
        if self.scheduler == "amortized_batch":
            if retry_fence:
                # Publication is fenced, but a retry may be rebuilt with a
                # different active Raw view (redirect, sanitizer, restored
                # history). Refresh the active/projectable Pending snapshot so
                # the accepted retry accrues exactly the members it carried.
                # No decision row or projection epoch is emitted here.
                try:
                    self._rebalance_amortized_pending(baseline)
                except Exception:
                    logger.warning(
                        "Object Context V1.2 retry snapshot refresh failed; "
                        "the request remains Raw without waiting-area credit",
                        exc_info=True,
                    )
                    self._active_pending_snapshot_ids = frozenset()
            else:
                published = self._plan_and_publish_amortized_batch(baseline)
        else:
            published = self._plan_and_publish_economic_batch(baseline)
        if published:
            # Render only after the atomic epoch is durable. A failed/concurrent
            # commit leaves the already-built Q0 byte-stable for this request.
            projected, projected_conversation = self._project_views(
                request_messages, identity_messages=conversation_scope
            )
        candidate = projected if projected is not None else request_messages
        raw_tokens = estimate_messages_tokens_rough(request_messages)
        rendered_tokens = estimate_messages_tokens_rough(candidate)
        saved_tokens = max(0, raw_tokens - rendered_tokens)
        # ``request_messages`` is the assembled provider message view.  At
        # this point it can already contain the system prompt, ephemeral
        # prefills, MoA context, and memory/plugin injections.  Those values
        # are useful for request diagnostics, but they are not the corpus V1
        # compresses.  Keep a second, explicit scope based on the immutable
        # persisted conversation supplied by the host.  Project that view
        # from the same occurrence snapshot so rough-token rounding is
        # measured independently from the assembled request.
        raw_conversation_tokens = estimate_messages_tokens_rough(
            conversation_scope
        )
        conversation_candidate = (
            projected_conversation
            if projected_conversation is not None
            else conversation_scope
        )
        rendered_conversation_tokens = estimate_messages_tokens_rough(
            conversation_candidate
        )
        conversation_tokens_saved = max(
            0, raw_conversation_tokens - rendered_conversation_tokens
        )
        self._last_rendered_refs = self._refs_in_messages(candidate)
        self._remember_request_projection(
            raw_tokens=raw_conversation_tokens,
            saved_tokens=conversation_tokens_saved,
        )
        self._projection_sequence += 1
        projection_sequence = self._projection_sequence
        metadata = {
            "event": "request_projection",
            "projection_id": (
                f"{self._object_session_id or 'session'}:"
                f"{projection_sequence}:{time.time_ns()}"
            ),
            "projection_sequence": projection_sequence,
            "turn_id": self._active_turn_id,
            "session_id": self._object_session_id,
            "conversation_metric_scope": "persisted_history",
        }
        projection_latency_ms = max(
            0.0, (time.perf_counter() - started_at) * 1000
        )
        if self._store is not None and self._conversation_id:
            try:
                self._store.record_metrics(
                    self._conversation_id,
                    {
                        "raw_context_tokens": raw_tokens,
                        "rendered_context_tokens": rendered_tokens,
                        "raw_conversation_tokens": raw_conversation_tokens,
                        "rendered_conversation_tokens": (
                            rendered_conversation_tokens
                        ),
                        "conversation_tokens_saved": conversation_tokens_saved,
                        "conversation_compression_ratio": (
                            1
                            - (
                                rendered_conversation_tokens
                                / raw_conversation_tokens
                            )
                            if raw_conversation_tokens
                            else 0
                        ),
                        "hot_tail_tokens": self._last_hot_tail_tokens,
                        "tokens_saved": saved_tokens,
                        "compression_ratio": (
                            1 - (rendered_tokens / raw_tokens) if raw_tokens else 0
                        ),
                        "projection_latency_ms": projection_latency_ms,
                    },
                    metadata=metadata,
                )
            except Exception:
                logger.debug(
                    "Object Context projection telemetry write failed",
                    exc_info=True,
                )
        self._snapshot_raw_exposure(
            candidate,
            request_attempt_id=request_attempt_id,
        )
        self._planning_request_attempt_id = ""
        return candidate if candidate != request_messages else None

    def get_projection_timeline(self) -> dict[str, Any]:
        """Return content-free projection and provider-cache dynamics."""

        if self._store is None or not self._conversation_id:
            return {
                "schema_version": 4,
                "conversation_id": self._conversation_id,
                "session_id": self._object_session_id,
                "projections": [],
                "cache_requests": [],
                "economic_decisions": [],
                "request_observations": [],
            }
        try:
            projections = self._store.request_projection_timeline(
                self._conversation_id
            )
        except Exception:
            logger.debug(
                "Object Context projection timeline query failed",
                exc_info=True,
            )
            projections = []
        try:
            cache_requests = self._store.cache_usage_timeline(
                self._conversation_id
            )
        except Exception:
            logger.debug(
                "Object Context cache-usage timeline query failed",
                exc_info=True,
            )
            cache_requests = []
        try:
            economic_decisions = self._store.projection_decisions(
                self._conversation_id
            )
        except Exception:
            logger.debug(
                "Object Context economic-decision timeline query failed",
                exc_info=True,
            )
            economic_decisions = []
        try:
            request_observations = self._store.request_observation_timeline(
                self._conversation_id
            )
        except Exception:
            logger.debug(
                "Object Context request-observation timeline query failed",
                exc_info=True,
            )
            request_observations = []
        return {
            "schema_version": 4,
            "conversation_id": self._conversation_id,
            "session_id": self._object_session_id,
            "projections": projections,
            "cache_requests": cache_requests,
            "economic_decisions": economic_decisions,
            "request_observations": request_observations,
        }

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        projected = self._project(messages)
        candidate = projected if projected is not None else messages
        return self.should_compress(estimate_messages_tokens_rough(candidate))

    def prune_tool_results_only(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        del current_tokens
        # Object V1 and the summarizer may only create request projections or a
        # normal compaction boundary. Never rewrite the authoritative raw trace.
        return messages, 0

    def compress(
        self,
        messages: list[dict[str, Any]],
        current_tokens: int | None = None,
        focus_topic: str | None = None,
        force: bool = False,
        memory_context: str = "",
    ) -> list[dict[str, Any]]:
        projected = self._project(messages)
        candidate = projected if projected is not None else messages
        # The independent whole-history summarizer consumes the same Card view
        # as the normal provider. It does not auto-rehydrate immutable raw trace.
        projected_tokens = (
            estimate_messages_tokens_rough(candidate)
            if projected is not None
            else current_tokens
        )
        return super().compress(
            candidate,
            current_tokens=projected_tokens,
            focus_topic=focus_topic,
            force=force,
            memory_context=memory_context,
        )

    @staticmethod
    def get_tool_schemas() -> list[dict[str, Any]]:
        return [
            {
                "name": RETRIEVE_OBJECT_TOOL_NAME,
                "description": (
                    "Load one exact immutable structured object referenced by an "
                    "OBJECT_CARD. Use it when exact implementation, values, lines, "
                    "configuration, or quotations are required. The complete object "
                    "is mounted only for the current real user turn."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "object_ref": {
                            "type": "string",
                            "description": "Exact immutable object://...@vN reference.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Why exact content is required now.",
                        },
                    },
                    "required": ["object_ref", "reason"],
                    "additionalProperties": False,
                },
            }
        ]

    @staticmethod
    def _retrieval_error(object_ref: str, code: str, message: str) -> str:
        return json.dumps(
            {
                "retrieval_error": {
                    "object_ref": object_ref,
                    "code": code,
                    "message": message,
                    "exact_content_returned": False,
                }
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs: Any) -> str:
        if name != RETRIEVE_OBJECT_TOOL_NAME:
            return super().handle_tool_call(name, args, **kwargs)
        started_at = time.perf_counter()

        def finish(payload: str) -> str:
            self._record_metric(
                "retrieval_latency_ms",
                max(0.0, (time.perf_counter() - started_at) * 1000),
            )
            return payload

        def fail(object_ref: str, code: str, message: str) -> str:
            self._record_metric("retrieval_failures", 1, metadata={"code": code})
            return finish(self._retrieval_error(object_ref, code, message))

        store = self._store
        if store is None or not self._conversation_id:
            return fail("", "STORE_UNAVAILABLE", "Exact object storage is unavailable.")
        if not isinstance(args, dict):
            return fail("", "INVALID_ARGUMENTS", "Tool arguments must be an object.")
        object_ref = str(args.get("object_ref") or "").strip()
        reason = str(args.get("reason") or "").strip()
        if store.parse_object_ref(object_ref) is None:
            return fail(
                object_ref,
                "MALFORMED_OBJECT_REF",
                "Use an exact immutable object://obj_<id>@vN reference.",
            )
        if not reason:
            return fail(
                object_ref,
                "INVALID_REASON",
                "A non-empty retrieval reason is required.",
            )
        if not self._active_turn_id:
            return fail(
                object_ref,
                "NO_ACTIVE_USER_TURN",
                "Exact objects can only be mounted during a real user turn.",
            )
        try:
            record = store.get_object(self._conversation_id, object_ref)
        except RuntimeError as exc:
            self._record_metric("exact_recovery_hash_pass_rate", 0)
            code = "OBJECT_HASH_MISMATCH" if "HASH" in str(exc) else "RESOLVER_FAILURE"
            return fail(object_ref, code, "Stored content failed integrity validation.")
        except Exception:
            return fail(
                object_ref, "RESOLVER_FAILURE", "The object resolver failed safely."
            )
        if record is None:
            code = (
                "UNAUTHORIZED_OBJECT_REFERENCE"
                if store.object_exists(object_ref)
                else "OBJECT_NOT_FOUND"
            )
            return fail(
                object_ref,
                code,
                "The exact object is missing or is outside this conversation.",
            )
        max_retrieval_tokens = max(
            1,
            int(
                max(1, int(getattr(self, "context_length", 0) or 1))
                * self.retrieval_max_tokens_ratio
            ),
        )
        if record.token_count > max_retrieval_tokens:
            return fail(
                object_ref,
                "OBJECT_TOO_LARGE",
                "The complete object cannot fit safely; V1 never truncates retrievals.",
            )
        deltas = store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        tool_call_id = str(kwargs.get("tool_call_id") or "")
        repeated = store.retrieval_count_for_ref(self._conversation_id, object_ref) > 0
        consecutive_turn = store.was_retrieved_in_previous_user_turn(
            self._conversation_id,
            self._active_turn_id,
            object_ref,
        )
        store.mount_retrieval(
            conversation_id=self._conversation_id,
            turn_id=self._active_turn_id,
            object_ref=object_ref,
            tool_call_id=tool_call_id,
            reason=reason,
            mounted_at_delta=current_delta,
        )
        self._record_metric("retrieval_count", 1)
        self._record_metric("retrieved_tokens", record.token_count)
        self._record_metric("repeated_retrieval_rate", 1 if repeated else 0)
        self._record_metric("consecutive_turn_retrievals", 1 if consecutive_turn else 0)
        self._record_metric("exact_recovery_hash_pass_rate", 1)
        return finish(
            json.dumps(
                {
                    "retrieved_object": {
                        "object_ref": record.object_ref,
                        "type": record.object_type.value,
                        "name": record.name,
                        "language": record.language,
                        "sha256_verified": True,
                        "scope": "full_object",
                        "reason": reason,
                        "content": record.content,
                    }
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )

    def _run_activity_gc(self, extra_refs: Iterable[str] = ()) -> dict[str, int]:
        store = self._store
        if store is None or not self._conversation_id:
            return {}
        deltas = store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        leased_refs = {
            lease.object_ref for lease in store.list_leases(self._conversation_id)
        }
        active_refs = set(self._last_rendered_refs) | leased_refs | set(extra_refs)
        counts = store.update_activity(
            conversation_id=self._conversation_id,
            current_delta=current_delta,
            active_refs=active_refs,
            recent_access_deltas=self.recent_retrieval_active_deltas,
            grace_deltas=self.wm_grace_deltas,
        )
        archived = store.archive_evictable(self._conversation_id)
        for object_ref in archived:
            if self._archive_hook is None:
                continue
            try:
                self._archive_hook({
                    "event": "object_archived",
                    "conversation_id": self._conversation_id,
                    "raw_source": object_ref,
                })
            except Exception:
                logger.warning("Object Context archive hook failed", exc_info=True)
        activity_metric_names = {
            "active": "active_object_count",
            "inactive_candidate": "inactive_candidate_count",
            "evictable": "evictable_object_count",
            "archived": "archived_object_count",
        }
        for state, count in counts.items():
            self._record_metric(activity_metric_names[state], count)
        status = store.aggregate_status(self._conversation_id)
        self._record_metric(
            "working_memory_object_count", status["working_memory_object_count"]
        )
        self._record_metric("working_memory_bytes", status["working_memory_bytes"])
        return counts

    def on_turn_complete(
        self,
        messages: list[dict[str, Any]],
        usage: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().on_turn_complete(messages, usage=usage, **kwargs)
        if bool(kwargs.get("failed")) or bool(kwargs.get("interrupted")):
            self.confirm_response_rejected()
        store = self._store
        turn_id = str(kwargs.get("turn_id") or self._active_turn_id or "")
        if store is None or not self._conversation_id or not turn_id:
            return
        deltas = store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        try:
            unmounted = store.unmount_turn(
                self._conversation_id, turn_id, at_delta=current_delta
            )
            self._record_metric("turns_object_remained_mounted", unmounted)
            self._active_turn_id = ""
            self._last_rendered_refs = self._refs_in_messages(
                self._project(messages) or messages
            )
            self._run_activity_gc(self._refs_in_messages(messages))
        except Exception as exc:
            self._last_failure = f"turn_unmount_or_gc:{exc}"
            logger.warning("Object Context turn finalization failed", exc_info=True)

    def create_object_version(
        self,
        *,
        base_ref: str,
        content: str,
        object_type,
        name: str = "",
        language: str = "",
        derived_from: Iterable[str] = (),
    ) -> str:
        """Explicit internal version creation; never inferred from similarity."""

        from .models import ObjectType

        store = self._store
        if store is None or not self._conversation_id:
            raise RuntimeError("Object Context store is unavailable")
        parsed_type = (
            object_type
            if isinstance(object_type, ObjectType)
            else ObjectType(str(object_type))
        )
        record = store.create_object_version(
            conversation_id=self._conversation_id,
            base_ref=base_ref,
            content=content,
            object_type=parsed_type,
            name=name,
            language=language,
            derived_from=derived_from,
        )
        return record.object_ref

    def get_object(self, object_ref: str) -> ObjectRecord | None:
        """Resolve one exact version inside the active conversation lineage."""

        if self._store is None or not self._conversation_id:
            return None
        return self._store.get_object(self._conversation_id, object_ref)

    def resolve_object(self, object_ref: str) -> str | None:
        """Return the logical storage location without exposing a physical path."""

        record = self.get_object(object_ref)
        return record.location.value if record is not None else None

    def mark_object_accessed(self, object_ref: str) -> bool:
        if self._store is None or not self._conversation_id:
            return False
        deltas = self._store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        return self._store.mark_accessed(
            self._conversation_id, object_ref, at_delta=current_delta
        )

    def compress_delta(self, delta_id: str) -> bool:
        """Explicitly retry one eligible/failed Delta; success remains at-most-once."""

        if self._store is None or not self._conversation_id:
            return False
        delta = self._store.get_delta(delta_id)
        if delta is None or delta.conversation_id != self._conversation_id:
            return False
        if delta.state not in {
            DeltaState.COMPRESSION_ELIGIBLE,
            DeltaState.COMPRESSION_FAILED,
        }:
            return delta.state == DeltaState.COMPRESSED
        self._compress_delta_batch([delta])
        current = self._store.get_delta(delta_id)
        return bool(current and current.state == DeltaState.COMPRESSED)

    def build_context(self, messages: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Build the request-only V1 view while preserving the caller's raw list."""

        projected = self._project(messages)
        return projected if projected is not None else copy.deepcopy(list(messages))

    def unmount_turn(self, turn_id: str) -> int:
        if self._store is None or not self._conversation_id or not turn_id:
            return 0
        deltas = self._store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        count = self._store.unmount_turn(
            self._conversation_id, turn_id, at_delta=current_delta
        )
        if turn_id == self._active_turn_id:
            self._active_turn_id = ""
        return count

    def mount_object(
        self,
        turn_id: str,
        object_ref: str,
        *,
        reason: str = "Internal exact-object mount.",
        tool_call_id: str = "",
    ) -> RetrievalLease:
        """Create an authorized turn-scoped lease for an exact object version."""

        if self._store is None or not self._conversation_id:
            raise RuntimeError("Object Context store is unavailable")
        if not turn_id or turn_id != self._active_turn_id:
            raise RuntimeError("objects may only mount in the active real user turn")
        if self._store.parse_object_ref(object_ref) is None:
            raise ValueError("object_ref must be an exact immutable reference")
        record = self._store.get_object(self._conversation_id, object_ref)
        if record is None:
            raise LookupError("object is missing or outside this conversation")
        deltas = self._store.list_deltas(self._conversation_id)
        current_delta = deltas[-1].global_sequence if deltas else 0
        return self._store.mount_retrieval(
            conversation_id=self._conversation_id,
            turn_id=turn_id,
            object_ref=record.object_ref,
            tool_call_id=str(tool_call_id or ""),
            reason=str(reason or "Internal exact-object mount."),
            mounted_at_delta=current_delta,
        )

    def run_working_memory_gc(self) -> dict[str, int]:
        return self._run_activity_gc()

    def pin_object(self, object_ref: str) -> bool:
        return bool(
            self._store and self._store.pin(self._conversation_id, object_ref, True)
        )

    def unpin_object(self, object_ref: str) -> bool:
        return bool(
            self._store and self._store.pin(self._conversation_id, object_ref, False)
        )

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        object_status: dict[str, Any] = {}
        economic_decisions: list[dict[str, Any]] = []
        durable_pending: list[PendingLedgerRecord] = []
        if self._store is not None and self._conversation_id:
            try:
                object_status = self._store.aggregate_status(self._conversation_id)
            except Exception:
                logger.debug("Object Context status query failed", exc_info=True)
            try:
                economic_decisions = self._store.projection_decisions(
                    self._conversation_id
                )
            except Exception:
                logger.debug(
                    "Object Context decision status query failed", exc_info=True
                )
            try:
                durable_pending = self._store.list_pending_ledgers(
                    self._conversation_id
                )
            except Exception:
                logger.debug(
                    "Object Context Pending status query failed", exc_info=True
                )
        normal_projection_count = sum(
            1
            for decision in economic_decisions
            if decision.get("decision_kind") == "flush"
            and decision.get("decision_mode") == "normal"
        )
        emergency_projection_count = sum(
            1
            for decision in economic_decisions
            if decision.get("decision_kind") == "emergency"
        )
        amortized_projection_count = sum(
            1
            for decision in economic_decisions
            if decision.get("decision_kind") == "flush"
            and decision.get("decision_mode") == "amortized"
        )
        fixed_projection_count = sum(
            1
            for decision in economic_decisions
            if decision.get("decision_kind") == "flush"
            and decision.get("decision_mode") == "fixed"
        )
        capacity_projection_count = sum(
            1
            for decision in economic_decisions
            if decision.get("decision_kind") == "flush"
            and decision.get("decision_mode") == "capacity"
        )
        amortized_decisions = [
            decision
            for decision in economic_decisions
            if decision.get("policy_version") == "1.2"
            or decision.get("decision_mode") in {"amortized", "capacity"}
        ]
        hot_partition = self._last_hot_partition
        status.update({
            "object_context_version": (
                "1.2" if self.scheduler == "amortized_batch" else "1.1"
            ),
            "object_context_available": self._store is not None,
            "object_context_enabled": self.object_context_enabled,
            "scheduler": self.scheduler,
            "effective_scheduler": self.scheduler,
            "batch_policy": self.batch_policy,
            "fixed_batch_size": self.fixed_batch_size,
            "hot_tail_max_inferences": self.hot_tail_max_inferences,
            "hot_tail_max_tokens": self.hot_tail_max_tokens,
            "pending_max_inferences": self.pending_max_inferences,
            "pending_max_tokens": self.pending_max_tokens,
            "amortized_cache_read_weight": self.amortized_cache_read_weight,
            "cache_baseline_state": self._cache_baseline_state,
            "latest_success_sequence": self._last_success_sequence,
            "retry_fence_active": self._pending_raw_exposure is not None,
            "min_raw_exposures": self.min_raw_exposures,
            "economic_min_net_saving_tokens": self.economic_min_net_saving_tokens,
            "economic_min_net_saving_usd": self.economic_min_net_saving_usd,
            "economic_cache_read_ratio_fallback": (
                self.economic_cache_read_ratio_fallback
            ),
            "economic_cache_write_ratio_fallback": (
                self.economic_cache_write_ratio_fallback
            ),
            "emergency_context_ratio": self.emergency_context_ratio,
            "card_summary_enabled": self.card_summary_enabled,
            "delta_states": object_status.get("delta_states", {}),
            "object_states": object_status.get("object_states", {}),
            "working_memory_object_count": object_status.get(
                "working_memory_object_count", 0
            ),
            "working_memory_bytes": object_status.get("working_memory_bytes", 0),
            "retrieval_count": object_status.get("retrieval_count", 0),
            "objects_never_retrieved": object_status.get("objects_never_retrieved", 0),
            "retrieval_overhead": object_status.get("retrieval_overhead", 0.0),
            "metric_totals": object_status.get("metric_totals", {}),
            "metric_averages": object_status.get("metric_averages", {}),
            # Request-only projection metrics deliberately exclude the
            # per-Delta Card-construction rows in metric_totals. Operator
            # surfaces can therefore report cumulative request savings
            # without counting the same compression benefit twice.
            "request_projection_count": object_status.get(
                "request_projection_count", 0
            ),
            "request_metric_totals": object_status.get(
                "request_metric_totals", {}
            ),
            "request_metric_averages": object_status.get(
                "request_metric_averages", {}
            ),
            "last_request_metrics": object_status.get(
                "last_request_metrics", {}
            ),
            "hot_tail_tokens": self._last_hot_tail_tokens,
            "hot_underexposed_count": (
                hot_partition.underexposed_delta_count
                if hot_partition is not None
                else 0
            ),
            "hot_seen_delta_count": (
                hot_partition.seen_hot_delta_count
                if hot_partition is not None
                else 0
            ),
            "hot_seen_bucket_count": (
                hot_partition.seen_hot_bucket_count
                if hot_partition is not None
                else 0
            ),
            "hot_overflow_tokens": (
                hot_partition.hot_overflow_tokens
                if hot_partition is not None
                else 0
            ),
            "pending_delta_count": len(durable_pending),
            "pending_bucket_count": len({
                ledger.bucket_sequence for ledger in durable_pending
            }),
            "pending_raw_tokens": sum(
                ledger.raw_tokens for ledger in durable_pending
            ),
            "pending_gain_tokens": sum(
                ledger.gain_tokens for ledger in durable_pending
            ),
            "pending_wait_area_token_requests": sum(
                ledger.wait_area_token_requests for ledger in durable_pending
            ),
            "last_compressed_batch_size": self._last_batch_size,
            "economic_decision_count": len(economic_decisions),
            "amortized_decision_count": len(amortized_decisions),
            "normal_projection_count": normal_projection_count,
            "emergency_projection_count": emergency_projection_count,
            "amortized_projection_count": amortized_projection_count,
            "fixed_projection_count": fixed_projection_count,
            "capacity_projection_count": capacity_projection_count,
            "last_economic_decision": (
                economic_decisions[-1] if economic_decisions else {}
            ),
            "last_amortized_decision": (
                amortized_decisions[-1] if amortized_decisions else {}
            ),
            "last_failure": self._last_failure,
        })
        return status
