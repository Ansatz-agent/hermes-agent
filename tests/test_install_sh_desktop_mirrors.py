from __future__ import annotations

import gzip
import os
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
PYTHON_PRIMARY = "https://mirrors.ustc.edu.cn/pypi/simple"
PYTHON_FALLBACK = "https://pypi.tuna.tsinghua.edu.cn/simple"
NPM_REGISTRY = "https://registry.npmmirror.com"
NODE_MIRROR = "https://registry.npmmirror.com/-/binary/node/"
PLAYWRIGHT_MIRROR = "https://registry.npmmirror.com/-/binary/playwright/"


def _write_toolchain(tmp_path: Path, uv_body: str) -> Path:
    toolchain = tmp_path / "auth-toolchain"
    wheelhouse = toolchain / "wheelhouse"
    python_source = tmp_path / "python-source" / "cpython-3.11.16-macos-aarch64-none"
    python_executable = python_source / "bin" / "python3.11"

    wheelhouse.mkdir(parents=True)
    python_executable.parent.mkdir(parents=True)
    python_executable.write_text(
        '#!/bin/bash\nif [ "$1" = "--version" ]; then echo "Python 3.11.16"; fi\nexit 0\n',
        encoding="utf-8",
    )
    python_executable.chmod(0o755)
    with tarfile.open(toolchain / "python.tar.gz", "w:gz") as archive:
        archive.add(python_source, arcname=python_source.name)

    with gzip.open(toolchain / "uv.gz", "wb") as archive:
        archive.write(uv_body.encode("utf-8"))
    (toolchain / "auth-requirements.txt").write_text(
        "httpx==0.28.1 --hash=sha256:" + "a" * 64 + "\n", encoding="utf-8"
    )
    (wheelhouse / "httpx.whl").write_bytes(b"fixture wheel\n")
    (toolchain / "manifest.json").write_text("{}\n", encoding="utf-8")
    return toolchain


def _runtime_env(tmp_path: Path, stub_bin: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "shell-home"),
            "SHELL": "/bin/zsh",
            "PATH": f"{stub_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "UV_DEFAULT_INDEX": PYTHON_PRIMARY,
            "HERMES_UV_FALLBACK_INDEX": PYTHON_FALLBACK,
            "NPM_CONFIG_REGISTRY": NPM_REGISTRY,
            "HERMES_NODE_MIRROR": NODE_MIRROR,
            "PLAYWRIGHT_DOWNLOAD_HOST": PLAYWRIGHT_MIRROR,
        }
    )
    Path(env["HOME"]).mkdir(parents=True)
    return env


def _stage_args(
    stage: str, install_dir: Path, hermes_home: Path, toolchain: Path
) -> list[str]:
    return [
        "bash",
        str(INSTALL_SH),
        "--stage",
        stage,
        "--bundled-source",
        "--bundled-toolchain",
        str(toolchain),
        "--bootstrap-scope",
        "runtime",
        "--skip-computer-use",
        "--non-interactive",
        "--json",
        "--dir",
        str(install_dir),
        "--hermes-home",
        str(hermes_home),
    ]


