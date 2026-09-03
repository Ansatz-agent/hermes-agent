"""Request-only Raw/Card and retrieval projection for Object Context V1."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable, Sequence

from .detection import message_key


def _part_text_and_key(content: Any, ordinal: int) -> tuple[str, str] | None:
    if isinstance(content, str):
        return (content, "") if ordinal == 0 else None
    if not isinstance(content, list) or ordinal < 0 or ordinal >= len(content):
        return None
    part = content[ordinal]
    if isinstance(part, str):
        return part, ""
    if not isinstance(part, dict):
        return None
    for key in ("text", "content", "file_content", "data"):
        value = part.get(key)
        if isinstance(value, str):
            return value, key
    return None


def _set_part_text(content: Any, ordinal: int, key: str, text: str) -> Any:
    if isinstance(content, str):
        if ordinal != 0:
            raise ValueError("string content has only part ordinal zero")
        return text
    if not isinstance(content, list) or ordinal < 0 or ordinal >= len(content):
        raise ValueError("content part ordinal is out of bounds")
    selected = list(content)
    part = selected[ordinal]
    if isinstance(part, str):
        selected[ordinal] = text
    elif isinstance(part, dict) and key:
        replacement = dict(part)
        replacement[key] = text
        selected[ordinal] = replacement
    else:
        raise ValueError("content part cannot accept text replacement")
    return selected


def apply_occurrence_cards(
    messages: Sequence[dict[str, Any]],
    occurrences: Iterable[dict[str, Any]],
    *,
    allowed_refs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Replace exact registered spans while preserving every other byte."""

    rendered = copy.deepcopy(list(messages))
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for occurrence in occurrences:
        object_ref = str(occurrence.get("object_ref") or "")
        card_text = occurrence.get("card_text")
        if allowed_refs is not None and object_ref not in allowed_refs:
            continue
        if not isinstance(card_text, str) or not card_text:
            continue
        grouped[
            (
                int(occurrence.get("message_ordinal") or 0),
                int(occurrence.get("part_ordinal") or 0),
            )
        ].append(occurrence)

    for (message_ordinal, part_ordinal), rows in grouped.items():
        if message_ordinal < 0 or message_ordinal >= len(rendered):
            raise ValueError("object occurrence message ordinal is out of bounds")
        message = rendered[message_ordinal]
        resolved = _part_text_and_key(message.get("content"), part_ordinal)
        if resolved is None:
            raise ValueError("object occurrence source part no longer exists")
        source, part_key = resolved
        replacements: list[tuple[int, int, str]] = []
        prior_end = -1
        for row in sorted(rows, key=lambda item: int(item.get("span_start") or 0)):
            start = int(row.get("span_start") or 0)
            end = int(row.get("span_end") or 0)
            if start < 0 or end <= start or end > len(source):
                raise ValueError("object occurrence span is invalid")
            if start < prior_end:
                raise ValueError("object occurrence spans overlap")
            prior_end = end
            replacements.append((start, end, str(row["card_text"])))
        updated = source
        for start, end, card_text in reversed(replacements):
            updated = updated[:start] + card_text + updated[end:]
        message["content"] = _set_part_text(
            message.get("content"), part_ordinal, part_key, updated
        )
    return rendered


def project_compressed_messages(
    messages: Sequence[dict[str, Any]],
    occurrences_by_message: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Project stored compressed occurrences onto a live request snapshot."""

    rendered = copy.deepcopy(list(messages))
    for ordinal, message in enumerate(list(rendered)):
        if not isinstance(message, dict):
            continue
        rows = occurrences_by_message.get(message_key(message), [])
        if not rows:
            continue
        local_rows = []
        for row in rows:
            local = dict(row)
            local["message_ordinal"] = 0
            local_rows.append(local)
        rendered[ordinal] = apply_occurrence_cards([message], local_rows)[0]
    return rendered
