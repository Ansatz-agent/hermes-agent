"""Behavioral coverage for required Node dependency installation."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_node_deps_stage(
    tmp_path: Path,
    *,
    fail_directory: str | None,
    bundled_runtime: bool = False,
    fail_playwright: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, list[str]]:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    bin_dir = tmp_path / "bin"
    hermes_home = tmp_path / "home"
    managed_bin = hermes_home / "bin"
    npm_calls = tmp_path / "npm-calls"
    npx_calls = tmp_path / "npx-calls"

    tui_dir.mkdir(parents=True)
    bin_dir.mkdir()
    managed_bin.mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"installer-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    (tui_dir / "package.json").write_text(
        '{"name":"tui-regression-probe","private":true}\n',
        encoding="utf-8",
    )
    _write_executable(bin_dir / "node", "#!/bin/sh\necho v26.0.0\n")
    _write_executable(
        bin_dir / "npm",
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo 12.0.0
    exit 0
fi
printf '%s\\n' "$PWD" >> "$NPM_CALLS"
if [ -n "${NPM_FAIL_DIRECTORY:-}" ] && [ "$PWD" = "$NPM_FAIL_DIRECTORY" ]; then
    echo "simulated npm lifecycle failure" >&2
    exit 37
fi
exit 0
""",
    )
    _write_executable(
        bin_dir / "npx",
        """#!/bin/sh
printf '%s\n' "$*" >> "$NPX_CALLS"
if [ "$NPX_FAIL" = "1" ]; then
    echo "simulated Playwright download failure" >&2
    exit 42
fi
exit 0
""",
    )
    _write_executable(managed_bin / "uv", "#!/bin/sh\necho 'uv probe'\n")

    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(hermes_home),
            "HERMES_INSTALL_DIR": str(install_dir),
            "NPM_CALLS": str(npm_calls),
            "NPM_FAIL_DIRECTORY": fail_directory or "",
            "NPX_CALLS": str(npx_calls),
            "NPX_FAIL": "1" if fail_playwright else "0",
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    if bundled_runtime:
        env.update(
            {
                "UV_DEFAULT_INDEX": "https://mirrors.ustc.edu.cn/pypi/simple",
                "HERMES_UV_FALLBACK_INDEX": "https://pypi.tuna.tsinghua.edu.cn/simple",
                "NPM_CONFIG_REGISTRY": "https://registry.npmmirror.com",
                "HERMES_NODE_MIRROR": "https://registry.npmmirror.com/-/binary/node/",
                "PLAYWRIGHT_DOWNLOAD_HOST": "https://registry.npmmirror.com/-/binary/playwright",
            }
        )
    stage_args = [
        "bash",
        str(INSTALL_SH),
        "--stage",
        "node-deps",
        "--json",
        "--skip-computer-use",
    ]
    if bundled_runtime:
        stage_args.extend(["--bundled-source", "--bootstrap-scope", "runtime"])
    else:
        stage_args.append("--skip-browser")
    proc = subprocess.run(
        stage_args,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    calls = npm_calls.read_text(encoding="utf-8").splitlines() if npm_calls.exists() else []
    return proc, install_dir, calls


def _stage_result(proc: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(proc.stdout.splitlines()[-1])


def test_root_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    proc, actual_install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(install_dir),
    )

    assert actual_install_dir == install_dir
    assert proc.returncode != 0
    assert _stage_result(proc) == {
        "ok": False,
        "stage": "node-deps",
        "skipped": False,
        "reason": "exit code 1",
    }
    assert calls == [str(install_dir)]
    assert "Node.js dependencies installed" not in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout
    assert not (install_dir / "node_modules").exists()


def test_tui_node_dependency_failure_is_fatal(tmp_path: Path) -> None:
    install_dir = tmp_path / "install"
    tui_dir = install_dir / "ui-tui"
    proc, _, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=str(tui_dir),
    )

    assert proc.returncode != 0
    assert _stage_result(proc)["ok"] is False
    assert calls == [str(install_dir), str(tui_dir)]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" not in proc.stdout


def test_node_dependency_success_remains_successful(tmp_path: Path) -> None:
    proc, install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=None,
    )

    assert proc.returncode == 0, proc.stderr
    assert _stage_result(proc) == {
        "ok": True,
        "stage": "node-deps",
        "skipped": False,
    }
    assert calls == [str(install_dir), str(install_dir / "ui-tui")]
    assert "Node.js dependencies installed" in proc.stdout
    assert "TUI dependencies installed" in proc.stdout
    assert "Browser engine setup complete" not in proc.stdout


def test_bundled_runtime_playwright_failure_is_fatal_and_never_reports_success(
    tmp_path: Path,
) -> None:
    proc, install_dir, calls = _run_node_deps_stage(
        tmp_path,
        fail_directory=None,
        bundled_runtime=True,
        fail_playwright=True,
    )

    assert proc.returncode != 0
    assert _stage_result(proc)["ok"] is False
    assert calls == [str(install_dir)], proc.stdout + proc.stderr
    assert "Browser engine setup complete" not in proc.stdout
    assert "Playwright browser installation failed" in proc.stdout
