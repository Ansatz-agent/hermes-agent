"""Profile-only local memory provider for Hermes/Ansatz.

The provider stores user evidence and atomic profile/work-preference records in
SQLite, uses the active Ansatz model through ``ctx.llm`` for bounded structured
extraction at session boundaries, and performs small in-process semantic recall.
It deliberately does not implement resources, skills, generic events, or a
general-purpose filesystem knowledge base. Profile categories form a normalized
virtual directory tree inside the provider database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.memory_provider import MemoryProvider, RecallStatus, is_trivial_prompt
from agent.message_content import flatten_message_text
from agent.skill_commands import extract_user_instruction_from_skill_message
from tools.registry import tool_error

from .embedder import DEFAULT_DIMENSION, DEFAULT_MODEL_ID, LocalBgeEmbedder
from .store import (
    PROFILE_KINDS,
    ProfileStore,
    cosine_similarity,
    normalize_scope_path,
    profile_item_text,
)

logger = logging.getLogger(__name__)


_PLUGIN_ID = "profile_memory"
_DEFAULT_RECALL_LIMIT = 3
_DEFAULT_SCORE_THRESHOLD = 0.35
_DEFAULT_LEXICAL_THRESHOLD = 0.05
_DEFAULT_SEMANTIC_ONLY_THRESHOLD = 0.50
_DEFAULT_SEMANTIC_SCORE_WINDOW = 0.08
_DEFAULT_SCOPE_SCORE_THRESHOLD = 0.42
_DEFAULT_SCOPE_SCORE_WINDOW = 0.10
_DEFAULT_SCOPE_ROUTE_LIMIT = 5
_DEFAULT_INTENT_GATE_MIN_SKIP_CONFIDENCE = 0.85
_DEFAULT_INTENT_GATE_TIMEOUT = 30.0
_DEFAULT_EXTRACTION_TIMEOUT = 90.0
_DEFAULT_EXTRACTION_MAX_CHARS = 16000
_DEFAULT_INFERRED_ACTIVATION_SESSIONS = 2


REMEMBER_SCHEMA = {
    "name": "profile_remember",
    "description": (
        "Store one explicit durable user-profile fact or work preference. "
        "Use one atomic rule per call; never store one-off task instructions, "
        "assistant guesses, generic events, or resource content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": sorted(PROFILE_KINDS),
                "description": "Profile item type.",
            },
            "scope": {
                "type": "string",
                "description": (
                    "Open-vocabulary topic path below the kind's stable category "
                    "root, such as artifacts/latex/equations or "
                    "communication/answer-structure. Reuse an existing category "
                    "path when it represents the same topic."
                ),
            },
            "applies_when": {
                "type": "string",
                "description": "Natural-language condition for applying this item.",
            },
            "rule": {
                "type": "string",
                "description": "One atomic durable user fact or behavioral rule.",
            },
            "evidence": {
                "type": "string",
                "description": "The user's own statement supporting this item.",
            },
            "supersedes_id": {
                "type": "string",
                "description": "Existing profile item id corrected by this new item.",
            },
        },
        "required": ["kind", "scope", "applies_when", "rule", "evidence"],
        "additionalProperties": False,
    },
}


FORGET_SCHEMA = {
    "name": "profile_forget",
    "description": "Revoke one exact user-profile item by id when the user asks to forget it.",
    "parameters": {
        "type": "object",
        "properties": {
            "item_id": {"type": "string", "description": "Exact profile item id."},
        },
        "required": ["item_id"],
        "additionalProperties": False,
    },
}


BROWSE_SCHEMA = {
    "name": "profile_browse",
    "description": (
        "Read the user's profile category tree and stored preferences. Use when "
        "the user asks what is remembered, wants to inspect a category, or needs "
        "an exact item id before changing or revoking a preference."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": (
                    "Optional profile:// category path or relative path prefix. "
                    "Omit to browse the complete tree."
                ),
            },
            "kind": {
                "type": "string",
                "enum": sorted(PROFILE_KINDS),
                "description": "Optional profile item type filter.",
            },
            "status": {
                "type": "string",
                "enum": ["active", "candidate", "all"],
                "description": "Items to show; defaults to active and candidate.",
            },
        },
        "additionalProperties": False,
    },
}


_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["add", "strengthen", "supersede"],
                    },
                    "target_id": {"type": "string"},
                    "kind": {"type": "string", "enum": sorted(PROFILE_KINDS)},
                    "scope": {"type": "string", "minLength": 1, "maxLength": 320},
                    "applies_when": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 800,
                    },
                    "rule": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "durability": {
                        "type": "string",
                        "enum": ["explicit", "inferred"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence_turns": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "integer", "minimum": 1},
                    },
                },
                "required": [
                    "operation",
                    "kind",
                    "scope",
                    "applies_when",
                    "rule",
                    "durability",
                    "confidence",
                    "evidence_turns",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}


_INTENT_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["retrieve", "skip"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 300},
    },
    "required": ["decision", "confidence", "reason"],
    "additionalProperties": False,
}


_INTENT_GATE_INSTRUCTIONS = """Decide whether the current user request should retrieve durable user-profile memory before answering.

Profile memory can contain stable identity facts, communication preferences, and workflow/artifact conventions. Return "retrieve" whenever any such information could materially improve the answer, including work-product creation or editing, underspecified requests, requests about the user, or ambiguous cases.

