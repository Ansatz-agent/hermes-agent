import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import threading
from urllib.request import Request, urlopen

import pytest

from hermes_cli import bundled_update as update


def bundled(root, commit):
    root.mkdir(parents=True)
    (root / update.SOURCE_MARKER).write_text(json.dumps({"schemaVersion": 1, "commit": commit}))
    (root / ".install_method").write_text("desktop-bundle\n")
    return root


@pytest.mark.parametrize("relation,count,http_code,available,reason,error", [
    ("ahead", 2, 200, True, None, None),
    ("behind", 0, 200, False, None, None),
    ("diverged", 2, 200, False, "diverged-source", None),
    (None, None, 404, False, "unpublished-source", None),
    (None, None, 403, False, None, "github-check-failed"),
])
def test_github_check_real_http(tmp_path, monkeypatch, relation, count, http_code, available, reason, error):
    current, target = "a" * 40, "b" * 40
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            comparing = "/compare/" in self.path
            self.send_response(http_code if comparing else 200)
            self.end_headers()
            self.wfile.write(json.dumps({"status": relation, "ahead_by": count} if comparing else {"sha": target}).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def local_request(request, **kwargs):
        url = request.full_url.replace("https://api.github.com", f"http://127.0.0.1:{server.server_port}")
        return urlopen(Request(url, headers=dict(request.header_items())), **kwargs)
    monkeypatch.setattr(update, "urlopen", local_request)
    try:
        root = bundled(tmp_path / "hermes-agent", current)
        status = update.check_bundled_update(root)
        assert bool(status.get("updateAvailable")) is available
        assert status.get("reason") == reason
        assert status.get("error") == error
        assert requests == [f"/repos/{update.REPOSITORY}/commits/main", f"/repos/{update.REPOSITORY}/compare/{current}...{target}"]
        assert not (root / ".git").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Update test")
    git(root, "config", "user.email", "update@example.invalid")
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli/main.py").write_text("VERSION = 'base'\n")
    (root / "pyproject.toml").write_text("[project]\nname = 'fixture'\nversion = '1.0'\n")
    (root / "apps/desktop").mkdir(parents=True)
    (root / "apps/desktop/package.json").write_text('{"name":"fixture","version":"1.0"}')
    (root / ".gitignore").write_text(".env\nvenv/\nnode_modules/\napps/desktop/build/\napps/desktop/release/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    current = git(root, "rev-parse", "HEAD")
    (root / "hermes_cli/main.py").write_text("VERSION = 'updated'\n")
    git(root, "commit", "-am", "update")
    return root, current, git(root, "rev-parse", "HEAD")


def installed(tmp_path, current):
    root = bundled(tmp_path / "home/hermes-agent", current)
    (root / "hermes_cli").mkdir()
    (root / "hermes_cli/main.py").write_text("LOCAL_EDIT = True\n")
    (root / ".env").write_text("fixture credential\n")
    (root / "venv").mkdir()
    (root / "venv/keep").write_text("runtime\n")
    (root / "apps/desktop/build").mkdir(parents=True)
    (root / "apps/desktop/build/keep").write_text("packaging inputs\n")
    return root


def test_adopt_then_apply_real_git_update_preserves_runtime_and_source_backup(tmp_path, remote, monkeypatch):
    repo, current, target = remote
    root = installed(tmp_path, current)
    monkeypatch.setenv("HERMES_HOME", str(root.parent))
    backup = update.adopt_bundled_checkout(root, "main", current, target, repository_url=str(repo))
    assert git(root, "rev-parse", "HEAD") == current
    assert git(root, "status", "--porcelain") == ""
    assert (root / ".install_method").read_text().strip() == "git"
    assert (backup / ".install_method").read_text().strip() == "desktop-bundle"
    assert not (root / update.SOURCE_MARKER).exists()
    assert update.read_source_commit(backup) == current
    assert (backup / "hermes_cli/main.py").read_text() == "LOCAL_EDIT = True\n"
    assert (root / ".env").read_text() == "fixture credential\n"
    assert (root / "venv/keep").read_text() == "runtime\n"
    assert (root / "apps/desktop/build/keep").read_text() == "packaging inputs\n"
    git(root, "fetch", "origin", "main")
    git(root, "merge", "--ff-only", "origin/main")
    assert git(root, "rev-parse", "HEAD") == target
    assert (root / "hermes_cli/main.py").read_text() == "VERSION = 'updated'\n"


def test_adoption_rename_failure_rolls_back(tmp_path, remote, monkeypatch):
    repo, current, target = remote
    root = installed(tmp_path, current)
    original_rename = Path.rename

    def fail_source_move(source, destination):
        if source.name == "hermes_cli" and ".ansatz-git-stage-" in str(source) and Path(destination).parent == root:
            raise OSError("simulated move failure")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_source_move)
    with pytest.raises(OSError, match="simulated"):
        update.adopt_bundled_checkout(root, "main", current, target, repository_url=str(repo))
    assert not (root / ".git").exists()
    assert (root / "hermes_cli/main.py").read_text() == "LOCAL_EDIT = True\n"
    assert (root / "apps/desktop/build/keep").read_text() == "packaging inputs\n"
    assert not (root / "pyproject.toml").exists()
    assert update.read_source_commit(root) == current


def test_marker_move_failure_restores_bundled_identity(tmp_path, remote, monkeypatch):
    repo, current, target = remote
    root = installed(tmp_path, current)
    original_rename = Path.rename

    def fail_marker_move(source, destination):
        if source == root / update.SOURCE_MARKER:
            raise OSError("marker move failed")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_marker_move)
    with pytest.raises(OSError, match="marker move failed"):
        update.adopt_bundled_checkout(root, "main", current, target, repository_url=str(repo))
    assert not (root / ".git").exists()
    assert (root / ".install_method").read_text().strip() == "desktop-bundle"
    assert update.read_source_commit(root) == current
    assert (root / "hermes_cli/main.py").read_text() == "LOCAL_EDIT = True\n"


