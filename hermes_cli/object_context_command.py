"""Configuration helpers for the classic CLI's ``/object_context`` command.

The command deliberately persists settings for the next CLI process instead of
hot-swapping the live agent.  Changing a Context Engine changes both request
projection and the model-visible tool schema, so doing it in the middle of a
conversation would invalidate prompt-cache and session invariants.
"""

from __future__ import annotations

import math
import re
import shlex
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


OBJECT_CONTEXT_ENGINE = "object_context"
BUILTIN_ENGINE = "compressor"
ECONOMIC_SCHEDULER = "economic"
AMORTIZED_BATCH_SCHEDULER = "amortized_batch"
DYNAMIC_BATCH_POLICY = "dynamic"
FIXED_BATCH_POLICY = "fixed"


@dataclass(frozen=True)
class ParameterSpec:
    """One supported Object Context V1.1/V1.2 configuration value."""

    default: Any
    value_type: type[bool] | type[int] | type[float] | type[str]
    minimum: int | float | None
    maximum: int | float | None
    description: str
    nullable: bool = False
    choices: tuple[str, ...] = ()

    @property
    def range_label(self) -> str:
        if self.value_type is bool:
            return "true | false"
        if self.value_type is str:
            return " | ".join(self.choices) if self.choices else "string"
        assert self.minimum is not None
        nullable = " | null" if self.nullable else ""
        if self.maximum is None:
            return f">= {self.minimum}{nullable}"
        return f"{self.minimum} ≤ value ≤ {self.maximum}{nullable}"


# Keep this table aligned with ObjectContextEngine's constructor and
# DEFAULT_CONFIG["context"]["object_context"].  It is intentionally explicit:
# accepting arbitrary keys would make typos look successful while the engine
# silently ignores them.
PARAMETER_SPECS: dict[str, ParameterSpec] = {
    "enabled": ParameterSpec(
        True,
        bool,
        None,
        None,
        "additional Object Context gate; /object_context on|off also updates it",
    ),
    "scheduler": ParameterSpec(
        "economic",
        str,
        None,
        None,
        "economic (V1.1 immediate) or amortized_batch (V1.2 bounded batch)",
        choices=("economic", "amortized_batch"),
    ),
    "hot_tail_max_inferences": ParameterSpec(
        4,
        int,
        1,
        None,
        "V1.2 maximum inference crossings retained in the bounded Hot Tail",
    ),
    "hot_tail_max_tokens": ParameterSpec(
        12800,
        int,
        1,
        None,
        "V1.2 maximum raw tokens retained in the bounded Hot Tail",
    ),
    "amortized_cache_read_weight": ParameterSpec(
        0.10,
        float,
        0.0,
        1.0,
        "V1.2 repeated-read cost weight used by amortized batch scoring",
    ),
    "batch_policy": ParameterSpec(
        DYNAMIC_BATCH_POLICY,
        str,
        None,
        None,
        "V1.2 dynamic W/Q flush-all or fixed-count comparison policy",
        choices=(DYNAMIC_BATCH_POLICY, FIXED_BATCH_POLICY),
    ),
    "fixed_batch_size": ParameterSpec(
        4,
        int,
        1,
        None,
        "eligible Pending Deltas selected by an ordinary fixed-policy flush",
    ),
    "min_raw_exposures": ParameterSpec(
        1,
        int,
        1,
        None,
        "successful raw request exposures required before projection",
    ),
    "economic_min_net_saving_tokens": ParameterSpec(
        1000,
        int,
        0,
        None,
        "V1.1 required immediate net saving; V1.2 comparison telemetry only",
    ),
    "economic_min_net_saving_usd": ParameterSpec(
        None,
        float,
        0.0,
        None,
        "optional V1.1 USD gate; never a V1.2 flush trigger",
        nullable=True,
    ),
    "economic_cache_read_ratio_fallback": ParameterSpec(
        0.10,
        float,
        0.0,
        1.0,
        "V1.1/cache-comparison read weight when route pricing is unavailable",
    ),
    "economic_cache_write_ratio_fallback": ParameterSpec(
        1.00,
        float,
        0.0,
        None,
        "V1.1/cache-comparison write weight when route pricing is unavailable",
    ),
    "emergency_context_ratio": ParameterSpec(
        0.90,
        float,
        0.10,
        1.0,
        "separate request-viability pressure threshold",
    ),
    "object_prefilter_min_tokens": ParameterSpec(
        256,
        int,
        1,
        None,
        "minimum structured-object size considered for Card work",
    ),
    "min_absolute_saving_tokens": ParameterSpec(
        128,
        int,
        0,
        None,
        "minimum raw-minus-Card token saving",
    ),
    "min_relative_saving_ratio": ParameterSpec(
        0.25,
        float,
        0.0,
        1.0,
        "minimum proportional raw-minus-Card saving",
    ),
    "card_summary_enabled": ParameterSpec(
        False,
        bool,
        None,
        None,
        "generate an LLM semantic summary for each accepted Card",
    ),
    "summary_max_tokens": ParameterSpec(
        64,
        int,
        8,
        None,
        "hard output limit for the Card semantic summary",
    ),
    "wm_grace_deltas": ParameterSpec(
        20,
        int,
        0,
        None,
        "unreferenced-object grace distance before it is evictable",
    ),
    "recent_retrieval_active_deltas": ParameterSpec(
        20,
        int,
        0,
        None,
        "distance for which a recently retrieved object remains active",
    ),
    "retrieval_max_tokens_ratio": ParameterSpec(
        0.50,
        float,
        0.05,
        1.0,
        "largest exact retrieval relative to the model context",
    ),
}

LEGACY_V0_KEYS = frozenset({"min_code_chars", "max_read_lines"})
DEPRECATED_V1_KEYS = frozenset({
    "hot_tail_max_deltas",
    "hot_tail_token_budget_ratio",
    "context_soft_limit_ratio",
})
_BOOLEAN_WORDS = frozenset(
    {"true", "false", "yes", "no", "on", "off", "enable", "disable"}
)
_INTEGER_RE = re.compile(r"^[+-]?\d+$")


class ObjectContextCommandError(ValueError):
    """A safe, user-facing command or configuration error."""


