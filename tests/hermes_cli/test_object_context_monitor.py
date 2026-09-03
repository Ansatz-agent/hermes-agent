from __future__ import annotations

import http.client
import json
import os
import re
import stat
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen

import pytest

from hermes_cli.object_context_monitor import (
    build_monitor_dashboard_payload,
    build_monitor_payload,
    render_monitor_html,
    start_monitor_server,
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
        "raw_conversation_tokens": saved + spent if raw is None else raw,
        "rendered_conversation_tokens": (
            spent if raw is None else max(0, raw - saved)
        ),
        "conversation_tokens_saved": saved,
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


def _request_event(
    sequence: int,
    turn_id: str,
    *,
    prompt: int,
    output: int,
    cache_read: int,
    cache_write: int = 0,
    latency_ms: float,
) -> dict:
    uncached = max(0, prompt - cache_read - cache_write)
    return {
        "api_request_id": f"request-{sequence}",
        "request_sequence": sequence,
        "turn_id": turn_id,
        "session_id": "session-a",
        "started_at": 100 + sequence,
        "metrics": {
            "input_tokens": uncached,
            "output_tokens": output,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
            "prompt_tokens": prompt,
            "total_tokens": prompt + output,
            "api_duration_ms": latency_ms,
        },
    }


def _economic_decision(
    sequence: int,
    *,
    kind: str,
    mode: str,
    reason: str,
    gross: float,
    replacement: float,
    penalty: float,
    summary_cost: float,
    net: float,
) -> dict:
    return {
        "projection_epoch_id": f"decision-{sequence}",
        "request_sequence": sequence,
        "decision_kind": kind,
        "decision_mode": mode,
        "decision_reason": reason,
        "candidate_count": 1,
        "member_delta_ids": [f"delta-{sequence}"],
        "member_object_refs": [
            f"object://obj_{sequence:024x}@v1"
        ],
        "earliest_changed_delta_id": f"delta-{sequence}",
        "baseline_prompt_tokens": 1_000,
        "candidate_prompt_tokens": 1_000 - gross,
        "gross_tokens_removed": gross,
        "card_or_receipt_tokens": replacement,
        "baseline_reusable_prefix_tokens": 800,
        "candidate_reusable_prefix_tokens": 700,
        "cache_tokens_invalidated": 100,
        "cache_penalty_equivalent_tokens": penalty,
        "known_summary_cost_equivalent_tokens": summary_cost,
        "net_saving_equivalent_tokens": net,
        "net_saving_usd": None,
        "cache_read_weight": 0.1,
        "cache_write_weight": 1.0,
        "pricing_source": "test",
        "pricing_version": "v1",
        "estimator_source": "rough_message_estimator",
        "created_at": 200 + sequence,
    }


def _amortized_decision(
    sequence: int,
    *,
    kind: str,
    mode: str,
    reason: str,
    projected_w: float,
    q: float,
    crossed: bool,
    count_over: bool = False,
    tokens_over: bool = False,
    immediate_crossed: bool = False,
    immediate_net: float = 0,
) -> dict:
    decision = _economic_decision(
        sequence,
        kind=kind,
        mode=mode,
        reason=reason,
        gross=1_500,
        replacement=100,
        penalty=900,
        summary_cost=0,
        # Deliberately differ from the independent V1.1 counterfactual below;
        # the monitor must never relabel this fixed-policy/actual score.
        net=-9_000 - sequence,
    )
    decision.update({
        "request_attempt_id": f"00000000-0000-0000-0000-{sequence:012d}",
        "policy_version": "1.2",
        "batch_policy": "dynamic",
        "fixed_batch_size": 4,
        "baseline_state": "known",
        "cache_granularity_tokens": 128,
        "hot_underexposed_count": 1,
        "hot_seen_delta_count": 3,
        "hot_seen_bucket_count": 2,
        "hot_tail_tokens": 4_000 + sequence * 100,
        "hot_overflow_tokens": 200 if mode == "capacity" else 0,
        "hot_start_token_offset": 6_000,
        "pending_delta_count": 2,
        "pending_bucket_count": 2,
        "pending_raw_tokens": 2_000 + sequence * 100,
        "pending_gain_tokens": 1_500,
        "wait_area_token_requests": max(0, projected_w * 10 - 1_500),
        "wait_loss_now": max(0, projected_w - 150),
        "wait_loss_increment": 150.0,
        "wait_loss_projected": projected_w,
        "shared_cached_hot_tokens": 500,
        "shared_overhead_equivalent_tokens": q,
        "crossing_margin": projected_w - q,
        "emergency_triggered": False,
        "pending_count_over": count_over,
        "pending_tokens_over": tokens_over,
        "amortized_crossed": crossed,
        "immediate_crossed": immediate_crossed,
        "amortized_cache_read_weight": 0.1,
        "amortized_baseline_prompt_tokens": 10_000,
        "amortized_candidate_prompt_tokens": 8_500,
        "amortized_baseline_reusable_prefix_tokens": 8_960,
        "amortized_candidate_reusable_prefix_tokens": 7_936,
        "immediate_cache_penalty_equivalent_tokens": 900.0,
        "immediate_net_saving_equivalent_tokens": immediate_net,
        "immediate_net_saving_usd": None,
        "immediate_cache_read_weight": 0.1,
        "immediate_cache_write_weight": 1.0,
        "immediate_pricing_source": "configured_fallback",
        "immediate_pricing_version": "test-v1",
    })
    return decision


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
        "requests": [
            _request_event(
                1, "turn-a", prompt=100, output=10, cache_read=0,
                latency_ms=1.5,
            ),
            _request_event(
                2, "turn-a", prompt=200, output=20, cache_read=120,
                cache_write=10, latency_ms=2.5,
            ),
            _request_event(
                3, "turn-b", prompt=400, output=30, cache_read=320,
                latency_ms=3.0,
            ),
        ],
        "usage_aggregate": {
            "input_tokens": 250,
            "output_tokens": 60,
            "cache_read_tokens": 440,
            "cache_write_tokens": 10,
            "reasoning_tokens": 0,
            "api_call_count": 3,
            "total_tokens": 760,
        },
        "request_usage_coverage": {
            "event_count": 3,
            "aggregate_api_call_count": 3,
            "event_tokens": 760,
            "aggregate_tokens": 760,
            "call_percent": 100,
            "token_percent": 100,
            "complete": True,
        },
        "cache_requests": [
            _cache_event(1, "turn-a", prompt=100, cache_read=0),
            _cache_event(2, "turn-a", prompt=200, cache_read=120, cache_write=10),
            _cache_event(3, "turn-b", prompt=400, cache_read=320),
        ],
        "economic_decisions": [
            _economic_decision(
                1,
                kind="wait",
                mode="normal",
                reason="WAIT_BELOW_THRESHOLD",
                gross=100,
                replacement=10,
                penalty=120,
                summary_cost=0,
                net=-20,
            ),
            _economic_decision(
                2,
                kind="flush",
                mode="normal",
                reason="FLUSH_NET_POSITIVE",
                gross=100,
                replacement=10,
                penalty=30,
                summary_cost=0,
                net=70,
            ),
            _economic_decision(
                3,
                kind="emergency",
                mode="emergency",
                reason="EMERGENCY_FLUSH",
                gross=80,
                replacement=10,
                penalty=130,
                summary_cost=0,
                net=-50,
            ),
        ],
    }


