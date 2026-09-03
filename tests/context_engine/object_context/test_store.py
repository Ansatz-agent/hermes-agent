import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent.context_engine import ContextDelta
from plugins.context_engine.object_context.detection import detect_delta_objects
from plugins.context_engine.object_context.models import (
    ActivityState,
    DeltaState,
    ObjectType,
)
from plugins.context_engine.object_context.store import ObjectContextStore


def _decision_record(
    epoch_id: str,
    *,
    kind: str = "flush",
    mode: str = "normal",
    reason: str = "FLUSH_NET_POSITIVE",
    delta_ids=(),
    object_refs=(),
):
    return {
        "projection_epoch_id": epoch_id,
        "conversation_id": "conv-a",
        "session_id": "session-a",
        "request_sequence": 2,
        "decision_kind": kind,
        "decision_mode": mode,
        "decision_reason": reason,
        "candidate_count": len(delta_ids),
        "member_delta_ids": delta_ids,
        "member_object_refs": object_refs,
        "earliest_changed_delta_id": delta_ids[0] if delta_ids else "",
        "baseline_prompt_tokens": 2_000 if delta_ids else None,
        "candidate_prompt_tokens": 500 if delta_ids else None,
        "gross_tokens_removed": 1_500 if delta_ids else None,
        "card_or_receipt_tokens": 40 if delta_ids else None,
        "baseline_reusable_prefix_tokens": 1_000 if delta_ids else None,
        "candidate_reusable_prefix_tokens": 800 if delta_ids else None,
        "cache_tokens_invalidated": 200 if delta_ids else None,
        "cache_penalty_equivalent_tokens": 180.0 if delta_ids else None,
        "known_summary_cost_equivalent_tokens": 0.0 if delta_ids else None,
        "net_saving_equivalent_tokens": 1_320.0 if delta_ids else None,
        "net_saving_usd": None,
        "cache_read_weight": 0.1,
        "cache_write_weight": 1.0,
        "pricing_source": "test",
        "pricing_version": "v1",
        "estimator_source": "rough_message_estimator",
    }


def _registered_json(store, *, delta_id="turn-1:user:0", conversation="conv-a"):
    content = '{"alpha": 1, "beta": [2, 3], "gamma": "value"}'
    message = {"role": "user", "content": content, "timestamp": 1.0}
    observed = ContextDelta(
        delta_id=delta_id,
        kind="user",
        conversation_id=conversation,
        session_id="session-a",
        turn_id="turn-1",
        sequence=0,
        messages=(message,),
    )
    delta = store.register_delta(
        delta_id=observed.delta_id,
        conversation_id=conversation,
        session_id="session-a",
        turn_id="turn-1",
        kind="user",
        inference_id="",
        turn_sequence=0,
        raw_view=observed.messages,
    )
    [detected] = detect_delta_objects(observed, min_tokens=1)
    record = store.register_object(
        conversation_id=conversation,
        session_id="session-a",
        delta=delta,
        detected=detected,
    )
    return observed, delta, detected, record


def _registered_multi_object_delta(store):
    messages = (
        {
            "role": "tool",
            "content": '{"alpha":1,"items":[1,2,3]}',
            "tool_call_id": "call-a",
            "timestamp": 1.0,
        },
        {
            "role": "tool",
            "content": '{"beta":2,"items":[4,5,6]}',
            "tool_call_id": "call-b",
            "timestamp": 2.0,
        },
    )
    observed = ContextDelta(
        delta_id="turn-1:inference:1",
        kind="inference",
        conversation_id="conv-a",
        session_id="session-a",
        turn_id="turn-1",
        sequence=1,
        messages=messages,
        inference_id="inference-1",
    )
    delta = store.register_delta(
        delta_id=observed.delta_id,
        conversation_id=observed.conversation_id,
        session_id=observed.session_id,
        turn_id=observed.turn_id,
        kind=observed.kind,
        inference_id=observed.inference_id,
        turn_sequence=observed.sequence,
        raw_view=observed.messages,
    )
    detected = detect_delta_objects(observed, min_tokens=1)
    records = [
        store.register_object(
            conversation_id=observed.conversation_id,
            session_id=observed.session_id,
            delta=delta,
            detected=item,
        )
        for item in detected
    ]
    return observed, delta, detected, records


