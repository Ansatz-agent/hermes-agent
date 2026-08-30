"""Deterministic graders for syntax-sensitive workflow preferences."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

try:
    from .dataset import AssertionSpec, EvalCase
except ImportError:  # Direct ``python evals/preference_memory/runner.py``.
    from dataset import AssertionSpec, EvalCase


_FENCE_RE = re.compile(
    r"\A\s*```[^\n]*\n(?P<body>[\s\S]*?)\n```\s*\Z",
    re.MULTILINE,
)
_ANY_FENCE_RE = re.compile(r"```")
_MARKDOWN_TABLE_RE = re.compile(r"(?m)^\s*\|.+\|\s*$\n^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$")


@dataclass(frozen=True)
class AssertionResult:
    assertion_id: str
    kind: str
    passed: bool
    weight: float
    description: str
    metric: str
    diagnostic: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.assertion_id,
            "kind": self.kind,
            "passed": self.passed,
            "weight": self.weight,
            "description": self.description,
            "metric": self.metric,
            "diagnostic": self.diagnostic,
        }


def unwrap_single_code_fence(text: str) -> tuple[str, bool]:
    match = _FENCE_RE.fullmatch(text or "")
    if not match:
        return text or "", False
    return match.group("body"), True


def _list_param(spec: AssertionSpec, key: str = "values") -> list[str]:
    value = spec.params.get(key)
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValueError(
            f"assertion {spec.assertion_id!r} requires non-empty string list {key!r}"
        )
    return value


def _string_param(spec: AssertionSpec, key: str) -> str:
    value = spec.params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"assertion {spec.assertion_id!r} requires string {key!r}")
    return value


def _contains_all(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    values = _list_param(spec)
    missing = [value for value in values if value not in text]
    return (
        not missing,
        "missing: " + repr(missing) if missing else "all required strings present",
    )


def _contains_any(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    values = _list_param(spec)
    present = [value for value in values if value in text]
    return (
        bool(present),
        "matched: " + repr(present) if present else "none of the alternatives appeared",
    )


def _not_contains_any(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    values = _list_param(spec)
    present = [value for value in values if value in text]
    return (
        not present,
        "forbidden strings present: " + repr(present)
        if present
        else "no forbidden strings present",
    )


def _regex(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    pattern = _string_param(spec, "pattern")
    matched = bool(re.search(pattern, text, re.MULTILINE | re.DOTALL))
    return matched, f"pattern {'matched' if matched else 'did not match'}: {pattern}"


def _not_regex(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    pattern = _string_param(spec, "pattern")
    matched = bool(re.search(pattern, text, re.MULTILINE | re.DOTALL))
    return (
        not matched,
        f"forbidden pattern {'matched' if matched else 'did not match'}: {pattern}",
    )


def _ordered_contains(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    values = _list_param(spec)
    cursor = 0
    for value in values:
        position = text.find(value, cursor)
        if position < 0:
            return False, f"{value!r} was absent or out of order"
        cursor = position + len(value)
    return True, "required strings appeared in order"


def _source_only(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    del spec
    _, fenced = unwrap_single_code_fence(text)
    if fenced:
        return True, "single code fence with no surrounding prose"
    if _ANY_FENCE_RE.search(text):
        return False, "partial or multiple code fences leave surrounding prose"
    return (
        bool(text.strip()),
        "raw source accepted" if text.strip() else "response is empty",
    )


def _latex_prose_single_line(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    del spec
    source, _ = unwrap_single_code_fence(text)
    environment_depth = 0
    prose_run: list[tuple[int, str]] = []

    def finish_run() -> tuple[bool, str] | None:
        if len(prose_run) > 1:
            lines = ", ".join(str(number) for number, _ in prose_run)
            return False, f"one prose segment spans physical lines {lines}"
        prose_run.clear()
        return None

    for number, raw_line in enumerate(source.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            failure = finish_run()
            if failure:
                return failure
            continue
        if line.startswith("\\begin{"):
            failure = finish_run()
            if failure:
                return failure
            environment_depth += 1
            continue
        if line.startswith("\\end{"):
            environment_depth = max(0, environment_depth - 1)
            continue
        if environment_depth:
            continue
        if line.startswith(("\\section", "\\subsection", "\\paragraph", "\\item", "%")):
            failure = finish_run()
            if failure:
                return failure
            continue
        prose_run.append((number, line))

    failure = finish_run()
    if failure:
        return failure
    return True, "each prose segment occupies one physical source line"


def _max_chars(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    maximum = spec.params.get("max")
    if not isinstance(maximum, int) or maximum <= 0:
        raise ValueError(
            f"assertion {spec.assertion_id!r} requires positive integer 'max'"
        )
    actual = len(text.strip())
    return actual <= maximum, f"{actual} characters; maximum {maximum}"


def _markdown_table(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    del spec
    matched = bool(_MARKDOWN_TABLE_RE.search(text))
    return matched, "Markdown table found" if matched else "Markdown table not found"


def _no_markdown_table(text: str, spec: AssertionSpec) -> tuple[bool, str]:
    del spec
    matched = bool(_MARKDOWN_TABLE_RE.search(text))
    return (
        not matched,
        "unexpected Markdown table found" if matched else "no Markdown table found",
    )


_GRADERS: dict[str, Callable[[str, AssertionSpec], tuple[bool, str]]] = {
    "contains_all": _contains_all,
    "contains_any": _contains_any,
    "not_contains_any": _not_contains_any,
    "regex": _regex,
    "not_regex": _not_regex,
    "ordered_contains": _ordered_contains,
    "source_only": _source_only,
    "latex_prose_single_line": _latex_prose_single_line,
    "max_chars": _max_chars,
    "markdown_table": _markdown_table,
    "no_markdown_table": _no_markdown_table,
}


def grade_response(case: EvalCase, response: str) -> dict[str, Any]:
    """Grade one response and return both strict and partial-credit results."""
    assertion_results: list[AssertionResult] = []
    earned = 0.0
    total = 0.0
    for spec in case.assertions:
        grader = _GRADERS.get(spec.kind)
        if grader is None:
            raise ValueError(
                f"case {case.case_id!r} uses unsupported assertion kind {spec.kind!r}"
            )
        passed, diagnostic = grader(response or "", spec)
        total += spec.weight
        if passed:
            earned += spec.weight
        assertion_results.append(
            AssertionResult(
                assertion_id=spec.assertion_id,
                kind=spec.kind,
                passed=passed,
                weight=spec.weight,
                description=spec.description,
                metric=spec.metric,
                diagnostic=diagnostic,
            )
        )
    score = earned / total if total else 0.0
    return {
        "score": round(score, 6),
        "passed": score + 1e-12 >= case.pass_threshold,
        "pass_threshold": case.pass_threshold,
        "earned_weight": earned,
        "total_weight": total,
        "assertions": [result.as_dict() for result in assertion_results],
    }
