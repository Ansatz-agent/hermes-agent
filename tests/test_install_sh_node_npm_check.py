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
import shutil
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"

# Everything install.sh's node-deps stage needs from the host, EXCEPT node and
# npm. A packaged GUI bootstrap runs with a sanitized PATH (no user shell
# profile), so `#!/usr/bin/env node` shebangs only resolve once the installer
# itself puts a node on PATH — the exact condition the shebang regressions
# below reproduce.
_SANITIZED_TOOLS = [
    "bash", "sh", "env", "uname", "mktemp", "mkdir", "mv", "rm", "ln", "ls",
    "grep", "sed", "awk", "head", "tail", "cut", "tr", "sort", "dirname",
    "basename", "chmod", "cat", "sleep", "date", "touch", "cp", "wc", "find",
    "xargs", "id", "readlink", "tar", "git", "stat", "true", "false",
    "expr", "hostname", "ps", "kill", "printf", "od", "df", "uniq", "tee",
]


def _sanitized_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "sanitized-bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for tool in _SANITIZED_TOOLS:
        resolved = shutil.which(tool)
        if resolved:
            (bin_dir / tool).symlink_to(resolved)
    return bin_dir


# A managed-Node stand-in that also works as an interpreter: real npm is a JS
# file whose `#!/usr/bin/env node` shebang hands the script to node. The shell
# fakes below keep that shebang, so `env` runs this node with the npm script
# as $1 — dispatch it to /bin/sh (the shebang line is a comment to sh).
_MANAGED_NODE_INTERPRETER = """#!/bin/sh
if [ "${1:-}" = "--version" ]; then
    echo v26.0.0
    exit 0
fi
script="$1"
shift
exec /bin/sh "$script" "$@"
"""


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


def test_failed_in_place_npm_repair_replaces_node_before_node_deps(
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
    # The stale npm may be invoked once to attempt an in-place self-upgrade,
    # but it must never install either project dependency tree.
    assert old_npm_calls.exists()
    assert not {
        str(install_dir),
        str(install_dir / "ui-tui"),
    }.intersection(old_npm_calls.read_text(encoding="utf-8").splitlines())
    assert new_npm_calls.read_text(encoding="utf-8").splitlines() == [
        str(install_dir),
        str(install_dir / "ui-tui"),
    ]
    assert "npm 11.12.1 cannot honor this repo's .npmrc" in proc.stdout
    assert "Managed npm repair failed — replacing Hermes-managed Node 26" in proc.stdout


def _bad_band_node_archive(tmp_path: Path) -> tuple[Path, str]:
    """A latest-v26.x archive as the domestic mirror actually served it:
    Node v26.0.0 bundling npm 11.12.1 (inside the bad 11.10-11.16 band),
    with npm behind a `#!/usr/bin/env node` shebang like the real one.

    The fake npm honors engine-strict the way real npm does: while its own
    version sits in the bad band, every project install fails EBADENGINE.
    An `npm install --global ... npm@<range>` self-upgrade flips the version
    state to 11.17.0, after which project installs succeed and are logged.
    """
    system = platform.system()
    machine = platform.machine()
    node_os = "darwin" if system == "Darwin" else "linux"
    node_arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64"}[machine]
    root = f"node-v26.0.0-{node_os}-{node_arch}"
    archive_path = tmp_path / f"{root}.tar.xz"
    npm_body = """#!/usr/bin/env node
dir="$(cd "$(dirname "$0")" && pwd)"
state="$dir/.npm-version"
[ -f "$state" ] || echo 11.12.1 > "$state"
ver="$(cat "$state")"
if [ "${1:-}" = "--version" ]; then
    echo "$ver"
    exit 0
fi
case "$*" in
    *npm@*)
        printf '%s|%s|%s\\n' "$PWD" "${NPM_CONFIG_REGISTRY:-}" "$*" >> "$NPM_UPGRADE_CALLS"
        echo 11.17.0 > "$state"
        exit 0
        ;;
esac
if [ "$ver" = "11.12.1" ]; then
    echo "npm error code EBADENGINE" >&2
    exit 1
fi
printf '%s\\n' "$PWD" >> "$NEW_NPM_CALLS"
exit 0
"""
    with tarfile.open(archive_path, "w:xz") as archive:
        _add_executable(archive, f"{root}/bin/node", _MANAGED_NODE_INTERPRETER)
        _add_executable(archive, f"{root}/bin/npm", npm_body)
        _add_executable(archive, f"{root}/bin/npx", "#!/bin/sh\nexit 0\n")
    return archive_path, archive_path.name


def test_managed_npm_probe_survives_sanitized_gui_path(tmp_path: Path) -> None:
    """Reused managed npm must be probed with managed Node already on PATH.

    Real managed npm starts with `#!/usr/bin/env node`. In a packaged GUI
    bootstrap PATH has no node yet, so probing `$HERMES_HOME/node/bin/npm
    --version` before prepending `$HERMES_HOME/node/bin` yields an empty
    version (`env: node: No such file or directory`). The gate then read
    empty as incompatible and wiped a perfectly compatible managed tree.
    """
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    install_dir = hermes_home / "hermes-agent"
    fake_bin = tmp_path / "bin"
    sanitized_bin = _sanitized_bin(tmp_path)
    managed_npm_calls = tmp_path / "managed-npm-calls"
    curl_log = tmp_path / "curl-log"

    (install_dir / "ui-tui").mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"managed-npm-shebang-regression","private":true,'
        '"engines":{"node":">=22.22.0","npm":"<11.10.0 || >=11.17.0"}}\n',
        encoding="utf-8",
    )
    (install_dir / "ui-tui" / "package.json").write_text(
        '{"name":"managed-npm-shebang-tui","private":true}\n', encoding="utf-8"
    )
    # Any download attempt is itself the regression: a compatible managed
    # tree must be reused, not replaced. Log and fail every curl call.
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$CURL_LOG"
exit 22
""",
    )
    _write_executable(
        hermes_home / "node" / "bin" / "node", _MANAGED_NODE_INTERPRETER
    )
    _write_executable(
        hermes_home / "node" / "bin" / "npm",
        """#!/usr/bin/env node
