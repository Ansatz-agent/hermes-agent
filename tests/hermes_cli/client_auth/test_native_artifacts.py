from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "check_auth_native_artifacts.py"


def _write(directory: Path, platform: str, transport: str, **overrides) -> None:
    payload = {
        "platform": platform,
        "owner_transport": transport,
        "locked_start_passed": True,
        "handle_noninheritance_passed": True,
        "service_locked_waiting_passed": True,
        **overrides,
    }
    (directory / f"{platform}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def _run(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *arguments, str(directory)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_native_artifact_checker_requires_all_real_platform_results(tmp_path: Path):
    _write(tmp_path, "linux", "unix-peercred")
    _write(tmp_path, "macos", "unix-getpeereid")
    _write(tmp_path, "windows", "named-pipe-sid")

    result = _run(tmp_path)

    assert result.returncode == 0
    assert "linux, macos, windows" in result.stdout


def test_native_artifact_checker_allows_explicit_partial_local_only(tmp_path: Path):
    _write(tmp_path, "macos", "unix-getpeereid")

    assert _run(tmp_path).returncode != 0
    assert _run(tmp_path, "--allow-partial-local").returncode == 0


def test_native_artifact_checker_rejects_false_or_wrong_transport(tmp_path: Path):
    _write(
        tmp_path,
        "linux",
        "named-pipe-sid",
        service_locked_waiting_passed=False,
    )

    result = _run(tmp_path, "--allow-partial-local")

    assert result.returncode != 0
    assert "linux.json" in result.stderr
