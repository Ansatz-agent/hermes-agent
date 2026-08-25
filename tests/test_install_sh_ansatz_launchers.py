from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from tests.test_install_desktop_product_launchers import (
    ANSATZ_LAUNCHERS,
    _installer_fixture,
    _make_interpreter_observable,
    _run_path_stage,
)


CANONICAL_LAUNCHERS = ("ansatz", "ansatz-agent", "ansatz-acp")
LEGACY_TO_CANONICAL = {
    "hermes": "ansatz",
    "hermes-agent": "ansatz-agent",
    "hermes-acp": "ansatz-acp",
}


def _assert_executable_launchers(command_dir: Path, names: tuple[str, ...]) -> None:
    for name in names:
        launcher = command_dir / name
        assert launcher.is_file(), f"missing {name}"
        assert launcher.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"


def test_default_install_publishes_canonical_and_legacy_families(tmp_path: Path) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product=None,
    )

    assert result.returncode == 0, result.stderr
    command_dir = shell_home / ".local" / "bin"
    _assert_executable_launchers(
        command_dir,
        (*CANONICAL_LAUNCHERS, *LEGACY_TO_CANONICAL),
    )

    for legacy, canonical in LEGACY_TO_CANONICAL.items():
        text = command_dir.joinpath(legacy).read_text(encoding="utf-8")
        assert canonical in text
        assert "-t 0" in text and "-t 2" in text


def test_desktop_product_also_publishes_canonical_family(tmp_path: Path) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)

    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product="ansatz-voice-trace",
    )

    assert result.returncode == 0, result.stderr
    command_dir = shell_home / ".local" / "bin"
    _assert_executable_launchers(
        command_dir,
        (*CANONICAL_LAUNCHERS, *ANSATZ_LAUNCHERS),
    )


def test_legacy_launchers_are_silent_for_non_interactive_invocations(
    tmp_path: Path,
) -> None:
    install_dir, hermes_home, shell_home = _installer_fixture(tmp_path)
    _make_interpreter_observable(install_dir)
    result = _run_path_stage(
        install_dir,
        hermes_home,
        shell_home,
        desktop_product=None,
    )
    assert result.returncode == 0, result.stderr

    command_dir = shell_home / ".local" / "bin"
    for legacy in LEGACY_TO_CANONICAL:
        invoked = subprocess.run(
            [str(command_dir / legacy), "probe"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert invoked.returncode == 0
        assert invoked.stdout
        assert invoked.stderr == ""
