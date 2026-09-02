"""Loopback Trace service and durability-race contracts."""

from __future__ import annotations

import http.client
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest


class MemorySecureStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def available(self) -> bool:
        return True

    def read(self, name: str) -> bytes | None:
        return self.values.get(name)

    def write(self, name: str, value: bytes) -> None:
        self.values[name] = value


@pytest.fixture
def service_root() -> Iterator[Path]:
    root = Path(__file__).resolve().parents[4] / "tmp" / "trace-service-tests" / str(uuid.uuid4())
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def _field(number: int, value: bytes) -> bytes:
    def varint(item: int) -> bytes:
        result = bytearray()
        while True:
            byte = item & 0x7F
            item >>= 7
            result.append(byte | (0x80 if item else 0))
            if not item:
                return bytes(result)

    return varint(number << 3 | 2) + varint(len(value)) + value


def _otlp(run_id: str) -> bytes:
    def attribute(key: str, value: str) -> bytes:
        return _field(9, _field(1, key.encode()) + _field(2, _field(1, value.encode())))

    span = (
        _field(1, bytes.fromhex("00112233445566778899aabbccddeeff"))
        + attribute("hermes.session.id", "session-1")
        + attribute("hermes.run.id", run_id)
    )
    return _field(1, _field(2, _field(2, span)))


def _post(lease, body: bytes, *, authorization: str | None = None, path: str = "/v1/traces") -> int:
    parsed = urlsplit(lease.endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    headers = {"Content-Type": "application/x-protobuf"}
    if authorization is not None:
        headers["Authorization"] = authorization
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def test_entrypoint_bound_leases_have_no_desktop_fallback_and_backlog_preserves_fifo(
    service_root: Path,
) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.gateway import Retry
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceOwner
    from hermes_cli.client_auth.trace.service import TraceService

    class RetryUploader:
        def __init__(self) -> None:
            self.batches = []

        def send(self, batch):
            self.batches.append(batch)
            return Retry("offline", next_retry_at_ms=10_000)

    owner = TraceOwner(
        account_id="account-alice",
        session_id="auth-session-alice",
        installation_id="11111111-1111-4111-8111-111111111111",
    )
    outbox = TraceOutbox.open(
        service_root / "outbox",
        owner=owner,
        key_protector=TraceKeyProtector(MemorySecureStore()),
    )
    uploader = RetryUploader()
    service = TraceService(
        owner=owner,
        outbox=outbox,
        uploader=uploader,
        plugins_toml="/sealed/ansatz-voice-trace/plugins.toml",
    )
    try:
        cli = service.open_ingress(TraceEntrypoint.CLI, "cli-1")
        dashboard = service.open_ingress(TraceEntrypoint.DASHBOARD, "dashboard-1")

        assert cli.local_authorization != dashboard.local_authorization
        assert _post(dashboard, _otlp("run-1"), authorization=None) == 401
        assert _post(dashboard, _otlp("run-cli"), authorization=cli.local_authorization) == 200
        assert _post(dashboard, _otlp("run-1"), authorization=dashboard.local_authorization) == 200
        assert _post(dashboard, _otlp("run-2"), authorization=dashboard.local_authorization) == 200

        first = outbox.peek_eligible()
        assert first is not None
        assert first.entrypoint is TraceEntrypoint.CLI
        assert first.run_id == "run-cli"
        assert len(uploader.batches) == 1  # the second request saw backlog
        outbox.quarantine(first.batch_id, error_class="test_advance_fifo")
        dashboard_batch = outbox.peek_eligible()
        assert dashboard_batch is not None
        assert dashboard_batch.entrypoint is TraceEntrypoint.DASHBOARD
        assert dashboard_batch.run_id == "run-1"

        with pytest.raises(ValueError, match="entrypoint"):
            service.open_ingress(None, "ambiguous")  # type: ignore[arg-type]
    finally:
        service.close()
