from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _manifest(scope: str) -> list[dict[str, object]]:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--manifest", "--bootstrap-scope", scope],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["bootstrap_scope"] == scope
    return payload["stages"]


def test_auth_scope_contains_only_the_minimal_auth_runtime() -> None:
    stages = _manifest("auth")
    names = [stage["name"] for stage in stages]

    assert names == [
        "auth-prerequisites",
        "repository",
        "venv",
        "python-auth-deps",
        "auth-complete",
    ]
    assert not {
        "python-deps",
        "node-deps",
        "path",
        "config",
        "setup",
        "gateway",
        "desktop",
        "complete",
    }.intersection(names)
    assert all(stage["needs_user_input"] is False for stage in stages)


def test_runtime_scope_keeps_the_full_runtime_manifest() -> None:
    names = [stage["name"] for stage in _manifest("runtime")]

    assert names[:5] == [
        "prerequisites",
        "repository",
        "venv",
        "python-deps",
        "node-deps",
    ]
    assert names[-1] == "complete"
    assert {"path", "config", "setup", "gateway"}.issubset(names)


def test_unknown_bootstrap_scope_is_rejected() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--manifest", "--bootstrap-scope", "unknown"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unknown bootstrap scope" in result.stderr.lower()


def test_bundled_runtime_lock_failure_never_falls_back_to_unlocked_pip(tmp_path: Path) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "home"
    uv_path = hermes_home / "bin" / "uv"
    uv_log = tmp_path / "uv.log"
    venv_python = install_dir / "venv" / "bin" / "python"

    uv_path.parent.mkdir(parents=True)
    venv_python.parent.mkdir(parents=True)
    install_dir.joinpath("pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0.0.0"\n', encoding="utf-8"
    )
    install_dir.joinpath("uv.lock").write_text("version = 1\n", encoding="utf-8")
    install_dir.joinpath("uv.toml").write_text(
        'exclude-newer = "2026-08-19T00:00:00Z"\n', encoding="utf-8"
    )
    os.symlink(sys.executable, venv_python)
    uv_path.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(uv_log))}",
                'if [ "$1" = "--version" ]; then echo "uv 0.12.5"; exit 0; fi',
                f'if [ "$1 $2" = "python find" ]; then echo {shlex.quote(sys.executable)}; exit 0; fi',
                'if [ "$1 $2" = "python install" ]; then exit 0; fi',
                'if [ "$1" = "sync" ]; then exit 42; fi',
                'if [ "$1" = "pip" ]; then exit 0; fi',
                "exit 0",
            ]
        ),
        encoding="utf-8",
    )
    uv_path.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "python-deps",
            "--bundled-source",
            "--bootstrap-scope",
            "runtime",
            "--non-interactive",
            "--json",
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert any(call.startswith("sync ") for call in calls)
    assert any("--config-file" in call and str(install_dir / "uv.toml") in call for call in calls)
    assert not any(call.startswith("pip ") for call in calls)
