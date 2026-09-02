"""Crash-safe encrypted Trace outbox contracts."""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


class MemorySecureStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def available(self) -> bool:
        return True

    def read(self, name: str) -> bytes | None:
        return self.values.get(name)

    def write(self, name: str, value: bytes) -> None:
        self.values[name] = value


@pytest.fixture
def outbox_root() -> Iterator[Path]:
    repo_root = Path(__file__).resolve().parents[4]
    root = repo_root / "tmp" / "trace-outbox-tests" / str(uuid.uuid4())
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def test_commit_is_visibility_boundary_and_committed_batch_recovers_after_reopen(
    outbox_root: Path,
) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint
    from hermes_cli.client_auth.trace.outbox import TraceEnvelope, TraceOutbox, TraceOwner

    protector = TraceKeyProtector(MemorySecureStore())
    owner = TraceOwner(
        account_id="account-alice",
        session_id="auth-session-alice",
        installation_id="11111111-1111-4111-8111-111111111111",
    )
    envelope = TraceEnvelope(
        body=b"serialized-otlp-batch",
        entrypoint=TraceEntrypoint.CLI,
        hermes_session_id="session-1",
        run_id="run-1",
    )
    outbox = TraceOutbox.open(outbox_root, owner=owner, key_protector=protector)

    pending = outbox.begin_enqueue(envelope)
    assert outbox.peek_eligible() is None

    committed = outbox.commit(pending)
    assert committed.batch_id == pending.batch_id
    assert committed.body == envelope.body
    outbox.close()

    reopened = TraceOutbox.open(outbox_root, owner=owner, key_protector=protector)
    try:
        recovered = reopened.peek_eligible()
        assert recovered is not None
        assert recovered.batch_id == committed.batch_id
        assert recovered.body == envelope.body
        assert recovered.entrypoint is TraceEntrypoint.CLI
    finally:
        reopened.close()


def _owner(account_id: str = "account-alice"):
    from hermes_cli.client_auth.trace.outbox import TraceOwner

    return TraceOwner(
        account_id=account_id,
        session_id=f"auth-session-{account_id}",
        installation_id="11111111-1111-4111-8111-111111111111",
    )


def _envelope(body: bytes, *, entrypoint: str = "cli", run_id: str = "run-1"):
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint
    from hermes_cli.client_auth.trace.outbox import TraceEnvelope

    return TraceEnvelope(
        body=body,
        entrypoint=TraceEntrypoint.parse(entrypoint),
        hermes_session_id="session-1",
        run_id=run_id,
    )


def test_payload_is_ciphertext_on_disk_and_database_is_account_bound(outbox_root: Path) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceOutboxUnavailable

    store = MemorySecureStore()
    protector = TraceKeyProtector(store)
    outbox = TraceOutbox.open(outbox_root, owner=_owner(), key_protector=protector)
    sentinel = b"plaintext-otlp-must-never-reach-disk"
    outbox.commit(outbox.begin_enqueue(_envelope(sentinel)))
    outbox.close()

    for path in outbox_root.glob("outbox.db*"):
        assert sentinel not in path.read_bytes()

    with pytest.raises(TraceOutboxUnavailable, match="owner mismatch"):
        TraceOutbox.open(
            outbox_root,
            owner=_owner("account-bob"),
            key_protector=protector,
        )


def test_identical_admission_reuses_stable_uuidv4_batch_and_dedupe(outbox_root: Path) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox

    outbox = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=TraceKeyProtector(MemorySecureStore()),
    )
    try:
        first = outbox.commit(outbox.begin_enqueue(_envelope(b"same")))
        second = outbox.commit(outbox.begin_enqueue(_envelope(b"same")))

        assert uuid.UUID(first.batch_id).version == 4
        assert second.batch_id == first.batch_id
        assert second.dedupe_key == first.dedupe_key
        assert outbox.diagnostics().pending == 1
        assert outbox.diagnostics().deduplicated == 1
    finally:
        outbox.close()


def test_fifo_retry_eligibility_persists_and_blocks_younger_batches(outbox_root: Path) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox

    now = [100.0]
    store = MemorySecureStore()
    protector = TraceKeyProtector(store)
    outbox = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=protector,
        clock=lambda: now[0],
    )
    first = outbox.commit(outbox.begin_enqueue(_envelope(b"first", run_id="run-1")))
    now[0] = 101.0
    outbox.commit(outbox.begin_enqueue(_envelope(b"second", run_id="run-2")))
    outbox.mark_retry(first.batch_id, next_retry_at_ms=120_000)

    assert outbox.peek_eligible(now_ms=110_000) is None
    outbox.close()

    reopened = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=protector,
        clock=lambda: now[0],
    )
    try:
        recovered = reopened.peek_eligible(now_ms=120_000)
        assert recovered is not None
        assert recovered.batch_id == first.batch_id
        assert recovered.attempt_count == 1
    finally:
        reopened.close()


