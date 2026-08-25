"""Profile-scoped exact Working Memory and Object Registry for V1."""

from __future__ import annotations

import hashlib
import json
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
    RetrievalLease,
)


SCHEMA_VERSION = 1
REQUEST_PROJECTION_METRIC_NAMES = frozenset(
    {
        "raw_context_tokens",
        "rendered_context_tokens",
        "tokens_saved",
        "compression_ratio",
        "hot_tail_tokens",
        "projection_latency_ms",
    }
)
OBJECT_REF_RE = re.compile(
    r"^object://(?P<object_id>obj_[a-f0-9]{24})@v(?P<version>[1-9][0-9]*)$"
)


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

                CREATE INDEX IF NOT EXISTS idx_delta_conversation_sequence
                    ON deltas(conversation_id, global_sequence);
                CREATE INDEX IF NOT EXISTS idx_occurrence_message
                    ON object_occurrences(conversation_id, message_key);
                CREATE INDEX IF NOT EXISTS idx_version_activity
                    ON object_versions(activity_state, location);
                CREATE INDEX IF NOT EXISTS idx_retrieval_turn
                    ON retrieval_events(conversation_id, turn_id);
                """
            )
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported Object Context V1 schema {row['value']} "
                    f"(expected {SCHEMA_VERSION})"
                )
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

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
        )

    def get_delta(self, delta_id: str) -> DeltaRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM deltas WHERE delta_id = ?", (delta_id,)
            ).fetchone()
        return self._delta_from_row(row) if row is not None else None

    def list_deltas(self, conversation_id: str) -> list[DeltaRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM deltas WHERE conversation_id = ? "
                "ORDER BY global_sequence",
                (conversation_id,),
            ).fetchall()
        return [self._delta_from_row(row) for row in rows]

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
                "SELECT object_ref FROM object_occurrences WHERE occurrence_key = ?",
                (detected.occurrence_key,),
            ).fetchone()
            if existing_occurrence is not None:
                existing_ref = str(existing_occurrence["object_ref"])
                conn.commit()
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
                refs = list(delta.object_refs)
                if object_ref not in refs:
                    refs.append(object_ref)
                    conn.execute(
                        "UPDATE deltas SET object_refs_json = ? WHERE delta_id = ?",
                        (canonical_json(refs), delta.delta_id),
                    )
                existing_ref = object_ref
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
    ) -> None:
        """Atomically publish several newly-cold Deltas as one prefix change."""

        if not batch:
            return
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for delta_id, cards, compressed_view in batch:
                row = conn.execute(
                    "SELECT state FROM deltas WHERE delta_id = ?", (delta_id,)
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"delta not found: {delta_id}")
                if str(row["state"]) not in {
                    DeltaState.COMPRESSION_ELIGIBLE.value,
                    DeltaState.COMPRESSING.value,
                    DeltaState.COMPRESSION_FAILED.value,
                }:
                    raise RuntimeError(
                        f"delta is not compressible: {delta_id} ({row['state']})"
                    )
                for object_ref, summary, card_text, contains in cards:
                    target = conn.execute(
                        "SELECT 1 FROM object_versions WHERE object_ref = ?",
                        (object_ref,),
                    ).fetchone()
                    if target is None:
                        raise RuntimeError(f"Card target missing: {object_ref}")
                    conn.execute(
                        "UPDATE object_versions SET summary = ?, "
                        "contains_json = ?, card_text = ? WHERE object_ref = ?",
                        (
                            summary,
                            canonical_json(contains),
                            card_text,
                            object_ref,
                        ),
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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_delta_failed(self, delta_id: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE deltas SET state = ?, failure_error = ? WHERE delta_id = ?",
                (
                    DeltaState.COMPRESSION_FAILED.value,
                    str(error)[:1000],
                    delta_id,
                ),
            )

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
            request_metric_rows = conn.execute(
                "SELECT id, name, value FROM metrics "
                "WHERE conversation_id = ? AND delta_id = '' AND name IN ("
                "'raw_context_tokens', 'rendered_context_tokens', "
                "'tokens_saved', 'compression_ratio', 'hot_tail_tokens', "
                "'projection_latency_ms'"
                ") ORDER BY id",
                (conversation_id,),
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
            "request_projection_count": request_metric_counts.get(
                "tokens_saved", 0
            ),
            "request_metric_totals": request_metric_totals,
            "request_metric_averages": request_metric_averages,
            "last_request_metrics": last_request_metrics,
        }