@dataclass(frozen=True)
class ObjectContextCommandResult:
    """Plain-text response returned to the CLI handler."""

    lines: tuple[str, ...]
    changed: bool = False
    artifact_path: str = ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _format_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _normalized_scheduler(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    choices = PARAMETER_SPECS["scheduler"].choices
    return normalized if normalized in choices else ECONOMIC_SCHEDULER


def _reported_scheduler(engine_status: Mapping[str, Any] | None) -> str | None:
    if not isinstance(engine_status, Mapping):
        return None
    for name in ("effective_scheduler", "scheduler"):
        value = str(engine_status.get(name) or "").strip().casefold()
        if value in PARAMETER_SPECS["scheduler"].choices:
            return value
    return None


def _normalized_batch_policy(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    choices = PARAMETER_SPECS["batch_policy"].choices
    return normalized if normalized in choices else DYNAMIC_BATCH_POLICY


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 1 else default


def _scheduler_version(scheduler: str) -> str:
    return "V1.2" if scheduler == AMORTIZED_BATCH_SCHEDULER else "V1.1"


def _metric_value(metrics: Mapping[str, Any], name: str) -> float:
    value = metrics.get(name, 0)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _format_tokens(value: Any) -> str:
    try:
        number = max(0, int(round(float(value))))
    except (TypeError, ValueError, OverflowError):
        number = 0
    return f"~{number:,}"


def _format_bytes(value: Any) -> str:
    try:
        size = max(0.0, float(value))
    except (TypeError, ValueError):
        size = 0.0
    if not math.isfinite(size):
        size = 0.0
    units = ("B", "KiB", "MiB", "GiB")
    unit = units[0]
    for unit in units:
        if size < 1024 or unit == units[-1]:
            break
        size /= 1024
    return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"


def _saving_percent(raw_tokens: float, saved_tokens: float) -> float:
    if raw_tokens <= 0:
        return 0.0
    return max(0.0, min(100.0, saved_tokens / raw_tokens * 100.0))


def parse_parameter_value(name: str, raw_value: str) -> Any:
    """Parse one parameter strictly and enforce the engine's real range."""

    spec = PARAMETER_SPECS.get(name)
    if spec is None:
        if name in DEPRECATED_V1_KEYS:
            raise ObjectContextCommandError(
                f"{name} is a deprecated V1 Hot Tail setting and does not control "
                "either supported scheduler. Use /object_context help."
            )
        raise ObjectContextCommandError(
            "Unknown Object Context parameter: "
            f"{name}. Use /object_context help to list valid names."
        )

    text = str(raw_value or "").strip()
    if spec.nullable and text.casefold() in {"null", "none"}:
        return None
    if spec.value_type is str:
        normalized = text.casefold()
        if normalized and (not spec.choices or normalized in spec.choices):
            return normalized
        raise ObjectContextCommandError(
            f"{name} requires {spec.range_label}; received {raw_value!r}."
        )
    if spec.value_type is bool:
        normalized = text.casefold()
        if normalized in {"true", "yes", "on", "enable"}:
            return True
        if normalized in {"false", "no", "off", "disable"}:
            return False
        raise ObjectContextCommandError(
            f"{name} requires true or false; received {raw_value!r}."
        )
    if not text or text.casefold() in _BOOLEAN_WORDS:
        raise ObjectContextCommandError(
            f"{name} requires a numeric value, not {raw_value!r}."
        )

    try:
        if spec.value_type is int:
            if not _INTEGER_RE.fullmatch(text):
                raise ValueError
            value: int | float = int(text)
        else:
            value = float(text)
            if not math.isfinite(value):
                raise ValueError
    except (TypeError, ValueError) as exc:
        kind = "integer" if spec.value_type is int else "number"
        raise ObjectContextCommandError(
            f"{name} requires a finite {kind}; received {raw_value!r}."
        ) from exc

    assert spec.minimum is not None
    if value < spec.minimum or (
        spec.maximum is not None and value > spec.maximum
    ):
        raise ObjectContextCommandError(
            f"{name} must satisfy {spec.range_label}; received {_format_value(value)}."
        )
    return value


def _assert_paths_writable(*paths: str) -> None:
    from hermes_cli import managed_scope
    from hermes_cli.config import is_managed

    if is_managed():
        raise ObjectContextCommandError(
            "This Hermes installation is managed; local configuration writes are disabled."
        )
    for path in paths:
        if managed_scope.is_key_managed(path):
            raise ObjectContextCommandError(
                f"Cannot change {path}: it is managed by your administrator."
            )


def _raw_config_for_write() -> dict[str, Any]:
    from hermes_cli.config import read_user_config_raw

    try:
        raw = read_user_config_raw()
    except Exception as exc:
        raise ObjectContextCommandError(
            f"Cannot read config.yaml: {exc}. Fix its YAML before retrying."
        ) from exc
    if not isinstance(raw, dict):
        raise ObjectContextCommandError(
            "config.yaml must contain a YAML mapping before it can be updated."
        )
    return raw


def _context_mapping(raw: dict[str, Any], *, create: bool) -> dict[str, Any]:
    value = raw.get("context")
    if value is None:
        if not create:
            return {}
        value = {}
        raw["context"] = value
    if not isinstance(value, dict):
        raise ObjectContextCommandError(
            "Cannot update V1: config.yaml 'context' must be a mapping."
        )
    return value


def _object_mapping(context: dict[str, Any], *, create: bool) -> dict[str, Any]:
    value = context.get("object_context")
    if value is None:
        if not create:
            return {}
        value = {}
        context["object_context"] = value
    if not isinstance(value, dict):
        raise ObjectContextCommandError(
            "Cannot update V1: 'context.object_context' must be a mapping."
        )
    return value


def _save_raw_config(raw: dict[str, Any]) -> None:
    from hermes_cli.config import save_config

    try:
        # `raw` contains only user-owned values, not merged defaults. Keep
        # explicit values even when they equal today's default so `/status`
        # can truthfully distinguish an override from an inherited default.
        save_config(raw, strip_defaults=False)
    except Exception as exc:
        raise ObjectContextCommandError(f"Could not save config.yaml: {exc}") from exc


def _set_engine(engine_name: str) -> bool:
    _assert_paths_writable("context.engine", "context.object_context.enabled")
    raw = _raw_config_for_write()
    context = _context_mapping(raw, create=True)
    current = str(context.get("engine") or BUILTIN_ENGINE)
    object_cfg = _object_mapping(context, create=True)
    enabled = engine_name == OBJECT_CONTEXT_ENGINE
    if current == engine_name and object_cfg.get("enabled", True) is enabled:
        return False
    context["engine"] = engine_name
    object_cfg["enabled"] = enabled
    _save_raw_config(raw)
    return True


def _set_parameter(name: str, value: Any) -> bool:
    path = f"context.object_context.{name}"
    _assert_paths_writable(path)
    raw = _raw_config_for_write()
    context = _context_mapping(raw, create=True)
    object_cfg = _object_mapping(context, create=True)
    if (
        name in object_cfg
        and object_cfg.get(name) == value
        and type(object_cfg.get(name)) is type(value)
    ):
        return False
    object_cfg[name] = value
    _save_raw_config(raw)
    return True


def _reset_parameters(name: str) -> tuple[bool, tuple[str, ...]]:
    if (
        name not in PARAMETER_SPECS
        and name not in LEGACY_V0_KEYS
        and name not in DEPRECATED_V1_KEYS
        and name != "all"
    ):
        raise ObjectContextCommandError(
            "Unknown Object Context parameter: "
            f"{name}. Use /object_context help to list valid names."
        )

    targets = (
        set(PARAMETER_SPECS) | set(LEGACY_V0_KEYS) | set(DEPRECATED_V1_KEYS)
        if name == "all"
        else {name}
    )
    _assert_paths_writable(
        *(f"context.object_context.{target}" for target in sorted(targets))
    )
    raw = _raw_config_for_write()
    context = _context_mapping(raw, create=False)
    object_cfg = _object_mapping(context, create=False)
    removed = tuple(sorted(target for target in targets if target in object_cfg))
    if not removed:
        return False, ()
    for target in removed:
        object_cfg.pop(target, None)
    if not object_cfg:
        context.pop("object_context", None)
    if not context:
        raw.pop("context", None)
    _save_raw_config(raw)
    return True, removed


def _active_label(active_engine: str) -> str:
    if not active_engine:
        return "unknown"
    if active_engine == OBJECT_CONTEXT_ENGINE:
        return "ON (object_context)"
    if active_engine == BUILTIN_ENGINE:
        return "OFF (compressor)"
    return active_engine


def _savings_summary_lines(
    active_engine: str,
    engine_status: Mapping[str, Any] | None,
) -> list[str]:
    if active_engine != OBJECT_CONTEXT_ENGINE:
        return []
    if not isinstance(engine_status, Mapping):
        return ["", "Live Object Context savings: unavailable until an agent is active."]
    if engine_status.get("object_context_available") is False:
        return ["", "Live Object Context savings: storage is unavailable for this session."]

    version = _scheduler_version(
        _reported_scheduler(engine_status) or ECONOMIC_SCHEDULER
    )

    latest = _mapping(engine_status.get("last_request_metrics"))
    totals = _mapping(engine_status.get("request_metric_totals"))
    try:
        count = max(0, int(engine_status.get("request_projection_count", 0) or 0))
    except (TypeError, ValueError):
        count = 0
    if count == 0:
        return [
            "",
            f"Live {version} savings: no request projection has been recorded yet.",
            "Details: /object_context stats",
        ]

    conversation_metrics_available = all(
        name in latest and name in totals
        for name in ("raw_conversation_tokens", "conversation_tokens_saved")
    )
    raw_name = (
        "raw_conversation_tokens"
        if conversation_metrics_available
        else "raw_context_tokens"
    )
    saved_name = (
        "conversation_tokens_saved"
        if conversation_metrics_available
        else "tokens_saved"
    )
    last_raw = _metric_value(latest, raw_name)
    last_saved = _metric_value(latest, saved_name)
    total_raw = _metric_value(totals, raw_name)
    total_saved = _metric_value(totals, saved_name)
    request_word = "request" if count == 1 else "requests"
    scope_label = (
        "persisted conversation-record tokens"
        if conversation_metrics_available
        else "legacy assembled-message tokens"
    )
    return [
        "",
        f"Live {version} savings (estimated {scope_label}):",
        (
            f"  Last projection: {_format_tokens(last_saved)} avoided "
            f"({_saving_percent(last_raw, last_saved):.1f}%)"
        ),
        (
            f"  Session:         {_format_tokens(total_saved)} avoided across "
            f"{count:,} projected {request_word} "
            f"({_saving_percent(total_raw, total_saved):.1f}%)"
        ),
        "  Details: /object_context stats",
    ]


def _stats_result(
    active_engine: str,
    engine_status: Mapping[str, Any] | None,
) -> ObjectContextCommandResult:
    version = _scheduler_version(
        _reported_scheduler(engine_status) or ECONOMIC_SCHEDULER
    )
    lines = [f"Object Context {version} Token Savings", "─" * 72]
    if active_engine != OBJECT_CONTEXT_ENGINE:
        lines.extend(
            [
                f"Unavailable: the active session is {_active_label(active_engine)}.",
                "Use /object_context on, restart the CLI, and send a message first.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if not isinstance(engine_status, Mapping):
        lines.extend(
            [
                "Unavailable: no live Object Context status is available yet.",
                "Send a message first, then run /object_context stats again.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if engine_status.get("object_context_available") is False:
        lines.extend(
            [
                "Unavailable: Object Context storage is not active for this session.",
                "Check the V1 store initialization warning in the agent log.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))

    latest = _mapping(engine_status.get("last_request_metrics"))
    totals = _mapping(engine_status.get("request_metric_totals"))
    all_totals = _mapping(engine_status.get("metric_totals"))
    try:
        count = max(0, int(engine_status.get("request_projection_count", 0) or 0))
    except (TypeError, ValueError):
        count = 0

    if count == 0:
        lines.extend(
            [
                "No request projection has been recorded for this conversation yet.",
                "Send a message that reaches the model, then run this command again.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))

    last_request_raw = _metric_value(latest, "raw_context_tokens")
    last_request_rendered = _metric_value(latest, "rendered_context_tokens")
    last_request_saved = _metric_value(latest, "tokens_saved")
    total_request_raw = _metric_value(totals, "raw_context_tokens")
    total_request_rendered = _metric_value(totals, "rendered_context_tokens")
    total_request_saved = _metric_value(totals, "tokens_saved")
    conversation_metrics_available = all(
        name in latest and name in totals
        for name in (
            "raw_conversation_tokens",
            "rendered_conversation_tokens",
            "conversation_tokens_saved",
        )
    )
    last_conversation_raw = _metric_value(latest, "raw_conversation_tokens")
    last_conversation_rendered = _metric_value(
        latest, "rendered_conversation_tokens"
    )
    last_conversation_saved = _metric_value(
        latest, "conversation_tokens_saved"
    )
    total_conversation_raw = _metric_value(totals, "raw_conversation_tokens")
    total_conversation_rendered = _metric_value(
        totals, "rendered_conversation_tokens"
    )
    total_conversation_saved = _metric_value(
        totals, "conversation_tokens_saved"
    )
    retrieved_tokens = _metric_value(all_totals, "retrieved_tokens")
    try:
        retrieval_count = max(0, int(engine_status.get("retrieval_count", 0) or 0))
        working_objects = max(
            0, int(engine_status.get("working_memory_object_count", 0) or 0)
        )
    except (TypeError, ValueError):
        retrieval_count = 0
        working_objects = 0

    lines.extend(["Scope: active conversation", ""])
    if conversation_metrics_available:
        lines.extend(
            [
                f"Conversation records only ({version} evaluation scope):",
                f"  Last raw conversation             {_format_tokens(last_conversation_raw):>14}",
                f"  Last rendered conversation        {_format_tokens(last_conversation_rendered):>14}",
                f"  Last tokens avoided               {_format_tokens(last_conversation_saved):>14}",
                f"  Last reduction                    {_saving_percent(last_conversation_raw, last_conversation_saved):>13.1f}%",
                f"  Cumulative raw conversation       {_format_tokens(total_conversation_raw):>14}",
                f"  Cumulative rendered conversation  {_format_tokens(total_conversation_rendered):>14}",
                f"  Cumulative tokens avoided         {_format_tokens(total_conversation_saved):>14}",
                f"  Cumulative reduction              {_saving_percent(total_conversation_raw, total_conversation_saved):>13.1f}%",
            ]
        )
    else:
        lines.extend(
            [
                f"Conversation records only ({version} evaluation scope):",
                "  Unavailable for this legacy session; send one new model request",
                "  to begin recording the system/prefill-free denominator.",
            ]
        )
    lines.extend(
        [
            "",
            "Assembled request-message view (diagnostic):",
            f"  Last raw message view             {_format_tokens(last_request_raw):>14}",
            f"  Last rendered message view        {_format_tokens(last_request_rendered):>14}",
            f"  Last tokens avoided               {_format_tokens(last_request_saved):>14}",
            f"  Last reduction                    {_saving_percent(last_request_raw, last_request_saved):>13.1f}%",
            f"  Projected requests                {count:>14,}",
            f"  Cumulative raw message view       {_format_tokens(total_request_raw):>14}",
            f"  Cumulative rendered message view  {_format_tokens(total_request_rendered):>14}",
            f"  Cumulative tokens avoided         {_format_tokens(total_request_saved):>14}",
            f"  Average avoided / request         {_format_tokens(total_request_saved / count):>14}",
            f"  Cumulative reduction              {_saving_percent(total_request_raw, total_request_saved):>13.1f}%",
            "",
            "Retrieval and Working Memory:",
            f"  Successful retrievals             {retrieval_count:>14,}",
            f"  Retrieved payload tokens          {_format_tokens(retrieved_tokens):>14}",
            f"  Working Memory objects            {working_objects:>14,}",
            (
                "  Working Memory size               "
                f"{_format_bytes(engine_status.get('working_memory_bytes', 0)):>14}"
            ),
            "",
            "Conversation-only metrics use persisted user/assistant/tool history and",
            "exclude system prompt, tool schemas, prefills, and request-only injections.",
            "The assembled message view includes system/prefill but not tool schemas;",
            "provider tokenizer, caching, transport retries, and billing can still differ.",
            "The avoided totals already reflect retrieval projection; retrieved payload",
            "is shown separately and is not subtracted a second time.",
        ]
    )
    return ObjectContextCommandResult(tuple(lines))


def _monitor_result(
    active_engine: str,
    engine_status: Mapping[str, Any] | None,
    monitor_timeline: Mapping[str, Any] | None,
) -> ObjectContextCommandResult:
    lines = ["Object Context Session Dynamics Monitor", "─" * 72]
    session_timelines = (
        monitor_timeline.get("sessions")
        if isinstance(monitor_timeline, Mapping)
        else None
    )
    has_session_snapshot = bool(
        isinstance(session_timelines, list)
        and any(
            isinstance(item, Mapping)
            and str(item.get("conversation_id") or "").strip()
            for item in session_timelines
        )
    )
    if active_engine != OBJECT_CONTEXT_ENGINE and not has_session_snapshot:
        lines.extend(
            [
                f"Unavailable: the active session is {_active_label(active_engine)}.",
                "Use /object_context on, restart the CLI, and send a message first.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if not isinstance(engine_status, Mapping) and not has_session_snapshot:
        lines.extend(
            [
                "Unavailable: no live Object Context status is available yet.",
                "Send a message first, then run /object_context monitor again.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if (
        isinstance(engine_status, Mapping)
        and engine_status.get("object_context_available") is False
        and not has_session_snapshot
    ):
        lines.extend(
            [
                "Unavailable: Object Context storage is not active for this session.",
                "Check the V1 store initialization warning in the agent log.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if not isinstance(monitor_timeline, Mapping):
        lines.extend(
            [
                "Unavailable: the active engine does not expose projection telemetry.",
                "Restart the CLI after updating Ansatz, then send a message first.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    projections = monitor_timeline.get("projections")
    if (
        (not isinstance(projections, list) or not projections)
        and not has_session_snapshot
    ):
        lines.extend(
            [
                "No request projection has been recorded for this conversation yet.",
                "Send a message that reaches the model, then run this command again.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))

    try:
        from hermes_cli.object_context_monitor import (
            build_monitor_dashboard_payload,
            write_monitor_html,
        )

        payload = build_monitor_dashboard_payload(monitor_timeline)
        path = write_monitor_html(monitor_timeline)
    except Exception as exc:
        raise ObjectContextCommandError(
            f"Could not create the session dynamics webpage: {exc}"
        ) from exc

    selected = next(
        (
            session
            for session in payload.get("sessions", [])
            if session.get("conversation_id")
            == payload.get("selected_conversation_id")
        ),
        {},
    )
    global_totals = payload.get("global_totals", {})
    lines.extend(
        [
            f"Sessions: {int(payload.get('session_count', 0)):,}",
            (
                f"Requests: {int(global_totals.get('request_count', 0)):,} total"
                f" · {int(selected.get('request_count', 0)):,} selected"
            ),
            (
                f"Projects: {int(global_totals.get('project_count', 0)):,} total"
                f" · {int(selected.get('project_count', 0)):,} selected"
            ),
            f"Turns:    {int(selected.get('turn_count', 0)):,} selected",
            "Dashboard: " + str(path),
            "Opening the private local HTML dashboard in your browser…",
        ]
    )
    project_count = int(selected.get("project_count", 0) or 0)
    turnless = int(selected.get("turnless_project_count", 0) or 0)
    request_count = int(selected.get("request_count", 0) or 0)
    request_events = int(selected.get("request_event_count", 0) or 0)
    legacy = int(selected.get("legacy_project_count", 0) or 0)
    if request_events < request_count:
        lines.append(
            f"Request-series coverage: {request_events:,}/{request_count:,}; "
            "the provider-token KPI still uses the exact SessionDB aggregate."
        )
    if turnless:
        lines.append(
            f"Coverage: {project_count - turnless:,}/{project_count:,} projects "
            "have turn identity."
        )
    if legacy:
        lines.append(f"Compatibility: {legacy:,} of the uncovered projects are legacy.")
    return ObjectContextCommandResult(tuple(lines), artifact_path=str(path))


def _status_result(
    active_engine: str,
    engine_status: Mapping[str, Any] | None,
) -> ObjectContextCommandResult:
    from hermes_cli.config import load_config
    from hermes_constants import display_hermes_home

    effective = load_config() or {}
    raw = _raw_config_for_write()
    effective_context = _mapping(effective.get("context"))
    raw_context = _mapping(raw.get("context"))
    effective_object = _mapping(effective_context.get("object_context"))
    raw_object = _mapping(raw_context.get("object_context"))
    configured_engine = str(effective_context.get("engine") or BUILTIN_ENGINE)
    configured_enabled = effective_object.get("enabled", True) is not False
    configured_on = (
        configured_engine == OBJECT_CONTEXT_ENGINE and configured_enabled
    )
    configured_scheduler = _normalized_scheduler(
        effective_object.get(
            "scheduler", PARAMETER_SPECS["scheduler"].default
        )
    )
    active_scheduler = (
        _reported_scheduler(engine_status)
        if active_engine == OBJECT_CONTEXT_ENGINE
        else None
    )
    effective_scheduler = active_scheduler or configured_scheduler
    scheduler_source = (
        "active runtime"
        if active_scheduler is not None
        else (
            "configured fallback; runtime did not report scheduler"
            if active_engine == OBJECT_CONTEXT_ENGINE
            else "configured next launch"
        )
    )

    configured_hot_inferences = _positive_int(
        effective_object.get("hot_tail_max_inferences"),
        PARAMETER_SPECS["hot_tail_max_inferences"].default,
    )
    configured_hot_tokens = _positive_int(
        effective_object.get("hot_tail_max_tokens"),
        PARAMETER_SPECS["hot_tail_max_tokens"].default,
    )
    active_status = engine_status if isinstance(engine_status, Mapping) else {}
    hot_inferences = _positive_int(
        active_status.get("hot_tail_max_inferences"),
        configured_hot_inferences,
    )
    hot_tokens = _positive_int(
        active_status.get("hot_tail_max_tokens"),
        configured_hot_tokens,
    )
    pending_inferences = _positive_int(
        active_status.get("pending_max_inferences"),
        hot_inferences * 2,
    )
    pending_tokens = _positive_int(
        active_status.get("pending_max_tokens"),
        hot_tokens * 2,
    )
    configured_batch_policy = _normalized_batch_policy(
        effective_object.get(
            "batch_policy", PARAMETER_SPECS["batch_policy"].default
        )
    )
    active_batch_policy = (
        _normalized_batch_policy(active_status.get("batch_policy"))
        if active_engine == OBJECT_CONTEXT_ENGINE
        and active_status.get("batch_policy") is not None
        else None
    )
    effective_batch_policy = active_batch_policy or configured_batch_policy
    configured_fixed_batch_size = _positive_int(
        effective_object.get("fixed_batch_size"),
        PARAMETER_SPECS["fixed_batch_size"].default,
    )
    fixed_batch_size = _positive_int(
        active_status.get("fixed_batch_size"),
        configured_fixed_batch_size,
    )
    version = _scheduler_version(effective_scheduler)

    lines = [
        "Object Context V1.1 / V1.2",
        "─" * 72,
        f"Configured: {'ON' if configured_on else 'OFF'} ({configured_engine})",
        f"Active session: {_active_label(active_engine)}",
        f"Effective scheduler: {effective_scheduler} ({version}; {scheduler_source})",
        (
            "Algorithm: immediate next-request economic; no fixed Hot Tail"
            if effective_scheduler == ECONOMIC_SCHEDULER
            else (
                "Algorithm: bounded Hot Tail/pending; fixed oldest-"
                f"{fixed_batch_size} eligible Deltas"
                if effective_batch_policy == FIXED_BATCH_POLICY
                else "Algorithm: bounded Hot Tail/pending amortized batch crossing"
            )
        ),
        (
            "V1.2 batch policy: "
            f"{effective_batch_policy}"
            + (
                f" (ordinary batch={fixed_batch_size:,} Deltas)"
                if effective_batch_policy == FIXED_BATCH_POLICY
                else " (W/Q crossing, flush-all)"
            )
        ),
        (
            "V1.2 Hot Tail caps: "
            f"{hot_inferences:,} inferences / {hot_tokens:,} tokens"
        ),
        (
            "V1.2 pending caps: "
            f"{pending_inferences:,} inferences / {pending_tokens:,} tokens "
            "(fixed 2x Hot Tail)"
        ),
        f"Config: {display_hermes_home()}/config.yaml",
    ]
    if active_engine and active_engine != configured_engine:
        lines.append("Restart pending: configured and active engines differ.")
    if (
        active_scheduler is not None
        and active_scheduler != configured_scheduler
    ):
        lines.append("Restart pending: configured and active schedulers differ.")
    if (
        active_batch_policy is not None
        and active_batch_policy != configured_batch_policy
    ):
        lines.append("Restart pending: configured and active batch policies differ.")
    if (
        active_engine == OBJECT_CONTEXT_ENGINE
        and active_status.get("fixed_batch_size") is not None
        and fixed_batch_size != configured_fixed_batch_size
    ):
        lines.append("Restart pending: configured and active fixed batch sizes differ.")
    if (
        active_engine == OBJECT_CONTEXT_ENGINE
        and effective_scheduler == AMORTIZED_BATCH_SCHEDULER
        and not all(
            name in active_status
            for name in (
                "hot_tail_max_inferences",
                "hot_tail_max_tokens",
                "pending_max_inferences",
                "pending_max_tokens",
            )
        )
    ):
        lines.append(
            "Runtime cap fields unavailable; V1.2 caps above use configured/2x values."
        )
    active_card_summary = (
        engine_status.get("card_summary_enabled")
        if active_engine == OBJECT_CONTEXT_ENGINE
        and isinstance(engine_status, Mapping)
        else None
    )
    if isinstance(active_card_summary, bool):
        lines.append(
            "Active Card summaries: "
            + ("ON" if active_card_summary else "OFF")
        )
        configured_card_summary = effective_object.get(
            "card_summary_enabled",
            PARAMETER_SPECS["card_summary_enabled"].default,
        )
        if (
            configured_on
            and isinstance(configured_card_summary, bool)
            and configured_card_summary is not active_card_summary
        ):
            lines.append(
                "Restart pending: configured and active Card-summary settings differ."
            )
    lines.extend(_savings_summary_lines(active_engine, engine_status))
    lines.extend(["", "Parameters:"])
    for name, spec in PARAMETER_SPECS.items():
        value = effective_object.get(name, spec.default)
        source = "explicit" if name in raw_object else "default"
        lines.append(
            f"  {name:<36} {_format_value(value):>8}  [{source}]"
        )

    deprecated = sorted(key for key in DEPRECATED_V1_KEYS if key in raw_object)
    if deprecated:
        lines.extend(
            [
                "",
                "Deprecated inactive V1 scheduler settings: "
                + ", ".join(deprecated),
                "They remain ignored and are not mapped to either supported scheduler.",
            ]
        )
    legacy = sorted(key for key in LEGACY_V0_KEYS if key in raw_object)
    if legacy:
        lines.extend(
            [
                "",
                "Ignored V0 settings: " + ", ".join(legacy),
                "Remove them with /object_context reset all.",
            ]
        )
    lines.extend(
        [
            "",
            "Usage:",
            "  /object_context on|off|status|stats|monitor",
            "  /object_context set <parameter> <value>",
            "  /object_context reset [parameter|all]",
            "  /object_context help",
            "",
            "Configuration changes take effect after restarting the CLI.",
        ]
    )
    return ObjectContextCommandResult(tuple(lines))


def _help_result() -> ObjectContextCommandResult:
    lines = [
        "Object Context V1.1 / V1.2 command",
        "─" * 72,
        "  /object_context                 show status and effective values",
        "  /object_context stats           show live Object Context token savings",
        "  /object_context monitor         open the session dynamics webpage",
        "  /object_context on              select object_context for next launch",
        "  /object_context off             select the built-in compressor",
        "  /object_context set KEY VALUE   persist one validated override",
        "  /object_context reset [KEY|all] remove explicit overrides",
        "",
        "Supported parameters:",
    ]
    for name, spec in PARAMETER_SPECS.items():
        lines.append(
            f"  {name:<36} default={_format_value(spec.default):<6} "
            f"range={spec.range_label}"
        )
        lines.append(f"    {spec.description}")
    lines.extend(
        [
            "",
            "Examples:",
            "  /object_context stats",
            "  /object_context monitor",
            "  /object_context set scheduler amortized_batch",
            "  /object_context set batch_policy fixed",
            "  /object_context set fixed_batch_size 4",
            "  /object_context set hot_tail_max_inferences 4",
            "  /object_context set hot_tail_max_tokens 12800",
            "  /object_context set economic_min_net_saving_tokens 1500",
            "  /object_context set emergency_context_ratio 0.92",
            "  /object_context set economic_min_net_saving_usd null",
            "  /object_context set card_summary_enabled true",
            "  /object_context reset economic_min_net_saving_tokens",
            "",
            "V1.2 pending caps are fixed at 2x the configured Hot Tail caps.",
            "Fixed policy batches count eligible Pending Deltas, not Cards.",
            "Capacity/emergency safety actions may override ordinary fixed-N batching.",
            "Changing scheduler never rewrites historical V1.1 telemetry.",
            "Changes are profile-scoped and require a CLI restart.",
        ]
    )
    return ObjectContextCommandResult(tuple(lines))


def run_object_context_command(
    args_raw: str,
    *,
    active_engine: str = "",
    engine_status: Mapping[str, Any] | None = None,
    monitor_timeline: Mapping[str, Any] | None = None,
) -> ObjectContextCommandResult:
    """Execute the configuration command and return terminal-ready text."""

    try:
        args = shlex.split(str(args_raw or ""))
    except ValueError as exc:
        raise ObjectContextCommandError(f"Invalid command quoting: {exc}") from exc

    if not args or args[0].casefold() in {"status", "show"}:
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context status")
        return _status_result(active_engine, engine_status)

    action = args[0].casefold()
    if action == "stats":
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context stats")
        return _stats_result(active_engine, engine_status)

    if action == "monitor":
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context monitor")
        return _monitor_result(active_engine, engine_status, monitor_timeline)

    if action in {"help", "?"}:
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context help")
        return _help_result()

    if action in {"on", "enable"}:
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context on")
        changed = _set_engine(OBJECT_CONTEXT_ENGINE)
        state = "enabled" if changed else "already enabled"
        return ObjectContextCommandResult(
            (
                f"Object Context is {state} in config.yaml.",
                "The live conversation was not changed; restart the CLI to apply it.",
            ),
            changed=changed,
        )

    if action in {"off", "disable"}:
        if len(args) > 1:
            raise ObjectContextCommandError("Usage: /object_context off")
        changed = _set_engine(BUILTIN_ENGINE)
        state = "disabled" if changed else "already disabled"
        return ObjectContextCommandResult(
            (
                f"Object Context is {state} in config.yaml.",
                "The live conversation was not changed; restart the CLI to apply it.",
            ),
            changed=changed,
        )

    if action == "set":
        if len(args) != 3:
            raise ObjectContextCommandError(
                "Usage: /object_context set <parameter> <value>"
            )
        name = args[1]
        value = parse_parameter_value(name, args[2])
        changed = _set_parameter(name, value)
        state = "Saved" if changed else "Unchanged"
        return ObjectContextCommandResult(
            (
                f"{state}: context.object_context.{name} = {_format_value(value)}",
                "This does not toggle Object Context. Use /object_context on if needed.",
                "Restart the CLI to apply the parameter to a live agent.",
            ),
            changed=changed,
        )

    if action == "reset":
        if len(args) > 2:
            raise ObjectContextCommandError(
                "Usage: /object_context reset [parameter|all]"
            )
        name = args[1] if len(args) == 2 else "all"
        changed, removed = _reset_parameters(name)
        if not changed:
            message = (
                "No explicit Object Context overrides were set."
                if name == "all"
                else f"No explicit override was set for {name}."
            )
        elif name == "all":
            message = "Reset Object Context overrides: " + ", ".join(removed)
        elif name in LEGACY_V0_KEYS:
            message = f"Removed ignored V0 setting: {name}"
        elif name in DEPRECATED_V1_KEYS:
            message = f"Removed deprecated inactive V1 setting: {name}"
        else:
            default = PARAMETER_SPECS[name].default
            message = f"Reset {name}; effective default is {_format_value(default)}."
        return ObjectContextCommandResult(
            (
                message,
                "Restart the CLI to apply the effective values to a live agent.",
            ),
            changed=changed,
        )

    raise ObjectContextCommandError(
        "Unknown action. Use /object_context "
        "[status|stats|monitor|on|off|set|reset|help]."
    )


def active_context_engine_name(agent: Any) -> str:
    """Return the live engine's stable name for status output."""

    engine = getattr(agent, "context_compressor", None) if agent is not None else None
    name = str(getattr(engine, "name", "") or "").strip()
    if name:
        return name
    if engine is not None and type(engine).__name__ == "ContextCompressor":
        return BUILTIN_ENGINE
    return ""


def active_context_engine_status(agent: Any) -> dict[str, Any] | None:
    """Read the live engine's public status without exposing engine internals."""

    engine = getattr(agent, "context_compressor", None) if agent is not None else None
    get_status = getattr(engine, "get_status", None)
    if not callable(get_status):
        return None
    try:
        status = get_status()
    except Exception:
        return None
    return dict(status) if isinstance(status, Mapping) else None


def active_context_engine_monitor(agent: Any) -> dict[str, Any] | None:
    """Read content-free projection telemetry from the active engine."""

    engine = getattr(agent, "context_compressor", None) if agent is not None else None
    get_timeline = getattr(engine, "get_projection_timeline", None)
    if not callable(get_timeline):
        return None
    try:
        timeline = get_timeline()
    except Exception:
        return None
    if not isinstance(timeline, Mapping):
        return None
    result = dict(timeline)
    status = active_context_engine_status(agent) or {}
    object_metrics = dict(_mapping(status.get("metric_totals")))
    object_metrics["retrieval_count"] = _safe_nonnegative_int(
        status.get("retrieval_count")
    )
    result["object_context_metrics"] = object_metrics
    result["retrieve_object_schema_tokens_per_request"] = (
        _retrieve_object_schema_tokens_rough()
    )
    session_db = getattr(agent, "_session_db", None)
    session_id = str(getattr(agent, "session_id", "") or "")
    lineage = _compression_lineage(session_db, session_id)
    result["auxiliary_usage"] = _auxiliary_usage_for(
        session_db, lineage or [session_id]
    )
    return result


def _stored_session_title(
    session_db: Any,
    conversation_id: str,
    projections: list[dict[str, Any]],
    *,
    current_session_id: str = "",
) -> str:
    """Resolve stored title metadata without reading conversation content.

    New projection events retain the concrete session segment that produced
    them. Checking those segments newest-first lets compressed conversations
    use the title carried to their latest continuation while legacy telemetry
    safely falls back to the stable root row.
    """

    getter = getattr(session_db, "get_session_title", None)
    if not callable(getter):
        return ""
    candidates = [
        str(event.get("session_id") or "").strip()
        for event in reversed(projections)
        if isinstance(event, Mapping)
    ]
    candidates.extend([str(current_session_id or "").strip(), conversation_id])
    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            title = str(getter(candidate) or "").strip()
        except Exception:
            continue
        if title:
            return title
    return ""


_LEGACY_API_USAGE_RE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"INFO \[(?P<session_id>[^\]]+)\] agent\.conversation_loop: "
    r"API call #(?P<sequence>\d+): model=(?P<model>.*?) "
    r"provider=(?P<provider>.*?) in=(?P<prompt>\d+) "
    r"out=(?P<output>\d+) total=(?P<total>\d+) "
    r"latency=(?P<latency>[0-9.]+)s"
    r"(?: cache=(?P<cache_read>\d+)/(?P<cache_prompt>\d+) \([^)]*\))?$"
)


def _safe_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_nonnegative_float(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return max(0.0, number) if math.isfinite(number) else 0.0


def _compression_lineage(session_db: Any, session_id: str) -> list[str]:
    normalized = str(session_id or "").strip()
    if not normalized:
        return []
    getter = getattr(session_db, "get_compression_lineage", None)
    if callable(getter):
        try:
            lineage = getter(normalized)
            if isinstance(lineage, list) and lineage:
                return [str(value) for value in lineage if str(value or "").strip()]
        except Exception:
            pass
    return [normalized]


def _auxiliary_usage_for(
    session_db: Any, session_ids: list[str]
) -> list[dict[str, Any]]:
    getter = getattr(session_db, "auxiliary_usage_breakdown", None)
    if not callable(getter):
        return []
    try:
        rows = getter(session_ids)
    except Exception:
        return []
    return [dict(row) for row in rows or [] if isinstance(row, Mapping)]


def _main_request_turn_ids(timeline: Mapping[str, Any]) -> set[str]:
    """Return durable main-loop turn identities available to the monitor."""

    return {
        str(event.get("turn_id") or "").strip()
        for event in timeline.get("requests") or []
        if isinstance(event, Mapping)
        and str(event.get("turn_id") or "").strip()
    }


def _scope_monitor_retrieval_metrics(
    store: Any,
    conversation_id: str,
    timeline: Mapping[str, Any],
    metrics: dict[str, Any],
) -> None:
    """Replace conversation-wide retrieval totals with main-turn totals."""

    turn_ids = _main_request_turn_ids(timeline)
    getter = getattr(store, "retrieval_totals_for_turns", None)
    if not turn_ids or not callable(getter):
        return
    try:
        scoped = getter(conversation_id, turn_ids)
    except Exception:
        return
    if isinstance(scoped, Mapping):
        metrics["retrieval_count"] = _safe_nonnegative_int(
            scoped.get("retrieval_count")
        )
        metrics["retrieved_tokens"] = _safe_nonnegative_int(
            scoped.get("retrieved_tokens")
        )


def _retrieve_object_schema_tokens_rough() -> int:
    """Estimate the per-request OC retrieval schema using the live schema."""

    try:
        from agent.model_metadata import _estimate_tools_tokens_rough
        from plugins.context_engine.object_context.engine import ObjectContextEngine

        tools = [
            {"type": "function", "function": schema}
            for schema in ObjectContextEngine.get_tool_schemas()
        ]
        return _safe_nonnegative_int(_estimate_tools_tokens_rough(tools))
    except Exception:
        return 0


def _session_usage_summary(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    fields = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "api_call_count",
    )
    totals = {
        field: sum(_safe_nonnegative_int(row.get(field)) for row in rows)
        for field in fields
    }
    totals["total_tokens"] = (
        totals["input_tokens"]
        + totals["output_tokens"]
        + totals["cache_read_tokens"]
        + totals["cache_write_tokens"]
    )
    return totals


def _legacy_log_paths(log_dir: Path) -> list[Path]:
    """Return agent logs oldest-first, including stdlib rotated backups."""

    paths = [path for path in log_dir.glob("agent.log*") if path.is_file()]

    def key(path: Path) -> tuple[int, int, str]:
        if path.name == "agent.log":
            return (1, 0, path.name)
        suffix = path.name.removeprefix("agent.log.")
        try:
            # Higher backup numbers are older.
            return (0, -int(suffix), path.name)
        except ValueError:
            return (0, 0, path.name)

    return sorted(paths, key=key)


def _best_sequential_log_chain(
    rows: list[dict[str, Any]], expected_calls: int
) -> list[dict[str, Any]]:
    """Select the longest monotonic API-call chain from interleaved logs.

    Background-review agents can share the parent session log tag while their
    own counters restart at one.  The main agent's durable counter is monotonic,
    so retaining the longest consecutive chain rejects those auxiliary runs.
    """

    best: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        sequence = _safe_nonnegative_int(row.get("request_sequence"))
        if sequence <= 0 or (expected_calls > 0 and sequence > expected_calls):
            continue
        previous = best.get(sequence - 1)
        candidate = [*previous, row] if previous else [row]
        if len(candidate) > len(best.get(sequence, [])):
            best[sequence] = candidate
    if expected_calls > 0 and expected_calls in best:
        return best[expected_calls]
    return max(best.values(), key=len, default=[])


def _attach_legacy_log_usage(
    conversations: list[dict[str, Any]],
    *,
    hermes_home: Path,
) -> None:
    """Merge exact request usage recoverable from redacted agent logs.

    This is a historical compatibility path only.  New calls are persisted in
    ``session_request_usage``.  Parsing is restricted to the numeric API-call
    INFO record; turn-context lines (which contain message previews) are never
    captured or serialized.
    """

    targets: dict[str, dict[str, Any]] = {}
    for conversation in conversations:
        for physical in conversation.pop("_physical_sessions", []):
            session_id = str(physical.get("id") or "")
            if not session_id:
                continue
            targets[session_id] = {
                "conversation": conversation,
                "expected_calls": _safe_nonnegative_int(
                    physical.get("api_call_count")
                ),
                "turns": list(physical.get("turn_boundaries") or []),
                "rows": [],
            }
    if not targets:
        return

    for path in _legacy_log_paths(hermes_home / "logs"):
        try:
            lines = path.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with lines:
            for line in lines:
                match = _LEGACY_API_USAGE_RE.match(line.rstrip("\r\n"))
                if match is None:
                    continue
                session_id = match.group("session_id")
                target = targets.get(session_id)
                if target is None:
                    continue
                try:
                    created_at = datetime.strptime(
                        match.group("timestamp"), "%Y-%m-%d %H:%M:%S,%f"
                    ).astimezone().timestamp()
                except ValueError:
                    continue
                prompt_tokens = _safe_nonnegative_int(match.group("prompt"))
                output_tokens = _safe_nonnegative_int(match.group("output"))
                cache_read_tokens = _safe_nonnegative_int(
                    match.group("cache_read")
                )
                turns = target["turns"]
                starts = [
                    _safe_nonnegative_float(turn.get("started_at"))
                    for turn in turns
                ]
                turn_index = bisect_right(starts, created_at) - 1
                turn_id = (
                    str(turns[turn_index].get("turn_id") or "")
                    if turn_index >= 0
                    else ""
                )
                sequence = _safe_nonnegative_int(match.group("sequence"))
                target["rows"].append(
                    {
                        "session_id": session_id,
                        "conversation_id": str(
                            target["conversation"].get("conversation_id") or ""
                        ),
                        "api_request_id": f"legacy-log:{session_id}:{sequence}",
                        "turn_id": turn_id,
                        "request_sequence": sequence,
                        "started_at": created_at,
                        "model": match.group("model"),
                        "billing_provider": match.group("provider"),
                        "source": "legacy_log",
                        "metrics": {
                            "input_tokens": max(
                                0, prompt_tokens - cache_read_tokens
                            ),
                            "output_tokens": output_tokens,
                            "cache_read_tokens": cache_read_tokens,
                            "cache_write_tokens": 0,
                            "prompt_tokens": prompt_tokens,
                            "total_tokens": _safe_nonnegative_int(
                                match.group("total")
                            ),
                            "api_duration_ms": (
                                _safe_nonnegative_float(match.group("latency"))
                                * 1000.0
                            ),
                        },
                    }
                )

    for target in targets.values():
        conversation = target["conversation"]
        chain = _best_sequential_log_chain(
            target["rows"], target["expected_calls"]
        )
        structured = list(conversation.get("requests") or [])
        merged: dict[tuple[str, int], dict[str, Any]] = {
            (
                str(event.get("session_id") or ""),
                _safe_nonnegative_int(event.get("request_sequence")),
            ): event
            for event in chain
        }
        # Structured SQLite evidence wins when the same call also remains in
        # agent.log.  This supports a session spanning the schema upgrade.
        for event in structured:
            merged[
                (
                    str(event.get("session_id") or ""),
                    _safe_nonnegative_int(event.get("request_sequence")),
                )
            ] = event
        conversation["requests"] = sorted(
            merged.values(),
            key=lambda event: (
                _safe_nonnegative_float(event.get("started_at")),
                str(event.get("api_request_id") or ""),
            ),
        )


def _finalize_usage_coverage(conversation: dict[str, Any]) -> None:
    aggregate = conversation.get("usage_aggregate")
    if not isinstance(aggregate, Mapping):
        aggregate = {}
    requests = [
        event
        for event in conversation.get("requests") or []
        if isinstance(event, Mapping)
    ]
    event_tokens = sum(
        _metric_value(_mapping(event.get("metrics")), "total_tokens")
        for event in requests
    )
    aggregate_tokens = _safe_nonnegative_int(aggregate.get("total_tokens"))
    aggregate_calls = _safe_nonnegative_int(aggregate.get("api_call_count"))
    conversation["request_usage_coverage"] = {
        "event_count": len(requests),
        "aggregate_api_call_count": aggregate_calls,
        "event_tokens": event_tokens,
        "aggregate_tokens": aggregate_tokens,
        "call_percent": (
            min(100.0, len(requests) / aggregate_calls * 100.0)
            if aggregate_calls > 0
            else (100.0 if not requests else 0.0)
        ),
        "token_percent": (
            min(100.0, event_tokens / aggregate_tokens * 100.0)
            if aggregate_tokens > 0
            else (100.0 if event_tokens <= 0 else 0.0)
        ),
        "complete": bool(
            len(requests) == aggregate_calls
            and round(event_tokens) == aggregate_tokens
        ),
    }


def _monitor_session_metadata(session_db: Any) -> list[dict[str, Any]]:
    """List every user-visible conversation without serializing its content.

    ``list_sessions_rich`` is the existing user-facing conversation projection:
    it hides implementation-only children, folds compression continuations into
    one logical run, and can include archived sessions.  The monitor retains
    only identity/title/activity metadata from each row; previews and all other
    SessionDB fields are deliberately discarded.
    """

    listing = getattr(session_db, "list_sessions_rich", None)
    if not callable(listing):
        return []

    resolver = getattr(session_db, "get_conversation_root", None)
    lineage_getter = getattr(session_db, "get_compression_lineage", None)
    session_getter = getattr(session_db, "get_session", None)
    request_getter = getattr(session_db, "request_usage_timeline", None)
    turn_getter = getattr(session_db, "content_free_turn_boundaries", None)
    page_size = 200
    offset = 0
    seen_session_ids: set[str] = set()
    seen_conversation_ids: set[str] = set()
    conversations: list[dict[str, Any]] = []
    while True:
        try:
            raw_page = listing(
                limit=page_size,
                offset=offset,
                include_archived=True,
                order_by_last_active=True,
                compact_rows=True,
            )
        except Exception:
            break
        if not isinstance(raw_page, list) or not raw_page:
            break

        added = 0
        for row in raw_page:
            if not isinstance(row, Mapping):
                continue
            session_id = str(row.get("id") or "").strip()
            if not session_id or session_id in seen_session_ids:
                continue
            seen_session_ids.add(session_id)
            added += 1

            conversation_id = str(row.get("_lineage_root_id") or "").strip()
            if not conversation_id:
                conversation_id = session_id
                if callable(resolver):
                    try:
                        conversation_id = str(
                            resolver(session_id) or session_id
                        ).strip()
                    except Exception:
                        conversation_id = session_id
            conversation_id = conversation_id or session_id
            if conversation_id in seen_conversation_ids:
                continue
            seen_conversation_ids.add(conversation_id)

            lineage = [session_id]
            if callable(lineage_getter):
                try:
                    loaded_lineage = lineage_getter(session_id)
                    if isinstance(loaded_lineage, list) and loaded_lineage:
                        lineage = [str(value) for value in loaded_lineage if value]
                except Exception:
                    lineage = [session_id]
            physical_sessions: list[dict[str, Any]] = []
            for physical_id in lineage:
                physical_row: Mapping[str, Any] | None = (
                    row if physical_id == session_id else None
                )
                if physical_row is None and callable(session_getter):
                    try:
                        loaded_row = session_getter(physical_id)
                        if isinstance(loaded_row, Mapping):
                            physical_row = loaded_row
                    except Exception:
                        physical_row = None
                if physical_row is None:
                    physical_row = {"id": physical_id}
                boundaries: list[dict[str, Any]] = []
                if callable(turn_getter):
                    try:
                        loaded_boundaries = turn_getter(physical_id)
                        if isinstance(loaded_boundaries, list):
                            boundaries = [
                                dict(item)
                                for item in loaded_boundaries
                                if isinstance(item, Mapping)
                            ]
                    except Exception:
                        boundaries = []
                physical_sessions.append(
                    {
                        "id": physical_id,
                        "started_at": physical_row.get("started_at", 0),
                        "ended_at": physical_row.get("ended_at", 0),
                        "input_tokens": physical_row.get("input_tokens", 0),
                        "output_tokens": physical_row.get("output_tokens", 0),
                        "cache_read_tokens": physical_row.get(
                            "cache_read_tokens", 0
                        ),
                        "cache_write_tokens": physical_row.get(
                            "cache_write_tokens", 0
                        ),
                        "reasoning_tokens": physical_row.get(
                            "reasoning_tokens", 0
                        ),
                        "api_call_count": physical_row.get("api_call_count", 0),
                        "turn_boundaries": boundaries,
                    }
                )
            auxiliary_usage = _auxiliary_usage_for(
                session_db,
                [str(item.get("id") or "") for item in physical_sessions],
            )
            requests: list[dict[str, Any]] = []
            if callable(request_getter):
                try:
                    loaded_requests = request_getter(conversation_id)
                    if isinstance(loaded_requests, list):
                        requests = [
                            dict(item)
                            for item in loaded_requests
                            if isinstance(item, Mapping)
                        ]
                except Exception:
                    requests = []
            conversations.append(
                {
                    "conversation_id": conversation_id,
                    "session_id": session_id,
                    "title": str(row.get("title") or "").strip(),
                    "source": "session_db",
                    "started_at": row.get("started_at", 0),
                    "last_activity_at": row.get("last_active", 0),
                    "projections": [],
                    "cache_requests": [],
                    "requests": requests,
                    "usage_aggregate": _session_usage_summary(
                        physical_sessions
                    ),
                    "auxiliary_usage": auxiliary_usage,
                    "_physical_sessions": physical_sessions,
                }
            )

        if len(raw_page) < page_size or added == 0:
            break
        offset += len(raw_page)
    try:
        from hermes_constants import get_hermes_home

        _attach_legacy_log_usage(conversations, hermes_home=get_hermes_home())
    except Exception:
        # Log recovery is compatibility-only. Structured request events and
        # aggregate SessionDB totals remain available if the log is absent or
        # unreadable.
        for conversation in conversations:
            conversation.pop("_physical_sessions", None)
    for conversation in conversations:
        _finalize_usage_coverage(conversation)
    return conversations


def persisted_context_engine_telemetry(
    session_db: Any,
    session_id: str,
    *,
    include_all_sessions: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Load content-free V1 telemetry from the profile store.

    ``HermesCLI`` restores ``session_id`` and transcript history eagerly but
    builds the agent only on the first model turn. Read-only monitoring must not
    force that expensive startup merely to query metrics already owned by the
    profile's Object Context store. The all-session form is used only by the
    monitor and remains a numeric/identity-only snapshot.
    """

    current_session_id = str(session_id or "").strip()
    if not current_session_id and not include_all_sessions:
        return None

    conversation_id = current_session_id
    resolver = getattr(session_db, "get_conversation_root", None)
    if current_session_id and callable(resolver):
        try:
            conversation_id = str(resolver(current_session_id) or current_session_id)
        except Exception:
            conversation_id = current_session_id

    try:
        from hermes_constants import get_hermes_home
        from plugins.context_engine.object_context.store import ObjectContextStore

        store_path = get_hermes_home() / "context" / "object_context_v1.sqlite3"
        store = ObjectContextStore(store_path) if store_path.is_file() else None
        projections = (
            store.request_projection_timeline(conversation_id) if store else []
        )
        cache_requests = store.cache_usage_timeline(conversation_id) if store else []
        economic_decisions = (
            store.projection_decisions(conversation_id) if store else []
        )
        request_observations = (
            store.request_observation_timeline(conversation_id) if store else []
        )
        if (
            not projections
            and not economic_decisions
            and not request_observations
            and not include_all_sessions
        ):
            return None
        status = (
            store.aggregate_status(conversation_id)
            if store
            else {
                "request_projection_count": 0,
                "request_metric_totals": {},
                "request_metric_averages": {},
                "last_request_metrics": {},
            }
        )
        session_timelines: list[dict[str, Any]] = []
        if include_all_sessions:
            session_timelines = _monitor_session_metadata(session_db)
            session_by_conversation = {
                str(item.get("conversation_id") or ""): item
                for item in session_timelines
                if str(item.get("conversation_id") or "")
            }
            if conversation_id and conversation_id not in session_by_conversation:
                current_item = {
                    "conversation_id": conversation_id,
                    "session_id": current_session_id or conversation_id,
                    "title": _stored_session_title(
                        session_db,
                        conversation_id,
                        projections,
                        current_session_id=current_session_id,
                    ),
                    "source": "session_db",
                    "started_at": 0,
                    "last_activity_at": 0,
                    "projections": [],
                    "cache_requests": [],
                    "economic_decisions": [],
                    "request_observations": [],
                }
                session_timelines.insert(0, current_item)
                session_by_conversation[conversation_id] = current_item

            summary_by_conversation = {
                str(summary.get("conversation_id") or ""): dict(summary)
                for summary in (
                    store.request_projection_conversations() if store else []
                )
                if str(summary.get("conversation_id") or "")
            }
            for decision_summary in (
                store.projection_decision_conversations() if store else []
            ):
                decision_conversation_id = str(
                    decision_summary.get("conversation_id") or ""
                )
                if not decision_conversation_id:
                    continue
                summary_by_conversation.setdefault(
                    decision_conversation_id,
                    {"conversation_id": decision_conversation_id},
                ).update(decision_summary)
            summaries = list(summary_by_conversation.values())
            for summary in summaries:
                stored_conversation_id = str(summary.get("conversation_id") or "")
                stored_projections = store.request_projection_timeline(
                    stored_conversation_id
                )
                stored_cache_requests = store.cache_usage_timeline(
                    stored_conversation_id
                )
                stored_economic_decisions = store.projection_decisions(
                    stored_conversation_id
                )
                stored_request_observations = (
                    store.request_observation_timeline(stored_conversation_id)
                )
                title = _stored_session_title(
                    session_db,
                    stored_conversation_id,
                    stored_projections,
                    current_session_id=(
                        current_session_id
                        if stored_conversation_id == conversation_id
                        else ""
                    ),
                )
                stored_item = session_by_conversation.get(stored_conversation_id)
                if stored_item is None:
                    stored_item = {
                        "conversation_id": stored_conversation_id,
                        "session_id": stored_conversation_id,
                        "title": "",
                        "source": "persisted",
                        "started_at": 0,
                        "last_activity_at": summary.get("last_projection_at", 0),
                        "projections": [],
                        "cache_requests": [],
                        "economic_decisions": [],
                        "request_observations": [],
                    }
                    session_timelines.append(stored_item)
                    session_by_conversation[stored_conversation_id] = stored_item
                if not str(stored_item.get("title") or "").strip():
                    stored_item["title"] = title
                stored_item.update(
                    {
                        "first_projection_at": summary.get(
                            "first_projection_at", 0
                        ),
                        "last_projection_at": summary.get(
                            "last_projection_at", 0
                        ),
                        "projections": stored_projections,
                        "cache_requests": stored_cache_requests,
                        "economic_decisions": stored_economic_decisions,
                        "request_observations": stored_request_observations,
                        "retrieve_object_schema_tokens_per_request": (
                            _retrieve_object_schema_tokens_rough()
                        ),
                    }
                )
                stored_status = store.aggregate_status(stored_conversation_id)
                stored_metrics = dict(
                    _mapping(stored_status.get("metric_totals"))
                )
                stored_metrics["retrieval_count"] = _safe_nonnegative_int(
                    stored_status.get("retrieval_count")
                )
                _scope_monitor_retrieval_metrics(
                    store,
                    stored_conversation_id,
                    stored_item,
                    stored_metrics,
                )
                stored_item["object_context_metrics"] = stored_metrics
                stored_item.setdefault(
                    "auxiliary_usage",
                    _auxiliary_usage_for(
                        session_db,
                        [
                            str(
                                stored_item.get("session_id")
                                or stored_conversation_id
                            )
                        ],
                    ),
                )
            if not session_timelines:
                return None
    except Exception:
        return None

    status.update(
        {
            "object_context_version": (
                "1.2"
                if any(
                    decision.get("policy_version") == "1.2"
                    for decision in economic_decisions
                )
                else "1.1"
            ),
            "object_context_available": store is not None,
        }
    )
    timeline: dict[str, Any] = {
        "schema_version": 4,
        "conversation_id": conversation_id,
        "session_id": current_session_id,
        "title": _stored_session_title(
            session_db,
            conversation_id,
            projections,
            current_session_id=current_session_id,
        ),
        "source": "persisted",
        "projections": projections,
        "cache_requests": cache_requests,
        "economic_decisions": economic_decisions,
        "request_observations": request_observations,
        "object_context_metrics": {
            **dict(_mapping(status.get("metric_totals"))),
            "retrieval_count": _safe_nonnegative_int(
                status.get("retrieval_count")
            ),
        },
        "retrieve_object_schema_tokens_per_request": (
            _retrieve_object_schema_tokens_rough()
        ),
        "auxiliary_usage": _auxiliary_usage_for(
            session_db,
            _compression_lineage(session_db, current_session_id),
        ),
    }
    if include_all_sessions:
        active_item = next(
            (
                item
                for item in session_timelines
                if str(item.get("conversation_id") or "") == conversation_id
            ),
            None,
        )
        if active_item is not None:
            timeline["title"] = str(active_item.get("title") or timeline["title"])
        timeline.update(
            {
                "active_conversation_id": conversation_id,
                "sessions": session_timelines,
            }
        )
    return status, timeline
