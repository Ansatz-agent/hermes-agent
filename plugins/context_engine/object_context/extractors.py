"""Deterministic structural indexes and bounded summary fallbacks for V1."""

from __future__ import annotations

import ast
import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Any

import yaml

from .models import ObjectRecord, ObjectType


def _bounded_unique(values: list[str], limit: int = 100) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))[:limit]


def _python_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    try:
        args = ast.unparse(node.args)
    except Exception:
        args = "..."
    prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({args})"


def _code_structure(record: ObjectRecord) -> dict[str, Any]:
    language = record.language.lower().strip()
    result: dict[str, Any] = {"language": language or "unknown"}
    if language in {"py", "python", "python3"}:
        try:
            tree = ast.parse(record.content)
        except (SyntaxError, ValueError, TypeError):
            return result
        classes: list[str] = []
        functions: list[str] = []
        variables: list[str] = []
        imports: list[str] = []
        entry_points: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(_python_signature(node))
                if node.name == "main":
                    entry_points.append("main()")
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                else:
                    module = node.module or ""
                    imports.append(module)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    if isinstance(target, ast.Name):
                        variables.append(target.id)
            elif isinstance(node, ast.If):
                try:
                    if "__name__" in ast.unparse(
                        node.test
                    ) and "__main__" in ast.unparse(node.test):
                        entry_points.append("__main__")
                except Exception:
                    pass
        result.update({
            "classes": _bounded_unique(classes),
            "functions": _bounded_unique(functions),
            "module_variables": _bounded_unique(variables),
            "imports": _bounded_unique(imports),
            "entry_points": _bounded_unique(entry_points),
        })
        return result

    result["classes"] = _bounded_unique(
        re.findall(r"(?m)^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", record.content)
    )
    result["functions"] = _bounded_unique(
        re.findall(
            r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+"
            r"([A-Za-z_$][\w$]*\s*\([^)]*\))",
            record.content,
        )
    )
    result["imports"] = _bounded_unique(
        re.findall(
            r"(?m)^\s*(?:import\s+.*?\s+from\s+|require\s*\()"
            r"['\"]([^'\"]+)",
            record.content,
        )
    )
    return result


def _structured_structure(record: ObjectRecord) -> dict[str, Any]:
    fmt = str(record.metadata.get("format") or "")
    text = record.content.strip()
    parsed: Any = None
    try:
        if fmt == "json":
            parsed = json.loads(text)
        elif fmt == "yaml":
            parsed = yaml.safe_load(text)
        elif fmt == "xml":
            root = ET.fromstring(text)
            return {
                "format": "xml",
                "root_tag": root.tag,
                "child_tags": _bounded_unique([child.tag for child in root]),
                "element_count": sum(1 for _ in root.iter()),
            }
        elif fmt == "csv":
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
            rows = list(csv.reader(io.StringIO(text), dialect=dialect))
            return {
                "format": "csv",
                "columns": rows[0] if rows else [],
                "row_count": max(0, len(rows) - 1),
                "column_count": len(rows[0]) if rows else 0,
            }
    except Exception:
        return {"format": fmt or "unknown"}
    result: dict[str, Any] = {"format": fmt or "unknown"}
    if isinstance(parsed, dict):
        result["top_level_keys"] = [str(key) for key in list(parsed)[:100]]
        result["shape"] = {"type": "object", "key_count": len(parsed)}
        result["data_types"] = {
            str(key): type(value).__name__ for key, value in list(parsed.items())[:100]
        }
        result["schema"] = dict(result["data_types"])
    elif isinstance(parsed, list):
        result["shape"] = {"type": "array", "length": len(parsed)}
        if parsed and isinstance(parsed[0], dict):
            result["columns"] = [str(key) for key in list(parsed[0])[:100]]
    return result


def _markdown_table_structure(text: str) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return {"format": "markdown", "row_count": 0, "column_count": 0}
    header = [cell.strip() for cell in lines[0].strip("|").split("|")]
    return {
        "format": "markdown",
        "columns": header,
        "header": header,
        "row_count": max(0, len(lines) - 2),
        "column_count": len(header),
    }


class _HTMLTableIndex(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows = 0
        self.current: list[str] = []
        self.headers: list[str] = []
        self._capture = False
        self._buffer: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self.rows += 1
        if tag in {"th", "td"}:
            self._capture = True
            self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"th", "td"} and self._capture:
            value = "".join(self._buffer).strip()
            if self.rows == 1:
                self.headers.append(value)
            self._capture = False


def _table_structure(record: ObjectRecord) -> dict[str, Any]:
    fmt = str(record.metadata.get("format") or "markdown")
    if fmt != "html":
        return _markdown_table_structure(record.content)
    parser = _HTMLTableIndex()
    try:
        parser.feed(record.content)
    except Exception:
        return {"format": "html"}
    return {
        "format": "html",
        "columns": parser.headers,
        "header": parser.headers,
        "row_count": max(0, parser.rows - (1 if parser.headers else 0)),
        "column_count": len(parser.headers),
    }


