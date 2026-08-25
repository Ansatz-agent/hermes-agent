from __future__ import annotations

import json
import os
import re
import stat

import pytest

from hermes_cli.object_context_monitor import (
    build_monitor_dashboard_payload,
    build_monitor_payload,
    render_monitor_html,
    write_monitor_html,
)


def _event(
    sequence: int,
    turn_id: str,
    *,
    saved: float,
    spent: float,
    latency: float | None,
    raw: float | None = None,
    legacy: bool = False,
) -> dict:
    metrics = {
        "tokens_saved": saved,
        "raw_context_tokens": saved + spent if raw is None else raw,
        "rendered_context_tokens": spent,
    }
    if latency is not None:
        metrics["projection_latency_ms"] = latency
    return {
        "projection_id": f"projection-{sequence}",
        "projection_sequence": sequence,
        "turn_id": turn_id,
        "session_id": "session-a",
        "legacy": legacy,
        "metrics": metrics,
    }


def _cache_event(
    sequence: int,
    turn_id: str,
    *,
    prompt: float,
    cache_read: float,
    cache_write: float = 0,
    created_at: float | None = None,
) -> dict:
    return {
        "cache_request_id": f"cache-request-{sequence}",
        "request_sequence": sequence,
        "turn_id": turn_id,
        "session_id": "session-a",
        "created_at": created_at or 0,
        "metrics": {
            "prompt_tokens": prompt,
            "uncached_input_tokens": max(
                0, prompt - cache_read - cache_write
            ),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "prompt_cache_hit_ratio": (
                cache_read / prompt if prompt else 0
            ),
        },
    }


def _timeline() -> dict:
    return {
        "schema_version": 1,
        "conversation_id": "conversation-a",
        "session_id": "session-a",
        "title": "Alpha experiment",
        "projections": [
            _event(1, "turn-a", saved=60, spent=40, latency=1.5),
            _event(2, "turn-a", saved=80, spent=20, latency=2.5),
            _event(3, "turn-b", saved=70, spent=30, latency=3.0),
        ],
        "cache_requests": [
            _cache_event(1, "turn-a", prompt=100, cache_read=0),
            _cache_event(2, "turn-a", prompt=200, cache_read=120, cache_write=10),
            _cache_event(3, "turn-b", prompt=400, cache_read=320),
        ],
    }


def _dashboard() -> dict:
    current = _timeline()
    for index, event in enumerate(current["projections"], start=1):
        event["created_at"] = 100 + index
    older = _timeline()
    older["conversation_id"] = "conversation-b"
    older["session_id"] = "session-b"
    older["title"] = "Beta experiment"
    older["projections"] = older["projections"][:1]
    older["projections"][0]["created_at"] = 50
    older["projections"][0]["metrics"]["tokens_saved"] = 10
    older["cache_requests"] = older["cache_requests"][:1]
    older["cache_requests"][0]["created_at"] = 51
    return {
        "schema_version": 2,
        "active_conversation_id": "conversation-a",
        "conversation_id": "conversation-a",
        "session_id": "resumed-session-a",
        "sessions": [older, current],
    }


def _chart(payload: dict, group_key: str, chart_key: str) -> dict:
    group = next(group for group in payload["groups"] if group["key"] == group_key)
    return next(chart for chart in group["charts"] if chart["key"] == chart_key)


