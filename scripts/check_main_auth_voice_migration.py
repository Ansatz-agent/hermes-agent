#!/usr/bin/env python3
"""Validate auth/Voice migration ownership and release-artifact boundaries."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any


PRODUCT_OWNERS = {
    "common-product",
    "package-shared",
    "package-macos",
    "package-windows",
}
GENERATED_SUFFIXES = (
    ".dmg",
    ".pkg",
    ".exe",
    ".msi",
    ".zip",
    ".tar.gz",
    ".tar.xz",
    ".gz",
    ".whl",
    ".blockmap",
)
GENERATED_PREFIXES = (
    "apps/desktop/release/",
    "apps/desktop/build/",
)
CHERRY_PICK_RE = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{40})\)",
    re.IGNORECASE,
)
SENSITIVE_BASENAMES = {
    ".env",
    "credential.json",
    "credentials.json",
    "cookies.json",
    "session.json",
}
SENSITIVE_SUFFIXES = (".pem", ".p12", ".pfx", ".key")
SENSITIVE_EVIDENCE_PREFIXES = ("tests/.artifacts/",)
SENSITIVE_EVIDENCE_TOKENS = ("cookie", "csrf", "keychain", "session")


class MigrationError(RuntimeError):
    """A fail-closed migration contract error."""


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise MigrationError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def normalized_repo_path(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\\" in raw:
        raise MigrationError(f"invalid {field}: {raw!r}")
    prefix = raw.endswith("/")
    candidate = raw[:-1] if prefix else raw
    path = PurePosixPath(candidate)
    if path.is_absolute() or raw.startswith("./") or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise MigrationError(f"invalid {field}: {raw!r}")
    return path.as_posix() + ("/" if prefix else "")


def load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise MigrationError(f"{label} must be a JSON object: {path}")
    return value


def load_product_paths(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise MigrationError(f"cannot read product path manifest {path}: {exc}") from exc
    if any(not line for line in lines):
        raise MigrationError("product path manifest contains a blank line")
    result = [normalized_repo_path(line, field="product path") for line in lines]
    if result != sorted(result):
        raise MigrationError("product path manifest must be sorted")
    if len(result) != len(set(result)):
        raise MigrationError("product path manifest contains duplicates")
    return result


def validate_ledger(ledger: dict[str, Any], product_paths: list[str]) -> list[dict[str, str]]:
    if ledger.get("schema_version") != 1:
        raise MigrationError("unsupported migration ledger schema_version")
    owners = ledger.get("owner_enum")
    strategies = ledger.get("strategy_enum")
    if not isinstance(owners, list) or not owners or len(owners) != len(set(owners)):
        raise MigrationError("owner_enum must be a unique non-empty list")
    if not isinstance(strategies, list) or not strategies or len(strategies) != len(set(strategies)):
        raise MigrationError("strategy_enum must be a unique non-empty list")

    commits = ledger.get("commits")
    if not isinstance(commits, list):
        raise MigrationError("commits must be a list")
    seen_commits: set[str] = set()
    for entry in commits:
        if not isinstance(entry, dict):
            raise MigrationError("commit entries must be objects")
        sha = entry.get("sha")
        if not isinstance(sha, str) or len(sha) != 40 or any(
            character not in "0123456789abcdef" for character in sha
        ):
            raise MigrationError(f"invalid commit sha: {sha!r}")
        if sha in seen_commits:
            raise MigrationError(f"commit classified twice: {sha}")
        seen_commits.add(sha)
        if entry.get("owner") not in owners:
            raise MigrationError(f"invalid owner for commit {sha}")
        if entry.get("strategy") not in strategies:
            raise MigrationError(f"invalid strategy for commit {sha}")
        paths = entry.get("paths", [])
        if not isinstance(paths, list):
            raise MigrationError(f"commit paths must be a list: {sha}")
        normalized = [normalized_repo_path(path, field=f"commit path {sha}") for path in paths]
        if len(normalized) != len(set(normalized)):
            raise MigrationError(f"duplicate commit path: {sha}")
        if entry.get("strategy") == "path-extract" and not normalized:
            raise MigrationError(f"path-extract lacks paths: {sha}")

    raw_path_owners = ledger.get("path_owners")
    if not isinstance(raw_path_owners, list):
        raise MigrationError("path_owners must be a list")
    path_owners: list[dict[str, str]] = []
    seen_rules: set[tuple[str, str]] = set()
    for entry in raw_path_owners:
        if not isinstance(entry, dict):
            raise MigrationError("path owner entries must be objects")
        match = entry.get("match", "exact")
        if match not in {"exact", "prefix"}:
            raise MigrationError(f"invalid path owner match: {match!r}")
        path = normalized_repo_path(entry.get("path"), field="owned path")
        if match == "prefix" and not path.endswith("/"):
            raise MigrationError(f"prefix ownership must end with '/': {path}")
        if match == "exact" and path.endswith("/"):
            raise MigrationError(f"exact ownership cannot end with '/': {path}")
        owner = entry.get("owner")
        if owner not in owners:
            raise MigrationError(f"invalid path owner for {path}: {owner!r}")
        rule = (match, path)
        if rule in seen_rules:
            raise MigrationError(f"path listed under two owners: {path}")
        seen_rules.add(rule)
        path_owners.append({"match": match, "path": path, "owner": owner})

    projection = sorted(
        entry["path"] for entry in path_owners if entry["owner"] in PRODUCT_OWNERS
    )
    if projection != product_paths:
        raise MigrationError("product path manifest does not equal ledger projection")
    return path_owners


def commit_range(repo: Path, start: str, end: str) -> set[str]:
    return output_paths(git(repo, "log", "--format=%H", f"{start}..{end}"))


def commit_changed_paths(repo: Path, sha: str) -> set[str]:
    return output_paths(
        git(repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", sha)
    )


def validate_locked_commit_coverage(
    repo: Path,
    base: str,
    ledger: dict[str, Any],
) -> list[str]:
    if ledger.get("enforce_locked_commit_coverage") is not True:
        return []

    required_refs = {
        "common_baseline": ledger.get("common_baseline"),
        "dmg_integration_reference": ledger.get("dmg_integration_reference"),
        "windows_integration_reference": ledger.get("windows_integration_reference"),
        "candidate_tip": ledger.get("candidate_tip"),
        "base": ledger.get("base"),
    }
    for field, value in required_refs.items():
        if not isinstance(value, str) or len(value) != 40:
            raise MigrationError(f"locked commit coverage requires {field}")
        git(repo, "rev-parse", "--verify", f"{value}^{{commit}}")
    if git(repo, "rev-parse", base).strip() != required_refs["base"]:
        raise MigrationError("checker base does not match the locked migration base")

    expected = set()
    expected.update(
        commit_range(
            repo,
            required_refs["common_baseline"],
            required_refs["dmg_integration_reference"],
        )
    )
    expected.update(
        commit_range(
            repo,
            required_refs["common_baseline"],
            required_refs["windows_integration_reference"],
        )
    )
    expected.update(
        commit_range(repo, required_refs["base"], required_refs["candidate_tip"])
    )
    actual = {entry["sha"] for entry in ledger["commits"]}
    violations: list[str] = []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        violations.append("locked commit coverage is missing: " + ", ".join(missing))
    if extra:
        violations.append("locked commit coverage has unexpected entries: " + ", ".join(extra))

    contract_paths_raw = ledger.get("contract_bookkeeping_paths")
    post_tip_commits_raw = ledger.get("post_tip_commits")
    if not isinstance(contract_paths_raw, list) or not contract_paths_raw:
        raise MigrationError("contract_bookkeeping_paths must be a non-empty list")
    if "post_tip_subjects" in ledger:
        raise MigrationError(
            "post_tip_subjects is forbidden; register exact post_tip_commits instead"
        )
    if not isinstance(post_tip_commits_raw, list):
        raise MigrationError("post_tip_commits must be a list")
    contract_paths = {
        normalized_repo_path(path, field="contract bookkeeping path")
        for path in contract_paths_raw
    }
    post_tip_commits = {
        sha
        for sha in post_tip_commits_raw
        if isinstance(sha, str)
        and len(sha) == 40
        and all(character in "0123456789abcdef" for character in sha)
    }
    if len(post_tip_commits) != len(post_tip_commits_raw):
        raise MigrationError("post_tip_commits must contain unique full lowercase SHAs")

    source_strategies = {
        entry["sha"]: entry["strategy"] for entry in ledger["commits"]
    }
    rows = git(
        repo,
        "log",
        "--first-parent",
        "--reverse",
        "--format=%H%x00%s%x00%P",
        f"{required_refs['candidate_tip']}..HEAD",
    ).splitlines()
    observed_post_tip_commits: set[str] = set()
    for row in rows:
        if not row:
            continue
        parts = row.split("\0")
        if len(parts) != 3:
            raise MigrationError("cannot parse post-tip commit metadata")
        sha, subject, parent_text = parts
        observed_post_tip_commits.add(sha)
        changed = commit_changed_paths(repo, sha)
        if changed and changed <= contract_paths:
            continue
        if sha in post_tip_commits:
            continue
        message = git(repo, "show", "-s", "--format=%B", sha)
        cherry_pick = CHERRY_PICK_RE.search(message)
        if cherry_pick and source_strategies.get(cherry_pick.group(1)) in {
            "cherry-pick",
            "path-extract",
            "reference-equivalent",
        }:
            continue
        parents = parent_text.split()
        if len(parents) > 1:
            imported = commit_range(repo, parents[0], parents[1])
            if imported and imported <= actual:
                continue
        violations.append(
            f"post-tip commit is not covered by migration policy: {sha} {subject}"
        )
    orphaned = sorted(post_tip_commits - observed_post_tip_commits)
    if orphaned:
        violations.append(
            "registered post-tip commits are not present after candidate_tip: "
            + ", ".join(orphaned)
        )
    return violations


def output_paths(output: str) -> set[str]:
    return {line for line in output.splitlines() if line}


def candidate_paths(repo: Path, base: str) -> set[str]:
    paths = output_paths(git(repo, "diff", "--name-only", f"{base}...HEAD", "--"))
    paths.update(output_paths(git(repo, "diff", "--name-only", "--")))
    paths.update(output_paths(git(repo, "diff", "--cached", "--name-only", "--")))
    paths.update(output_paths(git(repo, "ls-files", "--others", "--exclude-standard")))
    return paths


def matching_rules(path: str, path_owners: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        entry
        for entry in path_owners
        if (entry["match"] == "exact" and path == entry["path"])
        or (entry["match"] == "prefix" and path.startswith(entry["path"]))
    ]


def artifact_violation(path: str) -> str | None:
    lowered = path.lower()
    basename = PurePosixPath(lowered).name
    if lowered.endswith(GENERATED_SUFFIXES) or lowered.startswith(GENERATED_PREFIXES):
        return "generated artifact is forbidden"
    if basename in SENSITIVE_BASENAMES or basename.endswith(SENSITIVE_SUFFIXES):
        return "credential or secret file is forbidden"
    if lowered.startswith(SENSITIVE_EVIDENCE_PREFIXES) and any(
        token in lowered for token in SENSITIVE_EVIDENCE_TOKENS
    ):
        return "raw session/cookie/keychain evidence is forbidden"
    return None


def check_candidate(
    repo: Path,
    base: str,
    path_owners: list[dict[str, str]],
) -> list[str]:
    violations: list[str] = []
    git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    for path in sorted(candidate_paths(repo, base)):
        try:
            normalized = normalized_repo_path(path, field="candidate path")
        except MigrationError as exc:
            violations.append(str(exc))
            continue
        artifact = artifact_violation(normalized)
        if artifact:
            violations.append(f"{artifact}: {normalized}")
        matches = matching_rules(normalized, path_owners)
        if not matches:
            violations.append(f"unowned changed path: {normalized}")
        elif len(matches) > 1:
            owners = ", ".join(sorted({entry["owner"] for entry in matches}))
            violations.append(f"changed path has multiple owners ({owners}): {normalized}")
    return violations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--product-paths", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    ledger_path = (args.ledger or repo / "docs/security/main-auth-voice-migration-ledger.json").resolve()
    product_path = (args.product_paths or repo / "docs/security/main-auth-voice-product-paths.txt").resolve()
    try:
        git(repo, "rev-parse", "--show-toplevel")
        ledger = load_json(ledger_path, label="migration ledger")
        product_paths = load_product_paths(product_path)
        path_owners = validate_ledger(ledger, product_paths)
        violations = validate_locked_commit_coverage(repo, args.base, ledger)
        violations.extend(check_candidate(repo, args.base, path_owners))
    except MigrationError as exc:
        print(f"main auth voice migration: {exc}", file=sys.stderr)
        return 1

    if violations:
        for violation in violations:
            print(f"main auth voice migration: {violation}", file=sys.stderr)
        return 1

    print("main auth voice migration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
