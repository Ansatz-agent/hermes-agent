#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEDGER = REPO_ROOT / "docs/security/main-auth-voice-migration-ledger.json"
DEFAULT_PRODUCT_PATHS = REPO_ROOT / "docs/security/main-auth-voice-product-paths.txt"
PRODUCT_OWNERS = {
    "common-product",
    "package-shared",
    "package-macos",
    "package-windows",
}
ALLOWED_REASONS = {
    "auth-integration",
    "dependency-lock-regeneration",
    "domestic-mirror-policy",
    "neutral-platform-wording",
    "package-producer-interface-extraction",
    "upstream-main-preservation",
    "windows-adapter-addition",
}


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def resolve_commit(repo: Path, reference: str) -> str:
    result = git(repo, "rev-parse", "--verify", f"{reference}^{{commit}}")
    if result.returncode != 0:
        raise ValueError(f"cannot resolve reference: {reference}")
    return result.stdout.strip()


def reference_blob(repo: Path, reference: str, path: str) -> str | None:
    result = git(repo, "rev-parse", f"{reference}:{path}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def candidate_blob(repo: Path, path: str) -> str | None:
    candidate = repo / path
    if not candidate.exists() and not candidate.is_symlink():
        return None
    result = git(repo, "hash-object", "--", path)
    if result.returncode != 0:
        raise ValueError(f"cannot hash candidate product path: {path}")
    return result.stdout.strip()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def load_product_paths(path: Path) -> list[str]:
    paths = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
    if paths != sorted(set(paths)):
        raise ValueError("product paths must be unique and sorted")
    return paths


def validate_waivers(
    *,
    repo: Path,
    reference: str,
    ledger: dict[str, Any],
    product_paths: set[str],
    path_owners: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    errors: list[str] = []
    waivers: dict[str, dict[str, Any]] = {}
    configured_reasons = ledger.get("parity_reason_enum")
    if not isinstance(configured_reasons, list) or set(configured_reasons) != ALLOWED_REASONS:
        errors.append("parity_reason_enum does not match the locked reason set")

    raw_waivers = ledger.get("parity_waivers")
    if not isinstance(raw_waivers, list):
        return {}, [*errors, "parity_waivers must be a list"]

    for raw in raw_waivers:
        if not isinstance(raw, dict):
            errors.append("parity waiver must be an object")
            continue
        path = raw.get("path")
        if not isinstance(path, str) or not path or path.endswith("/") or any(
            character in path for character in "*?[]"
        ):
            errors.append(f"waiver path must be exact: {path!r}")
        elif path in waivers:
            errors.append(f"duplicate parity waiver: {path}")
        else:
            waivers[path] = raw

        reason = raw.get("reason")
        if reason not in ALLOWED_REASONS:
            errors.append(f"invalid waiver reason for {path}: {reason!r}")
        if path not in product_paths:
            errors.append(f"waiver path is not a product path: {path}")
        owner = raw.get("owner")
        if owner not in PRODUCT_OWNERS or owner != path_owners.get(path):
            errors.append(f"waiver owner does not match product ownership: {path}")
        if raw.get("reference") != reference:
            errors.append(f"waiver reference is not the full locked DMG reference: {path}")

        tests = raw.get("tests")
        if not isinstance(tests, list) or not tests:
            errors.append(f"waiver has no path-specific tests: {path}")
        else:
            for test in tests:
                if not isinstance(test, str) or not test or not (repo / test).is_file():
                    errors.append(f"waiver test does not exist for {path}: {test!r}")

    return waivers, errors


def check(
    *,
    repo: Path,
    reference_name: str,
    ledger_path: Path,
    product_paths_path: Path,
) -> list[str]:
    errors: list[str] = []
    ledger = load_json(ledger_path)
    reference = resolve_commit(repo, reference_name)
    if ledger.get("dmg_reference") != reference:
        errors.append("requested reference does not match ledger dmg_reference")

    product_paths = load_product_paths(product_paths_path)
    raw_path_owners = ledger.get("path_owners")
    if not isinstance(raw_path_owners, list):
        return [*errors, "path_owners must be a list"]
    path_owners = {
        entry["path"]: entry["owner"]
        for entry in raw_path_owners
        if isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("owner"), str)
    }
    for path in product_paths:
        if path_owners.get(path) not in PRODUCT_OWNERS:
            errors.append(f"product path has no product owner: {path}")

    waivers, waiver_errors = validate_waivers(
        repo=repo,
        reference=reference,
        ledger=ledger,
        product_paths=set(product_paths),
        path_owners=path_owners,
    )
    errors.extend(waiver_errors)

    drift: set[str] = set()
    for path in product_paths:
        if candidate_blob(repo, path) == reference_blob(repo, reference, path):
            continue
        drift.add(path)
        if path not in waivers:
            errors.append(f"unwaived product drift: {path}")

    for path in sorted(waivers.keys() - drift):
        errors.append(f"stale parity waiver: {path}")
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check path-specific parity with the accepted macOS DMG product"
    )
    parser.add_argument("--repo", type=Path, default=REPO_ROOT)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    parser.add_argument("--product-paths", type=Path, default=DEFAULT_PRODUCT_PATHS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    try:
        errors = check(
            repo=repo,
            reference_name=args.reference,
            ledger_path=args.ledger.resolve(),
            product_paths_path=args.product_paths.resolve(),
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        errors = [str(error)]
    if errors:
        for error in errors:
            print(f"main auth voice parity: {error}")
        return 1
    print("main auth voice parity: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
