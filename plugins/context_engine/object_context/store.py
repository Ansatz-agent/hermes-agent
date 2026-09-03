"""Profile-scoped exact Working Memory and Object Registry for V1."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from agent.model_metadata import estimate_messages_tokens_rough, estimate_tokens_rough

from .models import (
    ActivityState,
    DeltaRecord,
    DeltaState,
    DetectedObject,
    ObjectLocation,
    ObjectRecord,
    ObjectType,
    PendingLedgerAccrual,
    PendingLedgerRecord,
    RetrievalLease,
    SuccessfulRequestObservationResult,
)


SCHEMA_VERSION = 5
OBJECT_REFS_REPAIR_KEY = "object_refs_json_rebuilt_from_occurrences_v1"
REQUEST_PROJECTION_METRIC_NAMES = frozenset(
    {
        "raw_context_tokens",
        "rendered_context_tokens",
        "raw_conversation_tokens",
        "rendered_conversation_tokens",
        "conversation_tokens_saved",
        "conversation_compression_ratio",
        "tokens_saved",
        "compression_ratio",
        "hot_tail_tokens",
        "projection_latency_ms",
    }
)
CACHE_USAGE_METRIC_NAMES = frozenset(
    {
        "prompt_tokens",
        "uncached_input_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "prompt_cache_hit_ratio",
    }
)
OBJECT_REF_RE = re.compile(
    r"^object://(?P<object_id>obj_[a-f0-9]{24})@v(?P<version>[1-9][0-9]*)$"
)
PROJECTION_DECISION_FIELDS = frozenset({
    "projection_epoch_id",
    "conversation_id",
    "session_id",
    "request_sequence",
    "decision_kind",
    "decision_mode",
    "decision_reason",
    "candidate_count",
    "member_delta_ids",
    "member_object_refs",
    "earliest_changed_delta_id",
    "baseline_prompt_tokens",
    "candidate_prompt_tokens",
    "gross_tokens_removed",
    "card_or_receipt_tokens",
    "baseline_reusable_prefix_tokens",
    "candidate_reusable_prefix_tokens",
    "cache_tokens_invalidated",
    "cache_penalty_equivalent_tokens",
    "known_summary_cost_equivalent_tokens",
    "net_saving_equivalent_tokens",
    "net_saving_usd",
    "cache_read_weight",
    "cache_write_weight",
    "pricing_source",
    "pricing_version",
    "estimator_source",
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
})
PROJECTION_DECISION_MODES = frozenset(
    {"normal", "emergency", "amortized", "fixed", "capacity"}
)
PROJECTION_BATCH_POLICIES = frozenset({"dynamic", "fixed"})
PROJECTION_BASELINE_STATES = frozenset({"known", "cold", "unknown"})
PROJECTION_POLICY_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}$")
PROJECTION_DECISION_REASON_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
PROJECTION_METADATA_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/@-]{0,199}$")
ROUTE_NAMESPACE_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
CONFIRMED_SUCCESS_OUTCOME = "confirmed_success"
PROJECTION_V12_POSITIVE_INTEGER_FIELDS = frozenset({
    "cache_granularity_tokens",
    "fixed_batch_size",
})
PROJECTION_V12_NONNEGATIVE_INTEGER_FIELDS = frozenset({
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
    "shared_cached_hot_tokens",
    "amortized_baseline_prompt_tokens",
    "amortized_candidate_prompt_tokens",
    "amortized_baseline_reusable_prefix_tokens",
    "amortized_candidate_reusable_prefix_tokens",
})
PROJECTION_V12_NONNEGATIVE_NUMBER_FIELDS = frozenset({
    "amortized_cache_read_weight",
    "immediate_cache_penalty_equivalent_tokens",
    "immediate_cache_read_weight",
    "immediate_cache_write_weight",
    "wait_area_token_requests",
    "wait_loss_now",
    "wait_loss_increment",
    "wait_loss_projected",
    "shared_overhead_equivalent_tokens",
})
PROJECTION_V12_FINITE_NUMBER_FIELDS = frozenset({
    "crossing_margin",
    "immediate_net_saving_equivalent_tokens",
    "immediate_net_saving_usd",
})
PROJECTION_V12_BOOLEAN_FIELDS = frozenset({
    "emergency_triggered",
    "pending_count_over",
    "pending_tokens_over",
    "amortized_crossed",
    "immediate_crossed",
})


def encode_exact(content: str) -> bytes:
    return content.encode("utf-8", errors="surrogatepass")


def decode_exact(content: bytes) -> str:
    return content.decode("utf-8", errors="surrogatepass")


def exact_sha256(content: str) -> str:
    return hashlib.sha256(encode_exact(content)).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class ObjectContextStore:
    """Durable lossless store with logical immutable versions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._prepare_path()
        self._initialize_schema()

    def _prepare_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _initialize_schema(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS blobs (
                    sha256 TEXT PRIMARY KEY,
                    content BLOB NOT NULL,
                    byte_size INTEGER NOT NULL,
                    char_count INTEGER NOT NULL,
                    token_count INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logical_objects (
                    object_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    canonical_name TEXT NOT NULL DEFAULT '',
                    created_at_delta INTEGER NOT NULL,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS object_versions (
                    object_ref TEXT PRIMARY KEY,
                    object_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '',
                    language TEXT NOT NULL DEFAULT '',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    supersedes_ref TEXT NOT NULL DEFAULT '',
                    derived_from_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    contains_json TEXT NOT NULL DEFAULT '{}',
                    card_text TEXT NOT NULL DEFAULT '',
                    activity_state TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,
                    created_at_delta INTEGER NOT NULL,
                    last_accessed_delta INTEGER NOT NULL,
                    inactive_since_delta INTEGER,
                    location TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    FOREIGN KEY (object_id) REFERENCES logical_objects(object_id),
                    FOREIGN KEY (sha256) REFERENCES blobs(sha256),
                    UNIQUE (object_id, version)
                );

                CREATE TABLE IF NOT EXISTS deltas (
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
                    first_seen_success_sequence INTEGER,
                    last_seen_success_sequence INTEGER,
                    eligibility_success_sequence INTEGER,
                    projection_epoch_id TEXT,
                    projected_at_request_sequence INTEGER,
                    UNIQUE (conversation_id, global_sequence)
                );

                CREATE TABLE IF NOT EXISTS object_occurrences (
                    occurrence_key TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    delta_id TEXT NOT NULL,
                    object_ref TEXT NOT NULL,
                    message_key TEXT NOT NULL,
                    message_ordinal INTEGER NOT NULL,
                    part_ordinal INTEGER NOT NULL,
                    span_start INTEGER NOT NULL,
                    span_end INTEGER NOT NULL,
                    whole_part INTEGER NOT NULL,
                    detection_method TEXT NOT NULL,
                    source_role TEXT NOT NULL,
                    tool_name TEXT NOT NULL DEFAULT '',
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    FOREIGN KEY (delta_id) REFERENCES deltas(delta_id),
                    FOREIGN KEY (object_ref) REFERENCES object_versions(object_ref)
                );

                CREATE TABLE IF NOT EXISTS retrieval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    object_ref TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    mounted_at_delta INTEGER NOT NULL,
                    unmounted_at_delta INTEGER,
                    created_at REAL NOT NULL,
                    UNIQUE (conversation_id, turn_id, tool_call_id, object_ref)
                );

                CREATE TABLE IF NOT EXISTS retrieval_leases (
                    conversation_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    object_ref TEXT NOT NULL,
                    tool_call_id TEXT NOT NULL DEFAULT '',
                    reason TEXT NOT NULL,
                    mounted_at_delta INTEGER NOT NULL,
                    expires_at TEXT NOT NULL,
                    PRIMARY KEY (conversation_id, turn_id, object_ref)
                );

                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    delta_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    value REAL NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projection_epochs (
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
                    request_attempt_id TEXT,
                    policy_version TEXT,
                    batch_policy TEXT,
                    fixed_batch_size INTEGER,
                    baseline_state TEXT,
                    cache_granularity_tokens INTEGER,
                    hot_underexposed_count INTEGER,
                    hot_seen_delta_count INTEGER,
                    hot_seen_bucket_count INTEGER,
                    hot_tail_tokens INTEGER,
                    hot_overflow_tokens INTEGER,
                    hot_start_token_offset INTEGER,
                    pending_delta_count INTEGER,
                    pending_bucket_count INTEGER,
                    pending_raw_tokens INTEGER,
                    pending_gain_tokens INTEGER,
                    wait_area_token_requests REAL,
                    wait_loss_now REAL,
                    wait_loss_increment REAL,
                    wait_loss_projected REAL,
                    shared_cached_hot_tokens INTEGER,
                    shared_overhead_equivalent_tokens REAL,
                    crossing_margin REAL,
                    emergency_triggered INTEGER,
                    pending_count_over INTEGER,
                    pending_tokens_over INTEGER,
                    amortized_crossed INTEGER,
                    immediate_crossed INTEGER,
                    amortized_cache_read_weight REAL,
                    amortized_baseline_prompt_tokens INTEGER,
                    amortized_candidate_prompt_tokens INTEGER,
                    amortized_baseline_reusable_prefix_tokens INTEGER,
                    amortized_candidate_reusable_prefix_tokens INTEGER,
                    immediate_cache_penalty_equivalent_tokens REAL,
                    immediate_net_saving_equivalent_tokens REAL,
                    immediate_net_saving_usd REAL,
                    immediate_cache_read_weight REAL,
                    immediate_cache_write_weight REAL,
                    immediate_pricing_source TEXT,
                    immediate_pricing_version TEXT,
                    created_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pending_ledgers (
                    conversation_id TEXT NOT NULL,
                    delta_id TEXT PRIMARY KEY,
                    entered_success_sequence INTEGER NOT NULL,
                    bucket_sequence INTEGER NOT NULL,
                    raw_tokens INTEGER NOT NULL,
                    projected_tokens INTEGER NOT NULL,
                    gain_tokens INTEGER NOT NULL,
                    wait_area_token_requests INTEGER NOT NULL DEFAULT 0,
                    last_accrued_success_sequence INTEGER,
                    ledger_generation INTEGER NOT NULL DEFAULT 1,
                    estimator_version TEXT NOT NULL DEFAULT '',
                    pending_reason TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    FOREIGN KEY (delta_id) REFERENCES deltas(delta_id)
                );

                CREATE TABLE IF NOT EXISTS request_observations (
                    request_attempt_id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    success_sequence INTEGER NOT NULL,
                    exposure_request_sequence INTEGER NOT NULL,
                    route_namespace_hash TEXT NOT NULL DEFAULT '',
                    outcome TEXT NOT NULL DEFAULT 'confirmed_success',
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

                CREATE INDEX IF NOT EXISTS idx_delta_conversation_sequence
                    ON deltas(conversation_id, global_sequence);
                CREATE INDEX IF NOT EXISTS idx_occurrence_message
                    ON object_occurrences(conversation_id, message_key);
                CREATE INDEX IF NOT EXISTS idx_occurrence_delta
                    ON object_occurrences(
                        delta_id, message_ordinal, part_ordinal, span_start,
                        span_end, occurrence_key
                    );
                CREATE INDEX IF NOT EXISTS idx_version_activity
                    ON object_versions(activity_state, location);
                CREATE INDEX IF NOT EXISTS idx_retrieval_turn
                    ON retrieval_events(conversation_id, turn_id);
                CREATE INDEX IF NOT EXISTS idx_projection_epoch_conversation_request
                    ON projection_epochs(conversation_id, request_sequence);
                """
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            stored_version = int(row["value"]) if row is not None else 1
            if stored_version > SCHEMA_VERSION or stored_version < 1:
                raise RuntimeError(
                    f"Unsupported Object Context V1 schema {stored_version} "
                    f"(expected {SCHEMA_VERSION})"
                )
            self._migrate_delta_exposure_schema(conn)
            self._migrate_projection_epoch_schema(conn)
            self._migrate_amortized_scheduler_schema(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) "
                "VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.commit()
            repair = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (OBJECT_REFS_REPAIR_KEY,),
            ).fetchone()
            if repair is None or str(repair["value"]) != "1":
                conn.execute("BEGIN IMMEDIATE")
                repair = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = ?",
                    (OBJECT_REFS_REPAIR_KEY,),
                ).fetchone()
                if repair is None or str(repair["value"]) != "1":
                    self._rebuild_all_delta_object_refs(conn)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, '1')",
                        (OBJECT_REFS_REPAIR_KEY,),
                    )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _migrate_delta_exposure_schema(conn: sqlite3.Connection) -> None:
        """Add the V1.1 exposure columns without rewriting legacy payloads.

        The column inventory, rather than only ``schema_meta``, is the source
        of truth so reopening a database after an interrupted migration is
        idempotent. Existing raw Deltas deliberately start unseen; existing
        compressed/Card bytes are left untouched.
        """

        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(deltas)").fetchall()
        }
        additions = {
            "raw_seen_count": "INTEGER NOT NULL DEFAULT 0",
            "first_seen_request_sequence": "INTEGER",
            "last_seen_request_sequence": "INTEGER",
            "first_seen_success_sequence": "INTEGER",
            "last_seen_success_sequence": "INTEGER",
            "eligibility_success_sequence": "INTEGER",
            "projection_epoch_id": "TEXT",
            "projected_at_request_sequence": "INTEGER",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE deltas ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _migrate_projection_epoch_schema(conn: sqlite3.Connection) -> None:
        """Complete interrupted/pre-release V3 telemetry migrations safely."""

        columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(projection_epochs)"
            ).fetchall()
        }
        additions = {
            "card_or_receipt_tokens": "INTEGER",
            "decision_mode": "TEXT NOT NULL DEFAULT 'normal'",
            "request_attempt_id": "TEXT",
            "policy_version": "TEXT",
            "batch_policy": "TEXT",
            "fixed_batch_size": "INTEGER",
            "baseline_state": "TEXT",
            "cache_granularity_tokens": "INTEGER",
            "hot_underexposed_count": "INTEGER",
            "hot_seen_delta_count": "INTEGER",
            "hot_seen_bucket_count": "INTEGER",
            "hot_tail_tokens": "INTEGER",
            "hot_overflow_tokens": "INTEGER",
            "hot_start_token_offset": "INTEGER",
            "pending_delta_count": "INTEGER",
            "pending_bucket_count": "INTEGER",
            "pending_raw_tokens": "INTEGER",
            "pending_gain_tokens": "INTEGER",
            "wait_area_token_requests": "REAL",
            "wait_loss_now": "REAL",
            "wait_loss_increment": "REAL",
            "wait_loss_projected": "REAL",
            "shared_cached_hot_tokens": "INTEGER",
            "shared_overhead_equivalent_tokens": "REAL",
            "crossing_margin": "REAL",
            "emergency_triggered": "INTEGER",
            "pending_count_over": "INTEGER",
            "pending_tokens_over": "INTEGER",
            "amortized_crossed": "INTEGER",
            "immediate_crossed": "INTEGER",
            "amortized_cache_read_weight": "REAL",
            "amortized_baseline_prompt_tokens": "INTEGER",
            "amortized_candidate_prompt_tokens": "INTEGER",
            "amortized_baseline_reusable_prefix_tokens": "INTEGER",
            "amortized_candidate_reusable_prefix_tokens": "INTEGER",
            "immediate_cache_penalty_equivalent_tokens": "REAL",
            "immediate_net_saving_equivalent_tokens": "REAL",
            "immediate_net_saving_usd": "REAL",
            "immediate_cache_read_weight": "REAL",
            "immediate_cache_write_weight": "REAL",
            "immediate_pricing_source": "TEXT",
            "immediate_pricing_version": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE projection_epochs ADD COLUMN {name} {declaration}"
                )

    @staticmethod
    def _migrate_amortized_scheduler_schema(conn: sqlite3.Connection) -> None:
        """Complete an interrupted V4 migration without backfilling wait area."""

        pending_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(pending_ledgers)").fetchall()
        }
        pending_additions = {
            "conversation_id": "TEXT NOT NULL DEFAULT ''",
            "delta_id": "TEXT",
            "entered_success_sequence": "INTEGER NOT NULL DEFAULT 0",
            "bucket_sequence": "INTEGER NOT NULL DEFAULT 0",
            "raw_tokens": "INTEGER NOT NULL DEFAULT 0",
            "projected_tokens": "INTEGER NOT NULL DEFAULT 0",
            "gain_tokens": "INTEGER NOT NULL DEFAULT 0",
            "wait_area_token_requests": "INTEGER NOT NULL DEFAULT 0",
            "last_accrued_success_sequence": "INTEGER",
            "ledger_generation": "INTEGER NOT NULL DEFAULT 1",
            "estimator_version": "TEXT NOT NULL DEFAULT ''",
            "pending_reason": "TEXT NOT NULL DEFAULT ''",
            "created_at": "REAL NOT NULL DEFAULT 0",
            "updated_at": "REAL NOT NULL DEFAULT 0",
        }
        for name, declaration in pending_additions.items():
            if name not in pending_columns:
                conn.execute(
                    f"ALTER TABLE pending_ledgers ADD COLUMN {name} {declaration}"
                )

        observation_columns = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(request_observations)"
            ).fetchall()
        }
        observation_additions = {
            "request_attempt_id": "TEXT",
            "conversation_id": "TEXT NOT NULL DEFAULT ''",
            "success_sequence": "INTEGER NOT NULL DEFAULT 0",
            "exposure_request_sequence": "INTEGER NOT NULL DEFAULT 0",
            "route_namespace_hash": "TEXT NOT NULL DEFAULT ''",
            "outcome": "TEXT NOT NULL DEFAULT 'confirmed_success'",
            "min_raw_exposures": "INTEGER NOT NULL DEFAULT 1",
            "raw_delta_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "pending_accruals_json": "TEXT NOT NULL DEFAULT '[]'",
            "raw_exposed_delta_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "accrued_delta_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "skipped_pending_delta_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "newly_eligible_delta_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "created_at": "REAL NOT NULL DEFAULT 0",
        }
        for name, declaration in observation_additions.items():
            if name not in observation_columns:
                conn.execute(
                    "ALTER TABLE request_observations ADD COLUMN "
                    f"{name} {declaration}"
                )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_pending_ledger_delta_id ON pending_ledgers(delta_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_ledger_conversation_bucket "
            "ON pending_ledgers(conversation_id, bucket_sequence, delta_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_request_observation_attempt "
            "ON request_observations(request_attempt_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_request_observation_conversation_success "
            "ON request_observations(conversation_id, success_sequence)"
        )

    @staticmethod
    def _new_object_id() -> str:
        return f"obj_{uuid.uuid4().hex[:24]}"

    @staticmethod
    def _object_ref(object_id: str, version: int) -> str:
        return f"object://{object_id}@v{version}"

    @staticmethod
    def parse_object_ref(object_ref: str) -> tuple[str, int] | None:
        match = OBJECT_REF_RE.fullmatch(str(object_ref or "").strip())
        if match is None:
            return None
        return match.group("object_id"), int(match.group("version"))

    def register_delta(
        self,
        *,
        delta_id: str,
        conversation_id: str,
        session_id: str,
        turn_id: str,
        kind: str,
        inference_id: str,
        turn_sequence: int,
        raw_view: Sequence[dict[str, Any]],
    ) -> DeltaRecord:
        raw_messages = tuple(dict(message) for message in raw_view)
        raw_view_json = canonical_json(list(raw_messages))
        raw_tokens = estimate_messages_tokens_rough(list(raw_messages))
        now = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM deltas WHERE delta_id = ?", (delta_id,)
            ).fetchone()
            if existing is not None and (
                str(existing["conversation_id"]) != conversation_id
                or str(existing["raw_view_json"]) != raw_view_json
            ):
                raise RuntimeError(
                    "delta identity collision with different conversation or raw view"
                )
            if existing is None:
                row = conn.execute(
                    "SELECT COALESCE(MAX(global_sequence), 0) AS seq "
                    "FROM deltas WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()
                global_sequence = int(row["seq"]) + 1
                conn.execute(
                    "INSERT INTO deltas("
                    "delta_id, conversation_id, session_id, turn_id, kind, "
                    "inference_id, turn_sequence, global_sequence, "
                    "raw_token_count, state, raw_view_json, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        delta_id,
                        conversation_id,
                        session_id,
                        turn_id,
                        kind,
                        inference_id,
                        int(turn_sequence),
                        global_sequence,
                        raw_tokens,
                        DeltaState.HOT.value,
                        raw_view_json,
                        now,
                    ),
                )
                existing = conn.execute(
                    "SELECT * FROM deltas WHERE delta_id = ?", (delta_id,)
                ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._delta_from_row(existing)

    def find_delta_by_raw_view(
        self,
        conversation_id: str,
        raw_view: Sequence[dict[str, Any]],
    ) -> DeltaRecord | None:
        """Resolve a previously observed Delta during restart reconciliation."""

        encoded = canonical_json([dict(message) for message in raw_view])
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deltas WHERE conversation_id = ? "
                "AND raw_view_json = ? ORDER BY global_sequence LIMIT 1",
                (conversation_id, encoded),
            ).fetchone()
        return self._delta_from_row(row) if row is not None else None

    def _delta_from_row(self, row: sqlite3.Row) -> DeltaRecord:
        compressed = row["compressed_view_json"]
        return DeltaRecord(
            delta_id=str(row["delta_id"]),
            conversation_id=str(row["conversation_id"]),
            session_id=str(row["session_id"]),
            turn_id=str(row["turn_id"]),
            kind=str(row["kind"]),
            inference_id=str(row["inference_id"] or ""),
            turn_sequence=int(row["turn_sequence"]),
            global_sequence=int(row["global_sequence"]),
            raw_token_count=int(row["raw_token_count"]),
            state=DeltaState(str(row["state"])),
            raw_view=tuple(json.loads(row["raw_view_json"])),
            object_refs=tuple(json.loads(row["object_refs_json"] or "[]")),
            compressed_view=(
                tuple(json.loads(compressed)) if compressed is not None else None
            ),
            raw_seen_count=max(0, int(row["raw_seen_count"] or 0)),
            first_seen_request_sequence=(
                int(row["first_seen_request_sequence"])
                if row["first_seen_request_sequence"] is not None
                else None
            ),
            last_seen_request_sequence=(
                int(row["last_seen_request_sequence"])
                if row["last_seen_request_sequence"] is not None
                else None
            ),
            first_seen_success_sequence=(
                int(row["first_seen_success_sequence"])
                if row["first_seen_success_sequence"] is not None
                else None
            ),
            last_seen_success_sequence=(
                int(row["last_seen_success_sequence"])
                if row["last_seen_success_sequence"] is not None
                else None
            ),
            eligibility_success_sequence=(
                int(row["eligibility_success_sequence"])
                if row["eligibility_success_sequence"] is not None
                else None
            ),
            projection_epoch_id=str(row["projection_epoch_id"] or ""),
            projected_at_request_sequence=(
                int(row["projected_at_request_sequence"])
                if row["projected_at_request_sequence"] is not None
                else None
            ),
        )

    def get_delta(self, delta_id: str) -> DeltaRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deltas WHERE delta_id = ?", (delta_id,)
            ).fetchone()
        return self._delta_from_row(row) if row is not None else None

    @staticmethod
    def _sync_delta_object_refs(conn: sqlite3.Connection, delta_id: str) -> None:
        """Rebuild one Delta's denormalized ref cache from occurrence truth."""

        rows = conn.execute(
            "SELECT object_ref FROM object_occurrences WHERE delta_id = ? "
            "ORDER BY message_ordinal, part_ordinal, span_start, span_end, "
            "occurrence_key",
            (delta_id,),
        ).fetchall()
        refs = list(dict.fromkeys(str(row["object_ref"]) for row in rows))
        encoded = canonical_json(refs)
        conn.execute(
            "UPDATE deltas SET object_refs_json = ? "
            "WHERE delta_id = ? AND object_refs_json != ?",
            (encoded, delta_id, encoded),
        )

    @staticmethod
    def _rebuild_all_delta_object_refs(conn: sqlite3.Connection) -> None:
        """Repair legacy caches once without touching object or occurrence rows."""

        refs_by_delta: dict[str, list[str]] = {}
        seen_by_delta: dict[str, set[str]] = {}
        for row in conn.execute(
            "SELECT delta_id, object_ref FROM object_occurrences "
            "ORDER BY delta_id, message_ordinal, part_ordinal, span_start, "
            "span_end, occurrence_key"
        ).fetchall():
            delta_id = str(row["delta_id"])
            object_ref = str(row["object_ref"])
            seen = seen_by_delta.setdefault(delta_id, set())
            if object_ref in seen:
                continue
            seen.add(object_ref)
            refs_by_delta.setdefault(delta_id, []).append(object_ref)

        updates = []
        for row in conn.execute(
            "SELECT delta_id, object_refs_json FROM deltas"
        ).fetchall():
            delta_id = str(row["delta_id"])
            encoded = canonical_json(refs_by_delta.get(delta_id, []))
            if str(row["object_refs_json"]) != encoded:
                updates.append((encoded, delta_id))
        if updates:
            conn.executemany(
                "UPDATE deltas SET object_refs_json = ? WHERE delta_id = ?",
                updates,
            )

    def list_deltas(self, conversation_id: str) -> list[DeltaRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deltas WHERE conversation_id = ? "
                "ORDER BY global_sequence",
                (conversation_id,),
            ).fetchall()
        return [self._delta_from_row(row) for row in rows]

    def max_request_sequence(self, conversation_id: str) -> int:
        """Return the largest durable selection sequence seen in this lineage."""

        with self._connect() as conn:
            return self._legacy_max_request_sequence_conn(conn, conversation_id)

    @staticmethod
    def _legacy_max_request_sequence_conn(
        conn: sqlite3.Connection, conversation_id: str
    ) -> int:
        row = conn.execute(
            "SELECT MAX(sequence) AS sequence FROM ("
            "SELECT COALESCE(first_seen_request_sequence, 0) AS sequence "
            "FROM deltas WHERE conversation_id = ? UNION ALL "
            "SELECT COALESCE(last_seen_request_sequence, 0) AS sequence "
            "FROM deltas WHERE conversation_id = ? UNION ALL "
            "SELECT COALESCE(projected_at_request_sequence, 0) AS sequence "
            "FROM deltas WHERE conversation_id = ?)",
            (conversation_id, conversation_id, conversation_id),
        ).fetchone()
        return max(0, int(row["sequence"] or 0)) if row is not None else 0

    def latest_success_sequence(self, conversation_id: str) -> int:
        """Return the latest exactly-once V4 success in one conversation."""

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(success_sequence), 0) AS sequence "
                "FROM request_observations WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        return max(0, int(row["sequence"] or 0)) if row is not None else 0

    @staticmethod
    def _next_success_sequence_conn(
        conn: sqlite3.Connection, conversation_id: str
    ) -> int:
        row = conn.execute(
            "SELECT MAX(success_sequence) AS sequence "
            "FROM request_observations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        if row is not None and row["sequence"] is not None:
            return int(row["sequence"]) + 1
        # V4 success boundaries are a separate, conversation-wide dense clock.
        # Legacy request/projection sequences were allocated by individual
        # engine instances and are neither dense nor globally unique, so they
        # must never seed this clock.  Migrated exposure counts are retained,
        # but the first real V4 observation is boundary one.
        return 1

    @staticmethod
    def _mark_raw_deltas_seen_conn(
        conn: sqlite3.Connection,
        delta_ids: Iterable[str],
        *,
        request_sequence: int,
        conversation_id: str | None = None,
        success_sequence: int | None = None,
        min_raw_exposures: int = 1,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        normalized = tuple(dict.fromkeys(str(delta_id) for delta_id in delta_ids))
        sequence = max(1, int(request_sequence))
        threshold = max(1, int(min_raw_exposures))
        updated: list[str] = []
        newly_eligible: list[str] = []
        for delta_id in normalized:
            row = conn.execute(
                "SELECT conversation_id, state, compressed_view_json, "
                "raw_seen_count, last_seen_request_sequence, "
                "last_seen_success_sequence, "
                "eligibility_success_sequence FROM deltas WHERE delta_id = ?",
                (delta_id,),
            ).fetchone()
            if row is None:
                continue
            if conversation_id is not None and str(row["conversation_id"]) != str(
                conversation_id
            ):
                raise RuntimeError(
                    f"raw Delta belongs to another conversation: {delta_id}"
                )
            if (
                str(row["state"]) == DeltaState.COMPRESSED.value
                or row["compressed_view_json"] is not None
            ):
                continue
            raw_seen_count = max(0, int(row["raw_seen_count"] or 0))
            current_success = (
                max(1, int(success_sequence))
                if success_sequence is not None
                else None
            )
            # V4 observations are idempotent by UUID and receive a dense,
            # conversation-wide success sequence inside this transaction.
            # The legacy request sequence is engine-local, so two concurrent
            # engines may legitimately reuse it; it must not suppress either
            # successful Raw exposure.
            exposure_is_new = (
                row["last_seen_success_sequence"] != current_success
                if current_success is not None
                else row["last_seen_request_sequence"] != sequence
            )
            if exposure_is_new:
                conn.execute(
                    "UPDATE deltas SET "
                    "raw_seen_count = raw_seen_count + 1, "
                    "first_seen_request_sequence = "
                    "COALESCE(first_seen_request_sequence, ?), "
                    "last_seen_request_sequence = ? WHERE delta_id = ?",
                    (sequence, sequence, delta_id),
                )
                raw_seen_count += 1

            if current_success is not None:
                conn.execute(
                    "UPDATE deltas SET first_seen_success_sequence = "
                    "COALESCE(first_seen_success_sequence, ?), "
                    "last_seen_success_sequence = ? WHERE delta_id = ?",
                    (current_success, current_success, delta_id),
                )
                if (
                    row["eligibility_success_sequence"] is None
                    and raw_seen_count >= threshold
                ):
                    conn.execute(
                        "UPDATE deltas SET eligibility_success_sequence = ? "
                        "WHERE delta_id = ? "
                        "AND eligibility_success_sequence IS NULL",
                        (current_success, delta_id),
                    )
                    newly_eligible.append(delta_id)
            updated.append(delta_id)
        return tuple(updated), tuple(newly_eligible)

    def mark_raw_deltas_seen(
        self, delta_ids: Iterable[str], *, request_sequence: int
    ) -> tuple[str, ...]:
        """Confirm one successful raw request exposure, once per sequence.

        Only still-unprojected rows are eligible. Replaying the same success
        notification is idempotent, while a later successful request increments
        the exposure count exactly once.
        """

        normalized = tuple(dict.fromkeys(str(delta_id) for delta_id in delta_ids))
        if not normalized:
            return ()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            updated, _ = self._mark_raw_deltas_seen_conn(
                conn,
                normalized,
                request_sequence=request_sequence,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return updated

    @staticmethod
    def _pending_ledger_from_row(row: sqlite3.Row) -> PendingLedgerRecord:
        return PendingLedgerRecord(
            conversation_id=str(row["conversation_id"]),
            delta_id=str(row["delta_id"]),
            entered_success_sequence=int(row["entered_success_sequence"]),
            bucket_sequence=int(row["bucket_sequence"]),
            raw_tokens=int(row["raw_tokens"]),
            projected_tokens=int(row["projected_tokens"]),
            gain_tokens=int(row["gain_tokens"]),
            wait_area_token_requests=int(row["wait_area_token_requests"]),
            last_accrued_success_sequence=(
                int(row["last_accrued_success_sequence"])
                if row["last_accrued_success_sequence"] is not None
                else None
            ),
            ledger_generation=int(row["ledger_generation"]),
            estimator_version=str(row["estimator_version"] or ""),
            pending_reason=str(row["pending_reason"] or ""),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )

    def get_pending_ledger(self, delta_id: str) -> PendingLedgerRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM pending_ledgers WHERE delta_id = ?", (delta_id,)
            ).fetchone()
        return self._pending_ledger_from_row(row) if row is not None else None

    def list_pending_ledgers(
        self,
        conversation_id: str,
        *,
        delta_ids: Iterable[str] | None = None,
    ) -> list[PendingLedgerRecord]:
        """List pending ledgers, optionally intersected with current raw IDs."""

        normalized = (
            None
            if delta_ids is None
            else tuple(sorted({str(delta_id) for delta_id in delta_ids}))
        )
        if normalized == ():
            return []
        rows: list[sqlite3.Row] = []
        with self._connect() as conn:
            if normalized is None:
                rows = conn.execute(
                    "SELECT * FROM pending_ledgers WHERE conversation_id = ? "
                    "ORDER BY bucket_sequence, delta_id",
                    (conversation_id,),
                ).fetchall()
            else:
                for start in range(0, len(normalized), 300):
                    chunk = normalized[start : start + 300]
                    placeholders = ",".join("?" for _ in chunk)
                    rows.extend(
                        conn.execute(
                            "SELECT * FROM pending_ledgers "
                            "WHERE conversation_id = ? "
                            f"AND delta_id IN ({placeholders})",
                            (conversation_id, *chunk),
                        ).fetchall()
                    )
        rows.sort(key=lambda row: (int(row["bucket_sequence"]), str(row["delta_id"])))
        return [self._pending_ledger_from_row(row) for row in rows]

    def retire_pending_ledger(
        self,
        *,
        conversation_id: str,
        delta_id: str,
    ) -> bool:
        """Remove a still-Raw Delta from Pending and restore neutral Hot state."""

        conversation = str(conversation_id or "")
        identity = str(delta_id or "")
        if not conversation or not identity:
            raise ValueError("pending ledger identity is incomplete")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT conversation_id, state, compressed_view_json, "
                "projection_epoch_id FROM deltas WHERE delta_id = ?",
                (identity,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"delta not found: {identity}")
            if str(row["conversation_id"]) != conversation:
                raise RuntimeError("pending Delta belongs to another conversation")
            if (
                str(row["state"]) == DeltaState.COMPRESSED.value
                or row["compressed_view_json"] is not None
                or str(row["projection_epoch_id"] or "")
            ):
                conn.commit()
                return False
            deleted = conn.execute(
                "DELETE FROM pending_ledgers "
                "WHERE conversation_id = ? AND delta_id = ?",
                (conversation, identity),
            ).rowcount
            if deleted:
                conn.execute(
                    "UPDATE deltas SET state = ?, failure_error = '' "
                    "WHERE delta_id = ? AND compressed_view_json IS NULL "
                    "AND COALESCE(projection_epoch_id, '') = ''",
                    (DeltaState.HOT.value, identity),
                )
            conn.commit()
            return bool(deleted)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_delta_eligibility_if_underexposed(
        self,
        *,
        conversation_id: str,
        delta_id: str,
        min_raw_exposures: int,
    ) -> bool:
        """Re-protect one Raw Delta after the configured exposure gate rises.

        Eligibility is a boundary fact for the threshold that was active when
        it was assigned.  If the current threshold is higher than the Delta's
        durable exposure count, the old boundary and any Pending ledger are no
        longer valid.  Clear both atomically; a later successful Raw carry will
        assign a new boundary when the new threshold is actually reached.
        """

        conversation = str(conversation_id or "")
        identity = str(delta_id or "")
        threshold = max(1, int(min_raw_exposures))
        if not conversation or not identity:
            raise ValueError("Delta eligibility identity is incomplete")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT conversation_id, state, raw_seen_count, "
                "eligibility_success_sequence, compressed_view_json, "
                "projection_epoch_id FROM deltas WHERE delta_id = ?",
                (identity,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"delta not found: {identity}")
            if str(row["conversation_id"]) != conversation:
                raise RuntimeError("Delta belongs to another conversation")
            if (
                str(row["state"]) == DeltaState.COMPRESSED.value
                or row["compressed_view_json"] is not None
                or str(row["projection_epoch_id"] or "")
            ):
                conn.commit()
                return False
            if int(row["raw_seen_count"] or 0) >= threshold:
                conn.commit()
                return False
            deleted = conn.execute(
                "DELETE FROM pending_ledgers "
                "WHERE conversation_id = ? AND delta_id = ?",
                (conversation, identity),
            ).rowcount
            had_boundary = row["eligibility_success_sequence"] is not None
            changed_state = str(row["state"]) != DeltaState.HOT.value
            if had_boundary or deleted or changed_state:
                conn.execute(
                    "UPDATE deltas SET eligibility_success_sequence = NULL, "
                    "state = ?, failure_error = '' WHERE delta_id = ? "
                    "AND compressed_view_json IS NULL "
                    "AND COALESCE(projection_epoch_id, '') = ''",
                    (DeltaState.HOT.value, identity),
                )
            conn.commit()
            return bool(had_boundary or deleted or changed_state)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def upsert_pending_ledger(
        self,
        *,
        conversation_id: str,
        delta_id: str,
        entered_success_sequence: int,
        raw_tokens: int,
        projected_tokens: int,
        gain_tokens: int | None = None,
        bucket_sequence: int | None = None,
        estimator_version: str = "",
        pending_reason: str = "",
        min_raw_exposures: int = 1,
        reset_wait_area: bool = False,
    ) -> PendingLedgerRecord:
        """Promote one raw Delta into the durable amortized pending set.

        Repeating an identical candidate preserves its accumulated wait area.
        Re-estimation must be explicit via ``reset_wait_area``; the generation
        changes for auditability, while the physical historical area remains
        intact and future requests accrue the new gain estimate.
        """

        conversation = str(conversation_id or "")
        identity = str(delta_id or "")
        if not conversation or not identity:
            raise ValueError("pending ledger identity is incomplete")
        entered = int(entered_success_sequence)
        if entered < 1:
            raise ValueError("entered_success_sequence must be positive")
        raw = int(raw_tokens)
        projected = int(projected_tokens)
        expected_gain = raw - projected
        gain = expected_gain if gain_tokens is None else int(gain_tokens)
        if raw <= 0 or projected < 0 or projected >= raw:
            raise ValueError("pending token estimates must satisfy 0 <= C < D")
        if gain <= 0 or gain != expected_gain:
            raise ValueError("gain_tokens must equal raw_tokens - projected_tokens")
        threshold = max(1, int(min_raw_exposures))
        estimator = str(estimator_version or "")
        reason = str(pending_reason or "")
        if len(estimator) > 200 or len(reason) > 500:
            raise ValueError("pending metadata exceeds its content-free bound")

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            delta = conn.execute(
                "SELECT conversation_id, state, raw_token_count, raw_seen_count, "
                "compressed_view_json, projection_epoch_id, "
                "eligibility_success_sequence FROM deltas WHERE delta_id = ?",
                (identity,),
            ).fetchone()
            if delta is None:
                raise RuntimeError(f"delta not found: {identity}")
            if str(delta["conversation_id"]) != conversation:
                raise RuntimeError("pending Delta belongs to another conversation")
            if (
                str(delta["state"]) == DeltaState.COMPRESSED.value
                or delta["compressed_view_json"] is not None
                or str(delta["projection_epoch_id"] or "")
            ):
                raise RuntimeError(f"delta is already projected: {identity}")
            if int(delta["raw_seen_count"] or 0) < threshold:
                raise RuntimeError(f"delta is still raw-unseen: {identity}")
            if int(delta["raw_token_count"]) != raw:
                raise ValueError("raw_tokens differs from the durable Delta estimate")
            if str(delta["state"]) not in {
                DeltaState.HOT.value,
                DeltaState.COMPRESSION_ELIGIBLE.value,
                DeltaState.COMPRESSION_SKIPPED.value,
                DeltaState.COMPRESSION_FAILED.value,
            }:
                raise RuntimeError(
                    f"delta is not pending-eligible: {identity} ({delta['state']})"
                )

            existing = conn.execute(
                "SELECT * FROM pending_ledgers WHERE delta_id = ?", (identity,)
            ).fetchone()
            durable_bucket_value = delta["eligibility_success_sequence"]
            durable_bucket = (
                int(durable_bucket_value)
                if durable_bucket_value is not None
                else None
            )
            latest_row = conn.execute(
                "SELECT COALESCE(MAX(success_sequence), 0) AS sequence "
                "FROM request_observations WHERE conversation_id = ?",
                (conversation,),
            ).fetchone()
            latest_success = max(0, int(latest_row["sequence"] or 0))
            if existing is None:
                if durable_bucket is None:
                    raise RuntimeError(
                        "Delta has no durable eligibility success boundary"
                    )
                bucket = int(
                    bucket_sequence
                    if bucket_sequence is not None
                    else durable_bucket
                )
                if bucket != durable_bucket:
                    raise ValueError(
                        "bucket_sequence must equal the durable eligibility boundary"
                    )
                if entered != latest_success:
                    raise ValueError(
                        "entered_success_sequence must equal the current success boundary"
                    )
                if bucket > entered:
                    raise ValueError(
                        "bucket_sequence must be within the success history"
                    )
            else:
                existing_entered = int(existing["entered_success_sequence"])
                existing_bucket = int(existing["bucket_sequence"])
                if entered != existing_entered:
                    raise ValueError(
                        "entered_success_sequence cannot change after promotion"
                    )
                if (
                    bucket_sequence is not None
                    and int(bucket_sequence) != existing_bucket
                ):
                    raise ValueError(
                        "bucket_sequence cannot change after promotion"
                    )
                if durable_bucket is not None and existing_bucket != durable_bucket:
                    raise RuntimeError(
                        "pending bucket differs from the durable eligibility boundary"
                    )
                entered = existing_entered
                bucket = existing_bucket
            now = time.time()
            if existing is None:
                conn.execute(
                    "INSERT INTO pending_ledgers("
                    "conversation_id, delta_id, entered_success_sequence, "
                    "bucket_sequence, raw_tokens, projected_tokens, gain_tokens, "
                    "wait_area_token_requests, last_accrued_success_sequence, "
                    "ledger_generation, estimator_version, pending_reason, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, "
                    "NULL, 1, ?, ?, ?, ?)",
                    (
                        conversation,
                        identity,
                        entered,
                        bucket,
                        raw,
                        projected,
                        gain,
                        estimator,
                        reason,
                        now,
                        now,
                    ),
                )
            elif reset_wait_area:
                conn.execute(
                    "UPDATE pending_ledgers SET raw_tokens = ?, projected_tokens = ?, "
                    "gain_tokens = ?, ledger_generation = ledger_generation + 1, "
                    "estimator_version = ?, pending_reason = ?, updated_at = ? "
                    "WHERE delta_id = ?",
                    (
                        raw,
                        projected,
                        gain,
                        estimator,
                        reason,
                        now,
                        identity,
                    ),
                )
            else:
                stable = (
                    str(existing["conversation_id"]) == conversation
                    and int(existing["bucket_sequence"]) == bucket
                    and int(existing["raw_tokens"]) == raw
                    and int(existing["projected_tokens"]) == projected
                    and int(existing["gain_tokens"]) == gain
                    and str(existing["estimator_version"] or "") == estimator
                )
                if not stable:
                    raise RuntimeError(
                        "pending estimate changed without reset_wait_area"
                    )
                conn.execute(
                    "UPDATE pending_ledgers SET pending_reason = ?, updated_at = ? "
                    "WHERE delta_id = ?",
                    (reason, now, identity),
                )

            conn.execute(
                "UPDATE deltas SET state = ?, failure_error = '' "
                "WHERE delta_id = ?",
                (DeltaState.COMPRESSION_ELIGIBLE.value, identity),
            )
            row = conn.execute(
                "SELECT * FROM pending_ledgers WHERE delta_id = ?", (identity,)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self._pending_ledger_from_row(row)

    def promote_pending_delta(self, **kwargs: Any) -> PendingLedgerRecord:
        """Named promotion API; delegates to the idempotent ledger upsert."""

        return self.upsert_pending_ledger(**kwargs)

    @staticmethod
    def _normalize_request_attempt_id(request_attempt_id: str | uuid.UUID) -> str:
        try:
            return str(uuid.UUID(str(request_attempt_id)))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError("request_attempt_id must be a UUID") from exc

    @staticmethod
    def _normalize_route_namespace_hash(route_namespace_hash: str) -> str:
        value = str(route_namespace_hash or "")
        if value and ROUTE_NAMESPACE_HASH_RE.fullmatch(value) is None:
            raise ValueError(
                "route_namespace_hash must be empty or a lowercase SHA-256 digest"
            )
        return value

    @staticmethod
    def _normalize_pending_accruals(
        pending_accruals: Iterable[
            PendingLedgerAccrual | Mapping[str, Any] | Sequence[Any]
        ],
    ) -> tuple[PendingLedgerAccrual, ...]:
        normalized: dict[str, PendingLedgerAccrual] = {}
        for value in pending_accruals:
            if isinstance(value, PendingLedgerAccrual):
                item = value
            elif isinstance(value, Mapping):
                item = PendingLedgerAccrual(
                    delta_id=str(value.get("delta_id") or ""),
                    gain_tokens=int(value.get("gain_tokens") or 0),
                    ledger_generation=int(value.get("ledger_generation") or 0),
                )
            elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                if len(value) != 3:
                    raise ValueError("pending accrual tuple must have three fields")
                item = PendingLedgerAccrual(
                    delta_id=str(value[0]),
                    gain_tokens=int(value[1]),
                    ledger_generation=int(value[2]),
                )
            else:
                raise TypeError("unsupported pending accrual snapshot")
            if not item.delta_id or item.gain_tokens <= 0 or item.ledger_generation < 1:
                raise ValueError("pending accrual snapshot is invalid")
            prior = normalized.get(item.delta_id)
            if prior is not None and prior != item:
                raise ValueError("conflicting pending accrual snapshots")
            normalized[item.delta_id] = item
        return tuple(normalized[key] for key in sorted(normalized))

    @staticmethod
    def _observation_result_from_row(
        row: sqlite3.Row, *, duplicate: bool
    ) -> SuccessfulRequestObservationResult:
        return SuccessfulRequestObservationResult(
            request_attempt_id=str(row["request_attempt_id"]),
            conversation_id=str(row["conversation_id"]),
            success_sequence=int(row["success_sequence"]),
            exposure_request_sequence=int(row["exposure_request_sequence"]),
            route_namespace_hash=str(row["route_namespace_hash"] or ""),
            outcome=str(row["outcome"] or CONFIRMED_SUCCESS_OUTCOME),
            duplicate=duplicate,
            raw_exposed_delta_ids=tuple(
                json.loads(str(row["raw_exposed_delta_ids_json"] or "[]"))
            ),
            accrued_delta_ids=tuple(
                json.loads(str(row["accrued_delta_ids_json"] or "[]"))
            ),
            skipped_pending_delta_ids=tuple(
                json.loads(str(row["skipped_pending_delta_ids_json"] or "[]"))
            ),
            newly_eligible_delta_ids=tuple(
                json.loads(str(row["newly_eligible_delta_ids_json"] or "[]"))
            ),
        )

    def confirm_successful_request_observation(
        self,
        *,
        conversation_id: str,
        request_attempt_id: str | uuid.UUID,
        raw_delta_ids: Iterable[str],
        pending_accruals: Iterable[
            PendingLedgerAccrual | Mapping[str, Any] | Sequence[Any]
        ] = (),
        exposure_request_sequence: int | None = None,
        min_raw_exposures: int = 1,
        route_namespace_hash: str = "",
    ) -> SuccessfulRequestObservationResult:
        """Atomically record one successful request and all scheduler effects.

        ``pending_accruals`` is the immutable ledger-generation/gain snapshot
        taken when the request was rendered. Only snapshotted ledgers that were
        actually carried raw accrue one unit of their snapshotted gain.
        """

        conversation = str(conversation_id or "")
        if not conversation:
            raise ValueError("conversation_id is required")
        attempt_id = self._normalize_request_attempt_id(request_attempt_id)
        route_hash = self._normalize_route_namespace_hash(route_namespace_hash)
        raw_ids = tuple(
            sorted({str(delta_id) for delta_id in raw_delta_ids if str(delta_id)})
        )
        snapshots = self._normalize_pending_accruals(pending_accruals)
        snapshot_json = canonical_json(
            [
                {
                    "delta_id": item.delta_id,
                    "gain_tokens": item.gain_tokens,
                    "ledger_generation": item.ledger_generation,
                }
                for item in snapshots
            ]
        )
        raw_json = canonical_json(raw_ids)
        threshold = max(1, int(min_raw_exposures))

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM request_observations "
                "WHERE request_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                same_payload = (
                    str(existing["conversation_id"]) == conversation
                    and str(existing["route_namespace_hash"] or "") == route_hash
                    and str(existing["outcome"] or "")
                    == CONFIRMED_SUCCESS_OUTCOME
                    and str(existing["raw_delta_ids_json"]) == raw_json
                    and str(existing["pending_accruals_json"]) == snapshot_json
                    and int(existing["min_raw_exposures"]) == threshold
                    and (
                        exposure_request_sequence is None
                        or int(existing["exposure_request_sequence"])
                        == max(1, int(exposure_request_sequence))
                    )
                )
                if not same_payload:
                    raise RuntimeError(
                        "request_attempt_id replayed with different observation"
                    )
                result = self._observation_result_from_row(
                    existing, duplicate=True
                )
                conn.commit()
                return result

            success_sequence = self._next_success_sequence_conn(conn, conversation)
            exposure_sequence = (
                success_sequence
                if exposure_request_sequence is None
                else max(1, int(exposure_request_sequence))
            )
            raw_exposed, newly_eligible = self._mark_raw_deltas_seen_conn(
                conn,
                raw_ids,
                request_sequence=exposure_sequence,
                conversation_id=conversation,
                success_sequence=success_sequence,
                min_raw_exposures=threshold,
            )
            raw_set = set(raw_exposed)
            accrued: list[str] = []
            skipped: list[str] = []
            now = time.time()
            for snapshot in snapshots:
                if snapshot.delta_id not in raw_set:
                    skipped.append(snapshot.delta_id)
                    continue
                row = conn.execute(
                    "SELECT p.*, d.state, d.compressed_view_json, "
                    "d.projection_epoch_id FROM pending_ledgers AS p "
                    "JOIN deltas AS d ON d.delta_id = p.delta_id "
                    "WHERE p.delta_id = ? AND p.conversation_id = ?",
                    (snapshot.delta_id, conversation),
                ).fetchone()
                if (
                    row is None
                    or str(row["state"]) == DeltaState.COMPRESSED.value
                    or row["compressed_view_json"] is not None
                    or str(row["projection_epoch_id"] or "")
                    or int(row["entered_success_sequence"]) >= success_sequence
                ):
                    skipped.append(snapshot.delta_id)
                    continue
                same_generation = (
                    int(row["ledger_generation"]) == snapshot.ledger_generation
                )
                if same_generation and int(row["gain_tokens"]) != snapshot.gain_tokens:
                    raise RuntimeError(
                        f"pending gain snapshot mismatch: {snapshot.delta_id}"
                    )
                last_accrued = row["last_accrued_success_sequence"]
                if last_accrued is not None and int(last_accrued) >= success_sequence:
                    skipped.append(snapshot.delta_id)
                    continue
                cursor = conn.execute(
                    "UPDATE pending_ledgers SET wait_area_token_requests = "
                    "wait_area_token_requests + ?, "
                    "last_accrued_success_sequence = ?, updated_at = ? "
                    "WHERE delta_id = ? AND conversation_id = ?",
                    (
                        snapshot.gain_tokens,
                        success_sequence,
                        now,
                        snapshot.delta_id,
                        conversation,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"pending ledger changed during accrual: {snapshot.delta_id}"
                    )
                accrued.append(snapshot.delta_id)

            conn.execute(
                "INSERT INTO request_observations("
                "request_attempt_id, conversation_id, success_sequence, "
                "exposure_request_sequence, route_namespace_hash, outcome, "
                "min_raw_exposures, "
                "raw_delta_ids_json, pending_accruals_json, "
                "raw_exposed_delta_ids_json, accrued_delta_ids_json, "
                "skipped_pending_delta_ids_json, "
                "newly_eligible_delta_ids_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt_id,
                    conversation,
                    success_sequence,
                    exposure_sequence,
                    route_hash,
                    CONFIRMED_SUCCESS_OUTCOME,
                    threshold,
                    raw_json,
                    snapshot_json,
                    canonical_json(raw_exposed),
                    canonical_json(accrued),
                    canonical_json(skipped),
                    canonical_json(newly_eligible),
                    now,
                ),
            )
            stored = conn.execute(
                "SELECT * FROM request_observations "
                "WHERE request_attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            result = self._observation_result_from_row(stored, duplicate=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return result

    def record_successful_request_observation(
        self, **kwargs: Any
    ) -> SuccessfulRequestObservationResult:
        """Compatibility spelling for the atomic success-observation API."""

        return self.confirm_successful_request_observation(**kwargs)

    def request_observation_timeline(
        self, conversation_id: str
    ) -> list[dict[str, Any]]:
        """Return a content-free summary of confirmed request observations."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT request_attempt_id, success_sequence, "
                "exposure_request_sequence, route_namespace_hash, outcome, "
                "raw_delta_ids_json, accrued_delta_ids_json, "
                "skipped_pending_delta_ids_json, "
                "newly_eligible_delta_ids_json, created_at "
                "FROM request_observations WHERE conversation_id = ? "
                "ORDER BY success_sequence, created_at, request_attempt_id",
                (conversation_id,),
            ).fetchall()
        timeline: list[dict[str, Any]] = []
        for row in rows:
            timeline.append({
                "request_attempt_id": str(row["request_attempt_id"]),
                "success_sequence": int(row["success_sequence"]),
                "exposure_request_sequence": int(
                    row["exposure_request_sequence"]
                ),
                "route_namespace_hash": str(row["route_namespace_hash"] or ""),
                "outcome": str(row["outcome"] or CONFIRMED_SUCCESS_OUTCOME),
                "raw_delta_count": len(
                    json.loads(str(row["raw_delta_ids_json"] or "[]"))
                ),
                "accrued_delta_count": len(
                    json.loads(str(row["accrued_delta_ids_json"] or "[]"))
                ),
                "skipped_pending_delta_count": len(
                    json.loads(
                        str(row["skipped_pending_delta_ids_json"] or "[]")
                    )
                ),
                "newly_eligible_delta_count": len(
                    json.loads(
                        str(row["newly_eligible_delta_ids_json"] or "[]")
                    )
                ),
                "created_at": float(row["created_at"]),
            })
        return timeline

    @staticmethod
    def _normalize_projection_v12_telemetry(
        record: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Validate and normalize nullable, content-free V1.2 telemetry."""

        normalized: dict[str, Any] = {}
        request_attempt_id = record.get("request_attempt_id")
        normalized["request_attempt_id"] = (
            None
            if request_attempt_id in {None, ""}
            else ObjectContextStore._normalize_request_attempt_id(
                request_attempt_id
            )
        )
        policy_version = record.get("policy_version")
        if policy_version is None:
            normalized["policy_version"] = None
        elif not isinstance(policy_version, str) or not (
            PROJECTION_POLICY_VERSION_RE.fullmatch(policy_version)
        ):
            raise ValueError("projection policy_version is invalid")
        else:
            normalized["policy_version"] = policy_version

        batch_policy = record.get("batch_policy")
        if batch_policy is None:
            normalized["batch_policy"] = None
        elif not isinstance(batch_policy, str) or (
            batch_policy not in PROJECTION_BATCH_POLICIES
        ):
            raise ValueError("projection batch_policy is invalid")
        else:
            normalized["batch_policy"] = batch_policy

        baseline_state = record.get("baseline_state")
        if baseline_state is None:
            normalized["baseline_state"] = None
        elif not isinstance(baseline_state, str) or (
            baseline_state not in PROJECTION_BASELINE_STATES
        ):
            raise ValueError("projection baseline_state is invalid")
        else:
            normalized["baseline_state"] = baseline_state

        for name in (
            PROJECTION_V12_POSITIVE_INTEGER_FIELDS
            | PROJECTION_V12_NONNEGATIVE_INTEGER_FIELDS
        ):
            value = record.get(name)
            if value is None:
                normalized[name] = None
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"projection {name} must be an integer")
            minimum = 1 if name in PROJECTION_V12_POSITIVE_INTEGER_FIELDS else 0
            if value < minimum:
                raise ValueError(f"projection {name} must be >= {minimum}")
            normalized[name] = value

        for name in (
            PROJECTION_V12_NONNEGATIVE_NUMBER_FIELDS
            | PROJECTION_V12_FINITE_NUMBER_FIELDS
        ):
            value = record.get(name)
            if value is None:
                normalized[name] = None
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"projection {name} must be a finite number")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"projection {name} must be finite")
            if (
                name in PROJECTION_V12_NONNEGATIVE_NUMBER_FIELDS
                and number < 0.0
            ):
                raise ValueError(f"projection {name} must be nonnegative")
            normalized[name] = number

        for name in PROJECTION_V12_BOOLEAN_FIELDS:
            value = record.get(name)
            if value is None:
                normalized[name] = None
            elif not isinstance(value, bool):
                raise TypeError(f"projection {name} must be bool")
            else:
                normalized[name] = value

        for name in ("immediate_pricing_source", "immediate_pricing_version"):
            value = record.get(name)
            if value is None:
                normalized[name] = None
                continue
            if not isinstance(value, str) or len(value) > 200:
                raise TypeError(f"projection {name} must be a bounded string")
            if value and PROJECTION_METADATA_TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"projection {name} is invalid")
            if name == "immediate_pricing_source" and not value:
                raise ValueError("projection immediate_pricing_source is empty")
            normalized[name] = value

        ordered_pairs = (
            ("hot_seen_bucket_count", "hot_seen_delta_count"),
            ("pending_bucket_count", "pending_delta_count"),
            ("pending_gain_tokens", "pending_raw_tokens"),
            ("hot_overflow_tokens", "hot_tail_tokens"),
        )
        for smaller_name, larger_name in ordered_pairs:
            smaller = normalized[smaller_name]
            larger = normalized[larger_name]
            if smaller is not None and larger is not None and smaller > larger:
                raise ValueError(
                    f"projection {smaller_name} exceeds {larger_name}"
                )
        return normalized

    @staticmethod
    def _validate_projection_v12_relationships(params: Mapping[str, Any]) -> None:
        """Reject incomplete or internally inconsistent V1.2 score telemetry.

        V1.1 columns stay nullable for migration compatibility.  A row that
        explicitly claims V1.2, however, is an auditable policy decision: all
        primitive facts needed to recompute W, Q, the fixed-policy request
        score, and the independent V1.1 immediate counterfactual are required.
        """

        policy_version = str(params.get("policy_version") or "")
        if policy_version != "1.2":
            # Current V1.1 rows may carry a few shared request facts (for
            # example baseline_state and request_attempt_id), while migrated
            # historical rows keep every added column NULL.  Only an explicit
            # V1.2 policy claim activates the complete W/Q contract below.
            return

        required = {
            "request_attempt_id",
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
            "immediate_cache_read_weight",
            "immediate_cache_write_weight",
            "immediate_pricing_source",
            "immediate_pricing_version",
            "baseline_prompt_tokens",
            "candidate_prompt_tokens",
            "gross_tokens_removed",
            "baseline_reusable_prefix_tokens",
            "candidate_reusable_prefix_tokens",
            "cache_tokens_invalidated",
            "cache_penalty_equivalent_tokens",
            "known_summary_cost_equivalent_tokens",
            "net_saving_equivalent_tokens",
            "cache_read_weight",
            "cache_write_weight",
            "pricing_source",
            "pricing_version",
            "estimator_source",
        }
        missing = sorted(name for name in required if params.get(name) is None)
        if missing:
            raise ValueError(
                "V1.2 projection telemetry is incomplete: " + ", ".join(missing)
            )

        for name, allow_empty in (
            ("pricing_source", False),
            ("pricing_version", True),
            ("estimator_source", False),
            ("immediate_pricing_source", False),
            ("immediate_pricing_version", True),
        ):
            value = params.get(name)
            if not isinstance(value, str) or len(value) > 200:
                raise TypeError(f"projection {name} must be a bounded string")
            if not value and not allow_empty:
                raise ValueError(f"projection {name} is empty")
            if value and PROJECTION_METADATA_TOKEN_RE.fullmatch(value) is None:
                raise ValueError(f"projection {name} is invalid")

        def nonnegative_int(name: str) -> int:
            value = params.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"projection {name} must be an integer")
            if value < 0:
                raise ValueError(f"projection {name} must be nonnegative")
            return value

        def finite_number(name: str, *, nonnegative: bool = False) -> float:
            value = params.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"projection {name} must be a finite number")
            result = float(value)
            if not math.isfinite(result):
                raise ValueError(f"projection {name} must be finite")
            if nonnegative and result < 0.0:
                raise ValueError(f"projection {name} must be nonnegative")
            return result

        def require_close(actual_name: str, expected: float) -> None:
            actual = finite_number(actual_name)
            if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError(f"projection {actual_name} is inconsistent")

        granularity = nonnegative_int("cache_granularity_tokens")
        if granularity < 1:
            raise ValueError("projection cache_granularity_tokens must be positive")
        policy_weight = finite_number(
            "amortized_cache_read_weight", nonnegative=True
        )
        batch_policy = str(params["batch_policy"])
        if batch_policy not in PROJECTION_BATCH_POLICIES:
            raise ValueError("projection batch_policy is invalid")
        fixed_batch_size = nonnegative_int("fixed_batch_size")
        if fixed_batch_size < 1:
            raise ValueError("projection fixed_batch_size must be positive")
        selected_batch_size = len(tuple(params.get("member_delta_ids") or ()))
        decision_mode = str(params.get("decision_mode") or "")
        decision_kind = str(params.get("decision_kind") or "")
        decision_reason = str(params.get("decision_reason") or "")
        if decision_mode == "fixed":
            if batch_policy != "fixed":
                raise ValueError("fixed decision mode requires fixed batch policy")
            if decision_kind == "flush" and (
                decision_reason != "FLUSH_FIXED_BATCH_SIZE"
                or selected_batch_size != fixed_batch_size
            ):
                raise ValueError("fixed flush does not match configured batch size")
        if policy_weight > 1.0:
            raise ValueError(
                "V1.2 amortized_cache_read_weight must be within [0, 1]"
            )
        primary_read = finite_number("cache_read_weight", nonnegative=True)
        primary_write = finite_number("cache_write_weight", nonnegative=True)
        if not math.isclose(primary_read, policy_weight, abs_tol=1e-12):
            raise ValueError("V1.2 primary cache read weight must equal policy weight")
        if not math.isclose(primary_write, 1.0, abs_tol=1e-12):
            raise ValueError("V1.2 primary cache write weight must equal one")

        # Validate the primary (actual action) full-render score.  Emergency
        # may choose a different batch from all Pending, hence this score has
        # its own T/L facts below.
        main_t0 = nonnegative_int("baseline_prompt_tokens")
        main_tc = nonnegative_int("candidate_prompt_tokens")
        main_l0 = nonnegative_int("baseline_reusable_prefix_tokens")
        main_lc = nonnegative_int("candidate_reusable_prefix_tokens")
        if main_tc > main_t0:
            raise ValueError("projection candidate prompt exceeds baseline")
        if main_l0 > main_t0 or main_lc > min(main_l0, main_tc):
            raise ValueError("projection reusable-prefix facts exceed request lengths")
        if main_l0 % granularity or main_lc % granularity:
            raise ValueError("projection reusable prefixes are not block-rounded")
        main_gain = main_t0 - main_tc
        main_invalidated = main_l0 - main_lc
        if nonnegative_int("gross_tokens_removed") != main_gain:
            raise ValueError("projection gross_tokens_removed is inconsistent")
        if nonnegative_int("cache_tokens_invalidated") != main_invalidated:
            raise ValueError("projection cache_tokens_invalidated is inconsistent")
        main_penalty = main_invalidated * max(0.0, primary_write - primary_read)
        require_close("cache_penalty_equivalent_tokens", main_penalty)
        summary_cost = finite_number(
            "known_summary_cost_equivalent_tokens", nonnegative=True
        )
        require_close(
            "net_saving_equivalent_tokens",
            main_gain * primary_write - main_penalty - summary_cost,
        )

        # Validate the all-Pending summary-free policy view used by W/Q and by
        # the independent immediate counterfactual.
        policy_t0 = nonnegative_int("amortized_baseline_prompt_tokens")
        policy_tc = nonnegative_int("amortized_candidate_prompt_tokens")
        policy_l0 = nonnegative_int(
            "amortized_baseline_reusable_prefix_tokens"
        )
        policy_lc = nonnegative_int(
            "amortized_candidate_reusable_prefix_tokens"
        )
        if policy_tc > policy_t0:
            raise ValueError("V1.2 policy candidate prompt exceeds baseline")
        if policy_l0 > policy_t0 or policy_lc > min(policy_l0, policy_tc):
            raise ValueError("V1.2 policy prefix facts exceed request lengths")
        if policy_l0 % granularity or policy_lc % granularity:
            raise ValueError("V1.2 policy prefixes are not block-rounded")

        hot_start = nonnegative_int("hot_start_token_offset")
        if hot_start > policy_t0:
            raise ValueError("projection Hot start exceeds the baseline request")
        shared_tokens = nonnegative_int("shared_cached_hot_tokens")
        expected_shared = max(
            0,
            policy_l0 - max(policy_lc, hot_start),
        )
        if shared_tokens != expected_shared:
            raise ValueError("projection shared_cached_hot_tokens is inconsistent")

        area = finite_number("wait_area_token_requests", nonnegative=True)
        pending_gain = nonnegative_int("pending_gain_tokens")
        require_close("wait_loss_now", policy_weight * area)
        require_close("wait_loss_increment", policy_weight * pending_gain)
        wait_now = finite_number("wait_loss_now", nonnegative=True)
        wait_increment = finite_number("wait_loss_increment", nonnegative=True)
        require_close("wait_loss_projected", wait_now + wait_increment)
        require_close(
            "shared_overhead_equivalent_tokens",
            (1.0 - policy_weight) * shared_tokens,
        )
        wait_projected = finite_number("wait_loss_projected", nonnegative=True)
        shared_overhead = finite_number(
            "shared_overhead_equivalent_tokens", nonnegative=True
        )
        require_close("crossing_margin", wait_projected - shared_overhead)

        immediate_read = finite_number(
            "immediate_cache_read_weight", nonnegative=True
        )
        immediate_write = finite_number(
            "immediate_cache_write_weight", nonnegative=True
        )
        policy_invalidated = policy_l0 - policy_lc
        immediate_penalty = policy_invalidated * max(
            0.0, immediate_write - immediate_read
        )
        require_close(
            "immediate_cache_penalty_equivalent_tokens", immediate_penalty
        )
        require_close(
            "immediate_net_saving_equivalent_tokens",
            (policy_t0 - policy_tc) * immediate_write - immediate_penalty,
        )

        baseline_state = str(params["baseline_state"])
        amortized_crossed = bool(params["amortized_crossed"])
        pending_count = nonnegative_int("pending_delta_count")
        if baseline_state in {"cold", "unknown"}:
            if policy_l0 or policy_lc or shared_tokens or shared_overhead:
                raise ValueError(
                    f"{baseline_state} baseline cannot claim reusable cache"
                )
        if baseline_state == "unknown" and amortized_crossed:
            raise ValueError("unknown baseline cannot claim amortized crossing")
        expected_crossing = (
            baseline_state in {"known", "cold"}
            and pending_count > 0
            and wait_projected >= shared_overhead
        )
        if amortized_crossed != expected_crossing:
            raise ValueError("projection amortized_crossed is inconsistent")

    @staticmethod
    def _insert_projection_decision(
        conn: sqlite3.Connection, record: Mapping[str, Any]
    ) -> None:
        """Insert one deliberately content-free V1.1/V1.2 decision record."""

        unexpected = set(record).difference(PROJECTION_DECISION_FIELDS)
        if unexpected:
            raise ValueError(
                "projection decision contains unsupported fields: "
                + ", ".join(sorted(unexpected))
            )
        epoch_id = str(record.get("projection_epoch_id") or "")
        conversation_id = str(record.get("conversation_id") or "")
        decision_kind = str(record.get("decision_kind") or "")
        decision_mode = str(record.get("decision_mode") or "normal")
        decision_reason = str(record.get("decision_reason") or "")
        if not epoch_id or not conversation_id or not decision_reason:
            raise ValueError("projection decision identity is incomplete")
        if PROJECTION_DECISION_REASON_RE.fullmatch(decision_reason) is None:
            raise ValueError("projection decision_reason is invalid")
        if decision_kind not in {"wait", "flush", "emergency"}:
            raise ValueError("projection decision kind is invalid")
        if decision_mode not in PROJECTION_DECISION_MODES:
            raise ValueError("projection decision mode is invalid")
        member_delta_ids = tuple(
            dict.fromkeys(
                str(value)
                for value in (record.get("member_delta_ids") or ())
                if str(value)
            )
        )
        member_object_refs = tuple(
            dict.fromkeys(
                str(value)
                for value in (record.get("member_object_refs") or ())
                if str(value)
            )
        )
        cache_read_weight = max(
            0.0, float(record.get("cache_read_weight") or 0.0)
        )
        cache_write_weight = max(
            0.0, float(record.get("cache_write_weight") or 0.0)
        )
        if not math.isfinite(cache_read_weight) or not math.isfinite(
            cache_write_weight
        ):
            raise ValueError("projection cache weights must be finite")
        params = {
            **{key: record.get(key) for key in PROJECTION_DECISION_FIELDS},
            **ObjectContextStore._normalize_projection_v12_telemetry(record),
            "projection_epoch_id": epoch_id,
            "conversation_id": conversation_id,
            "session_id": str(record.get("session_id") or ""),
            "request_sequence": max(0, int(record.get("request_sequence") or 0)),
            "decision_kind": decision_kind,
            "decision_mode": decision_mode,
            "decision_reason": decision_reason,
            "candidate_count": max(0, int(record.get("candidate_count") or 0)),
            "member_delta_ids_json": canonical_json(member_delta_ids),
            "member_object_refs_json": canonical_json(member_object_refs),
            "earliest_changed_delta_id": str(
                record.get("earliest_changed_delta_id") or ""
            ),
            "cache_read_weight": cache_read_weight,
            "cache_write_weight": cache_write_weight,
            "pricing_source": str(record.get("pricing_source") or ""),
            "pricing_version": str(record.get("pricing_version") or ""),
            "estimator_source": str(record.get("estimator_source") or ""),
            "created_at": time.time(),
        }
        ObjectContextStore._validate_projection_v12_relationships(params)
        conn.execute(
            "INSERT INTO projection_epochs("
            "projection_epoch_id, conversation_id, session_id, request_sequence, "
            "decision_kind, decision_mode, decision_reason, candidate_count, "
            "member_delta_ids_json, member_object_refs_json, "
            "earliest_changed_delta_id, baseline_prompt_tokens, "
            "candidate_prompt_tokens, gross_tokens_removed, "
            "card_or_receipt_tokens, "
            "baseline_reusable_prefix_tokens, candidate_reusable_prefix_tokens, "
            "cache_tokens_invalidated, cache_penalty_equivalent_tokens, "
            "known_summary_cost_equivalent_tokens, net_saving_equivalent_tokens, "
            "net_saving_usd, cache_read_weight, cache_write_weight, pricing_source, "
            "pricing_version, estimator_source, request_attempt_id, "
            "policy_version, batch_policy, fixed_batch_size, baseline_state, "
            "cache_granularity_tokens, "
            "hot_underexposed_count, hot_seen_delta_count, hot_seen_bucket_count, "
            "hot_tail_tokens, hot_overflow_tokens, hot_start_token_offset, "
            "pending_delta_count, pending_bucket_count, pending_raw_tokens, "
            "pending_gain_tokens, wait_area_token_requests, wait_loss_now, "
            "wait_loss_increment, wait_loss_projected, shared_cached_hot_tokens, "
            "shared_overhead_equivalent_tokens, crossing_margin, "
            "emergency_triggered, pending_count_over, pending_tokens_over, "
            "amortized_crossed, immediate_crossed, "
            "amortized_cache_read_weight, "
            "amortized_baseline_prompt_tokens, "
            "amortized_candidate_prompt_tokens, "
            "amortized_baseline_reusable_prefix_tokens, "
            "amortized_candidate_reusable_prefix_tokens, "
            "immediate_cache_penalty_equivalent_tokens, "
            "immediate_net_saving_equivalent_tokens, immediate_net_saving_usd, "
            "immediate_cache_read_weight, immediate_cache_write_weight, "
            "immediate_pricing_source, immediate_pricing_version, created_at"
            ") VALUES ("
            ":projection_epoch_id, :conversation_id, :session_id, "
            ":request_sequence, :decision_kind, :decision_mode, :decision_reason, "
            ":candidate_count, :member_delta_ids_json, "
            ":member_object_refs_json, :earliest_changed_delta_id, "
            ":baseline_prompt_tokens, :candidate_prompt_tokens, "
            ":gross_tokens_removed, :card_or_receipt_tokens, "
            ":baseline_reusable_prefix_tokens, "
            ":candidate_reusable_prefix_tokens, :cache_tokens_invalidated, "
            ":cache_penalty_equivalent_tokens, "
            ":known_summary_cost_equivalent_tokens, "
            ":net_saving_equivalent_tokens, :net_saving_usd, "
            ":cache_read_weight, :cache_write_weight, :pricing_source, "
            ":pricing_version, :estimator_source, :request_attempt_id, "
            ":policy_version, :batch_policy, :fixed_batch_size, "
            ":baseline_state, :cache_granularity_tokens, "
            ":hot_underexposed_count, :hot_seen_delta_count, "
            ":hot_seen_bucket_count, :hot_tail_tokens, :hot_overflow_tokens, "
            ":hot_start_token_offset, :pending_delta_count, "
            ":pending_bucket_count, :pending_raw_tokens, :pending_gain_tokens, "
            ":wait_area_token_requests, :wait_loss_now, :wait_loss_increment, "
            ":wait_loss_projected, :shared_cached_hot_tokens, "
            ":shared_overhead_equivalent_tokens, :crossing_margin, "
            ":emergency_triggered, :pending_count_over, :pending_tokens_over, "
            ":amortized_crossed, :immediate_crossed, "
            ":amortized_cache_read_weight, "
            ":amortized_baseline_prompt_tokens, "
            ":amortized_candidate_prompt_tokens, "
            ":amortized_baseline_reusable_prefix_tokens, "
            ":amortized_candidate_reusable_prefix_tokens, "
            ":immediate_cache_penalty_equivalent_tokens, "
            ":immediate_net_saving_equivalent_tokens, :immediate_net_saving_usd, "
            ":immediate_cache_read_weight, :immediate_cache_write_weight, "
            ":immediate_pricing_source, :immediate_pricing_version, :created_at)",
            params,
        )

    def record_projection_decision(self, record: Mapping[str, Any]) -> None:
        with self._connect() as conn:
            self._insert_projection_decision(conn, record)

    def projection_decisions(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM projection_epochs WHERE conversation_id = ? "
                "ORDER BY request_sequence, created_at, projection_epoch_id",
                (conversation_id,),
            ).fetchall()
        decisions: list[dict[str, Any]] = []
        for row in rows:
            decision = dict(row)
            decision["member_delta_ids"] = json.loads(
                str(decision.pop("member_delta_ids_json") or "[]")
            )
            decision["member_object_refs"] = json.loads(
                str(decision.pop("member_object_refs_json") or "[]")
            )
            for name in PROJECTION_V12_BOOLEAN_FIELDS:
                if decision.get(name) is not None:
                    decision[name] = bool(decision[name])
            decisions.append(decision)
        return decisions

    def projection_decision_conversations(self) -> list[dict[str, Any]]:
        """List roots with V1.1 decisions, including wait-only conversations."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id, COUNT(*) AS decision_count, "
                "MIN(created_at) AS first_decision_at, "
                "MAX(created_at) AS last_decision_at "
                "FROM projection_epochs GROUP BY conversation_id "
                "ORDER BY last_decision_at DESC, conversation_id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_delta_states(
        self, states: dict[str, DeltaState], *, expected: DeltaState | None = None
    ) -> None:
        if not states:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for delta_id, state in states.items():
                if expected is None:
                    conn.execute(
                        "UPDATE deltas SET state = ? WHERE delta_id = ?",
                        (state.value, delta_id),
                    )
                else:
                    conn.execute(
                        "UPDATE deltas SET state = ? WHERE delta_id = ? AND state = ?",
                        (state.value, delta_id, expected.value),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_delta_skipped(self, delta_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE deltas SET state = ?, compressed_view_json = NULL, "
                "compressed_at = ?, failure_error = '' WHERE delta_id = ? "
                "AND state IN (?, ?, ?)",
                (
                    DeltaState.COMPRESSION_SKIPPED.value,
                    time.time(),
                    delta_id,
                    DeltaState.COMPRESSION_ELIGIBLE.value,
                    DeltaState.COMPRESSING.value,
                    DeltaState.COMPRESSION_FAILED.value,
                ),
            )

    def register_object(
        self,
        *,
        conversation_id: str,
        session_id: str,
        delta: DeltaRecord,
        detected: DetectedObject,
        base_ref: str = "",
        derived_from: Iterable[str] = (),
    ) -> ObjectRecord:
        now = time.time()
        digest = exact_sha256(detected.content)
        raw = encode_exact(detected.content)
        derived = tuple(dict.fromkeys(str(ref) for ref in derived_from if ref))
        conn = self._connect()
        existing_ref = ""
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_occurrence = conn.execute(
                "SELECT object_ref, delta_id FROM object_occurrences "
                "WHERE occurrence_key = ?",
                (detected.occurrence_key,),
            ).fetchone()
            if existing_occurrence is not None:
                if str(existing_occurrence["delta_id"]) != delta.delta_id:
                    raise RuntimeError(
                        "object occurrence identity collision with a different Delta"
                    )
                existing_ref = str(existing_occurrence["object_ref"])
            else:
                conn.execute(
                    "INSERT OR IGNORE INTO blobs("
                    "sha256, content, byte_size, char_count, token_count, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        digest,
                        sqlite3.Binary(raw),
                        len(raw),
                        len(detected.content),
                        estimate_tokens_rough(detected.content),
                        now,
                    ),
                )

                object_id = ""
                version = 1
                supersedes = ""
                if self.parse_object_ref(base_ref) is not None:
                    base = conn.execute(
                        "SELECT v.object_id, o.conversation_id "
                        "FROM object_versions AS v "
                        "JOIN logical_objects AS o ON o.object_id = v.object_id "
                        "WHERE v.object_ref = ?",
                        (base_ref,),
                    ).fetchone()
                    if (
                        base is not None
                        and str(base["conversation_id"]) == conversation_id
                    ):
                        object_id = str(base["object_id"])
                        latest = conn.execute(
                            "SELECT MAX(version) AS version FROM object_versions "
                            "WHERE object_id = ?",
                            (object_id,),
                        ).fetchone()
                        version = int(latest["version"] or 0) + 1
                        supersedes = base_ref

                if not object_id:
                    object_id = self._new_object_id()
                    conn.execute(
                        "INSERT INTO logical_objects("
                        "object_id, conversation_id, object_type, canonical_name, "
                        "created_at_delta, created_at"
                        ") VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            object_id,
                            conversation_id,
                            detected.object_type.value,
                            detected.name,
                            delta.global_sequence,
                            now,
                        ),
                    )

                object_ref = self._object_ref(object_id, version)
                conn.execute(
                    "INSERT INTO object_versions("
                    "object_ref, object_id, version, sha256, name, language, "
                    "metadata_json, supersedes_ref, derived_from_json, "
                    "activity_state, created_at_delta, last_accessed_delta, "
                    "location, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        object_ref,
                        object_id,
                        version,
                        digest,
                        detected.name,
                        detected.language,
                        canonical_json(detected.metadata),
                        supersedes,
                        canonical_json(derived),
                        ActivityState.ACTIVE.value,
                        delta.global_sequence,
                        delta.global_sequence,
                        ObjectLocation.WORKING_MEMORY.value,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO object_occurrences("
                    "occurrence_key, conversation_id, session_id, delta_id, "
                    "object_ref, message_key, message_ordinal, part_ordinal, "
                    "span_start, span_end, whole_part, detection_method, "
                    "source_role, tool_name, tool_call_id, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        detected.occurrence_key,
                        conversation_id,
                        session_id,
                        delta.delta_id,
                        object_ref,
                        detected.message_key,
                        detected.message_ordinal,
                        detected.part_ordinal,
                        detected.start,
                        detected.end,
                        1 if detected.whole_part else 0,
                        detected.detection_method,
                        detected.source_role,
                        detected.tool_name,
                        detected.tool_call_id,
                        now,
                    ),
                )
                existing_ref = object_ref
            self._sync_delta_object_refs(conn, delta.delta_id)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        record = self.get_object(conversation_id, existing_ref)
        if record is None:
            raise RuntimeError("stored object could not be resolved")
        return record

    def create_object_version(
        self,
        *,
        conversation_id: str,
        base_ref: str,
        content: str,
        object_type: ObjectType,
        name: str = "",
        language: str = "",
        metadata: dict[str, Any] | None = None,
        derived_from: Iterable[str] = (),
    ) -> ObjectRecord:
        """Create an explicit immutable version without inventing a trace Delta.

        Versions created by an editor/runtime API are registry objects, not
        assistant messages.  A later real occurrence may reference the returned
        ref, but this operation must not fabricate conversation history merely
        to satisfy the occurrence schema.
        """

        if self.parse_object_ref(base_ref) is None:
            raise ValueError("base_ref must be an exact immutable object reference")
        requested_derived = tuple(
            dict.fromkeys(str(ref) for ref in derived_from if str(ref))
        )
        digest = exact_sha256(content)
        raw = encode_exact(content)
        now = time.time()
        new_ref = ""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            base = conn.execute(
                "SELECT v.*, o.conversation_id, o.object_type "
                "FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE v.object_ref = ? AND o.conversation_id = ?",
                (base_ref, conversation_id),
            ).fetchone()
            if base is None:
                raise PermissionError(
                    "base object is missing or outside this conversation"
                )
            if str(base["object_type"]) != object_type.value:
                raise ValueError("an immutable logical object's type cannot change")
            for relation_ref in requested_derived:
                if self.parse_object_ref(relation_ref) is None:
                    raise ValueError(
                        "derived_from contains an invalid object reference"
                    )
                allowed = conn.execute(
                    "SELECT 1 FROM object_versions AS v "
                    "JOIN logical_objects AS o ON o.object_id = v.object_id "
                    "WHERE v.object_ref = ? AND o.conversation_id = ?",
                    (relation_ref, conversation_id),
                ).fetchone()
                if allowed is None:
                    raise PermissionError(
                        "derived_from object is outside this conversation"
                    )

            conn.execute(
                "INSERT OR IGNORE INTO blobs("
                "sha256, content, byte_size, char_count, token_count, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    digest,
                    sqlite3.Binary(raw),
                    len(raw),
                    len(content),
                    estimate_tokens_rough(content),
                    now,
                ),
            )
            latest = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM object_versions WHERE object_id = ?",
                (str(base["object_id"]),),
            ).fetchone()
            version = int(latest["version"] or 0) + 1
            new_ref = self._object_ref(str(base["object_id"]), version)
            sequence = conn.execute(
                "SELECT COALESCE(MAX(global_sequence), ?) AS sequence "
                "FROM deltas WHERE conversation_id = ?",
                (int(base["created_at_delta"]), conversation_id),
            ).fetchone()
            created_at_delta = int(sequence["sequence"] or 0)
            merged_metadata = json.loads(base["metadata_json"] or "{}")
            merged_metadata.update(metadata or {})
            conn.execute(
                "INSERT INTO object_versions("
                "object_ref, object_id, version, sha256, name, language, "
                "metadata_json, supersedes_ref, derived_from_json, "
                "activity_state, created_at_delta, last_accessed_delta, "
                "location, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_ref,
                    str(base["object_id"]),
                    version,
                    digest,
                    name or str(base["name"] or ""),
                    language or str(base["language"] or ""),
                    canonical_json(merged_metadata),
                    base_ref,
                    canonical_json(requested_derived),
                    ActivityState.ACTIVE.value,
                    created_at_delta,
                    created_at_delta,
                    ObjectLocation.WORKING_MEMORY.value,
                    now,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        record = self.get_object(conversation_id, new_ref)
        if record is None:
            raise RuntimeError("new immutable object version could not be resolved")
        return record

    def _object_from_row(self, row: sqlite3.Row) -> ObjectRecord:
        raw = row["content"]
        blob = bytes(raw) if not isinstance(raw, bytes) else raw
        content = decode_exact(blob)
        digest = exact_sha256(content)
        if digest != str(row["sha256"]):
            raise RuntimeError("OBJECT_HASH_MISMATCH")
        return ObjectRecord(
            object_ref=str(row["object_ref"]),
            object_id=str(row["object_id"]),
            version=int(row["version"]),
            object_type=ObjectType(str(row["object_type"])),
            content=content,
            sha256=digest,
            byte_size=int(row["byte_size"]),
            char_count=int(row["char_count"]),
            token_count=int(row["token_count"]),
            conversation_id=str(row["conversation_id"]),
            source_delta_id=str(row["delta_id"] or ""),
            source_message_key=str(row["message_key"] or ""),
            source_message_ordinal=int(row["message_ordinal"] or 0),
            source_part_ordinal=int(row["part_ordinal"] or 0),
            source_start=int(row["span_start"] or 0),
            source_end=int(row["span_end"] or 0),
            name=str(row["name"] or ""),
            language=str(row["language"] or ""),
            metadata=json.loads(row["metadata_json"] or "{}"),
            supersedes=str(row["supersedes_ref"] or ""),
            derived_from=tuple(json.loads(row["derived_from_json"] or "[]")),
            summary=str(row["summary"] or ""),
            contains=json.loads(row["contains_json"] or "{}"),
            card_text=str(row["card_text"] or ""),
            activity_state=ActivityState(str(row["activity_state"])),
            pinned=bool(row["pinned"]),
            created_at_delta=int(row["created_at_delta"]),
            last_accessed_delta=int(row["last_accessed_delta"]),
            inactive_since_delta=(
                int(row["inactive_since_delta"])
                if row["inactive_since_delta"] is not None
                else None
            ),
            location=ObjectLocation(str(row["location"])),
        )

    def get_object(self, conversation_id: str, object_ref: str) -> ObjectRecord | None:
        if self.parse_object_ref(object_ref) is None:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT v.*, o.conversation_id, o.object_type, b.content, "
                "b.byte_size, b.char_count, b.token_count, "
                "occ.delta_id, occ.message_key, occ.message_ordinal, "
                "occ.part_ordinal, occ.span_start, occ.span_end "
                "FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "JOIN blobs AS b ON b.sha256 = v.sha256 "
                "LEFT JOIN object_occurrences AS occ "
                "  ON occ.object_ref = v.object_ref "
                "WHERE v.object_ref = ? AND o.conversation_id = ? "
                "ORDER BY occ.created_at LIMIT 1",
                (object_ref, conversation_id),
            ).fetchone()
        return self._object_from_row(row) if row is not None else None

    def object_exists(self, object_ref: str) -> bool:
        if self.parse_object_ref(object_ref) is None:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM object_versions WHERE object_ref = ?",
                (object_ref,),
            ).fetchone()
        return row is not None

    def list_objects(self, conversation_id: str) -> list[ObjectRecord]:
        with self._connect() as conn:
            refs = [
                str(row["object_ref"])
                for row in conn.execute(
                    "SELECT v.object_ref FROM object_versions AS v "
                    "JOIN logical_objects AS o ON o.object_id = v.object_id "
                    "WHERE o.conversation_id = ? "
                    "ORDER BY v.created_at_delta, v.version",
                    (conversation_id,),
                ).fetchall()
            ]
        records: list[ObjectRecord] = []
        for ref in refs:
            record = self.get_object(conversation_id, ref)
            if record is not None:
                records.append(record)
        return records

    def occurrences_for_delta(self, delta_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT occ.*, v.card_text, v.summary, v.contains_json, "
                "v.object_id, v.version, v.name, v.language, v.metadata_json, "
                "v.supersedes_ref, v.derived_from_json, o.object_type "
                "FROM object_occurrences AS occ "
                "JOIN object_versions AS v ON v.object_ref = occ.object_ref "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE occ.delta_id = ? "
                "ORDER BY occ.message_ordinal, occ.part_ordinal, occ.span_start",
                (delta_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def occurrence_cards_for_messages(
        self, conversation_id: str, message_keys: Iterable[str]
    ) -> dict[str, list[dict[str, Any]]]:
        keys = sorted({key for key in message_keys if key})
        if not keys:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        with self._connect() as conn:
            for start in range(0, len(keys), 300):
                chunk = keys[start : start + 300]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT occ.*, v.card_text, d.state "
                    "FROM object_occurrences AS occ "
                    "JOIN object_versions AS v ON v.object_ref = occ.object_ref "
                    "JOIN deltas AS d ON d.delta_id = occ.delta_id "
                    f"WHERE occ.conversation_id = ? "
                    f"AND occ.message_key IN ({placeholders}) "
                    "AND d.state = ? ORDER BY occ.part_ordinal, occ.span_start",
                    (conversation_id, *chunk, DeltaState.COMPRESSED.value),
                ).fetchall()
                for row in rows:
                    if not row["card_text"]:
                        continue
                    result.setdefault(str(row["message_key"]), []).append(dict(row))
        return result

    def publish_cards_and_compressed_delta(
        self,
        *,
        delta_id: str,
        cards: Sequence[tuple[str, str, str, dict[str, Any]]],
        compressed_view: Sequence[dict[str, Any]],
    ) -> None:
        """Atomically publish validated Cards and the Delta compressed view."""

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM deltas WHERE delta_id = ?", (delta_id,)
            ).fetchone()
            if row is None:
                raise RuntimeError("delta not found")
            if str(row["state"]) not in {
                DeltaState.COMPRESSION_ELIGIBLE.value,
                DeltaState.COMPRESSING.value,
                DeltaState.COMPRESSION_FAILED.value,
            }:
                raise RuntimeError(f"delta is not compressible: {row['state']}")
            for object_ref, summary, card_text, contains in cards:
                exists = conn.execute(
                    "SELECT 1 FROM object_versions WHERE object_ref = ?",
                    (object_ref,),
                ).fetchone()
                if exists is None:
                    raise RuntimeError(f"Card target missing: {object_ref}")
                conn.execute(
                    "UPDATE object_versions SET summary = ?, contains_json = ?, "
                    "card_text = ? WHERE object_ref = ?",
                    (summary, canonical_json(contains), card_text, object_ref),
                )
            conn.execute(
                "UPDATE deltas SET state = ?, compressed_view_json = ?, "
                "compressed_at = ?, failure_error = '' WHERE delta_id = ?",
                (
                    DeltaState.COMPRESSED.value,
                    canonical_json(list(compressed_view)),
                    time.time(),
                    delta_id,
                ),
            )
            # Legacy/manual compression can target a Delta that was already
            # promoted by the V1.2 scheduler.  Its durable Pending ledger must
            # disappear in the same transaction as the compressed view just as
            # it does for publish_compressed_batch().
            conn.execute(
                "DELETE FROM pending_ledgers WHERE delta_id = ?",
                (delta_id,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def publish_compressed_batch(
        self,
        batch: Sequence[
            tuple[
                str,
                Sequence[tuple[str, str, str, dict[str, Any]]],
                Sequence[dict[str, Any]],
            ]
        ],
        *,
        projection_epoch_id: str = "",
        request_sequence: int | None = None,
        min_raw_exposures: int = 0,
        known_object_refs_by_delta: dict[str, Sequence[str]] | None = None,
        projection_decision: Mapping[str, Any] | None = None,
    ) -> None:
        """Atomically verify and publish one immutable projection batch."""

        if not batch:
            return
        batch_ids = tuple(str(item[0]) for item in batch)
        if any(not delta_id for delta_id in batch_ids):
            raise ValueError("projection batch contains an empty Delta identity")
        if len(set(batch_ids)) != len(batch_ids):
            raise ValueError("projection batch contains duplicate Delta identities")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_conversation_id = ""
            verified: list[
                tuple[
                    str,
                    Sequence[tuple[str, str, str, dict[str, Any]]],
                    Sequence[dict[str, Any]],
                ]
            ] = []
            for delta_id, cards, compressed_view in batch:
                row = conn.execute(
                    "SELECT conversation_id, state, raw_seen_count, projection_epoch_id, "
                    "compressed_view_json FROM deltas WHERE delta_id = ?",
                    (delta_id,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"delta not found: {delta_id}")
                row_conversation_id = str(row["conversation_id"] or "")
                if not batch_conversation_id:
                    batch_conversation_id = row_conversation_id
                elif row_conversation_id != batch_conversation_id:
                    raise RuntimeError(
                        "projection batch spans multiple conversations"
                    )
                allowed_states = {
                    DeltaState.COMPRESSION_ELIGIBLE.value,
                    DeltaState.COMPRESSING.value,
                    DeltaState.COMPRESSION_FAILED.value,
                }
                if projection_epoch_id:
                    # Economic/amortized epochs may atomically publish any
                    # legal Raw-seen candidate selected by the request-time
                    # planner.  ``COMPRESSION_SKIPPED`` is a retryable
                    # scheduler result, not a permanent safety exclusion;
                    # emergency planning is explicitly allowed to cross it.
                    allowed_states.update(
                        {
                            DeltaState.HOT.value,
                            DeltaState.COMPRESSION_SKIPPED.value,
                        }
                    )
                if str(row["state"]) not in allowed_states:
                    raise RuntimeError(
                        f"delta is not compressible: {delta_id} ({row['state']})"
                    )
                if row["compressed_view_json"] is not None or str(
                    row["projection_epoch_id"] or ""
                ):
                    raise RuntimeError(f"delta is already projected: {delta_id}")
                if int(row["raw_seen_count"] or 0) < max(
                    0, int(min_raw_exposures)
                ):
                    raise RuntimeError(f"delta is still raw-unseen: {delta_id}")
                for object_ref in dict.fromkeys(
                    (known_object_refs_by_delta or {}).get(delta_id, ())
                ):
                    target = conn.execute(
                        "SELECT 1 FROM object_versions AS v "
                        "JOIN logical_objects AS o ON o.object_id = v.object_id "
                        "WHERE v.object_ref = ? AND o.conversation_id = ?",
                        (str(object_ref), str(row["conversation_id"])),
                    ).fetchone()
                    event = conn.execute(
                        "SELECT 1 FROM retrieval_events "
                        "WHERE conversation_id = ? AND object_ref = ? "
                        "AND status = 'success' LIMIT 1",
                        (str(row["conversation_id"]), str(object_ref)),
                    ).fetchone()
                    if target is None or event is None:
                        raise RuntimeError(
                            f"retrieval receipt target missing: {object_ref}"
                        )
                for object_ref, summary, card_text, contains in cards:
                    target = conn.execute(
                        "SELECT v.summary, v.contains_json, v.card_text "
                        "FROM object_versions AS v "
                        "JOIN object_occurrences AS occ "
                        "ON occ.object_ref = v.object_ref "
                        "WHERE v.object_ref = ? AND occ.delta_id = ? LIMIT 1",
                        (object_ref, delta_id),
                    ).fetchone()
                    if target is None:
                        raise RuntimeError(f"Card target missing: {object_ref}")
                    existing_card = str(target["card_text"] or "")
                    if existing_card and (
                        existing_card != card_text
                        or str(target["summary"] or "") != summary
                        or str(target["contains_json"] or "{}")
                        != canonical_json(contains)
                    ):
                        raise RuntimeError(
                            f"immutable Card bytes changed: {object_ref}"
                        )
                verified.append((delta_id, cards, compressed_view))

            if projection_decision is not None:
                decision_conversation = str(
                    projection_decision.get("conversation_id") or ""
                )
                if decision_conversation != batch_conversation_id:
                    raise RuntimeError(
                        "projection decision belongs to another conversation"
                    )
                if str(projection_decision.get("decision_kind") or "") not in {
                    "flush",
                    "emergency",
                }:
                    raise RuntimeError(
                        "published projection decision must be flush/emergency"
                    )
                decision_members = tuple(
                    dict.fromkeys(
                        str(value)
                        for value in (
                            projection_decision.get("member_delta_ids") or ()
                        )
                        if str(value)
                    )
                )
                if decision_members != batch_ids:
                    raise RuntimeError(
                        "projection decision members disagree with batch"
                    )
                if (
                    str(projection_decision.get("policy_version") or "") == "1.2"
                    and str(projection_decision.get("decision_mode") or "")
                    in {"amortized", "capacity"}
                ):
                    ledger_count = int(
                        conn.execute(
                            "SELECT COUNT(*) AS count FROM pending_ledgers "
                            f"WHERE delta_id IN ({','.join('?' for _ in batch_ids)})",
                            batch_ids,
                        ).fetchone()["count"]
                    )
                    if ledger_count != len(batch_ids):
                        raise RuntimeError(
                            "V1.2 normal/capacity batch must publish all Pending members"
                        )

            for delta_id, _, _ in verified:
                conn.execute(
                    "UPDATE deltas SET state = ? WHERE delta_id = ?",
                    (DeltaState.COMPRESSING.value, delta_id),
                )
            for delta_id, cards, compressed_view in verified:
                for object_ref, summary, card_text, contains in cards:
                    conn.execute(
                        "UPDATE object_versions SET summary = ?, "
                        "contains_json = ?, card_text = ? "
                        "WHERE object_ref = ? AND card_text = ''",
                        (
                            summary,
                            canonical_json(contains),
                            card_text,
                            object_ref,
                        ),
                    )
                conn.execute(
                    "UPDATE deltas SET state = ?, compressed_view_json = ?, "
                    "compressed_at = ?, failure_error = '', "
                    "projection_epoch_id = ?, "
                    "projected_at_request_sequence = ? WHERE delta_id = ?",
                    (
                        DeltaState.COMPRESSED.value,
                        canonical_json(list(compressed_view)),
                        time.time(),
                        projection_epoch_id or None,
                        request_sequence,
                        delta_id,
                    ),
                )
                conn.execute(
                    "DELETE FROM pending_ledgers WHERE delta_id = ?",
                    (delta_id,),
                )
            if projection_decision is not None:
                if str(projection_decision.get("projection_epoch_id") or "") != str(
                    projection_epoch_id or ""
                ):
                    raise RuntimeError(
                        "projection decision and batch epoch identities disagree"
                    )
                self._insert_projection_decision(conn, projection_decision)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_delta_failed(self, delta_id: str, error: str) -> None:
        self.mark_deltas_failed_if_unprojected((delta_id,), error)

    def mark_deltas_failed_if_unprojected(
        self, delta_ids: Iterable[str], error: str
    ) -> tuple[str, ...]:
        """Publish failure state without clobbering a concurrent winning epoch."""

        updated: list[str] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for delta_id in dict.fromkeys(str(value) for value in delta_ids):
                cursor = conn.execute(
                    "UPDATE deltas SET state = ?, failure_error = ? "
                    "WHERE delta_id = ? AND compressed_view_json IS NULL "
                    "AND COALESCE(projection_epoch_id, '') = '' "
                    "AND state IN (?, ?, ?, ?, ?)",
                    (
                        DeltaState.COMPRESSION_FAILED.value,
                        str(error)[:1000],
                        delta_id,
                        DeltaState.HOT.value,
                        DeltaState.COMPRESSION_ELIGIBLE.value,
                        DeltaState.COMPRESSING.value,
                        DeltaState.COMPRESSION_FAILED.value,
                        DeltaState.COMPRESSION_SKIPPED.value,
                    ),
                )
                if cursor.rowcount:
                    updated.append(delta_id)
        return tuple(updated)

    def mount_retrieval(
        self,
        *,
        conversation_id: str,
        turn_id: str,
        object_ref: str,
        tool_call_id: str,
        reason: str,
        mounted_at_delta: int,
    ) -> RetrievalLease:
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO retrieval_leases("
                "conversation_id, turn_id, object_ref, tool_call_id, reason, "
                "mounted_at_delta, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'turn_end')",
                (
                    conversation_id,
                    turn_id,
                    object_ref,
                    tool_call_id,
                    reason,
                    mounted_at_delta,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO retrieval_events("
                "conversation_id, turn_id, tool_call_id, object_ref, reason, "
                "status, mounted_at_delta, created_at"
                ") VALUES (?, ?, ?, ?, ?, 'success', ?, ?)",
                (
                    conversation_id,
                    turn_id,
                    tool_call_id,
                    object_ref,
                    reason,
                    mounted_at_delta,
                    now,
                ),
            )
            conn.execute(
                "UPDATE object_versions SET last_accessed_delta = ?, "
                "activity_state = ?, inactive_since_delta = NULL, location = ? "
                "WHERE object_ref = ?",
                (
                    mounted_at_delta,
                    ActivityState.ACTIVE.value,
                    ObjectLocation.WORKING_MEMORY.value,
                    object_ref,
                ),
            )
        return RetrievalLease(
            turn_id=turn_id,
            object_ref=object_ref,
            mounted_at_delta=mounted_at_delta,
            tool_call_id=tool_call_id,
            reason=reason,
        )

    def mark_accessed(
        self, conversation_id: str, object_ref: str, *, at_delta: int
    ) -> bool:
        """Mark an authorized object active and logically resident again."""

        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE object_versions SET last_accessed_delta = ?, "
                "activity_state = ?, inactive_since_delta = NULL, location = ? "
                "WHERE object_ref = ? AND object_id IN ("
                "SELECT object_id FROM logical_objects WHERE conversation_id = ?)",
                (
                    int(at_delta),
                    ActivityState.ACTIVE.value,
                    ObjectLocation.WORKING_MEMORY.value,
                    object_ref,
                    conversation_id,
                ),
            )
        return cursor.rowcount > 0

    def list_leases(
        self, conversation_id: str, turn_id: str | None = None
    ) -> list[RetrievalLease]:
        sql = "SELECT * FROM retrieval_leases WHERE conversation_id = ?"
        params: tuple[Any, ...] = (conversation_id,)
        if turn_id is not None:
            sql += " AND turn_id = ?"
            params += (turn_id,)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            RetrievalLease(
                turn_id=str(row["turn_id"]),
                object_ref=str(row["object_ref"]),
                mounted_at_delta=int(row["mounted_at_delta"]),
                tool_call_id=str(row["tool_call_id"] or ""),
                reason=str(row["reason"]),
                expires_at=str(row["expires_at"]),
            )
            for row in rows
        ]

    def unmount_turn(self, conversation_id: str, turn_id: str, *, at_delta: int) -> int:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM retrieval_leases "
                "WHERE conversation_id = ? AND turn_id = ?",
                (conversation_id, turn_id),
            ).fetchone()["n"]
            conn.execute(
                "DELETE FROM retrieval_leases "
                "WHERE conversation_id = ? AND turn_id = ?",
                (conversation_id, turn_id),
            )
            conn.execute(
                "UPDATE retrieval_events SET unmounted_at_delta = ? "
                "WHERE conversation_id = ? AND turn_id = ? "
                "AND unmounted_at_delta IS NULL",
                (at_delta, conversation_id, turn_id),
            )
            conn.commit()
            return int(count)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def retrieval_event_for_tool_call(
        self, conversation_id: str, tool_call_id: str
    ) -> dict[str, Any] | None:
        if not tool_call_id:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM retrieval_events WHERE conversation_id = ? "
                "AND tool_call_id = ? ORDER BY id DESC LIMIT 1",
                (conversation_id, tool_call_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def retrieval_count_for_ref(self, conversation_id: str, object_ref: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS count FROM retrieval_events "
                "WHERE conversation_id = ? AND object_ref = ? "
                "AND status = 'success'",
                (conversation_id, object_ref),
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def retrieval_totals_for_turns(
        self, conversation_id: str, turn_ids: Iterable[str]
    ) -> dict[str, int]:
        """Return content-free successful-retrieval totals for exact turns.

        A conversation can be projected by independent background agents that
        intentionally reuse its Object Context store.  The monitor must not
        attribute those agents' retrievals to the user-facing main request
        timeline, so it scopes retrieval counts and payload size through the
        durable turn identity instead of the conversation-wide metric sum.
        """

        normalized = tuple(
            dict.fromkeys(
                str(turn_id).strip()
                for turn_id in turn_ids
                if str(turn_id).strip()
            )
        )
        if not normalized:
            return {"retrieval_count": 0, "retrieved_tokens": 0}
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS retrieval_count, "
                "COALESCE(SUM(b.token_count), 0) AS retrieved_tokens "
                "FROM retrieval_events AS r "
                "LEFT JOIN object_versions AS v ON v.object_ref = r.object_ref "
                "LEFT JOIN blobs AS b ON b.sha256 = v.sha256 "
                "WHERE r.conversation_id = ? AND r.status = 'success' "
                f"AND r.turn_id IN ({placeholders})",
                (conversation_id, *normalized),
            ).fetchone()
        return {
            "retrieval_count": int(row["retrieval_count"] if row else 0),
            "retrieved_tokens": int(row["retrieved_tokens"] if row else 0),
        }

    def was_retrieved_in_previous_user_turn(
        self,
        conversation_id: str,
        current_turn_id: str,
        object_ref: str,
    ) -> bool:
        """Return whether the immediately preceding real User Delta retrieved it."""

        with self._connect() as conn:
            current = conn.execute(
                "SELECT MIN(global_sequence) AS sequence FROM deltas "
                "WHERE conversation_id = ? AND turn_id = ? AND kind = 'user'",
                (conversation_id, current_turn_id),
            ).fetchone()
            if current is None or current["sequence"] is None:
                return False
            previous = conn.execute(
                "SELECT turn_id FROM deltas WHERE conversation_id = ? "
                "AND kind = 'user' AND global_sequence < ? "
                "ORDER BY global_sequence DESC LIMIT 1",
                (conversation_id, int(current["sequence"])),
            ).fetchone()
            if previous is None:
                return False
            retrieved = conn.execute(
                "SELECT 1 FROM retrieval_events WHERE conversation_id = ? "
                "AND turn_id = ? AND object_ref = ? AND status = 'success' "
                "LIMIT 1",
                (conversation_id, str(previous["turn_id"]), object_ref),
            ).fetchone()
        return retrieved is not None

    def pin(self, conversation_id: str, object_ref: str, pinned: bool) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE object_versions SET pinned = ?, activity_state = ?, "
                "inactive_since_delta = NULL, location = ? WHERE object_ref = ? "
                "AND object_id IN (SELECT object_id FROM logical_objects "
                "WHERE conversation_id = ?)",
                (
                    1 if pinned else 0,
                    ActivityState.ACTIVE.value,
                    ObjectLocation.WORKING_MEMORY.value,
                    object_ref,
                    conversation_id,
                ),
            )
        return cursor.rowcount > 0

    def update_activity(
        self,
        *,
        conversation_id: str,
        current_delta: int,
        active_refs: set[str],
        recent_access_deltas: int,
        grace_deltas: int,
    ) -> dict[str, int]:
        counts = {state.value: 0 for state in ActivityState}
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT v.* FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE o.conversation_id = ?",
                (conversation_id,),
            ).fetchall()
            for row in rows:
                ref = str(row["object_ref"])
                state = ActivityState(str(row["activity_state"]))
                recent = (
                    current_delta - int(row["last_accessed_delta"])
                    < recent_access_deltas
                )
                if ref in active_refs or bool(row["pinned"]) or recent:
                    state = ActivityState.ACTIVE
                    inactive_since = None
                elif state == ActivityState.ACTIVE:
                    state = ActivityState.INACTIVE_CANDIDATE
                    inactive_since = current_delta
                elif state == ActivityState.INACTIVE_CANDIDATE:
                    prior = row["inactive_since_delta"]
                    inactive_since = int(prior) if prior is not None else current_delta
                    if current_delta - inactive_since >= grace_deltas:
                        state = ActivityState.EVICTABLE
                else:
                    inactive_since = row["inactive_since_delta"]
                conn.execute(
                    "UPDATE object_versions SET activity_state = ?, "
                    "inactive_since_delta = ?, location = ? WHERE object_ref = ?",
                    (
                        state.value,
                        inactive_since,
                        (
                            ObjectLocation.WORKING_MEMORY.value
                            if state == ActivityState.ACTIVE
                            else str(row["location"])
                        ),
                        ref,
                    ),
                )
                counts[state.value] += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return counts

    def archive_evictable(self, conversation_id: str) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT v.object_ref FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE o.conversation_id = ? AND v.activity_state = ? "
                "AND v.pinned = 0",
                (conversation_id, ActivityState.EVICTABLE.value),
            ).fetchall()
            refs = [str(row["object_ref"]) for row in rows]
            if refs:
                placeholders = ",".join("?" for _ in refs)
                conn.execute(
                    f"UPDATE object_versions SET activity_state = ?, location = ? "
                    f"WHERE object_ref IN ({placeholders})",
                    (
                        ActivityState.ARCHIVED.value,
                        ObjectLocation.COLD_ARCHIVE.value,
                        *refs,
                    ),
                )
        return refs

    def record_metric(
        self,
        conversation_id: str,
        name: str,
        value: float,
        *,
        delta_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO metrics("
                "conversation_id, delta_id, name, value, metadata_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    delta_id,
                    name,
                    float(value),
                    canonical_json(metadata or {}),
                    time.time(),
                ),
            )

    def record_metrics(
        self,
        conversation_id: str,
        values: Mapping[str, float],
        *,
        delta_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist one atomic group of related metrics with shared metadata."""

        created_at = time.time()
        encoded_metadata = canonical_json(metadata or {})
        rows = [
            (
                conversation_id,
                delta_id,
                str(name),
                float(value),
                encoded_metadata,
                created_at,
            )
            for name, value in values.items()
        ]
        if not rows:
            return
        with self._connect() as conn:
            conn.executemany(
                "INSERT INTO metrics("
                "conversation_id, delta_id, name, value, metadata_json, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )

    def request_projection_timeline(
        self, conversation_id: str
    ) -> list[dict[str, Any]]:
        """Return ordered request-projection events without prompt/object content.

        New telemetry rows carry a shared ``projection_id`` and real
        ``turn_id`` in ``metadata_json``. Older V1 rows predate that identity;
        they are reconstructed only at the per-projection token level and
        explicitly marked legacy so callers do not invent turn or latency data.
        """

        names = sorted(REQUEST_PROJECTION_METRIC_NAMES)
        placeholders = ",".join("?" for _ in names)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, value, metadata_json, created_at FROM metrics "
                "WHERE conversation_id = ? AND delta_id = '' "
                f"AND name IN ({placeholders}) ORDER BY id",
                (conversation_id, *names),
            ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        ordered_keys: list[str] = []
        legacy_key = ""
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            projection_id = str(metadata.get("projection_id") or "")
            is_legacy = not projection_id
            name = str(row["name"])
            if is_legacy:
                if name == "raw_context_tokens" or not legacy_key:
                    legacy_key = f"legacy:{int(row['id'])}"
                projection_id = legacy_key
            if projection_id not in grouped:
                grouped[projection_id] = {
                    "projection_id": projection_id,
                    "projection_sequence": metadata.get("projection_sequence"),
                    "turn_id": str(metadata.get("turn_id") or ""),
                    "session_id": str(metadata.get("session_id") or ""),
                    "created_at": float(row["created_at"]),
                    "legacy": is_legacy,
                    "metrics": {},
                }
                ordered_keys.append(projection_id)
            event = grouped[projection_id]
            event["created_at"] = min(
                float(event["created_at"]), float(row["created_at"])
            )
            event["metrics"][name] = float(row["value"])

        timeline = [grouped[key] for key in ordered_keys]
        for ordinal, event in enumerate(timeline, start=1):
            try:
                sequence = int(event.get("projection_sequence"))
            except (TypeError, ValueError):
                sequence = ordinal
            event["projection_sequence"] = max(1, sequence)
        return timeline

    def request_projection_conversations(self) -> list[dict[str, Any]]:
        """List conversation roots with request-projection telemetry.

        ``tokens_saved`` is written exactly once for every atomic request
        projection, including legacy V1 rows, so it is the stable event-count
        anchor. Only identifiers, counts, and timestamps leave the store; no
        message, object, Card, or retrieval content is exposed.
        """

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT conversation_id, COUNT(*) AS projection_count, "
                "MIN(created_at) AS first_projection_at, "
                "MAX(created_at) AS last_projection_at "
                "FROM metrics WHERE delta_id = '' AND name = 'tokens_saved' "
                "AND conversation_id != '' GROUP BY conversation_id "
                "ORDER BY last_projection_at DESC, conversation_id ASC"
            ).fetchall()
        return [
            {
                "conversation_id": str(row["conversation_id"]),
                "projection_count": max(0, int(row["projection_count"] or 0)),
                "first_projection_at": float(row["first_projection_at"] or 0.0),
                "last_projection_at": float(row["last_projection_at"] or 0.0),
            }
            for row in rows
        ]

    def cache_usage_timeline(self, conversation_id: str) -> list[dict[str, Any]]:
        """Return ordered, content-free provider cache-usage events.

        Only atomic events carrying the ``provider_cache_usage`` identity are
        eligible. Older V1 builds wrote cache buckets as unrelated metric rows
        and calculated their ratio against uncached input, so those rows cannot
        be reconstructed into an exact request-level hit rate and are
        deliberately excluded.
        """

        names = sorted(CACHE_USAGE_METRIC_NAMES)
        placeholders = ",".join("?" for _ in names)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, value, metadata_json, created_at FROM metrics "
                "WHERE conversation_id = ? AND delta_id = '' "
                f"AND name IN ({placeholders}) ORDER BY id",
                (conversation_id, *names),
            ).fetchall()

        grouped: dict[str, dict[str, Any]] = {}
        ordered_keys: list[str] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(metadata, dict):
                continue
            if metadata.get("event") != "provider_cache_usage":
                continue
            request_id = str(metadata.get("cache_request_id") or "")
            if not request_id:
                continue
            if request_id not in grouped:
                grouped[request_id] = {
                    "cache_request_id": request_id,
                    "request_sequence": metadata.get("cache_request_sequence"),
                    "turn_id": str(metadata.get("turn_id") or ""),
                    "session_id": str(metadata.get("session_id") or ""),
                    "created_at": float(row["created_at"]),
                    "metrics": {},
                }
                ordered_keys.append(request_id)
            event = grouped[request_id]
            event["created_at"] = min(
                float(event["created_at"]), float(row["created_at"])
            )
            event["metrics"][str(row["name"])] = float(row["value"])

        timeline = [grouped[key] for key in ordered_keys]
        # A conversation can span resumed physical sessions whose local
        # counters restart at one. Labels must remain monotonic across the
        # stable conversation root, so persisted row order is authoritative.
        for ordinal, event in enumerate(timeline, start=1):
            event["request_sequence"] = ordinal
        return timeline

    def metrics(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM metrics WHERE conversation_id = ? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def aggregate_status(self, conversation_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            delta_rows = conn.execute(
                "SELECT state, COUNT(*) AS count FROM deltas "
                "WHERE conversation_id = ? GROUP BY state",
                (conversation_id,),
            ).fetchall()
            object_rows = conn.execute(
                "SELECT v.activity_state, v.location, COUNT(*) AS count "
                "FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE o.conversation_id = ? "
                "GROUP BY v.activity_state, v.location",
                (conversation_id,),
            ).fetchall()
            working_memory_bytes = conn.execute(
                "SELECT COALESCE(SUM(scoped.byte_size), 0) AS bytes FROM ("
                "SELECT DISTINCT b.sha256, b.byte_size FROM blobs AS b "
                "JOIN object_versions AS v ON v.sha256 = b.sha256 "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE o.conversation_id = ? AND v.location = ?"
                ") AS scoped",
                (conversation_id, ObjectLocation.WORKING_MEMORY.value),
            ).fetchone()
            retrieval = conn.execute(
                "SELECT COUNT(*) AS count FROM retrieval_events "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
            never_retrieved = conn.execute(
                "SELECT COUNT(*) AS count FROM object_versions AS v "
                "JOIN logical_objects AS o ON o.object_id = v.object_id "
                "WHERE o.conversation_id = ? AND NOT EXISTS ("
                "SELECT 1 FROM retrieval_events AS r "
                "WHERE r.conversation_id = o.conversation_id "
                "AND r.object_ref = v.object_ref AND r.status = 'success')",
                (conversation_id,),
            ).fetchone()
            metric_rows = conn.execute(
                "SELECT name, SUM(value) AS total, AVG(value) AS average "
                "FROM metrics WHERE conversation_id = ? GROUP BY name",
                (conversation_id,),
            ).fetchall()
            # Request projection metrics use the existing empty-delta marker;
            # per-Delta Card-construction metrics carry their source delta_id.
            # Keep them separate here so an operator-facing savings total does
            # not double count both the one-time Card saving and every request
            # that subsequently benefits from that Card.
            request_metric_names = sorted(REQUEST_PROJECTION_METRIC_NAMES)
            request_metric_placeholders = ",".join(
                "?" for _ in request_metric_names
            )
            request_metric_rows = conn.execute(
                "SELECT id, name, value FROM metrics "
                "WHERE conversation_id = ? AND delta_id = '' "
                f"AND name IN ({request_metric_placeholders}) ORDER BY id",
                (conversation_id, *request_metric_names),
            ).fetchall()
        metric_totals = {
            str(row["name"]): float(row["total"] or 0) for row in metric_rows
        }
        metric_averages = {
            str(row["name"]): float(row["average"] or 0) for row in metric_rows
        }
        request_metric_totals: dict[str, float] = {}
        request_metric_counts: dict[str, int] = {}
        last_request_metrics: dict[str, float] = {}
        for row in request_metric_rows:
            name = str(row["name"])
            value = float(row["value"] or 0)
            request_metric_totals[name] = request_metric_totals.get(name, 0.0) + value
            request_metric_counts[name] = request_metric_counts.get(name, 0) + 1
            last_request_metrics[name] = value
        request_metric_averages = {
            name: total / request_metric_counts[name]
            for name, total in request_metric_totals.items()
            if request_metric_counts.get(name, 0) > 0
        }
        retrieved_tokens = metric_totals.get("retrieved_tokens", 0.0)
        tokens_saved = metric_totals.get("tokens_saved", 0.0)
        return {
            "delta_states": {
                str(row["state"]): int(row["count"]) for row in delta_rows
            },
            "object_states": {
                state.value: sum(
                    int(row["count"])
                    for row in object_rows
                    if str(row["activity_state"]) == state.value
                )
                for state in ActivityState
            },
            "working_memory_object_count": sum(
                int(row["count"])
                for row in object_rows
                if str(row["location"]) == ObjectLocation.WORKING_MEMORY.value
            ),
            "working_memory_bytes": int(
                working_memory_bytes["bytes"] if working_memory_bytes else 0
            ),
            "retrieval_count": int(retrieval["count"] if retrieval else 0),
            "objects_never_retrieved": int(
                never_retrieved["count"] if never_retrieved else 0
            ),
            "retrieval_overhead": (
                retrieved_tokens / tokens_saved if tokens_saved > 0 else 0.0
            ),
            "metric_totals": metric_totals,
            "metric_averages": metric_averages,
            "request_projection_count": request_metric_counts.get("tokens_saved", 0),
            "request_metric_totals": request_metric_totals,
            "request_metric_averages": request_metric_averages,
            "last_request_metrics": last_request_metrics,
        }
