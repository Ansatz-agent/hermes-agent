"""SQLite persistence for the profile-only memory provider.

The store intentionally models only user-profile evidence and atomic profile
items.  It is not a generic event, resource, or conversation database; Hermes'
existing SessionDB remains the source of truth for full transcripts.
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import struct
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


PROFILE_KINDS = frozenset({"identity", "interaction_preference", "workflow_preference"})
SCOPE_ROOTS = {
    "identity": "identity",
    "interaction_preference": "preferences/interaction",
    "workflow_preference": "preferences/workflow",
}
_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_scope_nodes (
    scope_id TEXT PRIMARY KEY,
    parent_id TEXT,
    segment TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    depth INTEGER NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(parent_id) REFERENCES profile_scope_nodes(scope_id)
);
CREATE INDEX IF NOT EXISTS profile_scope_nodes_parent
    ON profile_scope_nodes(parent_id, segment);

CREATE TABLE IF NOT EXISTS profile_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_index INTEGER NOT NULL DEFAULT -1,
    user_text TEXT NOT NULL,
    source_type TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(session_id, turn_index, text_hash, source_type)
);

CREATE TABLE IF NOT EXISTS profile_items (
    item_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    scope_id TEXT,
    applies_when TEXT NOT NULL,
    rule TEXT NOT NULL,
    status TEXT NOT NULL,
    confidence REAL NOT NULL,
    explicit INTEGER NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL,
    supersedes_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS profile_items_live_hash
    ON profile_items(content_hash)
    WHERE status IN ('active', 'candidate');
CREATE INDEX IF NOT EXISTS profile_items_status_kind
    ON profile_items(status, kind);

CREATE TABLE IF NOT EXISTS profile_item_evidence (
    item_id TEXT NOT NULL,
    evidence_id INTEGER NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supports',
    PRIMARY KEY(item_id, evidence_id, relation),
    FOREIGN KEY(item_id) REFERENCES profile_items(item_id) ON DELETE CASCADE,
    FOREIGN KEY(evidence_id) REFERENCES profile_evidence(evidence_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_embeddings (
    item_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(item_id) REFERENCES profile_items(item_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_scope_embeddings (
    scope_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(scope_id) REFERENCES profile_scope_nodes(scope_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS profile_extraction_runs (
    session_id TEXT NOT NULL,
    transcript_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    PRIMARY KEY(session_id, transcript_hash)
);
"""


def _clean_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit]


def _clean_evidence(value: Any, *, limit: int) -> str:
    """Bound evidence without rewriting the user's original whitespace."""
    return str(value or "").strip()[:limit]


def normalize_scope_path(raw: Any, *, max_segments: int = 8) -> tuple[str, str]:
    """Normalize an open-vocabulary relative category path without fixing topics."""
    value = unicodedata.normalize("NFKC", str(raw or "")).strip()
    if not value:
        return "", "scope is required"
    if value.startswith("/") or "://" in value or "%" in value or "\\" in value:
        return "", "scope must be a safe relative path"
    segments = value.split("/")
    if len(segments) > max_segments or any(not segment.strip() for segment in segments):
        return "", f"scope must contain 1-{max_segments} non-empty segments"
    normalized: List[str] = []
    for segment in segments:
        clean = "-".join(segment.strip().split()).casefold()
        if clean in {".", ".."} or len(clean) > 64:
            return "", "scope contains an invalid segment"
        if not all(char.isalnum() or char in {"-", "_", "."} for char in clean):
            return "", "scope contains unsupported characters"
        normalized.append(clean)
    result = "/".join(normalized)
    if len(result) > 320:
        return "", "scope is too long"
    return result, ""


