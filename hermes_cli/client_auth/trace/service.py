"""Native-owner loopback OTLP ingress and recovery scheduler."""

from __future__ import annotations

import hashlib
import secrets
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Protocol

from .gateway import Accepted, GatewayUploader, Quarantine, Retry, TerminalRevocation
from .identity import TraceEntrypoint
from .otlp import derive_correlation
from .outbox import (
    DurableTraceBatch,
    PendingTraceCommit,
    TraceEnvelope,
    TraceOutbox,
    TraceOutboxDiagnostics,
    TraceOwner,
)


_MAX_BODY_BYTES = 8 * 1024 * 1024


class Uploader(Protocol):
    def send(self, batch: DurableTraceBatch): ...


@dataclass(frozen=True, slots=True)
class TraceIngressLease:
    endpoint: str
    local_authorization: str
    installation_id: str
    entrypoint: TraceEntrypoint
    plugins_toml: str


class TraceService:
    def __init__(
        self,
        *,
        owner: TraceOwner,
        outbox: TraceOutbox,
        uploader: Uploader,
        plugins_toml: str,
    ) -> None:
        self._owner = owner
        self._outbox = outbox
        self._uploader = uploader
        self._plugins_toml = plugins_toml
        self._leases: dict[str, tuple[TraceEntrypoint, str]] = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ansatz-trace")
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._closed = False

    def open_ingress(
        self,
        entrypoint: TraceEntrypoint,
        consumer_id: str,
    ) -> TraceIngressLease:
        parsed = TraceEntrypoint.parse(
            entrypoint.value if isinstance(entrypoint, TraceEntrypoint) else None
        )
        if not consumer_id:
            raise ValueError("Trace consumer identity is required")
        with self._lock:
            self._require_open()
            self._ensure_server()
            bearer = secrets.token_urlsafe(32)
            self._leases[bearer] = (parsed, consumer_id)
            assert self._server is not None
            port = self._server.server_address[1]
        return TraceIngressLease(
            endpoint=f"http://127.0.0.1:{port}/v1/traces",
            local_authorization=f"Bearer {bearer}",
            installation_id=self._owner.installation_id,
            entrypoint=parsed,
            plugins_toml=self._plugins_toml,
        )

    def pump(self) -> int:
        uploaded = 0
        while True:
            with self._lock:
                batch = self._outbox.peek_eligible()
            if batch is None:
                return uploaded
            result = self._uploader.send(batch)
            with self._lock:
                if isinstance(result, Accepted):
                    self._outbox.acknowledge(result.receipt)
                    uploaded += 1
                    continue
                if isinstance(result, Retry):
                    self._outbox.mark_retry(
                        batch.batch_id,
                        next_retry_at_ms=result.next_retry_at_ms,
                    )
                    return uploaded
                if isinstance(result, Quarantine):
                    self._outbox.quarantine(batch.batch_id, error_class=result.reason)
                    continue
                if isinstance(result, TerminalRevocation):
                    return uploaded
                return uploaded

    def next_retry_at(self) -> int | None:
        with self._lock:
            return self._outbox.next_retry_at()

    def diagnostics(self) -> TraceOutboxDiagnostics:
        with self._lock:
            return self._outbox.diagnostics()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            server = self._server
        if server is not None:
            server.shutdown()
            server.server_close()
        if self._server_thread is not None:
            self._server_thread.join(timeout=5)
        self._executor.shutdown(wait=True, cancel_futures=False)
        with self._lock:
            self._outbox.close()

    def _ensure_server(self) -> None:
        if self._server is not None:
            return
        service = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "AnsatzTraceIngress/1"

            def do_POST(self) -> None:  # noqa: N802
                service._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                self.send_error(405)

            def log_message(self, _format: str, *args: object) -> None:
                del args

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="ansatz-trace-ingress", daemon=True)
        thread.start()
        self._server = server
        self._server_thread = thread

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        if request.client_address[0] != "127.0.0.1" or request.path != "/v1/traces":
            _respond(request, 404)
            return
        authorization = request.headers.get_all("Authorization") or []
        content_types = request.headers.get_all("Content-Type") or []
        lengths = request.headers.get_all("Content-Length") or []
        encodings = request.headers.get_all("Content-Encoding") or []
        if len(authorization) != 1 or not authorization[0].startswith("Bearer "):
            _respond(request, 401)
            return
        bearer = authorization[0][len("Bearer ") :]
        with self._lock:
            binding = self._leases.get(bearer)
        if binding is None:
            _respond(request, 401)
            return
        if content_types != ["application/x-protobuf"]:
            _respond(request, 415)
            return
        if encodings and encodings != ["identity"]:
            _respond(request, 415)
            return
        if len(lengths) != 1:
            _respond(request, 411)
            return
        try:
            length = int(lengths[0])
        except ValueError:
            _respond(request, 400)
            return
        if length < 1 or length > _MAX_BODY_BYTES:
            _respond(request, 413)
            return
        body = request.rfile.read(length)
        if len(body) != length:
            _respond(request, 400)
            return
        correlation = derive_correlation(body)
        if correlation is None:
            _respond(request, 400)
            return
        entrypoint, _consumer_id = binding
        envelope = TraceEnvelope(
            body=body,
            entrypoint=entrypoint,
            hermes_session_id=correlation.session_id,
            run_id=correlation.run_id,
        )
        if self._admit(envelope):
            _respond(request, 200, protobuf=True)
        else:
            _respond(request, 503)

    def _admit(self, envelope: TraceEnvelope) -> bool:
        with self._lock:
            backlog = self._outbox.diagnostics().pending > 0
            pending = self._outbox.begin_enqueue(envelope)
        local = self._executor.submit(self._commit, pending)
        cloud: Future[object] | None = None
        if not backlog:
            cloud = self._executor.submit(self._uploader.send, self._preview(pending))
        futures: set[Future[object]] = {local}
        if cloud is not None:
            futures.add(cloud)
        while futures:
            done, futures = wait(futures, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    result = future.result()
                except Exception:
                    continue
                if future is local:
                    if cloud is not None:
                        cloud.add_done_callback(lambda item: self._ack_cloud_result(pending.batch_id, item))
                    return True
                if isinstance(result, Accepted):
                    local.add_done_callback(lambda item: self._ack_after_local(item, result))
                    return True
        return False

    def _commit(self, pending: PendingTraceCommit) -> DurableTraceBatch:
        with self._lock:
            return self._outbox.commit(pending)

    def _ack_cloud_result(self, batch_id: str, future: Future[object]) -> None:
        try:
            result = future.result()
        except Exception:
            return
        if isinstance(result, Accepted):
            with self._lock:
                try:
                    self._outbox.acknowledge(result.receipt)
                except Exception:
                    return

    def _ack_after_local(self, future: Future[object], result: Accepted) -> None:
        try:
            future.result()
        except Exception:
            return
        with self._lock:
            try:
                self._outbox.acknowledge(result.receipt)
            except Exception:
                return

    def _preview(self, pending: PendingTraceCommit) -> DurableTraceBatch:
        envelope = pending.envelope
        return DurableTraceBatch(
            batch_id=pending.batch_id,
            dedupe_key=pending.dedupe_key,
            body=envelope.body,
            entrypoint=envelope.entrypoint,
            hermes_session_id=envelope.hermes_session_id,
            run_id=envelope.run_id,
            installation_id=self._owner.installation_id,
            account_id=self._owner.account_id,
            account_session_id=self._owner.session_id,
            sequence=0,
            created_at_ms=pending.created_at_ms,
            next_retry_at_ms=pending.created_at_ms,
            attempt_count=0,
            payload_sha256=hashlib.sha256(envelope.body).hexdigest(),
        )

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Trace service is closed")


def _respond(request: BaseHTTPRequestHandler, status: int, *, protobuf: bool = False) -> None:
    request.send_response(status)
    request.send_header("Cache-Control", "no-store")
    request.send_header("Content-Type", "application/x-protobuf" if protobuf else "application/json")
    request.send_header("Content-Length", "0")
    request.end_headers()


__all__ = ["TraceIngressLease", "TraceService"]
