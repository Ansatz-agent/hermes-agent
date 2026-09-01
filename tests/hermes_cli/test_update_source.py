from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from hermes_cli.update_source import (
    UPDATE_BASE_URL_ENV,
    UpdateSourceError,
    download_release_archive,
    extract_release_archive,
    fetch_latest_release,
    parse_release_metadata,
    release_is_newer,
    resolve_update_base_url,
    sync_auth_runtime_marker,
)


COMMIT = "a" * 40


def _source_archive(path: Path, *, extra: tuple[str, bytes, str] | None = None) -> bytes:
    required = {
        "pyproject.toml": b"[project]\nname='ansatz-agent'\n",
        "uv.lock": b"version = 1\n",
        "hermes_cli/main.py": b"pass\n",
        "scripts/install.ps1": b"# installer\n",
        "scripts/install.sh": b"#!/bin/sh\n",
        "apps/desktop/package.json": b"{}\n",
        "desktop_auth_runtime/uv.lock": b"version = 1\n",
    }
    with tarfile.open(path, "w:gz") as archive:
        for relative, contents in required.items():
            info = tarfile.TarInfo(f"hermes-agent/{relative}")
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
        if extra:
            name, contents, kind = extra
            info = tarfile.TarInfo(name)
            if kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = contents.decode()
            else:
                info.size = len(contents)
            archive.addfile(info, None if kind == "symlink" else io.BytesIO(contents))
    return path.read_bytes()


def test_vm_host_http_override_is_accepted_without_address_policy():
    assert resolve_update_base_url(
        {UPDATE_BASE_URL_ENV: "http://192.168.56.1:8765/releases/"}
    ) == "http://192.168.56.1:8765/releases"


