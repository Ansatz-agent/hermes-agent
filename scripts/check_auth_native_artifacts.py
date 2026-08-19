#!/usr/bin/env python3
"""Validate native auth hard-gate evidence produced by CI runners."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_TRANSPORTS = {
    "linux": "unix-peercred",
    "macos": "unix-getpeereid",
    "windows": "named-pipe-sid",
}
_BOOLEAN_FIELDS = (
    "locked_start_passed",
    "handle_noninheritance_passed",
    "service_locked_waiting_passed",
)
_FIELDS = frozenset({"platform", "owner_transport", *_BOOLEAN_FIELDS})


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name}: unreadable JSON: {error}") from error
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError(f"{path.name}: expected exactly {sorted(_FIELDS)}")
    platform = value["platform"]
    if platform not in _TRANSPORTS:
        raise ValueError(f"{path.name}: invalid platform {platform!r}")
    expected_transport = _TRANSPORTS[platform]
    if value["owner_transport"] != expected_transport:
        raise ValueError(
            f"{path.name}: {platform} requires owner_transport={expected_transport}"
        )
    for field in _BOOLEAN_FIELDS:
        if value[field] is not True:
            raise ValueError(f"{path.name}: {field} must be true")
    return value


def validate(directory: Path, *, allow_partial_local: bool) -> list[str]:
    if not directory.is_dir():
        raise ValueError(f"artifact directory does not exist: {directory}")
    records: dict[str, dict[str, Any]] = {}
    sources: dict[str, Path] = {}
    for path in sorted(directory.glob("*.json")):
        record = _load(path)
        platform = record["platform"]
        prior = records.get(platform)
        if prior is not None and prior != record:
            raise ValueError(
                f"{path.name}: disagrees with duplicate {sources[platform].name}"
            )
        records[platform] = record
        sources[platform] = path
    if not records:
        raise ValueError("no native auth artifacts found")
    missing = set(_TRANSPORTS) - set(records)
    if missing and not allow_partial_local:
        raise ValueError(f"missing platform artifacts: {', '.join(sorted(missing))}")
    return [platform for platform in _TRANSPORTS if platform in records]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-partial-local", action="store_true")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        platforms = validate(
            args.directory,
            allow_partial_local=args.allow_partial_local,
        )
    except ValueError as error:
        print(f"native auth artifact validation failed: {error}", file=sys.stderr)
        return 1
    print(f"validated native auth artifacts: {', '.join(platforms)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