def _error_structure(text: str) -> dict[str, Any]:
    exception = re.search(r"(?m)^([A-Za-z_][\w.]*?(?:Error|Exception)):\s*(.*)$", text)
    frames = re.findall(r'(?m)^\s*File "([^"]+)", line (\d+)', text)
    js_frames = re.findall(r"(?m)^\s*at\s+.*?\(?([^():]+):(\d+)(?::\d+)?\)?$", text)
    all_frames = frames + js_frames
    result: dict[str, Any] = {
        "exception_type": exception.group(1) if exception else "",
        "exception_message": exception.group(2) if exception else "",
        "stack_depth": len(all_frames),
        "files": _bounded_unique([frame[0] for frame in all_frames]),
        "line_numbers": [int(frame[1]) for frame in all_frames[:100]],
    }
    if all_frames:
        result["top_frame"] = {
            "file": all_frames[-1][0],
            "line": int(all_frames[-1][1]),
        }
    return result


def _log_structure(text: str) -> dict[str, Any]:
    upper = text.upper()
    statuses = []
    for marker in ("SUCCESS", "PASSED", "FAILED", "ERROR", "CANCELLED"):
        if marker in upper:
            statuses.append(marker.lower())
    stages = re.findall(r"(?im)(?:stage|phase)\s*[:=]\s*([\w .-]+)", text)
    metrics = re.findall(
        r"(?im)\b([A-Za-z_][\w.-]{1,40})\s*[:=]\s*(-?\d+(?:\.\d+)?)", text
    )
    checkpoints = re.findall(r"(?im)^.*checkpoint.*$", text)
    start_markers = re.findall(
        r"(?im)^.*\b(?:start(?:ed|ing)?|begin|launched)\b.*$", text
    )
    end_markers = re.findall(
        r"(?im)^.*\b(?:end(?:ed|ing)?|complete(?:d)?|finished)\b.*$", text
    )
    warnings = len(re.findall(r"(?im)\bWARN(?:ING)?\b", text))
    errors = len(re.findall(r"(?im)\b(?:ERROR|FATAL|CRITICAL)\b", text))
    return {
        "run_status": statuses[-1] if statuses else "unknown",
        "stage_names": _bounded_unique([stage.strip() for stage in stages]),
        "metric_names": _bounded_unique([name for name, _ in metrics]),
        "checkpoint_events": checkpoints[:50],
        "start_markers": start_markers[:50],
        "end_markers": end_markers[:50],
        "warning_count": warnings,
        "error_count": errors,
        "line_count": len(text.splitlines()),
    }


def extract_structure(record: ObjectRecord) -> dict[str, Any]:
    if record.object_type == ObjectType.CODE:
        return _code_structure(record)
    if record.object_type == ObjectType.STRUCTURED_DATA:
        return _structured_structure(record)
    if record.object_type == ObjectType.TABLE:
        return _table_structure(record)
    if record.object_type == ObjectType.ERROR_TRACE:
        return _error_structure(record.content)
    if record.object_type == ObjectType.LOG:
        return _log_structure(record.content)
    return {
        "line_count": len(record.content.splitlines()),
        "char_count": len(record.content),
    }


def deterministic_summary(record: ObjectRecord, contains: dict[str, Any]) -> str:
    """Fail-safe semantic stub used when bounded LLM summary is unavailable."""

    name = f" named {record.name}" if record.name else ""
    if record.object_type == ObjectType.CODE:
        language = record.language or str(contains.get("language") or "unknown")
        display_language = {
            "python": "Python",
            "javascript": "JavaScript",
            "typescript": "TypeScript",
            "cpp": "C++",
            "bash": "Bash",
            "latex": "LaTeX",
        }.get(language.lower(), language.capitalize())
        return f"{display_language} code object{name}."
    if record.object_type == ObjectType.FILE_CONTENT:
        return f"Exact file content object{name}."
    if record.object_type == ObjectType.ERROR_TRACE:
        error_type = str(contains.get("exception_type") or "error")
        return f"Exact {error_type} trace{name}."
    if record.object_type == ObjectType.LOG:
        status = str(contains.get("run_status") or "unknown")
        return f"Execution log{name}; detected status: {status}."
    if record.object_type == ObjectType.STRUCTURED_DATA:
        fmt = str(contains.get("format") or "structured")
        return f"Exact {fmt} structured-data object{name}."
    if record.object_type == ObjectType.TABLE:
        return f"Exact table object{name}."
    if record.object_type == ObjectType.ARTIFACT:
        return f"Exact generated artifact{name}."
    return f"Exact tool-result object{name}."