def _timeline_with_v12() -> dict:
    timeline = _timeline()
    timeline["economic_decisions"].extend([
        _amortized_decision(
            4,
            kind="wait",
            mode="amortized",
            reason="WAIT_BELOW_AMORTIZED_CROSSING",
            projected_w=250,
            q=450,
            crossed=False,
            immediate_crossed=True,
            immediate_net=600,
        ),
        _amortized_decision(
            5,
            kind="flush",
            mode="amortized",
            reason="FLUSH_AMORTIZED_CROSSING",
            projected_w=450,
            q=450,
            crossed=True,
            immediate_net=-100,
        ),
        _amortized_decision(
            6,
            kind="flush",
            mode="capacity",
            reason="FLUSH_PENDING_CAPACITY",
            projected_w=100,
            q=450,
            crossed=False,
            count_over=True,
            tokens_over=True,
            immediate_crossed=True,
            immediate_net=500,
        ),
    ])
    timeline["request_observations"] = [
        {
            "request_attempt_id": "00000000-0000-0000-0000-000000000006",
            "success_sequence": 6,
            "exposure_request_sequence": 6,
            "route_namespace_hash": "abcdef012345",
            "outcome": "confirmed_success",
            "raw_delta_count": 2,
            "accrued_delta_count": 2,
            "skipped_pending_delta_count": 0,
            "newly_eligible_delta_count": 1,
            "created_at": 206,
            "raw_message": "REQUEST OBSERVATION SECRET",
        }
    ]
    return timeline


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
    older["projections"][0]["metrics"]["conversation_tokens_saved"] = 10
    older["projections"][0]["metrics"]["rendered_conversation_tokens"] = 90
    older["requests"] = older["requests"][:1]
    older["usage_aggregate"] = {
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "api_call_count": 1,
        "total_tokens": 110,
    }
    older["request_usage_coverage"] = {
        "event_count": 1,
        "aggregate_api_call_count": 1,
        "event_tokens": 110,
        "aggregate_tokens": 110,
        "call_percent": 100,
        "token_percent": 100,
        "complete": True,
    }
    older["cache_requests"] = older["cache_requests"][:1]
    older["cache_requests"][0]["created_at"] = 51
    older["economic_decisions"] = []
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