def canonical_scope_path(kind: str, raw_scope: Any) -> str:
    """Place a relative topic below the stable root for its profile kind."""
    clean_kind = _clean_text(kind, limit=80)
    root = SCOPE_ROOTS.get(clean_kind)
    if root is None:
        raise ValueError(f"unsupported profile kind: {clean_kind!r}")
    normalized, error = normalize_scope_path(raw_scope, max_segments=10)
    if error:
        raise ValueError(error)

    root_parts = root.split("/")
    parts = normalized.split("/")
    if parts[: len(root_parts)] == root_parts:
        tail = parts[len(root_parts) :]
    else:
        # Accept old labels, user-facing short paths, and fully qualified paths.
        # The item kind is authoritative if a caller supplied a conflicting root.
        if parts and parts[0] in {"profile", "user-profile"}:
            parts = parts[1:]
        if clean_kind == "identity" and parts[:1] == ["identity"]:
            parts = parts[1:]
        elif clean_kind != "identity":
            if parts[:1] == ["preferences"]:
                parts = parts[1:]
            if parts[:1] and parts[0] in {"interaction", "workflow"}:
                parts = parts[1:]
        tail = parts

    if len(tail) > 8:
        raise ValueError("scope must contain at most 8 topic segments")
    result = "/".join([*root_parts, *tail])
    if len(result) > 320:
        raise ValueError("scope is too long after adding its category root")
    return result


def _scope_id(path: str) -> str:
    return hashlib.sha256(f"profile-scope\n{path}".encode("utf-8")).hexdigest()[:32]


