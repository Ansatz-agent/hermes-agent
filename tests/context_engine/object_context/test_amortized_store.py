import json
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.context_engine.object_context.models import (
    DeltaState,
    PendingLedgerAccrual,
)
from plugins.context_engine.object_context.store import ObjectContextStore


def _register_delta(
    store: ObjectContextStore,
    delta_id: str,
    *,
    conversation_id: str = "conv-a",
    sequence: int = 0,
):
    return store.register_delta(
        delta_id=delta_id,
        conversation_id=conversation_id,
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


def _attempt(number: int) -> str:
    return str(uuid.UUID(int=number))


def _promote(
    store: ObjectContextStore,
    delta,
    *,
    entered: int,
    gain: int = 2,
    bucket: int | None = None,
    reset: bool = False,
):
    assert delta.raw_token_count > gain
    return store.upsert_pending_ledger(
        conversation_id=delta.conversation_id,
        delta_id=delta.delta_id,
        entered_success_sequence=entered,
        bucket_sequence=bucket,
        raw_tokens=delta.raw_token_count,
        projected_tokens=delta.raw_token_count - gain,
        gain_tokens=gain,
        estimator_version="rough-v1",
        pending_reason="ECONOMIC_WAIT",
        reset_wait_area=reset,
    )


def test_v3_migration_is_idempotent_and_does_not_backfill_wait_area(tmp_path):
    path = tmp_path / "legacy-v3.sqlite3"
    raw_view = json.dumps([{"role": "user", "content": "legacy raw"}])
    compressed_view = json.dumps([{"role": "user", "content": "stable card"}])
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT INTO schema_meta(key, value) VALUES('schema_version', '3');
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
                raw_seen_count INTEGER NOT NULL DEFAULT 0,
                first_seen_request_sequence INTEGER,
                last_seen_request_sequence INTEGER,
                projection_epoch_id TEXT,
                projected_at_request_sequence INTEGER,
                UNIQUE (conversation_id, global_sequence)
            );
            """
        )
        conn.execute(
            "INSERT INTO deltas(delta_id, conversation_id, session_id, turn_id, "
            "kind, turn_sequence, global_sequence, raw_token_count, state, "
            "raw_view_json, created_at, raw_seen_count, "
            "first_seen_request_sequence, last_seen_request_sequence) "
            "VALUES('legacy-raw', 'conv-a', 'session-a', 'turn-1', 'user', "
            "0, 1, 10, 'hot', ?, 1.0, 7, 1, 7)",
            (raw_view,),
        )
        conn.execute(
            "INSERT INTO deltas(delta_id, conversation_id, session_id, turn_id, "
            "kind, turn_sequence, global_sequence, raw_token_count, state, "
            "raw_view_json, compressed_view_json, created_at, compressed_at, "
            "raw_seen_count, first_seen_request_sequence, "
            "last_seen_request_sequence, projection_epoch_id, "
            "projected_at_request_sequence) VALUES('legacy-compressed', "
            "'conv-a', 'session-a', 'turn-2', 'user', 1, 2, 10, "
            "'compressed', ?, ?, 1.0, 2.0, 1, 8, 8, 'legacy-epoch', 9)",
            (raw_view, compressed_view),
        )

    first = ObjectContextStore(path)
    raw = first.get_delta("legacy-raw")
    compressed = first.get_delta("legacy-compressed")
    assert raw is not None and compressed is not None
    assert raw.raw_seen_count == 7
    assert raw.first_seen_success_sequence is None
    assert raw.last_seen_success_sequence is None
    assert raw.eligibility_success_sequence is None
    assert first.list_pending_ledgers("conv-a") == []
    assert first.latest_success_sequence("conv-a") == 0
    assert compressed.compressed_view == ({"role": "user", "content": "stable card"},)
    assert compressed.projection_epoch_id == "legacy-epoch"

    # Simulate an interrupted pre-release V4 table creation. Column inventory,
    # not only schema_meta, must make reopening safe and repeatable.
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE pending_ledgers")
        conn.execute("DROP TABLE request_observations")
        conn.execute("CREATE TABLE pending_ledgers(delta_id TEXT)")
        conn.execute("CREATE TABLE request_observations(request_attempt_id TEXT)")
        conn.execute("UPDATE schema_meta SET value = '3' WHERE key = 'schema_version'")

    ObjectContextStore(path)
    ObjectContextStore(path)
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            == "5"
        )
        delta_columns = [row[1] for row in conn.execute("PRAGMA table_info(deltas)")]
        assert delta_columns.count("first_seen_success_sequence") == 1
        assert delta_columns.count("eligibility_success_sequence") == 1
        assert conn.execute("SELECT COUNT(*) FROM pending_ledgers").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM request_observations").fetchone()[0] == 0
        )

    first_v4_success = ObjectContextStore(path).confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(999),
        raw_delta_ids=["legacy-raw"],
    )
    # The V4 observation clock is independent of engine-local legacy request
    # watermarks and is dense from its first real confirmed success.
    assert first_v4_success.success_sequence == 1
    migrated_raw = ObjectContextStore(path).get_delta("legacy-raw")
    assert migrated_raw is not None
    assert migrated_raw.eligibility_success_sequence == 1
    assert ObjectContextStore(path).list_pending_ledgers("conv-a") == []


def test_threshold_success_bucket_and_idempotent_pending_upsert(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    delta = _register_delta(store, "delta-a")
    projected = delta.raw_token_count - 2

    with pytest.raises(RuntimeError, match="raw-unseen"):
        store.upsert_pending_ledger(
            conversation_id="conv-a",
            delta_id=delta.delta_id,
            entered_success_sequence=1,
            raw_tokens=delta.raw_token_count,
            projected_tokens=projected,
        )

    first = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(1),
        raw_delta_ids=[delta.delta_id],
        min_raw_exposures=2,
    )
    assert first.success_sequence == 1
    assert first.newly_eligible_delta_ids == ()

    second = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(2),
        raw_delta_ids=[delta.delta_id],
        min_raw_exposures=2,
    )
    assert second.success_sequence == 2
    assert second.newly_eligible_delta_ids == (delta.delta_id,)
    exposed = store.get_delta(delta.delta_id)
    assert exposed is not None
    assert exposed.first_seen_success_sequence == 1
    assert exposed.last_seen_success_sequence == 2
    assert exposed.eligibility_success_sequence == 2

    ledger = store.upsert_pending_ledger(
        conversation_id="conv-a",
        delta_id=delta.delta_id,
        entered_success_sequence=2,
        raw_tokens=delta.raw_token_count,
        projected_tokens=projected,
        estimator_version="rough-v1",
        pending_reason="WAIT",
        min_raw_exposures=2,
    )
    assert ledger.bucket_sequence == 2
    assert ledger.wait_area_token_requests == 0
    assert store.get_delta(delta.delta_id).state == DeltaState.COMPRESSION_ELIGIBLE

    third = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(3),
        raw_delta_ids=[delta.delta_id],
        pending_accruals=[PendingLedgerAccrual(delta.delta_id, ledger.gain_tokens, 1)],
        min_raw_exposures=2,
    )
    assert third.accrued_delta_ids == (delta.delta_id,)
    accrued = store.get_pending_ledger(delta.delta_id)
    assert accrued.wait_area_token_requests == ledger.gain_tokens

    repeated = store.upsert_pending_ledger(
        conversation_id="conv-a",
        delta_id=delta.delta_id,
        entered_success_sequence=2,
        raw_tokens=delta.raw_token_count,
        projected_tokens=projected,
        estimator_version="rough-v1",
        pending_reason="WAIT_AGAIN",
        min_raw_exposures=2,
    )
    assert repeated.entered_success_sequence == 2
    assert repeated.wait_area_token_requests == ledger.gain_tokens
    assert repeated.ledger_generation == 1

    with pytest.raises(ValueError, match="cannot change after promotion"):
        store.upsert_pending_ledger(
            conversation_id="conv-a",
            delta_id=delta.delta_id,
            entered_success_sequence=3,
            raw_tokens=delta.raw_token_count,
            projected_tokens=projected,
            estimator_version="rough-v1",
            min_raw_exposures=2,
        )

    with pytest.raises(RuntimeError, match="without reset"):
        store.upsert_pending_ledger(
            conversation_id="conv-a",
            delta_id=delta.delta_id,
            entered_success_sequence=2,
            raw_tokens=delta.raw_token_count,
            projected_tokens=projected - 1,
            estimator_version="rough-v2",
            min_raw_exposures=2,
        )

    reset = store.upsert_pending_ledger(
        conversation_id="conv-a",
        delta_id=delta.delta_id,
        entered_success_sequence=2,
        bucket_sequence=2,
        raw_tokens=delta.raw_token_count,
        projected_tokens=projected - 1,
        estimator_version="rough-v2",
        min_raw_exposures=2,
        reset_wait_area=True,
    )
    assert reset.ledger_generation == 2
    assert reset.wait_area_token_requests == ledger.gain_tokens
    assert reset.last_accrued_success_sequence == third.success_sequence

    # A request rendered before re-estimation still contributes the exact old
    # snapshotted gain when its accepted callback arrives afterward.
    old_snapshot = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(4),
        raw_delta_ids=[delta.delta_id],
        pending_accruals=[
            (delta.delta_id, ledger.gain_tokens, ledger.ledger_generation)
        ],
        min_raw_exposures=2,
    )
    assert old_snapshot.accrued_delta_ids == (delta.delta_id,)
    reset = store.get_pending_ledger(delta.delta_id)
    assert reset.wait_area_token_requests == 2 * ledger.gain_tokens
    assert store.list_pending_ledgers("conv-a", delta_ids=[]) == []
    assert store.list_pending_ledgers(
        "conv-a", delta_ids=["not-present", delta.delta_id]
    ) == [reset]


def test_threshold_increase_reprotects_pending_until_new_success_boundary(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    delta = _register_delta(store, "delta-threshold")
    first = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(50),
        raw_delta_ids=[delta.delta_id],
        min_raw_exposures=1,
    )
    assert first.success_sequence == 1
    ledger = _promote(store, delta, entered=1)
    assert ledger.bucket_sequence == 1

    assert (
        store.reset_delta_eligibility_if_underexposed(
            conversation_id="conv-a",
            delta_id=delta.delta_id,
            min_raw_exposures=2,
        )
        is True
    )
    protected = store.get_delta(delta.delta_id)
    assert protected is not None
    assert protected.state == DeltaState.HOT
    assert protected.eligibility_success_sequence is None
    assert store.get_pending_ledger(delta.delta_id) is None

    second = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(51),
        raw_delta_ids=[delta.delta_id],
        min_raw_exposures=2,
    )
    assert second.success_sequence == 2
    assert second.newly_eligible_delta_ids == (delta.delta_id,)
    assert store.get_delta(delta.delta_id).eligibility_success_sequence == 2
    promoted = _promote(store, delta, entered=2)
    assert promoted.entered_success_sequence == 2
    assert promoted.bucket_sequence == 2


def test_new_pending_ledger_rejects_future_or_fabricated_boundaries(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    delta = _register_delta(store, "delta-boundary")
    observed = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(60),
        raw_delta_ids=[delta.delta_id],
    )
    assert observed.success_sequence == 1

    with pytest.raises(ValueError, match="current success boundary"):
        _promote(store, delta, entered=2)
    with pytest.raises(ValueError, match="durable eligibility boundary"):
        _promote(store, delta, entered=1, bucket=2)

    ledger = _promote(store, delta, entered=1)
    assert ledger.entered_success_sequence == 1
    assert ledger.bucket_sequence == 1


def test_success_observation_is_exactly_once_across_restart(tmp_path):
    path = tmp_path / "objects.sqlite3"
    store = ObjectContextStore(path)
    pending_delta = _register_delta(store, "delta-pending", sequence=1)
    fresh_delta = _register_delta(store, "delta-fresh", sequence=2)
    initial = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(10),
        raw_delta_ids=[pending_delta.delta_id, fresh_delta.delta_id],
    )
    ledger = _promote(store, pending_delta, entered=initial.success_sequence, gain=3)

    attempt_id = _attempt(11)
    observed = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=attempt_id,
        raw_delta_ids=[pending_delta.delta_id, fresh_delta.delta_id],
        pending_accruals=[
            (pending_delta.delta_id, ledger.gain_tokens, ledger.ledger_generation)
        ],
    )
    assert observed.success_sequence == 2
    assert observed.duplicate is False
    assert observed.raw_exposed_delta_ids == (
        fresh_delta.delta_id,
        pending_delta.delta_id,
    )
    assert observed.accrued_delta_ids == (pending_delta.delta_id,)
    assert store.get_delta(fresh_delta.delta_id).raw_seen_count == 2
    assert (
        store.get_pending_ledger(pending_delta.delta_id).wait_area_token_requests == 3
    )

    replay = ObjectContextStore(path).confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=attempt_id,
        raw_delta_ids=[fresh_delta.delta_id, pending_delta.delta_id],
        pending_accruals=[
            (pending_delta.delta_id, ledger.gain_tokens, ledger.ledger_generation)
        ],
    )
    assert replay.duplicate is True
    assert replay.success_sequence == observed.success_sequence
    assert ObjectContextStore(path).latest_success_sequence("conv-a") == 2
    assert ObjectContextStore(path).get_delta(fresh_delta.delta_id).raw_seen_count == 2
    assert (
        ObjectContextStore(path)
        .get_pending_ledger(pending_delta.delta_id)
        .wait_area_token_requests
        == 3
    )

    with pytest.raises(RuntimeError, match="different observation"):
        store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=attempt_id,
            raw_delta_ids=[pending_delta.delta_id],
            pending_accruals=[
                (pending_delta.delta_id, ledger.gain_tokens, ledger.ledger_generation)
            ],
        )


def test_gain_mismatch_rolls_back_observation_exposure_and_wait_area(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    delta = _register_delta(store, "delta-a")
    first = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(20),
        raw_delta_ids=[delta.delta_id],
    )
    ledger = _promote(store, delta, entered=first.success_sequence, gain=4)

    with pytest.raises(RuntimeError, match="gain snapshot mismatch"):
        store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=_attempt(21),
            raw_delta_ids=[delta.delta_id],
            pending_accruals=[
                (delta.delta_id, ledger.gain_tokens + 1, ledger.ledger_generation)
            ],
        )

    assert store.latest_success_sequence("conv-a") == 1
    assert store.get_delta(delta.delta_id).raw_seen_count == 1
    unchanged = store.get_pending_ledger(delta.delta_id)
    assert unchanged.wait_area_token_requests == 0
    assert unchanged.last_accrued_success_sequence is None
    with sqlite3.connect(store.path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM request_observations").fetchone()[0] == 1
        )


def test_concurrent_success_sequences_are_dense_and_unique(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    delta = _register_delta(store, "delta-concurrent")
    workers = 8
    barrier = threading.Barrier(workers)

    def observe(number: int):
        barrier.wait()
        return store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=_attempt(100 + number),
            raw_delta_ids=[delta.delta_id],
            # Engine-local selection sequences can collide across concurrent
            # instances; UUID/success_sequence, not this value, is authoritative.
            exposure_request_sequence=1,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(observe, range(workers)))

    assert sorted(result.success_sequence for result in results) == list(
        range(1, workers + 1)
    )
    assert len({result.request_attempt_id for result in results}) == workers
    assert store.latest_success_sequence("conv-a") == workers
    exposed = store.get_delta(delta.delta_id)
    assert exposed is not None
    assert exposed.raw_seen_count == workers
    assert exposed.first_seen_success_sequence == 1
    assert exposed.last_seen_success_sequence == workers


def test_flush_deletes_ledgers_atomically_and_rollback_restores_them(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    committed = _register_delta(store, "delta-commit", sequence=1)
    rolled_back = _register_delta(store, "delta-rollback", sequence=2)
    success = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(30),
        raw_delta_ids=[committed.delta_id, rolled_back.delta_id],
    )
    _promote(store, committed, entered=success.success_sequence)
    rollback_ledger = _promote(store, rolled_back, entered=success.success_sequence)

    store.publish_compressed_batch([
        (
            committed.delta_id,
            [],
            [{"role": "user", "content": "compressed"}],
        )
    ])
    assert store.get_delta(committed.delta_id).state == DeltaState.COMPRESSED
    assert store.get_pending_ledger(committed.delta_id) is None

    with pytest.raises(RuntimeError, match="epoch identities disagree"):
        store.publish_compressed_batch(
            [
                (
                    rolled_back.delta_id,
                    [],
                    [{"role": "user", "content": "not committed"}],
                )
            ],
            projection_epoch_id="epoch-a",
            projection_decision={
                "projection_epoch_id": "epoch-b",
                "conversation_id": "conv-a",
                "decision_kind": "flush",
                "decision_reason": "TEST_EPOCH_MISMATCH",
                "member_delta_ids": [rolled_back.delta_id],
            },
        )
    assert (
        store.get_delta(rolled_back.delta_id).state == DeltaState.COMPRESSION_ELIGIBLE
    )
    assert store.get_delta(rolled_back.delta_id).compressed_view is None
    assert store.get_pending_ledger(rolled_back.delta_id) == rollback_ledger

    manual = _register_delta(store, "delta-manual", sequence=3)
    manual_success = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(31),
        raw_delta_ids=[manual.delta_id],
    )
    _promote(store, manual, entered=manual_success.success_sequence)
    store.publish_cards_and_compressed_delta(
        delta_id=manual.delta_id,
        cards=[],
        compressed_view=[{"role": "user", "content": "manual compressed"}],
    )
    assert store.get_delta(manual.delta_id).state == DeltaState.COMPRESSED
    assert store.get_pending_ledger(manual.delta_id) is None


def test_wait_area_equals_sum_of_snapshotted_gain_for_raw_carried_requests(tmp_path):
    store = ObjectContextStore(tmp_path / "objects.sqlite3")
    deltas = [
        _register_delta(store, f"delta-{index}", sequence=index)
        for index in range(1, 7)
    ]
    first = store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=_attempt(200),
        raw_delta_ids=[delta.delta_id for delta in deltas],
    )
    ledgers = {
        delta.delta_id: _promote(
            store,
            delta,
            entered=first.success_sequence,
            gain=(index % 4) + 1,
        )
        for index, delta in enumerate(deltas)
    }
    expected = {delta.delta_id: 0 for delta in deltas}
    snapshots = [
        PendingLedgerAccrual(
            delta_id=ledger.delta_id,
            gain_tokens=ledger.gain_tokens,
            ledger_generation=ledger.ledger_generation,
        )
        for ledger in ledgers.values()
    ]

    for request_number in range(1, 13):
        carried = [
            delta.delta_id
            for index, delta in enumerate(deltas)
            if (index + request_number) % 3 != 0
        ]
        result = store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=_attempt(200 + request_number),
            raw_delta_ids=carried,
            pending_accruals=snapshots,
        )
        assert set(result.accrued_delta_ids) == set(carried)
        assert set(result.skipped_pending_delta_ids) == (
            set(ledgers).difference(carried)
        )
        for delta_id in carried:
            expected[delta_id] += ledgers[delta_id].gain_tokens

    assert {
        ledger.delta_id: ledger.wait_area_token_requests
        for ledger in store.list_pending_ledgers("conv-a")
    } == expected