def test_payload_contains_four_groups_and_all_sixteen_requested_dynamics():
    payload = build_monitor_payload(_timeline())

    assert [group["key"] for group in payload["groups"]] == [
        "saved",
        "cache",
        "time",
        "spent",
    ]
    assert all(len(group["charts"]) == 4 for group in payload["groups"])
    assert payload["project_count"] == 3
    assert payload["turn_count"] == 2
    assert payload["title"] == "Alpha experiment"
    assert payload["cache_request_count"] == 3
    assert payload["cache_turn_count"] == 2
    assert payload["download_point_count"] == 60
    assert payload["totals"] == {
        "tokens_saved": 210.0,
        "projection_latency_ms": 7.0,
        "rendered_context_tokens": 90.0,
        "prompt_tokens": 700.0,
        "uncached_input_tokens": 250.0,
        "cache_read_tokens": 440.0,
        "cache_write_tokens": 10.0,
        "cache_hit_percent": 62.857143,
    }

    assert _chart(payload, "saved", "project-saved")["values"] == [60.0, 80.0, 70.0]
    assert _chart(payload, "saved", "project-saved-cumulative")["values"] == [
        60.0,
        140.0,
        210.0,
    ]
    assert _chart(payload, "saved", "turn-saved")["values"] == [140.0, 70.0]
    assert _chart(payload, "saved", "turn-saved-cumulative")["values"] == [
        140.0,
        210.0,
    ]
    saved_group = next(group for group in payload["groups"] if group["key"] == "saved")
    assert saved_group["default_display_mode"] == "absolute"
    assert saved_group["display_modes"] == [
        {"key": "absolute", "label": "Token 数"},
        {"key": "relative", "label": "节省比例"},
    ]
    assert _chart(payload, "saved", "project-saved")["modes"]["relative"] == {
        "label": "节省比例",
        "title": "Project · 节省率",
        "unit": "percent",
        "values": [60.0, 80.0, 70.0],
    }
    assert _chart(payload, "saved", "project-saved-cumulative")["modes"][
        "relative"
    ]["values"] == [60.0, 70.0, 70.0]
    assert _chart(payload, "saved", "turn-saved")["modes"]["relative"][
        "values"
    ] == [70.0, 70.0]
    assert _chart(payload, "saved", "turn-saved-cumulative")["modes"][
        "relative"
    ]["values"] == [70.0, 70.0]

    cache_group = next(group for group in payload["groups"] if group["key"] == "cache")
    assert cache_group["default_display_mode"] == "relative"
    assert cache_group["display_modes"] == [
        {"key": "relative", "label": "命中率"},
        {"key": "absolute", "label": "命中 Token"},
    ]
    assert _chart(payload, "cache", "request-cache-hit")["values"] == [
        0.0,
        60.0,
        80.0,
    ]
    assert _chart(payload, "cache", "request-cache-hit-cumulative")[
        "values"
    ] == [0.0, 40.0, 62.857143]
    assert _chart(payload, "cache", "turn-cache-hit")["values"] == [
        40.0,
        80.0,
    ]
    assert _chart(payload, "cache", "turn-cache-hit-cumulative")[
        "values"
    ] == [40.0, 62.857143]
    assert _chart(payload, "cache", "request-cache-hit")["modes"][
        "absolute"
    ]["values"] == [0.0, 120.0, 320.0]
    assert _chart(payload, "cache", "request-cache-hit-cumulative")[
        "modes"
    ]["absolute"]["values"] == [0.0, 120.0, 440.0]

    assert _chart(payload, "time", "project-time")["values"] == [1.5, 2.5, 3.0]
    assert _chart(payload, "time", "project-time-cumulative")["values"] == [
        1.5,
        4.0,
        7.0,
    ]
    assert _chart(payload, "time", "turn-time")["values"] == [4.0, 3.0]
    assert _chart(payload, "time", "turn-time-cumulative")["values"] == [
        4.0,
        7.0,
    ]

    assert _chart(payload, "spent", "project-spent")["values"] == [40.0, 20.0, 30.0]
    assert _chart(payload, "spent", "project-spent-cumulative")["values"] == [
        40.0,
        60.0,
        90.0,
    ]
    assert _chart(payload, "spent", "turn-spent")["values"] == [60.0, 30.0]
    assert _chart(payload, "spent", "turn-spent-cumulative")["values"] == [
        60.0,
        90.0,
    ]


