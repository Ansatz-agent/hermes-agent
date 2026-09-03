import json
import sqlite3
import uuid

import pytest

from plugins.context_engine.object_context.models import PendingLedgerAccrual
from plugins.context_engine.object_context.store import ObjectContextStore


def _attempt(number: int) -> str:
    return str(uuid.UUID(int=number))


def _register_delta(store: ObjectContextStore, delta_id: str, sequence: int):
    return store.register_delta(
        delta_id=delta_id,
        conversation_id="conv-a",
        session_id="session-a",
        turn_id=f"turn-{sequence}",
        kind="user",
        inference_id="",
        turn_sequence=sequence,
        raw_view=(
            {
                "role": "user",
                "content": (f"raw payload {delta_id} " * 20).strip(),
            },
        ),
    )


def test_request_observation_route_outcome_and_content_free_timeline(tmp_path):
    path = tmp_path / "objects.sqlite3"
    store = ObjectContextStore(path)
    pending_delta = _register_delta(store, "delta-pending", 1)
    other_delta = _register_delta(store, "delta-other", 2)
    route_hash = "a" * 64

    first = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(1),
        raw_delta_ids=[pending_delta.delta_id, other_delta.delta_id],
        route_namespace_hash=route_hash,
    )
    assert first.route_namespace_hash == route_hash
    assert first.outcome == "confirmed_success"
    ledger = store.upsert_pending_ledger(
        conversation_id="conv-a",
        delta_id=pending_delta.delta_id,
        entered_success_sequence=first.success_sequence,
        raw_tokens=pending_delta.raw_token_count,
        projected_tokens=pending_delta.raw_token_count - 3,
        estimator_version="rough-v1",
    )
    snapshot = PendingLedgerAccrual(
        delta_id=ledger.delta_id,
        gain_tokens=ledger.gain_tokens,
        ledger_generation=ledger.ledger_generation,
    )

    second_attempt = _attempt(2)
    second = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=second_attempt,
        raw_delta_ids=[pending_delta.delta_id, other_delta.delta_id],
        pending_accruals=[snapshot],
        route_namespace_hash=route_hash,
    )
    third = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(3),
        raw_delta_ids=[other_delta.delta_id],
        pending_accruals=[snapshot],
        route_namespace_hash=route_hash,
    )
    assert second.accrued_delta_ids == (pending_delta.delta_id,)
    assert third.skipped_pending_delta_ids == (pending_delta.delta_id,)

    replay = ObjectContextStore(path).confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=second_attempt,
        raw_delta_ids=[other_delta.delta_id, pending_delta.delta_id],
        pending_accruals=[snapshot],
        route_namespace_hash=route_hash,
    )
    assert replay.duplicate is True
    assert replay.route_namespace_hash == route_hash
    assert replay.outcome == "confirmed_success"

    with pytest.raises(RuntimeError, match="different observation"):
        store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=second_attempt,
            raw_delta_ids=[pending_delta.delta_id, other_delta.delta_id],
            pending_accruals=[snapshot],
            route_namespace_hash="b" * 64,
        )

    timeline = ObjectContextStore(path).request_observation_timeline("conv-a")
    assert len(timeline) == 3
    assert set(timeline[0]) == {
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
    assert timeline[0]["raw_delta_count"] == 2
    assert timeline[0]["accrued_delta_count"] == 0
    assert timeline[0]["newly_eligible_delta_count"] == 2
    assert timeline[1]["accrued_delta_count"] == 1
    assert timeline[1]["skipped_pending_delta_count"] == 0
    assert timeline[2]["raw_delta_count"] == 1
    assert timeline[2]["skipped_pending_delta_count"] == 1
    assert [row["success_sequence"] for row in timeline] == [1, 2, 3]
    assert all(row["route_namespace_hash"] == route_hash for row in timeline)
    assert all(row["outcome"] == "confirmed_success" for row in timeline)
    assert not any(
        "json" in key or "content" in key or "payload" in key
        for row in timeline
        for key in row
    )


@pytest.mark.parametrize(
    "route_hash",
    ["not-a-hash", "A" * 64, "a" * 63, "a" * 65],
)
def test_request_observation_rejects_non_sha256_route_hash(tmp_path, route_hash):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")

    with pytest.raises(ValueError, match="route_namespace_hash"):
        store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=_attempt(10),
            raw_delta_ids=[],
            route_namespace_hash=route_hash,
        )

    assert store.latest_success_sequence("conv-a") == 0
    assert store.request_observation_timeline("conv-a") == []


def test_legacy_request_observation_migrates_to_default_route_and_outcome(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    attempt_id = _attempt(20)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '4');
            CREATE TABLE request_observations (
                request_attempt_id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                success_sequence INTEGER NOT NULL,
                exposure_request_sequence INTEGER NOT NULL,
                min_raw_exposures INTEGER NOT NULL DEFAULT 1,
                raw_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                pending_accruals_json TEXT NOT NULL DEFAULT '[]',
                raw_exposed_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                accrued_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                skipped_pending_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                newly_eligible_delta_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                UNIQUE (conversation_id, success_sequence)
            );
            """
        )
        conn.execute(
            "INSERT INTO request_observations("
            "request_attempt_id, conversation_id, success_sequence, "
            "exposure_request_sequence, min_raw_exposures, raw_delta_ids_json, "
            "pending_accruals_json, raw_exposed_delta_ids_json, "
            "accrued_delta_ids_json, skipped_pending_delta_ids_json, "
            "newly_eligible_delta_ids_json, created_at) "
            "VALUES (?, 'conv-a', 1, 7, 1, ?, '[]', ?, '[]', '[]', ?, 3.5)",
            (
                attempt_id,
                json.dumps(["delta-a"]),
                json.dumps(["delta-a"]),
                json.dumps(["delta-a"]),
            ),
        )

    migrated = ObjectContextStore(path)
    [row] = migrated.request_observation_timeline("conv-a")
    assert row == {
        "request_attempt_id": attempt_id,
        "success_sequence": 1,
        "exposure_request_sequence": 7,
        "route_namespace_hash": "",
        "outcome": "confirmed_success",
        "raw_delta_count": 1,
        "accrued_delta_count": 0,
        "skipped_pending_delta_count": 0,
        "newly_eligible_delta_count": 1,
        "created_at": 3.5,
    }

    ObjectContextStore(path)
    with sqlite3.connect(path) as conn:
        columns = [
            item[1] for item in conn.execute("PRAGMA table_info(request_observations)")
        ]
        stored = conn.execute(
            "SELECT route_namespace_hash, outcome FROM request_observations"
        ).fetchone()
    assert columns.count("route_namespace_hash") == 1
    assert columns.count("outcome") == 1
    assert stored == ("", "confirmed_success")
