"""Context Compression Strategy V1: lossless structured-object virtualization."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextDelta
from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

from .cards import benefit_gate, build_card, render_card
from .detection import (
    RETRIEVE_OBJECT_TOOL_NAME,
    detect_delta_objects,
    iter_text_parts,
    message_key,
)
from .extractors import deterministic_summary, extract_structure
from .models import DeltaRecord, DeltaState, ObjectRecord, RetrievalLease
from .renderer import (
    apply_occurrence_cards,
    project_compressed_messages,
    project_historical_retrievals,
)
from .state import recompute_hot_tail
from .store import ObjectContextStore, canonical_json
from .summaries import BoundedSummaryGenerator


logger = logging.getLogger(__name__)

ENGINE_NAME = "object_context"
DEFAULT_HOT_TAIL_MAX_DELTAS = 8
DEFAULT_HOT_TAIL_TOKEN_BUDGET_RATIO = 0.25
DEFAULT_CONTEXT_SOFT_LIMIT_RATIO = 0.75
DEFAULT_OBJECT_PREFILTER_MIN_TOKENS = 256
DEFAULT_MIN_ABSOLUTE_SAVING_TOKENS = 128
DEFAULT_MIN_RELATIVE_SAVING_RATIO = 0.25
DEFAULT_SUMMARY_MAX_TOKENS = 64
DEFAULT_WM_GRACE_DELTAS = 20
DEFAULT_RECENT_RETRIEVAL_ACTIVE_DELTAS = 20
DEFAULT_RETRIEVAL_MAX_TOKENS_RATIO = 0.50

_OBJECT_REF_SCAN_RE = re.compile(r"object://obj_[a-f0-9]{24}@v[1-9][0-9]*")


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


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


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
        self.hot_tail_max_deltas = _safe_int(
            object_cfg.get("hot_tail_max_deltas"),
            DEFAULT_HOT_TAIL_MAX_DELTAS,
            minimum=1,
        )
        self.hot_tail_token_budget_ratio = _safe_float(
            object_cfg.get("hot_tail_token_budget_ratio"),
            DEFAULT_HOT_TAIL_TOKEN_BUDGET_RATIO,
            minimum=0.01,
        )
        self.context_soft_limit_ratio = _safe_float(
            object_cfg.get("context_soft_limit_ratio"),
            DEFAULT_CONTEXT_SOFT_LIMIT_RATIO,
            minimum=0.10,
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
        # Immutable replacement snapshot for high-frequency status-bar reads.
        # Detailed status continues to aggregate SQLite; this mapping must
        # remain entirely in-memory so prompt-toolkit repaint never performs
        # storage I/O.
        self._status_bar_savings_snapshot: dict[str, int | float | bool] = {}

    @property
    def name(self) -> str:
        return ENGINE_NAME

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
            last_raw = max(0, _safe_int(latest.get("raw_context_tokens"), 0))
            last_saved = max(0, _safe_int(latest.get("tokens_saved"), 0))
            session_raw = max(0, _safe_int(totals.get("raw_context_tokens"), 0))
            session_saved = max(0, _safe_int(totals.get("tokens_saved"), 0))
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

    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Retain summarizer accounting and record provider cache telemetry."""

        super().update_from_response(usage)
        cache_read = max(0, _safe_int(usage.get("cache_read_tokens"), 0))
        cache_write = max(0, _safe_int(usage.get("cache_write_tokens"), 0))
        input_tokens = max(
            0,
            _safe_int(
                usage.get("input_tokens", usage.get("prompt_tokens")),
                self.last_prompt_tokens,
            ),
        )
        self._record_metric("cache_read_tokens", cache_read)
        self._record_metric("cache_write_tokens", cache_write)
        if input_tokens > 0:
            self._record_metric(
                "prompt_cache_hit_ratio", min(1.0, cache_read / input_tokens)
            )

    def on_session_start(self, session_id: str, **kwargs: Any) -> None:
        super().on_session_start(session_id, **kwargs)
        self._clear_status_bar_savings()
        self._projection_sequence = 0
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
        super().on_session_end(session_id, messages)
        self._object_session_id = ""
        self._conversation_id = ""
        self._active_turn_id = ""
        self._last_rendered_refs.clear()
        self._projection_sequence = 0
        self._clear_status_bar_savings()

    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._object_session_id = ""
        self._conversation_id = ""
        self._active_turn_id = ""
        self._last_rendered_refs.clear()
        self._projection_sequence = 0
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
        added = False
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
            added = True
        if added:
            self._schedule_newly_cold()

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
        if schedule:
            self._schedule_newly_cold()

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
        try:
            self._ingest_delta(delta, schedule=True)
        except Exception as exc:
            self._last_failure = f"delta_ingest:{exc}"
            self._record_metric("compression_failures", 1, delta_id=delta.delta_id)
            logger.warning(
                "Object Context Delta ingestion failed; raw trace remains active",
                exc_info=True,
            )

    def _hot_tail_budget(self) -> tuple[int, int]:
        context_length = max(1, int(getattr(self, "context_length", 0) or 1))
        return (
            max(1, int(context_length * self.hot_tail_token_budget_ratio)),
            max(1, int(context_length * self.context_soft_limit_ratio)),
        )

    def _schedule_newly_cold(self) -> None:
        store = self._store
        if store is None or not self._conversation_id:
            return
        deltas = store.list_deltas(self._conversation_id)
        token_budget, soft_limit = self._hot_tail_budget()
        decision = recompute_hot_tail(
            deltas,
            active_turn_id=self._active_turn_id,
            max_deltas=self.hot_tail_max_deltas,
            token_budget=token_budget,
            current_prompt_tokens=max(0, int(self.last_prompt_tokens or 0)),
            context_soft_limit=soft_limit,
        )
        self._last_hot_tail_tokens = decision.hot_tokens
        if not decision.newly_cold_delta_ids:
            return
        store.set_delta_states(
            {
                delta_id: DeltaState.COMPRESSION_ELIGIBLE
                for delta_id in decision.newly_cold_delta_ids
            },
            expected=DeltaState.HOT,
        )
        cold = [store.get_delta(delta_id) for delta_id in decision.newly_cold_delta_ids]
        self._compress_delta_batch([delta for delta in cold if delta is not None])

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
        store = self._store
        if store is None:
            return None
        self._ensure_delta_objects(delta)
        occurrences = store.occurrences_for_delta(delta.delta_id)
        accepted: set[str] = set()
        card_updates: list[tuple[str, str, str, dict[str, Any]]] = []
        rows_for_render: list[dict[str, Any]] = []
        raw_tokens = delta.raw_token_count
        card_tokens = 0
        for occurrence in occurrences:
            object_ref = str(occurrence["object_ref"])
            record = store.get_object(self._conversation_id, object_ref)
            if record is None:
                continue
            contains = extract_structure(record)
            provisional = render_card(
                build_card(
                    record,
                    summary=deterministic_summary(record, contains),
                    contains=contains,
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
            previous = (
                store.get_object(self._conversation_id, record.supersedes)
                if record.supersedes
                else None
            )
            summary, used_fallback = self._summary_generator.generate(
                engine=self,
                record=record,
                contains=contains,
                previous=previous,
            )
            card_text = render_card(
                build_card(record, summary=summary, contains=contains)
            )
            beneficial, _, rendered_tokens = benefit_gate(
                raw_span,
                card_text,
                min_absolute_saving_tokens=self.min_absolute_saving_tokens,
                min_relative_saving_ratio=self.min_relative_saving_ratio,
            )
            if not beneficial:
                continue
            accepted.add(object_ref)
            card_tokens += rendered_tokens
            row = dict(occurrence)
            row["card_text"] = card_text
            rows_for_render.append(row)
            card_updates.append((object_ref, summary, card_text, contains))
            if used_fallback:
                self._record_metric("summary_fallbacks", 1, delta_id=delta.delta_id)
        if not accepted:
            store.mark_delta_skipped(delta.delta_id)
            return None
        compressed_view = apply_occurrence_cards(
            delta.raw_view, rows_for_render, allowed_refs=accepted
        )
        rendered_tokens = estimate_messages_tokens_rough(compressed_view)
        self._record_metric("raw_context_tokens", raw_tokens, delta_id=delta.delta_id)
        self._record_metric(
            "rendered_context_tokens", rendered_tokens, delta_id=delta.delta_id
        )
        self._record_metric("card_tokens", card_tokens, delta_id=delta.delta_id)
        self._record_metric(
            "tokens_saved",
            max(0, raw_tokens - rendered_tokens),
            delta_id=delta.delta_id,
        )
        self._record_metric(
            "compression_ratio",
            1 - (rendered_tokens / raw_tokens) if raw_tokens else 0,
            delta_id=delta.delta_id,
        )
        return delta.delta_id, card_updates, compressed_view

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
        for key in ("timestamp", "tool_call_id"):
            request_value = request_message.get(key)
            raw_value = raw_message.get(key)
            if request_value is not None or raw_value is not None:
                if request_value != raw_value:
                    return False
        request_name = request_message.get("tool_name") or request_message.get("name")
        raw_name = raw_message.get("tool_name") or raw_message.get("name")
        if request_name or raw_name:
            return request_name == raw_name
        return request_message.get("timestamp") is not None

    def _project(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        identity_messages: Sequence[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]] | None:
        store = self._store
        if store is None or not self._conversation_id or not messages:
            return None
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
            occurrences = store.occurrence_cards_for_messages(
                self._conversation_id, [*keys, *identity_keys]
            )
            # The provider request may substitute a persisted ``api_content``
            # sidecar into a user message. Its clean raw content remains the
            # immutable occurrence identity, while exact spans are still valid
            # because injected material is appended. Alias those rows onto the
            # request copy by durable timestamp/role/tool identity.
            for request_message in messages:
                if not isinstance(request_message, dict):
                    continue
                request_key = message_key(request_message)
                if request_key in occurrences:
                    continue
                for raw_message in identity_list:
                    if not self._identity_match(request_message, raw_message):
                        continue
                    raw_key = message_key(raw_message)
                    if raw_key in occurrences:
                        occurrences[request_key] = occurrences[raw_key]
                    break
            leases = store.list_leases(self._conversation_id, self._active_turn_id)
            active_tool_call_ids = {lease.tool_call_id for lease in leases}
            projected = project_compressed_messages(messages, occurrences)
            projected = project_historical_retrievals(
                projected,
                event_lookup=lambda tool_call_id: store.retrieval_event_for_tool_call(
                    self._conversation_id, tool_call_id
                ),
                active_tool_call_ids=active_tool_call_ids,
            )
            if projected == list(messages):
                return None
            return projected
        except Exception as exc:
            self._last_failure = f"renderer:{exc}"
            self._record_metric(
                "compression_failures", 1, metadata={"stage": "renderer"}
            )
            logger.warning(
                "Object Context render failed; using immutable raw context",
                exc_info=True,
            )
            return None

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
        started_at = time.perf_counter()
        projected = self._project(
            request_messages, identity_messages=conversation_messages
        )
        candidate = projected if projected is not None else request_messages
        raw_tokens = estimate_messages_tokens_rough(request_messages)
        rendered_tokens = estimate_messages_tokens_rough(candidate)
        saved_tokens = max(0, raw_tokens - rendered_tokens)
        self._last_rendered_refs = self._refs_in_messages(candidate)
        self._remember_request_projection(
            raw_tokens=raw_tokens,
            saved_tokens=saved_tokens,
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
        return projected

    def get_projection_timeline(self) -> dict[str, Any]:
        """Return content-free projection dynamics for the active conversation."""

        if self._store is None or not self._conversation_id:
            return {
                "schema_version": 1,
                "conversation_id": self._conversation_id,
                "session_id": self._object_session_id,
                "projections": [],
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
        return {
            "schema_version": 1,
            "conversation_id": self._conversation_id,
            "session_id": self._object_session_id,
            "projections": projections,
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

    def get_tool_schemas(self) -> list[dict[str, Any]]:
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
        if self._store is not None and self._conversation_id:
            try:
                object_status = self._store.aggregate_status(self._conversation_id)
            except Exception:
                logger.debug("Object Context status query failed", exc_info=True)
        status.update({
            "object_context_version": 1,
            "object_context_available": self._store is not None,
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
            "last_compressed_batch_size": self._last_batch_size,
            "last_failure": self._last_failure,
        })
        return status
