"""Versioned Card construction, rendering, and benefit checks for V1."""

from __future__ import annotations

import json
from typing import Any

from agent.model_metadata import estimate_tokens_rough

from .models import ObjectCard, ObjectRecord
from .store import ObjectContextStore, canonical_json


CARD_SCHEMA_VERSION = "1.1"
RETRIEVAL_CARD_SCHEMA_VERSION = "1.1"
CARD_OPEN = "<OBJECT_CARD>"
CARD_CLOSE = "</OBJECT_CARD>"
RETRIEVAL_CARD_OPEN = "<RETRIEVAL_CARD>"
RETRIEVAL_CARD_CLOSE = "</RETRIEVAL_CARD>"
_ORIGIN_KEYS = frozenset({"role", "tool", "operation", "target"})


def build_card(
    record: ObjectRecord,
    *,
    summary: str,
    contains: dict[str, Any],
    origin: dict[str, Any] | None = None,
) -> ObjectCard:
    card = ObjectCard(
        schema_version=CARD_SCHEMA_VERSION,
        object_ref=record.object_ref,
        object_id=record.object_id,
        version=record.version,
        object_type=record.object_type,
        name=record.name or f"{record.object_type.value}_{record.object_id[-8:]}",
        language=record.language,
        summary=str(summary or "").strip(),
        contains=contains if isinstance(contains, dict) else {},
        origin=origin if isinstance(origin, dict) else {},
        metadata={
            key: record.metadata[key]
            for key in ("format", "mime_type")
            if key in record.metadata
        },
        supersedes=record.supersedes,
        derived_from=record.derived_from,
    )
    validate_card(card)
    return card


def validate_card(card: ObjectCard) -> None:
    if card.schema_version != CARD_SCHEMA_VERSION:
        raise ValueError("unsupported Card schema")
    parsed = ObjectContextStore.parse_object_ref(card.object_ref)
    if parsed is None:
        raise ValueError("Card has malformed object_ref")
    object_id, version = parsed
    if object_id != card.object_id or version != card.version:
        raise ValueError("Card identity fields disagree")
    if not isinstance(card.origin, dict):
        raise ValueError("Card origin must be a mapping")
    unexpected_origin_keys = set(card.origin).difference(_ORIGIN_KEYS)
    if unexpected_origin_keys:
        raise ValueError("Card origin contains unsupported fields")
    if any(
        not isinstance(value, str) or not value.strip()
        for value in card.origin.values()
    ):
        raise ValueError("Card origin values must be non-empty strings")
    if "@latest" in card.object_ref:
        raise ValueError("historical Card cannot use latest")


def card_payload(card: ObjectCard) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": card.schema_version,
        "object_ref": card.object_ref,
        "object_id": card.object_id,
        "version": card.version,
        "type": card.object_type.value,
        "name": card.name,
        "contains": card.contains,
    }
    if card.summary:
        payload["summary"] = card.summary
    if card.origin:
        payload["origin"] = card.origin
    if card.language:
        payload["language"] = card.language
    if card.metadata:
        payload["metadata"] = card.metadata
    if card.supersedes:
        payload["relations"] = {"supersedes": card.supersedes}
    if card.derived_from:
        payload.setdefault("relations", {})["derived_from"] = list(card.derived_from)
    return payload


def render_card(card: ObjectCard) -> str:
    validate_card(card)
    return f"{CARD_OPEN}\n{canonical_json(card_payload(card))}\n{CARD_CLOSE}"


def render_retrieval_card(
    *,
    object_ref: str,
    status: str = "success",
) -> str:
    if ObjectContextStore.parse_object_ref(object_ref) is None:
        raise ValueError("malformed retrieval object_ref")
    payload = {
        "schema_version": RETRIEVAL_CARD_SCHEMA_VERSION,
        "object_ref": object_ref,
        "action": "retrieved",
        "status": str(status or "unknown"),
    }
    return f"{RETRIEVAL_CARD_OPEN}\n{canonical_json(payload)}\n{RETRIEVAL_CARD_CLOSE}"


def parse_card_text(text: str) -> dict[str, Any] | None:
    stripped = str(text or "").strip()
    for opening, closing in (
        (CARD_OPEN, CARD_CLOSE),
        (RETRIEVAL_CARD_OPEN, RETRIEVAL_CARD_CLOSE),
    ):
        if stripped.startswith(opening) and stripped.endswith(closing):
            body = stripped[len(opening) : -len(closing)].strip()
            try:
                parsed = json.loads(body)
            except (TypeError, ValueError):
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def benefit_gate(
    raw_content: str,
    rendered_card: str,
    *,
    min_absolute_saving_tokens: int,
    min_relative_saving_ratio: float,
) -> tuple[bool, int, int]:
    raw_tokens = estimate_tokens_rough(raw_content)
    card_tokens = estimate_tokens_rough(rendered_card)
    saving = raw_tokens - card_tokens
    relative = saving / raw_tokens if raw_tokens > 0 else 0.0
    return (
        saving >= max(0, int(min_absolute_saving_tokens))
        and relative >= max(0.0, float(min_relative_saving_ratio)),
        raw_tokens,
        card_tokens,
    )