def test_payload_preserves_existing_groups_and_adds_v11_economic_charts():
    payload = build_monitor_payload(_timeline())

    assert payload["schema_version"] == 12
    assert payload["object_context_used"] is True
    assert [group["key"] for group in payload["groups"]] == [
        "saved",
        "conversation_saved",
        "economic",
        "amortized",
        "cache",
        "time",
        "spent",
    ]
    assert _chart(payload, "amortized", "amortized-crossing-margin")
    assert payload["project_count"] == 3
    assert payload["turn_count"] == 2
    assert payload["title"] == "Alpha experiment"
    assert payload["cache_request_count"] == 3
    assert payload["cache_turn_count"] == 2
    assert payload["download_point_count"] == 81
    assert payload["economic_metrics_available"] is True
    assert payload["economic_decision_count"] == 3
    assert payload["amortized_metrics_available"] is False
    assert payload["amortized_decision_count"] == 0
    assert payload["normal_projection_count"] == 1
    assert payload["emergency_projection_count"] == 1
    assert payload["economic_reason_counts"] == {
        "WAIT_BELOW_THRESHOLD": 1,
        "FLUSH_NET_POSITIVE": 1,
        "EMERGENCY_FLUSH": 1,
    }
    assert payload["amortized_summary"]["latest"] is None
    assert _chart(payload, "amortized", "amortized-w-projected")[
        "values"
    ] == []
    assert "V1.1/legacy session" in _chart(
        payload, "amortized", "amortized-w-projected"
    )["empty_message"]
    assert payload["totals"] == {
        "tokens_saved": 210.0,
        "economic_normal_gross_tokens_removed": 100.0,
        "economic_normal_cache_penalty_tokens": 30.0,
        "economic_normal_summary_cost_tokens": 0.0,
        "economic_normal_net_saving_tokens": 70.0,
        "economic_emergency_gross_tokens_removed": 80.0,
        "economic_emergency_net_effect_tokens": -50.0,
        "raw_conversation_tokens": 300.0,
        "rendered_conversation_tokens": 90.0,
        "conversation_tokens_saved": 210.0,
        "conversation_reduction_percent": 70.0,
        "compression_inference_tokens": 0.0,
        "compression_schema_tokens_rough": 0.0,
        "compression_overhead_tokens": 0.0,
        "auxiliary_provider_tokens": 0.0,
        "all_provider_tokens_known": 760.0,
        "net_tokens_saved_known": 210.0,
        "api_duration_ms": 7.0,
        "provider_tokens": 760.0,
        "prompt_tokens": 700.0,
        "uncached_input_tokens": 250.0,
        "cache_read_tokens": 440.0,
        "cache_write_tokens": 10.0,
        "cache_hit_percent": 62.857143,
    }

    assert _chart(payload, "economic", "economic-gross")["values"] == [
        100.0,
        100.0,
        80.0,
    ]
    assert _chart(payload, "economic", "economic-cache-penalty")[
        "values"
    ] == [120.0, 30.0, 130.0]
    assert _chart(payload, "economic", "economic-normal-net")["values"] == [
        -20.0,
        70.0,
    ]
    assert _chart(payload, "economic", "economic-normal-net")["signed"] is True
    assert _chart(payload, "economic", "economic-emergency-net")[
        "values"
    ] == [-50.0]

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
    assert _chart(
        payload, "conversation_saved", "conversation-project-saved"
    )["values"] == [60.0, 80.0, 70.0]
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

    assert _chart(payload, "time", "request-time")["values"] == [1.5, 2.5, 3.0]
    assert _chart(payload, "time", "request-time-cumulative")["values"] == [
        1.5,
        4.0,
        7.0,
    ]
    assert _chart(payload, "time", "turn-time")["values"] == [4.0, 3.0]
    assert _chart(payload, "time", "turn-time-cumulative")["values"] == [
        4.0,
        7.0,
    ]

    assert _chart(payload, "spent", "request-spent")["values"] == [110.0, 220.0, 430.0]
    assert _chart(payload, "spent", "request-spent-cumulative")["values"] == [
        110.0,
        330.0,
        760.0,
    ]
    assert _chart(payload, "spent", "turn-spent")["values"] == [330.0, 430.0]
    assert _chart(payload, "spent", "turn-spent-cumulative")["values"] == [
        330.0,
        760.0,
    ]


def test_v12_decisions_are_separate_aggregated_and_exported_content_free():
    timeline = _timeline_with_v12()
    first_v12 = timeline["economic_decisions"][3]
    first_v12["raw_content"] = "AMORTIZED RAW SECRET"
    first_v12["candidate_prompt_tokens"] = "NUMERIC FIELD SECRET"
    first_v12["wait_loss_now"] = {"secret": "MAPPING SECRET"}

    payload = build_monitor_payload(timeline)

    # Historical V1.1 charts/totals remain unchanged by V1.2 counterfactuals.
    assert payload["economic_decision_count"] == 3
    assert payload["normal_projection_count"] == 1
    assert payload["totals"]["economic_normal_net_saving_tokens"] == 70.0
    assert _chart(payload, "economic", "economic-normal-net")["values"] == [
        -20.0,
        70.0,
    ]

    assert payload["amortized_metrics_available"] is True
    assert payload["amortized_decision_count"] == 3
    assert payload["amortized_wait_count"] == 1
    assert payload["amortized_flush_count"] == 2
    assert payload["amortized_emergency_count"] == 0
    assert payload["amortized_mode_counts"] == {
        "amortized": 2,
        "capacity": 1,
    }
    assert payload["amortized_reason_counts"] == {
        "WAIT_BELOW_AMORTIZED_CROSSING": 1,
        "FLUSH_AMORTIZED_CROSSING": 1,
        "FLUSH_PENDING_CAPACITY": 1,
    }
    assert payload["amortized_summary"] == next(
        group["summary"]
        for group in payload["groups"]
        if group["key"] == "amortized"
    )
    summary = payload["amortized_summary"]
    assert summary["publication_count"] == 2
    assert summary["capacity_trigger_count"] == 1
    assert summary["pending_count_over_count"] == 1
    assert summary["pending_tokens_over_count"] == 1
    assert summary["amortized_crossed_count"] == 1
    assert summary["immediate_crossed_count"] == 2

    assert _chart(payload, "amortized", "amortized-hot-tokens")[
        "values"
    ] == [4_400.0, 4_500.0, 4_600.0]
    assert _chart(payload, "amortized", "amortized-hot-overflow")[
        "values"
    ] == [0.0, 0.0, 200.0]
    assert _chart(payload, "amortized", "amortized-pending-buckets")[
        "values"
    ] == [2.0, 2.0, 2.0]
    assert _chart(payload, "amortized", "amortized-pending-raw")[
        "values"
    ] == [2_400.0, 2_500.0, 2_600.0]
    assert _chart(payload, "amortized", "amortized-w-projected")[
        "values"
    ] == [250.0, 450.0, 100.0]
    assert _chart(payload, "amortized", "amortized-q-overhead")[
        "values"
    ] == [450.0, 450.0, 450.0]
    assert _chart(payload, "amortized", "amortized-crossing-margin")[
        "values"
    ] == [-200.0, 0.0, -350.0]
    assert _chart(payload, "amortized", "amortized-crossed")["values"] == [
        0.0,
        1.0,
        0.0,
    ]
    assert _chart(payload, "amortized", "amortized-cap-count")["values"] == [
        0.0,
        0.0,
        1.0,
    ]
    assert _chart(payload, "amortized", "amortized-cap-tokens")["values"] == [
        0.0,
        0.0,
        1.0,
    ]
    counterfactual = _chart(
        payload, "amortized", "amortized-immediate-net-counterfactual"
    )
    assert counterfactual["values"] == [600.0, -100.0, 500.0]
    assert counterfactual["signed"] is True

    exported = payload["amortized_decisions"]
    assert exported[0]["request_attempt_id"].endswith("000000000004")
    assert exported[0]["policy_version"] == "1.2"
    assert exported[0]["batch_policy"] == "dynamic"
    assert exported[0]["fixed_batch_size"] == 4
    assert exported[0]["candidate_prompt_tokens"] is None
    assert exported[0]["wait_loss_now"] is None
    assert exported[0]["immediate_net_saving_equivalent_tokens"] == 600.0
    assert exported[0]["net_saving_equivalent_tokens"] == -9_004.0
    assert exported[0]["immediate_pricing_source"] == "configured_fallback"
    assert exported[2]["capacity_triggered"] is True
    assert "raw_content" not in exported[0]
    assert payload["request_observation_count"] == 1
    assert set(payload["request_observations"][0]) == {
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
    }
    assert payload["last_activity_at"] == 206.0

    serialized = json.dumps(payload, ensure_ascii=False)
    for secret in (
        "AMORTIZED RAW SECRET",
        "NUMERIC FIELD SECRET",
        "MAPPING SECRET",
        "REQUEST OBSERVATION SECRET",
    ):
        assert secret not in serialized


