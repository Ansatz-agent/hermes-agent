#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
import tomllib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "hermes_cli" / "client_auth" / "entrypoints.json"

_EXCLUDED_ANYWHERE = frozenset(
    {
        ".git",
        ".venv",
        "build",
        "dist",
        "node_modules",
        "test",
        "tests",
        "__tests__",
    }
)
_SOURCE_ONLY_PREFIXES = (("apps", "desktop", "scripts"),)
_SOURCE_ONLY_ROOTS = frozenset(
    {
        "evals",
        "optional-skills",
        "scripts",
        "skills",
        "tests",
        "website",
    }
)
_INSTALLERS = frozenset(
    {
        "scripts/hermes-gateway",
        "scripts/install.ps1",
        "scripts/install.sh",
    }
)
_PACKAGED_RUNTIME_SCRIPTS = frozenset(
    {
        "scripts/discord-voice-doctor.py",
        "scripts/keystroke_diagnostic.py",
    }
)
_SPAWN_MODULE = re.compile(
    r"\b(?:spawn|execFile)\(\s*['\"]python(?:3)?['\"]\s*,\s*"
    r"\[\s*['\"]-m['\"]\s*,\s*['\"]([^'\"]+)['\"]"
)


def scan_entrypoints(root: Path) -> set[str]:
    return _scan_entrypoints(root, source_tree=True)


def scan_distribution_entrypoints(root: Path) -> set[str]:
    return _scan_entrypoints(root, source_tree=False)


def _scan_entrypoints(root: Path, *, source_tree: bool) -> set[str]:
    root = root.resolve()
    found: set[str] = set()
    found.update(_scan_pyproject(root))
    found.update(_scan_python(root, source_tree=source_tree))
    found.update(_scan_shell_and_services(root, source_tree=source_tree))
    found.update(_scan_dockerfiles(root, source_tree=source_tree))
    found.update(_scan_ui(root, source_tree=source_tree))
    return found


def _scan_pyproject(root: Path) -> set[str]:
    path = root / "pyproject.toml"
    if not path.is_file():
        return set()
    try:
        project = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return set()
    scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
    if not isinstance(scripts, dict):
        return set()
    return {
        f"pyproject:{name}"
        for name, target in scripts.items()
        if isinstance(name, str) and name and isinstance(target, str) and target
    }


def _scan_python(root: Path, *, source_tree: bool) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.py"):
        relative = path.relative_to(root)
        if _excluded(relative, source_tree=source_tree) or path.name == "registration_lifecycle.py":
            continue
        if path.name == "__main__.py" or _has_main_guard(path):
            found.add(f"python:{relative.as_posix()}")
    return found


def _has_main_guard(path: Path) -> bool:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError, UnicodeError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if not isinstance(test, ast.Compare) or len(test.ops) != 1:
            continue
        if not isinstance(test.ops[0], ast.Eq) or len(test.comparators) != 1:
            continue
        values = (test.left, test.comparators[0])
        if any(
            isinstance(value, ast.Name) and value.id == "__name__"
            for value in values
        ) and any(
            isinstance(value, ast.Constant) and value.value == "__main__"
            for value in values
        ):
            return True
    return False


