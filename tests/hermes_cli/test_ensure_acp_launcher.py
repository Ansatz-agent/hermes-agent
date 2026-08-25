"""`ansatz update` must self-heal the public launcher family.

ACP hosts (Zed, JetBrains, Buzz Desktop) resolve the agent by the
canonical and legacy command names on the login-shell PATH. Fresh installs get
the launchers from ``scripts/install.sh``; existing installs get them from
``_ensure_acp_launcher()`` during ``ansatz update``.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

from hermes_cli.main import _ensure_acp_launcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir


@pytest.fixture
def venv_console_scripts(tmp_path, monkeypatch):
    """Install executable stand-ins for the six scripts beside venv Python."""
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    for name in (
        "ansatz",
        "ansatz-agent",
        "ansatz-acp",
        "hermes",
        "hermes-agent",
        "hermes-acp",
    ):
        script = venv_bin / name
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{name}'\n", encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setattr("hermes_cli.main.sys.executable", str(python))
    return venv_bin








def test_does_not_follow_symlink_into_venv(fake_home, tmp_path):
    """#21454 failure mode: never write through a symlinked hermes-acp."""
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    console_script = tmp_path / "venv" / "bin" / "hermes-acp"
    console_script.parent.mkdir(parents=True)
    marker = "#!/usr/bin/env python\n# real console script\n"
    console_script.write_text(marker, encoding="utf-8")
    (fake_home / "hermes-acp").symlink_to(console_script)

    _ensure_acp_launcher()

    assert console_script.read_text(encoding="utf-8") == marker
    assert (fake_home / "hermes-acp").is_symlink()


def test_legacy_only_install_gains_canonical_and_missing_compatibility_launchers(
    fake_home,
    tmp_path,
):
    """A legacy-only PATH install becomes a complete, safe six-command family."""
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    unrelated = fake_home / "custom-tool"
    unrelated.write_text("do not replace\n", encoding="utf-8")
    real_legacy_acp = tmp_path / "venv" / "bin" / "hermes-acp"
    real_legacy_acp.parent.mkdir(parents=True)
    real_legacy_acp.write_text("#!/bin/sh\n", encoding="utf-8")
    (fake_home / "hermes-acp").symlink_to(real_legacy_acp)

    _ensure_acp_launcher()

    for launcher in ("ansatz", "ansatz-agent", "ansatz-acp", "hermes-agent"):
        path = fake_home / launcher
        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR
    assert (fake_home / "hermes-acp").is_symlink()
    assert unrelated.read_text(encoding="utf-8") == "do not replace\n"


def test_legacy_only_fallback_preserves_the_acp_subcommand_for_both_names(
    fake_home,
    tmp_path,
    monkeypatch,
):
    """Both repaired ACP wrappers must add ``acp`` to the legacy launcher."""
    empty_venv_python = tmp_path / "empty-venv" / "bin" / "python"
    monkeypatch.setattr("hermes_cli.main.sys.executable", str(empty_venv_python))
    legacy = fake_home / "hermes"
    legacy.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\"\n", encoding="utf-8")
    legacy.chmod(0o755)

    _ensure_acp_launcher()

    for launcher_name in ("ansatz-acp", "hermes-acp"):
        launcher = fake_home / launcher_name
        assert f'exec "{legacy}" "acp" "$@"' in launcher.read_text(encoding="utf-8")
        result = subprocess.run(
            [launcher, "--stdio"], check=True, capture_output=True, text=True
        )
        assert result.stdout.strip() == "acp --stdio"


def test_repair_uses_verified_venv_scripts_not_an_occupied_canonical_path(
    fake_home,
    venv_console_scripts,
):
    """Never point a repaired alias at an unrelated PATH `ansatz` file."""
    occupied_ansatz = fake_home / "ansatz"
    occupied_ansatz.write_text("unrelated ansatz\n", encoding="utf-8")

    _ensure_acp_launcher()

    assert occupied_ansatz.read_text(encoding="utf-8") == "unrelated ansatz\n"
    for name in (
        "ansatz-agent",
        "ansatz-acp",
        "hermes",
        "hermes-agent",
        "hermes-acp",
    ):
        launcher = fake_home / name
        expected_target = venv_console_scripts / name
        assert f'exec "{expected_target}"' in launcher.read_text(encoding="utf-8")
        result = subprocess.run([launcher], check=True, capture_output=True, text=True)
        assert result.stdout.strip() == name


def test_repair_preserves_unrelated_and_symlinked_family_members(
    fake_home,
    venv_console_scripts,
    tmp_path,
):
    """Existing occupied names are not launcher-repair ownership targets."""
    (fake_home / "hermes").write_text("legacy fallback\n", encoding="utf-8")
    unrelated_agent = fake_home / "ansatz-agent"
    unrelated_agent.write_text("leave me alone\n", encoding="utf-8")
    acp_target = tmp_path / "outside" / "ansatz-acp"
    acp_target.parent.mkdir(parents=True)
    acp_target.write_text("outside target\n", encoding="utf-8")
    (fake_home / "ansatz-acp").symlink_to(acp_target)

    _ensure_acp_launcher()

    assert unrelated_agent.read_text(encoding="utf-8") == "leave me alone\n"
    assert (fake_home / "ansatz-acp").is_symlink()
    assert acp_target.read_text(encoding="utf-8") == "outside target\n"
    assert (fake_home / "ansatz").is_file()
    assert (fake_home / "hermes-agent").is_file()


def test_repair_uses_exclusive_creation_when_a_name_appears_mid_repair(
    fake_home,
    venv_console_scripts,
    monkeypatch,
):
    """A racing file wins; repair neither overwrites it nor follows it."""
    (fake_home / "hermes").write_text("legacy fallback\n", encoding="utf-8")
    raced = fake_home / "ansatz"
    original_open = Path.open

    def create_racer_then_open(path, mode="r", *args, **kwargs):
        if path == raced and mode == "x":
            with original_open(raced, "w", encoding="utf-8") as handle:
                handle.write("created by racer\n")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", create_racer_then_open)

    _ensure_acp_launcher()

    assert raced.read_text(encoding="utf-8") == "created by racer\n"
    assert (fake_home / "ansatz-agent").is_file()


def test_repair_sets_permissions_on_the_created_file_descriptor(
    fake_home,
    venv_console_scripts,
    monkeypatch,
):
    """A post-create path swap cannot redirect the permission change."""
    (fake_home / "hermes").write_text("legacy fallback\n", encoding="utf-8")

    monkeypatch.setattr(
        Path,
        "chmod",
        lambda *_args, **_kwargs: pytest.fail("repair must not chmod by pathname"),
    )

    _ensure_acp_launcher()

    assert (fake_home / "ansatz").stat().st_mode & stat.S_IXUSR







def test_unwritable_bin_dir_is_skipped(fake_home):
    (fake_home / "hermes").write_text("#!/bin/sh\n", encoding="utf-8")
    if os.geteuid() == 0:
        pytest.skip("root ignores directory write permissions")
    fake_home.chmod(0o555)
    try:
        _ensure_acp_launcher()  # must not raise
        assert not (fake_home / "hermes-acp").exists()
    finally:
        fake_home.chmod(0o755)