def test_release_api_and_static_archive_work_over_plain_http(tmp_path):
    archive_bytes = _source_archive(tmp_path / "source.tar.gz")
    archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            requests.append(self.path)
            if self.path == "/repos/Ansatz-agent/hermes-agent/releases/latest":
                body = json.dumps(
                    {
                        "tag_name": "v0.18.0",
                        "target_commitish": COMMIT,
                        "draft": False,
                        "prerelease": False,
                        "published_at": "2026-09-01T00:00:00Z",
                        "assets": [
                            {
                                "name": "hermes-backend.tar.gz",
                                "state": "uploaded",
                                "size": len(archive_bytes),
                                "digest": f"sha256:{archive_sha256}",
                                "browser_download_url": "/Ansatz-agent/hermes-agent/releases/download/v0.18.0/hermes-backend.tar.gz",
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/Ansatz-agent/hermes-agent/releases/download/v0.18.0/hermes-backend.tar.gz":
                self.send_response(200)
                self.send_header("Content-Length", str(len(archive_bytes)))
                self.end_headers()
                self.wfile.write(archive_bytes)
                return
            self.send_error(404)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{server.server_port}"
        release = fetch_latest_release(
            base_url=base_url,
            target_platform="windows",
            architecture="x64",
        )
        downloaded = download_release_archive(release, tmp_path / "download.tar.gz")
        extracted = extract_release_archive(downloaded, tmp_path / "unpacked")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    assert release.version == "0.18.0"
    assert extracted.joinpath("hermes_cli/main.py").is_file()
    assert requests[0] == "/repos/Ansatz-agent/hermes-agent/releases/latest"
    assert "/Ansatz-agent/hermes-agent/releases/download/v0.18.0/hermes-backend.tar.gz" in requests


def test_release_metadata_prefers_platform_asset_when_present():
    platform_asset = {
        "name": "hermes-backend-windows-x64.tar.gz",
        "state": "uploaded",
        "size": 1,
        "digest": f"sha256:{'c' * 64}",
        "browser_download_url": "/download/windows-x64.tar.gz",
    }
    universal_asset = {
        "name": "hermes-backend.tar.gz",
        "state": "uploaded",
        "size": 1,
        "digest": f"sha256:{'d' * 64}",
        "browser_download_url": "/download/source.tar.gz",
    }

    release = parse_release_metadata(
        {
            "tag_name": "v0.18.0",
            "target_commitish": COMMIT,
            "draft": False,
            "prerelease": False,
            "assets": [universal_asset, platform_asset],
        },
        base_url="https://updates.example",
        target_platform="windows",
        architecture="x64",
    )

    assert release.archive.url.endswith("/download/windows-x64.tar.gz")
    assert release.archive.sha256 == "c" * 64


def test_no_git_desktop_bundle_update_check_uses_release_server(
    tmp_path, monkeypatch, capsys
):
    """A managed source snapshot must not fall back to the Git check path."""
    install_root = tmp_path / "managed" / "hermes-agent"
    install_root.mkdir(parents=True)
    (install_root / ".install_method").write_text(
        "desktop-bundle\n", encoding="utf-8"
    )
    (install_root / ".hermes-bundled-source.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "commit": "c" * 40,
                "archiveSha256": "d" * 64,
                "version": "0.17.0",
                "source": "release-server",
            }
        ),
        encoding="utf-8",
    )
    assert not (install_root / ".git").exists()

    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            requests.append(self.path)
            if self.path == "/repos/Ansatz-agent/hermes-agent/releases/latest":
                body = json.dumps(
                    {
                        "tag_name": "v0.18.0",
                        "target_commitish": COMMIT,
                        "draft": False,
                        "prerelease": False,
                        "assets": [
                            {
                                "name": "hermes-backend.tar.gz",
                                "state": "uploaded",
                                "size": 1,
                                "digest": f"sha256:{'b' * 64}",
                                "browser_download_url": "/download/v0.18.0/hermes-backend.tar.gz",
                            }
                        ],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(404)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from hermes_cli import main as main_module

        monkeypatch.setattr(main_module, "PROJECT_ROOT", install_root)
        monkeypatch.setenv(
            UPDATE_BASE_URL_ENV, f"http://127.0.0.1:{server.server_port}"
        )
        main_module._cmd_update_check()
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    output = capsys.readouterr().out
    assert "Ansatz 0.18.0 is available" in output
    assert "Not a git repository" not in output
    assert len(requests) == 1
    assert requests[0] == "/repos/Ansatz-agent/hermes-agent/releases/latest"


@pytest.mark.parametrize(
    "extra",
    [
        ("hermes-agent/../../outside.txt", b"bad", "file"),
        ("hermes-agent/link", b"../../outside.txt", "symlink"),
    ],
)
def test_archive_rejects_traversal_and_links(tmp_path, extra):
    archive = tmp_path / "unsafe.tar.gz"
    _source_archive(archive, extra=extra)

    with pytest.raises(UpdateSourceError):
        extract_release_archive(archive, tmp_path / "out")
    assert not (tmp_path / "outside.txt").exists()


def test_same_version_new_commit_is_a_release_but_lower_version_is_not():
    from hermes_cli.update_source import ReleaseArchive, ReleaseMetadata

    archive = ReleaseArchive("https://example.test/source.tar.gz", 1, "b" * 64)
    same_version = ReleaseMetadata("0.17.0", COMMIT, "stable", archive)
    old_version = ReleaseMetadata("0.16.9", COMMIT, "stable", archive)

    assert release_is_newer(same_version, current_version="0.17.0", current_commit="c" * 40)
    assert not release_is_newer(old_version, current_version="0.17.0", current_commit="c" * 40)


def test_sync_auth_runtime_marker_tracks_source_when_lock_is_unchanged(tmp_path):
    from hermes_cli.update_source import ReleaseArchive, ReleaseMetadata

    root = tmp_path / "hermes-agent"
    (root / "desktop_auth_runtime").mkdir(parents=True)
    lock = b"version = 1\n"
    (root / "desktop_auth_runtime" / "uv.lock").write_bytes(lock)
    lock_hash = hashlib.sha256(lock).hexdigest()
    (root / ".hermes-auth-bootstrap-complete").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "scope": "auth",
                "sourceCommit": "c" * 40,
                "sourceArchiveSha256": "d" * 64,
                "authLockSha256": lock_hash,
                "protocolVersion": 2,
            }
        ),
        encoding="utf-8",
    )
    release = ReleaseMetadata(
        "0.17.1",
        COMMIT,
        "stable",
        ReleaseArchive("https://example.test/source.tar.gz", 1, "e" * 64),
    )

    assert sync_auth_runtime_marker(root, release)
    marker = json.loads(
        (root / ".hermes-auth-bootstrap-complete").read_text(encoding="utf-8")
    )
    assert marker["sourceCommit"] == COMMIT
    assert marker["sourceArchiveSha256"] == "e" * 64
    assert marker["authLockSha256"] == lock_hash


def test_sync_auth_runtime_marker_refuses_lock_mismatch(tmp_path):
    from hermes_cli.update_source import ReleaseArchive, ReleaseMetadata

    root = tmp_path / "hermes-agent"
    (root / "desktop_auth_runtime").mkdir(parents=True)
    (root / "desktop_auth_runtime" / "uv.lock").write_text(
        "version = 2\n", encoding="utf-8"
    )
    marker = {
        "schemaVersion": 2,
        "scope": "auth",
        "sourceCommit": "c" * 40,
        "sourceArchiveSha256": "d" * 64,
        "authLockSha256": "f" * 64,
        "protocolVersion": 2,
    }
    marker_path = root / ".hermes-auth-bootstrap-complete"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    release = ReleaseMetadata(
        "0.17.1",
        COMMIT,
        "stable",
        ReleaseArchive("https://example.test/source.tar.gz", 1, "e" * 64),
    )

    assert not sync_auth_runtime_marker(root, release)
    assert json.loads(marker_path.read_text(encoding="utf-8")) == marker
