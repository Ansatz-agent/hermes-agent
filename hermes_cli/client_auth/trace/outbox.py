"""Crash-safe encrypted FIFO outbox owned by the native auth process."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .crypto import (
    EncryptedRecord,
    TraceKeyProtector,
    TraceKeyUnavailable,
    decrypt_record,
    encrypt_record,
)
from .identity import TraceEntrypoint, TraceInstallationIdentity


DEFAULT_CAPACITY_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_RETENTION_SECONDS = 30 * 24 * 60 * 60
MAX_BATCH_BYTES = 8 * 1024 * 1024
MIN_FREE_RESERVE_BYTES = 1024 * 1024 * 1024


class TraceOutboxUnavailable(RuntimeError):
    """Durable Trace admission is unavailable without affecting Agent work."""


@dataclass(frozen=True, slots=True)
class TraceOwner:
    account_id: str
    session_id: str
    installation_id: str

    def __post_init__(self) -> None:
        if not self.account_id or not self.session_id:
            raise ValueError("Trace account and session identity are required")
        TraceInstallationIdentity.parse(self.installation_id)


@dataclass(frozen=True, slots=True)
class TraceEnvelope:
    body: bytes
    entrypoint: TraceEntrypoint
    hermes_session_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.body, bytes):
            raise TypeError("Trace body must be bytes")
        if len(self.body) > MAX_BATCH_BYTES:
            raise ValueError("Trace batch exceeds 8 MiB")
        TraceEntrypoint.parse(self.entrypoint.value)
        if not self.hermes_session_id or not self.run_id:
            raise ValueError("Trace session and run identities are required")


@dataclass(frozen=True, slots=True)
class PendingTraceCommit:
    batch_id: str
    dedupe_key: str
    envelope: TraceEnvelope
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class DurableTraceBatch:
    batch_id: str
    dedupe_key: str
    body: bytes
    entrypoint: TraceEntrypoint
    hermes_session_id: str
    run_id: str
    installation_id: str
    account_id: str
    account_session_id: str
    sequence: int
    created_at_ms: int
    next_retry_at_ms: int
    attempt_count: int
    payload_sha256: str


@dataclass(frozen=True, slots=True)
class TraceReceipt:
    batch_id: str
    outcome: str
    received_at: str

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.batch_id)
        except ValueError as exc:
            raise ValueError("invalid Trace receipt batch ID") from exc
        if parsed.version != 4 or str(parsed) != self.batch_id:
            raise ValueError("invalid Trace receipt batch ID")
        if self.outcome not in {"accepted", "duplicate"}:
            raise ValueError("invalid Trace receipt outcome")
        if not self.received_at:
            raise ValueError("invalid Trace receipt timestamp")


@dataclass(frozen=True, slots=True)
class TraceOutboxDiagnostics:
    pending: int
    quarantined: int
    tombstones: int
    payload_bytes: int
    deduplicated: int
    evicted_capacity: int
    expired: int


class TraceOutbox:
    """One account-local SQLite queue with encrypted OTLP bodies."""

    def __init__(
        self,
        *,
        root: Path,
        owner: TraceOwner,
        connection: sqlite3.Connection,
        key: bytes,
        key_reference: bytes,
        clock: Callable[[], float],
        disk_usage: Callable[[str | os.PathLike[str]], shutil._ntuple_diskusage],
        capacity_bytes: int,
        retention_seconds: int,
    ) -> None:
        self.root = root
        self.owner = owner
        self._connection = connection
        self._key = key
        self._key_reference = key_reference
        self._clock = clock
        self._disk_usage = disk_usage
        self._capacity_bytes = capacity_bytes
        self._retention_seconds = retention_seconds
        self._closed = False
        self._deduplicated = 0

    @classmethod
    def open(
        cls,
        root: str | Path,
        *,
        owner: TraceOwner,
        key_protector: TraceKeyProtector,
        clock: Callable[[], float] = time.time,
        disk_usage: Callable[[str | os.PathLike[str]], shutil._ntuple_diskusage] = shutil.disk_usage,
        capacity_bytes: int = DEFAULT_CAPACITY_BYTES,
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
    ) -> TraceOutbox:
        path = Path(root)
        path.mkdir(parents=True, exist_ok=True)
        database = path / "outbox.db"
        try:
            connection = sqlite3.connect(database, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            cls._create_schema(connection)
            existing_owner = connection.execute(
                "SELECT account_id, installation_id, key_reference FROM owner WHERE singleton = 1"
            ).fetchone()
            if existing_owner is None:
                key = os.urandom(32)
                key_reference = key_protector.wrap(owner.account_id, key)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "INSERT INTO owner(singleton, account_id, installation_id, key_reference) VALUES(1, ?, ?, ?)",
                        (owner.account_id, owner.installation_id, key_reference),
                    )
                    connection.execute("COMMIT")
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
            else:
                if (
                    existing_owner["account_id"] != owner.account_id
                    or existing_owner["installation_id"] != owner.installation_id
                ):
                    raise TraceOutboxUnavailable("Trace outbox owner mismatch")
                key_reference = bytes(existing_owner["key_reference"])
                key = key_protector.unwrap(owner.account_id, key_reference)
        except (OSError, sqlite3.Error, TraceKeyUnavailable) as exc:
            try:
                connection.close()
            except (NameError, sqlite3.Error):
                pass
            raise TraceOutboxUnavailable("encrypted Trace outbox unavailable") from exc
        return cls(
            root=path,
            owner=owner,
            connection=connection,
            key=key,
            key_reference=key_reference,
            clock=clock,
            disk_usage=disk_usage,
            capacity_bytes=capacity_bytes,
            retention_seconds=retention_seconds,
        )

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS owner (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                account_id TEXT NOT NULL,
                installation_id TEXT NOT NULL,
                key_reference BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS batches (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                dedupe_key TEXT NOT NULL UNIQUE,
                entrypoint TEXT NOT NULL,
                hermes_session_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                account_session_id TEXT NOT NULL,
                created_at_ms INTEGER NOT NULL,
                next_retry_at_ms INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                payload_sha256 TEXT NOT NULL,
                nonce BLOB NOT NULL,
                ciphertext BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_class TEXT
            );
            CREATE TABLE IF NOT EXISTS receipts (
                batch_id TEXT PRIMARY KEY,
                dedupe_key TEXT NOT NULL UNIQUE,
                outcome TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                entrypoint TEXT NOT NULL,
                hermes_session_id TEXT NOT NULL,
                run_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS counters (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                evicted_capacity INTEGER NOT NULL DEFAULT 0,
                expired INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO counters(singleton) VALUES(1);
            """
        )

    def begin_enqueue(self, envelope: TraceEnvelope) -> PendingTraceCommit:
        self._require_open()
        created_at_ms = self._now_ms()
        dedupe_key = self._dedupe_key(envelope)
        existing = self._connection.execute(
            "SELECT batch_id, created_at_ms FROM batches WHERE dedupe_key = ? UNION ALL "
            "SELECT batch_id, 0 AS created_at_ms FROM receipts WHERE dedupe_key = ? LIMIT 1",
            (dedupe_key, dedupe_key),
        ).fetchone()
        if existing is not None:
            self._deduplicated += 1
        return PendingTraceCommit(
            batch_id=existing["batch_id"] if existing is not None else str(uuid.uuid4()),
            dedupe_key=dedupe_key,
            envelope=envelope,
            created_at_ms=(existing["created_at_ms"] or created_at_ms) if existing is not None else created_at_ms,
        )

    def commit(self, pending: PendingTraceCommit) -> DurableTraceBatch:
        self._require_open()
        existing = self._load_by_dedupe(pending.dedupe_key)
        if existing is not None:
            return existing
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            raced = self._connection.execute(
                "SELECT * FROM batches WHERE dedupe_key = ?", (pending.dedupe_key,)
            ).fetchone()
            if raced is not None:
                self._connection.execute("COMMIT")
                self._deduplicated += 1
                return self._decode_row(raced)
            sequence = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM batches"
            ).fetchone()[0]
            compressed = _brotli_compress(pending.envelope.body)
            payload_sha256 = hashlib.sha256(pending.envelope.body).hexdigest()
            metadata = self._metadata(
                batch_id=pending.batch_id,
                dedupe_key=pending.dedupe_key,
                envelope=pending.envelope,
                created_at_ms=pending.created_at_ms,
                sequence=sequence,
                payload_sha256=payload_sha256,
                account_session_id=self.owner.session_id,
            )
            record = encrypt_record(self._key, metadata, compressed)
            self._ensure_admission_capacity(len(record.nonce) + len(record.ciphertext))
            self._connection.execute(
                """INSERT INTO batches(
                    sequence, batch_id, dedupe_key, entrypoint, hermes_session_id, run_id, account_session_id,
                    created_at_ms, next_retry_at_ms, attempt_count, payload_sha256,
                    nonce, ciphertext, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'pending')""",
                (
                    sequence,
                    pending.batch_id,
                    pending.dedupe_key,
                    pending.envelope.entrypoint.value,
                    pending.envelope.hermes_session_id,
                    pending.envelope.run_id,
                    self.owner.session_id,
                    pending.created_at_ms,
                    pending.created_at_ms,
                    payload_sha256,
                    record.nonce,
                    record.ciphertext,
                ),
            )
            self._connection.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._connection.execute("ROLLBACK")
            existing = self._load_by_dedupe(pending.dedupe_key)
            if existing is None:
                raise
            return existing
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        return self._load_batch_id(pending.batch_id)

    def peek_eligible(self, now_ms: int | None = None) -> DurableTraceBatch | None:
        self._require_open()
        eligible_at = self._now_ms() if now_ms is None else now_ms
        self._expire_before(eligible_at)
        row = self._connection.execute(
            "SELECT * FROM batches WHERE status = 'pending' ORDER BY sequence LIMIT 1"
        ).fetchone()
        if row is None or row["next_retry_at_ms"] > eligible_at:
            return None
        try:
            return self._decode_row(row)
        except Exception as exc:
            raise TraceOutboxUnavailable("Trace outbox ciphertext is invalid") from exc

    def mark_retry(self, batch_id: str, *, next_retry_at_ms: int) -> None:
        self._require_open()
        if next_retry_at_ms < 0:
            raise ValueError("invalid Trace retry time")
        cursor = self._connection.execute(
            """UPDATE batches
               SET next_retry_at_ms = ?, attempt_count = attempt_count + 1
               WHERE batch_id = ? AND status = 'pending'""",
            (next_retry_at_ms, batch_id),
        )
        if cursor.rowcount != 1:
            raise TraceOutboxUnavailable("unknown pending Trace batch")

    def acknowledge(self, receipt: TraceReceipt) -> None:
        self._require_open()
        row = self._connection.execute(
            "SELECT * FROM batches WHERE batch_id = ? AND status = 'pending'",
            (receipt.batch_id,),
        ).fetchone()
        if row is None:
            existing = self.lookup_receipt(receipt.batch_id)
            if existing == receipt:
                return
            raise TraceOutboxUnavailable("unknown Trace receipt batch")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(
                """INSERT INTO receipts(
                    batch_id, dedupe_key, outcome, received_at, payload_sha256,
                    entrypoint, hermes_session_id, run_id
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["batch_id"],
                    row["dedupe_key"],
                    receipt.outcome,
                    receipt.received_at,
                    row["payload_sha256"],
                    row["entrypoint"],
                    row["hermes_session_id"],
                    row["run_id"],
                ),
            )
            self._connection.execute(
                "DELETE FROM batches WHERE batch_id = ?", (receipt.batch_id,)
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def lookup_receipt(self, batch_id: str) -> TraceReceipt | None:
        row = self._connection.execute(
            "SELECT batch_id, outcome, received_at FROM receipts WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if row is None:
            return None
        return TraceReceipt(
            batch_id=row["batch_id"],
            outcome=row["outcome"],
            received_at=row["received_at"],
        )

    def diagnostics(self) -> TraceOutboxDiagnostics:
        row = self._connection.execute(
            """SELECT
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN status = 'quarantined' THEN 1 ELSE 0 END) AS quarantined,
                COALESCE(SUM(length(nonce) + length(ciphertext)), 0) AS payload_bytes
               FROM batches"""
        ).fetchone()
        tombstones = self._connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        counters = self._connection.execute(
            "SELECT evicted_capacity, expired FROM counters WHERE singleton = 1"
        ).fetchone()
        return TraceOutboxDiagnostics(
            pending=row["pending"] or 0,
            quarantined=row["quarantined"] or 0,
            tombstones=tombstones,
            payload_bytes=row["payload_bytes"],
            deduplicated=self._deduplicated,
            evicted_capacity=counters["evicted_capacity"],
            expired=counters["expired"],
        )

    def quarantine(self, batch_id: str, *, error_class: str) -> None:
        self._require_open()
        if not error_class or len(error_class) > 128:
            raise ValueError("invalid Trace quarantine class")
        cursor = self._connection.execute(
            """UPDATE batches SET status = 'quarantined', error_class = ?
               WHERE batch_id = ? AND status = 'pending'""",
            (error_class, batch_id),
        )
        if cursor.rowcount != 1:
            raise TraceOutboxUnavailable("unknown pending Trace batch")

    def compact_if_idle(self) -> bool:
        self._require_open()
        if self._connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0] != 0:
            return False
        self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def _load_by_dedupe(self, dedupe_key: str) -> DurableTraceBatch | None:
        row = self._connection.execute(
            "SELECT * FROM batches WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return None if row is None else self._decode_row(row)

    def _load_batch_id(self, batch_id: str) -> DurableTraceBatch:
        row = self._connection.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise TraceOutboxUnavailable("committed Trace batch is missing")
        return self._decode_row(row)

    def _decode_row(self, row: sqlite3.Row) -> DurableTraceBatch:
        entrypoint = TraceEntrypoint.parse(row["entrypoint"])
        metadata = self._metadata(
            batch_id=row["batch_id"],
            dedupe_key=row["dedupe_key"],
            envelope=TraceEnvelope(
                body=b"",
                entrypoint=entrypoint,
                hermes_session_id=row["hermes_session_id"],
                run_id=row["run_id"],
            ),
            created_at_ms=row["created_at_ms"],
            sequence=row["sequence"],
            payload_sha256=row["payload_sha256"],
            account_session_id=row["account_session_id"],
        )
        compressed = decrypt_record(
            self._key,
            metadata,
            EncryptedRecord(nonce=bytes(row["nonce"]), ciphertext=bytes(row["ciphertext"])),
        )
        body = _brotli_decompress(compressed)
        if hashlib.sha256(body).hexdigest() != row["payload_sha256"]:
            raise TraceOutboxUnavailable("Trace payload digest mismatch")
        return DurableTraceBatch(
            batch_id=row["batch_id"],
            dedupe_key=row["dedupe_key"],
            body=body,
            entrypoint=entrypoint,
            hermes_session_id=row["hermes_session_id"],
            run_id=row["run_id"],
            installation_id=self.owner.installation_id,
            account_id=self.owner.account_id,
            account_session_id=row["account_session_id"],
            sequence=row["sequence"],
            created_at_ms=row["created_at_ms"],
            next_retry_at_ms=row["next_retry_at_ms"],
            attempt_count=row["attempt_count"],
            payload_sha256=row["payload_sha256"],
        )

    def _metadata(
        self,
        *,
        batch_id: str,
        dedupe_key: str,
        envelope: TraceEnvelope,
        created_at_ms: int,
        sequence: int,
        payload_sha256: str,
        account_session_id: str,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "account_id": self.owner.account_id,
            "account_session_id": account_session_id,
            "installation_id": self.owner.installation_id,
            "batch_id": batch_id,
            "dedupe_key": dedupe_key,
            "entrypoint": envelope.entrypoint.value,
            "hermes_session_id": envelope.hermes_session_id,
            "run_id": envelope.run_id,
            "created_at_ms": created_at_ms,
            "sequence": sequence,
            "payload_sha256": payload_sha256,
        }

    def _dedupe_key(self, envelope: TraceEnvelope) -> str:
        identity = json.dumps(
            {
                "account_id": self.owner.account_id,
                "installation_id": self.owner.installation_id,
                "entrypoint": envelope.entrypoint.value,
                "hermes_session_id": envelope.hermes_session_id,
                "run_id": envelope.run_id,
                "payload_sha256": hashlib.sha256(envelope.body).hexdigest(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def _ensure_admission_capacity(self, record_bytes: int) -> None:
        usage = self._disk_usage(self.root)
        reserve = max(MIN_FREE_RESERVE_BYTES, int(usage.total * 0.05))
        if usage.free - record_bytes < reserve:
            raise TraceOutboxUnavailable("Trace outbox free-space reserve reached")
        if record_bytes > self._capacity_bytes:
            raise TraceOutboxUnavailable("Trace batch exceeds outbox capacity")
        current = self._connection.execute(
            "SELECT COALESCE(SUM(length(nonce) + length(ciphertext)), 0) FROM batches"
        ).fetchone()[0]
        while current + record_bytes > self._capacity_bytes:
            oldest = self._connection.execute(
                """SELECT batch_id, length(nonce) + length(ciphertext) AS bytes
                   FROM batches WHERE status = 'pending' ORDER BY sequence LIMIT 1"""
            ).fetchone()
            if oldest is None:
                raise TraceOutboxUnavailable("Trace outbox capacity unavailable")
            self._connection.execute(
                "DELETE FROM batches WHERE batch_id = ?", (oldest["batch_id"],)
            )
            self._connection.execute(
                "UPDATE counters SET evicted_capacity = evicted_capacity + 1 WHERE singleton = 1"
            )
            current -= oldest["bytes"]

    def _expire_before(self, now_ms: int) -> None:
        cutoff = now_ms - self._retention_seconds * 1000
        expired = self._connection.execute(
            "SELECT COUNT(*) FROM batches WHERE created_at_ms < ?", (cutoff,)
        ).fetchone()[0]
        if not expired:
            return
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("DELETE FROM batches WHERE created_at_ms < ?", (cutoff,))
            self._connection.execute(
                "UPDATE counters SET expired = expired + ? WHERE singleton = 1", (expired,)
            )
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    def _require_open(self) -> None:
        if self._closed:
            raise TraceOutboxUnavailable("Trace outbox is closed")


def _brotli_compress(body: bytes) -> bytes:
    import brotli

    return brotli.compress(body)


def _brotli_decompress(body: bytes) -> bytes:
    import brotli

    return brotli.decompress(body)


__all__ = [
    "DurableTraceBatch",
    "PendingTraceCommit",
    "TraceEnvelope",
    "TraceOutbox",
    "TraceOutboxDiagnostics",
    "TraceOutboxUnavailable",
    "TraceOwner",
    "TraceReceipt",
]