def test_unknown_commit_never_replaces_bundled_source(tmp_path, remote):
    repo, _, target = remote
    current = "c" * 40
    root = installed(tmp_path, current)
    with pytest.raises(subprocess.CalledProcessError):
        update.adopt_bundled_checkout(root, "main", current, target, repository_url=str(repo))
    assert not (root / ".git").exists()
    assert (root / "hermes_cli/main.py").read_text() == "LOCAL_EDIT = True\n"


def test_cli_check_accepts_bundled_install(tmp_path, monkeypatch, capsys):
    from hermes_cli import main as cli, update_cmd

    root = bundled(tmp_path / "runtime", "a" * 40)
    monkeypatch.setattr(cli, "PROJECT_ROOT", root)
    monkeypatch.setattr(update, "check_bundled_update", lambda root, branch: {
        "updateAvailable": False, "message": "Unpublished source retained."
    })
    update_cmd._cmd_update_check()
    assert "Unpublished source retained." in capsys.readouterr().out


def test_mac_packaged_executable_detects_ansatz(tmp_path, monkeypatch):
    from hermes_cli import main as cli

    executable = tmp_path / "release/mac-arm64/Ansatz.app/Contents/MacOS/Ansatz"
    executable.parent.mkdir(parents=True)
    executable.write_text("fixture")
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli._desktop_packaged_executable(tmp_path) == executable


def test_app_update_rebuilds_payload_even_when_desktop_stamp_matches(tmp_path, monkeypatch):
    from hermes_cli import main as cli, update_cmd

    desktop = tmp_path / "apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}")
    calls = []
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(update_cmd.sys, "platform", "darwin")
    monkeypatch.setenv("ANSATZ_DESKTOP_UPDATE_APP", "/Applications/Ansatz.app")
    monkeypatch.setattr(cli, "_resolve_node_runtime_npm", lambda: "npm")
    monkeypatch.setattr(cli, "_desktop_build_needed", lambda *args, **kwargs: False)
    monkeypatch.setattr(update, "prepare_desktop_rebuild", lambda *args: calls.append(args))
    monkeypatch.setattr(cli, "_run_logged_subprocess", lambda command, **kwargs: (
        calls.append(command) or subprocess.CompletedProcess(command, 0)
    ))
    update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=False)
    assert calls[0] == (tmp_path, Path("/Applications/Ansatz.app"))
    assert "--force-build" in calls[1]