def test_legacy_projects_remain_in_token_projects_without_fabricated_turn_or_time():
    timeline = _timeline()
    timeline["projections"].append(
        _event(4, "", saved=5, spent=2, latency=None, legacy=True)
    )

    payload = build_monitor_payload(timeline)

    assert payload["project_count"] == 4
    assert payload["timed_project_count"] == 3
    assert payload["turn_count"] == 2
    assert payload["legacy_project_count"] == 1
    assert payload["turnless_project_count"] == 1
    assert _chart(payload, "saved", "project-saved")["values"][-1] == 5.0
    assert len(_chart(payload, "saved", "turn-saved")["values"]) == 2
    assert len(_chart(payload, "time", "project-time")["values"]) == 3


def test_session_without_exact_cache_telemetry_keeps_empty_cache_charts():
    timeline = _timeline()
    timeline.pop("cache_requests")

    payload = build_monitor_payload(timeline)

    assert payload["cache_request_count"] == 0
    assert payload["totals"]["cache_hit_percent"] == 0.0
    assert _chart(payload, "cache", "request-cache-hit")["values"] == []
    assert _chart(payload, "cache", "turn-cache-hit-cumulative")["values"] == []


def test_new_projection_without_turn_identity_is_labeled_and_excluded_from_turns():
    timeline = _timeline()
    timeline["projections"].append(
        _event(4, "", saved=5, spent=2, latency=1.0, legacy=False)
    )

    payload = build_monitor_payload(timeline)

    assert payload["legacy_project_count"] == 0
    assert payload["turnless_project_count"] == 1
    assert payload["timed_project_count"] == 4
    assert len(_chart(payload, "saved", "project-saved")["values"]) == 4
    assert len(_chart(payload, "saved", "turn-saved")["values"]) == 2


def test_savings_percentages_use_weighted_raw_totals_not_mean_percentages():
    timeline = _timeline()
    timeline["projections"] = [
        _event(1, "turn-a", saved=9, spent=1, raw=10, latency=1.0),
        _event(2, "turn-a", saved=10, spent=90, raw=100, latency=1.0),
    ]

    payload = build_monitor_payload(timeline)

    project_cumulative = _chart(
        payload, "saved", "project-saved-cumulative"
    )["modes"]["relative"]["values"]
    turn_percentage = _chart(payload, "saved", "turn-saved")["modes"][
        "relative"
    ]["values"]
    assert project_cumulative == [90.0, 17.272727]
    assert turn_percentage == [17.272727]


def test_savings_percentage_fails_closed_for_zero_raw_and_bounds_invalid_ratio():
    timeline = _timeline()
    timeline["projections"] = [
        _event(1, "turn-a", saved=5, spent=0, raw=0, latency=1.0),
        _event(2, "turn-b", saved=10, spent=0, raw=5, latency=1.0),
    ]

    payload = build_monitor_payload(timeline)

    assert _chart(payload, "saved", "project-saved")["modes"]["relative"][
        "values"
    ] == [0.0, 100.0]


def test_dashboard_aggregates_all_sessions_and_selects_active_conversation():
    payload = build_monitor_dashboard_payload(_dashboard())

    assert payload["session_count"] == 2
    assert payload["selected_conversation_id"] == "conversation-a"
    assert [session["conversation_id"] for session in payload["sessions"]] == [
        "conversation-a",
        "conversation-b",
    ]
    assert payload["sessions"][0]["is_active"] is True
    assert [session["title"] for session in payload["sessions"]] == [
        "Alpha experiment",
        "Beta experiment",
    ]
    assert payload["global_totals"] == {
        "project_count": 4,
        "turn_count": 3,
        "cache_request_count": 4,
        "timed_project_count": 4,
        "legacy_project_count": 0,
        "tokens_saved": 220.0,
        "projection_latency_ms": 8.5,
        "rendered_context_tokens": 130.0,
        "prompt_tokens": 800.0,
        "uncached_input_tokens": 350.0,
        "cache_read_tokens": 440.0,
        "cache_write_tokens": 10.0,
        "cache_hit_percent": 55.0,
    }
    assert all(
        sum(len(group["charts"]) for group in session["groups"]) == 16
        for session in payload["sessions"]
    )


