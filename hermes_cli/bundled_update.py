"""GitHub checks and one-time Git adoption for the Desktop source payload.

The bundled SHA is the base, not a request to replace local code with main.
Only a published descendant may update it. The normal Git updater owns all
dependency refresh and build work after adoption.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

REPOSITORY = "Ansatz-agent/hermes-agent"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
SOURCE_MARKER = ".hermes-bundled-source.json"
SHA_RE = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)


def read_source_commit(root: Path) -> str:
    marker = json.loads((root / SOURCE_MARKER).read_text(encoding="utf-8"))
    commit = marker.get("commit", "")
    if marker.get("schemaVersion") != 1 or not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise ValueError("The bundled source commit marker is invalid; reinstall Ansatz.")
    return commit.lower()


def _github_json(route: str):
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/{route}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ansatz-update"},
    )
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def check_bundled_update(root: Path, branch: str = "main") -> dict:
    status = {"supported": True, "branch": branch, "hermesRoot": str(root), "commits": [], "dirty": False}
    try:
        current = read_source_commit(root)
        target = _github_json(f"commits/{quote(branch, safe='')}").get("sha", "")
        if not isinstance(target, str) or not SHA_RE.fullmatch(target):
            raise ValueError("GitHub returned an invalid target commit.")
        status.update(currentSha=current, targetSha=target, behind=0, updateAvailable=False)
        if target == current:
            return status
        try:
            comparison = _github_json(f"compare/{current}...{target}")
        except HTTPError as exc:
            if exc.code != 404:
                raise
            return {**status, "reason": "unpublished-source", "message": (
                "The installed source commit is not available for comparison on GitHub. "
                "Publish this build's commit before checking for updates; the local installation was kept."
            )}
        relation = comparison.get("status")
        count = comparison.get("ahead_by")
        if relation == "ahead" and isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return {**status, "behind": count, "updateAvailable": True}
        if relation in {"behind", "identical"}:
            return status
        return {**status, "supported": False, "reason": "diverged-source", "message": (
            "The installed source and GitHub branch have diverged. Automatic replacement was skipped."
        )}
    except Exception as exc:
        message = f"GitHub update check failed: {exc}"
        return {**status, "error": "github-check-failed", "message": message}


def adopt_bundled_checkout(root: Path, branch: str, current: str, target: str, *, repository_url: str = REPOSITORY_URL) -> Path:
    """Restore a complete Git tree at the bundled BASE, retaining a backup.

    Network/staging failure leaves the live tree untouched. The final rename
    transaction rolls back on error. Local source edits remain in the returned
    backup; credentials, virtualenvs and root node_modules are never replaced.
    """
    root = root.resolve()
    if (root / ".git").exists() or (root / ".git").is_symlink():
        raise ValueError("Git adoption is only for a bundled source directory.")
    if read_source_commit(root) != current or not SHA_RE.fullmatch(target):
        raise ValueError("Bundled source identity changed; check for updates again.")
    if (root / ".install_method").read_text().strip() != "desktop-bundle":
        raise ValueError("This directory is not a Desktop bundled installation.")

    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    def git(args, cwd):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True, timeout=600).stdout.strip()

    protected = {".env", "venv", ".venv", "auth-venv", "node_modules", ".install_method", SOURCE_MARKER}
    with tempfile.TemporaryDirectory(prefix=".ansatz-git-stage-", dir=root.parent) as temporary:
        stage = Path(temporary) / "source"
        git(["clone", "--single-branch", "--branch", branch, "--", repository_url, str(stage)], root.parent)
        git(["merge-base", "--is-ancestor", current, target], stage)
        git(["checkout", "-B", branch, current], stage)
        tracked = set(git(["ls-tree", "--name-only", "HEAD"], stage).splitlines())
        if tracked & protected:
            raise ValueError("The remote source contains a protected runtime path.")
        for required in ("hermes_cli/main.py", "pyproject.toml", "apps/desktop/package.json"):
            if not (stage / required).is_file():
                raise ValueError(f"GitHub source is missing {required}")
        # Generated artifacts are not part of the source snapshot.
        for relative in ("apps/desktop/build", "apps/desktop/release"):
            source, destination = root / relative, stage / relative
            if source.exists() and not destination.exists():
                shutil.copytree(source, destination, symlinks=True)
        # These local identity files must not become a stash on the first update.
        with (stage / ".git/info/exclude").open("a") as exclude:
            exclude.write(f"\n/{SOURCE_MARKER}\n/.install_method\n/.hermes_build_sha\n")
        # Leave bundled-refresh mode once Git owns the source. Otherwise the
        # next app launch would replace the checkout with its filtered payload.
        (stage / ".install_method").write_text("git\n", encoding="utf-8")

        backups = root.parent / "source-update-backups"
        backups.mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=f"{current[:12]}-", dir=backups))
        installed = []
        moved = []
        try:
            # Publish .git last, after the complete base tree is installed.
            for name in sorted(tracked) + [".git", ".install_method"]:
                destination = root / name
                if destination.exists() or destination.is_symlink():
                    destination.rename(backup / name)
                    moved.append(name)
                (stage / name).rename(destination)
                installed.append(name)
        except Exception:
            for name in reversed(installed):
                (root / name).rename(stage / name)
            for name in reversed(moved):
                (backup / name).rename(root / name)
            raise
        return backup


def prepare_desktop_rebuild(root: Path, app: Path) -> None:
    """Bind the rebuilt app's bootstrap payload to the newly updated Git tree."""
    from hermes_constants import with_hermes_node_path

    env = with_hermes_node_path()
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    node = shutil.which("node", path=env.get("PATH"))
    if not node:
        raise RuntimeError("Node.js is required to rebuild Ansatz.")
    desktop = root / "apps/desktop"
    toolchain = app / "Contents/Resources/bootstrap/auth-toolchain"
    if not (toolchain / "manifest.json").is_file():
        raise RuntimeError("The installed app's authentication toolchain is missing; reinstall Ansatz.")
    destination = desktop / "build/bootstrap/auth-toolchain"
    shutil.copytree(toolchain, destination, dirs_exist_ok=True)
    for script in ("write-build-stamp.mjs", "build-backend-payload.mjs"):
        subprocess.run([node, str(desktop / "scripts" / script)], cwd=desktop, env=env, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--branch", default="main")
    args = parser.parse_args()
    print(json.dumps(check_bundled_update(args.root, args.branch)))


if __name__ == "__main__":
    main()
