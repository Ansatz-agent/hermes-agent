"""Self-contained all-session dashboard for Object Context dynamics."""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import math
import os
import secrets
import threading
from collections import OrderedDict
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from hermes_constants import get_hermes_home
from utils import atomic_write_text


MONITOR_SCHEMA_VERSION = 12
MONITOR_DIRNAME = "object-context-monitor"
_EXCLUDED_AUXILIARY_TASKS = frozenset({"background_review"})
_MONITOR_LOOPBACK_HOST = "127.0.0.1"
logger = logging.getLogger(__name__)
_DECISION_EXPORT_FIELDS = (
    "projection_epoch_id",
    "request_attempt_id",
    "request_sequence",
    "decision_kind",
    "decision_mode",
    "decision_reason",
    "candidate_count",
    "member_delta_ids",
    "member_object_refs",
    "earliest_changed_delta_id",
    "baseline_prompt_tokens",
    "candidate_prompt_tokens",
    "gross_tokens_removed",
    "card_or_receipt_tokens",
    "baseline_reusable_prefix_tokens",
    "candidate_reusable_prefix_tokens",
    "cache_tokens_invalidated",
    "cache_penalty_equivalent_tokens",
    "known_summary_cost_equivalent_tokens",
    "net_saving_equivalent_tokens",
    "net_saving_usd",
    "cache_read_weight",
    "cache_write_weight",
    "pricing_source",
    "pricing_version",
    "estimator_source",
    "policy_version",
    "created_at",
)
_AMORTIZED_EXPORT_FIELDS = (
    *_DECISION_EXPORT_FIELDS,
    "batch_policy",
    "fixed_batch_size",
    "baseline_state",
    "cache_granularity_tokens",
    "hot_underexposed_count",
    "hot_seen_delta_count",
    "hot_seen_bucket_count",
    "hot_tail_tokens",
    "hot_overflow_tokens",
    "hot_start_token_offset",
    "pending_delta_count",
    "pending_bucket_count",
    "pending_raw_tokens",
    "pending_gain_tokens",
    "wait_area_token_requests",
    "wait_loss_now",
    "wait_loss_increment",
    "wait_loss_projected",
    "shared_cached_hot_tokens",
    "shared_overhead_equivalent_tokens",
    "crossing_margin",
    "emergency_triggered",
    "pending_count_over",
    "pending_tokens_over",
    "amortized_crossed",
    "immediate_crossed",
    "amortized_cache_read_weight",
    "amortized_baseline_prompt_tokens",
    "amortized_candidate_prompt_tokens",
    "amortized_baseline_reusable_prefix_tokens",
    "amortized_candidate_reusable_prefix_tokens",
    "immediate_cache_penalty_equivalent_tokens",
    "immediate_net_saving_equivalent_tokens",
    "immediate_net_saving_usd",
    "immediate_cache_read_weight",
    "immediate_cache_write_weight",
    "immediate_pricing_source",
    "immediate_pricing_version",
)


class _MonitorHTTPServer(ThreadingHTTPServer):
    """Loopback-only HTTP transport for a live monitor document."""

    daemon_threads = True
    block_on_close = False
    request_queue_size = 5


class _MonitorRequestHandler(BaseHTTPRequestHandler):
    """Serve exactly one unguessable monitor route without HTTP caching."""

    protocol_version = "HTTP/1.1"
    server_version = "AnsatzObjectContextMonitor"
    sys_version = ""

    def _monitor_server(self) -> "ObjectContextMonitorServer":
        return getattr(self.server, "monitor")

    def _authorized(self) -> bool:
        monitor = self._monitor_server()
        path = self.path.partition("?")[0]
        host = str(self.headers.get("Host") or "")
        return secrets.compare_digest(path, monitor.route_path) and (
            secrets.compare_digest(host, monitor.authority)
        )

    def _send_common_headers(self, *, content_type: str, length: int) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )

    def _send_plain(self, status: int, message: str, *, include_body: bool) -> None:
        body = message.encode("utf-8")
        self.send_response(status)
        self._send_common_headers(
            content_type="text/plain; charset=utf-8",
            length=len(body),
        )
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _serve_monitor(self, *, include_body: bool) -> None:
        if not self._authorized():
            self._send_plain(404, "Not found\n", include_body=include_body)
            return
        try:
            body = self._monitor_server().render_latest().encode("utf-8")
        except Exception as exc:
            logger.debug(
                "Object Context live monitor render failed (%s)",
                type(exc).__name__,
            )
            self._send_plain(
                503,
                "Monitor data is temporarily unavailable. Refresh to retry.\n",
                include_body=include_body,
            )
            return
        self.send_response(200)
        self._send_common_headers(
            content_type="text/html; charset=utf-8",
            length=len(body),
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; "
            "style-src 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'none'; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'",
        )
        self.end_headers()
        if include_body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve_monitor(include_body=True)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler contract
        self._serve_monitor(include_body=False)

    def log_message(self, _format: str, *_args: Any) -> None:
        """Keep browser refreshes out of the interactive CLI transcript."""


class ObjectContextMonitorServer:
    """A process-local live view backed by a fresh telemetry loader per GET."""

    def __init__(
        self,
        server: _MonitorHTTPServer,
        *,
        route_path: str,
        timeline_loader: Callable[[], Mapping[str, Any] | None],
        initial_timeline: Mapping[str, Any],
    ) -> None:
        self._server = server
        self._timeline_loader = timeline_loader
        self._latest_timeline = dict(initial_timeline)
        self._latest_html = render_monitor_html(self._latest_timeline)
        self._load_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False
        self.route_path = route_path
        port = int(server.server_address[1])
        self.authority = f"{_MONITOR_LOOPBACK_HOST}:{port}"
        self.url = f"http://{self.authority}{route_path}"
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
            name="object-context-monitor-http",
        )
        setattr(server, "monitor", self)
        try:
            self._thread.start()
        except BaseException:
            self._closed = True
            server.server_close()
            raise
        atexit.register(self.close)

    @property
    def is_running(self) -> bool:
        return not self._closed and self._thread.is_alive()

    def render_latest(self) -> str:
        """Reload persisted telemetry and fall back to the last good document."""

        with self._load_lock:
            candidate: Mapping[str, Any] | None = None
            try:
                loaded = self._timeline_loader()
                if isinstance(loaded, Mapping):
                    candidate = dict(loaded)
            except Exception as exc:
                logger.debug(
                    "Object Context live monitor reload failed (%s); using last snapshot",
                    type(exc).__name__,
                )

            if candidate is not None:
                try:
                    rendered = render_monitor_html(candidate)
                except Exception as exc:
                    logger.debug(
                        "Object Context live monitor rejected refreshed telemetry (%s); "
                        "using last snapshot",
                        type(exc).__name__,
                    )
                else:
                    self._latest_timeline = dict(candidate)
                    self._latest_html = rendered
                    return rendered
            return self._latest_html

    def close(self) -> None:
        """Stop accepting refreshes and release the loopback socket."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        try:
            if self._thread.is_alive():
                self._server.shutdown()
        finally:
            self._server.server_close()
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=1.0)
            try:
                atexit.unregister(self.close)
            except Exception:
                pass


def start_monitor_server(
    *,
    timeline_loader: Callable[[], Mapping[str, Any] | None],
    initial_timeline: Mapping[str, Any],
) -> ObjectContextMonitorServer:
    """Start a token-gated monitor on an ephemeral IPv4 loopback port."""

    route_path = f"/{secrets.token_urlsafe(24)}/"
    server = _MonitorHTTPServer(
        (_MONITOR_LOOPBACK_HOST, 0),
        _MonitorRequestHandler,
    )
    try:
        return ObjectContextMonitorServer(
            server,
            route_path=route_path,
            timeline_loader=timeline_loader,
            initial_timeline=initial_timeline,
        )
    except BaseException:
        server.server_close()
        raise


_AMORTIZED_SIGNAL_FIELDS = frozenset(
    field
    for field in _AMORTIZED_EXPORT_FIELDS
    if field not in _DECISION_EXPORT_FIELDS
    and field
    not in {"baseline_state", "cache_granularity_tokens", "emergency_triggered"}
)
_AMORTIZED_FLAG_FIELDS = frozenset({
    "emergency_triggered",
    "pending_count_over",
    "pending_tokens_over",
    "amortized_crossed",
    "immediate_crossed",
})
_DECISION_TOKEN_FIELDS = frozenset({
    "projection_epoch_id",
    "request_attempt_id",
    "decision_kind",
    "decision_mode",
    "decision_reason",
    "pricing_source",
    "pricing_version",
    "estimator_source",
    "policy_version",
    "baseline_state",
    "batch_policy",
    "earliest_changed_delta_id",
    "immediate_pricing_source",
    "immediate_pricing_version",
})
_REQUEST_OBSERVATION_EXPORT_FIELDS = (
    "request_attempt_id",
    "success_sequence",
    "exposure_request_sequence",
    "route_namespace_hash",
    "outcome",
    "raw_delta_count",
    "accrued_delta_count",
    "skipped_pending_delta_count",
    "newly_eligible_delta_count",
    "created_at",
)
_DECISION_NONNEGATIVE_INTEGER_FIELDS = frozenset({
    "request_sequence",
    "candidate_count",
    "baseline_prompt_tokens",
    "candidate_prompt_tokens",
    "gross_tokens_removed",
    "card_or_receipt_tokens",
    "baseline_reusable_prefix_tokens",
    "candidate_reusable_prefix_tokens",
    "cache_tokens_invalidated",
    "cache_granularity_tokens",
    "fixed_batch_size",
    "hot_underexposed_count",
    "hot_seen_delta_count",
    "hot_seen_bucket_count",
    "hot_tail_tokens",
    "hot_overflow_tokens",
    "hot_start_token_offset",
    "pending_delta_count",
    "pending_bucket_count",
    "pending_raw_tokens",
    "pending_gain_tokens",
    "shared_cached_hot_tokens",
    "amortized_baseline_prompt_tokens",
    "amortized_candidate_prompt_tokens",
    "amortized_baseline_reusable_prefix_tokens",
    "amortized_candidate_reusable_prefix_tokens",
})
_DECISION_NONNEGATIVE_NUMBER_FIELDS = frozenset({
    "cache_penalty_equivalent_tokens",
    "known_summary_cost_equivalent_tokens",
    "cache_read_weight",
    "cache_write_weight",
    "wait_area_token_requests",
    "wait_loss_now",
    "wait_loss_increment",
    "wait_loss_projected",
    "shared_overhead_equivalent_tokens",
    "amortized_cache_read_weight",
    "immediate_cache_penalty_equivalent_tokens",
    "immediate_cache_read_weight",
    "immediate_cache_write_weight",
    "created_at",
})
_DECISION_SIGNED_NUMBER_FIELDS = frozenset({
    "net_saving_equivalent_tokens",
    "net_saving_usd",
    "crossing_margin",
    "immediate_net_saving_equivalent_tokens",
    "immediate_net_saving_usd",
})


def _content_free_token(value: Any) -> str:
    text = str(value or "").strip()
    if not (1 <= len(text) <= 128):
        return ""
    if not all(
        character.isascii()
        and (character.isalnum() or character in "._:+-/@")
        for character in text
    ):
        return ""
    return text


def _optional_finite_number(value: Any, *, nonnegative: bool) -> float | None:
    if value is None or isinstance(value, (bool, str, bytes, bytearray)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0):
        return None
    return number


def _optional_nonnegative_integer(value: Any) -> int | None:
    number = _optional_finite_number(value, nonnegative=True)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _is_v12_decision(decision: Mapping[str, Any]) -> bool:
    """Recognize explicit and early V1.2 rows without relabelling V1.1."""

    version = str(decision.get("policy_version") or "").strip().casefold()
    version_tokens = (
        version.replace("_", "-").replace(":", "-").split("-")
        if version
        else ()
    )
    explicit_v12 = any(
        token in {"1.2", "v1.2"}
        or token.startswith(("1.2.", "v1.2."))
        for token in version_tokens
    )
    if explicit_v12:
        return True
    if version:
        return False
    return any(decision.get(field) is not None for field in _AMORTIZED_SIGNAL_FIELDS)


def _decision_counts(
    decisions: Sequence[Mapping[str, Any]], field: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for decision in decisions:
        value = _content_free_token(decision.get(field))
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _decision_flag(decision: Mapping[str, Any], field: str) -> bool:
    return decision.get(field) is True


def _sanitize_decision(
    decision: Mapping[str, Any], fields: Sequence[str]
) -> dict[str, Any]:
    """Copy only the audited content-free decision telemetry contract."""

    sanitized = {field: decision.get(field) for field in fields}
    for field in _DECISION_TOKEN_FIELDS.intersection(fields):
        value = _content_free_token(decision.get(field))
        sanitized[field] = value or None
    for field in ("member_delta_ids", "member_object_refs"):
        if field not in fields:
            continue
        raw_values = decision.get(field)
        values = (
            raw_values
            if isinstance(raw_values, Sequence)
            and not isinstance(raw_values, (str, bytes, bytearray))
            else ()
        )
        sanitized[field] = [
            token
            for token in (_content_free_token(value) for value in values)
            if token
        ]
    for field in _AMORTIZED_FLAG_FIELDS.intersection(fields):
        value = decision.get(field)
        sanitized[field] = value if isinstance(value, bool) else None
    for field in _DECISION_NONNEGATIVE_INTEGER_FIELDS.intersection(fields):
        sanitized[field] = _optional_nonnegative_integer(decision.get(field))
    for field in _DECISION_NONNEGATIVE_NUMBER_FIELDS.intersection(fields):
        sanitized[field] = _optional_finite_number(
            decision.get(field), nonnegative=True
        )
    for field in _DECISION_SIGNED_NUMBER_FIELDS.intersection(fields):
        sanitized[field] = _optional_finite_number(
            decision.get(field), nonnegative=False
        )
    return sanitized


def _sanitize_request_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    """Whitelist the store's content-free successful-request summary."""

    sanitized = {
        field: observation.get(field)
        for field in _REQUEST_OBSERVATION_EXPORT_FIELDS
    }
    for field in ("request_attempt_id", "route_namespace_hash", "outcome"):
        value = _content_free_token(observation.get(field))
        sanitized[field] = value or None
    for field in (
        "success_sequence",
        "exposure_request_sequence",
        "raw_delta_count",
        "accrued_delta_count",
        "skipped_pending_delta_count",
        "newly_eligible_delta_count",
    ):
        sanitized[field] = _optional_nonnegative_integer(observation.get(field))
    sanitized["created_at"] = _optional_finite_number(
        observation.get("created_at"), nonnegative=True
    )
    return sanitized


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number)


