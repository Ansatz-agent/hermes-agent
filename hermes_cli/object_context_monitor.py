"""Self-contained all-session dashboard for Object Context dynamics."""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections import OrderedDict
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Mapping, Sequence

from hermes_constants import get_hermes_home
from utils import atomic_write_text


MONITOR_SCHEMA_VERSION = 7
MONITOR_DIRNAME = "object-context-monitor"


def _finite_nonnegative(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return max(0.0, number)


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


def _sequence_label(event: Mapping[str, Any], fallback: int) -> str:
    try:
        sequence = int(event.get("projection_sequence") or fallback)
    except (TypeError, ValueError):
        sequence = fallback
    return f"P{max(1, sequence)}"


def build_monitor_payload(timeline: Mapping[str, Any]) -> dict[str, Any]:
    """Build one session's content-free chart payload."""

    raw_events = timeline.get("projections")
    events = [event for event in raw_events or [] if isinstance(event, Mapping)]
    raw_requests = timeline.get("requests")
    requests = [
        event for event in raw_requests or [] if isinstance(event, Mapping)
    ]
    legacy_count = sum(1 for event in events if bool(event.get("legacy")))
    turnless_count = sum(
        1
        for event in events
        if bool(event.get("legacy")) or not str(event.get("turn_id") or "")
    )
    request_turnless_count = sum(
        1 for event in requests if not str(event.get("turn_id") or "")
    )

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
    # With Object Context off there are no projections.  Savings are still a
    # real zero-valued series, aligned to universal model requests.
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
    ) -> dict[str, Any]:
        payload = {
            "key": key,
            "title": title,
            "axis": axis,
            "unit": unit,
            "labels": list(labels),
            "ids": list(ids or labels),
            "values": [round(_finite_nonnegative(value), 6) for value in values],
        }
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
            "description": "绝对值 raw − rendered · 比例 saved / raw",
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
            "description": "provider API request latency · 与 Object Context 开关无关",
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
            "description": "provider-reported prompt + output tokens · 与 Object Context 开关无关",
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
    last_activity_at = _finite_nonnegative(
        timeline.get("last_activity_at") or timeline.get("last_active")
    )
    if last_activity_at <= 0:
        last_activity_at = max(
            last_projection_at,
            max(request_timestamps, default=0.0),
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
        "object_context_used": bool(events),
        "has_projection_telemetry": bool(events),
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
            "tokens_saved": round(sum(savings_project_values), 6),
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

    global_totals = {
        "project_count": sum(int(item["project_count"]) for item in sessions),
        "request_count": sum(int(item["request_count"]) for item in sessions),
        "request_event_count": sum(
            int(item["request_event_count"]) for item in sessions
        ),
        "turn_count": sum(int(item["turn_count"]) for item in sessions),
        "cache_request_count": sum(
            int(item["cache_request_count"]) for item in sessions
        ),
        "timed_request_count": sum(
            int(item["timed_request_count"]) for item in sessions
        ),
        "legacy_project_count": sum(int(item["legacy_project_count"]) for item in sessions),
        "tokens_saved": total("tokens_saved"),
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
    return {
        "schema_version": MONITOR_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "active_session_id": str(timeline.get("session_id") or ""),
        "active_conversation_id": active_conversation_id,
        "selected_conversation_id": selected_conversation_id,
        "session_count": len(sessions),
        "global_totals": global_totals,
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
    .table-wrap { overflow:auto; } table { border-collapse:collapse; width:100%; min-width:960px; }
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
      <div class="table-wrap"><table><thead><tr><th>Run</th><th>Context</th><th>Requests</th><th>Turns</th><th>Saved</th><th>Cache Hit</th><th>API Time</th><th>Tokens</th><th>Series</th><th>Updated</th></tr></thead><tbody id="run-body"></tbody></table></div>
    </section>
    <div id="workspace" class="workspace"></div>
    <footer>本地离线快照 · 不含 prompt / 消息内容 · <code>/oc monitor</code> 刷新</footer>
  </main>
</div>
<script>
const DATA=__DATA__;
const NS="http://www.w3.org/2000/svg";
const byId=id=>document.getElementById(id);
const TOKEN_UNITS=[{threshold:1e9,label:"B"},{threshold:1e6,label:"M"},{threshold:1e3,label:"K"}];
const fmtToken=value=>{const n=Math.max(0,Number(value||0));const unit=TOKEN_UNITS.find(item=>n>=item.threshold);if(unit){const scaled=n/unit.threshold;return `${new Intl.NumberFormat("en-US",{maximumSignificantDigits:3}).format(scaled)}${unit.label} tok`;}return new Intl.NumberFormat("en-US",{maximumFractionDigits:n<10?2:n<100?1:0}).format(n)+" tok";};
const fmt=(value,unit)=>{const n=Number(value||0);if(unit==="percent")return new Intl.NumberFormat("zh-CN",{minimumFractionDigits:n>0&&n<1?2:0,maximumFractionDigits:2}).format(n)+"%";if(unit==="ms"){if(n>=36e5)return `${(n/36e5).toFixed(2)} h`;if(n>=6e4)return `${(n/6e4).toFixed(2)} min`;return n>=1000?`${(n/1000).toFixed(2)} s`:`${n.toFixed(n<10?2:1)} ms`;}return fmtToken(n);};
const fmtCache=(percent,count)=>Number(count||0)>0?fmt(percent,"percent"):"—";
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
function sessionDownload(session){const rows=[];for(const group of session.groups)for(const chart of group.charts){if(chart.modes)for(const modeKey of Object.keys(chart.modes))rows.push(...chartRows(session,group,resolveChartMode(chart,modeKey)));else rows.push(...chartRows(session,group,resolveChartMode(chart,"default")));}return csvDownload(session,"all_charts",rows);}
function renderChart(session,group,chart){
  const card=document.createElement("article");card.className="chart";const head=document.createElement("div");head.className="chart-head";const title=document.createElement("div");title.className="chart-title";title.textContent=chart.title;const tools=document.createElement("div");tools.className="chart-tools";
  const summary=document.createElement("div");summary.className="chart-summary";const latest=chart.values.length?chart.values[chart.values.length-1]:0;summary.textContent=`${fmt(latest,chart.unit)} · ${chart.values.length} pts`;
  const downloadData=chartDownload(session,group,chart);const download=document.createElement("a");download.className="download";download.textContent="CSV ↓";download.title=`下载「${chart.title}」完整数据`;download.setAttribute("aria-label",download.title);download.href=downloadData.href;download.download=downloadData.filename;tools.append(summary,download);head.append(title,tools);card.append(head);
  if(!chart.values.length){const empty=document.createElement("div");empty.className="empty";empty.textContent="暂无数据";card.append(empty);return card;}
  const W=760,H=275,M={l:72,r:18,t:16,b:43},PW=W-M.l-M.r,PH=H-M.t-M.b;const values=chart.values.map(Number);const maxValue=Math.max(...values,0);const yMax=chart.unit==="percent"?100:(maxValue>0?maxValue:1);const sx=index=>M.l+(values.length===1?PW/2:index*PW/(values.length-1));const sy=value=>M.t+PH-(Number(value)/yMax)*PH;const svg=node("svg",{viewBox:`0 0 ${W} ${H}`,role:"img","aria-label":chart.title});
  for(let tick=0;tick<=4;tick++){const y=M.t+tick*PH/4;svg.append(node("line",{x1:M.l,y1:y,x2:W-M.r,y2:y,class:"grid"}));const label=node("text",{x:M.l-9,y:y+4,"text-anchor":"end",class:"axis-text"});label.textContent=fmt(yMax*(1-tick/4),chart.unit);svg.append(label);}
  const points=values.map((value,index)=>`${sx(index)},${sy(value)}`);svg.append(node("polyline",{points:points.join(" "),class:"series",stroke:group.color}));for(const point of sample(chart)){const circle=node("circle",{cx:sx(point.index),cy:sy(point.value),r:3.5,class:"point",fill:group.color});const tip=node("title");const identity=chart.ids[point.index]||chart.labels[point.index]||String(point.index+1);tip.textContent=`${chart.labels[point.index]||point.index+1} · ${identity}\n${fmt(point.value,chart.unit)}`;circle.append(tip);svg.append(circle);}
  const first=node("text",{x:M.l,y:H-24,"text-anchor":"start",class:"axis-text"});first.textContent=chart.labels[0]||"1";svg.append(first);const last=node("text",{x:W-M.r,y:H-24,"text-anchor":"end",class:"axis-text"});last.textContent=chart.labels[chart.labels.length-1]||String(values.length);svg.append(last);const axis=node("text",{x:M.l+PW/2,y:H-6,"text-anchor":"middle",class:"axis-text"});axis.textContent=chart.axis;svg.append(axis);card.append(svg);return card;
}
let selectedId=DATA.selected_conversation_id;const groupDisplayModes={saved:"absolute",cache:"relative"};const selectedSession=()=>DATA.sessions.find(session=>session.conversation_id===selectedId)||DATA.sessions[0];
const activeChart=(group,chart)=>resolveChartMode(chart,groupDisplayModes[group.key]||group.default_display_mode||"default");
function renderGlobal(){const totals=DATA.global_totals;const host=byId("global-kpis");host.replaceChildren(kpi("Sessions",String(DATA.session_count)),kpi("Requests",new Intl.NumberFormat("en-US").format(totals.request_count)),kpi("Saved",fmt(totals.tokens_saved,"tokens")),kpi("Cache Hit",fmtCache(totals.cache_hit_percent,totals.request_count)),kpi("API Time",fmt(totals.api_duration_ms,"ms")),kpi("Provider Tokens",fmt(totals.provider_tokens,"tokens")));byId("snapshot-count").textContent=`${DATA.session_count} sessions · ${totals.request_count} model requests · ${totals.project_count} OC projections`;byId("generated").textContent=`Updated ${SHORT_DATE.format(new Date(DATA.generated_at))}`;}
function runMatches(session,query){return !query||String(session.title||"").toLowerCase().includes(query)||session.conversation_id.toLowerCase().includes(query)||String(session.session_id||"").toLowerCase().includes(query);}
function renderRuns(query=""){
  const normalized=query.trim().toLowerCase();const sessions=DATA.sessions.filter(session=>runMatches(session,normalized));const side=byId("side-runs");const body=byId("run-body");side.replaceChildren();body.replaceChildren();
  const sideGroups=[
    {label:"Object Context",sessions:sessions.filter(session=>session.object_context_used)},
    {label:"No Object Context",sessions:sessions.filter(session=>!session.object_context_used)},
  ];
  for(const group of sideGroups){if(!group.sessions.length)continue;const section=document.createElement("section");section.className="side-group";const heading=document.createElement("div");heading.className="side-group-head";const label=document.createElement("span");label.textContent=group.label;const count=document.createElement("span");count.className="side-group-count";count.textContent=String(group.sessions.length);heading.append(label,count);const list=document.createElement("div");list.className="side-group-list";for(const session of group.sessions){const button=document.createElement("button");button.className="side-run"+(session.conversation_id===selectedId?" selected":"");button.type="button";const titleRow=document.createElement("span");titleRow.className="side-run-title-row";const title=document.createElement("span");title.className="side-run-title";title.textContent=session.title;titleRow.append(title,contextTag(session));const id=document.createElement("span");id.className="side-run-id";id.textContent=session.conversation_id;const meta=document.createElement("span");meta.className="side-run-meta";meta.textContent=`${session.request_count} R · ${fmt(session.totals.provider_tokens,"tokens")}`;button.append(titleRow,id,meta);button.addEventListener("click",()=>selectSession(session.conversation_id));list.append(button);}section.append(heading,list);side.append(section);}
  for(const session of sessions){const row=document.createElement("tr");if(session.conversation_id===selectedId)row.className="selected";const identity=document.createElement("td");const runTitle=document.createElement("span");runTitle.className="run-title";runTitle.textContent=session.title;if(session.is_active){const badge=document.createElement("span");badge.className="badge";badge.textContent="current";runTitle.append(badge);}const runId=document.createElement("span");runId.className="run-id";runId.textContent=shortId(session.conversation_id);runId.title=session.conversation_id;identity.append(runTitle,runId);const contextCell=document.createElement("td");contextCell.append(contextTag(session));const coverage=Math.max(0,Math.min(1,Number(session.request_usage_coverage.call_percent||0)/100));const cells=[identity,contextCell,session.request_count,session.turn_count,fmt(session.totals.tokens_saved,"tokens"),fmtCache(session.totals.cache_hit_percent,session.request_count),fmt(session.totals.api_duration_ms,"ms"),fmt(session.totals.provider_tokens,"tokens")];for(const value of cells){if(value instanceof Node)row.append(value);else{const cell=document.createElement("td");cell.textContent=String(value);row.append(cell);}}const coverageCell=document.createElement("td");const bar=document.createElement("span");bar.className="coverage";const fill=document.createElement("i");fill.style.width=`${Math.round(coverage*100)}%`;bar.append(fill);coverageCell.append(bar,`${Math.round(coverage*100)}%`);coverageCell.title="已恢复逐请求曲线 / 聚合请求总数";row.append(coverageCell);const last=document.createElement("td");last.textContent=dateFmt(session.last_activity_at);row.append(last);row.addEventListener("click",()=>selectSession(session.conversation_id));body.append(row);}
  if(!sessions.length){const row=document.createElement("tr");const cell=document.createElement("td");cell.colSpan=10;cell.textContent="没有匹配的 session";cell.style.textAlign="center";cell.style.color="var(--muted)";row.append(cell);body.append(row);}
}
function renderWorkspace(){
  const session=selectedSession();const host=byId("workspace");host.replaceChildren();if(!session)return;const head=document.createElement("div");head.className="session-head";const left=document.createElement("div");const title=document.createElement("h2");title.textContent=session.title;title.append(contextTag(session));if(session.is_active){const badge=document.createElement("span");badge.className="badge";badge.textContent="current";title.append(badge);}const identity=document.createElement("div");identity.className="session-id";identity.textContent=session.conversation_id;left.append(title,identity);const actions=document.createElement("div");actions.className="session-actions";const allData=sessionDownload(session);const downloadAll=document.createElement("a");downloadAll.className="download download-all";downloadAll.textContent=`全部 CSV · ${allData.rowCount}`;downloadAll.title=`下载「${session.title}」全部图表的 CSV 数据（含可切换模式）`;downloadAll.setAttribute("aria-label",downloadAll.title);downloadAll.href=allData.href;downloadAll.download=allData.filename;const time=document.createElement("div");time.className="session-time";time.textContent=session.has_projection_telemetry?`${session.project_count} OC projections · ${dateFmt(session.first_projection_at)} → ${dateFmt(session.last_projection_at)}`:"OC 未启用 · 节省量为 0";actions.append(downloadAll,time);head.append(left,actions);host.append(head);
  const metrics=document.createElement("div");metrics.className="kpis session-kpis";metrics.append(kpi("Requests",String(session.request_count)),kpi("Turns",String(session.turn_count)),kpi("Saved",fmt(session.totals.tokens_saved,"tokens")),kpi("Cache Hit",fmtCache(session.totals.cache_hit_percent,session.request_count)),kpi("API Time",fmt(session.totals.api_duration_ms,"ms")),kpi("Provider Tokens",fmt(session.totals.provider_tokens,"tokens")));host.append(metrics);
  for(const group of session.groups){const section=document.createElement("section");section.className="metric-group";const heading=document.createElement("div");heading.className="group-heading";const title=document.createElement("h3");title.className="group-title";title.style.color=group.color;const dot=document.createElement("span");dot.className="group-dot";const label=document.createElement("span");label.textContent=group.title;title.append(dot,label);heading.append(title);if(Array.isArray(group.display_modes)){const toggle=document.createElement("div");toggle.className="metric-toggle";toggle.setAttribute("role","group");toggle.setAttribute("aria-label",`${group.title} 显示方式`);const selectedMode=groupDisplayModes[group.key]||group.default_display_mode;for(const mode of group.display_modes){const button=document.createElement("button");button.type="button";button.className="metric-mode";button.textContent=mode.label;button.setAttribute("aria-pressed",String(selectedMode===mode.key));button.addEventListener("click",()=>{if(groupDisplayModes[group.key]===mode.key)return;groupDisplayModes[group.key]=mode.key;renderWorkspace();});toggle.append(button);}heading.append(toggle);}const description=document.createElement("div");description.className="group-description";description.textContent=group.description;const charts=document.createElement("div");charts.className="charts";for(const chart of group.charts)charts.append(renderChart(session,group,activeChart(group,chart)));section.append(heading,description,charts);host.append(section);}
  if(!session.request_usage_coverage.complete||session.request_turnless_count||session.turnless_project_count){const coverage=document.createElement("div");coverage.className="coverage-note";const label=document.createElement("strong");label.textContent="Coverage";coverage.append(label);const chip=value=>{const item=document.createElement("span");item.className="coverage-chip";item.textContent=value;coverage.append(item);};chip(`Request series ${session.request_event_count}/${session.request_count}`);chip(`Token series ${fmt(session.request_usage_coverage.event_tokens,"tokens")} / ${fmt(session.totals.provider_tokens,"tokens")}`);if(session.request_turnless_count)chip(`Request turn ID ${session.request_count-session.request_turnless_count}/${session.request_count}`);if(session.project_count&&session.turnless_project_count)chip(`OC turn ID ${session.project_count-session.turnless_project_count}/${session.project_count}`);if(session.legacy_project_count)chip(`Legacy OC ${session.legacy_project_count}`);host.append(coverage);}
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
    "build_monitor_dashboard_payload",
    "build_monitor_payload",
    "monitor_directory",
    "render_monitor_html",
    "write_monitor_html",
]