def test_dashboard_falls_back_to_newest_session_when_active_has_no_telemetry():
    dashboard = _dashboard()
    dashboard["active_conversation_id"] = "conversation-without-projections"

    payload = build_monitor_dashboard_payload(dashboard)

    assert payload["selected_conversation_id"] == "conversation-a"
    assert not any(session["is_active"] for session in payload["sessions"])


def test_html_is_standalone_has_sixteen_charts_and_omits_unrecognized_content():
    timeline = _timeline()
    timeline["conversation_id"] = "conv</script><img src=x>"
    timeline["raw_message"] = "TOP SECRET PROMPT"

    html = render_monitor_html(timeline)

    assert html.startswith("<!doctype html>")
    assert html.count("<script>") == 1
    assert "https://" not in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "TOP SECRET PROMPT" not in html
    assert "</script><img src=x>" not in html
    assert "conv&lt;/script&gt;&lt;img src=x&gt;" in html

    match = re.search(r"const DATA=(.*);\nconst NS=", html)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["session_count"] == 1
    assert sum(
        len(group["charts"]) for group in payload["sessions"][0]["groups"]
    ) == 16
    assert payload["sessions"][0]["conversation_id"] == "conv</script><img src=x>"
    assert "Runs" in html
    assert "CSV ↓" in html
    assert "function chartDownload" in html
    assert "function sessionDownload" in html
    assert "全部 CSV" in html
    assert "session_title" in html
    assert "display_mode" in html
    assert "String(session.title" in html
    assert "point_index" in html
    assert "节省比例" in html
    assert 'groupDisplayModes={saved:"absolute",cache:"relative"}' in html
    assert "Prompt 缓存命中" in html
    assert "Request · 累计命中率" in html
    assert "命中 Token" in html
    assert "fmtCache" in html
    assert 'chart.unit==="percent"?100' in html
    assert "if(chart.modes)" in html
    assert '<div class="privacy">' not in html
    assert "仅含存储标题" not in html
    assert "Coverage" in html
    assert "暂无数据" in html
    assert "第一类 ·" not in html
    assert "Token 节省" in html
    assert 'label:"B"' in html
    assert 'label:"M"' in html
    assert 'label:"K"' in html
    assert 'maximumSignificantDigits:3' in html
    assert 'notation:n>=1000000?"compact"' not in html


def test_dashboard_write_uses_private_predictable_profile_directory(tmp_path):
    path = write_monitor_html(_timeline(), hermes_home=tmp_path)

    assert path.parent == tmp_path / "logs" / "object-context-monitor"
    assert path.name.startswith("object_context_monitor_")
    assert path.suffix == ".html"
    assert "Session Dynamics Monitor" in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_all_session_dashboard_uses_one_stable_snapshot_path(tmp_path):
    path = write_monitor_html(_dashboard(), hermes_home=tmp_path)

    assert path.name == "object_context_monitor_all_sessions.html"
    html = path.read_text(encoding="utf-8")
    assert "conversation-a" in html
    assert "conversation-b" in html
    assert "Alpha experiment" in html
    assert "Beta experiment" in html
    assert "download.download=downloadData.filename" in html
    assert "downloadAll.download=allData.filename" in html


def test_untitled_session_uses_readable_primary_label():
    timeline = _timeline()
    timeline["title"] = ""

    payload = build_monitor_payload(timeline)

    assert payload["title"] == "未命名会话"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -5, "invalid"])
def test_invalid_numeric_telemetry_fails_closed_to_zero(value):
    timeline = _timeline()
    timeline["projections"][0]["metrics"]["tokens_saved"] = value

    payload = build_monitor_payload(timeline)

    assert _chart(payload, "saved", "project-saved")["values"][0] == 0.0
