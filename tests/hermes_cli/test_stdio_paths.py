"""Windows PATH augmentation for product-scoped Hermes runtimes."""

from __future__ import annotations

import os

import pytest


@pytest.mark.windows_only
def test_configured_hermes_home_git_dirs_precede_legacy_path(tmp_path, monkeypatch):
    """A fresh Ansatz Git install is prepended before the legacy Hermes tree."""
    from hermes_cli import stdio

    configured_home = tmp_path / "AnsatzVoiceTraceClient"
    legacy_home = tmp_path / "legacy" / "hermes"
    configured_dirs = [
        configured_home / "git" / "cmd",
        configured_home / "git" / "bin",
        configured_home / "git" / "usr" / "bin",
    ]
    legacy_dirs = [
        legacy_home / "git" / "cmd",
        legacy_home / "git" / "bin",
        legacy_home / "git" / "usr" / "bin",
    ]
    for directory in [*configured_dirs, *legacy_dirs]:
        directory.mkdir(parents=True)

    existing = tmp_path / "existing"
    existing.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(configured_home))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "legacy"))
    monkeypatch.setenv("PATH", str(existing))

    stdio._augment_path_with_known_tools()
    entries = os.environ["PATH"].split(os.pathsep)

    expected_prefix = [*(str(path) for path in configured_dirs), *(str(path) for path in legacy_dirs)]
    assert entries[: len(expected_prefix)] == expected_prefix
    assert entries[len(expected_prefix)] == str(existing)

    # Re-running after startup must not duplicate any managed directory.
    stdio._augment_path_with_known_tools()
    assert os.environ["PATH"].split(os.pathsep) == entries
