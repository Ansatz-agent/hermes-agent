from pathlib import Path


INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


def test_auth_bootstrap_publishes_canonical_and_legacy_windows_launchers():
    source = INSTALL_PS1.read_text(encoding="utf-8")

    assert '$launcherPath = Join-Path $binDir "ansatz.cmd"' in source
    assert '$legacyLauncherPath = Join-Path $binDir "hermes.cmd"' in source
    assert '%~dp0ansatz.cmd`" %*' in source