def test_decision_version_fallback_and_alias_dedup_do_not_relabel_v11():
    explicit_v11 = _economic_decision(
        1,
        kind="wait",
        mode="normal",
        reason="WAIT_BELOW_THRESHOLD",
        gross=100,
        replacement=10,
        penalty=120,
        summary_cost=0,
        net=-20,
    )
    explicit_v11.update({
        "policy_version": "1.1",
        "pending_bucket_count": 99,
        "amortized_crossed": False,
    })
    early_v12 = _economic_decision(
        2,
        kind="wait",
        mode="amortized",
        reason="WAIT_BELOW_AMORTIZED_CROSSING",
        gross=100,
        replacement=10,
        penalty=120,
        summary_cost=0,
        net=-20,
    )
    early_v12["amortized_crossed"] = False
    future_v12 = dict(early_v12)
    future_v12["projection_epoch_id"] = "decision-3"
    future_v12["policy_version"] = "object-context-v1.2.1"
    timeline = {
        "conversation_id": "mixed-decisions",
        "projections": [],
        "requests": [],
        "economic_decisions": [explicit_v11, early_v12, future_v12],
        "amortized_decisions": [dict(early_v12)],
    }

    payload = build_monitor_payload(timeline)

    assert payload["economic_decision_count"] == 1
    assert payload["economic_decisions"][0]["policy_version"] == "1.1"
    assert payload["amortized_decision_count"] == 2
    assert [
        row["projection_epoch_id"] for row in payload["amortized_decisions"]
    ] == ["decision-2", "decision-3"]
    assert _chart(payload, "amortized", "amortized-crossed")["labels"] == [
        "A1",
        "A2",
    ]


def test_v12_nullable_series_keeps_original_decision_ordinal():
    timeline = _timeline_with_v12()
    timeline["economic_decisions"][3]["shared_overhead_equivalent_tokens"] = None

    payload = build_monitor_payload(timeline)

    q_chart = _chart(payload, "amortized", "amortized-q-overhead")
    assert q_chart["labels"] == ["A2", "A3"]
    assert q_chart["values"] == [450.0, 450.0]


def test_v12_immediate_chart_does_not_fallback_to_fixed_policy_net():
    timeline = _timeline_with_v12()
    first = timeline["economic_decisions"][3]
    first["immediate_net_saving_equivalent_tokens"] = None
    first["net_saving_equivalent_tokens"] = 123_456

    payload = build_monitor_payload(timeline)

    chart = _chart(
        payload, "amortized", "amortized-immediate-net-counterfactual"
    )
    assert chart["labels"] == ["A2", "A3"]
    assert chart["values"] == [-100.0, 500.0]


def test_v12_wait_only_session_is_active_without_fabricating_a_projection():
    decision = _amortized_decision(
        1,
        kind="wait",
        mode="amortized",
        reason="WAIT_BELOW_AMORTIZED_CROSSING",
        projected_w=10,
        q=100,
        crossed=False,
    )
    timeline = {
        "conversation_id": "wait-only",
        "session_id": "wait-only-session",
        "last_activity_at": 1,
        "projections": [],
        "requests": [],
        "economic_decisions": [decision],
    }

    payload = build_monitor_payload(timeline)

    assert payload["object_context_used"] is True
    assert payload["has_projection_telemetry"] is False
    assert payload["has_decision_telemetry"] is True
    assert payload["project_count"] == 0
    assert payload["amortized_decision_count"] == 1
    assert payload["economic_metrics_available"] is False
    assert payload["last_activity_at"] == decision["created_at"]
    assert "V1.2 scheduler telemetry only" in _chart(
        payload, "economic", "economic-normal-net"
    )["empty_message"]

    html = render_monitor_html(timeline)
    match = re.search(r"const DATA=(.*);\nconst NS=", html)
    assert match is not None
    embedded = json.loads(match.group(1))["sessions"][0]
    assert embedded["object_context_used"] is True
    assert embedded["has_projection_telemetry"] is False
    assert "尚未发布 Card" in html
    assert "V1.1 charts separate · V1.2 scheduler telemetry present" in html


