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
from dataclasses import dataclass
from typing import Any, Mapping


OBJECT_CONTEXT_ENGINE = "object_context"
BUILTIN_ENGINE = "compressor"


@dataclass(frozen=True)
class ParameterSpec:
    """One supported Object Context V1 configuration value."""

    default: int | float
    value_type: type[int] | type[float]
    minimum: int | float
    maximum: int | float | None
    description: str

    @property
    def range_label(self) -> str:
        if self.maximum is None:
            return f">= {self.minimum}"
        return f"{self.minimum} ≤ value ≤ {self.maximum}"


# Keep this table aligned with ObjectContextEngine's constructor and
# DEFAULT_CONFIG["context"]["object_context"].  It is intentionally explicit:
# accepting arbitrary keys would make typos look successful while the engine
# silently ignores them.
PARAMETER_SPECS: dict[str, ParameterSpec] = {
    "hot_tail_max_deltas": ParameterSpec(
        8,
        int,
        1,
        None,
        "maximum recent Deltas retained raw",
    ),
    "hot_tail_token_budget_ratio": ParameterSpec(
        0.25,
        float,
        0.01,
        1.0,
        "fraction of model context available to the raw Hot Tail",
    ),
    "context_soft_limit_ratio": ParameterSpec(
        0.75,
        float,
        0.10,
        1.0,
        "prompt pressure point that can cool older Deltas early",
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


def _format_value(value: int | float) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


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


def parse_parameter_value(name: str, raw_value: str) -> int | float:
    """Parse one parameter strictly and enforce the engine's real range."""

    spec = PARAMETER_SPECS.get(name)
    if spec is None:
        raise ObjectContextCommandError(
            f"Unknown V1 parameter: {name}. Use /object_context help to list valid names."
        )

    text = str(raw_value or "").strip()
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
    _assert_paths_writable("context.engine")
    raw = _raw_config_for_write()
    context = _context_mapping(raw, create=True)
    current = str(context.get("engine") or BUILTIN_ENGINE)
    if current == engine_name:
        return False
    context["engine"] = engine_name
    _save_raw_config(raw)
    return True


def _set_parameter(name: str, value: int | float) -> bool:
    path = f"context.object_context.{name}"
    _assert_paths_writable(path)
    raw = _raw_config_for_write()
    context = _context_mapping(raw, create=True)
    object_cfg = _object_mapping(context, create=True)
    if object_cfg.get(name) == value and type(object_cfg.get(name)) is type(value):
        return False
    object_cfg[name] = value
    _save_raw_config(raw)
    return True


def _reset_parameters(name: str) -> tuple[bool, tuple[str, ...]]:
    if name not in PARAMETER_SPECS and name not in LEGACY_V0_KEYS and name != "all":
        raise ObjectContextCommandError(
            f"Unknown V1 parameter: {name}. Use /object_context help to list valid names."
        )

    targets = (
        set(PARAMETER_SPECS) | set(LEGACY_V0_KEYS)
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
        return ["", "Live V1 savings: unavailable until an agent is active."]
    if engine_status.get("object_context_available") is False:
        return ["", "Live V1 savings: V1 storage is unavailable for this session."]

    latest = _mapping(engine_status.get("last_request_metrics"))
    totals = _mapping(engine_status.get("request_metric_totals"))
    try:
        count = max(0, int(engine_status.get("request_projection_count", 0) or 0))
    except (TypeError, ValueError):
        count = 0
    if count == 0:
        return [
            "",
            "Live V1 savings: no request projection has been recorded yet.",
            "Details: /object_context stats",
        ]

    last_raw = _metric_value(latest, "raw_context_tokens")
    last_saved = _metric_value(latest, "tokens_saved")
    total_raw = _metric_value(totals, "raw_context_tokens")
    total_saved = _metric_value(totals, "tokens_saved")
    request_word = "request" if count == 1 else "requests"
    return [
        "",
        "Live V1 savings (estimated conversation-view tokens):",
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
    lines = ["Object Context V1 Token Savings", "─" * 72]
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

    last_raw = _metric_value(latest, "raw_context_tokens")
    last_rendered = _metric_value(latest, "rendered_context_tokens")
    last_saved = _metric_value(latest, "tokens_saved")
    last_hot_tail = _metric_value(latest, "hot_tail_tokens")
    total_raw = _metric_value(totals, "raw_context_tokens")
    total_rendered = _metric_value(totals, "rendered_context_tokens")
    total_saved = _metric_value(totals, "tokens_saved")
    retrieved_tokens = _metric_value(all_totals, "retrieved_tokens")
    try:
        retrieval_count = max(0, int(engine_status.get("retrieval_count", 0) or 0))
        working_objects = max(
            0, int(engine_status.get("working_memory_object_count", 0) or 0)
        )
    except (TypeError, ValueError):
        retrieval_count = 0
        working_objects = 0

    lines.extend(
        [
            "Scope: active conversation",
            "",
            "Last model-request projection:",
            f"  Raw conversation view             {_format_tokens(last_raw):>14}",
            f"  Rendered V1 conversation view     {_format_tokens(last_rendered):>14}",
            f"  Tokens avoided                    {_format_tokens(last_saved):>14}",
            f"  Reduction                         {_saving_percent(last_raw, last_saved):>13.1f}%",
            f"  Hot Tail                          {_format_tokens(last_hot_tail):>14}",
            "",
            "Cumulative request projections:",
            f"  Projected requests                {count:>14,}",
            f"  Raw conversation-view tokens      {_format_tokens(total_raw):>14}",
            f"  Rendered V1-view tokens           {_format_tokens(total_rendered):>14}",
            f"  Tokens avoided                    {_format_tokens(total_saved):>14}",
            f"  Average avoided / request         {_format_tokens(total_saved / count):>14}",
            f"  Reduction                         {_saving_percent(total_raw, total_saved):>13.1f}%",
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
            "Savings are rough-token estimates for conversation messages only;",
            "provider tokenizer, system prompt, tool schemas, caching, and retries differ.",
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
    lines = ["Object Context V1 Session Dynamics Monitor", "─" * 72]
    session_timelines = (
        monitor_timeline.get("sessions")
        if isinstance(monitor_timeline, Mapping)
        else None
    )
    has_persisted_sessions = bool(
        isinstance(session_timelines, list)
        and any(
            isinstance(item, Mapping)
            and isinstance(item.get("projections"), list)
            and item.get("projections")
            for item in session_timelines
        )
    )
    if active_engine != OBJECT_CONTEXT_ENGINE and not has_persisted_sessions:
        lines.extend(
            [
                f"Unavailable: the active session is {_active_label(active_engine)}.",
                "Use /object_context on, restart the CLI, and send a message first.",
            ]
        )
        return ObjectContextCommandResult(tuple(lines))
    if not isinstance(engine_status, Mapping) and not has_persisted_sessions:
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
        and not has_persisted_sessions
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
        and not has_persisted_sessions
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
                f"Projects: {int(global_totals.get('project_count', 0)):,} total"
                f" · {int(selected.get('project_count', 0)):,} selected"
            ),
            f"Turns:    {int(selected.get('turn_count', 0)):,} selected",
            "Dashboard: " + str(path),
            "Opening the private local HTML dashboard in your browser…",
            "Run /object_context monitor again to refresh the snapshot.",
        ]
    )
    project_count = int(selected.get("project_count", 0) or 0)
    turnless = int(selected.get("turnless_project_count", 0) or 0)
    timed = int(selected.get("timed_project_count", 0) or 0)
    legacy = int(selected.get("legacy_project_count", 0) or 0)
    if turnless or timed < project_count:
        lines.append(
            f"Coverage: {project_count - turnless:,}/{project_count:,} projects "
            f"have turn identity; {timed:,}/{project_count:,} have timing."
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
    configured_on = configured_engine == OBJECT_CONTEXT_ENGINE

    lines = [
        "Object Context V1",
        "─" * 72,
        f"Configured: {'ON' if configured_on else 'OFF'} ({configured_engine})",
        f"Active session: {_active_label(active_engine)}",
        f"Config: {display_hermes_home()}/config.yaml",
    ]
    if active_engine and active_engine != configured_engine:
        lines.append("Restart pending: configured and active engines differ.")
    lines.extend(_savings_summary_lines(active_engine, engine_status))
    lines.extend(["", "Parameters:"])
    for name, spec in PARAMETER_SPECS.items():
        value = effective_object.get(name, spec.default)
        source = "explicit" if name in raw_object else "default"
        lines.append(
            f"  {name:<36} {_format_value(value):>8}  [{source}]"
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
        "Object Context V1 command",
        "─" * 72,
        "  /object_context                 show status and effective values",
        "  /object_context stats           show live V1 token savings",
        "  /object_context monitor         open the session dynamics webpage",
        "  /object_context on              select object_context for next launch",
        "  /object_context off             select the built-in compressor",
        "  /object_context set KEY VALUE   persist one validated V1 override",
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
            "  /object_context set hot_tail_max_deltas 4",
            "  /object_context set hot_tail_token_budget_ratio 0.15",
            "  /object_context reset hot_tail_max_deltas",
            "",
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
                f"Object Context V1 is {state} in config.yaml.",
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
                f"Object Context V1 is {state} in config.yaml.",
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
                "This does not toggle V1. Use /object_context on if needed.",
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
    return dict(timeline) if isinstance(timeline, Mapping) else None


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
        if not store_path.is_file():
            return None
        store = ObjectContextStore(store_path)
        projections = store.request_projection_timeline(conversation_id)
        if not projections and not include_all_sessions:
            return None
        status = store.aggregate_status(conversation_id)
        session_timelines: list[dict[str, Any]] = []
        if include_all_sessions:
            for summary in store.request_projection_conversations():
                stored_conversation_id = str(summary.get("conversation_id") or "")
                stored_projections = store.request_projection_timeline(
                    stored_conversation_id
                )
                if not stored_projections:
                    continue
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
                session_timelines.append(
                    {
                        "conversation_id": stored_conversation_id,
                        "session_id": stored_conversation_id,
                        "title": title,
                        "source": "persisted",
                        "first_projection_at": summary.get("first_projection_at", 0),
                        "last_projection_at": summary.get("last_projection_at", 0),
                        "projections": stored_projections,
                    }
                )
            if not session_timelines:
                return None
    except Exception:
        return None

    status.update(
        {
            "object_context_version": 1,
            "object_context_available": True,
        }
    )
    timeline: dict[str, Any] = {
        "schema_version": 2 if include_all_sessions else 1,
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
    }
    if include_all_sessions:
        timeline.update(
            {
                "active_conversation_id": conversation_id,
                "sessions": session_timelines,
            }
        )
    return status, timeline