def _content_hash(kind: str, scope: str, applies_when: str, rule: str) -> str:
    normalized = "\n".join(
        part.casefold() for part in (kind, scope, applies_when, rule)
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def profile_item_text(item: Dict[str, Any]) -> str:
    """Return the exact text embedded for one profile item."""
    # Natural language only: repeated English metadata labels made unrelated
    # workflow items artificially similar in the small Chinese BGE space.
    return f"{item.get('applies_when', '')}\n{item.get('rule', '')}"


def pack_vector(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    if not values:
        return b""
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(payload: bytes, dimension: int) -> List[float]:
    if not payload or dimension <= 0:
        return []
    expected = dimension * 4
    if len(payload) != expected:
        raise ValueError(
            f"Invalid profile embedding payload: expected {expected} bytes, got {len(payload)}"
        )
    return list(struct.unpack(f"<{dimension}f", payload))


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


class ProfileStore:
    """Thread-safe profile-scoped SQLite store."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            timeout=10.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            try:
                from hermes_state import apply_wal_with_fallback

                apply_wal_with_fallback(self._conn, db_label="profile_memory.sqlite3")
            except Exception:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
            self._migrate_scope_tree()
            self._secure_database_files()

    @staticmethod
    def _ensure_scope_path(conn: sqlite3.Connection, path: str) -> Dict[str, Any]:
        parent_id: Optional[str] = None
        current: List[str] = []
        now = time.time()
        for depth, segment in enumerate(path.split("/"), start=1):
            current.append(segment)
            current_path = "/".join(current)
            current_id = _scope_id(current_path)
            conn.execute(
                """
                INSERT OR IGNORE INTO profile_scope_nodes
                    (scope_id, parent_id, segment, path, depth, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_id,
                    parent_id,
                    segment,
                    current_path,
                    depth,
                    now,
                    now,
                ),
            )
            parent_id = current_id
        row = conn.execute(
            "SELECT * FROM profile_scope_nodes WHERE path=?", (path,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"failed to persist profile category: {path}")
        return dict(row)

    def _migrate_scope_tree(self) -> None:
        """Add normalized category nodes and attach records from flat-scope stores."""
        conn = self._require_connection()
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(profile_items)").fetchall()
        }
        if "scope_id" not in columns:
            conn.execute("ALTER TABLE profile_items ADD COLUMN scope_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS profile_items_scope_id "
            "ON profile_items(scope_id, status)"
        )

        for root in sorted(set(SCOPE_ROOTS.values())):
            self._ensure_scope_path(conn, root)

        rows = conn.execute(
            "SELECT item_id, kind, scope, applies_when, rule, status, confidence, "
            "explicit, content_hash "
            "FROM profile_items"
        ).fetchall()
        for row in rows:
            try:
                canonical = canonical_scope_path(row["kind"], row["scope"])
            except ValueError:
                # Preserve an invalid historical record rather than making the
                # whole provider unavailable. New writes still reject it.
                continue
            node = self._ensure_scope_path(conn, canonical)
            digest = _content_hash(
                str(row["kind"]),
                canonical,
                str(row["applies_when"]),
                str(row["rule"]),
            )
            duplicate = None
            if row["status"] in {"active", "candidate"}:
                duplicate = conn.execute(
                    """
                    SELECT item_id, status FROM profile_items
                    WHERE content_hash=? AND item_id<>?
                      AND status IN ('active', 'candidate')
                    """,
                    (digest, row["item_id"]),
                ).fetchone()
            if duplicate is not None:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO profile_item_evidence
                        (item_id, evidence_id, relation)
                    SELECT ?, evidence_id, relation
                    FROM profile_item_evidence WHERE item_id=?
                    """,
                    (duplicate["item_id"], row["item_id"]),
                )
                conn.execute(
                    """
                    UPDATE profile_items
                    SET confidence=MAX(confidence, ?),
                        explicit=MAX(explicit, ?),
                        status=CASE WHEN status='active' OR ?='active'
                                    THEN 'active' ELSE status END,
                        updated_at=?
                    WHERE item_id=?
                    """,
                    (
                        row["confidence"],
                        row["explicit"],
                        row["status"],
                        time.time(),
                        duplicate["item_id"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE profile_items
                    SET scope=?, scope_id=?, status='superseded', updated_at=?
                    WHERE item_id=?
                    """,
                    (canonical, node["scope_id"], time.time(), row["item_id"]),
                )
                continue
            try:
                conn.execute(
                    """
                    UPDATE profile_items
                    SET scope=?, scope_id=?, content_hash=?
                    WHERE item_id=?
                    """,
                    (canonical, node["scope_id"], digest, row["item_id"]),
                )
            except sqlite3.IntegrityError:
                # A legacy store may already contain two equivalent live rows.
                # Keep both records auditable and attach the directory without
                # violating the pre-existing live-content uniqueness index.
                conn.execute(
                    "UPDATE profile_items SET scope=?, scope_id=? WHERE item_id=?",
                    (canonical, node["scope_id"], row["item_id"]),
                )
        conn.execute(
            """
            UPDATE profile_items
            SET evidence_count=(
                SELECT COUNT(DISTINCT evidence_id)
                FROM profile_item_evidence
                WHERE profile_item_evidence.item_id=profile_items.item_id
                  AND relation='supports'
            )
            """
        )

    def ensure_scope(self, *, kind: str, scope: str) -> Dict[str, Any]:
        canonical = canonical_scope_path(kind, scope)
        with self._lock:
            return self._ensure_scope_path(self._require_connection(), canonical)

    def _secure_database_files(self) -> None:
        for path in (
            self.path,
            Path(f"{self.path}-wal"),
            Path(f"{self.path}-shm"),
        ):
            try:
                if path.exists():
                    path.chmod(0o600)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("ProfileStore is closed")
        return self._conn

    def add_evidence(
        self,
        *,
        session_id: str,
        turn_index: int,
        user_text: str,
        source_type: str,
    ) -> int:
        text = _clean_evidence(user_text, limit=4000)
        if not text:
            raise ValueError("profile evidence must not be empty")
        sid = _clean_text(session_id or "unknown", limit=200)
        source = _clean_text(source_type or "unknown", limit=80)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = time.time()
        with self._lock:
            conn = self._require_connection()
            conn.execute(
                """
                INSERT OR IGNORE INTO profile_evidence
                    (session_id, turn_index, user_text, source_type, text_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, int(turn_index), text, source, digest, now),
            )
            row = conn.execute(
                """
                SELECT evidence_id FROM profile_evidence
                WHERE session_id=? AND turn_index=? AND text_hash=? AND source_type=?
                """,
                (sid, int(turn_index), digest, source),
            ).fetchone()
            if row is None:
                raise RuntimeError("failed to persist profile evidence")
            self._secure_database_files()
            return int(row["evidence_id"])

    @staticmethod
    def _validate_item(
        *, kind: str, scope: str, applies_when: str, rule: str, confidence: float
    ) -> tuple[str, str, str, str, float]:
        clean_kind = _clean_text(kind, limit=80)
        if clean_kind not in PROFILE_KINDS:
            raise ValueError(f"unsupported profile kind: {clean_kind!r}")
        clean_scope = canonical_scope_path(clean_kind, scope)
        clean_applies = _clean_text(applies_when, limit=800)
        clean_rule = _clean_text(rule, limit=2000)
        if not clean_scope or not clean_applies or not clean_rule:
            raise ValueError("scope, applies_when, and rule are required")
        clean_confidence = max(0.0, min(1.0, float(confidence)))
        return clean_kind, clean_scope, clean_applies, clean_rule, clean_confidence

    def upsert_item(
        self,
        *,
        kind: str,
        scope: str,
        applies_when: str,
        rule: str,
        confidence: float,
        explicit: bool,
        evidence_ids: Iterable[int],
        activation_sessions: int = 2,
        supersedes_id: str = "",
    ) -> Dict[str, Any]:
        kind, scope, applies_when, rule, confidence = self._validate_item(
            kind=kind,
            scope=scope,
            applies_when=applies_when,
            rule=rule,
            confidence=confidence,
        )
        digest = _content_hash(kind, scope, applies_when, rule)
        evidence = []
        for value in evidence_ids:
            try:
                evidence_id = int(value)
            except (TypeError, ValueError):
                continue
            if evidence_id > 0:
                evidence.append(evidence_id)
        evidence = sorted(set(evidence))
        now = time.time()
        activation_sessions = max(1, int(activation_sessions))

        with self._lock:
            conn = self._require_connection()
            conn.execute("BEGIN IMMEDIATE")
            try:
                scope_node = self._ensure_scope_path(conn, scope)
                existing = conn.execute(
                    """
                    SELECT * FROM profile_items
                    WHERE status IN ('active', 'candidate')
                      AND (
                        content_hash=? OR
                        (kind=? AND scope=? AND applies_when=? AND rule=?)
                      )
                    """,
                    (digest, kind, scope, applies_when, rule),
                ).fetchone()
                if existing is None:
                    item_id = uuid.uuid4().hex
                    status = "active" if explicit else "candidate"
                    conn.execute(
                        """
                        INSERT INTO profile_items
                            (item_id, kind, scope, scope_id, applies_when, rule, status,
                             confidence, explicit, evidence_count, content_hash,
                             supersedes_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
                        """,
                        (
                            item_id,
                            kind,
                            scope,
                            scope_node["scope_id"],
                            applies_when,
                            rule,
                            status,
                            confidence,
                            1 if explicit else 0,
                            digest,
                            supersedes_id or None,
                            now,
                            now,
                        ),
                    )
                    created = True
                else:
                    item_id = str(existing["item_id"])
                    created = False
                    conn.execute(
                        """
                        UPDATE profile_items
                        SET confidence=MAX(confidence, ?),
                            explicit=MAX(explicit, ?), scope=?, scope_id=?,
                            content_hash=?, updated_at=?
                        WHERE item_id=?
                        """,
                        (
                            confidence,
                            1 if explicit else 0,
                            scope,
                            scope_node["scope_id"],
                            digest,
                            now,
                            item_id,
                        ),
                    )

                for evidence_id in evidence:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO profile_item_evidence
                            (item_id, evidence_id, relation)
                        VALUES (?, ?, 'supports')
                        """,
                        (item_id, evidence_id),
                    )

                counts = conn.execute(
                    """
                    SELECT COUNT(DISTINCT pie.evidence_id) AS evidence_count,
                           COUNT(DISTINCT pe.session_id) AS session_count
                    FROM profile_item_evidence pie
                    JOIN profile_evidence pe ON pe.evidence_id=pie.evidence_id
                    WHERE pie.item_id=? AND pie.relation='supports'
                    """,
                    (item_id,),
                ).fetchone()
                evidence_count = int(counts["evidence_count"] or 0)
                session_count = int(counts["session_count"] or 0)
                row = conn.execute(
                    "SELECT explicit, status FROM profile_items WHERE item_id=?",
                    (item_id,),
                ).fetchone()
                should_activate = bool(row["explicit"]) or (
                    session_count >= activation_sessions and confidence >= 0.6
                )
                next_status = "active" if should_activate else str(row["status"])
                conn.execute(
                    """
                    UPDATE profile_items
                    SET evidence_count=?, status=?, updated_at=?
                    WHERE item_id=?
                    """,
                    (evidence_count, next_status, now, item_id),
                )

                if supersedes_id and supersedes_id != item_id:
                    target = conn.execute(
                        "SELECT item_id FROM profile_items WHERE item_id=?",
                        (supersedes_id,),
                    ).fetchone()
                    if target is None:
                        raise ValueError(
                            f"profile item to supersede does not exist: {supersedes_id}"
                        )
                    conn.execute(
                        """
                        UPDATE profile_items SET status='superseded', updated_at=?
                        WHERE item_id=?
                        """,
                        (now, supersedes_id),
                    )

                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

            result = self.get_item(item_id)
            result["created"] = created
            return result

    def get_item(self, item_id: str) -> Dict[str, Any]:
        with self._lock:
            conn = self._require_connection()
            row = conn.execute(
                "SELECT * FROM profile_items WHERE item_id=?", (item_id,)
            ).fetchone()
            return dict(row) if row is not None else {}

    def list_items(
        self,
        *,
        statuses: Sequence[str] = ("active",),
        limit: int = 500,
        kind: str = "",
        scope_prefix: str = "",
    ) -> List[Dict[str, Any]]:
        clean_statuses = [str(value) for value in statuses if value]
        if not clean_statuses:
            return []
        placeholders = ",".join("?" for _ in clean_statuses)
        clauses = [f"status IN ({placeholders})"]
        params: List[Any] = list(clean_statuses)
        clean_kind = _clean_text(kind, limit=80)
        if clean_kind:
            if clean_kind not in PROFILE_KINDS:
                return []
            clauses.append("kind=?")
            params.append(clean_kind)
        clean_prefix = str(scope_prefix or "").strip().strip("/")
        if clean_prefix:
            clauses.append("(scope=? OR scope LIKE ?)")
            params.extend([clean_prefix, f"{clean_prefix}/%"])
        with self._lock:
            conn = self._require_connection()
            rows = conn.execute(
                f"""
                SELECT * FROM profile_items
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, item_id
                LIMIT ?
                """,
                [*params, max(1, int(limit))],
            ).fetchall()
            return [dict(row) for row in rows]

    def list_scope_nodes(self) -> List[Dict[str, Any]]:
        """Return the normalized directory tree with direct and subtree counts."""
        with self._lock:
            conn = self._require_connection()
            rows = conn.execute(
                """
                SELECT n.*,
                       SUM(CASE WHEN i.status='active' THEN 1 ELSE 0 END) AS active_count,
                       SUM(CASE WHEN i.status='candidate' THEN 1 ELSE 0 END) AS candidate_count
                FROM profile_scope_nodes n
                LEFT JOIN profile_items i ON i.scope_id=n.scope_id
                GROUP BY n.scope_id
                ORDER BY n.path
                """
            ).fetchall()
        nodes = [dict(row) for row in rows]
        by_path = {str(node["path"]): node for node in nodes}
        for node in nodes:
            node["active_count"] = int(node.get("active_count") or 0)
            node["candidate_count"] = int(node.get("candidate_count") or 0)
            node["subtree_active_count"] = 0
            node["subtree_candidate_count"] = 0
        for node in nodes:
            parts = str(node["path"]).split("/")
            for depth in range(1, len(parts) + 1):
                ancestor = by_path.get("/".join(parts[:depth]))
                if ancestor is None:
                    continue
                ancestor["subtree_active_count"] += int(node["active_count"])
                ancestor["subtree_candidate_count"] += int(node["candidate_count"])
        return nodes

    def get_scope_node(self, scope_id: str) -> Dict[str, Any]:
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    "SELECT * FROM profile_scope_nodes WHERE scope_id=?", (scope_id,)
                )
                .fetchone()
            )
            return dict(row) if row is not None else {}

    def scope_text(self, scope_id: str) -> str:
        """Build the semantic representation for one concrete category."""
        with self._lock:
            conn = self._require_connection()
            node = conn.execute(
                "SELECT path FROM profile_scope_nodes WHERE scope_id=?", (scope_id,)
            ).fetchone()
            if node is None:
                return ""
            rows = conn.execute(
                """
                SELECT applies_when, rule FROM profile_items
                WHERE scope_id=? AND status IN ('active', 'candidate')
                ORDER BY updated_at DESC, item_id
                LIMIT 20
                """,
                (scope_id,),
            ).fetchall()
        path_text = str(node["path"]).replace("/", " ").replace("-", " ")
        details = [f"{row['applies_when']}\n{row['rule']}" for row in rows]
        return "\n".join([path_text, *details])

    def set_status(self, item_id: str, status: str) -> bool:
        if status not in {"active", "candidate", "superseded", "revoked"}:
            raise ValueError(f"unsupported profile status: {status}")
        with self._lock:
            conn = self._require_connection()
            result = conn.execute(
                "UPDATE profile_items SET status=?, updated_at=? WHERE item_id=?",
                (status, time.time(), item_id),
            )
            return bool(result.rowcount)

    def save_embedding(
        self,
        *,
        item_id: str,
        model_id: str,
        vector: Sequence[float],
        content_hash: str,
    ) -> None:
        packed = pack_vector(vector)
        if not packed:
            raise ValueError("profile embedding must not be empty")
        with self._lock:
            conn = self._require_connection()
            conn.execute(
                """
                INSERT INTO profile_embeddings
                    (item_id, model_id, dimension, vector, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(item_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    dimension=excluded.dimension,
                    vector=excluded.vector,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    item_id,
                    model_id,
                    len(vector),
                    packed,
                    content_hash,
                    time.time(),
                ),
            )

    def embedding_for_item(
        self, item_id: str, *, model_id: str, content_hash: str
    ) -> List[float]:
        with self._lock:
            conn = self._require_connection()
            row = conn.execute(
                """
                SELECT dimension, vector FROM profile_embeddings
                WHERE item_id=? AND model_id=? AND content_hash=?
                """,
                (item_id, model_id, content_hash),
            ).fetchone()
            if row is None:
                return []
            return unpack_vector(row["vector"], int(row["dimension"]))

    def save_scope_embedding(
        self,
        *,
        scope_id: str,
        model_id: str,
        vector: Sequence[float],
        content_hash: str,
    ) -> None:
        packed = pack_vector(vector)
        if not packed:
            raise ValueError("profile category embedding must not be empty")
        with self._lock:
            self._require_connection().execute(
                """
                INSERT INTO profile_scope_embeddings
                    (scope_id, model_id, dimension, vector, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_id) DO UPDATE SET
                    model_id=excluded.model_id,
                    dimension=excluded.dimension,
                    vector=excluded.vector,
                    content_hash=excluded.content_hash,
                    updated_at=excluded.updated_at
                """,
                (
                    scope_id,
                    model_id,
                    len(vector),
                    packed,
                    content_hash,
                    time.time(),
                ),
            )

    def embedding_for_scope(
        self, scope_id: str, *, model_id: str, content_hash: str
    ) -> List[float]:
        with self._lock:
            row = (
                self._require_connection()
                .execute(
                    """
                SELECT dimension, vector FROM profile_scope_embeddings
                WHERE scope_id=? AND model_id=? AND content_hash=?
                """,
                    (scope_id, model_id, content_hash),
                )
                .fetchone()
            )
            if row is None:
                return []
            return unpack_vector(row["vector"], int(row["dimension"]))

    def extraction_recorded(self, *, session_id: str, transcript_hash: str) -> bool:
        with self._lock:
            conn = self._require_connection()
            row = conn.execute(
                """
                SELECT status FROM profile_extraction_runs
                WHERE session_id=? AND transcript_hash=?
                """,
                (session_id, transcript_hash),
            ).fetchone()
            return row is not None and row["status"] == "completed"

    def record_extraction(
        self,
        *,
        session_id: str,
        transcript_hash: str,
        status: str,
        provider: str = "",
        model: str = "",
        error: str = "",
    ) -> None:
        with self._lock:
            conn = self._require_connection()
            conn.execute(
                """
                INSERT INTO profile_extraction_runs
                    (session_id, transcript_hash, status, provider, model, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, transcript_hash) DO UPDATE SET
                    status=excluded.status,
                    provider=excluded.provider,
                    model=excluded.model,
                    error=excluded.error,
                    created_at=excluded.created_at
                """,
                (
                    session_id,
                    transcript_hash,
                    _clean_text(status, limit=40),
                    _clean_text(provider, limit=120),
                    _clean_text(model, limit=240),
                    _clean_text(error, limit=1000),
                    time.time(),
                ),
            )

    def render_snapshot(self, target: str | Path) -> None:
        """Write an audit-only Markdown view; recall never reads this file."""
        path = Path(target).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        items = self.list_items(statuses=("active", "candidate"), limit=5000)
        lines = [
            "# User profile memory",
            "",
            "> Audit snapshot generated from profile_memory.sqlite3. Recall uses",
            "> atomic database records, not this aggregate Markdown file.",
            "",
            "## Category tree",
            "",
        ]
        nodes = self.list_scope_nodes()
        for node in nodes:
            total = int(node["subtree_active_count"]) + int(
                node["subtree_candidate_count"]
            )
            if not total:
                continue
            indent = "  " * (int(node["depth"]) - 1)
            lines.append(
                f"{indent}- `{node['segment']}/` ({total} live/candidate item"
                f"{'s' if total != 1 else ''})"
            )
        lines.extend(["", "## Category contents", ""])
        for node in nodes:
            selected = [
                item for item in items if item.get("scope_id") == node["scope_id"]
            ]
            if not selected:
                continue
            lines.extend([f"### `profile://{node['path']}`", ""])
            for item in selected:
                lines.extend(
                    [
                        f"#### {item['item_id']}",
                        "",
                        f"- Kind: `{item['kind']}`",
                        f"- Status: `{item['status']}`",
                        f"- Applies when: {item['applies_when']}",
                        f"- Confidence: {float(item['confidence']):.2f}",
                        f"- Evidence count: {int(item['evidence_count'])}",
                        "",
                        str(item["rule"]),
                        "",
                    ]
                )
        payload = "\n".join(lines).rstrip() + "\n"
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.chmod(0o600)
        os.replace(temp, path)

    def stats(self) -> Dict[str, int]:
        with self._lock:
            conn = self._require_connection()
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM profile_items GROUP BY status"
            ).fetchall()
            result = {"active": 0, "candidate": 0, "superseded": 0, "revoked": 0}
            for row in rows:
                result[str(row["status"])] = int(row["count"])
            result["evidence"] = int(
                conn.execute("SELECT COUNT(*) FROM profile_evidence").fetchone()[0]
            )
            return result