def test_request_observation_export_rejects_malformed_numeric_values():
    timeline = {
        "conversation_id": "malformed-observation",
        "projections": [],
        "requests": [],
        "request_observations": [
            {
                "request_attempt_id": "attempt-1",
                "success_sequence": "NUMERIC SECRET",
                "exposure_request_sequence": 1.5,
                "route_namespace_hash": {"secret": "HASH SECRET"},
                "outcome": ["OUTCOME SECRET"],
                "raw_delta_count": True,
                "accrued_delta_count": -1,
                "skipped_pending_delta_count": float("inf"),
                "newly_eligible_delta_count": 2,
                "created_at": "TIMESTAMP SECRET",
            }
        ],
    }

    payload = build_monitor_payload(timeline)

    [observation] = payload["request_observations"]
    assert observation == {
        "request_attempt_id": "attempt-1",
        "success_sequence": None,
        "exposure_request_sequence": None,
        "route_namespace_hash": None,
        "outcome": None,
        "raw_delta_count": None,
        "accrued_delta_count": None,
        "skipped_pending_delta_count": None,
        "newly_eligible_delta_count": 2,
        "created_at": None,
    }
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "SECRET" not in serialized


def test_v11_wait_only_session_keeps_v11_scheduler_identity():
    decision = _economic_decision(
        1,
        kind="wait",
        mode="normal",
        reason="WAIT_BELOW_THRESHOLD",
        gross=100,
        replacement=10,
        penalty=120,
        summary_cost=0,
        net=-20,
    )
    timeline = {
        "conversation_id": "v11-wait-only",
        "projections": [],
        "requests": [],
        "economic_decisions": [decision],
    }

    payload = build_monitor_payload(timeline)
    html = render_monitor_html(timeline)

    assert payload["object_context_used"] is True
    assert payload["economic_decision_count"] == 1
    assert payload["amortized_decision_count"] == 0
    assert "V1.1 economic decisions · 尚未发布 Card" in html


def test_v12_emergency_is_a_publication_but_not_a_normal_flush():
    decision = _amortized_decision(
        1,
        kind="emergency",
        mode="emergency",
        reason="FLUSH_EMERGENCY",
        projected_w=0,
        q=0,
        crossed=False,
    )
    decision["emergency_triggered"] = True
    payload = build_monitor_payload({
        "conversation_id": "v12-emergency",
        "projections": [],
        "requests": [],
        "economic_decisions": [decision],
    })

    assert payload["amortized_flush_count"] == 0
    assert payload["amortized_emergency_count"] == 1
    assert payload["amortized_summary"]["publication_count"] == 1
    assert payload["amortized_summary"]["emergency_triggered_count"] == 1
    assert payload["amortized_mode_counts"] == {"emergency": 1}


def test_dashboard_aggregates_v12_counts_without_summing_w_or_q_snapshots():
    first = _timeline_with_v12()
    second = _timeline_with_v12()
    second["conversation_id"] = "conversation-v12-b"
    second["session_id"] = "session-v12-b"
    dashboard = {
        "active_conversation_id": "conversation-a",
        "sessions": [first, second],
    }

    payload = build_monitor_dashboard_payload(dashboard)

    totals = payload["global_totals"]
    assert totals["amortized_decision_count"] == 6
    assert totals["amortized_wait_count"] == 2
    assert totals["amortized_flush_count"] == 4
    assert totals["amortized_publication_count"] == 4
    assert totals["capacity_trigger_count"] == 2
    assert totals["request_observation_count"] == 2
    assert payload["amortized_mode_counts"] == {
        "amortized": 4,
        "capacity": 2,
    }
    assert payload["amortized_reason_counts"][
        "FLUSH_PENDING_CAPACITY"
    ] == 2
    assert not any(
        "wait_loss" in key or "shared_overhead" in key
        for key in totals
    )


def test_compression_overhead_ledger_separates_exact_rough_and_embedded_costs():
    timeline = _timeline()
    timeline["auxiliary_usage"] = [
        {
            "task": "object_context_card_summary",
            "api_call_count": 2,
            "total_tokens": 55,
        },
        {"task": "compression", "api_call_count": 1, "total_tokens": 20},
        {"task": "vision", "api_call_count": 1, "total_tokens": 99},
    ]
    timeline["object_context_metrics"] = {
        "card_summary_attempts": 3,
        "summary_fallbacks": 1,
        "card_tokens": 12,
        "retrieved_tokens": 15,
        "retrieval_count": 1,
    }
    timeline["retrieve_object_schema_tokens_per_request"] = 4

    payload = build_monitor_payload(timeline)
    overhead = payload["compression_overhead"]

    assert overhead["gross_saved_tokens"] == 210.0
    assert overhead["exact_inference_tokens"] == 75.0
    assert overhead["rough_schema_tokens"] == 12.0
    assert overhead["known_overhead_tokens"] == 87.0
    assert overhead["known_net_saved_tokens"] == 123.0
    assert overhead["card_text_tokens"] == 12.0
    assert overhead["retrieved_payload_tokens"] == 15.0
    assert overhead["other_auxiliary_tokens"] == 99.0
    assert overhead["recorded_card_summary_calls"] == 2
    assert overhead["unmetered_card_summary_attempts"] == 1
    assert overhead["coverage_complete"] is False
    assert "历史 combined task" in overhead["coverage_label"]
    components = {item["key"]: item for item in overhead["components"]}
    assert components["object_context_card_summary"]["deducted"] is True
    assert components["retrieve_object_schema"]["tokens"] == 12.0
    assert components["object_card_text"]["deducted"] is False
    assert components["retrieved_payload"]["deducted"] is False
    assert components["auxiliary:vision"]["tokens"] == 99.0
    assert components["auxiliary:vision"]["deducted"] is False
    assert payload["totals"]["all_provider_tokens_known"] == 934.0
    assert payload["totals"]["provider_tokens"] == 760.0