def _finite_number(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _cumulative(values: Sequence[float]) -> list[float]:
    total = 0.0
    result: list[float] = []
    for value in values:
        total += _finite_nonnegative(value)
        result.append(total)
    return result


def _percentage(numerator: Any, denominator: Any) -> float:
    """Return a bounded percentage, failing closed for a zero denominator."""

    safe_denominator = _finite_nonnegative(denominator)
    if safe_denominator <= 0:
        return 0.0
    percentage = _finite_nonnegative(numerator) / safe_denominator * 100.0
    return min(100.0, percentage)


def _cumulative_percentages(
    numerators: Sequence[float], denominators: Sequence[float]
) -> list[float]:
    """Return cumulative numerator / cumulative denominator percentages."""

    numerator_total = 0.0
    denominator_total = 0.0
    result: list[float] = []
    for numerator, denominator in zip(numerators, denominators, strict=True):
        numerator_total += _finite_nonnegative(numerator)
        denominator_total += _finite_nonnegative(denominator)
        result.append(_percentage(numerator_total, denominator_total))
    return result


def _metric(event: Mapping[str, Any], name: str) -> float:
    metrics = event.get("metrics")
    if not isinstance(metrics, Mapping):
        return 0.0
    return _finite_nonnegative(metrics.get(name))


def _has_metrics(event: Mapping[str, Any], *names: str) -> bool:
    metrics = event.get("metrics")
    return isinstance(metrics, Mapping) and all(name in metrics for name in names)


def _sequence_label(event: Mapping[str, Any], fallback: int) -> str:
    try:
        sequence = int(event.get("projection_sequence") or fallback)
    except (TypeError, ValueError):
        sequence = fallback
    return f"P{max(1, sequence)}"


def _auxiliary_total(row: Mapping[str, Any]) -> float:
    if "total_tokens" in row:
        return _finite_nonnegative(row.get("total_tokens"))
    return sum(
        _finite_nonnegative(row.get(field))
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )


def _compression_overhead(
    timeline: Mapping[str, Any], *, gross_saved: float, project_count: int
) -> dict[str, Any]:
    """Build a non-double-counting compression overhead ledger.

    Provider usage rows are exact when the provider returned usage.  Schema,
    Card-text, and retrieved-payload values are rough token estimates.  Card
    text and retrieved payload already live in the rendered conversation view,
    so they are exposed for diagnosis but never subtracted a second time.
    """

    usage_rows = [
        row
        for row in timeline.get("auxiliary_usage") or []
        if isinstance(row, Mapping)
        and str(row.get("task") or "").strip()
        not in _EXCLUDED_AUXILIARY_TASKS
    ]
    by_task: dict[str, dict[str, float]] = {}
    for row in usage_rows:
        task = str(row.get("task") or "").strip()
        if not task:
            continue
        aggregate = by_task.setdefault(task, {"tokens": 0.0, "calls": 0.0})
        aggregate["tokens"] += _auxiliary_total(row)
        aggregate["calls"] += _finite_nonnegative(row.get("api_call_count"))

    card_usage = by_task.get("object_context_card_summary", {})
    legacy_usage = by_task.get("compression", {})
    card_inference = _finite_nonnegative(card_usage.get("tokens"))
    card_calls = int(_finite_nonnegative(card_usage.get("calls")))
    summary_inference = _finite_nonnegative(legacy_usage.get("tokens"))
    summary_calls = int(_finite_nonnegative(legacy_usage.get("calls")))
    exact_inference = card_inference + summary_inference

    raw_metrics = timeline.get("object_context_metrics")
    metrics = raw_metrics if isinstance(raw_metrics, Mapping) else {}
    card_text = _finite_nonnegative(metrics.get("card_tokens"))
    retrieved_payload = _finite_nonnegative(metrics.get("retrieved_tokens"))
    card_attempts = int(_finite_nonnegative(metrics.get("card_summary_attempts")))
    summary_fallbacks = int(_finite_nonnegative(metrics.get("summary_fallbacks")))
    retrieval_count = int(_finite_nonnegative(metrics.get("retrieval_count")))
    schema_per_request = _finite_nonnegative(
        timeline.get("retrieve_object_schema_tokens_per_request")
    )
    schema_tokens = schema_per_request * max(0, int(project_count))
    known_overhead = exact_inference + schema_tokens
    known_net = _finite_number(gross_saved) - known_overhead

    other_auxiliary = sum(
        values["tokens"]
        for task, values in by_task.items()
        if task not in {"compression", "object_context_card_summary"}
    )
    all_auxiliary = exact_inference + other_auxiliary
    unmetered_attempts = max(0, card_attempts - card_calls)
    legacy_ambiguous = bool(summary_calls and project_count)
    legacy_metric_gap = bool(card_text and card_attempts == 0 and card_calls == 0)
    coverage_complete = not (
        unmetered_attempts or legacy_ambiguous or legacy_metric_gap
    )
    if legacy_ambiguous:
        coverage_label = "历史 combined task：普通 summary 与旧 Card 无法拆分"
    elif legacy_metric_gap:
        coverage_label = "历史 Card 缺少 attempt/provider usage 明细"
    elif unmetered_attempts:
        coverage_label = (
            f"{unmetered_attempts} 次 Card attempt 无 provider usage；"
            "可能为失败、限流或无 usage 响应"
        )
    else:
        coverage_label = "已记录的 compression inference usage 完整"

    components = [
        {
            "key": "object_context_card_summary",
            "label": "OC Card summary inference",
            "tokens": round(card_inference, 6),
            "calls": card_calls,
            "measurement": "provider usage · exact",
            "treatment": "从 Gross Saved 扣除",
            "deducted": True,
        },
        {
            "key": "compression",
            "label": "Context summary / legacy Card inference",
            "tokens": round(summary_inference, 6),
            "calls": summary_calls,
            "measurement": "provider usage · exact, historical task may be combined",
            "treatment": "从 Gross Saved 扣除",
            "deducted": True,
        },
        {
            "key": "retrieve_object_schema",
            "label": "retrieve_object tool schema",
            "tokens": round(schema_tokens, 6),
            "calls": max(0, int(project_count)),
            "measurement": (
                f"rough estimate · {round(schema_per_request, 6):g} tok/request"
            ),
            "treatment": "从 Gross Saved 扣除",
            "deducted": True,
        },
        {
            "key": "object_card_text",
            "label": "OBJECT_CARD text footprint",
            "tokens": round(card_text, 6),
            "calls": card_attempts,
            "measurement": "rough message-token estimate",
            "treatment": "已在 rendered 中，不二次扣除",
            "deducted": False,
        },
        {
            "key": "retrieved_payload",
            "label": "Retrieved payload / continuation",
            "tokens": round(retrieved_payload, 6),
            "calls": retrieval_count,
            "measurement": "rough message-token estimate",
            "treatment": "已在 rendered 中，不二次扣除",
            "deducted": False,
        },
    ]
    for task, values in sorted(by_task.items()):
        if task in {"compression", "object_context_card_summary"}:
            continue
        components.append(
            {
                "key": f"auxiliary:{task}",
                "label": f"Other auxiliary · {task}",
                "tokens": round(values["tokens"], 6),
                "calls": int(values["calls"]),
                "measurement": "provider usage · exact",
                "treatment": "非 compression；仅计入 Known Provider",
                "deducted": False,
            }
        )
    return {
        "gross_saved_tokens": round(gross_saved, 6),
        "exact_inference_tokens": round(exact_inference, 6),
        "rough_schema_tokens": round(schema_tokens, 6),
        "known_overhead_tokens": round(known_overhead, 6),
        "known_net_saved_tokens": round(known_net, 6),
        "card_text_tokens": round(card_text, 6),
        "retrieved_payload_tokens": round(retrieved_payload, 6),
        "other_auxiliary_tokens": round(other_auxiliary, 6),
        "all_auxiliary_tokens": round(all_auxiliary, 6),
        "card_summary_attempts": card_attempts,
        "recorded_card_summary_calls": card_calls,
        "unmetered_card_summary_attempts": unmetered_attempts,
        "summary_fallbacks": summary_fallbacks,
        "coverage_complete": coverage_complete,
        "coverage_label": coverage_label,
        "limitations": [
            "provider failures/retries without usage cannot be assigned exact tokens",
            "schema estimate counts projected requests; transport retries may add unmetered schema",
        ],
        "components": components,
    }


def build_monitor_payload(timeline: Mapping[str, Any]) -> dict[str, Any]:
    """Build one session's content-free chart payload."""

    raw_events = timeline.get("projections")
    all_events = [
        event for event in raw_events or [] if isinstance(event, Mapping)
    ]
    raw_requests = timeline.get("requests")
    requests = [
        event for event in raw_requests or [] if isinstance(event, Mapping)
    ]
    # Background-review agents deliberately reuse the parent conversation's
    # Object Context store, so their projections carry the same
    # ``conversation_id``.  Durable SessionDB request usage, however, contains
    # only the user-facing main loop.  When that exact request identity exists,
    # retain projections only for its turns.  Legacy projections have no turn
    # identity and remain visible rather than being silently erased; sessions
    # without any request timeline likewise preserve historical behavior.
    main_turn_ids = {
        str(event.get("turn_id") or "").strip()
        for event in requests
        if str(event.get("turn_id") or "").strip()
    }
    events = (
        [
            event
            for event in all_events
            if bool(event.get("legacy"))
            or str(event.get("turn_id") or "").strip() in main_turn_ids
        ]
        if main_turn_ids
        else all_events
    )
    legacy_count = sum(1 for event in events if bool(event.get("legacy")))
    turnless_count = sum(
        1
        for event in events
        if bool(event.get("legacy")) or not str(event.get("turn_id") or "")
    )
    request_turnless_count = sum(
        1 for event in requests if not str(event.get("turn_id") or "")
    )

    all_decisions: list[dict[str, Any]] = []
    seen_decision_ids: set[str] = set()
    for source_name in ("economic_decisions", "amortized_decisions"):
        raw_decisions = timeline.get(source_name)
        for decision in raw_decisions or []:
            if not isinstance(decision, Mapping):
                continue
            copied = dict(decision)
            identity = str(copied.get("projection_epoch_id") or "").strip()
            if identity and identity in seen_decision_ids:
                continue
            if identity:
                seen_decision_ids.add(identity)
            all_decisions.append(copied)
    amortized_decisions = [
        decision for decision in all_decisions if _is_v12_decision(decision)
    ]
    # Missing policy_version is the historical V1.1 representation.  Keep it
    # in the original group rather than retroactively reclassifying it.
    economic_decisions = [
        decision for decision in all_decisions if not _is_v12_decision(decision)
    ]
    request_observations = [
        _sanitize_request_observation(observation)
        for observation in timeline.get("request_observations") or []
        if isinstance(observation, Mapping)
    ]
    economic_metrics_available = bool(economic_decisions)
    amortized_metrics_available = bool(amortized_decisions)
    normal_decisions = [
        decision
        for decision in economic_decisions
        if str(decision.get("decision_mode") or "normal") == "normal"
    ]
    emergency_decisions = [
        decision
        for decision in economic_decisions
        if str(decision.get("decision_mode") or "normal") == "emergency"
    ]

    def decision_series(
        rows: Sequence[Mapping[str, Any]],
        field: str,
        *,
        label_prefix: str = "D",
    ) -> tuple[list[str], list[str], list[float]]:
        selected = [
            (ordinal, row)
            for ordinal, row in enumerate(rows, start=1)
            if row.get(field) is not None
        ]
        labels = [
            f"{label_prefix}{ordinal}" for ordinal, _row in selected
        ]
        identities = [
            " · ".join(
                part
                for part in (
                    _content_free_token(row.get("projection_epoch_id"))
                    or labels[index],
                    _content_free_token(row.get("decision_reason")),
                )
                if part
            )
            for index, (_ordinal, row) in enumerate(selected)
        ]
        values = [_finite_number(row.get(field)) for _ordinal, row in selected]
        return labels, identities, values

    request_labels = [f"R{index}" for index in range(1, len(requests) + 1)]
    request_ids = [
        str(event.get("api_request_id") or request_labels[index])
        for index, event in enumerate(requests)
    ]
    request_spent = [_metric(event, "total_tokens") for event in requests]
    request_prompt = [_metric(event, "prompt_tokens") for event in requests]
    request_time = [_metric(event, "api_duration_ms") for event in requests]

    project_labels = [
        _sequence_label(event, index) for index, event in enumerate(events, start=1)
    ]
    project_ids = [
        str(event.get("projection_id") or project_labels[index])
        for index, event in enumerate(events)
    ]
    project_saved = [_metric(event, "tokens_saved") for event in events]
    project_raw = [_metric(event, "raw_context_tokens") for event in events]
    conversation_events = [
        event
        for event in events
        if _has_metrics(
            event,
            "raw_conversation_tokens",
            "rendered_conversation_tokens",
            "conversation_tokens_saved",
        )
    ]
    conversation_project_labels = [
        _sequence_label(event, index)
        for index, event in enumerate(conversation_events, start=1)
    ]
    conversation_project_ids = [
        str(event.get("projection_id") or conversation_project_labels[index])
        for index, event in enumerate(conversation_events)
    ]
    conversation_project_saved = [
        _metric(event, "conversation_tokens_saved")
        for event in conversation_events
    ]
    conversation_project_raw = [
        _metric(event, "raw_conversation_tokens")
        for event in conversation_events
    ]
    conversation_project_rendered = [
        _metric(event, "rendered_conversation_tokens")
        for event in conversation_events
    ]
    # Preserve the original assembled-message savings series.  With Object
    # Context off there are no projections, so savings are a real zero-valued
    # series aligned to universal model requests.
    savings_project_labels = project_labels or request_labels
    savings_project_ids = project_ids or request_ids
    savings_project_values = project_saved or [0.0] * len(requests)
    savings_project_raw = project_raw or request_prompt
    project_saved_percent = [
        _percentage(saved, raw)
        for saved, raw in zip(
            savings_project_values, savings_project_raw, strict=True
        )
    ]
    conversation_project_saved_percent = [
        _percentage(saved, raw)
        for saved, raw in zip(
            conversation_project_saved,
            conversation_project_raw,
            strict=True,
        )
    ]

    request_turns: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    for event in requests:
        turn_id = str(event.get("turn_id") or "")
        if not turn_id:
            continue
        turn = request_turns.setdefault(
            turn_id,
            {
                "spent": 0.0,
                "time": 0.0,
                "prompt": 0.0,
                "read": 0.0,
                "write": 0.0,
                "uncached": 0.0,
                "requests": 0.0,
            },
        )
        turn["spent"] += _metric(event, "total_tokens")
        turn["time"] += _metric(event, "api_duration_ms")
        turn["prompt"] += _metric(event, "prompt_tokens")
        turn["read"] += _metric(event, "cache_read_tokens")
        turn["write"] += _metric(event, "cache_write_tokens")
        turn["uncached"] += _metric(event, "input_tokens")
        turn["requests"] += 1

    projection_turns: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    for event in events:
        turn_id = str(event.get("turn_id") or "")
        if bool(event.get("legacy")) or not turn_id:
            continue
        turn = projection_turns.setdefault(turn_id, {"saved": 0.0, "raw": 0.0})
        turn["saved"] += _metric(event, "tokens_saved")
        turn["raw"] += _metric(event, "raw_context_tokens")

    turn_ids = list(request_turns)
    turn_ids.extend(
        turn_id for turn_id in projection_turns if turn_id not in request_turns
    )
    turn_labels = [f"T{index}" for index in range(1, len(turn_ids) + 1)]
    turn_saved = [
        float(projection_turns.get(turn_id, {}).get("saved", 0.0))
        for turn_id in turn_ids
    ]
    turn_raw = [
        float(
            projection_turns.get(turn_id, {}).get(
                "raw", request_turns.get(turn_id, {}).get("prompt", 0.0)
            )
        )
        for turn_id in turn_ids
    ]
    turn_saved_percent = [
        _percentage(saved, raw)
        for saved, raw in zip(turn_saved, turn_raw, strict=True)
    ]
    request_turn_ids = list(request_turns)
    request_turn_labels = [
        f"T{turn_ids.index(turn_id) + 1}" for turn_id in request_turn_ids
    ]
    turn_spent = [request_turns[turn_id]["spent"] for turn_id in request_turn_ids]
    turn_time = [request_turns[turn_id]["time"] for turn_id in request_turn_ids]

    # Universal request telemetry owns cache data too.  Object Context's old
    # cache store remains a compatibility fallback for pre-v27 sessions whose
    # request log has already rotated away.
    request_cache_events = [
        event for event in requests if _metric(event, "prompt_tokens") > 0
    ]
    raw_cache_events = timeline.get("cache_requests")
    cache_events = request_cache_events or [
        event
        for event in raw_cache_events or []
        if isinstance(event, Mapping)
        and _metric(event, "prompt_tokens") > 0
    ]
    cache_labels = [f"R{index}" for index in range(1, len(cache_events) + 1)]
    cache_ids = [
        str(
            event.get("api_request_id")
            or event.get("cache_request_id")
            or cache_labels[index]
        )
        for index, event in enumerate(cache_events)
    ]
    cache_prompt = [_metric(event, "prompt_tokens") for event in cache_events]
    cache_read = [_metric(event, "cache_read_tokens") for event in cache_events]
    cache_write = [_metric(event, "cache_write_tokens") for event in cache_events]
    cache_uncached = [
        _metric(event, "input_tokens")
        or _metric(event, "uncached_input_tokens")
        for event in cache_events
    ]
    cache_hit_percent = [
        _percentage(read, prompt)
        for read, prompt in zip(cache_read, cache_prompt, strict=True)
    ]

    cache_turns: "OrderedDict[str, dict[str, float]]" = OrderedDict()
    for event in cache_events:
        turn_id = str(event.get("turn_id") or "")
        if not turn_id:
            continue
        turn = cache_turns.setdefault(
            turn_id,
            {"prompt": 0.0, "read": 0.0, "write": 0.0, "uncached": 0.0},
        )
        turn["prompt"] += _metric(event, "prompt_tokens")
        turn["read"] += _metric(event, "cache_read_tokens")
        turn["write"] += _metric(event, "cache_write_tokens")
        turn["uncached"] += (
            _metric(event, "input_tokens")
            or _metric(event, "uncached_input_tokens")
        )
    cache_turn_ids = list(cache_turns)
    cache_turn_labels: list[str] = []
    extra_turn_number = len(turn_ids)
    for turn_id in cache_turn_ids:
        if turn_id in turn_ids:
            cache_turn_labels.append(f"T{turn_ids.index(turn_id) + 1}")
        else:
            extra_turn_number += 1
            cache_turn_labels.append(f"T{extra_turn_number}")
    cache_turn_prompt = [cache_turns[turn_id]["prompt"] for turn_id in cache_turn_ids]
    cache_turn_read = [cache_turns[turn_id]["read"] for turn_id in cache_turn_ids]
    cache_turn_hit_percent = [
        _percentage(read, prompt)
        for read, prompt in zip(
            cache_turn_read, cache_turn_prompt, strict=True
        )
    ]

    economic_series = {
        field: decision_series(economic_decisions, field)
        for field in (
            "gross_tokens_removed",
            "card_or_receipt_tokens",
            "cache_penalty_equivalent_tokens",
            "known_summary_cost_equivalent_tokens",
        )
    }
    normal_net_series = decision_series(
        normal_decisions, "net_saving_equivalent_tokens"
    )
    emergency_net_series = decision_series(
        emergency_decisions, "net_saving_equivalent_tokens"
    )
    amortized_series = {
        field: decision_series(
            amortized_decisions,
            field,
            label_prefix="A",
        )
        for field in (
            "hot_tail_tokens",
            "hot_overflow_tokens",
            "pending_bucket_count",
            "pending_raw_tokens",
            "pending_gain_tokens",
            "wait_loss_projected",
            "shared_overhead_equivalent_tokens",
            "crossing_margin",
            "amortized_crossed",
            "pending_count_over",
            "pending_tokens_over",
            "immediate_crossed",
            "immediate_net_saving_equivalent_tokens",
        )
    }
    if economic_metrics_available:
        economic_empty_message = "No scored decision exposes this metric."
    elif amortized_metrics_available:
        economic_empty_message = (
            "V1.1 economic charts are separate; this session contains V1.2 "
            "scheduler telemetry only."
        )
    else:
        economic_empty_message = (
            "V1.1 economic metrics unavailable for this legacy session."
        )
    amortized_empty_message = (
        "V1.2 amortized metrics unavailable for this V1.1/legacy session."
        if not amortized_metrics_available
        else "No V1.2 decision exposes this metric."
    )
    amortized_mode_counts = _decision_counts(
        amortized_decisions, "decision_mode"
    )
    amortized_reason_counts = _decision_counts(
        amortized_decisions, "decision_reason"
    )
    amortized_mode_label = ", ".join(
        f"{name}={count}" for name, count in amortized_mode_counts.items()
    ) or "none"
    amortized_reason_label = ", ".join(
        f"{name}={count}" for name, count in amortized_reason_counts.items()
    ) or "none"

    def chart(
        key: str,
        title: str,
        axis: str,
        unit: str,
        labels: Sequence[str],
        values: Sequence[float],
        *,
        ids: Sequence[str] | None = None,
        modes: Mapping[str, Mapping[str, Any]] | None = None,
        empty_message: str = "",
        signed: bool = False,
    ) -> dict[str, Any]:
        payload = {
            "key": key,
            "title": title,
            "axis": axis,
            "unit": unit,
            "labels": list(labels),
            "ids": list(ids or labels),
            "values": [
                round(
                    _finite_number(value)
                    if signed
                    else _finite_nonnegative(value),
                    6,
                )
                for value in values
            ],
        }
        if signed:
            payload["signed"] = True
        if empty_message:
            payload["empty_message"] = empty_message
        if modes:
            payload["modes"] = {
                mode_key: {
                    "label": str(mode["label"]),
                    "title": str(mode["title"]),
                    "unit": str(mode["unit"]),
                    "values": [
                        round(_finite_nonnegative(value), 6)
                        for value in mode["values"]
                    ],
                }
                for mode_key, mode in modes.items()
            }
        return payload

    def savings_modes(
        absolute_title: str,
        percentage_title: str,
        absolute_values: Sequence[float],
        percentage_values: Sequence[float],
    ) -> dict[str, dict[str, Any]]:
        return {
            "absolute": {
                "label": "Token 数",
                "title": absolute_title,
                "unit": "tokens",
                "values": absolute_values,
            },
            "relative": {
                "label": "节省比例",
                "title": percentage_title,
                "unit": "percent",
                "values": percentage_values,
            },
        }

    def cache_modes(
        percentage_title: str,
        absolute_title: str,
        percentage_values: Sequence[float],
        absolute_values: Sequence[float],
    ) -> dict[str, dict[str, Any]]:
        return {
            "relative": {
                "label": "命中率",
                "title": percentage_title,
                "unit": "percent",
                "values": percentage_values,
            },
            "absolute": {
                "label": "命中 Token",
                "title": absolute_title,
                "unit": "tokens",
                "values": absolute_values,
            },
        }

    groups = [
        {
            "key": "saved",
            "title": "Token 节省",
            "description": (
                "原始 assembled message view：绝对值 raw − rendered · "
                "比例 saved / raw"
            ),
            "color": "#18a77b",
            "default_display_mode": "absolute",
            "display_modes": [
                {"key": "absolute", "label": "Token 数"},
                {"key": "relative", "label": "节省比例"},
            ],
            "charts": [
                chart(
                    "project-saved",
                    "Project · 节省",
                    "Project 轮次",
                    "tokens",
                    savings_project_labels,
                    savings_project_values,
                    ids=savings_project_ids,
                    modes=savings_modes(
                        "Project · 节省",
                        "Project · 节省率",
                        savings_project_values,
                        project_saved_percent,
                    ),
                ),
                chart(
                    "project-saved-cumulative",
                    "Project · 累计节省",
                    "Project 轮次",
                    "tokens",
                    savings_project_labels,
                    _cumulative(savings_project_values),
                    ids=savings_project_ids,
                    modes=savings_modes(
                        "Project · 累计节省",
                        "Project · 累计节省率",
                        _cumulative(savings_project_values),
                        _cumulative_percentages(
                            savings_project_values, savings_project_raw
                        ),
                    ),
                ),
                chart(
                    "turn-saved",
                    "Turn · 节省",
                    "Turn 轮次",
                    "tokens",
                    turn_labels,
                    turn_saved,
                    ids=turn_ids,
                    modes=savings_modes(
                        "Turn · 节省",
                        "Turn · 节省率",
                        turn_saved,
                        turn_saved_percent,
                    ),
                ),
                chart(
                    "turn-saved-cumulative",
                    "Turn · 累计节省",
                    "Turn 轮次",
                    "tokens",
                    turn_labels,
                    _cumulative(turn_saved),
                    ids=turn_ids,
                    modes=savings_modes(
                        "Turn · 累计节省",
                        "Turn · 累计节省率",
                        _cumulative(turn_saved),
                        _cumulative_percentages(turn_saved, turn_raw),
                    ),
                ),
            ],
        },
        {
            "key": "conversation_saved",
            "title": "对话记录 Token 节省",
            "description": (
                "额外指标：仅持久化 user / assistant / tool 对话，排除 "
                "system prompt、tool schema、prefill 与其他临时请求内容"
            ),
            "color": "#0f9f88",
            "default_display_mode": "absolute",
            "display_modes": [
                {"key": "absolute", "label": "Token 数"},
                {"key": "relative", "label": "节省比例"},
            ],
            "charts": [
                chart(
                    "conversation-project-saved",
                    "Project · 对话记录节省",
                    "Project 轮次",
                    "tokens",
                    conversation_project_labels,
                    conversation_project_saved,
                    ids=conversation_project_ids,
                    modes=savings_modes(
                        "Project · 对话记录节省",
                        "Project · 对话记录压缩率",
                        conversation_project_saved,
                        conversation_project_saved_percent,
                    ),
                    empty_message=(
                        "该 session 尚无 conversation-only 遥测；旧的 Token "
                        "节省图仍可正常查看，重启 CLI 后的新模型请求会开始记录。"
                    ),
                )
            ],
        },
        {
            "key": "economic",
            "title": "V1.1 即时经济决策",
            "description": (
                "每次请求的 content-free 决策分解；normal 与 emergency "
                "净值分开呈现，历史 V1 会话不补零"
            ),
            "color": "#c2418c",
            "charts": [
                chart(
                    "economic-gross",
                    "Decision · Gross removed",
                    "经济决策",
                    "tokens",
                    economic_series["gross_tokens_removed"][0],
                    economic_series["gross_tokens_removed"][2],
                    ids=economic_series["gross_tokens_removed"][1],
                    empty_message=economic_empty_message,
                ),
                chart(
                    "economic-replacement",
                    "Decision · Card / receipt footprint",
                    "经济决策",
                    "tokens",
                    economic_series["card_or_receipt_tokens"][0],
                    economic_series["card_or_receipt_tokens"][2],
                    ids=economic_series["card_or_receipt_tokens"][1],
                    empty_message=economic_empty_message,
                ),
                chart(
                    "economic-cache-penalty",
                    "Decision · Cache rewrite penalty",
                    "经济决策",
                    "tokens",
                    economic_series["cache_penalty_equivalent_tokens"][0],
                    economic_series["cache_penalty_equivalent_tokens"][2],
                    ids=economic_series["cache_penalty_equivalent_tokens"][1],
                    empty_message=economic_empty_message,
                ),
                chart(
                    "economic-summary-cost",
                    "Decision · Known summary cost",
                    "经济决策",
                    "tokens",
                    economic_series[
                        "known_summary_cost_equivalent_tokens"
                    ][0],
                    economic_series[
                        "known_summary_cost_equivalent_tokens"
                    ][2],
                    ids=economic_series[
                        "known_summary_cost_equivalent_tokens"
                    ][1],
                    empty_message=economic_empty_message,
                ),
                chart(
                    "economic-normal-net",
                    "Normal decision · Immediate net",
                    "normal 经济决策",
                    "tokens",
                    normal_net_series[0],
                    normal_net_series[2],
                    ids=normal_net_series[1],
                    empty_message=economic_empty_message,
                    signed=True,
                ),
                chart(
                    "economic-emergency-net",
                    "Emergency decision · Immediate net",
                    "emergency 经济决策",
                    "tokens",
                    emergency_net_series[0],
                    emergency_net_series[2],
                    ids=emergency_net_series[1],
                    empty_message=economic_empty_message,
                    signed=True,
                ),
            ],
        },
        {
            "key": "amortized",
            "title": "V1.2 摊销与容量决策",
            "description": (
                "独立的 content-free V1.2 调度遥测；W 是 projected waiting "
                "loss，Q 是 shared rewrite cost，二者都是 crossing 信号而非"
                "已实现的 Token 收益。"
                " "
                f"mode: {amortized_mode_label} · reason: {amortized_reason_label}"
            ),
            "color": "#0f766e",
            "charts": [
                chart(
                    "amortized-hot-tokens",
                    "Hot Tail · Raw tokens",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["hot_tail_tokens"][0],
                    amortized_series["hot_tail_tokens"][2],
                    ids=amortized_series["hot_tail_tokens"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-hot-overflow",
                    "Hot Tail · RAW_UNSEEN overflow",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["hot_overflow_tokens"][0],
                    amortized_series["hot_overflow_tokens"][2],
                    ids=amortized_series["hot_overflow_tokens"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-pending-buckets",
                    "Pending · Distinct buckets",
                    "V1.2 决策",
                    "count",
                    amortized_series["pending_bucket_count"][0],
                    amortized_series["pending_bucket_count"][2],
                    ids=amortized_series["pending_bucket_count"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-pending-raw",
                    "Pending · Raw tokens",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["pending_raw_tokens"][0],
                    amortized_series["pending_raw_tokens"][2],
                    ids=amortized_series["pending_raw_tokens"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-pending-gain",
                    "Pending · Compression gain",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["pending_gain_tokens"][0],
                    amortized_series["pending_gain_tokens"][2],
                    ids=amortized_series["pending_gain_tokens"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-w-projected",
                    "W · Projected waiting loss",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["wait_loss_projected"][0],
                    amortized_series["wait_loss_projected"][2],
                    ids=amortized_series["wait_loss_projected"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-q-overhead",
                    "Q · Shared rewrite cost",
                    "V1.2 决策",
                    "tokens",
                    amortized_series[
                        "shared_overhead_equivalent_tokens"
                    ][0],
                    amortized_series[
                        "shared_overhead_equivalent_tokens"
                    ][2],
                    ids=amortized_series[
                        "shared_overhead_equivalent_tokens"
                    ][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-crossing-margin",
                    "Crossing · W − Q margin",
                    "V1.2 决策",
                    "tokens",
                    amortized_series["crossing_margin"][0],
                    amortized_series["crossing_margin"][2],
                    ids=amortized_series["crossing_margin"][1],
                    empty_message=amortized_empty_message,
                    signed=True,
                ),
                chart(
                    "amortized-crossed",
                    "Crossing · Amortized flag",
                    "V1.2 决策",
                    "flag",
                    amortized_series["amortized_crossed"][0],
                    amortized_series["amortized_crossed"][2],
                    ids=amortized_series["amortized_crossed"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-cap-count",
                    "Capacity · Bucket cap exceeded",
                    "V1.2 决策",
                    "flag",
                    amortized_series["pending_count_over"][0],
                    amortized_series["pending_count_over"][2],
                    ids=amortized_series["pending_count_over"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-cap-tokens",
                    "Capacity · Token cap exceeded",
                    "V1.2 决策",
                    "flag",
                    amortized_series["pending_tokens_over"][0],
                    amortized_series["pending_tokens_over"][2],
                    ids=amortized_series["pending_tokens_over"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-immediate-counterfactual",
                    "Counterfactual · V1.1 immediate flag",
                    "V1.2 决策",
                    "flag",
                    amortized_series["immediate_crossed"][0],
                    amortized_series["immediate_crossed"][2],
                    ids=amortized_series["immediate_crossed"][1],
                    empty_message=amortized_empty_message,
                ),
                chart(
                    "amortized-immediate-net-counterfactual",
                    "Counterfactual · V1.1 immediate net",
                    "V1.2 决策",
                    "tokens",
                    amortized_series[
                        "immediate_net_saving_equivalent_tokens"
                    ][0],
                    amortized_series[
                        "immediate_net_saving_equivalent_tokens"
                    ][2],
                    ids=amortized_series[
                        "immediate_net_saving_equivalent_tokens"
                    ][1],
                    empty_message=amortized_empty_message,
                    signed=True,
                ),
            ],
        },
        {
            "key": "cache",
            "title": "Prompt 缓存命中",
            "description": (
                "provider-reported cache read / total prompt · "
                "累计值按 prompt token 加权"
            ),
            "color": "#8b5cf6",
            "default_display_mode": "relative",
            "display_modes": [
                {"key": "relative", "label": "命中率"},
                {"key": "absolute", "label": "命中 Token"},
            ],
            "charts": [
                chart(
                    "request-cache-hit",
                    "Request · 命中率",
                    "模型请求",
                    "percent",
                    cache_labels,
                    cache_hit_percent,
                    ids=cache_ids,
                    modes=cache_modes(
                        "Request · 命中率",
                        "Request · 命中 Token",
                        cache_hit_percent,
                        cache_read,
                    ),
                ),
                chart(
                    "request-cache-hit-cumulative",
                    "Request · 累计命中率",
                    "模型请求",
                    "percent",
                    cache_labels,
                    _cumulative_percentages(cache_read, cache_prompt),
                    ids=cache_ids,
                    modes=cache_modes(
                        "Request · 累计命中率",
                        "Request · 累计命中 Token",
                        _cumulative_percentages(cache_read, cache_prompt),
                        _cumulative(cache_read),
                    ),
                ),
                chart(
                    "turn-cache-hit",
                    "Turn · 命中率",
                    "Turn 轮次",
                    "percent",
                    cache_turn_labels,
                    cache_turn_hit_percent,
                    ids=cache_turn_ids,
                    modes=cache_modes(
                        "Turn · 命中率",
                        "Turn · 命中 Token",
                        cache_turn_hit_percent,
                        cache_turn_read,
                    ),
                ),
                chart(
                    "turn-cache-hit-cumulative",
                    "Turn · 累计命中率",
                    "Turn 轮次",
                    "percent",
                    cache_turn_labels,
                    _cumulative_percentages(cache_turn_read, cache_turn_prompt),
                    ids=cache_turn_ids,
                    modes=cache_modes(
                        "Turn · 累计命中率",
                        "Turn · 累计命中 Token",
                        _cumulative_percentages(cache_turn_read, cache_turn_prompt),
                        _cumulative(cache_turn_read),
                    ),
                ),
            ],
        },
        {
            "key": "time",
            "title": "模型请求耗时",
            "description": "main-loop provider API latency · auxiliary compression 不在此曲线",
            "color": "#3978d6",
            "charts": [
                chart("request-time", "Request · 耗时", "模型请求", "ms", request_labels, request_time, ids=request_ids),
                chart("request-time-cumulative", "Request · 累计耗时", "模型请求", "ms", request_labels, _cumulative(request_time), ids=request_ids),
                chart("turn-time", "Turn · 耗时", "Turn 轮次", "ms", request_turn_labels, turn_time, ids=request_turn_ids),
                chart("turn-time-cumulative", "Turn · 累计耗时", "Turn 轮次", "ms", request_turn_labels, _cumulative(turn_time), ids=request_turn_ids),
            ],
        },
        {
            "key": "spent",
            "title": "Token 花费",
            "description": "main-loop provider usage · compression auxiliary inference 见上方账本",
            "color": "#d97706",
            "charts": [
                chart("request-spent", "Request · 花费", "模型请求", "tokens", request_labels, request_spent, ids=request_ids),
                chart("request-spent-cumulative", "Request · 累计花费", "模型请求", "tokens", request_labels, _cumulative(request_spent), ids=request_ids),
                chart("turn-spent", "Turn · 花费", "Turn 轮次", "tokens", request_turn_labels, turn_spent, ids=request_turn_ids),
                chart("turn-spent-cumulative", "Turn · 累计花费", "Turn 轮次", "tokens", request_turn_labels, _cumulative(turn_spent), ids=request_turn_ids),
            ],
        },
    ]

    projection_timestamps = [
        _finite_nonnegative(event.get("created_at"))
        for event in events
        if _finite_nonnegative(event.get("created_at")) > 0
    ]
    request_timestamps = [
        _finite_nonnegative(event.get("started_at"))
        for event in requests
        if _finite_nonnegative(event.get("started_at")) > 0
    ]
    decision_timestamps = [
        _finite_nonnegative(decision.get("created_at"))
        for decision in all_decisions
        if _finite_nonnegative(decision.get("created_at")) > 0
    ]
    observation_timestamps = [
        _finite_nonnegative(observation.get("created_at"))
        for observation in request_observations
        if _finite_nonnegative(observation.get("created_at")) > 0
    ]
    conversation_id = str(timeline.get("conversation_id") or "")
    title = str(timeline.get("title") or "").strip() or "未命名会话"
    first_projection_at = (
        min(projection_timestamps)
        if projection_timestamps
        else _finite_nonnegative(timeline.get("first_projection_at"))
    )
    last_projection_at = (
        max(projection_timestamps)
        if projection_timestamps
        else _finite_nonnegative(timeline.get("last_projection_at"))
    )
    last_activity_at = max(
        _finite_nonnegative(
            timeline.get("last_activity_at") or timeline.get("last_active")
        ),
        last_projection_at,
        max(request_timestamps, default=0.0),
        max(decision_timestamps, default=0.0),
        max(observation_timestamps, default=0.0),
    )
    usage_aggregate = timeline.get("usage_aggregate")
    aggregate = usage_aggregate if isinstance(usage_aggregate, Mapping) else {}
    aggregate_api_calls = int(_finite_nonnegative(aggregate.get("api_call_count")))
    aggregate_prompt = (
        _finite_nonnegative(aggregate.get("input_tokens"))
        + _finite_nonnegative(aggregate.get("cache_read_tokens"))
        + _finite_nonnegative(aggregate.get("cache_write_tokens"))
    )
    aggregate_total = _finite_nonnegative(aggregate.get("total_tokens"))
    provider_tokens = aggregate_total or sum(request_spent)
    provider_prompt = aggregate_prompt or sum(cache_prompt)
    provider_cache_read = (
        _finite_nonnegative(aggregate.get("cache_read_tokens"))
        if aggregate
        else sum(cache_read)
    )
    provider_cache_write = (
        _finite_nonnegative(aggregate.get("cache_write_tokens"))
        if aggregate
        else sum(cache_write)
    )
    provider_uncached = (
        _finite_nonnegative(aggregate.get("input_tokens"))
        if aggregate
        else sum(cache_uncached)
    )
    raw_coverage = timeline.get("request_usage_coverage")
    usage_coverage = dict(raw_coverage) if isinstance(raw_coverage, Mapping) else {
        "event_count": len(requests),
        "aggregate_api_call_count": aggregate_api_calls or len(requests),
        "event_tokens": sum(request_spent),
        "aggregate_tokens": provider_tokens,
        "call_percent": 100.0 if requests else 0.0,
        "token_percent": 100.0 if requests else 0.0,
        "complete": bool(requests),
    }
    gross_saved = sum(project_saved)
    conversation_raw_tokens = sum(conversation_project_raw)
    conversation_rendered_tokens = sum(conversation_project_rendered)
    conversation_saved_tokens = sum(conversation_project_saved)
    compression_overhead = _compression_overhead(
        timeline,
        gross_saved=gross_saved,
        project_count=len(events),
    )
    normal_projection_decisions = [
        decision
        for decision in normal_decisions
        if str(decision.get("decision_kind") or "") == "flush"
    ]
    emergency_projection_decisions = [
        decision
        for decision in emergency_decisions
        if str(decision.get("decision_kind") or "") == "emergency"
    ]
    reason_counts = _decision_counts(economic_decisions, "decision_reason")
    sanitized_economic_decisions = [
        _sanitize_decision(decision, _DECISION_EXPORT_FIELDS)
        for decision in economic_decisions
    ]
    sanitized_amortized_decisions = [
        {
            **_sanitize_decision(decision, _AMORTIZED_EXPORT_FIELDS),
            "capacity_triggered": (
                str(decision.get("decision_mode") or "") == "capacity"
                or _decision_flag(decision, "pending_count_over")
                or _decision_flag(decision, "pending_tokens_over")
            ),
        }
        for decision in amortized_decisions
    ]
    amortized_wait_count = sum(
        str(decision.get("decision_kind") or "") == "wait"
        for decision in amortized_decisions
    )
    amortized_flush_count = sum(
        str(decision.get("decision_kind") or "") == "flush"
        for decision in amortized_decisions
    )
    amortized_emergency_count = sum(
        str(decision.get("decision_kind") or "") == "emergency"
        for decision in amortized_decisions
    )
    amortized_summary = {
        "decision_count": len(amortized_decisions),
        "wait_count": amortized_wait_count,
        "flush_count": amortized_flush_count,
        "emergency_count": amortized_emergency_count,
        "publication_count": amortized_flush_count + amortized_emergency_count,
        "mode_counts": amortized_mode_counts,
        "reason_counts": amortized_reason_counts,
        "capacity_trigger_count": sum(
            bool(decision["capacity_triggered"])
            for decision in sanitized_amortized_decisions
        ),
        "pending_count_over_count": sum(
            _decision_flag(decision, "pending_count_over")
            for decision in amortized_decisions
        ),
        "pending_tokens_over_count": sum(
            _decision_flag(decision, "pending_tokens_over")
            for decision in amortized_decisions
        ),
        "amortized_crossed_count": sum(
            _decision_flag(decision, "amortized_crossed")
            for decision in amortized_decisions
        ),
        "emergency_triggered_count": sum(
            _decision_flag(decision, "emergency_triggered")
            for decision in amortized_decisions
        ),
        "immediate_crossed_count": sum(
            _decision_flag(decision, "immediate_crossed")
            for decision in amortized_decisions
        ),
        "latest": (
            sanitized_amortized_decisions[-1]
            if sanitized_amortized_decisions
            else None
        ),
    }
    amortized_group = next(
        group for group in groups if group.get("key") == "amortized"
    )
    amortized_group["summary"] = amortized_summary
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "conversation_id": conversation_id,
        "session_id": str(timeline.get("session_id") or conversation_id),
        "title": title,
        "source": str(timeline.get("source") or "live"),
        "started_at": _finite_nonnegative(timeline.get("started_at")),
        "first_projection_at": first_projection_at,
        "last_projection_at": last_projection_at,
        "last_activity_at": last_activity_at,
        "object_context_used": bool(events or all_decisions),
        "has_projection_telemetry": bool(events),
        "has_decision_telemetry": bool(all_decisions),
        "project_count": len(events),
        "request_count": aggregate_api_calls or len(requests),
        "request_event_count": len(requests),
        "timed_request_count": sum(
            1
            for event in requests
            if isinstance(event.get("metrics"), Mapping)
            and "api_duration_ms" in event["metrics"]
        ),
        "request_turnless_count": request_turnless_count,
        "request_usage_coverage": usage_coverage,
        "turn_count": len(turn_ids),
        "cache_request_count": len(cache_events),
        "cache_turn_count": len(cache_turns),
        "cache_turnless_request_count": sum(
            1 for event in cache_events if not str(event.get("turn_id") or "")
        ),
        "legacy_project_count": legacy_count,
        "turnless_project_count": turnless_count,
        "conversation_metric_count": len(conversation_events),
        "conversation_metric_missing_count": max(
            0, len(events) - len(conversation_events)
        ),
        "economic_metrics_available": economic_metrics_available,
        "economic_decision_count": len(economic_decisions),
        "normal_decision_count": len(normal_decisions),
        "emergency_decision_count": len(emergency_decisions),
        "normal_projection_count": len(normal_projection_decisions),
        "emergency_projection_count": len(emergency_projection_decisions),
        "economic_reason_counts": reason_counts,
        "economic_decisions": sanitized_economic_decisions,
        "amortized_metrics_available": amortized_metrics_available,
        "amortized_decision_count": len(amortized_decisions),
        "amortized_wait_count": amortized_wait_count,
        "amortized_flush_count": amortized_flush_count,
        "amortized_emergency_count": amortized_emergency_count,
        "amortized_mode_counts": amortized_mode_counts,
        "amortized_reason_counts": amortized_reason_counts,
        "amortized_summary": amortized_summary,
        "amortized_decisions": sanitized_amortized_decisions,
        "request_observation_count": len(request_observations),
        "request_observations": request_observations,
        "download_point_count": sum(
            (
                sum(
                    len(mode_payload["values"])
                    for mode_payload in chart_payload["modes"].values()
                )
                if "modes" in chart_payload
                else len(chart_payload["values"])
            )
            for group in groups
            for chart_payload in group["charts"]
        ),
        "totals": {
            "tokens_saved": round(gross_saved, 6),
            "economic_normal_gross_tokens_removed": round(
                sum(
                    _finite_nonnegative(decision.get("gross_tokens_removed"))
                    for decision in normal_projection_decisions
                ),
                6,
            ),
            "economic_normal_cache_penalty_tokens": round(
                sum(
                    _finite_nonnegative(
                        decision.get("cache_penalty_equivalent_tokens")
                    )
                    for decision in normal_projection_decisions
                ),
                6,
            ),
            "economic_normal_summary_cost_tokens": round(
                sum(
                    _finite_nonnegative(
                        decision.get("known_summary_cost_equivalent_tokens")
                    )
                    for decision in normal_projection_decisions
                ),
                6,
            ),
            "economic_normal_net_saving_tokens": round(
                sum(
                    _finite_number(decision.get("net_saving_equivalent_tokens"))
                    for decision in normal_projection_decisions
                ),
                6,
            ),
            "economic_emergency_gross_tokens_removed": round(
                sum(
                    _finite_nonnegative(decision.get("gross_tokens_removed"))
                    for decision in emergency_projection_decisions
                ),
                6,
            ),
            "economic_emergency_net_effect_tokens": round(
                sum(
                    _finite_number(decision.get("net_saving_equivalent_tokens"))
                    for decision in emergency_projection_decisions
                ),
                6,
            ),
            "raw_conversation_tokens": round(conversation_raw_tokens, 6),
            "rendered_conversation_tokens": round(
                conversation_rendered_tokens, 6
            ),
            "conversation_tokens_saved": round(
                conversation_saved_tokens, 6
            ),
            "conversation_reduction_percent": round(
                _percentage(
                    conversation_saved_tokens, conversation_raw_tokens
                ),
                6,
            ),
            "compression_inference_tokens": compression_overhead[
                "exact_inference_tokens"
            ],
            "compression_schema_tokens_rough": compression_overhead[
                "rough_schema_tokens"
            ],
            "compression_overhead_tokens": compression_overhead[
                "known_overhead_tokens"
            ],
            "auxiliary_provider_tokens": compression_overhead[
                "all_auxiliary_tokens"
            ],
            "all_provider_tokens_known": round(
                provider_tokens + compression_overhead["all_auxiliary_tokens"],
                6,
            ),
            "net_tokens_saved_known": compression_overhead[
                "known_net_saved_tokens"
            ],
            "api_duration_ms": round(sum(request_time), 6),
            "provider_tokens": round(provider_tokens, 6),
            "prompt_tokens": round(provider_prompt, 6),
            "uncached_input_tokens": round(provider_uncached, 6),
            "cache_read_tokens": round(provider_cache_read, 6),
            "cache_write_tokens": round(provider_cache_write, 6),
            "cache_hit_percent": round(
                _percentage(provider_cache_read, provider_prompt), 6
            ),
        },
        "compression_overhead": compression_overhead,
        "groups": groups,
    }


def build_monitor_dashboard_payload(timeline: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one or many timelines into the all-session dashboard contract."""

    raw_sessions = timeline.get("sessions")
    sources = (
        [item for item in raw_sessions if isinstance(item, Mapping)]
        if isinstance(raw_sessions, list)
        else [timeline]
    )
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in sources:
        payload = build_monitor_payload(source)
        conversation_id = str(payload.get("conversation_id") or "")
        if not conversation_id or conversation_id in seen:
            continue
        seen.add(conversation_id)
        sessions.append(payload)
    sessions.sort(
        key=lambda item: (
            _finite_nonnegative(item.get("last_activity_at")),
            _finite_nonnegative(item.get("last_projection_at")),
            str(item.get("conversation_id") or ""),
        ),
        reverse=True,
    )

    active_conversation_id = str(
        timeline.get("active_conversation_id")
        or timeline.get("conversation_id")
        or ""
    )
    selected_conversation_id = active_conversation_id
    if selected_conversation_id not in seen:
        selected_conversation_id = (
            str(sessions[0].get("conversation_id") or "") if sessions else ""
        )
    for session in sessions:
        session["is_active"] = (
            bool(active_conversation_id)
            and session["conversation_id"] == active_conversation_id
        )

    def total(field: str) -> float:
        return round(
            sum(
                _finite_nonnegative(session.get("totals", {}).get(field))
                for session in sessions
            ),
            6,
        )

    def signed_total(field: str) -> float:
        return round(
            sum(
                _finite_number(session.get("totals", {}).get(field))
                for session in sessions
            ),
            6,
        )

    global_totals = {
        "project_count": sum(int(item["project_count"]) for item in sessions),
        "request_count": sum(int(item["request_count"]) for item in sessions),
        "request_event_count": sum(
            int(item["request_event_count"]) for item in sessions
        ),
        "request_observation_count": sum(
            int(item["request_observation_count"]) for item in sessions
        ),
        "turn_count": sum(int(item["turn_count"]) for item in sessions),
        "cache_request_count": sum(
            int(item["cache_request_count"]) for item in sessions
        ),
        "timed_request_count": sum(
            int(item["timed_request_count"]) for item in sessions
        ),
        "legacy_project_count": sum(int(item["legacy_project_count"]) for item in sessions),
        "economic_decision_count": sum(
            int(item["economic_decision_count"]) for item in sessions
        ),
        "amortized_decision_count": sum(
            int(item["amortized_decision_count"]) for item in sessions
        ),
        "amortized_wait_count": sum(
            int(item["amortized_wait_count"]) for item in sessions
        ),
        "amortized_flush_count": sum(
            int(item["amortized_flush_count"]) for item in sessions
        ),
        "amortized_emergency_count": sum(
            int(item["amortized_emergency_count"]) for item in sessions
        ),
        "amortized_publication_count": sum(
            int(item["amortized_summary"]["publication_count"])
            for item in sessions
        ),
        "amortized_crossed_count": sum(
            int(item["amortized_summary"]["amortized_crossed_count"])
            for item in sessions
        ),
        "capacity_trigger_count": sum(
            int(item["amortized_summary"]["capacity_trigger_count"])
            for item in sessions
        ),
        "pending_count_over_count": sum(
            int(item["amortized_summary"]["pending_count_over_count"])
            for item in sessions
        ),
        "pending_tokens_over_count": sum(
            int(item["amortized_summary"]["pending_tokens_over_count"])
            for item in sessions
        ),
        "amortized_emergency_triggered_count": sum(
            int(item["amortized_summary"]["emergency_triggered_count"])
            for item in sessions
        ),
        "amortized_immediate_crossed_count": sum(
            int(item["amortized_summary"]["immediate_crossed_count"])
            for item in sessions
        ),
        "normal_projection_count": sum(
            int(item["normal_projection_count"]) for item in sessions
        ),
        "emergency_projection_count": sum(
            int(item["emergency_projection_count"]) for item in sessions
        ),
        "conversation_metric_count": sum(
            int(item["conversation_metric_count"]) for item in sessions
        ),
        "conversation_metric_missing_count": sum(
            int(item["conversation_metric_missing_count"])
            for item in sessions
        ),
        "tokens_saved": total("tokens_saved"),
        "economic_normal_gross_tokens_removed": total(
            "economic_normal_gross_tokens_removed"
        ),
        "economic_normal_cache_penalty_tokens": total(
            "economic_normal_cache_penalty_tokens"
        ),
        "economic_normal_summary_cost_tokens": total(
            "economic_normal_summary_cost_tokens"
        ),
        "economic_normal_net_saving_tokens": signed_total(
            "economic_normal_net_saving_tokens"
        ),
        "economic_emergency_gross_tokens_removed": total(
            "economic_emergency_gross_tokens_removed"
        ),
        "economic_emergency_net_effect_tokens": signed_total(
            "economic_emergency_net_effect_tokens"
        ),
        "raw_conversation_tokens": total("raw_conversation_tokens"),
        "rendered_conversation_tokens": total(
            "rendered_conversation_tokens"
        ),
        "conversation_tokens_saved": total("conversation_tokens_saved"),
        "compression_inference_tokens": total("compression_inference_tokens"),
        "compression_schema_tokens_rough": total(
            "compression_schema_tokens_rough"
        ),
        "compression_overhead_tokens": total("compression_overhead_tokens"),
        "auxiliary_provider_tokens": total("auxiliary_provider_tokens"),
        "all_provider_tokens_known": total("all_provider_tokens_known"),
        "net_tokens_saved_known": signed_total("net_tokens_saved_known"),
        "api_duration_ms": total("api_duration_ms"),
        "provider_tokens": total("provider_tokens"),
        "prompt_tokens": total("prompt_tokens"),
        "uncached_input_tokens": total("uncached_input_tokens"),
        "cache_read_tokens": total("cache_read_tokens"),
        "cache_write_tokens": total("cache_write_tokens"),
    }
    global_totals["cache_hit_percent"] = round(
        _percentage(
            global_totals["cache_read_tokens"],
            global_totals["prompt_tokens"],
        ),
        6,
    )
    global_totals["conversation_reduction_percent"] = round(
        _percentage(
            global_totals["conversation_tokens_saved"],
            global_totals["raw_conversation_tokens"],
        ),
        6,
    )

    def merged_decision_counts(field: str) -> dict[str, int]:
        merged: dict[str, int] = {}
        for session in sessions:
            counts = session.get(field)
            if not isinstance(counts, Mapping):
                continue
            for label, value in counts.items():
                count = int(_finite_nonnegative(value))
                if count:
                    key = str(label)
                    merged[key] = merged.get(key, 0) + count
        return merged

    amortized_mode_counts = merged_decision_counts("amortized_mode_counts")
    amortized_reason_counts = merged_decision_counts("amortized_reason_counts")
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "active_session_id": str(timeline.get("session_id") or ""),
        "active_conversation_id": active_conversation_id,
        "selected_conversation_id": selected_conversation_id,
        "session_count": len(sessions),
        "global_totals": global_totals,
        "amortized_mode_counts": amortized_mode_counts,
        "amortized_reason_counts": amortized_reason_counts,
        "sessions": sessions,
    }


def _safe_script_json(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_monitor_html(timeline: Mapping[str, Any]) -> str:
    """Render a private, standalone, no-network experiment dashboard."""

    payload = build_monitor_dashboard_payload(timeline)
    data_json = _safe_script_json(payload)
    conversation = escape(payload["selected_conversation_id"] or "active conversation", quote=True)
    session = escape(payload["active_session_id"] or "active session", quote=True)
    template = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="object-context-conversation" content="__CONVERSATION__">
  <meta name="object-context-session" content="__SESSION__">
  <title>Object Context V1 · All Sessions</title>
  <style>
    :root { --ink:#202124; --muted:#70747a; --paper:#fff; --canvas:#f5f6f7; --border:#e1e3e6; --sidebar:#191b1f; --sidebar2:#292c31; --accent:#f4bd2d; --blue:#3978d6; }
    * { box-sizing:border-box; } html { scroll-behavior:smooth; }
    body { margin:0; color:var(--ink); background:var(--canvas); font:14px/1.45 Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
    button,input { font:inherit; } button { cursor:pointer; }
    .shell { min-height:100vh; display:grid; grid-template-columns:248px minmax(0,1fr); }
    aside { position:sticky; top:0; height:100vh; overflow:auto; background:linear-gradient(180deg,var(--sidebar),#15171a); color:#f6f6f3; padding:20px 14px; }
    .brand { display:flex; align-items:center; gap:10px; padding:0 7px 17px; border-bottom:1px solid #34373c; }
    .mark { width:34px; height:34px; border-radius:9px; display:grid; place-items:center; background:var(--accent); color:#232323; font-weight:900; letter-spacing:-.06em; }
    .brand strong { display:block; font-size:15px; } .brand span { display:block; color:#aeb1b6; font-size:11px; margin-top:1px; }
    .nav-label { margin:17px 8px 8px; color:#8f9399; font-size:10px; font-weight:750; text-transform:uppercase; letter-spacing:.12em; }
    .side-search,.run-search { width:100%; border:1px solid #41454a; background:#2b2e32; color:#fff; border-radius:7px; padding:9px 10px; outline:none; }
    .side-search:focus,.run-search:focus { border-color:var(--accent); box-shadow:0 0 0 2px rgba(244,189,45,.15); }
    .side-runs { margin-top:9px; }
    .side-group + .side-group { margin-top:16px; }
    .side-group-head { display:flex; align-items:center; justify-content:space-between; gap:8px; margin:0 8px 6px; color:#8f9399; font-size:9px; font-weight:760; text-transform:uppercase; letter-spacing:.09em; }
    .side-group-count { color:#6f7379; font-variant-numeric:tabular-nums; }
    .side-group-list { display:grid; gap:4px; }
    .side-run { width:100%; text-align:left; border:0; border-radius:8px; padding:9px 10px; color:#d9dbde; background:transparent; }
    .side-run:hover { background:var(--sidebar2); } .side-run.selected { background:#35383d; color:#fff; box-shadow:inset 3px 0 var(--accent); }
    .side-run-title-row { display:flex; align-items:center; gap:7px; min-width:0; }
    .side-run-title { display:block; min-width:0; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; font-weight:720; }
    .side-run-id { display:block; margin-top:3px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:#92969c; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:10px; }
    .side-run-meta { display:block; color:#979ba1; font-size:10px; margin-top:3px; }
    .context-tag { display:inline-flex; flex:none; align-items:center; padding:2px 6px; border-radius:999px; font:750 9px/1.4 ui-sans-serif,sans-serif; white-space:nowrap; vertical-align:2px; }
    .context-tag.oc-on { background:#e7f7f1; color:#087153; }
    .context-tag.oc-off { background:#eceef1; color:#5f6368; }
    .side-run .context-tag.oc-on { background:rgba(24,167,123,.18); color:#7ddfbd; }
    .side-run .context-tag.oc-off { background:#3a3d42; color:#b9bdc3; }
    main { min-width:0; padding:24px clamp(18px,2.7vw,38px) 60px; }
    .topbar { display:flex; justify-content:space-between; gap:20px; align-items:center; }
    .eyebrow { color:#876100; text-transform:uppercase; letter-spacing:.13em; font-weight:800; font-size:10px; }
    h1 { margin:2px 0 0; font-size:clamp(25px,3vw,34px); letter-spacing:-.035em; line-height:1.08; }
    .snapshot { text-align:right; color:var(--muted); font-size:11px; } .snapshot strong { color:var(--ink); display:block; font-size:12px; }
    .kpis { display:grid; grid-template-columns:repeat(6,minmax(115px,1fr)); gap:9px; margin:18px 0 24px; }
    .kpi { background:var(--paper); border:1px solid var(--border); border-radius:10px; padding:12px 14px; box-shadow:0 1px 2px rgba(0,0,0,.025); }
    .kpi-label { color:var(--muted); font-size:10px; font-weight:750; text-transform:uppercase; letter-spacing:.06em; }
    .kpi-value { margin-top:4px; font-size:21px; line-height:1.15; font-weight:780; font-variant-numeric:tabular-nums; }
    .panel,.chart { background:var(--paper); border:1px solid var(--border); border-radius:11px; box-shadow:0 1px 3px rgba(0,0,0,.03); }
    .panel-head { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:12px 15px; border-bottom:1px solid var(--border); }
    .panel-head h2 { margin:0; font-size:15px; }
    .run-search { width:min(290px,45vw); color:var(--ink); background:#fafafa; border-color:var(--border); }
    .table-wrap { overflow:auto; } table { border-collapse:collapse; width:100%; min-width:1100px; }
    th,td { padding:11px 15px; text-align:right; border-bottom:1px solid #ecebe7; font-variant-numeric:tabular-nums; white-space:nowrap; }
    th { color:var(--muted); font-size:10px; text-transform:uppercase; letter-spacing:.065em; background:#fafaf8; } th:first-child,td:first-child { text-align:left; }
    th:nth-child(2),td:nth-child(2) { text-align:center; }
    tbody tr { cursor:pointer; } tbody tr:hover { background:#fffaf0; } tbody tr.selected { background:#fff8df; box-shadow:inset 3px 0 var(--accent); } tbody tr:last-child td { border-bottom:0; }
    .run-title { display:block; font-weight:720; color:var(--ink); }
    .run-id { display:block; margin-top:2px; color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:10px; }
    .badge { display:inline-flex; margin-left:7px; padding:2px 6px; border-radius:999px; background:#e7f7f1; color:#087153; font:700 9px/1.4 ui-sans-serif,sans-serif; text-transform:uppercase; vertical-align:1px; }
    .coverage { display:inline-block; width:64px; height:6px; background:#e7e6e1; border-radius:999px; overflow:hidden; vertical-align:middle; margin-right:6px; } .coverage i { display:block; height:100%; background:var(--blue); }
    .workspace { margin-top:24px; } .session-head { display:flex; justify-content:space-between; gap:18px; align-items:center; padding-bottom:12px; border-bottom:1px solid var(--border); }
    .session-head h2 { margin:0 0 3px; font-size:21px; } .session-head h2 .context-tag { margin-left:7px; } .session-id { color:var(--muted); font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:11px; overflow-wrap:anywhere; }
    .session-actions { display:flex; flex-direction:column; align-items:flex-end; gap:8px; }
    .session-time { color:var(--muted); text-align:right; font-size:11px; } .session-kpis { margin-top:12px; margin-bottom:4px; }
    .overhead-ledger { margin-top:18px; overflow:hidden; }
    .overhead-summary { display:flex; flex-wrap:wrap; gap:8px; padding:12px 15px; background:#fffaf0; border-bottom:1px solid var(--border); }
    .overhead-formula { width:100%; color:#5f4a13; font-size:11px; }
    .overhead-chip { padding:4px 8px; border:1px solid #e2d2a5; border-radius:999px; background:#fff; color:#5f4a13; font-size:10px; font-variant-numeric:tabular-nums; }
    .overhead-chip.warn { color:#9a3412; border-color:#fdba74; background:#fff7ed; }
    .overhead-table { min-width:760px; }
    .overhead-table tbody tr { cursor:default; }
    .overhead-table th:nth-child(2),.overhead-table td:nth-child(2) { text-align:right; }
    .overhead-table th:nth-child(4),.overhead-table td:nth-child(4),.overhead-table th:nth-child(5),.overhead-table td:nth-child(5) { text-align:left; white-space:normal; }
    .measure-exact { color:#087153; font-weight:720; } .measure-rough { color:#9a6700; font-weight:720; }
    .ledger-note { padding:10px 15px 13px; color:var(--muted); font-size:10px; border-top:1px solid #ecebe7; }
    .metric-group { margin-top:26px; } .group-heading { display:flex; justify-content:space-between; align-items:center; gap:16px; }
    .group-title { display:flex; align-items:center; gap:8px; margin:0; font-size:17px; }
    .group-dot { width:8px; height:8px; border-radius:3px; background:currentColor; } .group-description { color:var(--muted); margin:3px 0 10px; font-size:11px; }
    .metric-toggle { display:inline-flex; flex:none; padding:3px; border:1px solid #d2d1cb; border-radius:8px; background:#ecebe6; box-shadow:inset 0 1px 2px rgba(0,0,0,.04); }
    .metric-mode { border:0; border-radius:5px; padding:6px 12px; color:#686b70; background:transparent; font-size:11px; font-weight:750; }
    .metric-mode:hover { color:#202124; } .metric-mode[aria-pressed="true"] { color:#202124; background:#fff; box-shadow:0 1px 3px rgba(0,0,0,.14); }
    .metric-mode:focus-visible { outline:2px solid #3978d6; outline-offset:2px; }
    .charts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }
    .chart { padding:13px 13px 8px; min-width:0; } .chart-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
    .chart-title { font-weight:720; } .chart-tools { display:flex; align-items:center; gap:8px; }
    .chart-summary { color:var(--muted); font-size:11px; font-variant-numeric:tabular-nums; white-space:nowrap; }
    .download { border:1px solid #c9c8c2; color:#45474b; background:#fbfbf9; border-radius:6px; padding:4px 7px; font-size:10px; font-weight:750; white-space:nowrap; text-decoration:none; }
    .download:hover { border-color:#888; background:#f2f1ed; }
    .download-all { color:#222; background:var(--accent); border-color:#c79517; padding:6px 10px; font-size:10px; }
    .download-all:hover { background:#e9ac13; border-color:#a97909; }
    svg { display:block; width:100%; height:auto; overflow:visible; margin-top:7px; } .grid { stroke:#e8e7e2; stroke-width:1; } .axis-text { fill:#777a7f; font-size:11px; }
    .series { fill:none; stroke-width:2.4; stroke-linecap:round; stroke-linejoin:round; } .point { stroke:#fff; stroke-width:1.5; cursor:crosshair; }
    .empty { height:210px; display:grid; place-items:center; color:var(--muted); border:1px dashed var(--border); border-radius:7px; margin-top:10px; background:#fafafa; }
    .coverage-note { display:flex; flex-wrap:wrap; align-items:center; gap:6px; margin-top:14px; color:var(--muted); font-size:10px; }
    .coverage-note strong { color:#55595f; text-transform:uppercase; letter-spacing:.07em; font-size:9px; }
    .coverage-chip { padding:3px 7px; border:1px solid var(--border); border-radius:999px; background:#fff; font-variant-numeric:tabular-nums; }
    footer { margin-top:30px; color:var(--muted); border-top:1px solid var(--border); padding-top:13px; font-size:10px; } code { color:#6d4e00; background:#eeeae0; border-radius:4px; padding:1px 4px; }
    @media(max-width:1100px) { .kpis { grid-template-columns:repeat(3,1fr); } .charts { grid-template-columns:1fr; } }
    @media(max-width:760px) { .shell { display:block; } aside { position:relative; height:auto; padding:14px; } .brand { padding-bottom:0; border-bottom:0; } .nav-label,.side-search,.side-runs { display:none; } main { padding:18px 11px 44px; } .topbar,.session-head { display:block; } .snapshot,.session-time { text-align:left; margin-top:8px; } .session-actions { align-items:flex-start; margin-top:10px; } .kpis { grid-template-columns:repeat(2,1fr); } .panel-head { align-items:flex-start; flex-direction:column; } .run-search { width:100%; } .group-heading { align-items:flex-start; flex-direction:column; gap:8px; } }
  </style>
</head>
<body>
<div class="shell">
  <aside>
    <div class="brand"><div class="mark">OC</div><div><strong>Object Context V1</strong><span>Session Dynamics Monitor</span></div></div>
    <div class="nav-label">Sessions / Runs</div>
    <input id="side-search" class="side-search" type="search" placeholder="搜索 session" autocomplete="off">
    <div id="side-runs" class="side-runs"></div>
  </aside>
  <main>
    <div class="topbar">
      <div><div class="eyebrow">Object Context V1</div><h1>Session Dynamics</h1></div>
      <div class="snapshot"><strong id="snapshot-count"></strong><span id="generated"></span></div>
    </div>
    <div id="global-kpis" class="kpis"></div>
    <section class="panel" aria-label="All sessions">
      <div class="panel-head"><h2>Runs</h2><input id="run-search" class="run-search" type="search" placeholder="搜索标题或 ID" autocomplete="off"></div>
      <div class="table-wrap"><table><thead><tr><th>Run</th><th>Context</th><th>Requests</th><th>Turns</th><th>Conversation-only</th><th>Gross Saved</th><th>Overhead</th><th>Known Net</th><th>Cache Hit</th><th>API Time</th><th>Known Provider</th><th>Series</th><th>Updated</th></tr></thead><tbody id="run-body"></tbody></table></div>
    </section>
    <div id="workspace" class="workspace"></div>
    <footer>不含 prompt / 消息内容 · 实时页面刷新浏览器即可重读数据 · <code>file://</code> 快照需重新运行 <code>/oc monitor</code></footer>
  </main>
</div>
<script>
const DATA=__DATA__;
const NS="http://www.w3.org/2000/svg";
const byId=id=>document.getElementById(id);
const TOKEN_UNITS=[{threshold:1e9,label:"B"},{threshold:1e6,label:"M"},{threshold:1e3,label:"K"}];
const fmtToken=value=>{const n=Math.max(0,Number(value||0));const unit=TOKEN_UNITS.find(item=>n>=item.threshold);if(unit){const scaled=n/unit.threshold;return `${new Intl.NumberFormat("en-US",{maximumSignificantDigits:3}).format(scaled)}${unit.label} tok`;}return new Intl.NumberFormat("en-US",{maximumFractionDigits:n<10?2:n<100?1:0}).format(n)+" tok";};
const fmtSignedToken=value=>{const n=Number(value||0);return n<0?"−"+fmtToken(Math.abs(n)):fmtToken(n);};
const fmt=(value,unit)=>{const n=Number(value||0);if(unit==="flag")return n<=0?"0":n>=1?"1":n.toFixed(2);if(unit==="count")return new Intl.NumberFormat("en-US",{maximumFractionDigits:0}).format(n);if(unit==="percent")return new Intl.NumberFormat("zh-CN",{minimumFractionDigits:n>0&&n<1?2:0,maximumFractionDigits:2}).format(n)+"%";if(unit==="ms"){if(n>=36e5)return `${(n/36e5).toFixed(2)} h`;if(n>=6e4)return `${(n/6e4).toFixed(2)} min`;return n>=1000?`${(n/1000).toFixed(2)} s`:`${n.toFixed(n<10?2:1)} ms`;}return fmtToken(n);};
const fmtChart=(value,chart)=>chart.signed&&chart.unit==="tokens"?fmtSignedToken(value):fmt(value,chart.unit);
const fmtCache=(percent,count)=>Number(count||0)>0?fmt(percent,"percent"):"—";
const fmtConversation=(totals,measured,missing)=>{const count=Number(measured||0);const gap=Number(missing||0);if(count<=0)return gap>0?"N/A · legacy":"0 tok";const suffix=gap>0?" · partial":"";return `${fmtToken(totals.conversation_tokens_saved)} · ${fmt(totals.conversation_reduction_percent,"percent")}${suffix}`;};
const schedulerStatus=session=>session.amortized_metrics_available?`${session.amortized_decision_count} V1.2 scheduler decisions · 尚未发布 Card`:`${session.economic_decision_count} V1.1 economic decisions · 尚未发布 Card`;
const SHORT_DATE=new Intl.DateTimeFormat("zh-CN",{month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"});
const dateFmt=value=>value?SHORT_DATE.format(new Date(Number(value)*1000)):"—";
const shortId=value=>{const s=String(value||"unknown");return s.length>28?s.slice(0,17)+"…"+s.slice(-8):s;};
const node=(name,attrs={})=>{const el=document.createElementNS(NS,name);for(const [key,value] of Object.entries(attrs))el.setAttribute(key,String(value));return el;};
function kpi(label,value){const box=document.createElement("div");box.className="kpi";const l=document.createElement("div");l.className="kpi-label";l.textContent=label;const v=document.createElement("div");v.className="kpi-value";v.textContent=value;box.append(l,v);return box;}
function contextTag(session){const used=Boolean(session.object_context_used);const tag=document.createElement("span");tag.className=`context-tag ${used?"oc-on":"oc-off"}`;tag.textContent=used?"OC":"No OC";tag.setAttribute("aria-label",used?"使用了 Object Context":"未使用 Object Context");return tag;}
function sample(chart,max=180){const n=chart.values.length;if(n<=max)return chart.values.map((value,index)=>({value,index}));const out=[];for(let slot=0;slot<max;slot++){const index=Math.round(slot*(n-1)/(max-1));out.push({value:chart.values[index],index});}return out;}
function csvCell(value){let text=String(value??"");if(/^[=+\-@]/.test(text))text="'"+text;return `"${text.replace(/"/g,'""')}"`;}
const CSV_HEADERS=["session_title","session_id","conversation_id","group_key","chart_key","chart_title","display_mode","point_index","label","identity","value","unit"];
function chartRows(session,group,chart){return chart.values.map((value,index)=>[session.title,session.session_id,session.conversation_id,group.key,chart.key,chart.title,chart.display_mode||"default",index+1,chart.labels[index]||"",chart.ids[index]||"",value,chart.unit]);}
function overheadRows(session){return session.compression_overhead.components.map((component,index)=>[session.title,session.session_id,session.conversation_id,"compression_overhead",component.key,component.label,component.measurement,index+1,component.treatment,component.deducted?"deducted":"informational",component.tokens,"tokens"]);}
function resolveChartMode(chart,modeKey){const mode=chart.modes&&chart.modes[modeKey];return mode?{...chart,...mode,display_mode:modeKey}:{...chart,display_mode:"default"};}
function safeFilename(value,fallback){const cleaned=String(value||"").trim().replace(/[\\/?%*:|"<>]/g,"_").replace(/\s+/g,"_").slice(0,64);return cleaned||fallback;}
function csvDownload(session,suffix,rows){
  const csv="\ufeff"+[CSV_HEADERS,...rows].map(row=>row.map(csvCell).join(",")).join("\r\n")+"\r\n";
  const title=safeFilename(session.title,"untitled");const identity=safeFilename(session.conversation_id,"session").slice(0,48);
  return {href:"data:text/csv;charset=utf-8,"+encodeURIComponent(csv),filename:`object_context_${title}_${identity}_${suffix}.csv`,rowCount:rows.length};
}
function chartDownload(session,group,chart){
  const rows=chartRows(session,group,chart);
  const suffix=chart.display_mode==="relative"?`${chart.key}_relative`:chart.key;
  return csvDownload(session,suffix,rows);
}
function sessionDownload(session){const rows=overheadRows(session);for(const group of session.groups)for(const chart of group.charts){if(chart.modes)for(const modeKey of Object.keys(chart.modes))rows.push(...chartRows(session,group,resolveChartMode(chart,modeKey)));else rows.push(...chartRows(session,group,resolveChartMode(chart,"default")));}return csvDownload(session,"all_metrics",rows);}
function renderChart(session,group,chart){
  const card=document.createElement("article");card.className="chart";const head=document.createElement("div");head.className="chart-head";const title=document.createElement("div");title.className="chart-title";title.textContent=chart.title;const tools=document.createElement("div");tools.className="chart-tools";
  const summary=document.createElement("div");summary.className="chart-summary";const latest=chart.values.length?chart.values[chart.values.length-1]:0;summary.textContent=`${fmtChart(latest,chart)} · ${chart.values.length} pts`;
  const downloadData=chartDownload(session,group,chart);const download=document.createElement("a");download.className="download";download.textContent="CSV ↓";download.title=`下载「${chart.title}」完整数据`;download.setAttribute("aria-label",download.title);download.href=downloadData.href;download.download=downloadData.filename;tools.append(summary,download);head.append(title,tools);card.append(head);
  if(!chart.values.length){const empty=document.createElement("div");empty.className="empty";empty.textContent=chart.empty_message||"暂无数据";card.append(empty);return card;}
  const W=760,H=275,M={l:72,r:18,t:16,b:43},PW=W-M.l-M.r,PH=H-M.t-M.b;const values=chart.values.map(Number);const minValue=chart.signed?Math.min(...values,0):0;const maxValue=Math.max(...values,0);const yMin=chart.unit==="percent"||chart.unit==="flag"?0:minValue;let yMax=chart.unit==="percent"?100:chart.unit==="flag"?1:(maxValue>0?maxValue:1);if(yMax===yMin)yMax=yMin+1;const sx=index=>M.l+(values.length===1?PW/2:index*PW/(values.length-1));const sy=value=>M.t+((yMax-Number(value))/(yMax-yMin))*PH;const svg=node("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":chart.title});
  for(let tick=0;tick<=4;tick++){const y=M.t+tick*PH/4;svg.append(node("line",{x1:M.l,y1:y,x2:W-M.r,y2:y,class:"grid"}));const label=node("text",{x:M.l-9,y:y+4,"text-anchor":"end",class:"axis-text"});label.textContent=fmtChart(yMax-(yMax-yMin)*tick/4,chart);svg.append(label);}
  const points=values.map((value,index)=>`${sx(index)},${sy(value)}`);svg.append(node("polyline",{points:points.join(" "),class:"series",stroke:group.color}));for(const point of sample(chart)){const circle=node("circle",{cx:sx(point.index),cy:sy(point.value),r:3.5,class:"point",fill:group.color});const tip=node("title");const identity=chart.ids[point.index]||chart.labels[point.index]||String(point.index+1);tip.textContent=`${chart.labels[point.index]||point.index+1} · ${identity}\n${fmtChart(point.value,chart)}`;circle.append(tip);svg.append(circle);}
  const first=node("text",{x:M.l,y:H-24,"text-anchor":"start",class:"axis-text"});first.textContent=chart.labels[0]||"1";svg.append(first);const last=node("text",{x:W-M.r,y:H-24,"text-anchor":"end",class:"axis-text"});last.textContent=chart.labels[chart.labels.length-1]||String(values.length);svg.append(last);const axis=node("text",{x:M.l+PW/2,y:H-6,"text-anchor":"middle",class:"axis-text"});axis.textContent=chart.axis;svg.append(axis);card.append(svg);return card;
}
function renderOverheadLedger(session){
  const data=session.compression_overhead;const panel=document.createElement("section");panel.className="panel overhead-ledger";panel.setAttribute("aria-label","Compression overhead ledger");const head=document.createElement("div");head.className="panel-head";const title=document.createElement("h2");title.textContent="Compression Overhead Ledger";const status=document.createElement("span");status.className=`overhead-chip${data.coverage_complete?"":" warn"}`;status.textContent=data.coverage_complete?"usage coverage complete":"partial / historical coverage";head.append(title,status);
  const summary=document.createElement("div");summary.className="overhead-summary";const formula=document.createElement("div");formula.className="overhead-formula";formula.textContent="Known Net = Gross Saved − exact compression inference − rough retrieve_object schema. Card text 与 retrieved payload 已包含在 rendered context，不能二次扣除。";const chip=(label,value,signed=false)=>{const item=document.createElement("span");item.className="overhead-chip";item.textContent=`${label}: ${signed?fmtSignedToken(value):fmtToken(value)}`;return item;};summary.append(formula,chip("Gross",data.gross_saved_tokens),chip("Exact inference",data.exact_inference_tokens),chip("Rough schema",data.rough_schema_tokens),chip("Known overhead",data.known_overhead_tokens),chip("Known net",data.known_net_saved_tokens,true));
  const wrap=document.createElement("div");wrap.className="table-wrap";const table=document.createElement("table");table.className="overhead-table";const thead=document.createElement("thead");const header=document.createElement("tr");for(const label of ["Component","Tokens","Calls","Evidence","Treatment"]){const th=document.createElement("th");th.textContent=label;header.append(th);}thead.append(header);const tbody=document.createElement("tbody");for(const component of data.components){const row=document.createElement("tr");const values=[component.label,fmtToken(component.tokens),String(component.calls),component.measurement,component.treatment];values.forEach((value,index)=>{const cell=document.createElement("td");cell.textContent=value;if(index===3)cell.className=component.measurement.includes("exact")?"measure-exact":"measure-rough";row.append(cell);});tbody.append(row);}table.append(thead,tbody);wrap.append(table);
  const note=document.createElement("div");note.className="ledger-note";note.textContent=`Coverage: ${data.coverage_label} · Card attempts ${data.card_summary_attempts}, recorded provider responses ${data.recorded_card_summary_calls}, fallbacks ${data.summary_fallbacks}, unmetered attempts ${data.unmetered_card_summary_attempts}. Other auxiliary inference (background review / vision / title / etc.) ${fmtToken(data.other_auxiliary_tokens)}，不计入 compression overhead；Known Provider 总量会包含它。Limitations: ${data.limitations.join(" · ")}.`;
  panel.append(head,summary,wrap,note);return panel;
}
let selectedId=DATA.selected_conversation_id;const groupDisplayModes={saved:"absolute",cache:"relative"};const selectedSession=()=>DATA.sessions.find(session=>session.conversation_id===selectedId)||DATA.sessions[0];
const activeChart=(group,chart)=>resolveChartMode(chart,groupDisplayModes[group.key]||group.default_display_mode||"default");
function renderGlobal(){const totals=DATA.global_totals;const host=byId("global-kpis");host.replaceChildren(kpi("Sessions",String(DATA.session_count)),kpi("Requests",new Intl.NumberFormat("en-US").format(totals.request_count)),kpi("Conversation-only Saved",fmtConversation(totals,totals.conversation_metric_count,totals.conversation_metric_missing_count)),kpi("Gross Saved",fmt(totals.tokens_saved,"tokens")),kpi("Compression Overhead",fmt(totals.compression_overhead_tokens,"tokens")),kpi("Known Net",fmtSignedToken(totals.net_tokens_saved_known)),kpi("Cache Hit",fmtCache(totals.cache_hit_percent,totals.request_count)),kpi("API Time",fmt(totals.api_duration_ms,"ms")),kpi("Known Provider",fmt(totals.all_provider_tokens_known,"tokens")));byId("snapshot-count").textContent=`${DATA.session_count} sessions · ${totals.request_count} main-loop requests · ${totals.project_count} OC projections`;byId("generated").textContent=`Updated ${SHORT_DATE.format(new Date(DATA.generated_at))}`;}
function runMatches(session,query){return !query||String(session.title||"").toLowerCase().includes(query)||session.conversation_id.toLowerCase().includes(query)||String(session.session_id||"").toLowerCase().includes(query);}
function renderRuns(query=""){
  const normalized=query.trim().toLowerCase();const sessions=DATA.sessions.filter(session=>runMatches(session,normalized));const side=byId("side-runs");const body=byId("run-body");side.replaceChildren();body.replaceChildren();
  const sideGroups=[
    {label:"Object Context",sessions:sessions.filter(session=>session.object_context_used)},
    {label:"No Object Context",sessions:sessions.filter(session=>!session.object_context_used)},
  ];
  for(const group of sideGroups){if(!group.sessions.length)continue;const section=document.createElement("section");section.className="side-group";const heading=document.createElement("div");heading.className="side-group-head";const label=document.createElement("span");label.textContent=group.label;const count=document.createElement("span");count.className="side-group-count";count.textContent=String(group.sessions.length);heading.append(label,count);const list=document.createElement("div");list.className="side-group-list";for(const session of group.sessions){const button=document.createElement("button");button.className="side-run"+(session.conversation_id===selectedId?" selected":"");button.type="button";const titleRow=document.createElement("span");titleRow.className="side-run-title-row";const title=document.createElement("span");title.className="side-run-title";title.textContent=session.title;titleRow.append(title,contextTag(session));const id=document.createElement("span");id.className="side-run-id";id.textContent=session.conversation_id;const meta=document.createElement("span");meta.className="side-run-meta";meta.textContent=`${session.request_count} R · ${fmt(session.totals.provider_tokens,"tokens")}`;button.append(titleRow,id,meta);button.addEventListener("click",()=>selectSession(session.conversation_id));list.append(button);}section.append(heading,list);side.append(section);}
  for(const session of sessions){const row=document.createElement("tr");if(session.conversation_id===selectedId)row.className="selected";const identity=document.createElement("td");const runTitle=document.createElement("span");runTitle.className="run-title";runTitle.textContent=session.title;if(session.is_active){const badge=document.createElement("span");badge.className="badge";badge.textContent="current";runTitle.append(badge);}const runId=document.createElement("span");runId.className="run-id";runId.textContent=shortId(session.conversation_id);runId.title=session.conversation_id;identity.append(runTitle,runId);const contextCell=document.createElement("td");contextCell.append(contextTag(session));const coverage=Math.max(0,Math.min(1,Number(session.request_usage_coverage.call_percent||0)/100));const cells=[identity,contextCell,session.request_count,session.turn_count,fmtConversation(session.totals,session.conversation_metric_count,session.conversation_metric_missing_count),fmt(session.totals.tokens_saved,"tokens"),fmt(session.totals.compression_overhead_tokens,"tokens"),fmtSignedToken(session.totals.net_tokens_saved_known),fmtCache(session.totals.cache_hit_percent,session.request_count),fmt(session.totals.api_duration_ms,"ms"),fmt(session.totals.all_provider_tokens_known,"tokens")];for(const value of cells){if(value instanceof Node)row.append(value);else{const cell=document.createElement("td");cell.textContent=String(value);row.append(cell);}}const coverageCell=document.createElement("td");const bar=document.createElement("span");bar.className="coverage";const fill=document.createElement("i");fill.style.width=`${Math.round(coverage*100)}%`;bar.append(fill);coverageCell.append(bar,`${Math.round(coverage*100)}%`);coverageCell.title="已恢复逐请求曲线 / 聚合请求总数";row.append(coverageCell);const last=document.createElement("td");last.textContent=dateFmt(session.last_activity_at);row.append(last);row.addEventListener("click",()=>selectSession(session.conversation_id));body.append(row);}
  if(!sessions.length){const row=document.createElement("tr");const cell=document.createElement("td");cell.colSpan=13;cell.textContent="没有匹配的 session";cell.style.textAlign="center";cell.style.color="var(--muted)";row.append(cell);body.append(row);}
}
function renderWorkspace(){
  const session=selectedSession();const host=byId("workspace");host.replaceChildren();if(!session)return;const head=document.createElement("div");head.className="session-head";const left=document.createElement("div");const title=document.createElement("h2");title.textContent=session.title;title.append(contextTag(session));if(session.is_active){const badge=document.createElement("span");badge.className="badge";badge.textContent="current";title.append(badge);}const identity=document.createElement("div");identity.className="session-id";identity.textContent=session.conversation_id;left.append(title,identity);const actions=document.createElement("div");actions.className="session-actions";const allData=sessionDownload(session);const downloadAll=document.createElement("a");downloadAll.className="download download-all";downloadAll.textContent=`全部 CSV · ${allData.rowCount}`;downloadAll.title=`下载「${session.title}」全部图表的 CSV 数据（含可切换模式）`;downloadAll.setAttribute("aria-label",downloadAll.title);downloadAll.href=allData.href;downloadAll.download=allData.filename;const time=document.createElement("div");time.className="session-time";time.textContent=session.has_projection_telemetry?`${session.project_count} OC projections · ${dateFmt(session.first_projection_at)} → ${dateFmt(session.last_projection_at)}`:session.has_decision_telemetry?schedulerStatus(session):"OC 未启用 · 节省量为 0";actions.append(downloadAll,time);head.append(left,actions);host.append(head);
  const metrics=document.createElement("div");metrics.className="kpis session-kpis";metrics.append(kpi("Requests",String(session.request_count)),kpi("Turns",String(session.turn_count)),kpi("Conversation-only Saved",fmtConversation(session.totals,session.conversation_metric_count,session.conversation_metric_missing_count)),kpi("Gross Saved",fmt(session.totals.tokens_saved,"tokens")),kpi("Compression Overhead",fmt(session.totals.compression_overhead_tokens,"tokens")),kpi("Known Net",fmtSignedToken(session.totals.net_tokens_saved_known)),kpi("Known Provider",fmt(session.totals.all_provider_tokens_known,"tokens")));host.append(metrics,renderOverheadLedger(session));
  for(const group of session.groups){const section=document.createElement("section");section.className="metric-group";const heading=document.createElement("div");heading.className="group-heading";const title=document.createElement("h3");title.className="group-title";title.style.color=group.color;const dot=document.createElement("span");dot.className="group-dot";const label=document.createElement("span");label.textContent=group.title;title.append(dot,label);heading.append(title);if(Array.isArray(group.display_modes)){const toggle=document.createElement("div");toggle.className="metric-toggle";toggle.setAttribute("role","group");toggle.setAttribute("aria-label",`${group.title} 显示方式`);const selectedMode=groupDisplayModes[group.key]||group.default_display_mode;for(const mode of group.display_modes){const button=document.createElement("button");button.type="button";button.className="metric-mode";button.textContent=mode.label;button.setAttribute("aria-pressed",String(selectedMode===mode.key));button.addEventListener("click",()=>{if(groupDisplayModes[group.key]===mode.key)return;groupDisplayModes[group.key]=mode.key;renderWorkspace();});toggle.append(button);}heading.append(toggle);}const description=document.createElement("div");description.className="group-description";description.textContent=group.description;const charts=document.createElement("div");charts.className="charts";for(const chart of group.charts)charts.append(renderChart(session,group,activeChart(group,chart)));section.append(heading,description,charts);host.append(section);}
  if(!session.request_usage_coverage.complete||session.request_turnless_count||session.turnless_project_count||session.conversation_metric_missing_count||!session.economic_metrics_available){const coverage=document.createElement("div");coverage.className="coverage-note";const label=document.createElement("strong");label.textContent="Coverage";coverage.append(label);const chip=value=>{const item=document.createElement("span");item.className="coverage-chip";item.textContent=value;coverage.append(item);};chip(`Request series ${session.request_event_count}/${session.request_count}`);chip(`Token series ${fmt(session.request_usage_coverage.event_tokens,"tokens")} / ${fmt(session.totals.provider_tokens,"tokens")}`);if(session.request_turnless_count)chip(`Request turn ID ${session.request_count-session.request_turnless_count}/${session.request_count}`);if(session.project_count&&session.turnless_project_count)chip(`OC turn ID ${session.project_count-session.turnless_project_count}/${session.project_count}`);if(session.conversation_metric_missing_count)chip(`Conversation-only ${session.conversation_metric_count}/${session.project_count} · legacy rows unavailable`);if(session.legacy_project_count)chip(`Legacy OC ${session.legacy_project_count}`);if(!session.economic_metrics_available)chip(session.amortized_metrics_available?"V1.1 charts separate · V1.2 scheduler telemetry present":"V1.1 economic metrics unavailable · legacy session");host.append(coverage);}
}
function selectSession(conversationId){selectedId=conversationId;renderRuns(byId("run-search").value);renderWorkspace();history.replaceState(null,"",`#session=${encodeURIComponent(conversationId)}`);byId("workspace").scrollIntoView({behavior:"smooth",block:"start"});}
function syncSearch(value){byId("run-search").value=value;byId("side-search").value=value;renderRuns(value);}byId("run-search").addEventListener("input",event=>syncSearch(event.target.value));byId("side-search").addEventListener("input",event=>syncSearch(event.target.value));
const hashMatch=location.hash.match(/^#session=(.+)$/);if(hashMatch){try{const candidate=decodeURIComponent(hashMatch[1]);if(DATA.sessions.some(session=>session.conversation_id===candidate))selectedId=candidate;}catch(_error){}}
renderGlobal();renderRuns();renderWorkspace();
</script>
</body>
</html>"""
    return (
        template.replace("__DATA__", data_json)
        .replace("__CONVERSATION__", conversation)
        .replace("__SESSION__", session)
    )


def monitor_directory(hermes_home: Path | None = None) -> Path:
    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / "logs" / MONITOR_DIRNAME


def write_monitor_html(
    timeline: Mapping[str, Any], *, hermes_home: Path | None = None
) -> Path:
    """Atomically write the private monitor snapshot."""

    directory = monitor_directory(hermes_home)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    if isinstance(timeline.get("sessions"), list):
        filename = "object_context_monitor_all_sessions.html"
    else:
        conversation_id = str(timeline.get("conversation_id") or "active")
        digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:12]
        filename = f"object_context_monitor_{digest}.html"
    path = directory / filename
    atomic_write_text(path, render_monitor_html(timeline), create_mode=0o600)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return path


__all__ = [
    "MONITOR_DIRNAME",
    "MONITOR_SCHEMA_VERSION",
    "ObjectContextMonitorServer",
    "build_monitor_dashboard_payload",
    "build_monitor_payload",
    "monitor_directory",
    "render_monitor_html",
    "start_monitor_server",
    "write_monitor_html",
]