@pytest.mark.parametrize("outcome", ["accepted", "duplicate"])
def test_acknowledge_removes_payload_and_keeps_payload_free_receipt(
    outbox_root: Path,
    outcome: str,
) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceReceipt

    outbox = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=TraceKeyProtector(MemorySecureStore()),
    )
    try:
        batch = outbox.commit(outbox.begin_enqueue(_envelope(b"payload")))
        receipt = TraceReceipt(
            batch_id=batch.batch_id,
            outcome=outcome,
            received_at="2026-09-02T00:00:00Z",
        )
        outbox.acknowledge(receipt)

        assert outbox.peek_eligible() is None
        assert outbox.lookup_receipt(batch.batch_id) == receipt
        diagnostics = outbox.diagnostics()
        assert diagnostics.pending == 0
        assert diagnostics.tombstones == 1
    finally:
        outbox.close()


def test_missing_key_and_corrupt_ciphertext_fail_closed(outbox_root: Path) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceOutboxUnavailable

    store = MemorySecureStore()
    outbox = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=TraceKeyProtector(store),
    )
    batch = outbox.commit(outbox.begin_enqueue(_envelope(b"payload")))
    outbox.close()

    connection = sqlite3.connect(outbox_root / "outbox.db")
    connection.execute(
        "UPDATE batches SET ciphertext = ? WHERE batch_id = ?",
        (b"corrupt", batch.batch_id),
    )
    connection.commit()
    connection.close()

    reopened = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=TraceKeyProtector(store),
    )
    try:
        with pytest.raises(TraceOutboxUnavailable, match="ciphertext"):
            reopened.peek_eligible()
    finally:
        reopened.close()

    store.values.clear()
    with pytest.raises(TraceOutboxUnavailable, match="encrypted Trace outbox unavailable"):
        TraceOutbox.open(
            outbox_root,
            owner=_owner(),
            key_protector=TraceKeyProtector(store),
        )


def test_capacity_eviction_retention_and_free_space_reserve_are_bounded(
    outbox_root: Path,
) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceOutboxUnavailable

    now = [100.0]
    store = MemorySecureStore()
    protector = TraceKeyProtector(store)
    ample = shutil._ntuple_diskusage(total=100 * 1024**3, used=1, free=99 * 1024**3)
    outbox = TraceOutbox.open(
        outbox_root,
        owner=_owner(),
        key_protector=protector,
        clock=lambda: now[0],
        disk_usage=lambda _: ample,
        capacity_bytes=220,
        retention_seconds=10,
    )
    first = outbox.commit(outbox.begin_enqueue(_envelope(bytes(range(128)), run_id="run-1")))
    now[0] = 101.0
    second = outbox.commit(
        outbox.begin_enqueue(_envelope(bytes(reversed(range(128))), run_id="run-2"))
    )

    assert outbox.peek_eligible().batch_id == second.batch_id
    assert outbox.diagnostics().evicted_capacity == 1
    assert first.batch_id != second.batch_id

    now[0] = 112.0
    assert outbox.peek_eligible() is None
    assert outbox.diagnostics().expired == 1
    outbox.close()

    low_space_root = outbox_root / "low-space"
    low_space = shutil._ntuple_diskusage(total=10 * 1024**3, used=10 * 1024**3, free=0)
    blocked = TraceOutbox.open(
        low_space_root,
        owner=_owner(),
        key_protector=protector,
        disk_usage=lambda _: low_space,
    )
    try:
        with pytest.raises(TraceOutboxUnavailable, match="free-space reserve"):
            blocked.commit(blocked.begin_enqueue(_envelope(b"payload")))
        assert blocked.diagnostics().pending == 0
    finally:
        blocked.close()


def test_quarantine_remains_encrypted_persisted_and_is_not_replayed(outbox_root: Path) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.outbox import TraceOutbox

    store = MemorySecureStore()
    protector = TraceKeyProtector(store)
    outbox = TraceOutbox.open(outbox_root, owner=_owner(), key_protector=protector)
    batch = outbox.commit(outbox.begin_enqueue(_envelope(b"quarantine-sentinel")))
    outbox.quarantine(batch.batch_id, error_class="invalid_otlp")

    assert outbox.peek_eligible() is None
    assert outbox.diagnostics().quarantined == 1
    outbox.close()

    assert b"quarantine-sentinel" not in (outbox_root / "outbox.db").read_bytes()
    reopened = TraceOutbox.open(outbox_root, owner=_owner(), key_protector=protector)
    try:
        assert reopened.peek_eligible() is None
        assert reopened.diagnostics().quarantined == 1
        assert not reopened.compact_if_idle()
    finally:
        reopened.close()
