#!/usr/bin/env python3
"""Build native auth evidence from pytest JUnit reports."""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


_LOCKED_START_CASES = frozenset(
    {
        "test_console_wrappers_guard_before_capability_target_import",
        "test_every_guarded_python_entry_exits_locked_before_capability_imports",
    }
)
_SERVICE_WAITING_CASES = frozenset(
    {
        "test_locked_waiting_never_prompts_and_authorizes_before_return",
        "test_s6_lifecycle_starts_only_desired_slots_and_locks_all",
        "test_s6_lifecycle_suppresses_named_gateways_when_default_multiplexes",
    }
)
_NATIVE_CASES = {
    "linux": frozenset(
        {
            "test_linux_child_cannot_inherit_owner_connection",
            "test_linux_unix_runtime_endpoint_enforces_permissions_and_peer_uid",
        }
    ),
    "macos": frozenset(
        {
            "test_macos_child_cannot_inherit_owner_connection",
            "test_macos_unix_runtime_endpoint_enforces_permissions_and_peer_uid",
        }
    ),
    "windows": frozenset(
        {
            "test_windows_named_pipe_restricts_dacl_verifies_sid_and_inheritance",
        }
    ),
}


def _verified_pass(path: Path, *, required_cases: frozenset[str]) -> bool:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"{path.name}: unreadable JUnit XML: {error}") from error

    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    if not suites:
        raise ValueError(f"{path.name}: no testsuite records")

    totals = {field: 0 for field in ("tests", "failures", "errors", "skipped")}
    for suite in suites:
        for field in totals:
            try:
                value = int(suite.get(field, "0"))
            except ValueError as error:
                raise ValueError(f"{path.name}: invalid {field} count") from error
            if value < 0:
                raise ValueError(f"{path.name}: negative {field} count")
            totals[field] += value

    if totals["failures"] or totals["errors"]:
        raise ValueError(
            f"{path.name}: failures={totals['failures']} errors={totals['errors']}"
        )
    passed = (
        totals["tests"]
        - totals["failures"]
        - totals["errors"]
        - totals["skipped"]
    )
    if passed <= 0:
        raise ValueError(f"{path.name}: no passing tests")

    passed_cases = {
        case.get("name", "")
        for case in root.findall(".//testcase")
        if not any(
            child.tag.rsplit("}", 1)[-1] in {"failure", "error", "skipped"}
            for child in case
        )
    }
    missing = required_cases - passed_cases
    if missing:
        raise ValueError(
            f"{path.name}: required cases did not pass: {', '.join(sorted(missing))}"
        )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True)
    parser.add_argument("--owner-transport", required=True)
    parser.add_argument("--locked-start", type=Path, required=True)
    parser.add_argument("--handle-noninheriting", type=Path, required=True)
    parser.add_argument("--service-waiting", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        native_cases = _NATIVE_CASES[args.platform]
        payload = {
            "platform": args.platform,
            "owner_transport": args.owner_transport,
            "locked_start_passed": _verified_pass(
                args.locked_start,
                required_cases=_LOCKED_START_CASES,
            ),
            "handle_noninheritance_passed": _verified_pass(
                args.handle_noninheriting,
                required_cases=native_cases,
            ),
            "service_locked_waiting_passed": _verified_pass(
                args.service_waiting,
                required_cases=_SERVICE_WAITING_CASES,
            ),
        }
    except (KeyError, ValueError) as error:
        print(f"native auth artifact generation failed: {error}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote native auth artifact: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