def test_v1_database_migrates_exposure_fields_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    raw_view = '[{"content":"legacy raw","role":"user"}]'
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '1');
            CREATE TABLE deltas (
                delta_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                inference_id TEXT NOT NULL DEFAULT '',
                turn_sequence INTEGER NOT NULL,
                global_sequence INTEGER NOT NULL,
                raw_token_count INTEGER NOT NULL,
                state TEXT NOT NULL,
                raw_view_json TEXT NOT NULL,
                compressed_view_json TEXT,
                object_refs_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                compressed_at REAL,
                failure_error TEXT NOT NULL DEFAULT '',
                UNIQUE (conversation_id, global_sequence)
            );
            """
        )
        conn.execute(
            "INSERT INTO deltas(delta_id, conversation_id, session_id, "
            "turn_id, kind, turn_sequence, global_sequence, raw_token_count, "
            "state, raw_view_json, created_at) "
            "VALUES('legacy:user:0', 'legacy', 'segment', 'legacy-turn', "
            "'user', 0, 1, 3, 'hot', ?, 1.0)",
            (raw_view,),
        )

    first = ObjectContextStore(path)
    migrated = first.get_delta("legacy:user:0")
    assert migrated is not None
    assert migrated.raw_seen_count == 0
    assert migrated.first_seen_request_sequence is None
    assert migrated.last_seen_request_sequence is None
    assert migrated.projection_epoch_id == ""
    assert migrated.projected_at_request_sequence is None

    second = ObjectContextStore(path)
    reopened = second.get_delta("legacy:user:0")
    assert reopened == migrated
    with sqlite3.connect(path) as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "5"
        columns = [row[1] for row in conn.execute("PRAGMA table_info(deltas)")]
        decision_columns = [
            row[1] for row in conn.execute("PRAGMA table_info(projection_epochs)")
        ]
    assert columns.count("raw_seen_count") == 1
    assert columns.count("projection_epoch_id") == 1
    assert decision_columns.count("decision_mode") == 1
    assert decision_columns.count("card_or_receipt_tokens") == 1
    assert reopened.raw_view == ({"content": "legacy raw", "role": "user"},)


def test_raw_exposure_confirmation_is_durable_and_idempotent(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, delta, _, _ = _registered_json(store)

    assert store.mark_raw_deltas_seen(
        [delta.delta_id, delta.delta_id], request_sequence=7
    ) == (delta.delta_id,)
    assert store.mark_raw_deltas_seen(
        [delta.delta_id], request_sequence=7
    ) == (delta.delta_id,)
    first = store.get_delta(delta.delta_id)
    assert first is not None
    assert first.raw_seen_count == 1
    assert first.first_seen_request_sequence == 7
    assert first.last_seen_request_sequence == 7

    store.mark_raw_deltas_seen([delta.delta_id], request_sequence=9)
    reopened = ObjectContextStore(store.path).get_delta(delta.delta_id)
    assert reopened is not None
    assert reopened.raw_seen_count == 2
    assert reopened.first_seen_request_sequence == 7
    assert reopened.last_seen_request_sequence == 9
    assert store.max_request_sequence("conv-a") == 9


def test_v2_to_v3_migration_preserves_raw_blob_and_card_bytes(tmp_path):
    path = tmp_path / "legacy-v2.sqlite3"
    store = ObjectContextStore(path)
    _, delta, _, record = _registered_json(store)
    store.set_delta_states(
        {delta.delta_id: DeltaState.COMPRESSION_ELIGIBLE},
        expected=DeltaState.HOT,
    )
    stable_card = "<OBJECT_CARD>{\"stable\":true}</OBJECT_CARD>"
    store.publish_cards_and_compressed_delta(
        delta_id=delta.delta_id,
        cards=[(record.object_ref, "stable", stable_card, {"json": True})],
        compressed_view=[{"role": "user", "content": stable_card}],
    )
    with sqlite3.connect(path) as conn:
        before = {
            "raw": conn.execute(
                "SELECT raw_view_json FROM deltas WHERE delta_id = ?",
                (delta.delta_id,),
            ).fetchone()[0],
            "compressed": conn.execute(
                "SELECT compressed_view_json FROM deltas WHERE delta_id = ?",
                (delta.delta_id,),
            ).fetchone()[0],
            "card": conn.execute(
                "SELECT card_text FROM object_versions WHERE object_ref = ?",
                (record.object_ref,),
            ).fetchone()[0],
            "blob": conn.execute(
                "SELECT content FROM blobs WHERE sha256 = ?",
                (record.sha256,),
            ).fetchone()[0],
        }
        conn.execute(
            "UPDATE schema_meta SET value = '2' WHERE key = 'schema_version'"
        )
        conn.execute("DROP TABLE projection_epochs")

    reopened = ObjectContextStore(path)

    with sqlite3.connect(path) as conn:
        after = {
            "raw": conn.execute(
                "SELECT raw_view_json FROM deltas WHERE delta_id = ?",
                (delta.delta_id,),
            ).fetchone()[0],
            "compressed": conn.execute(
                "SELECT compressed_view_json FROM deltas WHERE delta_id = ?",
                (delta.delta_id,),
            ).fetchone()[0],
            "card": conn.execute(
                "SELECT card_text FROM object_versions WHERE object_ref = ?",
                (record.object_ref,),
            ).fetchone()[0],
            "blob": conn.execute(
                "SELECT content FROM blobs WHERE sha256 = ?",
                (record.sha256,),
            ).fetchone()[0],
        }
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0] == "5"
    assert after == before
    assert reopened.projection_decisions("conv-a") == []


def test_content_free_wait_decision_round_trips_and_rejects_payload_fields(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    record = _decision_record(
        "decision-wait",
        kind="wait",
        reason="WAIT_NO_BASELINE",
    )

    store.record_projection_decision(record)

    [stored] = ObjectContextStore(store.path).projection_decisions("conv-a")
    assert stored["projection_epoch_id"] == "decision-wait"
    assert stored["decision_kind"] == "wait"
    assert stored["decision_mode"] == "normal"
    assert stored["decision_reason"] == "WAIT_NO_BASELINE"
    assert stored["member_delta_ids"] == []
    assert stored["member_object_refs"] == []
    assert stored["baseline_prompt_tokens"] is None
    assert "prompt" not in stored
    assert "message" not in stored
    assert "content" not in stored

    with pytest.raises(ValueError, match="unsupported fields"):
        store.record_projection_decision({**record, "prompt": "secret payload"})


def test_exact_blob_is_hash_verified_and_conversation_authorized(tmp_path):
    store = ObjectContextStore(tmp_path / "context" / "objects.sqlite3")
    _, _, _, record = _registered_json(store)

    resolved = store.get_object("conv-a", record.object_ref)
    assert resolved is not None
    assert resolved.content == record.content
    assert resolved.sha256 == record.sha256
    assert store.get_object("conv-b", record.object_ref) is None
    assert store.object_exists(record.object_ref) is True


def test_retrieval_totals_are_scoped_to_exact_main_turns(tmp_path):
    store = ObjectContextStore(tmp_path / "context" / "objects.sqlite3")
    _, delta, _, record = _registered_json(store)
    store.mount_retrieval(
        conversation_id="conv-a",
        turn_id="turn-main",
        object_ref=record.object_ref,
        tool_call_id="call-main",
        reason="main request",
        mounted_at_delta=delta.global_sequence,
    )
    store.mount_retrieval(
        conversation_id="conv-a",
        turn_id="turn-background",
        object_ref=record.object_ref,
        tool_call_id="call-background",
        reason="background review",
        mounted_at_delta=delta.global_sequence,
    )

    assert store.retrieval_totals_for_turns("conv-a", ["turn-main"]) == {
        "retrieval_count": 1,
        "retrieved_tokens": record.token_count,
    }
    assert store.retrieval_totals_for_turns(
        "conv-a", ["turn-main", "turn-main", ""]
    ) == {
        "retrieval_count": 1,
        "retrieved_tokens": record.token_count,
    }
    assert store.retrieval_totals_for_turns("conv-a", []) == {
        "retrieval_count": 0,
        "retrieved_tokens": 0,
    }


def test_status_separates_request_projection_metrics_from_delta_metrics(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")

    # One-time Card construction for a Delta: retained in the compatibility
    # aggregate, but excluded from cumulative outbound-request savings.
    store.record_metric("conv-a", "tokens_saved", 500, delta_id="turn-1:user:0")
    store.record_metric("conv-a", "raw_context_tokens", 800, delta_id="turn-1:user:0")

    for raw, rendered, saved, hot_tail in (
        (1_000, 400, 600, 250),
        (2_000, 500, 1_500, 300),
    ):
        store.record_metric("conv-a", "raw_context_tokens", raw)
        store.record_metric("conv-a", "rendered_context_tokens", rendered)
        store.record_metric("conv-a", "raw_conversation_tokens", raw)
        store.record_metric("conv-a", "rendered_conversation_tokens", rendered)
        store.record_metric("conv-a", "conversation_tokens_saved", saved)
        store.record_metric(
            "conv-a", "conversation_compression_ratio", saved / raw
        )
        store.record_metric("conv-a", "tokens_saved", saved)
        store.record_metric("conv-a", "compression_ratio", saved / raw)
        store.record_metric("conv-a", "hot_tail_tokens", hot_tail)

    status = store.aggregate_status("conv-a")

    assert status["metric_totals"]["tokens_saved"] == 2_600
    assert status["request_projection_count"] == 2
    assert status["request_metric_totals"]["raw_context_tokens"] == 3_000
    assert status["request_metric_totals"]["rendered_context_tokens"] == 900
    assert status["request_metric_totals"]["tokens_saved"] == 2_100
    assert status["request_metric_totals"]["conversation_tokens_saved"] == 2_100
    assert status["request_metric_averages"]["tokens_saved"] == 1_050
    assert status["last_request_metrics"] == {
        "raw_context_tokens": 2_000,
        "rendered_context_tokens": 500,
        "raw_conversation_tokens": 2_000,
        "rendered_conversation_tokens": 500,
        "conversation_tokens_saved": 1_500,
        "conversation_compression_ratio": 0.75,
        "tokens_saved": 1_500,
        "compression_ratio": 0.75,
        "hot_tail_tokens": 300,
    }


def test_projection_timeline_groups_atomic_metrics_and_marks_legacy_rows(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    store.record_metrics(
        "conv-a",
        {
            "raw_context_tokens": 100,
            "rendered_context_tokens": 40,
            "raw_conversation_tokens": 90,
            "rendered_conversation_tokens": 30,
            "conversation_tokens_saved": 60,
            "conversation_compression_ratio": 2 / 3,
            "tokens_saved": 60,
            "projection_latency_ms": 1.25,
        },
        metadata={
            "event": "request_projection",
            "projection_id": "projection-1",
            "projection_sequence": 1,
            "turn_id": "turn-a",
            "session_id": "session-a",
        },
    )
    store.record_metrics(
        "conv-a",
        {
            "raw_context_tokens": 200,
            "rendered_context_tokens": 75,
            "raw_conversation_tokens": 175,
            "rendered_conversation_tokens": 50,
            "conversation_tokens_saved": 125,
            "conversation_compression_ratio": 5 / 7,
            "tokens_saved": 125,
            "projection_latency_ms": 2.5,
        },
        metadata={
            "event": "request_projection",
            "projection_id": "projection-2",
            "projection_sequence": 2,
            "turn_id": "turn-a",
            "session_id": "session-a",
        },
    )
    # V1 rows written before projection identity existed remain usable only for
    # project-level token charts; the monitor must not invent turn or time data.
    store.record_metric("conv-a", "raw_context_tokens", 20)
    store.record_metric("conv-a", "rendered_context_tokens", 10)
    store.record_metric("conv-a", "tokens_saved", 10)

    timeline = store.request_projection_timeline("conv-a")

    assert len(timeline) == 3
    assert timeline[0] == {
        "projection_id": "projection-1",
        "projection_sequence": 1,
        "turn_id": "turn-a",
        "session_id": "session-a",
        "created_at": timeline[0]["created_at"],
        "legacy": False,
        "metrics": {
            "raw_context_tokens": 100.0,
            "rendered_context_tokens": 40.0,
            "raw_conversation_tokens": 90.0,
            "rendered_conversation_tokens": 30.0,
            "conversation_tokens_saved": 60.0,
            "conversation_compression_ratio": 2 / 3,
            "tokens_saved": 60.0,
            "projection_latency_ms": 1.25,
        },
    }
    assert timeline[1]["projection_id"] == "projection-2"
    assert timeline[1]["turn_id"] == "turn-a"
    assert timeline[1]["metrics"]["tokens_saved"] == 125.0
    assert timeline[2]["legacy"] is True
    assert timeline[2]["turn_id"] == ""
    assert timeline[2]["metrics"] == {
        "raw_context_tokens": 20.0,
        "rendered_context_tokens": 10.0,
        "tokens_saved": 10.0,
    }

    rows = [
        row
        for row in store.metrics("conv-a")
        if json.loads(row["metadata_json"] or "{}").get("projection_id")
        == "projection-1"
    ]
    assert len(rows) == 8
    assert len({row["created_at"] for row in rows}) == 1


def test_cache_usage_timeline_groups_exact_atomic_requests_and_skips_legacy_rows(
    tmp_path,
):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    for sequence, (prompt, uncached, cache_read, cache_write) in enumerate(
        ((1_000, 1_000, 0, 0), (1_250, 200, 1_000, 50)), start=1
    ):
        store.record_metrics(
            "conv-a",
            {
                "prompt_tokens": prompt,
                "uncached_input_tokens": uncached,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "prompt_cache_hit_ratio": cache_read / prompt,
            },
            metadata={
                "event": "provider_cache_usage",
                "cache_request_id": f"session-a:cache:{sequence}",
                "cache_request_sequence": sequence,
                "turn_id": "turn-a",
                "session_id": "session-a",
            },
        )
    # Pre-monitor cache telemetry has neither an atomic request identity nor a
    # trustworthy total-prompt denominator, so it must not become a chart point.
    store.record_metric("conv-a", "cache_read_tokens", 999)
    store.record_metric("conv-a", "prompt_cache_hit_ratio", 1.0)

    timeline = store.cache_usage_timeline("conv-a")

    assert len(timeline) == 2
    assert timeline[0]["request_sequence"] == 1
    assert timeline[1] == {
        "cache_request_id": "session-a:cache:2",
        "request_sequence": 2,
        "turn_id": "turn-a",
        "session_id": "session-a",
        "created_at": timeline[1]["created_at"],
        "metrics": {
            "prompt_tokens": 1_250.0,
            "uncached_input_tokens": 200.0,
            "cache_read_tokens": 1_000.0,
            "cache_write_tokens": 50.0,
            "prompt_cache_hit_ratio": 0.8,
        },
    }


def test_projection_conversations_lists_every_root_in_latest_first_order(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    for conversation_id, saved in (("conv-old", 10), ("conv-new", 20)):
        store.record_metrics(
            conversation_id,
            {
                "raw_context_tokens": 100,
                "rendered_context_tokens": 100 - saved,
                "tokens_saved": saved,
            },
            metadata={
                "event": "request_projection",
                "projection_id": f"projection-{conversation_id}",
            },
        )
    store.record_metric("conv-new", "tokens_saved", 5)
    store.record_metric("not-a-projection", "retrieved_tokens", 999)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE metrics SET created_at = 100 WHERE conversation_id = 'conv-old'"
        )
        conn.execute(
            "UPDATE metrics SET created_at = 200 WHERE conversation_id = 'conv-new'"
        )

    conversations = store.request_projection_conversations()

    assert conversations == [
        {
            "conversation_id": "conv-new",
            "projection_count": 2,
            "first_projection_at": 200.0,
            "last_projection_at": 200.0,
        },
        {
            "conversation_id": "conv-old",
            "projection_count": 1,
            "first_projection_at": 100.0,
            "last_projection_at": 100.0,
        },
    ]


def test_physical_dedup_does_not_merge_logical_identity(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    first = _registered_json(store, delta_id="turn-1:user:0")[3]
    second = _registered_json(store, delta_id="turn-2:user:0")[3]

    assert first.object_ref != second.object_ref
    assert first.object_id != second.object_id
    assert first.sha256 == second.sha256
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM logical_objects").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM object_occurrences").fetchone()[0] == 2
        )


def test_multi_object_delta_refs_are_rebuilt_from_authoritative_occurrences(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, delta, detected, records = _registered_multi_object_delta(store)

    expected_refs = tuple(record.object_ref for record in records)
    assert len(detected) == len(expected_refs) == 2
    assert store.get_delta(delta.delta_id).object_refs == expected_refs
    assert (
        tuple(row["object_ref"] for row in store.occurrences_for_delta(delta.delta_id))
        == expected_refs
    )

    # Idempotently encountering an existing occurrence must retain the full
    # authoritative list rather than reintroducing the caller's stale snapshot.
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE deltas SET object_refs_json = ? WHERE delta_id = ?",
            (f'["{records[-1].object_ref}"]', delta.delta_id),
        )
    store.register_object(
        conversation_id="conv-a",
        session_id="session-a",
        delta=delta,
        detected=detected[0],
    )
    assert store.get_delta(delta.delta_id).object_refs == expected_refs


def test_store_startup_repairs_legacy_delta_object_ref_cache_once(tmp_path):
    path = tmp_path / "objects.sqlite3"
    store = ObjectContextStore(path)
    _, delta, _, records = _registered_multi_object_delta(store)
    expected_refs = tuple(record.object_ref for record in records)
    empty_delta = store.register_delta(
        delta_id="turn-2:user:0",
        conversation_id="conv-a",
        session_id="session-a",
        turn_id="turn-2",
        kind="user",
        inference_id="",
        turn_sequence=0,
        raw_view=({"role": "user", "content": "ordinary prose", "timestamp": 3.0},),
    )

    with sqlite3.connect(path) as conn:
        raw_snapshot = conn.execute(
            "SELECT delta_id, raw_view_json, compressed_view_json "
            "FROM deltas ORDER BY delta_id"
        ).fetchall()
        occurrence_snapshot = conn.execute(
            "SELECT occurrence_key, delta_id, object_ref, message_key, "
            "span_start, span_end FROM object_occurrences ORDER BY occurrence_key"
        ).fetchall()
        version_snapshot = conn.execute(
            "SELECT object_ref, object_id, version, sha256 "
            "FROM object_versions ORDER BY object_ref"
        ).fetchall()
        conn.execute(
            "UPDATE deltas SET object_refs_json = ? WHERE delta_id IN (?, ?)",
            (
                '["object://obj_000000000000000000000000@v1"]',
                delta.delta_id,
                empty_delta.delta_id,
            ),
        )
        conn.execute(
            "DELETE FROM schema_meta "
            "WHERE key = 'object_refs_json_rebuilt_from_occurrences_v1'"
        )

    reopened = ObjectContextStore(path)

    assert reopened.get_delta(delta.delta_id).object_refs == expected_refs
    assert reopened.get_delta(empty_delta.delta_id).object_refs == ()
    with sqlite3.connect(path) as conn:
        marker = conn.execute(
            "SELECT value FROM schema_meta "
            "WHERE key = 'object_refs_json_rebuilt_from_occurrences_v1'"
        ).fetchone()
        assert (
            conn.execute(
                "SELECT delta_id, raw_view_json, compressed_view_json "
                "FROM deltas ORDER BY delta_id"
            ).fetchall()
            == raw_snapshot
        )
        assert (
            conn.execute(
                "SELECT occurrence_key, delta_id, object_ref, message_key, "
                "span_start, span_end FROM object_occurrences ORDER BY occurrence_key"
            ).fetchall()
            == occurrence_snapshot
        )
        assert (
            conn.execute(
                "SELECT object_ref, object_id, version, sha256 "
                "FROM object_versions ORDER BY object_ref"
            ).fetchall()
            == version_snapshot
        )
    assert marker == ("1",)


def test_explicit_version_is_immutable_and_records_relations(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    observed, _, detected, first = _registered_json(store)
    second_delta = store.register_delta(
        delta_id="turn-2:inference:1",
        conversation_id="conv-a",
        session_id="session-a",
        turn_id="turn-2",
        kind="inference",
        inference_id="inference-2",
        turn_sequence=1,
        raw_view=({"role": "assistant", "content": detected.content + " "},),
    )
    revised = detected.__class__(**{
        **detected.__dict__,
        "content": detected.content + " ",
        "occurrence_key": detected.occurrence_key + "-revision",
    })
    second = store.register_object(
        conversation_id="conv-a",
        session_id="session-a",
        delta=second_delta,
        detected=revised,
        base_ref=first.object_ref,
        derived_from=(first.object_ref,),
    )

    assert second.object_id == first.object_id
    assert second.version == 2
    assert second.supersedes == first.object_ref
    assert second.derived_from == (first.object_ref,)
    assert (
        store.get_object("conv-a", first.object_ref).content
        == observed.messages[0]["content"]
    )


def test_runtime_version_api_does_not_fabricate_conversation_delta(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, _, _, first = _registered_json(store)
    before = store.list_deltas("conv-a")

    second = store.create_object_version(
        conversation_id="conv-a",
        base_ref=first.object_ref,
        content='{"alpha": 2}',
        object_type=ObjectType.STRUCTURED_DATA,
        name="updated.json",
        derived_from=(first.object_ref,),
    )

    assert store.list_deltas("conv-a") == before
    assert second.object_id == first.object_id
    assert second.version == 2
    assert second.supersedes == first.object_ref
    assert second.derived_from == (first.object_ref,)
    assert second.source_delta_id == ""
    assert store.get_object("conv-a", first.object_ref).content == first.content

    with pytest.raises(PermissionError):
        store.create_object_version(
            conversation_id="conv-b",
            base_ref=first.object_ref,
            content="{}",
            object_type=ObjectType.STRUCTURED_DATA,
        )


def test_atomic_batch_failure_leaves_delta_and_cards_unpublished(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, delta, _, record = _registered_json(store)
    store.set_delta_states(
        {delta.delta_id: DeltaState.COMPRESSION_ELIGIBLE}, expected=DeltaState.HOT
    )

    with pytest.raises(RuntimeError, match="Card target missing"):
        store.publish_compressed_batch(
            [
                (
                    delta.delta_id,
                    [
                        (
                            record.object_ref,
                            "valid",
                            "<OBJECT_CARD>{}</OBJECT_CARD>",
                            {},
                        ),
                        (
                            "object://obj_000000000000000000000000@v1",
                            "bad",
                            "bad",
                            {},
                        ),
                    ],
                    [{"role": "user", "content": "card"}],
                )
            ],
            projection_epoch_id="epoch-failed",
            projection_decision=_decision_record(
                "epoch-failed",
                delta_ids=(delta.delta_id,),
                object_refs=(record.object_ref,),
            ),
        )

    current = store.get_delta(delta.delta_id)
    assert current.state == DeltaState.COMPRESSION_ELIGIBLE
    assert current.compressed_view is None
    assert store.get_object("conv-a", record.object_ref).card_text == ""
    assert store.projection_decisions("conv-a") == []


def test_batch_state_cards_and_flush_decision_commit_in_one_epoch(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, delta, _, record = _registered_json(store)
    store.mark_raw_deltas_seen([delta.delta_id], request_sequence=1)
    card = "<OBJECT_CARD>{\"object_ref\":\"stable\"}</OBJECT_CARD>"

    store.publish_compressed_batch(
        [
            (
                delta.delta_id,
                [(record.object_ref, "stable", card, {"json": True})],
                [{"role": "user", "content": card}],
            )
        ],
        projection_epoch_id="epoch-success",
        request_sequence=2,
        min_raw_exposures=1,
        projection_decision=_decision_record(
            "epoch-success",
            delta_ids=(delta.delta_id,),
            object_refs=(record.object_ref,),
        ),
    )

    projected = store.get_delta(delta.delta_id)
    [decision] = store.projection_decisions("conv-a")
    assert projected.state == DeltaState.COMPRESSED
    assert projected.projection_epoch_id == "epoch-success"
    assert projected.projected_at_request_sequence == 2
    assert store.get_object("conv-a", record.object_ref).card_text == card
    assert decision["projection_epoch_id"] == projected.projection_epoch_id
    assert decision["member_delta_ids"] == [delta.delta_id]
    assert decision["member_object_refs"] == [record.object_ref]
    assert decision["card_or_receipt_tokens"] == 40


def test_concurrent_batch_publication_commits_exactly_one_epoch(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, delta, _, record = _registered_json(store)
    store.mark_raw_deltas_seen([delta.delta_id], request_sequence=1)
    card = "<OBJECT_CARD>{\"stable\":true}</OBJECT_CARD>"
    barrier = threading.Barrier(2)

    def publish(epoch_id):
        barrier.wait()
        try:
            store.publish_compressed_batch(
                [
                    (
                        delta.delta_id,
                        [(record.object_ref, "stable", card, {})],
                        [{"role": "user", "content": card}],
                    )
                ],
                projection_epoch_id=epoch_id,
                request_sequence=2,
                min_raw_exposures=1,
                projection_decision=_decision_record(
                    epoch_id,
                    delta_ids=(delta.delta_id,),
                    object_refs=(record.object_ref,),
                ),
            )
            return "committed"
        except RuntimeError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(publish, ("epoch-a", "epoch-b")))

    assert sorted(results) == ["committed", "rejected"]
    [decision] = store.projection_decisions("conv-a")
    projected = store.get_delta(delta.delta_id)
    assert decision["projection_epoch_id"] == projected.projection_epoch_id
    assert decision["projection_epoch_id"] in {"epoch-a", "epoch-b"}
    assert store.mark_deltas_failed_if_unprojected(
        [delta.delta_id], "losing concurrent publisher"
    ) == ()
    assert store.get_delta(delta.delta_id).state == DeltaState.COMPRESSED


def test_corrupt_blob_raises_hash_mismatch(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, _, _, record = _registered_json(store)
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE blobs SET content = ? WHERE sha256 = ?",
            (sqlite3.Binary(b"corrupt"), record.sha256),
        )
    with pytest.raises(RuntimeError, match="OBJECT_HASH_MISMATCH"):
        store.get_object("conv-a", record.object_ref)


def test_activity_grace_pin_reactivation_and_archive_are_delta_based(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    _, _, _, record = _registered_json(store)

    first = store.update_activity(
        conversation_id="conv-a",
        current_delta=1,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=2,
    )
    assert first[ActivityState.INACTIVE_CANDIDATE.value] == 1
    store.update_activity(
        conversation_id="conv-a",
        current_delta=2,
        active_refs={record.object_ref},
        recent_access_deltas=0,
        grace_deltas=2,
    )
    assert (
        store.get_object("conv-a", record.object_ref).activity_state
        == ActivityState.ACTIVE
    )

    assert store.pin("conv-a", record.object_ref, True)
    store.update_activity(
        conversation_id="conv-a",
        current_delta=10,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=2,
    )
    assert store.archive_evictable("conv-a") == []
    assert store.pin("conv-a", record.object_ref, False)
    store.update_activity(
        conversation_id="conv-a",
        current_delta=11,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=2,
    )
    store.update_activity(
        conversation_id="conv-a",
        current_delta=13,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=2,
    )
    assert store.archive_evictable("conv-a") == [record.object_ref]
    archived = store.get_object("conv-a", record.object_ref)
    assert archived.activity_state == ActivityState.ARCHIVED
    assert archived.location.value == "cold_archive"
    assert archived.content == record.content
    assert store.mark_accessed("conv-a", record.object_ref, at_delta=14)
    restored = store.get_object("conv-a", record.object_ref)
    assert restored.activity_state == ActivityState.ACTIVE
    assert restored.location.value == "working_memory"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_store_uses_private_best_effort_permissions(tmp_path):
    store = ObjectContextStore(tmp_path / "private" / "objects.sqlite3")
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700


def test_unknown_schema_version_fails_closed(tmp_path):
    path = tmp_path / "objects.sqlite3"
    store = ObjectContextStore(path)
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(RuntimeError, match="Unsupported Object Context V1 schema"):
        ObjectContextStore(path)
