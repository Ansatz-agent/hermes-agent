"""Deterministic Markdown fenced-code parsing for Object Context V1.

The parser deliberately implements only the small CommonMark-compatible
surface V1 needs: complete backtick or tilde fences indented by at most
three spaces.  It never guesses whether ordinary prose is code, and it leaves
unclosed fences untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator


_OPENING_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<fence>`{3,}|~{3,})(?P<info>.*)$"
)


@dataclass(frozen=True)
class FencedCodeBlock:
    """One complete fenced block and its exact source offsets."""

    start_offset: int
    end_offset: int
    code_start_offset: int
    code_end_offset: int
    fence_char: str
    fence_length: int
    info_string: str
    language: str
    code: str
    block_index: int


def _line_body(line: str) -> str:
    """Return *line* without one trailing line-ending sequence."""

    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith(("\n", "\r")):
        return line[:-1]
    return line


def _opening_fence(line: str) -> tuple[str, int, str] | None:
    match = _OPENING_FENCE_RE.fullmatch(_line_body(line))
    if match is None:
        return None
    fence = match.group("fence")
    info = match.group("info")
    # CommonMark forbids a backtick in the info string of a backtick fence.
    if fence[0] == "`" and "`" in info:
        return None
    return fence[0], len(fence), info.strip()


def _is_closing_fence(line: str, fence_char: str, opening_length: int) -> bool:
    body = _line_body(line)
    pattern = rf"^ {{0,3}}{re.escape(fence_char)}{{{opening_length},}}[ \t]*$"
    return re.fullmatch(pattern, body) is not None


def _language_from_info(info_string: str) -> str:
    if not info_string:
        return ""
    return info_string.split(None, 1)[0]


def iter_fenced_code_blocks(text: str) -> Iterator[FencedCodeBlock]:
    """Yield complete fenced code blocks from *text* in source order.

    Offsets cover the full opening/body/closing fence, while ``code`` contains
    the exact body between the fence lines, including its original line
    endings.  An opening fence without a matching close is ignored so callers
    can safely preserve the source unchanged.
    """

    if not isinstance(text, str) or not text:
        return

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    block_index = 0
    line_index = 0
    while line_index < len(lines):
        opening = _opening_fence(lines[line_index])
        if opening is None:
            line_index += 1
            continue

        fence_char, fence_length, info_string = opening
        closing_index = line_index + 1
        while closing_index < len(lines):
            if _is_closing_fence(lines[closing_index], fence_char, fence_length):
                break
            closing_index += 1

        if closing_index >= len(lines):
            # Do not reinterpret nested-looking lines after an unclosed fence.
            # The entire remainder is part of the unfinished block in Markdown.
            break

        start_offset = offsets[line_index]
        code_start = start_offset + len(lines[line_index])
        code_end = offsets[closing_index]
        end_offset = code_end + len(lines[closing_index])
        code = text[code_start:code_end]
        yield FencedCodeBlock(
            start_offset=start_offset,
            end_offset=end_offset,
            code_start_offset=code_start,
            code_end_offset=code_end,
            fence_char=fence_char,
            fence_length=fence_length,
            info_string=info_string,
            language=_language_from_info(info_string),
            code=code,
            block_index=block_index,
        )
        block_index += 1
        line_index = closing_index + 1


def fenced_code_blocks(text: str) -> list[FencedCodeBlock]:
    """Return all complete fenced blocks in *text*."""

    return list(iter_fenced_code_blocks(text))
