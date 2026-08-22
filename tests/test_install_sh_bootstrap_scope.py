from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tarfile
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


def test_auth_complete_publishes_cli_launchers_in_a_clean_home(tmp_path: Path) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "hermes-home"
    shell_home = tmp_path / "shell-home"
    venv_python = install_dir / "venv" / "bin" / "python"

    venv_python.parent.mkdir(parents=True)
    shell_home.mkdir()
    os.symlink(sys.executable, venv_python)
    install_dir.joinpath("hermes").write_text(
        "#!/usr/bin/env python3\nraise SystemExit(0)\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env.update({"HOME": str(shell_home), "SHELL": "/bin/zsh"})
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "auth-complete",
            "--bundled-source",
            "--bootstrap-scope",
            "auth",
            "--non-interactive",
            "--json",
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    command_dir = shell_home / ".local" / "bin"
    for command in ("hermes", "hermes-agent", "hermes-acp"):
        launcher = command_dir / command
        assert launcher.is_file(), f"auth bootstrap did not publish {command}"
        assert str(install_dir) in launcher.read_text(encoding="utf-8")
    assert install_dir.joinpath(".hermes-auth-bootstrap-complete").is_file()


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

    env = os.environ.copy()
    env.update(
        {
            "UV_DEFAULT_INDEX": "https://mirrors.ustc.edu.cn/pypi/simple",
            "HERMES_UV_FALLBACK_INDEX": "https://pypi.tuna.tsinghua.edu.cn/simple",
            "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
            "HERMES_NODE_MIRROR": "https://registry.npmmirror.com/-/binary/node/",
            "PLAYWRIGHT_DOWNLOAD_HOST": "https://registry.npmmirror.com/-/binary/playwright/",
        }
    )

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
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    calls = uv_log.read_text(encoding="utf-8").splitlines()
    assert any(call.startswith("sync ") for call in calls)
    assert any("--config-file" in call and str(install_dir / "uv.toml") in call for call in calls)
    assert not any(call.startswith("pip ") for call in calls)


def test_bundled_auth_prerequisites_install_uv_and_python_without_network(tmp_path: Path) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "hermes-home"
    shell_home = tmp_path / "shell-home"
    toolchain = tmp_path / "auth-toolchain"
    python_source = tmp_path / "python-source" / "cpython-3.11.16-macos-aarch64-none"
    python_executable = python_source / "bin" / "python3.11"
    wheelhouse = toolchain / "wheelhouse"
    stub_bin = tmp_path / "stub-bin"
    network_log = tmp_path / "network.log"

    for directory in (install_dir, hermes_home, shell_home, wheelhouse, stub_bin):
        directory.mkdir(parents=True, exist_ok=True)
    python_executable.parent.mkdir(parents=True)
    python_executable.write_text(
        '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "Python 3.11.16"; fi\nexit 0\n',
        encoding="utf-8",
    )
    python_executable.chmod(0o755)
    with tarfile.open(toolchain / "python.tar.gz", "w:gz") as archive:
        archive.add(python_source, arcname=python_source.name)

    (toolchain / "uv").write_text(
        "\n".join(
            [
                "#!/bin/bash",
                'if [ "$1" = "--version" ]; then echo "uv 0.12.5"; exit 0; fi',
                'if [ "$1 $2" = "python find" ]; then '
                'echo "$HERMES_HOME/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11"; exit 0; fi',
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (toolchain / "uv").chmod(0o755)
    (toolchain / "auth-requirements.txt").write_text(
        "httpx==0.28.1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    (wheelhouse / "httpx.whl").write_bytes(b"fixture wheel\n")
    (toolchain / "manifest.json").write_text("{}\n", encoding="utf-8")
    (stub_bin / "curl").write_text(
        f'#!/bin/bash\nprintf "curl %s\\n" "$*" >> {shlex.quote(str(network_log))}\nexit 99\n',
        encoding="utf-8",
    )
    (stub_bin / "curl").chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(shell_home),
            "SHELL": "/bin/zsh",
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
    )
    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "auth-prerequisites",
            "--bundled-source",
            "--bundled-toolchain",
            str(toolchain),
            "--bootstrap-scope",
            "auth",
            "--non-interactive",
            "--json",
            "--dir",
            str(install_dir),
            "--hermes-home",
            str(hermes_home),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not network_log.exists(), "pre-login bootstrap invoked a network client"
    assert (hermes_home / "bin" / "uv").is_file()
    assert (
        hermes_home
        / "python"
        / "cpython-3.11.16-macos-aarch64-none"
        / "bin"
        / "python3.11"
    ).is_file()


def test_bundled_auth_stages_sync_hashed_wheels_without_network(tmp_path: Path) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "hermes-home"
    shell_home = tmp_path / "shell-home"
    toolchain = tmp_path / "auth-toolchain"
    wheelhouse = toolchain / "wheelhouse"
    python_source = tmp_path / "python-source" / "cpython-3.11.16-macos-aarch64-none"
    python_executable = python_source / "bin" / "python3.11"
    stub_bin = tmp_path / "stub-bin"
    uv_log = tmp_path / "uv.log"
    network_log = tmp_path / "network.log"

    for directory in (install_dir, hermes_home, shell_home, wheelhouse, stub_bin):
        directory.mkdir(parents=True, exist_ok=True)
    python_executable.parent.mkdir(parents=True)
    python_executable.write_text(
        '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "Python 3.11.16"; fi\nexit 0\n',
        encoding="utf-8",
    )
    python_executable.chmod(0o755)
    with tarfile.open(toolchain / "python.tar.gz", "w:gz") as archive:
        archive.add(python_source, arcname=python_source.name)

    (toolchain / "uv").write_text(
        "\n".join(
            [
                "#!/bin/bash",
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(uv_log))}",
                'if [ "$1" = "--version" ]; then echo "uv 0.12.5"; exit 0; fi',
                'if [ "$1 $2" = "python find" ]; then '
                'echo "$HERMES_HOME/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11"; exit 0; fi',
                'if [ "$1" = "venv" ]; then',
                '  mkdir -p "$PWD/venv/bin"',
                "  printf '%s\\n' '#!/bin/bash' 'if [ \"$1\" = \"--version\" ]; then echo \"Python 3.11.16\"; fi' 'exit 0' > \"$PWD/venv/bin/python\"",
                '  chmod 0755 "$PWD/venv/bin/python"',
                "  exit 0",
                "fi",
                'if [ "$1 $2" = "pip sync" ]; then exit 0; fi',
                'if [ "$1" = "sync" ]; then exit 91; fi',
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (toolchain / "uv").chmod(0o755)
    requirements = toolchain / "auth-requirements.txt"
    requirements.write_text(
        "httpx==0.28.1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    (wheelhouse / "httpx.whl").write_bytes(b"fixture wheel\n")
    (toolchain / "manifest.json").write_text("{}\n", encoding="utf-8")

    auth_project = install_dir / "desktop_auth_runtime"
    auth_project.mkdir()
    auth_project.joinpath("pyproject.toml").write_text(
        '[project]\nname="fixture-auth"\nversion="0.0.0"\n', encoding="utf-8"
    )
    auth_project.joinpath("uv.lock").write_text("version = 1\n", encoding="utf-8")
    auth_project.joinpath("uv.toml").write_text(
        'exclude-newer = "2026-08-19T00:00:00Z"\n', encoding="utf-8"
    )
    install_dir.joinpath("hermes").write_text(
        "#!/usr/bin/env python3\nraise SystemExit(0)\n", encoding="utf-8"
    )

    for command in ("curl", "git"):
        command_path = stub_bin / command
        command_path.write_text(
            f'#!/bin/bash\nprintf "{command} %s\\n" "$*" >> {shlex.quote(str(network_log))}\nexit 99\n',
            encoding="utf-8",
        )
        command_path.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(shell_home),
            "SHELL": "/bin/zsh",
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
        }
    )
    common_args = [
        "bash",
        str(INSTALL_SH),
        "--bundled-source",
        "--bundled-toolchain",
        str(toolchain),
        "--bootstrap-scope",
        "auth",
        "--non-interactive",
        "--json",
        "--dir",
        str(install_dir),
        "--hermes-home",
        str(hermes_home),
    ]

    for stage in ("auth-prerequisites", "venv", "python-auth-deps", "auth-complete"):
        result = subprocess.run(
            [*common_args, "--stage", stage],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"{stage}: {result.stderr}"

    calls = uv_log.read_text(encoding="utf-8").splitlines()
    offline_sync = next(call for call in calls if call.startswith("pip sync "))
    assert str(requirements) in offline_sync
    assert f"--python {install_dir / 'venv' / 'bin' / 'python'}" in offline_sync
    assert "--require-hashes" in offline_sync
    assert "--no-index" in offline_sync
    assert f"--find-links {wheelhouse}" in offline_sync
    assert "--offline" in offline_sync
    assert not any(call.startswith("sync --project") for call in calls)
    assert not network_log.exists(), "pre-login bootstrap invoked a network client"
    assert install_dir.joinpath(".hermes-auth-bootstrap-complete").is_file()
    for command in ("hermes", "hermes-agent", "hermes-acp"):
        assert shell_home.joinpath(".local", "bin", command).is_file()