def test_monitor_excludes_background_projections_and_auxiliary_usage():
    timeline = _timeline()
    timeline["projections"].extend(
        [
            _event(
                4,
                "turn-background",
                saved=1_000,
                spent=500,
                latency=9.0,
            ),
            _event(
                5,
                "turn-without-provider-usage",
                saved=2_000,
                spent=500,
                latency=9.0,
            ),
        ]
    )
    timeline["auxiliary_usage"] = [
        {
            "task": "background_review",
            "api_call_count": 12,
            "total_tokens": 50_000,
        },
        {"task": "vision", "api_call_count": 1, "total_tokens": 99},
    ]
    timeline["retrieve_object_schema_tokens_per_request"] = 4

    payload = build_monitor_payload(timeline)
    overhead = payload["compression_overhead"]
    components = {item["key"]: item for item in overhead["components"]}

    assert payload["project_count"] == 3
    assert payload["turn_count"] == 2
    assert payload["conversation_metric_count"] == 3
    assert payload["totals"]["tokens_saved"] == 210.0
    assert payload["totals"]["conversation_tokens_saved"] == 210.0
    assert overhead["rough_schema_tokens"] == 12.0
    assert overhead["other_auxiliary_tokens"] == 99.0
    assert "auxiliary:background_review" not in components
    assert components["auxiliary:vision"]["tokens"] == 99.0
    assert _chart(payload, "saved", "project-saved")["values"] == [
        60.0,
        80.0,
        70.0,
    ]


def test_known_net_preserves_negative_values_when_overhead_exceeds_savings():
    timeline = _timeline()
    timeline["auxiliary_usage"] = [
        {"task": "compression", "api_call_count": 1, "total_tokens": 500}
    ]
    timeline["retrieve_object_schema_tokens_per_request"] = 10

    session = build_monitor_payload(timeline)
    dashboard = build_monitor_dashboard_payload(timeline)

    assert session["totals"]["tokens_saved"] == 210.0
    assert session["totals"]["compression_overhead_tokens"] == 530.0
    assert session["totals"]["net_tokens_saved_known"] == -320.0
    assert dashboard["global_totals"]["net_tokens_saved_known"] == -320.0


def test_legacy_projects_remain_in_token_projects_without_fabricated_turn_or_time():
    timeline = _timeline()
    timeline["projections"].append(
        _event(4, "", saved=5, spent=2, latency=None, legacy=True)
    )

    payload = build_monitor_payload(timeline)

    assert payload["project_count"] == 4
    assert payload["timed_request_count"] == 3
    assert payload["turn_count"] == 2
    assert payload["legacy_project_count"] == 1
    assert payload["turnless_project_count"] == 1
    assert _chart(payload, "saved", "project-saved")["values"][-1] == 5.0
    assert len(_chart(payload, "saved", "turn-saved")["values"]) == 2
    assert len(_chart(payload, "time", "request-time")["values"]) == 3


def test_session_without_exact_cache_telemetry_keeps_empty_cache_charts():
    timeline = _timeline()
    timeline.pop("cache_requests")
    timeline.pop("requests")
    timeline.pop("usage_aggregate")
    timeline.pop("request_usage_coverage")

    payload = build_monitor_payload(timeline)

    assert payload["cache_request_count"] == 0
    assert payload["totals"]["cache_hit_percent"] == 0.0
    assert _chart(payload, "cache", "request-cache-hit")["values"] == []
    assert _chart(payload, "cache", "turn-cache-hit-cumulative")["values"] == []


def test_unmatched_projection_is_excluded_when_main_request_turns_exist():
    timeline = _timeline()
    timeline["projections"].append(
        _event(4, "", saved=5, spent=2, latency=1.0, legacy=False)
    )

    payload = build_monitor_payload(timeline)

    assert payload["legacy_project_count"] == 0
    assert payload["turnless_project_count"] == 0
    assert payload["timed_request_count"] == 3
    assert len(_chart(payload, "saved", "project-saved")["values"]) == 3
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
    assert turn_percentage == [17.272727, 0.0]


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
        "request_count": 4,
        "request_event_count": 4,
        "request_observation_count": 0,
        "turn_count": 3,
        "cache_request_count": 4,
        "timed_request_count": 4,
        "legacy_project_count": 0,
        "economic_decision_count": 3,
        "amortized_decision_count": 0,
        "amortized_wait_count": 0,
        "amortized_flush_count": 0,
        "amortized_emergency_count": 0,
        "amortized_publication_count": 0,
        "amortized_crossed_count": 0,
        "capacity_trigger_count": 0,
        "pending_count_over_count": 0,
        "pending_tokens_over_count": 0,
        "amortized_emergency_triggered_count": 0,
        "amortized_immediate_crossed_count": 0,
        "normal_projection_count": 1,
        "emergency_projection_count": 1,
        "conversation_metric_count": 4,
        "conversation_metric_missing_count": 0,
        "tokens_saved": 220.0,
        "economic_normal_gross_tokens_removed": 100.0,
        "economic_normal_cache_penalty_tokens": 30.0,
        "economic_normal_summary_cost_tokens": 0.0,
        "economic_normal_net_saving_tokens": 70.0,
        "economic_emergency_gross_tokens_removed": 80.0,
        "economic_emergency_net_effect_tokens": -50.0,
        "raw_conversation_tokens": 400.0,
        "rendered_conversation_tokens": 180.0,
        "conversation_tokens_saved": 220.0,
        "compression_inference_tokens": 0.0,
        "compression_schema_tokens_rough": 0.0,
        "compression_overhead_tokens": 0.0,
        "auxiliary_provider_tokens": 0.0,
        "all_provider_tokens_known": 870.0,
        "net_tokens_saved_known": 220.0,
        "api_duration_ms": 8.5,
        "provider_tokens": 870.0,
        "prompt_tokens": 800.0,
        "uncached_input_tokens": 350.0,
        "cache_read_tokens": 440.0,
        "cache_write_tokens": 10.0,
        "cache_hit_percent": 55.0,
        "conversation_reduction_percent": 55.0,
    }
    assert all(
        _chart(session, "amortized", "amortized-crossing-margin")
        for session in payload["sessions"]
    )


