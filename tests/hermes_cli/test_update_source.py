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
    release_is_newer,
    resolve_update_base_url,
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
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib callback name
            requests.append(self.path)
            if self.path.startswith("/api/v1/ansatz/releases/latest?"):
                body = json.dumps(
                    {
                        "schemaVersion": 1,
                        "product": "ansatz",
                        "channel": "stable",
                        "version": "0.18.0",
                        "commit": COMMIT,
                        "archive": {
                            "url": "/static/ansatz-0.18.0.tar.gz",
                            "size": len(archive_bytes),
                            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/static/ansatz-0.18.0.tar.gz":
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
    assert any("platform=windows" in request and "arch=x64" in request for request in requests)
    assert "/static/ansatz-0.18.0.tar.gz" in requests


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