if [ "${1:-}" = "--version" ]; then
    echo 11.17.0
    exit 0
fi
printf '%s\\n' "$PWD" >> "$MANAGED_NPM_CALLS"
exit 0
""",
    )
    _write_executable(hermes_home / "node" / "bin" / "npx", "#!/bin/sh\nexit 0\n")
    _write_executable(hermes_home / "bin" / "uv", "#!/bin/sh\necho 'uv 0.12.5'\n")

    env = {
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_INSTALL_DIR": str(install_dir),
        "MANAGED_NPM_CALLS": str(managed_npm_calls),
        "CURL_LOG": str(curl_log),
        "PATH": f"{fake_bin}:{sanitized_bin}",
        "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "tmp").mkdir()
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
    assert "found (Hermes-managed)" in proc.stdout
    assert "replacing Hermes-managed" not in proc.stdout
    assert managed_npm_calls.exists(), proc.stdout + proc.stderr
    assert str(install_dir) in managed_npm_calls.read_text(encoding="utf-8")
    if curl_log.exists():
        assert "node-v" not in curl_log.read_text(encoding="utf-8")


def test_reused_bad_band_managed_npm_heals_without_node_redownload(
    tmp_path: Path,
) -> None:
    """An existing Node 26/npm 11.12 runtime must self-heal in place.

    This is the state left on the user's Mac by the failed packaged bootstrap.
    Re-downloading the same stale latest-v26.x archive is wasteful and can
    reproduce the same bad npm. The installer must upgrade the managed npm
    first, keep the existing Node tree, and then finish node-deps.
    """
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    install_dir = hermes_home / "hermes-agent"
    fake_bin = tmp_path / "bin"
    sanitized_bin = _sanitized_bin(tmp_path)
    npm_calls = tmp_path / "npm-calls"
    npm_upgrade_calls = tmp_path / "npm-upgrade-calls"
    curl_log = tmp_path / "curl-log"

    (install_dir / "ui-tui").mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"reused-bad-band-regression","private":true,'
        '"engines":{"node":">=22.22.0","npm":"<11.10.0 || >=11.17.0"}}\n',
        encoding="utf-8",
    )
    (install_dir / "ui-tui" / "package.json").write_text(
        '{"name":"reused-bad-band-tui","private":true}\n', encoding="utf-8"
    )
    _write_executable(
        fake_bin / "curl",
        """#!/bin/sh
printf '%s\n' "$*" >> "$CURL_LOG"
exit 22
""",
    )
    _write_executable(
        hermes_home / "node" / "bin" / "node", _MANAGED_NODE_INTERPRETER
    )
    _write_executable(
        hermes_home / "node" / "bin" / "npm",
        """#!/usr/bin/env node
dir="$(cd "$(dirname "$0")" && pwd)"
state="$dir/.npm-version"
[ -f "$state" ] || echo 11.12.1 > "$state"
if [ "${1:-}" = "--version" ]; then
    cat "$state"
    exit 0