def _scan_shell_and_services(root: Path, *, source_tree: bool) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        normalized = relative.as_posix()
        if normalized in _INSTALLERS:
            found.add(f"installer:{normalized}")
            continue
        if _excluded(relative, source_tree=source_tree):
            continue
        if path.suffix == ".service":
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if any(line.strip().startswith("ExecStart=") for line in text.splitlines()):
                found.add(f"service:{normalized}")
            continue
        if path.name == "run" and "s6-rc.d" in relative.parts:
            found.add(f"s6:{normalized}")
            continue
        if path.suffix not in {".sh", ".bash"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if text.startswith("#!") and re.search(
            r"\bexec\s+(?:python(?:3)?|hermes(?:-agent|-acp)?)\b",
            text,
        ):
            found.add(f"shell:{normalized}")
    return found


def _scan_dockerfiles(root: Path, *, source_tree: bool) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("Dockerfile*"):
        if not path.is_file() or _excluded(
            path.relative_to(root), source_tree=source_tree
        ):
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        relative = path.relative_to(root).as_posix()
        for directive in ("ENTRYPOINT", "CMD"):
            if any(line.lstrip().upper().startswith(directive) for line in lines):
                found.add(f"docker:{relative}:{directive.casefold()}")
    return found


def _scan_ui(root: Path, *, source_tree: bool) -> set[str]:
    found: set[str] = set()
    desktop_package = root / "apps" / "desktop" / "package.json"
    if desktop_package.is_file():
        try:
            payload = json.loads(desktop_package.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("main"), str):
            found.add("electron:primary-backend")
    if (root / "ui-tui" / "package.json").is_file() and (
        root / "tui_gateway" / "entry.py"
    ).is_file():
        found.add("tui:tui-gateway")
    for suffix in ("*.ts", "*.js", "*.mjs", "*.cjs"):
        for path in root.rglob(suffix):
            relative = path.relative_to(root)
            if _excluded(relative, source_tree=source_tree):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            for module in _SPAWN_MODULE.findall(text):
                found.add(f"spawn:{relative.as_posix()}:{module}")
    return found


def _excluded(relative: Path, *, source_tree: bool) -> bool:
    parts = relative.parts
    if any(part in _EXCLUDED_ANYWHERE or part.startswith(".") for part in parts):
        return True
    if not source_tree or not parts:
        return False
    if relative.as_posix() in _PACKAGED_RUNTIME_SCRIPTS:
        return False
    if parts[0] in _SOURCE_ONLY_ROOTS:
        return True
    return any(parts[: len(prefix)] == prefix for prefix in _SOURCE_ONLY_PREFIXES)


def _manifest_payload(ids: set[str]) -> dict[str, object]:
    entries = []
    for entry_id in sorted(ids):
        if entry_id in {
            "installer:scripts/install.ps1",
            "installer:scripts/install.sh",
        }:
            startup = "distribution-bootstrap"
        elif (
            entry_id.startswith(("electron:", "tui:"))
            or entry_id.endswith("hermes_cli/client_auth/bridge.py")
            or entry_id.endswith("hermes_cli/client_auth/runtime.py")
            or entry_id == "python:hermes_cli/container_boot.py"
            or entry_id == "python:tui_gateway/entry.py"
        ):
            startup = "auth-shell"
        elif entry_id.startswith(("docker:", "s6:", "service:")):
            startup = "locked-waiting"
        else:
            startup = "guarded"
        interactive = entry_id in {
            "pyproject:ansatz",
            "pyproject:hermes",
            "python:cli.py",
            "python:hermes_cli/main.py",
            "tui:tui-gateway",
        }
        entries.append(
            {
                "id": entry_id,
                "interactive": interactive,
                "startup": startup,
            }
        )
    return {
        "version": 1,
        "notes": {
            "registration_lifecycle.py": (
                "provider/plugin replacement ownership; not user-account registration"
            )
        },
        "entrypoints": entries,
    }


def _check_manifest(root: Path) -> int:
    expected = _manifest_payload(scan_entrypoints(root))
    try:
        actual = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        actual = None
    if actual != expected:
        print(
            "auth entrypoint manifest is stale; run "
            "scripts/check_auth_entrypoints.py --write",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--scan-distribution", metavar="ROOT", type=Path)
    args = parser.parse_args()
    if args.scan_distribution is not None:
        for entry_id in sorted(scan_distribution_entrypoints(args.scan_distribution)):
            print(entry_id)
        return 0
    if args.write:
        payload = _manifest_payload(scan_entrypoints(REPO_ROOT))
        MANIFEST.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return 0
    return _check_manifest(REPO_ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
