import math
import sqlite3
import uuid

import pytest

from plugins.context_engine.object_context.store import ObjectContextStore


V12_FIELDS = (
    "request_attempt_id",
    "policy_version",
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


def _base_record(epoch_id: str = "epoch-v12"):
    return {
        "projection_epoch_id": epoch_id,
        "conversation_id": "conv-a",
        "session_id": "session-a",
        "request_sequence": 9,
        "decision_kind": "flush",
        "decision_mode": "amortized",
        "decision_reason": "FLUSH_AMORTIZED_CROSSING",
        "candidate_count": 2,
        "member_delta_ids": ("delta-a", "delta-b"),
        "member_object_refs": (),
        "earliest_changed_delta_id": "delta-a",
        "baseline_prompt_tokens": 10_000,
        "candidate_prompt_tokens": 8_500,
        "gross_tokens_removed": 1_500,
        "card_or_receipt_tokens": 100,
        "baseline_reusable_prefix_tokens": 8_960,
        "candidate_reusable_prefix_tokens": 7_936,
        "cache_tokens_invalidated": 1_024,
        "cache_penalty_equivalent_tokens": 921.6,
        "known_summary_cost_equivalent_tokens": 0.0,
        "net_saving_equivalent_tokens": 578.4,
        "net_saving_usd": None,
        "cache_read_weight": 0.10,
        "cache_write_weight": 1.0,
        "pricing_source": "configured-fallback",
        "pricing_version": "test-v1",
        "estimator_source": "rough-message-estimator",
        "request_attempt_id": str(uuid.UUID(int=42)),
        "policy_version": "1.2",
        "batch_policy": "dynamic",
        "fixed_batch_size": 4,
        "baseline_state": "known",
        "cache_granularity_tokens": 128,
        "hot_underexposed_count": 1,
        "hot_seen_delta_count": 3,
        "hot_seen_bucket_count": 2,
        "hot_tail_tokens": 4_000,
        "hot_overflow_tokens": 0,
        "hot_start_token_offset": 6_000,
        "pending_delta_count": 2,
        "pending_bucket_count": 2,
        "pending_raw_tokens": 2_000,
        "pending_gain_tokens": 1_500,
        "wait_area_token_requests": 7_716,
        "wait_loss_now": 771.6,
        "wait_loss_increment": 150.0,
        "wait_loss_projected": 921.6,
        "shared_cached_hot_tokens": 1_024,
        "shared_overhead_equivalent_tokens": 921.6,
        "crossing_margin": 0.0,
        "emergency_triggered": False,
        "pending_count_over": False,
        "pending_tokens_over": False,
        "amortized_crossed": True,
        "immediate_crossed": True,
        "amortized_cache_read_weight": 0.10,
        "amortized_baseline_prompt_tokens": 10_000,
        "amortized_candidate_prompt_tokens": 8_500,
        "amortized_baseline_reusable_prefix_tokens": 8_960,
        "amortized_candidate_reusable_prefix_tokens": 7_936,
        "immediate_cache_penalty_equivalent_tokens": 921.6,
        "immediate_net_saving_equivalent_tokens": 578.4,
        "immediate_net_saving_usd": None,
        "immediate_cache_read_weight": 0.10,
        "immediate_cache_write_weight": 1.0,
        "immediate_pricing_source": "configured_fallback",
        "immediate_pricing_version": "test-v1",
    }


def test_legacy_projection_epoch_migrates_with_null_v12_fields(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '3');
            CREATE TABLE projection_epochs (
                projection_epoch_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                request_sequence INTEGER NOT NULL,
                decision_kind TEXT NOT NULL,
                decision_mode TEXT NOT NULL DEFAULT 'normal',
                decision_reason TEXT NOT NULL,
                candidate_count INTEGER NOT NULL,
                member_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                member_object_refs_json TEXT NOT NULL DEFAULT '[]',
                earliest_changed_delta_id TEXT NOT NULL DEFAULT '',
                baseline_prompt_tokens INTEGER,
                candidate_prompt_tokens INTEGER,
                gross_tokens_removed INTEGER,
                card_or_receipt_tokens INTEGER,
                baseline_reusable_prefix_tokens INTEGER,
                candidate_reusable_prefix_tokens INTEGER,
                cache_tokens_invalidated INTEGER,
                cache_penalty_equivalent_tokens REAL,
                known_summary_cost_equivalent_tokens REAL,
                net_saving_equivalent_tokens REAL,
                net_saving_usd REAL,
                cache_read_weight REAL NOT NULL,
                cache_write_weight REAL NOT NULL,
                pricing_source TEXT NOT NULL,
                pricing_version TEXT NOT NULL DEFAULT '',
                estimator_source TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO projection_epochs("
            "projection_epoch_id, conversation_id, session_id, request_sequence, "
            "decision_kind, decision_mode, decision_reason, candidate_count, "
            "member_delta_ids_json, member_object_refs_json, "
            "earliest_changed_delta_id, baseline_prompt_tokens, "
            "candidate_prompt_tokens, gross_tokens_removed, "
            "card_or_receipt_tokens, baseline_reusable_prefix_tokens, "
            "candidate_reusable_prefix_tokens, cache_tokens_invalidated, "
            "cache_penalty_equivalent_tokens, "
            "known_summary_cost_equivalent_tokens, net_saving_equivalent_tokens, "
            "net_saving_usd, cache_read_weight, cache_write_weight, "
            "pricing_source, pricing_version, estimator_source, created_at) "
            "VALUES('legacy-epoch', 'conv-a', 'session-a', 7, 'wait', "
            "'normal', 'WAIT_BELOW_THRESHOLD', 1, '[\"delta-a\"]', '[]', "
            "'delta-a', 1000, 900, 100, 10, 800, 700, 100, 90.0, 0.0, "
            "10.0, NULL, 0.1, 1.0, 'legacy', 'v1', 'rough', 1.0)"
        )

    migrated = ObjectContextStore(path)
    [legacy] = migrated.projection_decisions("conv-a")
    assert legacy["projection_epoch_id"] == "legacy-epoch"
    assert legacy["decision_mode"] == "normal"
    assert legacy["member_delta_ids"] == ["delta-a"]
    assert legacy["net_saving_equivalent_tokens"] == 10.0
    assert all(legacy[field] is None for field in V12_FIELDS)

    ObjectContextStore(path)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "5"
        )
        columns = [
            row[1] for row in conn.execute("PRAGMA table_info(projection_epochs)")
        ]
    assert all(columns.count(field) == 1 for field in V12_FIELDS)


def test_v12_projection_telemetry_round_trips_with_typed_booleans(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    record = _base_record()

    store.record_projection_decision(record)

    [stored] = ObjectContextStore(store.path).projection_decisions("conv-a")
    for field in V12_FIELDS:
        assert stored[field] == record[field]
    for field in (
        "emergency_triggered",
        "pending_count_over",
        "pending_tokens_over",
        "amortized_crossed",
        "immediate_crossed",
    ):
        assert type(stored[field]) is bool
    assert stored["baseline_reusable_prefix_tokens"] == 8_960
    assert stored["candidate_reusable_prefix_tokens"] == 7_936
    assert stored["hot_start_token_offset"] == 6_000
    assert stored["shared_cached_hot_tokens"] == 1_024


@pytest.mark.parametrize("mode", ["normal", "emergency", "amortized", "capacity"])
def test_all_projection_decision_modes_are_supported(tmp_path, mode):
    store = ObjectContextStore(tmp_path / f"{mode}.sqlite3")
    record = _base_record(f"epoch-{mode}")
    record["decision_mode"] = mode

    store.record_projection_decision(record)

    [stored] = store.projection_decisions("conv-a")
    assert stored["decision_mode"] == mode


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("decision_mode", "fast-path", ValueError),
        ("decision_kind", "compress", ValueError),
        ("request_attempt_id", "not-a-uuid", ValueError),
        ("policy_version", "contains user text", ValueError),
        ("baseline_state", "warm-ish", ValueError),
        ("cache_granularity_tokens", 0, ValueError),
        ("cache_granularity_tokens", 1.5, TypeError),
        ("hot_tail_tokens", -1, ValueError),
        ("pending_delta_count", True, TypeError),
        ("wait_area_token_requests", math.nan, ValueError),
        ("wait_loss_now", -0.1, ValueError),
        ("crossing_margin", math.inf, ValueError),
        ("amortized_crossed", 1, TypeError),
        ("pending_bucket_count", 3, ValueError),
        ("pending_gain_tokens", 2_001, ValueError),
        ("hot_overflow_tokens", 4_001, ValueError),
        ("wait_loss_projected", 922.0, ValueError),
        ("shared_overhead_equivalent_tokens", 921.0, ValueError),
        ("crossing_margin", 1.0, ValueError),
        ("gross_tokens_removed", 1_499, ValueError),
        ("cache_tokens_invalidated", 1_023, ValueError),
        ("net_saving_equivalent_tokens", 999_999.0, ValueError),
        ("immediate_net_saving_equivalent_tokens", -123.0, ValueError),
        ("decision_reason", "arbitrary user payload", ValueError),
    ],
)
def test_v12_projection_telemetry_rejects_invalid_or_inconsistent_values(
    tmp_path, field, value, error
):
    store = ObjectContextStore(tmp_path / f"invalid-{field}.sqlite3")
    record = _base_record()
    record[field] = value

    with pytest.raises(error):
        store.record_projection_decision(record)

    assert store.projection_decisions("conv-a") == []


@pytest.mark.parametrize(
    "field",
    [
        "request_attempt_id",
        "amortized_baseline_prompt_tokens",
        "amortized_candidate_reusable_prefix_tokens",
        "wait_loss_projected",
        "immediate_net_saving_equivalent_tokens",
        "immediate_pricing_source",
    ],
)
def test_v12_projection_telemetry_rejects_missing_required_facts(tmp_path, field):
    store = ObjectContextStore(tmp_path / f"missing-{field}.sqlite3")
    record = _base_record()
    record.pop(field)

    with pytest.raises(ValueError, match="incomplete"):
        store.record_projection_decision(record)

    assert store.projection_decisions("conv-a") == []


def test_unknown_and_cold_baselines_cannot_claim_impossible_crossings(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    unknown = _base_record("unknown")
    unknown["baseline_state"] = "unknown"
    with pytest.raises(ValueError, match="unknown baseline"):
        store.record_projection_decision(unknown)

    cold = _base_record("cold")
    cold["baseline_state"] = "cold"
    with pytest.raises(ValueError, match="cold baseline"):
        store.record_projection_decision(cold)

    assert store.projection_decisions("conv-a") == []
