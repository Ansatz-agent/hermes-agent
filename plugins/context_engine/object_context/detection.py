"""Deterministic segmentation and conservative object detection for V1."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterable

import yaml

from agent.context_engine import ContextDelta
from agent.model_metadata import estimate_tokens_rough

from .fenced_code import fenced_code_blocks
from .models import DetectedObject, ObjectType
from .store import canonical_json, exact_sha256


RETRIEVE_OBJECT_TOOL_NAME = "retrieve_object"
_FILE_TOOLS = frozenset({
    "read_file",
    "read_text_file",
    "read_mcp_resource",
    "view_file",
    "load_file",
})
_ARTIFACT_TOOL_MARKERS = ("artifact", "document", "spreadsheet", "presentation")
_EXECUTION_TOOLS = frozenset({
    "terminal",
    "execute_code",
    "python",
    "shell",
    "bash",
    "exec_command",
})
_TRACEBACK_RE = re.compile(
    r"(?m)^(?:Traceback \(most recent call last\):|"
    r"(?:[A-Za-z_][\w.]*Error|Exception):\s|"
    r"Error: .+\n\s+at\s)"
)
_LOG_MARKER_RE = re.compile(
    r"(?im)(?:^|\s)(?:DEBUG|INFO|WARN(?:ING)?|ERROR|FATAL|CRITICAL)"
    r"(?:\s|:|\])|\b(?:epoch|step|checkpoint|build|test|passed|failed)\b"
)
_MARKDOWN_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_HTML_TABLE_RE = re.compile(r"(?is)<table\b[^>]*>.*?</table\s*>")
_CODE_SUFFIX_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".sh": "bash",
    ".bash": "bash",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".tex": "latex",
}


@dataclass(frozen=True)
class TextPart:
    ordinal: int
    text: str
    metadata: dict[str, Any]
    explicit_file: bool = False


def iter_text_parts(content: Any) -> Iterable[TextPart]:
    """Yield text-bearing content parts without flattening multimodal input."""

    if isinstance(content, str):
        yield TextPart(ordinal=0, text=content, metadata={})
        return
    if not isinstance(content, list):
        return
    for ordinal, part in enumerate(content):
        if isinstance(part, str):
            yield TextPart(ordinal=ordinal, text=part, metadata={})
            continue
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "")
        text = part.get("text")
        if not isinstance(text, str):
            for key in ("content", "file_content", "data"):
                value = part.get(key)
                if isinstance(value, str):
                    text = value
                    break
        if not isinstance(text, str):
            continue
        explicit_file = part_type in {
            "file",
            "input_file",
            "file_content",
            "document",
        }
        yield TextPart(
            ordinal=ordinal,
            text=text,
            metadata={
                key: part[key]
                for key in ("type", "name", "filename", "mime_type")
                if key in part
            },
            explicit_file=explicit_file,
        )


def message_key(message: dict[str, Any]) -> str:
    """Stable identity derived from durable message fields.

    Timestamps survive SessionDB projection and compression-tail copying. The
    exact content digest prevents a coincidental timestamp collision from
    binding a Card to different bytes.
    """

    identity = {
        "role": message.get("role"),
        "timestamp": message.get("timestamp"),
        "content": message.get("content"),
        "tool_call_id": message.get("tool_call_id"),
        "tool_name": message.get("tool_name") or message.get("name"),
        "tool_calls": message.get("tool_calls"),
    }
    digest = hashlib.sha256(
        canonical_json(identity).encode("utf-8", errors="surrogatepass")
    ).hexdigest()
    return f"msg_{digest}"


def occurrence_key(
    *,
    conversation_id: str,
    delta_id: str,
    message_identity: str,
    part_ordinal: int,
    start: int,
    end: int,
    content: str,
) -> str:
    identity = (
        f"{conversation_id}\0{delta_id}\0{message_identity}\0"
        f"{part_ordinal}\0{start}\0{end}\0{exact_sha256(content)}"
    )
    return (
        "occ_"
        + hashlib.sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest()
    )


def _structured_format(text: str) -> tuple[str, dict[str, Any]] | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (dict, list)):
            return "json", {"parsed_type": type(parsed).__name__}
    except (ValueError, TypeError):
        pass
    if stripped.startswith("<") and stripped.endswith(">"):
        try:
            root = ET.fromstring(stripped)
            return "xml", {"root_tag": root.tag}
        except ET.ParseError:
            pass
    try:
        parsed_yaml = yaml.safe_load(stripped)
        yaml_mapping = (
            isinstance(parsed_yaml, dict)
            and len(parsed_yaml) >= 2
            and sum(1 for line in stripped.splitlines() if ":" in line) >= 2
        )
        yaml_record_list = (
            isinstance(parsed_yaml, list)
            and bool(parsed_yaml)
            and all(isinstance(item, dict) for item in parsed_yaml[:20])
        )
        if (yaml_mapping or yaml_record_list) and "\n" in stripped:
            return "yaml", {"parsed_type": type(parsed_yaml).__name__}
    except yaml.YAMLError:
        pass
    try:
        sample = stripped[:8192]
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;")
        rows = list(csv.reader(io.StringIO(stripped), dialect=dialect))
        source_lines = [line for line in stripped.splitlines() if line.strip()]
        if (
            len(rows) >= 2
            and len(rows[0]) >= 2
            and len(source_lines) >= 2
            and all(dialect.delimiter in line for line in source_lines[:20])
        ):
            width = len(rows[0])
            if all(len(row) == width for row in rows[:100]):
                return "csv", {
                    "delimiter": dialect.delimiter,
                    "row_count": len(rows),
                    "column_count": width,
                }
    except (csv.Error, UnicodeError):
        pass
    return None


def _error_trace_spans(text: str) -> list[tuple[int, int]]:
    """Return complete Python/JS trace regions without absorbing prose."""

    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].lstrip()
        if stripped.startswith("Traceback (most recent call last):"):
            start_line = index
            index += 1
            last_trace_line = index
            while index < len(lines):
                line = lines[index]
                if (
                    re.match(r'^\s*File "[^"]+", line \d+', line)
                    or line.startswith((" ", "\t"))
                    or not line.strip()
                    or re.match(r"^[A-Za-z_][\w.]*?(?:Error|Exception):", line)
                ):
                    last_trace_line = index + 1
                    index += 1
                    continue
                break
            end = (
                offsets[last_trace_line] if last_trace_line < len(lines) else len(text)
            )
            spans.append((offsets[start_line], end))
            continue
        if re.match(r"^(?:[A-Za-z_$][\w.$]*Error|Error):\s", stripped):
            start_line = index
            index += 1
            while index < len(lines) and re.match(r"^\s+at\s", lines[index]):
                index += 1
            if index > start_line + 1:
                end = offsets[index] if index < len(lines) else len(text)
                spans.append((offsets[start_line], end))
                continue
        index += 1
    return spans


def _markdown_table_spans(text: str) -> list[tuple[int, int]]:
    lines = text.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    spans: list[tuple[int, int]] = []
    index = 1
    while index < len(lines):
        if not _MARKDOWN_TABLE_SEPARATOR_RE.fullmatch(lines[index].rstrip("\r\n")):
            index += 1
            continue
        start_line = index - 1
        end_line = index + 1
        while end_line < len(lines) and "|" in lines[end_line]:
            if not lines[end_line].strip():
                break
            end_line += 1
        if end_line - start_line >= 2:
            start = offsets[start_line]
            end = offsets[end_line] if end_line < len(lines) else len(text)
            spans.append((start, end))
        index = max(index + 1, end_line)
    return spans


def _classify_complete_payload(
    text: str,
    *,
    role: str,
    tool_name: str,
    explicit_file: bool,
    part_metadata: dict[str, Any] | None = None,
) -> tuple[ObjectType, str, dict[str, Any]] | None:
    stripped = text.strip()
    if not stripped:
        return None
    metadata = part_metadata or {}
    part_type = str(metadata.get("type") or "").lower()
    filename = str(metadata.get("filename") or metadata.get("name") or "")
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if explicit_file and suffix in _CODE_SUFFIX_LANGUAGES:
        return (
            ObjectType.CODE,
            "runtime:code_file",
            {"language": _CODE_SUFFIX_LANGUAGES[suffix]},
        )
    if part_type in {"code", "source_code"}:
        return (
            ObjectType.CODE,
            "runtime:code",
            {"language": str(metadata.get("language") or "")},
        )
    if part_type in {"table", "data_table"}:
        return (
            ObjectType.TABLE,
            "runtime:table",
            {"format": str(metadata.get("format") or "runtime")},
        )
    if part_type in {"artifact", "output_artifact"}:
        return ObjectType.ARTIFACT, "runtime:artifact", {}
    if explicit_file or tool_name in _FILE_TOOLS:
        return ObjectType.FILE_CONTENT, "runtime:file_content", {}
    if any(marker in tool_name.lower() for marker in _ARTIFACT_TOOL_MARKERS):
        return ObjectType.ARTIFACT, "runtime:artifact", {}
    trace = _TRACEBACK_RE.search(stripped)
    if trace is not None:
        return ObjectType.ERROR_TRACE, "parser:error_trace", {}
    structured = _structured_format(stripped)
    if structured is not None:
        fmt, metadata = structured
        return (
            ObjectType.STRUCTURED_DATA,
            f"parser:{fmt}",
            {"format": fmt, **metadata},
        )
    if _HTML_TABLE_RE.fullmatch(stripped):
        return ObjectType.TABLE, "parser:html_table", {"format": "html"}
    if role == "tool":
        lines = stripped.splitlines()
        markers = len(_LOG_MARKER_RE.findall(stripped))
        if tool_name in _EXECUTION_TOOLS and len(lines) >= 8 and markers >= 1:
            return ObjectType.LOG, "runtime:execution_log", {}
        if len(lines) >= 8 and markers >= 2:
            return ObjectType.LOG, "heuristic:log_markers", {}
        return ObjectType.TOOL_RESULT, "runtime:tool_result", {}
    return None


def _overlaps(span: tuple[int, int], occupied: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(
        start < other_end and end > other_start for other_start, other_end in occupied
    )


def detect_delta_objects(
    delta: ContextDelta,
    *,
    min_tokens: int,
    retrieval_tool_name: str = RETRIEVE_OBJECT_TOOL_NAME,
) -> list[DetectedObject]:
    """Detect exact structured-object spans in one committed Delta."""

    detected: list[DetectedObject] = []
    for message_ordinal, message in enumerate(delta.messages):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            continue
        try:
            from agent.context_compressor import (
                ContextCompressor,
                is_compaction_summary_message,
            )

            if is_compaction_summary_message(message) or (
                role == "user"
                and ContextCompressor._is_synthetic_compression_user_turn(message)
            ):
                continue
        except Exception:
            pass
        if any(
            message.get(marker)
            for marker in (
                "_thinking_prefill",
                "_empty_recovery_synthetic",
                "_empty_terminal_sentinel",
                "_dropped_toolcall_nudge",
                "_verification_stop_synthetic",
                "_pre_verify_synthetic",
                "_kanban_stop_synthetic",
            )
        ):
            continue
        tool_name = str(message.get("tool_name") or message.get("name") or "")
        if role == "tool" and tool_name == retrieval_tool_name:
            continue
        tool_call_id = str(message.get("tool_call_id") or "")
        msg_key = message_key(message)

        for part in iter_text_parts(message.get("content")):
            text = part.text
            if not text:
                continue
            if "<OBJECT_CARD>" in text or "<RETRIEVAL_CARD>" in text:
                continue
            occupied: list[tuple[int, int]] = []

            # Runtime metadata owns a complete tool/file/artifact boundary. It
            # takes precedence over nested parser candidates.
            complete = _classify_complete_payload(
                text,
                role=role,
                tool_name=tool_name,
                explicit_file=part.explicit_file,
                part_metadata=part.metadata,
            )
            if complete is not None and (role == "tool" or part.explicit_file):
                object_type, method, metadata = complete
                if estimate_tokens_rough(text) >= min_tokens:
                    key = occurrence_key(
                        conversation_id=delta.conversation_id,
                        delta_id=delta.delta_id,
                        message_identity=msg_key,
                        part_ordinal=part.ordinal,
                        start=0,
                        end=len(text),
                        content=text,
                    )
                    detected.append(
                        DetectedObject(
                            object_type=object_type,
                            content=text,
                            message_ordinal=message_ordinal,
                            part_ordinal=part.ordinal,
                            start=0,
                            end=len(text),
                            whole_part=True,
                            detection_method=method,
                            source_role=role,
                            message_key=msg_key,
                            occurrence_key=key,
                            name=str(
                                part.metadata.get("filename")
                                or part.metadata.get("name")
                                or ""
                            ),
                            language=str(metadata.get("language") or ""),
                            tool_name=tool_name,
                            tool_call_id=tool_call_id,
                            metadata={**part.metadata, **metadata},
                        )
                    )
                continue

            for block in fenced_code_blocks(text):
                if estimate_tokens_rough(block.code) < min_tokens:
                    continue
                span = (block.start_offset, block.end_offset)
                occupied.append(span)
                key = occurrence_key(
                    conversation_id=delta.conversation_id,
                    delta_id=delta.delta_id,
                    message_identity=msg_key,
                    part_ordinal=part.ordinal,
                    start=span[0],
                    end=span[1],
                    content=block.code,
                )
                detected.append(
                    DetectedObject(
                        object_type=ObjectType.CODE,
                        content=block.code,
                        message_ordinal=message_ordinal,
                        part_ordinal=part.ordinal,
                        start=span[0],
                        end=span[1],
                        whole_part=False,
                        detection_method="parser:markdown_fence",
                        source_role=role,
                        message_key=msg_key,
                        occurrence_key=key,
                        language=block.language,
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        metadata={"info_string": block.info_string},
                    )
                )

            for start, end in _markdown_table_spans(text):
                if _overlaps((start, end), occupied):
                    continue
                content = text[start:end]
                if estimate_tokens_rough(content) < min_tokens:
                    continue
                occupied.append((start, end))
                detected.append(
                    DetectedObject(
                        object_type=ObjectType.TABLE,
                        content=content,
                        message_ordinal=message_ordinal,
                        part_ordinal=part.ordinal,
                        start=start,
                        end=end,
                        whole_part=False,
                        detection_method="parser:markdown_table",
                        source_role=role,
                        message_key=msg_key,
                        occurrence_key=occurrence_key(
                            conversation_id=delta.conversation_id,
                            delta_id=delta.delta_id,
                            message_identity=msg_key,
                            part_ordinal=part.ordinal,
                            start=start,
                            end=end,
                            content=content,
                        ),
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        metadata={"format": "markdown"},
                    )
                )

            for match in _HTML_TABLE_RE.finditer(text):
                span = match.span()
                if _overlaps(span, occupied):
                    continue
                content = match.group(0)
                if estimate_tokens_rough(content) < min_tokens:
                    continue
                occupied.append(span)
                detected.append(
                    DetectedObject(
                        object_type=ObjectType.TABLE,
                        content=content,
                        message_ordinal=message_ordinal,
                        part_ordinal=part.ordinal,
                        start=span[0],
                        end=span[1],
                        whole_part=False,
                        detection_method="parser:html_table",
                        source_role=role,
                        message_key=msg_key,
                        occurrence_key=occurrence_key(
                            conversation_id=delta.conversation_id,
                            delta_id=delta.delta_id,
                            message_identity=msg_key,
                            part_ordinal=part.ordinal,
                            start=span[0],
                            end=span[1],
                            content=content,
                        ),
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                        metadata={"format": "html"},
                    )
                )

            for start, end in _error_trace_spans(text):
                if _overlaps((start, end), occupied):
                    continue
                content = text[start:end]
                if estimate_tokens_rough(content) < min_tokens:
                    continue
                occupied.append((start, end))
                detected.append(
                    DetectedObject(
                        object_type=ObjectType.ERROR_TRACE,
                        content=content,
                        message_ordinal=message_ordinal,
                        part_ordinal=part.ordinal,
                        start=start,
                        end=end,
                        whole_part=(start == 0 and end == len(text)),
                        detection_method="parser:error_trace",
                        source_role=role,
                        message_key=msg_key,
                        occurrence_key=occurrence_key(
                            conversation_id=delta.conversation_id,
                            delta_id=delta.delta_id,
                            message_identity=msg_key,
                            part_ordinal=part.ordinal,
                            start=start,
                            end=end,
                            content=content,
                        ),
                        tool_name=tool_name,
                        tool_call_id=tool_call_id,
                    )
                )

            if occupied:
                continue
            complete = _classify_complete_payload(
                text,
                role=role,
                tool_name=tool_name,
                explicit_file=part.explicit_file,
                part_metadata=part.metadata,
            )
            if complete is None or estimate_tokens_rough(text) < min_tokens:
                continue
            object_type, method, metadata = complete
            detected.append(
                DetectedObject(
                    object_type=object_type,
                    content=text,
                    message_ordinal=message_ordinal,
                    part_ordinal=part.ordinal,
                    start=0,
                    end=len(text),
                    whole_part=True,
                    detection_method=method,
                    source_role=role,
                    message_key=msg_key,
                    occurrence_key=occurrence_key(
                        conversation_id=delta.conversation_id,
                        delta_id=delta.delta_id,
                        message_identity=msg_key,
                        part_ordinal=part.ordinal,
                        start=0,
                        end=len(text),
                        content=text,
                    ),
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    language=str(metadata.get("language") or ""),
                    metadata={**part.metadata, **metadata},
                )
            )
    return detected
