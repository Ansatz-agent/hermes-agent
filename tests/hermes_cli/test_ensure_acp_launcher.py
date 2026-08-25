"""`ansatz update` must self-heal the public launcher family.

ACP hosts (Zed, JetBrains, Buzz Desktop) resolve the agent by the
canonical and legacy command names on the login-shell PATH. Fresh installs get
the launchers from ``scripts/install.sh``; existing installs get them from
``_ensure_acp_launcher()`` during ``ansatz update``.
"""

import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli.main import _ensure_acp_launcher


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    bin_dir = tmp_path / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    return bin_dir








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