fi
case "$*" in
    *npm@*)
        printf '%s\n' "$*" >> "$NPM_UPGRADE_CALLS"
        echo 11.17.0 > "$state"
        exit 0
        ;;
esac
if [ "$(cat "$state")" = "11.12.1" ]; then
    echo "npm error code EBADENGINE" >&2
    exit 1
fi
printf '%s\n' "$PWD" >> "$NPM_CALLS"
exit 0
""",
    )
    _write_executable(hermes_home / "node" / "bin" / "npx", "#!/bin/sh\nexit 0\n")
    _write_executable(hermes_home / "bin" / "uv", "#!/bin/sh\necho 'uv 0.12.5'\n")

    env = {
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_INSTALL_DIR": str(install_dir),
        "NPM_CALLS": str(npm_calls),
        "NPM_UPGRADE_CALLS": str(npm_upgrade_calls),
        "CURL_LOG": str(curl_log),
        "PATH": f"{fake_bin}:{sanitized_bin}",
        "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "tmp").mkdir()
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
    assert npm_upgrade_calls.exists(), proc.stdout + proc.stderr
    assert npm_calls.read_text(encoding="utf-8").splitlines() == [
        str(install_dir),
        str(install_dir / "ui-tui"),
    ]
    if curl_log.exists():
        assert "latest-v26.x" not in curl_log.read_text(encoding="utf-8")


def test_fresh_managed_node_upgrades_bad_band_bundled_npm(tmp_path: Path) -> None:
    """A fresh managed Node whose archive bundles bad-band npm must be healed.

    The reviewed domestic latest-v26.x mirror served Node v26.0.0 bundling
    npm 11.12.1 — inside the 11.10-11.16 band that engines.npm excludes — so
    the toolchain the installer had just provisioned failed its very first
    `npm install` with EBADENGINE. After installing managed Node, install.sh
    must upgrade the bundled npm into the manifest's engines.npm range (temp
    cwd, explicit --prefix, domestic-first registry), matching the rung that
    scripts/lib/node-bootstrap.sh already has.
    """
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    install_dir = hermes_home / "hermes-agent"
    fake_bin = tmp_path / "bin"
    sanitized_bin = _sanitized_bin(tmp_path)
    new_npm_calls = tmp_path / "new-npm-calls"
    npm_upgrade_calls = tmp_path / "npm-upgrade-calls"
    archive_path, archive_name = _bad_band_node_archive(tmp_path)

    (install_dir / "ui-tui").mkdir(parents=True)
    (install_dir / "package.json").write_text(
        '{"name":"bad-band-npm-regression","private":true,'
        '"engines":{"node":">=22.22.0","npm":"<11.10.0 || >=11.17.0"}}\n',
        encoding="utf-8",
    )
    (install_dir / "ui-tui" / "package.json").write_text(
        '{"name":"bad-band-npm-tui","private":true}\n', encoding="utf-8"
    )
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
    _write_executable(hermes_home / "bin" / "uv", "#!/bin/sh\necho 'uv 0.12.5'\n")

    env = {
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "HERMES_INSTALL_DIR": str(install_dir),
        "NEW_NPM_CALLS": str(new_npm_calls),
        "NPM_UPGRADE_CALLS": str(npm_upgrade_calls),
        "NODE_ARCHIVE": str(archive_path),
        "NODE_ARCHIVE_NAME": archive_name,
        "PATH": f"{fake_bin}:{sanitized_bin}",
        "TMPDIR": str(tmp_path / "tmp"),
    }
    (tmp_path / "tmp").mkdir()
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
    assert npm_upgrade_calls.exists(), proc.stdout + proc.stderr
    upgrade_lines = npm_upgrade_calls.read_text(encoding="utf-8").splitlines()
    assert len(upgrade_lines) == 1, upgrade_lines
    upgrade_cwd, upgrade_registry, upgrade_args = upgrade_lines[0].split("|", 2)
    # engines.npm range must come from the install manifest, not a hardcode.
    assert "npm@<11.10.0 || >=11.17.0" in upgrade_args
    # Explicit --prefix at the managed tree: the prefix-local npmrc points
    # global installs at the command link dir, which would strand the new npm.
    assert f"--prefix {hermes_home / 'node'}" in upgrade_args
    # Temp cwd so the checkout's .npmrc (engine-strict) can't gate the upgrade.
    assert upgrade_cwd not in (str(install_dir), str(install_dir / "ui-tui"))
    # Domestic-first registry policy also applies to the self-upgrade.
    assert upgrade_registry == "https://registry.npmmirror.com"
    assert new_npm_calls.read_text(encoding="utf-8").splitlines() == [
        str(install_dir),
        str(install_dir / "ui-tui"),
    ]