def test_dashboard_falls_back_to_newest_session_when_active_has_no_telemetry():
    dashboard = _dashboard()
    dashboard["active_conversation_id"] = "conversation-without-projections"

    payload = build_monitor_dashboard_payload(dashboard)

    assert payload["selected_conversation_id"] == "conversation-a"
    assert not any(session["is_active"] for session in payload["sessions"])


def test_dashboard_keeps_active_zero_telemetry_session_with_zero_savings():
    dashboard = _dashboard()
    dashboard["active_conversation_id"] = "conversation-plain"
    dashboard["sessions"].append(
        {
            "conversation_id": "conversation-plain",
            "session_id": "session-plain",
            "title": "No Object Context",
            "last_activity_at": 1_000,
            "projections": [],
            "cache_requests": [],
            "requests": [
                _request_event(
                    1, "plain-turn", prompt=120, output=15,
                    cache_read=20, latency_ms=900,
                )
            ],
            "usage_aggregate": {
                "input_tokens": 100,
                "output_tokens": 15,
                "cache_read_tokens": 20,
                "cache_write_tokens": 0,
                "api_call_count": 1,
                "total_tokens": 135,
            },
        }
    )

    payload = build_monitor_dashboard_payload(dashboard)

    assert payload["session_count"] == 3
    assert payload["selected_conversation_id"] == "conversation-plain"
    session = next(
        item
        for item in payload["sessions"]
        if item["conversation_id"] == "conversation-plain"
    )
    assert session["is_active"] is True
    assert session["object_context_used"] is False
    assert session["has_projection_telemetry"] is False
    assert session["project_count"] == 0
    assert session["request_count"] == 1
    assert session["turn_count"] == 1
    assert session["totals"]["tokens_saved"] == 0.0
    assert session["totals"]["api_duration_ms"] == 900.0
    assert session["totals"]["provider_tokens"] == 135.0
    assert _chart(session, "saved", "project-saved")["values"] == [0.0]
    assert _chart(session, "saved", "turn-saved")["values"] == [0.0]
    assert _chart(session, "time", "request-time")["values"] == [900.0]
    assert _chart(session, "spent", "request-spent")["values"] == [135.0]


def test_html_is_standalone_has_extra_conversation_chart_and_omits_content():
    timeline = _timeline()
    timeline["conversation_id"] = "conv</script><img src=x>"
    timeline["raw_message"] = "TOP SECRET PROMPT"
    timeline["economic_decisions"][0]["prompt"] = "ECONOMIC SECRET PROMPT"

    html = render_monitor_html(timeline)

    assert html.startswith("<!doctype html>")
    assert html.count("<script>") == 1
    assert "https://" not in html
    assert "http://" not in html.replace("http://www.w3.org/2000/svg", "")
    assert "TOP SECRET PROMPT" not in html
    assert "ECONOMIC SECRET PROMPT" not in html
    assert "</script><img src=x>" not in html
    assert "conv&lt;/script&gt;&lt;img src=x&gt;" in html

    match = re.search(r"const DATA=(.*);\nconst NS=", html)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["session_count"] == 1
    assert _chart(
        payload["sessions"][0],
        "amortized",
        "amortized-crossing-margin",
    )
    assert payload["sessions"][0]["conversation_id"] == "conv</script><img src=x>"
    assert "Runs" in html
    assert "<th>Context</th>" in html
    assert 'tag.textContent=used?"OC":"No OC"' in html
    assert '{label:"Object Context"' in html
    assert '{label:"No Object Context"' in html
    assert 'cell.colSpan=13' in html
    assert "CSV ↓" in html
    assert "function chartDownload" in html
    assert "function sessionDownload" in html
    assert "全部 CSV" in html
    assert "Compression Overhead Ledger" in html
    assert "Known Net = Gross Saved" in html
    assert "已在 rendered 中，不二次扣除" in html
    assert "function overheadRows" in html
    assert "Known Provider" in html
    assert "Conversation-only Saved" in html
    assert "对话记录 Token 节省" in html
    assert "Project · 对话记录节省" in html
    assert "V1.1 即时经济决策" in html
    assert "Normal decision · Immediate net" in html
    assert "Emergency decision · Immediate net" in html
    assert "V1.2 摊销与容量决策" in html
    assert "W · Projected waiting loss" in html
    assert "Q · Shared rewrite cost" in html
    assert "Counterfactual · V1.1 immediate net" in html
    assert 'unit==="flag"' in html
    assert 'unit==="count"' in html
    assert 'chart.unit==="flag"?1' in html
    assert "const fmtChart=" in html
    assert html.count("fmtChart(") == 3
    assert "该 session 尚无 conversation-only 遥测" in html
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