Return "skip" only when the current request is self-contained and makes historical personalization unnecessary or explicitly irrelevant. Strong skip evidence includes an explicit request not to use remembered preferences/history, or a fully specified temporary task that explicitly excludes the remembered-workflow dimensions it mentions. Current-turn instructions override historical preferences.

Examples:
- "Write part of my current paper and return LaTeX source." -> retrieve
- "What do you remember about how I write papers?" -> retrieve
- "Explain sample mean." -> retrieve (communication preferences could still help)
- "This is not a paper task; do not return formulas or source. Explain sample mean in two ordinary Chinese sentences." -> skip

When uncertain, return "retrieve". Judge only whether retrieval should run; do not answer the user's request.
"""


_EXTRACTION_INSTRUCTIONS = """Extract only durable user-profile information from the numbered USER turns.

Allowed kinds:
- identity: stable facts about the user, their role, language, expertise, or enduring environment.
- interaction_preference: stable preferences for how the assistant should communicate or collaborate.
- workflow_preference: stable work, tooling, coding, document, artifact, or process conventions.

Rules:
1. Output one atomic item per rule. Split nearby requirements into separate items.
2. Classify each item into the deepest matching existing_scope_path. Create an open-vocabulary relative topic path only when no existing category fits. Stable roots are added by the store: identity -> identity/, interaction_preference -> preferences/interaction/, workflow_preference -> preferences/workflow/.
3. "explicit" means the user clearly states a durable preference/fact (for example "以后", "always", "I prefer", "记住").
4. "inferred" requires repeated behavioral evidence; do not infer personality or preferences from a single ordinary request.
5. Exclude one-off instructions, current-task requirements, generic events, project progress, assistant statements, secrets, credentials, and anything not grounded in a numbered user turn.
6. Use strengthen only when an existing item expresses the same atomic rule. Use supersede only when the user explicitly corrects or replaces an existing item, and set target_id. Otherwise use add.
7. evidence_turns must contain only numbered turns that directly support the item.
8. Return {"items": []} when nothing qualifies.
9. Write applies_when and rule in the user's language; preserve technical identifiers exactly.
"""


def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        config = load_config_readonly() or {}
    except Exception:
        return {}
    memory = config.get("memory")
    if not isinstance(memory, dict):
        return {}
    profile = memory.get(_PLUGIN_ID)
    return dict(profile) if isinstance(profile, dict) else {}


def _normalize_scope(raw: Any) -> Tuple[str, str]:
    return normalize_scope_path(raw, max_segments=8)


def _normalize_category_input(raw: Any) -> Tuple[str, str]:
    value = str(raw or "").strip()
    if value.startswith("profile://"):
        value = value[len("profile://") :]
    return normalize_scope_path(value, max_segments=10)


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.strip().lower() in {"true", "yes", "1", "on"}:
            return True
        if value.strip().lower() in {"false", "no", "0", "off"}:
            return False
    return default


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _as_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _tokenize(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold()
    latin = set(re.findall(r"[a-z0-9_]{2,}", normalized))
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    grams: set[str] = set()
    for run in chinese_runs:
        if len(run) == 1:
            grams.add(run)
        else:
            grams.update(run[index : index + 2] for index in range(len(run) - 1))
    return latin | grams


def _lexical_similarity(query: str, text: str) -> float:
    query_tokens = _tokenize(query)
    text_tokens = _tokenize(text)
    if not query_tokens or not text_tokens:
        return 0.0
    overlap = query_tokens & text_tokens
    if not overlap:
        return 0.0
    # Overlap coefficient, not Jaccard: a precise task marker such as
    # ``latex`` should remain decisive even when the stored rule carries a
    # longer scope and applicability condition.
    return len(overlap) / min(len(query_tokens), len(text_tokens))


class ProfileMemoryProvider(MemoryProvider):
    """Local, profile-scoped memory with main-model extraction."""

    def __init__(
        self,
        *,
        llm: Any = None,
        config: Optional[Dict[str, Any]] = None,
        store: Optional[ProfileStore] = None,
        embedder: Any = None,
    ) -> None:
        self._llm = llm
        self._config_override = dict(config) if config is not None else None
        self._store = store
        self._embedder = embedder
        self._owns_store = store is None
        self._owns_embedder = embedder is None
        self._session_id = ""
        self._hermes_home = ""
        self._snapshot_path: Optional[Path] = None
        self._agent_context = "primary"
        self._last_recall: Optional[RecallStatus] = None
        self._last_intent_gate: Optional[Dict[str, Any]] = None
        self._extraction_lock = threading.Lock()

    @property
    def name(self) -> str:
        return _PLUGIN_ID

    def _config(self) -> Dict[str, Any]:
        return (
            dict(self._config_override)
            if self._config_override is not None
            else _load_config()
        )

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = str(session_id or "")
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary")
        read_only = bool(kwargs.get("read_only"))
        cfg = self._config()
        root = Path(self._hermes_home or Path.home() / ".hermes").expanduser()
        db_path = str(cfg.get("db_path") or root / "profile_memory.sqlite3")
        db_path = db_path.replace("$HERMES_HOME", str(root)).replace(
            "${HERMES_HOME}", str(root)
        )
        snapshot_path = str(
            cfg.get("snapshot_path") or root / "profile_memory" / "PROFILE.md"
        )
        snapshot_path = snapshot_path.replace("$HERMES_HOME", str(root)).replace(
            "${HERMES_HOME}", str(root)
        )
        self._snapshot_path = Path(snapshot_path).expanduser().resolve()
        if self._store is None:
            self._store = ProfileStore(db_path)

        if (
            not read_only
            and self._embedder is None
            and _as_bool(cfg.get("semantic_recall", True), True)
        ):
            model_path = str(
                cfg.get("embedding_model_path")
                or root / "models" / "profile-memory" / "bge-small-zh-v1.5-f16.gguf"
            )
            model_path = model_path.replace("$HERMES_HOME", str(root)).replace(
                "${HERMES_HOME}", str(root)
            )
            self._embedder = LocalBgeEmbedder(
                model_path=model_path,
                model_id=str(cfg.get("embedding_model") or DEFAULT_MODEL_ID),
                dimension=_as_int(
                    cfg.get("embedding_dimension"), DEFAULT_DIMENSION, 1, 16384
                ),
            )

    def system_prompt_block(self) -> str:
        return (
            "# Profile Memory\n"
            "Store only durable user identity, interaction preferences, and work "
            "preferences. Use profile_remember for explicit durable statements; "
            "make one atomic rule per call and classify it into the deepest reusable "
            "profile category with an applicability condition. Use profile_browse "
            "when the user asks to inspect remembered categories or preferences. "
            "Do not store one-off task instructions, generic events, "
            "resource content, secrets, or assistant inferences. Recalled profile "
            "items apply only when their stated condition is relevant. Use "
            "profile_forget only for an exact item id when the user asks to revoke it."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [REMEMBER_SCHEMA, FORGET_SCHEMA, BROWSE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "profile_remember":
            return self._remember(args)
        if tool_name == "profile_forget":
            return self._forget(args)
        if tool_name == "profile_browse":
            return self._browse(args)
        return tool_error(f"Unknown profile memory tool: {tool_name}")

    def _ensure_store(self) -> ProfileStore:
        if self._store is None:
            raise RuntimeError("profile memory provider is not initialized")
        return self._store

    def _remember(self, args: Dict[str, Any]) -> str:
        if self._agent_context != "primary":
            return tool_error("profile writes are disabled outside the primary agent")
        scope, error = _normalize_category_input(args.get("scope"))
        if error:
            return tool_error(error)
        evidence_text = str(args.get("evidence") or "").strip()
        if not evidence_text:
            return tool_error("evidence is required")
        try:
            store = self._ensure_store()
            evidence_id = store.add_evidence(
                session_id=self._session_id or "explicit",
                turn_index=-1,
                user_text=evidence_text,
                source_type="explicit_tool",
            )
            item = store.upsert_item(
                kind=str(args.get("kind") or ""),
                scope=scope,
                applies_when=str(args.get("applies_when") or ""),
                rule=str(args.get("rule") or ""),
                confidence=1.0,
                explicit=True,
                evidence_ids=[evidence_id],
                activation_sessions=self._activation_sessions(),
                supersedes_id=str(args.get("supersedes_id") or "").strip(),
            )
            self._embed_item(item)
            self._write_snapshot()
            return json.dumps(
                {
                    "status": "stored",
                    "item_id": item["item_id"],
                    "kind": item["kind"],
                    "scope": item["scope"],
                    "category": f"profile://{item['scope']}",
                    "profile_status": item["status"],
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            logger.warning("profile_remember failed: %s", exc)
            return tool_error(f"Failed to store profile item: {exc}")

    def _forget(self, args: Dict[str, Any]) -> str:
        if self._agent_context != "primary":
            return tool_error("profile writes are disabled outside the primary agent")
        item_id = str(args.get("item_id") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", item_id):
            return tool_error("item_id must be an exact 32-character profile id")
        try:
            changed = self._ensure_store().set_status(item_id, "revoked")
            if not changed:
                return tool_error("profile item not found")
            self._write_snapshot()
            return json.dumps({"status": "revoked", "item_id": item_id})
        except Exception as exc:
            return tool_error(f"Failed to revoke profile item: {exc}")

    def _browse(self, args: Dict[str, Any]) -> str:
        raw_status = str(args.get("status") or "").strip()
        if raw_status not in {"", "active", "candidate", "all"}:
            return tool_error("status must be active, candidate, or all")
        statuses = {
            "active": ("active",),
            "candidate": ("candidate",),
            "all": ("active", "candidate", "superseded", "revoked"),
            "": ("active", "candidate"),
        }[raw_status]
        kind = str(args.get("kind") or "").strip()
        if kind and kind not in PROFILE_KINDS:
            return tool_error("unsupported profile kind")

        requested_scope = str(args.get("scope") or "").strip()
        scope_prefix = requested_scope
        if scope_prefix.startswith("profile://"):
            scope_prefix = scope_prefix[len("profile://") :]
        scope_prefix = scope_prefix.strip("/")
        if scope_prefix:
            normalized, error = normalize_scope_path(scope_prefix, max_segments=10)
            if error:
                return tool_error(error)
            scope_prefix = normalized

        store = self._ensure_store()
        nodes = store.list_scope_nodes()
        scope_found = not scope_prefix
        if scope_prefix and any(str(node["path"]) == scope_prefix for node in nodes):
            scope_found = True
        elif scope_prefix:
            suffix_matches = [
                str(node["path"])
                for node in nodes
                if str(node["path"]).endswith(f"/{scope_prefix}")
            ]
            if len(suffix_matches) > 1:
                return tool_error(
                    "profile category is ambiguous; use a longer category path",
                    candidates=[f"profile://{path}" for path in sorted(suffix_matches)],
                )
            if suffix_matches:
                scope_prefix = suffix_matches[0]
                scope_found = True
        items = store.list_items(
            statuses=statuses,
            limit=500,
            kind=kind,
            scope_prefix=scope_prefix,
        )
        visible_paths = {str(item.get("scope") or "") for item in items}
        visible_tree = set()
        for item_path in visible_paths:
            parts = item_path.split("/")
            visible_tree.update(
                "/".join(parts[:depth]) for depth in range(1, len(parts) + 1)
            )
        categories = []
        for node in nodes:
            path = str(node["path"])
            if scope_prefix and not (
                path == scope_prefix
                or path.startswith(f"{scope_prefix}/")
                or scope_prefix.startswith(f"{path}/")
            ):
                continue
            if (kind or raw_status in {"active", "candidate"}) and (
                visible_paths and path not in visible_tree
            ):
                continue
            categories.append(
                {
                    "uri": f"profile://{path}",
                    "depth": int(node["depth"]),
                    "active_items": int(node["active_count"]),
                    "candidate_items": int(node["candidate_count"]),
                }
            )
        return json.dumps(
            {
                "requested_scope": requested_scope,
                "resolved_scope": f"profile://{scope_prefix}"
                if scope_found and scope_prefix
                else "profile://",
                "scope_found": scope_found,
                "categories": categories,
                "items": [
                    {
                        "item_id": item["item_id"],
                        "kind": item["kind"],
                        "category": f"profile://{item['scope']}",
                        "status": item["status"],
                        "applies_when": item["applies_when"],
                        "rule": item["rule"],
                        "confidence": item["confidence"],
                    }
                    for item in items
                ],
                "truncated": len(items) >= 500,
            },
            ensure_ascii=False,
        )

    def _activation_sessions(self) -> int:
        return _as_int(
            self._config().get("inferred_activation_sessions"),
            _DEFAULT_INFERRED_ACTIVATION_SESSIONS,
            2,
            10,
        )

    def _embed_item(self, item: Dict[str, Any]) -> None:
        if self._embedder is None or item.get("status") not in {"active", "candidate"}:
            return
        try:
            vector = self._embedder.embed(profile_item_text(item), is_query=False)
            if vector:
                self._ensure_store().save_embedding(
                    item_id=str(item["item_id"]),
                    model_id=str(self._embedder.model_id),
                    vector=vector,
                    content_hash=str(item["content_hash"]),
                )
        except Exception as exc:
            logger.debug(
                "profile embedding unavailable; lexical recall remains active: %s", exc
            )

    def _scope_vector(self, node: Dict[str, Any]) -> List[float]:
        if self._embedder is None:
            return []
        scope_id = str(node.get("scope_id") or "")
        text = self._ensure_store().scope_text(scope_id)
        if not text:
            return []
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            vector = self._ensure_store().embedding_for_scope(
                scope_id,
                model_id=str(self._embedder.model_id),
                content_hash=content_hash,
            )
            if vector:
                return vector
            vector = self._embedder.embed(text, is_query=False)
            if vector:
                self._ensure_store().save_scope_embedding(
                    scope_id=scope_id,
                    model_id=str(self._embedder.model_id),
                    vector=vector,
                    content_hash=content_hash,
                )
            return vector
        except Exception as exc:
            logger.debug("profile category embedding unavailable: %s", exc)
            return []

    @staticmethod
    def _scope_relation_score(
        item_scope: str, routed: Sequence[Tuple[float, str]]
    ) -> float:
        scores = [
            score
            for score, scope in routed
            if item_scope == scope
            or item_scope.startswith(f"{scope}/")
            or scope.startswith(f"{item_scope}/")
        ]
        return max(scores, default=0.0)

    def _route_scopes(
        self,
        query: str,
        query_vector: Sequence[float],
        *,
        lexical_threshold: float,
        cfg: Dict[str, Any],
    ) -> List[Tuple[float, str]]:
        nodes = [
            node
            for node in self._ensure_store().list_scope_nodes()
            if int(node.get("active_count") or 0) > 0
        ]
        if not nodes:
            return []
        threshold = _as_float(
            cfg.get("scope_score_threshold"),
            _DEFAULT_SCOPE_SCORE_THRESHOLD,
            0.0,
            1.0,
        )
        window = _as_float(
            cfg.get("scope_score_window"),
            _DEFAULT_SCOPE_SCORE_WINDOW,
            0.0,
            1.0,
        )
        scored: List[Tuple[float, str]] = []
        for node in nodes:
            path = str(node["path"])
            lexical_score = _lexical_similarity(
                query, path.replace("/", " ").replace("-", " ")
            )
            semantic_score = 0.0
            if query_vector:
                semantic_score = max(
                    0.0,
                    cosine_similarity(query_vector, self._scope_vector(node)),
                )
            if lexical_score >= lexical_threshold:
                scored.append((max(semantic_score, lexical_score) + 0.15, path))
            elif semantic_score >= threshold:
                scored.append((semantic_score, path))
        if not scored:
            return []
        best = max(score for score, _path in scored)
        scored = [pair for pair in scored if pair[0] >= best - window]
        scored.sort(reverse=True)
        limit = _as_int(cfg.get("scope_route_limit"), _DEFAULT_SCOPE_ROUTE_LIMIT, 1, 20)
        return scored[:limit]

    def _write_snapshot(self) -> None:
        if self._snapshot_path is None or self._store is None:
            return
        try:
            self._store.render_snapshot(self._snapshot_path)
        except Exception as exc:
            logger.debug("profile snapshot write failed: %s", exc)

    def _intent_gate_allows_recall(self, query: str, cfg: Dict[str, Any]) -> bool:
        self._last_intent_gate = None
        if not _as_bool(cfg.get("intent_gate", False), False):
            return True
        if self._llm is None:
            self._last_intent_gate = {
                "decision": "retrieve",
                "confidence": 0.0,
                "fail_open": True,
                "reason": "structured LLM unavailable",
            }
            return True

        try:
            result = self._llm.complete_structured(
                instructions=_INTENT_GATE_INSTRUCTIONS,
                input=[{"type": "text", "text": str(query or "")}],
                json_schema=_INTENT_GATE_SCHEMA,
                schema_name="profile_memory_retrieval_intent",
                temperature=0.0,
                max_tokens=160,
                timeout=_as_float(
                    cfg.get("intent_gate_timeout_seconds"),
                    _DEFAULT_INTENT_GATE_TIMEOUT,
                    1.0,
                    180.0,
                ),
                purpose="profile-memory retrieval intent gate",
            )
            parsed = result.parsed
            if not isinstance(parsed, dict):
                raise ValueError("intent gate returned no validated object")
            decision = str(parsed.get("decision") or "").strip().lower()
            if decision not in {"retrieve", "skip"}:
                raise ValueError("intent gate returned an invalid decision")
            confidence = _as_float(parsed.get("confidence"), 0.0, 0.0, 1.0)
            minimum = _as_float(
                cfg.get("intent_gate_min_skip_confidence"),
                _DEFAULT_INTENT_GATE_MIN_SKIP_CONFIDENCE,
                0.0,
                1.0,
            )
            skip = decision == "skip" and confidence >= minimum
            usage = getattr(result, "usage", None)
            self._last_intent_gate = {
                "decision": decision,
                "confidence": confidence,
                "minimum_skip_confidence": minimum,
                "skipped": skip,
                "fail_open": False,
                "reason": str(parsed.get("reason") or "")[:300],
                "provider": str(result.provider),
                "model": str(result.model),
                "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
                "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
                "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            }
            logger.info(
                "profile intent gate decision=%s confidence=%.3f skipped=%s",
                decision,
                confidence,
                skip,
            )
            return not skip
        except Exception as exc:
            self._last_intent_gate = {
                "decision": "retrieve",
                "confidence": 0.0,
                "skipped": False,
                "fail_open": True,
                "reason": f"{type(exc).__name__}: {exc}"[:300],
            }
            logger.warning("profile intent gate failed open: %s", exc)
            return True

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        self._last_recall = None
        if is_trivial_prompt(query) or self._store is None:
            return ""
        if int(self._store.stats().get("active") or 0) <= 0:
            self._last_intent_gate = None
            return ""
        cfg = self._config()
        if not self._intent_gate_allows_recall(query, cfg):
            return ""
        limit = _as_int(cfg.get("recall_limit"), _DEFAULT_RECALL_LIMIT, 1, 20)
        threshold = _as_float(
            cfg.get("score_threshold"), _DEFAULT_SCORE_THRESHOLD, 0.0, 1.0
        )
        lexical_threshold = _as_float(
            cfg.get("lexical_score_threshold"),
            _DEFAULT_LEXICAL_THRESHOLD,
            0.0,
            1.0,
        )
        items = self._store.list_items(statuses=("active",), limit=5000)
        if not items:
            return ""

        query_vector: List[float] = []
        semantic = False
        if self._embedder is not None:
            try:
                query_vector = self._embedder.embed(query, is_query=True)
                semantic = bool(query_vector)
            except Exception as exc:
                logger.debug("profile semantic query unavailable: %s", exc)

        routed_scopes = self._route_scopes(
            query,
            query_vector,
            lexical_threshold=lexical_threshold,
            cfg=cfg,
        )
        routed_workflow = [
            pair for pair in routed_scopes if pair[1].startswith("preferences/workflow")
        ]

        direct_matches: List[Tuple[float, Dict[str, Any]]] = []
        semantic_only: Dict[str, List[Tuple[float, Dict[str, Any]]]] = {
            kind: [] for kind in PROFILE_KINDS
        }
        semantic_only_threshold = _as_float(
            cfg.get("semantic_only_score_threshold"),
            _DEFAULT_SEMANTIC_ONLY_THRESHOLD,
            0.0,
            1.0,
        )
        semantic_window = _as_float(
            cfg.get("semantic_score_window"),
            _DEFAULT_SEMANTIC_SCORE_WINDOW,
            0.0,
            1.0,
        )
        for item in items:
            item_scope = str(item.get("scope") or "")
            lexical_score = _lexical_similarity(
                query,
                f"{item_scope} {item.get('applies_when', '')} {item.get('rule', '')}",
            )
            if semantic:
                vector = self._store.embedding_for_item(
                    str(item["item_id"]),
                    model_id=str(self._embedder.model_id),
                    content_hash=str(item["content_hash"]),
                )
                if not vector:
                    self._embed_item(item)
                    vector = self._store.embedding_for_item(
                        str(item["item_id"]),
                        model_id=str(self._embedder.model_id),
                        content_hash=str(item["content_hash"]),
                    )
                semantic_score = max(0.0, cosine_similarity(query_vector, vector))
                route_score = self._scope_relation_score(item_scope, routed_scopes)
                if route_score:
                    direct_matches.append(
                        (max(semantic_score, lexical_score) + 0.20 * route_score, item)
                    )
                    continue
                if lexical_score >= lexical_threshold:
                    # Exact scope/condition overlap is strong applicability
                    # evidence. A fixed boost is more stable than a tiny
                    # Jaccard value diluted by a long rule.
                    direct_matches.append((semantic_score + 0.15, item))
                    continue
                kind = str(item.get("kind") or "")
                if kind == "workflow_preference" and routed_workflow:
                    # A workflow directory was confidently selected, so do not
                    # leak semantically broad rules from unrelated branches.
                    continue
                kind_threshold = semantic_only_threshold
                if kind == "interaction_preference":
                    kind_threshold = min(kind_threshold, 0.30)
                elif kind == "identity":
                    kind_threshold = min(kind_threshold, 0.45)
                if semantic_score >= max(threshold, kind_threshold):
                    semantic_only.setdefault(kind, []).append((semantic_score, item))
            else:
                route_score = self._scope_relation_score(item_scope, routed_scopes)
                if route_score:
                    direct_matches.append((route_score, item))
                elif lexical_score >= lexical_threshold:
                    direct_matches.append((lexical_score, item))

        # After directory routing, semantic-only fallbacks are accepted only
        # near the best item of their own kind. Direct directory and lexical
        # matches are never displaced by this relative filter.
        scored = list(direct_matches)
        for candidates in semantic_only.values():
            if not candidates:
                continue
            best = max(score for score, _item in candidates)
            scored.extend(
                (score, item)
                for score, item in candidates
                if score >= best - semantic_window
            )

        scored.sort(
            key=lambda pair: (pair[0], float(pair[1].get("confidence", 0.0))),
            reverse=True,
        )
        selected = scored[:limit]
        if not selected:
            return ""
        max_chars = _as_int(cfg.get("max_injected_chars"), 3000, 500, 20000)
        lines = [
            "<profile_memory_context>",
            "Relevant durable user profile items. Apply only when the condition matches:",
        ]
        used = sum(len(line) for line in lines)
        count = 0
        for score, item in selected:
            line = (
                f"- [id={item['item_id']} kind={item['kind']} "
                f"category=profile://{item['scope']} "
                f"score={score:.3f}] When {item['applies_when']}: {item['rule']}"
            )
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
            count += 1
        if not count:
            return ""
        lines.append("</profile_memory_context>")
        self._last_recall = RecallStatus(
            provider_label="Profile memory", count=count, glyph="👤"
        )
        return "\n".join(lines)

    def recall_status(self) -> Optional[RecallStatus]:
        return self._last_recall

    def intent_gate_status(self) -> Optional[Dict[str, Any]]:
        """Return a copy of the most recent gate decision for diagnostics."""
        return (
            dict(self._last_intent_gate) if self._last_intent_gate is not None else None
        )

    @staticmethod
    def _user_turns(
        messages: Sequence[Dict[str, Any]], max_chars: int
    ) -> List[Tuple[int, str]]:
        candidates: List[str] = []
        try:
            from agent.context_compressor import is_compaction_summary_message
        except Exception:
            is_compaction_summary_message = lambda _message: False
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            if is_compaction_summary_message(message):
                continue
            text = flatten_message_text(message.get("content")).strip()
            text = extract_user_instruction_from_skill_message(text)
            if text is None:
                continue
            text = text.strip()
            if len(text) < 4 or is_trivial_prompt(text):
                continue
            candidates.append(text)

        # Prefer the most recent user evidence when a very long session must be
        # bounded, but return the retained turns in their original order.  A
        # durable correction near the end of a session must not be displaced by
        # old task chatter merely because that chatter appeared first.
        retained: List[str] = []
        total = 0
        for text in reversed(candidates):
            if total + len(text) > max_chars:
                remaining = max_chars - total
                if remaining < 40:
                    break
                text = text[-remaining:]
            retained.append(text)
            total += len(text)
            if total >= max_chars:
                break
        retained.reverse()
        return [(index, text) for index, text in enumerate(retained, start=1)]

    def _existing_profile_for_extraction(self) -> List[Dict[str, str]]:
        items = self._ensure_store().list_items(
            statuses=("active", "candidate"), limit=200
        )
        selected: List[Dict[str, str]] = []
        used = 0
        for item in items:
            record = {
                "id": str(item["item_id"]),
                "status": str(item["status"]),
                "kind": str(item["kind"]),
                "scope": str(item["scope"]),
                "applies_when": str(item["applies_when"]),
                "rule": str(item["rule"]),
            }
            size = len(json.dumps(record, ensure_ascii=False))
            if selected and used + size > 12000:
                break
            selected.append(record)
            used += size
        return selected

    def _existing_scope_paths_for_extraction(self) -> List[str]:
        return [
            str(node["path"])
            for node in self._ensure_store().list_scope_nodes()
            if int(node.get("active_count") or 0)
            or int(node.get("candidate_count") or 0)
        ][:200]

    def _extract_messages(self, messages: List[Dict[str, Any]]) -> int:
        cfg = self._config()
        if (
            self._agent_context != "primary"
            or not _as_bool(cfg.get("extract_on_session_end", True), True)
            or self._llm is None
            or self._store is None
        ):
            return 0
        max_chars = _as_int(
            cfg.get("extraction_max_user_chars"),
            _DEFAULT_EXTRACTION_MAX_CHARS,
            1000,
            100000,
        )
        turns = self._user_turns(messages or [], max_chars)
        if not turns:
            return 0
        transcript = "\n".join(f"USER TURN {index}: {text}" for index, text in turns)
        transcript_hash = hashlib.sha256(transcript.encode("utf-8")).hexdigest()
        sid = self._session_id or "unknown"
        if self._store.extraction_recorded(
            session_id=sid, transcript_hash=transcript_hash
        ):
            return 0
        if not self._extraction_lock.acquire(blocking=False):
            logger.debug(
                "profile extraction already running; skipped duplicate session end"
            )
            return 0
        applied = 0
        try:
            payload = json.dumps(
                {
                    "existing_profile_items": self._existing_profile_for_extraction(),
                    "existing_scope_paths": self._existing_scope_paths_for_extraction(),
                    "numbered_user_turns": [
                        {"turn": index, "text": text} for index, text in turns
                    ],
                },
                ensure_ascii=False,
            )
            result = self._llm.complete_structured(
                instructions=_EXTRACTION_INSTRUCTIONS,
                input=[{"type": "text", "text": payload}],
                json_schema=_EXTRACTION_SCHEMA,
                schema_name="profile_memory_extraction",
                temperature=0.0,
                max_tokens=2000,
                timeout=_as_float(
                    cfg.get("extraction_timeout_seconds"),
                    _DEFAULT_EXTRACTION_TIMEOUT,
                    5.0,
                    600.0,
                ),
                purpose="profile-memory session extraction",
            )
            parsed = result.parsed
            if not isinstance(parsed, dict) or not isinstance(
                parsed.get("items"), list
            ):
                raise ValueError("profile extractor returned no validated items object")
            turn_map = dict(turns)
            for raw in parsed["items"][:20]:
                if self._apply_extracted_item(raw, turn_map=turn_map, session_id=sid):
                    applied += 1
            self._store.record_extraction(
                session_id=sid,
                transcript_hash=transcript_hash,
                status="completed",
                provider=str(result.provider),
                model=str(result.model),
            )
            self._write_snapshot()
            return applied
        except Exception as exc:
            logger.warning("profile extraction failed for session %s: %s", sid, exc)
            self._store.record_extraction(
                session_id=sid,
                transcript_hash=transcript_hash,
                status="failed",
                error=str(exc),
            )
            return 0
        finally:
            self._extraction_lock.release()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self._extract_messages(messages)

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Extract before compression discards raw user evidence.

        The compression pipeline already runs this hook on its background
        worker.  Returning an empty string avoids duplicating profile records in
        the compression prompt; the durable SQLite write is the side effect we
        need here.
        """
        self._extract_messages(messages)
        return ""

    def _apply_extracted_item(
        self,
        raw: Any,
        *,
        turn_map: Dict[int, str],
        session_id: str,
    ) -> bool:
        if not isinstance(raw, dict):
            return False
        scope, error = _normalize_category_input(raw.get("scope"))
        if error:
            logger.debug("profile extractor emitted invalid scope: %s", error)
            return False
        evidence_turns = []
        for value in raw.get("evidence_turns") or []:
            try:
                turn = int(value)
            except (TypeError, ValueError):
                continue
            if turn in turn_map and turn not in evidence_turns:
                evidence_turns.append(turn)
        if not evidence_turns:
            return False
        evidence_ids = [
            self._ensure_store().add_evidence(
                session_id=session_id,
                turn_index=turn,
                user_text=turn_map[turn],
                source_type="main_model_extraction",
            )
            for turn in evidence_turns
        ]
        operation = str(raw.get("operation") or "add")
        if operation not in {"add", "strengthen", "supersede"}:
            return False
        target_id = str(raw.get("target_id") or "").strip()
        if operation == "strengthen":
            target = self._ensure_store().get_item(target_id)
            if not target or target.get("status") not in {"active", "candidate"}:
                return False
            item = self._ensure_store().upsert_item(
                kind=str(target["kind"]),
                scope=str(target["scope"]),
                applies_when=str(target["applies_when"]),
                rule=str(target["rule"]),
                confidence=float(raw.get("confidence", target["confidence"])),
                explicit=bool(target.get("explicit"))
                or str(raw.get("durability")) == "explicit",
                evidence_ids=evidence_ids,
                activation_sessions=self._activation_sessions(),
            )
        else:
            supersedes = target_id if operation == "supersede" else ""
            if supersedes and not re.fullmatch(r"[0-9a-f]{32}", supersedes):
                return False
            item = self._ensure_store().upsert_item(
                kind=str(raw.get("kind") or ""),
                scope=scope,
                applies_when=str(raw.get("applies_when") or ""),
                rule=str(raw.get("rule") or ""),
                confidence=float(raw.get("confidence", 0.0)),
                explicit=str(raw.get("durability")) == "explicit",
                evidence_ids=evidence_ids,
                activation_sessions=self._activation_sessions(),
                supersedes_id=supersedes,
            )
        self._embed_item(item)
        return True

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        **kwargs,
    ) -> None:
        self._session_id = str(new_session_id or self._session_id)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "db_path",
                "description": "Profile-scoped SQLite database path",
                "default": "$HERMES_HOME/profile_memory.sqlite3",
            },
            {
                "key": "snapshot_path",
                "description": "Audit-only Markdown profile snapshot",
                "default": "$HERMES_HOME/profile_memory/PROFILE.md",
            },
            {
                "key": "embedding_model_path",
                "description": "Local BGE GGUF model path",
                "default": "$HERMES_HOME/models/profile-memory/bge-small-zh-v1.5-f16.gguf",
            },
            {
                "key": "semantic_recall",
                "description": "Use local BGE semantic recall when available",
                "type": "boolean",
                "default": True,
            },
            {
                "key": "extract_on_session_end",
                "description": "Use the active Ansatz model to extract profile items",
                "type": "boolean",
                "default": True,
            },
            {
                "key": "intent_gate",
                "description": (
                    "Use the active Ansatz model to decide whether profile recall "
                    "is needed before retrieval"
                ),
                "type": "boolean",
                "default": False,
            },
            {
                "key": "intent_gate_min_skip_confidence",
                "description": "Confidence required for the intent gate to suppress recall",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_INTENT_GATE_MIN_SKIP_CONFIDENCE,
            },
            {
                "key": "intent_gate_timeout_seconds",
                "description": "Maximum duration of the retrieval intent decision",
                "type": "number",
                "minimum": 1.0,
                "maximum": 180.0,
                "default": _DEFAULT_INTENT_GATE_TIMEOUT,
            },
            {
                "key": "recall_limit",
                "description": "Maximum relevant profile items injected per turn",
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": _DEFAULT_RECALL_LIMIT,
            },
            {
                "key": "score_threshold",
                "description": "Minimum semantic similarity for profile recall",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_SCORE_THRESHOLD,
            },
            {
                "key": "semantic_only_score_threshold",
                "description": "Minimum similarity without lexical scope overlap",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_SEMANTIC_ONLY_THRESHOLD,
            },
            {
                "key": "semantic_score_window",
                "description": "Per-kind distance retained below the best semantic match",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_SEMANTIC_SCORE_WINDOW,
            },
            {
                "key": "scope_score_threshold",
                "description": "Minimum semantic similarity for category routing",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_SCOPE_SCORE_THRESHOLD,
            },
            {
                "key": "scope_score_window",
                "description": "Category distance retained below the best route",
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "default": _DEFAULT_SCOPE_SCORE_WINDOW,
            },
            {
                "key": "scope_route_limit",
                "description": "Maximum category branches selected before item ranking",
                "type": "integer",
                "minimum": 1,
                "maximum": 20,
                "default": _DEFAULT_SCOPE_ROUTE_LIMIT,
            },
            {
                "key": "inferred_activation_sessions",
                "description": "Distinct sessions required to activate an inferred item",
                "type": "integer",
                "minimum": 2,
                "maximum": 10,
                "default": _DEFAULT_INFERRED_ACTIVATION_SESSIONS,
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        config_path = Path(hermes_home) / "config.yaml"
        from hermes_cli.config import read_user_config_raw

        config = read_user_config_raw(config_path) or {}
        memory = config.setdefault("memory", {})
        memory[_PLUGIN_ID] = dict(values)
        try:
            import yaml

            payload = yaml.safe_dump(config, allow_unicode=True, sort_keys=False)
        except Exception as exc:
            raise RuntimeError(
                f"cannot serialize profile memory config: {exc}"
            ) from exc
        temp = config_path.with_name(f".{config_path.name}.profile-memory.tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.chmod(0o600)
        temp.replace(config_path)

    def backup_paths(self) -> List[str]:
        cfg = self._config()
        try:
            from hermes_constants import get_hermes_home

            root = Path(get_hermes_home())
        except Exception:
            root = Path.home() / ".hermes"
        db_path = str(cfg.get("db_path") or root / "profile_memory.sqlite3")
        snapshot = str(
            cfg.get("snapshot_path") or root / "profile_memory" / "PROFILE.md"
        )
        for token in ("$HERMES_HOME", "${HERMES_HOME}"):
            db_path = db_path.replace(token, str(root))
            snapshot = snapshot.replace(token, str(root))
        paths = [
            Path(db_path).expanduser().resolve(),
            Path(snapshot).expanduser().resolve(),
        ]
        external = []
        for path in paths:
            try:
                path.relative_to(root.expanduser().resolve())
            except ValueError:
                external.append(str(path))
        return external

    def shutdown(self) -> None:
        if self._owns_embedder and self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                pass
        if self._owns_store and self._store is not None:
            try:
                self._store.close()
            except Exception:
                pass
        self._embedder = None
        self._store = None


def register(ctx) -> None:
    ctx.register_memory_provider(ProfileMemoryProvider(llm=ctx.llm))
