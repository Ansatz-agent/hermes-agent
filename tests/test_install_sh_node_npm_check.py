"""Regression tests for install.sh Node/npm checks (#77003).

A stray `node` symlink without a sibling `npm` (leftover from a node
version manager) made the installer report "✓ Node.js found" and then fail
opaquely at the desktop stage. Node must only count as found when npm
resolves on the same PATH, and npm install stages must not report success
when the install actually failed.
"""

from __future__ import annotations

import io
import os
import platform
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _add_executable(archive: tarfile.TarFile, name: str, body: str) -> None:
    payload = body.encode("utf-8")
    info = tarfile.TarInfo(name)
    info.mode = 0o755
    info.size = len(payload)
    archive.addfile(info, io.BytesIO(payload))


def _managed_node_archive(tmp_path: Path) -> tuple[Path, str]:
    system = platform.system()
    machine = platform.machine()
    node_os = "darwin" if system == "Darwin" else "linux"
    node_arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}[machine]
    root = f"node-v26.0.0-{node_os}-{node_arch}"
    archive_path = tmp_path / f"{root}.tar.xz"
    with tarfile.open(archive_path, "w:xz") as archive:
        _add_executable(archive, f"{root}/bin/node", "#!/bin/sh\necho v26.0.0\n")
        _add_executable(
            archive,
            f"{root}/bin/npm",
            """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo 12.0.2
    exit 0
fi
printf '%s\\n' "$PWD" >> "$NEW_NPM_CALLS"
exit 0
""",
        )
        _add_executable(archive, f"{root}/bin/npx", "#!/bin/sh\nexit 0\n")
    return archive_path, archive_path.name


def test_check_node_requires_npm_alongside_node() -> None:
    """check_node must not report success when only `node` resolves.

    Before the fix, `command -v node` succeeding was enough — a stray node
    symlink (no sibling npm) passed the check, every later `npm install`
    failed silently, and the desktop build died with an opaque
    "Node.js / npm unavailable" (#77003).
    """
    text = INSTALL_SH.read_text()

    # The system-toolchain branch now gates on BOTH node and npm.
    assert (
        "if command -v node &> /dev/null && command -v npm &> /dev/null \\" in text
    )
    # The "node found but npm missing" case has its own explicit branch that
    # falls through to installing the Hermes-managed Node (which bundles npm).
    assert "node found but npm is not on PATH (stray node symlink?)" in text


def test_check_node_managed_requires_npm() -> None:
    """The Hermes-managed Node fallback also requires its npm to exist."""
    text = INSTALL_SH.read_text()
    assert (
        '[ -x "$HERMES_HOME/node/bin/node" ] && [ -x "$HERMES_HOME/node/bin/npm" ] \\'
        in text
    )


def test_incompatible_managed_npm_is_replaced_before_node_deps(
    tmp_path: Path,
) -> None:
    """A reusable managed Node must not bypass the repository's npm gate.

    The packaged app previously reused Node 26 with npm 11.12.1, then failed
    every browser-tool install with EBADENGINE. The real node-deps stage must
    replace that managed toolchain and complete with a compatible npm.
    """
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    install_dir = hermes_home / "hermes-agent"
    fake_bin = tmp_path / "bin"
    old_npm_calls = tmp_path / "old-npm-calls"
    new_npm_calls = tmp_path / "new-npm-calls"
    archive_path, archive_name = _managed_node_archive(tmp_path)

    (install_dir / "ui-tui").mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"managed-npm-regression","private":true}\n', encoding="utf-8"
    )
    (install_dir / "ui-tui" / "package.json").write_text(
        '{"name":"managed-npm-tui","private":true}\n', encoding="utf-8"
    )
    _write_executable(fake_bin / "node", "#!/bin/sh\necho v18.0.0\n")
    _write_executable(fake_bin / "npm", "#!/bin/sh\necho 12.0.2\n")
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
output=""
previous=""
for argument in "$@"; do
    if [ "$previous" = "-o" ]; then output="$argument"; fi
    previous="$argument"
done
if [ -n "$output" ]; then
    cp "$NODE_ARCHIVE" "$output"
else
    printf '%s\\n' "$NODE_ARCHIVE_NAME"
fi
""",
    )
    _write_executable(hermes_home / "node" / "bin" / "node", "#!/bin/sh\necho v26.0.0\n")
    _write_executable(
        hermes_home / "node" / "bin" / "npm",
        """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo 11.12.1
    exit 0
fi
printf '%s\\n' "$PWD" >> "$OLD_NPM_CALLS"
exit 91
""",
    )
    _write_executable(hermes_home / "node" / "bin" / "npx", "#!/bin/sh\nexit 0\n")
    _write_executable(hermes_home / "bin" / "uv", "#!/bin/sh\necho 'uv 0.12.5'\n")

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "HERMES_INSTALL_DIR": str(install_dir),
            "NEW_NPM_CALLS": str(new_npm_calls),
            "NODE_ARCHIVE": str(archive_path),
            "NODE_ARCHIVE_NAME": archive_name,
            "OLD_NPM_CALLS": str(old_npm_calls),
            "PATH": f"{fake_bin}:{env['PATH']}",
        }
    )
    proc = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--stage",
            "node-deps",
            "--json",
            "--skip-browser",
            "--skip-computer-use",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not old_npm_calls.exists()
    assert new_npm_calls.read_text(encoding="utf-8").splitlines() == [
        str(install_dir),
        str(install_dir / "ui-tui"),
    ]
    assert "npm 11.12.1 cannot honor this repo's .npmrc" in proc.stdout
