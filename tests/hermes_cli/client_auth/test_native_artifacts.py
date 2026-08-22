from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "check_auth_native_artifacts.py"
WRITER = REPO_ROOT / "scripts" / "write_auth_native_artifact.py"


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


def _junit(
    path: Path,
    *,
    tests: int,
    failures: int = 0,
    errors: int = 0,
    skipped: int = 0,
    cases: tuple[str, ...] = (),
) -> None:
    case_xml = "".join(f'<testcase name="{name}" />' for name in cases)
    path.write_text(
        (
            '<testsuites><testsuite name="pytest" '
            f'tests="{tests}" failures="{failures}" errors="{errors}" '
            f'skipped="{skipped}">{case_xml}</testsuite></testsuites>'
        ),
        encoding="utf-8",
    )


def _run_writer(
    output: Path,
    locked_start: Path,
    handle_noninheritance: Path,
    service_locked_waiting: Path,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(WRITER),
            "--platform",
            "linux",
            "--owner-transport",
            "unix-peercred",
            "--locked-start",
            str(locked_start),
            "--handle-noninheriting",
            str(handle_noninheritance),
            "--service-waiting",
            str(service_locked_waiting),
            "--output",
            str(output),
        ],
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


def test_native_artifact_writer_derives_green_evidence_from_junit(tmp_path: Path):
    locked = tmp_path / "locked.xml"
    handles = tmp_path / "handles.xml"
    services = tmp_path / "services.xml"
    output = tmp_path / "linux.json"
    _junit(
        locked,
        tests=3,
        skipped=1,
        cases=(
            "test_console_wrappers_guard_before_capability_target_import",
            "test_every_guarded_python_entry_exits_locked_before_capability_imports",
        ),
    )
    _junit(
        handles,
        tests=4,
        cases=(
            "test_linux_child_cannot_inherit_owner_connection",
            "test_linux_unix_runtime_endpoint_enforces_permissions_and_peer_uid",
        ),
    )
    _junit(
        services,
        tests=5,
        skipped=2,
        cases=(
            "test_locked_waiting_never_prompts_and_authorizes_before_return",
            "test_s6_lifecycle_starts_only_desired_slots_and_locks_all",
            "test_s6_lifecycle_suppresses_named_gateways_when_default_multiplexes",
        ),
    )

    result = _run_writer(output, locked, handles, services)

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "platform": "linux",
        "owner_transport": "unix-peercred",
        "locked_start_passed": True,
        "handle_noninheritance_passed": True,
        "service_locked_waiting_passed": True,
    }


def test_native_artifact_writer_rejects_all_skipped_or_failed_junit(tmp_path: Path):
    locked = tmp_path / "locked.xml"
    handles = tmp_path / "handles.xml"
    services = tmp_path / "services.xml"
    output = tmp_path / "linux.json"
    _junit(locked, tests=2, skipped=2)
    _junit(handles, tests=1)
    _junit(services, tests=2, failures=1)

    result = _run_writer(output, locked, handles, services)

    assert result.returncode != 0
    assert not output.exists()
    assert "no passing tests" in result.stderr


def test_native_matrix_can_collect_modules_that_import_cli_main():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/hermes_cli/test_gui_command.py",
            "-m",
            "macos_only and not integration",
            "--collect-only",
            "-q",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "test_electron_dist_binary_basename_macos" in result.stdout
    assert "AUTH_REQUIRED" not in result.stderr
