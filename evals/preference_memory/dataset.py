"""Dataset contract for the workflow-preference evaluation harness.

The shape follows PrefEval's preference/query pairing, but makes the intervening
history, applicability boundary, and output assertions explicit.  A setup can
contain one direct preference turn or several turns that reveal it implicitly.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class DatasetError(ValueError):
    """Raised when an evaluation dataset violates the public contract."""


@dataclass(frozen=True)
class Turn:
    user: str
    assistant: str

    @classmethod
    def from_dict(cls, raw: Any, *, where: str) -> "Turn":
        if not isinstance(raw, dict):
            raise DatasetError(f"{where} must be an object")
        user = raw.get("user")
        assistant = raw.get("assistant")
        if not isinstance(user, str) or not user.strip():
            raise DatasetError(f"{where}.user must be a non-empty string")
        if not isinstance(assistant, str) or not assistant.strip():
            raise DatasetError(f"{where}.assistant must be a non-empty string")
        return cls(user=user.strip(), assistant=assistant.strip())

    def as_messages(self) -> list[dict[str, str]]:
        return [
            {"role": "user", "content": self.user},
            {"role": "assistant", "content": self.assistant},
        ]


@dataclass(frozen=True)
class AssertionSpec:
    assertion_id: str
    kind: str
    weight: float = 1.0
    description: str = ""
    metric: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any, *, where: str) -> "AssertionSpec":
        if not isinstance(raw, dict):
            raise DatasetError(f"{where} must be an object")
        assertion_id = raw.get("id")
        kind = raw.get("kind")
        if not isinstance(assertion_id, str) or not assertion_id.strip():
            raise DatasetError(f"{where}.id must be a non-empty string")
        if not isinstance(kind, str) or not kind.strip():
            raise DatasetError(f"{where}.kind must be a non-empty string")
        try:
            weight = float(raw.get("weight", 1.0))
        except (TypeError, ValueError) as exc:
            raise DatasetError(f"{where}.weight must be numeric") from exc
        if weight <= 0:
            raise DatasetError(f"{where}.weight must be greater than zero")
        description = raw.get("description", "")
        if not isinstance(description, str):
            raise DatasetError(f"{where}.description must be a string")
        metric = raw.get("metric", "")
        if not isinstance(metric, str):
            raise DatasetError(f"{where}.metric must be a string")
        reserved = {"id", "kind", "weight", "description", "metric"}
        return cls(
            assertion_id=assertion_id.strip(),
            kind=kind.strip(),
            weight=weight,
            description=description.strip(),
            metric=metric.strip(),
            params={key: value for key, value in raw.items() if key not in reserved},
        )


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    category: str
    preference_form: str
    setup: tuple[Turn, ...]
    probe: str
    applicable: bool
    assertions: tuple[AssertionSpec, ...]
    expected_memory_markers: tuple[tuple[str, ...], ...] = ()
    tags: tuple[str, ...] = ()
    pass_threshold: float = 1.0
    notes: str = ""

    @classmethod
    def from_dict(cls, raw: Any, *, index: int) -> "EvalCase":
        where = f"cases[{index}]"
        if not isinstance(raw, dict):
            raise DatasetError(f"{where} must be an object")
        case_id = raw.get("id")
        category = raw.get("category")
        preference_form = raw.get("preference_form", "explicit")
        probe = raw.get("probe")
        for key, value in (
            ("id", case_id),
            ("category", category),
            ("preference_form", preference_form),
            ("probe", probe),
        ):
            if not isinstance(value, str) or not value.strip():
                raise DatasetError(f"{where}.{key} must be a non-empty string")

        setup_raw = raw.get("setup")
        if not isinstance(setup_raw, list) or not setup_raw:
            raise DatasetError(f"{where}.setup must be a non-empty list")
        setup = tuple(
            Turn.from_dict(turn, where=f"{where}.setup[{turn_index}]")
            for turn_index, turn in enumerate(setup_raw)
        )

        assertions_raw = raw.get("assertions")
        if not isinstance(assertions_raw, list) or not assertions_raw:
            raise DatasetError(f"{where}.assertions must be a non-empty list")
        assertions = tuple(
            AssertionSpec.from_dict(item, where=f"{where}.assertions[{item_index}]")
            for item_index, item in enumerate(assertions_raw)
        )
        assertion_ids = [item.assertion_id for item in assertions]
        if len(assertion_ids) != len(set(assertion_ids)):
            raise DatasetError(f"{where}.assertions contains duplicate ids")

        applicable = raw.get("applicable")
        if not isinstance(applicable, bool):
            raise DatasetError(f"{where}.applicable must be true or false")
        markers = _marker_groups(
            raw.get("expected_memory_markers", []), f"{where}.expected_memory_markers"
        )
        tags = _string_tuple(raw.get("tags", []), f"{where}.tags")
        try:
            pass_threshold = float(raw.get("pass_threshold", 1.0))
        except (TypeError, ValueError) as exc:
            raise DatasetError(f"{where}.pass_threshold must be numeric") from exc
        if not 0 < pass_threshold <= 1:
            raise DatasetError(f"{where}.pass_threshold must be in (0, 1]")
        notes = raw.get("notes", "")
        if not isinstance(notes, str):
            raise DatasetError(f"{where}.notes must be a string")

        return cls(
            case_id=case_id.strip(),
            category=category.strip(),
            preference_form=preference_form.strip(),
            setup=setup,
            probe=probe.strip(),
            applicable=applicable,
            assertions=assertions,
            expected_memory_markers=markers,
            tags=tags,
            pass_threshold=pass_threshold,
            notes=notes.strip(),
        )


@dataclass(frozen=True)
class EvalDataset:
    name: str
    version: str
    description: str
    distractors: tuple[Turn, ...]
    cases: tuple[EvalCase, ...]
    source_path: Path
    sha256: str

    @property
    def cases_by_id(self) -> dict[str, EvalCase]:
        return {case.case_id: case for case in self.cases}

    def select(self, case_ids: Iterable[str] | None = None) -> tuple[EvalCase, ...]:
        wanted = [item for item in (case_ids or []) if item]
        if not wanted:
            return self.cases
        lookup = self.cases_by_id
        missing = [case_id for case_id in wanted if case_id not in lookup]
        if missing:
            raise DatasetError(f"unknown case id(s): {', '.join(missing)}")
        return tuple(lookup[case_id] for case_id in wanted)

    def intervening_turns(
        self, case: EvalCase, distance: int, seed: int
    ) -> tuple[Turn, ...]:
        """Return a deterministic PrefEval-style distractor history.

        ``distance`` counts complete user/assistant exchanges.  The pool is
        cycled for long conditions, with its starting position derived from a
        stable digest rather than Python's process-randomized ``hash()``.
        """
        if distance < 0:
            raise DatasetError("distance must be non-negative")
        if distance == 0:
            return ()
        if not self.distractors:
            raise DatasetError("dataset has no distractor turns")
        digest = hashlib.sha256(f"{self.name}:{case.case_id}:{seed}".encode()).digest()
        offset = int.from_bytes(digest[:8], "big") % len(self.distractors)
        return tuple(
            self.distractors[(offset + index) % len(self.distractors)]
            for index in range(distance)
        )


def _string_tuple(raw: Any, where: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise DatasetError(f"{where} must be a list of strings")
    return tuple(item.strip() for item in raw if item.strip())


def _marker_groups(raw: Any, where: str) -> tuple[tuple[str, ...], ...]:
    """Parse required recall concepts with optional lexical alternatives.

    A string is a required literal marker. A nested list is one required
    concept for which any listed spelling or language is accepted.
    """
    if not isinstance(raw, list):
        raise DatasetError(f"{where} must be a list")
    groups: list[tuple[str, ...]] = []
    for index, item in enumerate(raw):
        item_where = f"{where}[{index}]"
        if isinstance(item, str):
            marker = item.strip()
            if not marker:
                raise DatasetError(f"{item_where} must be non-empty")
            groups.append((marker,))
            continue
        alternatives = _string_tuple(item, item_where)
        if not alternatives:
            raise DatasetError(f"{item_where} must contain a non-empty marker")
        groups.append(alternatives)
    return tuple(groups)


def load_dataset(path: str | Path) -> EvalDataset:
    source_path = Path(path).expanduser().resolve()
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise DatasetError(f"cannot read dataset {source_path}: {exc}") from exc
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DatasetError(f"invalid JSON in {source_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise DatasetError("dataset root must be an object")
    if raw.get("schema_version") != 1:
        raise DatasetError("dataset.schema_version must be 1")

    name = raw.get("name")
    version = raw.get("version")
    description = raw.get("description", "")
    for key, value in (("name", name), ("version", version)):
        if not isinstance(value, str) or not value.strip():
            raise DatasetError(f"dataset.{key} must be a non-empty string")
    if not isinstance(description, str):
        raise DatasetError("dataset.description must be a string")

    distractors_raw = raw.get("distractors")
    if not isinstance(distractors_raw, list) or not distractors_raw:
        raise DatasetError("dataset.distractors must be a non-empty list")
    distractors = tuple(
        Turn.from_dict(item, where=f"distractors[{index}]")
        for index, item in enumerate(distractors_raw)
    )

    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise DatasetError("dataset.cases must be a non-empty list")
    cases = tuple(
        EvalCase.from_dict(item, index=index) for index, item in enumerate(cases_raw)
    )
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise DatasetError("dataset contains duplicate case ids")

    return EvalDataset(
        name=name.strip(),
        version=version.strip(),
        description=description.strip(),
        distractors=distractors,
        cases=cases,
        source_path=source_path,
        sha256=hashlib.sha256(payload).hexdigest(),
    )
