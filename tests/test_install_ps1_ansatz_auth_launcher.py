from pathlib import Path
import re


INSTALL_PS1 = Path(__file__).resolve().parents[1] / "scripts" / "install.ps1"


def test_auth_bootstrap_publishes_canonical_and_legacy_windows_launchers():
    source = INSTALL_PS1.read_text(encoding="utf-8")

    assert '$launcherPath = Join-Path $binDir "ansatz.cmd"' in source
    assert '$legacyLauncherPath = Join-Path $binDir "hermes.cmd"' in source
    assert '%~dp0ansatz.cmd`" %*' in source


def test_normal_path_install_publishes_the_complete_console_script_family():
    source = INSTALL_PS1.read_text(encoding="utf-8")

    expected = (
        "ansatz.exe",
        "ansatz-agent.exe",
        "ansatz-acp.exe",
        "hermes.exe",
        "hermes-agent.exe",
        "hermes-acp.exe",
    )
    stage_start = source.index("function Set-PathVariable")
    stage_end = source.index("function Write-AuthBootstrapComplete", stage_start)
    path_stage = source[stage_start:stage_end]
    loop = re.search(
        r'foreach \(\$launcher in @\((?P<launchers>[^)]*)\)\) \{\s*'
        r'\$src = "\$InstallDir\\venv\\Scripts\\\$launcher"\s*'
        r'if \(Test-Path \$src\) \{\s*'
        r'Copy-Item -Force \$src "\$hermesBin\\\$launcher"',
        path_stage,
    )
    assert loop is not None
    assert tuple(re.findall(r'"([^"]+)"', loop.group("launchers"))) == expected
