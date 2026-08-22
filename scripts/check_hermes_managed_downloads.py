#!/usr/bin/env python3
"""Inventory and validate Hermes-managed dependency download origins."""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


DEFAULT_ACCOUNT_ORIGINS = ("https://c2sml.cn/agent",)
SCAN_EXACT = {
    "scripts/install.sh",
    "scripts/install.ps1",
    "tools/lazy_deps.py",
    "tools/sensevoice_stt.py",
    "tools/browser_use_cli.py",
    "tools/computer_use/cua_backend.py",
    "tools/neutts_synth.py",
    "hermes_cli/tools_config.py",
    "hermes_cli/managed_uv.py",
    "hermes_cli/setup.py",
    "hermes_cli/update_cmd.py",
    "hermes_cli/model_catalog.py",
    "hermes_cli/config_defaults.py",
    "apps/desktop/electron/bootstrap-process.ts",
    "apps/desktop/electron/runtime-download-policy.ts",
    "apps/desktop/scripts/prepare-auth-toolchain-inputs.mjs",
    "apps/desktop/scripts/build-auth-toolchain.mjs",
    "apps/desktop/scripts/build-backend-payload.mjs",
    "apps/desktop/scripts/prepare-windows-git-runtime.mjs",
}
URL_RE = re.compile(r"https://[^\s\"'<>`|)\]}]+")
MODEL_PATTERNS = (
    re.compile(r"\bsnapshot_download\s*\(\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\bhf_hub_download\s*\([^)]*?repo_id\s*=\s*[\"']([^\"']+)[\"']", re.DOTALL),
)
PACKAGE_MANAGER_RE = re.compile(
    r"uv[\"']?\s*[, ]+\s*[\"']?tool[\"']?\s*[, ]+\s*[\"']?install[\"']?\s*[, ]+\s*[\"']?browser-use",
    re.IGNORECASE,
)
REMOTE_SHELL_RE = re.compile(
    r"(?:curl|wget)[^\n|]*https://[^\n|]+\|\s*(?:ba)?sh\b",
    re.IGNORECASE,
)
REMOTE_POWERSHELL_RE = re.compile(
    r"(?:Invoke-RestMethod|irm)[^\n|]*https://[^\n|]+\|\s*(?:Invoke-Expression|iex)\b",
    re.IGNORECASE,
)
POLICY_ENV_MARKERS = (
    "managed_download_environment(",
    "buildManagedDownloadEnvironment(",
    "MANAGED_DOWNLOAD_ENVIRONMENT",
)
DEPENDENCY_PATH_HINTS = (
    "/simple",
    ".git",
    ".json",
    ".ps1",
    ".sh",
    ".tar.gz",
    ".tgz",
    ".whl",
    ".zip",
    "/archive/",
    "/dist/",
    "/releases/",
    "/releases/download/",
    "modelscope.cn/models/",
)
DEPENDENCY_HOSTS = {
    "raw.githubusercontent.com",
    "registry.npmmirror.com",
}
NON_DOWNLOAD_LITERAL_PATHS = {"hermes_cli/config_defaults.py"}


class DownloadPolicyError(RuntimeError):
    """A fail-closed managed-download policy error."""