@pytest.mark.macos_only
def test_bundled_runtime_locked_python_sync_retries_only_named_domestic_indexes(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "hermes-home"
    stub_bin = tmp_path / "stub-bin"
    uv_log = tmp_path / "uv.log"
    network_log = tmp_path / "network.log"
    stub_bin.mkdir()
    install_dir.mkdir()
    hermes_home.mkdir()

    uv_body = "\n".join(
        [
            "#!/bin/bash",
            f"printf '%s|index=%s\\n' \"$*\" \"${{UV_DEFAULT_INDEX:-}}\" >> {shlex.quote(str(uv_log))}",
            'if [ "$1" = "--version" ]; then echo "uv 0.12.5"; exit 0; fi',
            'if [ "$1 $2" = "python find" ]; then '
            'echo "$HERMES_HOME/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11"; exit 0; fi',
            'if [ "$1" = "sync" ] && [ "$UV_DEFAULT_INDEX" = "' + PYTHON_PRIMARY + '" ]; then exit 42; fi',
            'if [ "$1" = "sync" ] && [ "$UV_DEFAULT_INDEX" = "' + PYTHON_FALLBACK + '" ]; then exit 0; fi',
            'if [ "$1" = "sync" ]; then exit 99; fi',
            "exit 0",
        ]
    ) + "\n"
    toolchain = _write_toolchain(tmp_path, uv_body)

    install_dir.joinpath("pyproject.toml").write_text(
        '[project]\nname="fixture"\nversion="0.0.0"\n', encoding="utf-8"
    )
    install_dir.joinpath("uv.lock").write_text("version = 1\n", encoding="utf-8")
    install_dir.joinpath("uv.toml").write_text(
        'exclude-newer = "2026-08-19T00:00:00Z"\n', encoding="utf-8"
    )
    venv_python = install_dir / "venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    os.symlink(sys.executable, venv_python)
    curl_stub = stub_bin / "curl"
    curl_stub.write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$*" >> {shlex.quote(str(network_log))}\nexit 99\n',
        encoding="utf-8",
    )
    curl_stub.chmod(0o755)

    result = subprocess.run(
        _stage_args("python-deps", install_dir, hermes_home, toolchain),
        cwd=REPO_ROOT,
        env=_runtime_env(tmp_path, stub_bin),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    sync_calls = [line for line in uv_log.read_text(encoding="utf-8").splitlines() if line.startswith("sync ")]
    assert len(sync_calls) == 2
    assert sync_calls[0].endswith(f"index={PYTHON_PRIMARY}")
    assert sync_calls[1].endswith(f"index={PYTHON_FALLBACK}")
    assert not network_log.exists()
    assert not any("github.com" in line or "releases.astral.sh" in line for line in sync_calls)


@pytest.mark.macos_only
def test_bundled_runtime_node_download_and_probes_use_named_domestic_mirrors(
    tmp_path: Path,
) -> None:
    install_dir = tmp_path / "hermes-agent"
    hermes_home = tmp_path / "hermes-home"
    stub_bin = tmp_path / "stub-bin"
    curl_log = tmp_path / "curl.log"
    node_archive = tmp_path / "node-v26.7.0-darwin-arm64.tar.gz"
    node_source = tmp_path / "node-source" / "node-v26.7.0-darwin-arm64" / "bin"
    stub_bin.mkdir()
    install_dir.mkdir()
    hermes_home.mkdir()
    node_source.mkdir(parents=True)

    for name, output in (("node", "v26.7.0"), ("npm", "11.9.0"), ("npx", "11.9.0")):
        executable = node_source / name
        executable.write_text(f'#!/bin/bash\necho "{output}"\n', encoding="utf-8")
        executable.chmod(0o755)
    with tarfile.open(node_archive, "w:gz") as archive:
        archive.add(node_source.parent, arcname=node_source.parent.name)

    uv_body = "\n".join(
        [
            "#!/bin/bash",
            'if [ "$1" = "--version" ]; then echo "uv 0.12.5"; exit 0; fi',
            'if [ "$1 $2" = "python find" ]; then '
            'echo "$HERMES_HOME/python/cpython-3.11.16-macos-aarch64-none/bin/python3.11"; exit 0; fi',
            "exit 0",
        ]
    ) + "\n"
    toolchain = _write_toolchain(tmp_path, uv_body)

    curl_stub = stub_bin / "curl"
    curl_stub.write_text(
        "\n".join(
            [
                "#!/bin/bash",
                f"printf '%s\\n' \"$*\" >> {shlex.quote(str(curl_log))}",
                'out=""',
                'while [ "$#" -gt 0 ]; do',
                '  if [ "$1" = "-o" ]; then out="$2"; shift 2; else shift; fi',
                "done",
                f'if [ -n "$out" ]; then cp {shlex.quote(str(node_archive))} "$out"; exit 0; fi',
                'case "$(tail -n 1 ' + shlex.quote(str(curl_log)) + ')" in',
                '  *-fsSI*) exit 0 ;;',
                "esac",
                'echo "node-v26.7.0-darwin-arm64.tar.gz"',
                "exit 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    curl_stub.chmod(0o755)

    result = subprocess.run(
        _stage_args("prerequisites", install_dir, hermes_home, toolchain),
        cwd=REPO_ROOT,
        env=_runtime_env(tmp_path, stub_bin),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = curl_log.read_text(encoding="utf-8").splitlines()
    assert any(f"{NODE_MIRROR}latest-v26.x/" in call for call in calls)
    assert any(PYTHON_PRIMARY in call for call in calls)
    assert any(NPM_REGISTRY in call for call in calls)
    forbidden = ("github.com", "raw.githubusercontent.com", "releases.astral.sh", "nodejs.org", "pypi.org")
    assert not any(host in call for call in calls for host in forbidden)
    assert (hermes_home / "node" / "bin" / "node").is_file()