def test_live_monitor_browser_refresh_reloads_current_telemetry():
    state = {"timeline": _timeline()}
    load_count = 0

    def load_timeline():
        nonlocal load_count
        load_count += 1
        return state["timeline"]

    server = start_monitor_server(
        timeline_loader=load_timeline,
        initial_timeline=state["timeline"],
    )
    try:
        parsed = urlsplit(server.url)
        assert parsed.hostname == "127.0.0.1"
        assert parsed.path != "/"
        assert server.is_running is True

        with urlopen(server.url, timeout=3) as response:
            first_html = response.read().decode("utf-8")
            assert response.headers["Cache-Control"] == (
                "no-store, no-cache, must-revalidate"
            )
            assert "default-src 'none'" in response.headers[
                "Content-Security-Policy"
            ]

        refreshed = _timeline()
        refreshed["title"] = "Telemetry refreshed in browser"
        refreshed["projections"].append(
            _event(4, "turn-c", saved=45, spent=30, latency=2.0)
        )
        refreshed["requests"].append(
            _request_event(
                4,
                "turn-c",
                prompt=75,
                output=8,
                cache_read=30,
                latency_ms=2.0,
            )
        )
        state["timeline"] = refreshed
        with urlopen(server.url, timeout=3) as response:
            second_html = response.read().decode("utf-8")
    finally:
        server.close()

    first_match = re.search(r"const DATA=(.*);\nconst NS=", first_html)
    second_match = re.search(r"const DATA=(.*);\nconst NS=", second_html)
    assert first_match is not None
    assert second_match is not None
    first_payload = json.loads(first_match.group(1))
    second_payload = json.loads(second_match.group(1))
    assert first_payload["sessions"][0]["project_count"] == 3
    assert second_payload["sessions"][0]["project_count"] == 4
    assert second_payload["sessions"][0]["title"] == (
        "Telemetry refreshed in browser"
    )
    assert load_count == 2
    assert server.is_running is False


def test_live_monitor_rejects_unknown_routes_and_spoofed_hosts():
    timeline = _timeline()
    server = start_monitor_server(
        timeline_loader=lambda: timeline,
        initial_timeline=timeline,
    )
    try:
        parsed = urlsplit(server.url)
        with pytest.raises(HTTPError) as unknown_route:
            urlopen(f"http://{parsed.netloc}/", timeout=3)
        assert unknown_route.value.code == 404

        connection = http.client.HTTPConnection(
            parsed.hostname,
            parsed.port,
            timeout=3,
        )
        connection.putrequest("GET", parsed.path, skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 404
        assert response.read() == b"Not found\n"
        connection.close()
    finally:
        server.close()


def test_live_monitor_keeps_last_good_snapshot_when_reload_fails():
    timeline = _timeline()

    def fail_reload():
        raise RuntimeError("transient sqlite read failure")

    server = start_monitor_server(
        timeline_loader=fail_reload,
        initial_timeline=timeline,
    )
    try:
        with urlopen(server.url, timeout=3) as response:
            html = response.read().decode("utf-8")
    finally:
        server.close()

    assert "Session Dynamics Monitor" in html
    assert '"project_count":3' in html


def test_untitled_session_uses_readable_primary_label():
    timeline = _timeline()
    timeline["title"] = ""

    payload = build_monitor_payload(timeline)

    assert payload["title"] == "未命名会话"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -5, "invalid"])
def test_invalid_numeric_telemetry_fails_closed_to_zero(value):
    timeline = _timeline()
    timeline["projections"][0]["metrics"]["tokens_saved"] = value
    timeline["projections"][0]["metrics"]["conversation_tokens_saved"] = value

    payload = build_monitor_payload(timeline)

    assert _chart(payload, "saved", "project-saved")["values"][0] == 0.0
    assert _chart(
        payload, "conversation_saved", "conversation-project-saved"
    )["values"][0] == 0.0


def test_legacy_projection_is_not_relabelled_as_conversation_only():
    timeline = _timeline()
    metrics = timeline["projections"][0]["metrics"]
    for name in (
        "raw_conversation_tokens",
        "rendered_conversation_tokens",
        "conversation_tokens_saved",
    ):
        metrics.pop(name)

    payload = build_monitor_payload(timeline)

    assert payload["conversation_metric_count"] == 2
    assert payload["conversation_metric_missing_count"] == 1
    assert payload["totals"]["conversation_tokens_saved"] == 150.0
    assert _chart(payload, "saved", "project-saved")["values"] == [
        60.0,
        80.0,
        70.0,
    ]
    assert _chart(
        payload, "conversation_saved", "conversation-project-saved"
    )["values"] == [80.0, 70.0]
    html = render_monitor_html(timeline)
    assert "legacy rows unavailable" in html


def test_fully_legacy_session_keeps_old_charts_and_marks_v11_unavailable():
    timeline = _timeline()
    timeline["economic_decisions"] = []
    for event in timeline["projections"]:
        for name in (
            "raw_conversation_tokens",
            "rendered_conversation_tokens",
            "conversation_tokens_saved",
        ):
            event["metrics"].pop(name)

    payload = build_monitor_payload(timeline)
    original = _chart(payload, "saved", "project-saved")
    conversation = _chart(
        payload, "conversation_saved", "conversation-project-saved"
    )
    economic = _chart(payload, "economic", "economic-normal-net")
    amortized = _chart(payload, "amortized", "amortized-w-projected")

    assert original["values"] == [60.0, 80.0, 70.0]
    assert conversation["values"] == []
    assert economic["values"] == []
    assert amortized["values"] == []
    assert "unavailable for this legacy session" in economic["empty_message"]
    assert payload["economic_metrics_available"] is False
    assert payload["economic_decision_count"] == 0
    assert payload["amortized_decision_count"] == 0
    assert "V1.1/legacy session" in amortized["empty_message"]
    assert "旧的 Token 节省图仍可正常查看" in conversation["empty_message"]
    assert payload["conversation_metric_count"] == 0
    assert payload["conversation_metric_missing_count"] == 3