@dataclass(frozen=True, order=True)
class Sink:
    path: str
    kind: str
    value: str
    line: int

    def key(self) -> tuple[str, str, str]:
        return (self.path, self.kind, self.value)

    def as_json(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "value": self.value,
            "line": self.line,
        }


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
        raise DownloadPolicyError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def normalize_path(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\\" in raw:
        raise DownloadPolicyError(f"invalid {field}: {raw!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or raw.startswith("./") or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise DownloadPolicyError(f"invalid {field}: {raw!r}")
    return path.as_posix()


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DownloadPolicyError(f"cannot read origin manifest {path}: {exc}") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise DownloadPolicyError("origin manifest must use schema_version 1")
    return value


def source_line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def clean_url(raw: str) -> str:
    return raw.rstrip(".,;:")


def is_dependency_url(path: str, value: str) -> bool:
    if path in NON_DOWNLOAD_LITERAL_PATHS:
        return False
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").lower()
    url_path = parsed.path.lower()
    if hostname in DEPENDENCY_HOSTS:
        return True
    if hostname in {"modelscope.cn", "www.modelscope.cn"} and url_path.startswith("/models/"):
        return True
    if hostname == "npmmirror.com" and url_path.startswith("/mirrors/"):
        return True
    return any(hint in url_path for hint in DEPENDENCY_PATH_HINTS)


def executable_lines(path: str, source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    if path.endswith(".py"):
        docstring_lines: set[int] = set()
        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                body = getattr(node, "body", None)
                if not isinstance(body, list) or not body:
                    continue
                first = body[0]
                if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant) or not isinstance(first.value.value, str):
                    continue
                start = getattr(first, "lineno", 0)
                end = getattr(first, "end_lineno", start)
                docstring_lines.update(range(start, end + 1))
        return [
            (number, line)
            for number, line in enumerate(lines, start=1)
            if number not in docstring_lines and not line.lstrip().startswith("#")
        ]
    return [
        (number, line)
        for number, line in enumerate(lines, start=1)
        if not line.lstrip().startswith(("#", "//"))
    ]


def is_account_url(value: str, account_origins: tuple[str, ...]) -> bool:
    return any(value == origin or value.startswith(origin + "/") for origin in account_origins)


def discover_sinks(
    path: str,
    source: str,
    *,
    account_origins: tuple[str, ...],
) -> tuple[list[Sink], list[str]]:
    sinks: list[Sink] = []
    violations: list[str] = []
    for number, line in executable_lines(path, source):
        if path.endswith((".sh", ".ps1")) and (
            REMOTE_SHELL_RE.search(line) or REMOTE_POWERSHELL_RE.search(line)
        ):
            violations.append(f"{path}:{number}:{line.strip()}")

    for match in URL_RE.finditer(source):
        value = clean_url(match.group(0))
        if is_account_url(value, account_origins):
            continue
        if not is_dependency_url(path, value):
            continue
        sinks.append(Sink(path, "literal-url", value, source_line(source, match.start())))

    for pattern in MODEL_PATTERNS:
        for match in pattern.finditer(source):
            sinks.append(
                Sink(path, "model-id", match.group(1), source_line(source, match.start()))
            )

    for match in PACKAGE_MANAGER_RE.finditer(source):
        sinks.append(
            Sink(
                path,
                "package-manager",
                "uv tool install browser-use",
                source_line(source, match.start()),
            )
        )
    return sorted(set(sinks)), violations


def local_sources(repo: Path) -> dict[str, str]:
    sources: dict[str, str] = {}
    for path in sorted(SCAN_EXACT):
        target = repo / path
        if not target.is_file():
            continue
        try:
            sources[path] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise DownloadPolicyError(f"cannot read managed dependency path {path}: {exc}") from exc
    return sources


def ref_sources(repo: Path, ref: str) -> dict[str, str]:
    git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
    sources: dict[str, str] = {}
    for path in sorted(SCAN_EXACT):
        probe = subprocess.run(
            ("git", "cat-file", "-e", f"{ref}:{path}"),
            cwd=repo,
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            continue
        sources[path] = git(repo, "show", f"{ref}:{path}")
    return sources


def inventory(
    source_sets: list[dict[str, str]],
    *,
    account_origins: tuple[str, ...],
) -> tuple[list[Sink], list[str]]:
    sinks: set[Sink] = set()
    violations: set[str] = set()
    for sources in source_sets:
        for path, source in sources.items():
            discovered, source_violations = discover_sinks(
                path,
                source,
                account_origins=account_origins,
            )
            sinks.update(discovered)
            violations.update(source_violations)
    return sorted(sinks), sorted(violations)


def caller_key(raw: object) -> tuple[str, str, str]:
    if not isinstance(raw, dict):
        raise DownloadPolicyError("manifest callers must be objects")
    path = normalize_path(raw.get("path"), field="caller path")
    kind = raw.get("kind")
    value = raw.get("value")
    if kind not in {"literal-url", "model-id", "package-manager"}:
        raise DownloadPolicyError(f"invalid caller kind for {path}: {kind!r}")
    if not isinstance(value, str) or not value:
        raise DownloadPolicyError(f"invalid caller value for {path}")
    return path, kind, value


def validate_entries(
    manifest: dict[str, Any],
    sinks: list[Sink],
    sources: dict[str, str],
    temporary_legacy_unmanaged_children: set[tuple[str, str, str]],
) -> list[str]:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise DownloadPolicyError("manifest entries must be a list")
    violations: list[str] = []
    discovered = {sink.key(): sink for sink in sinks}
    registered: dict[tuple[str, str, str], str] = {}
    seen_ids: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise DownloadPolicyError("manifest entries must be objects")
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id or entry_id in seen_ids:
            raise DownloadPolicyError(f"invalid or duplicate manifest id: {entry_id!r}")
        seen_ids.add(entry_id)
        for field in ("integrity", "idle_timeout_seconds", "total_timeout_seconds"):
            if entry.get(field) in {None, "", 0}:
                violations.append(f"{entry_id}: {field} is required")
        phases = entry.get("phases")
        environment = entry.get("environment")
        owners = entry.get("owners")
        if not isinstance(phases, list) or not phases:
            violations.append(f"{entry_id}: phases are required")
        if not isinstance(environment, list):
            violations.append(f"{entry_id}: environment must be a list")
        if not isinstance(owners, list) or not owners:
            violations.append(f"{entry_id}: owners are required")

        delivery = entry.get("delivery")
        callers = entry.get("callers", [])
        packaged_outputs = entry.get("packaged_outputs", [])
        if not isinstance(callers, list) or not isinstance(packaged_outputs, list):
            violations.append(f"{entry_id}: callers and packaged_outputs must be lists")
            continue
        output_paths = [normalize_path(path, field=f"{entry_id} packaged output") for path in packaged_outputs]

        if delivery == "domestic-first":
            primary = entry.get("domestic_primary")
            if not isinstance(primary, str) or not primary.startswith("https://"):
                violations.append(f"{entry_id}: domestic_primary is required")
            elif "github.com" in primary or "raw.githubusercontent.com" in primary:
                violations.append(f"{entry_id}: GitHub cannot be a domestic primary")
        elif delivery == "bundled":
            if any(entry.get(field) is not None for field in ("domestic_primary", "domestic_secondary", "official_fallback")):
                violations.append(f"{entry_id}: bundled delivery cannot define runtime origins")
            if not entry.get("build_provenance"):
                violations.append(f"{entry_id}: build_provenance is required")
            sha = entry.get("sha256")
            sha_manifest_field = entry.get("sha256_manifest_field")
            has_fixed_sha = (
                isinstance(sha, str)
                and len(sha) == 64
                and all(character in "0123456789abcdef" for character in sha)
            )
            has_manifest_sha = (
                isinstance(sha_manifest_field, str)
                and sha_manifest_field.endswith(".sha256")
                and "#" in sha_manifest_field
            )
            if not has_fixed_sha and not has_manifest_sha:
                violations.append(f"{entry_id}: sha256 is required")
            if not output_paths:
                violations.append(f"{entry_id}: packaged_outputs are required")
        else:
            violations.append(f"{entry_id}: invalid delivery {delivery!r}")

        matched = False
        for raw_caller in callers:
            key = caller_key(raw_caller)
            if key in registered:
                violations.append(
                    f"caller registered by both {registered[key]} and {entry_id}: {key[0]}"
                )
            registered[key] = entry_id
            if key in discovered:
                matched = True
        if callers and not matched and not output_paths:
            violations.append(f"{entry_id}: orphan manifest entry")
        if not callers and not output_paths:
            violations.append(f"{entry_id}: orphan manifest entry")

        if delivery == "domestic-first":
            primary = entry.get("domestic_primary")
            secondary = entry.get("domestic_secondary")
            official = entry.get("official_fallback")
            for owner in owners if isinstance(owners, list) else []:
                if not isinstance(owner, str) or owner not in sources:
                    continue
                source = sources[owner]
                official_positions = [source.find(value) for value in (official,) if isinstance(value, str) and source.find(value) >= 0]
                domestic_positions = [source.find(value) for value in (primary, secondary) if isinstance(value, str) and source.find(value) >= 0]
                if official_positions and domestic_positions and min(official_positions) < min(domestic_positions):
                    violations.append(f"{entry_id}: domestic origin must precede official fallback in {owner}")

    for sink in sinks:
        if sink.key() not in registered:
            violations.append(
                f"unclassified {sink.kind} caller: {sink.path}:{sink.line}: {sink.value}"
            )
        if sink.kind == "package-manager":
            source = sources.get(sink.path, "")
            if (
                not any(marker in source for marker in POLICY_ENV_MARKERS)
                and sink.key() not in temporary_legacy_unmanaged_children
            ):
                violations.append(
                    f"child installer lacks sanitized managed environment: {sink.path}:{sink.line}"
                )
    for orphan in sorted(temporary_legacy_unmanaged_children - set(discovered)):
        violations.append(
            "orphan temporary legacy unmanaged child caller: "
            + " | ".join(orphan)
        )
    return violations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--inventory", action="store_true")
    parser.add_argument("--source-ref", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    manifest_path = (args.manifest or repo / "docs/security/hermes-managed-download-origins.json").resolve()
    try:
        source_sets = [local_sources(repo)]
        source_sets.extend(ref_sources(repo, ref) for ref in args.source_ref)
        if args.inventory:
            sinks, violations = inventory(
                source_sets,
                account_origins=DEFAULT_ACCOUNT_ORIGINS,
            )
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sinks": [sink.as_json() for sink in sinks],
                        "unsafe_remote_execution": violations,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        manifest = load_manifest(manifest_path)
        raw_origins = manifest.get("account_server_origins", DEFAULT_ACCOUNT_ORIGINS)
        if not isinstance(raw_origins, list) or not all(
            isinstance(origin, str) and origin.startswith("https://") for origin in raw_origins
        ):
            raise DownloadPolicyError("account_server_origins must be HTTPS URLs")
        account_origins = tuple(raw_origins)
        sources = source_sets[0]
        sinks, violations = inventory([sources], account_origins=account_origins)
        legacy_unsafe = manifest.get("temporary_legacy_unsafe_callers", [])
        if not isinstance(legacy_unsafe, list) or not all(
            isinstance(item, str) and item for item in legacy_unsafe
        ):
            raise DownloadPolicyError("temporary_legacy_unsafe_callers must be a string list")
        discovered_unsafe = set(violations)
        declared_unsafe = set(legacy_unsafe)
        violations = sorted(discovered_unsafe - declared_unsafe)
        for orphan in sorted(declared_unsafe - discovered_unsafe):
            violations.append(f"orphan temporary legacy unsafe caller: {orphan}")
        raw_legacy_children = manifest.get("temporary_legacy_unmanaged_child_callers", [])
        if not isinstance(raw_legacy_children, list):
            raise DownloadPolicyError(
                "temporary_legacy_unmanaged_child_callers must be a caller list"
            )
        legacy_children = {caller_key(item) for item in raw_legacy_children}
        violations.extend(validate_entries(manifest, sinks, sources, legacy_children))
    except DownloadPolicyError as exc:
        print(f"hermes managed downloads: {exc}", file=sys.stderr)
        return 1

    if violations:
        for violation in sorted(set(violations)):
            print(f"hermes managed downloads: {violation}", file=sys.stderr)
        return 1

    print("hermes managed downloads: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
