from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from tools.sensevoice_stt import (
    MODEL_MANIFEST,
    ModelManifest,
    _download_archive,
    _part_path,
)


def test_production_manifest_is_modelscope_primary_secondary_then_pinned_fallback() -> None:
    assert MODEL_MANIFEST.urls[0].startswith("https://www.modelscope.cn/models/fuyuantech/")
    assert MODEL_MANIFEST.urls[1].startswith("https://modelscope.cn/models/fuyuantech/")
    assert MODEL_MANIFEST.urls[2].startswith(
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
    )


def test_controlled_proxy_preserves_order_strips_secrets_and_resumes(tmp_path) -> None:
    payload = b"verified-sensevoice-fixture"
    requests_seen: list[tuple[str, str | None, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            headers = {key.lower(): value for key, value in self.headers.items()}
            requests_seen.append((self.path, headers.get("range"), headers))
            if self.path in {"/primary/model", "/secondary/model"}:
                self.send_response(503)
                self.end_headers()
                return
            start = 3 if headers.get("range") == "bytes=3-" else 0
            body = payload[start:]
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(body)))
            if start:
                self.send_header(
                    "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
                )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        manifest = ModelManifest(
            version="fixture",
            urls=(
                f"{base}/primary/model",
                f"{base}/secondary/model",
                f"{base}/official/model",
            ),
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        tmp_path.mkdir(exist_ok=True)
        part = _part_path(tmp_path, manifest)
        part.write_bytes(payload[:3])
        assert not (tmp_path / manifest.version / ".ready").exists()

        downloaded = _download_archive(
            manifest,
            cache_root=tmp_path,
            http_get=requests.get,
            progress=None,
        )

        assert downloaded.read_bytes() == payload
        assert [path for path, _range, _headers in requests_seen] == [
            "/primary/model",
            "/secondary/model",
            "/official/model",
        ]
        assert requests_seen[-1][1] == "bytes=3-"
        for _path, _range, headers in requests_seen:
            assert "authorization" not in headers
            assert "cookie" not in headers
            assert "x-csrf-token" not in headers
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
