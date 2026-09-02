"""Real-boundary contracts shared by CLI, Dashboard, and Desktop Trace producers."""

from __future__ import annotations

import http.client
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest


INSTALLATION_ID = "11111111-1111-4111-8111-111111111111"


class MemorySecureStore:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def available(self) -> bool:
        return True

    def read(self, name: str) -> bytes | None:
        return self.values.get(name)

    def write(self, name: str, value: bytes) -> None:
        self.values[name] = value


class RotatingCredentialSource:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    def __call__(self, force: bool):
        from hermes_cli.client_auth.client import TraceCredential

        self.calls.append(force)
        return TraceCredential(
            access_token="trace-token-rotated" if force else "trace-token-initial",
            expires_at="2099-09-03T00:00:00Z",
            expires_in=3600,
            installation_id=INSTALLATION_ID,
        )


class GatewayHarness:
    def __init__(self) -> None:
        self.accepted: dict[str, bytes] = {}
        self.mode = "offline"
        self.requests = []
        self.unauthorized_issued = False

    def __call__(self, request):
        from hermes_cli.client_auth.trace.gateway import GatewayResponse

        self.requests.append(request)
        batch_id = request.headers["Idempotency-Key"]
        if self.mode == "offline":
            raise OSError("gateway offline")
        if self.mode == "response_loss":
            self.accepted[batch_id] = request.body
            raise ConnectionResetError("receipt lost")
        if self.mode == "duplicate":
            assert self.accepted[batch_id] == request.body
            return self._receipt(request, "duplicate")
        if self.mode == "unauthorized_once" and not self.unauthorized_issued:
            self.unauthorized_issued = True
            return GatewayResponse(status=401, headers={})
        self.accepted[batch_id] = request.body
        return self._receipt(request, "accepted")

    @staticmethod
    def _receipt(request, outcome: str):
        from hermes_cli.client_auth.trace.gateway import GatewayResponse

        return GatewayResponse(
            status=202,
            headers={
                "x-trace-batch-id": request.headers["Idempotency-Key"],
                "x-trace-receipt": outcome,
            },
        )


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


def _key_value(key: str, value: str) -> bytes:
    return _field(1, key.encode()) + _field(2, _field(1, value.encode()))


def _otlp(run_id: str, *, forged_entrypoint: str = "desktop") -> bytes:
    span = (
        _field(1, bytes.fromhex("00112233445566778899aabbccddeeff"))
        + _field(9, _key_value("hermes.session.id", "hermes-session-1"))
        + _field(9, _key_value("hermes.run.id", run_id))
        + _field(9, _key_value("hermes.entrypoint", forged_entrypoint))
    )
    resource = _field(1, _key_value("hermes.entrypoint", forged_entrypoint))
    resource_spans = _field(1, resource) + _field(2, _field(2, span))
    return _field(1, resource_spans)


def _post(lease, body: bytes) -> int:
    parsed = urlsplit(lease.endpoint)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    connection.request(
        "POST",
        parsed.path,
        body=body,
        headers={
            "Authorization": lease.local_authorization,
            "Content-Type": "application/x-protobuf",
        },
    )
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def _owner(account_id: str = "account-alice"):
    from hermes_cli.client_auth.trace.outbox import TraceOwner

    return TraceOwner(
        account_id=account_id,
        session_id=f"auth-session-{account_id}",
        installation_id=INSTALLATION_ID,
    )


def _open_service(root, owner, protector, uploader, clock):
    from hermes_cli.client_auth.trace.outbox import TraceOutbox
    from hermes_cli.client_auth.trace.service import TraceService

    outbox = TraceOutbox.open(root, owner=owner, key_protector=protector, clock=lambda: clock[0])
    return TraceService(
        owner=owner,
        outbox=outbox,
        uploader=uploader,
        plugins_toml="/sealed/ansatz-voice-trace/plugins.toml",
    ), outbox


@pytest.mark.parametrize("entrypoint_name", ["cli", "dashboard", "desktop"])
def test_three_entrypoints_share_identity_durability_and_idempotent_recovery(entrypoint_name: str) -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector
    from hermes_cli.client_auth.trace.gateway import GatewayUploader, TraceCredentialProvider
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint
    from hermes_cli.client_auth.trace.outbox import TraceOutbox, TraceOutboxUnavailable

    root = (
        Path(__file__).resolve().parents[4]
        / "tmp"
        / "three-entrypoint-e2e"
        / f"{entrypoint_name}-{uuid.uuid4()}"
    )
    root.mkdir(parents=True)
    clock = [1_000.0]
    secure_store = MemorySecureStore()
    protector = TraceKeyProtector(secure_store)
    owner = _owner()
    credential_source = RotatingCredentialSource()
    credentials = TraceCredentialProvider(credential_source, monotonic=lambda: clock[0])
    gateway = GatewayHarness()
    uploader = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=credentials,
        transport=gateway,
        now_ms=lambda: int(clock[0] * 1000),
        random=lambda: 0,
    )
    service, outbox = _open_service(root, owner, protector, uploader, clock)
    entrypoint = TraceEntrypoint.parse(entrypoint_name)
    agent_work_runs = 1

    try:
        lease = service.open_ingress(entrypoint, f"{entrypoint_name}-e2e")
        assert lease.entrypoint is entrypoint
        assert lease.installation_id == owner.installation_id == INSTALLATION_ID

        first_body = _otlp("run-offline", forged_entrypoint="desktop")
        assert _post(lease, first_body) == 200
        first = outbox.peek_eligible()
        assert first is not None
        assert first.entrypoint is entrypoint
        assert first.account_id == owner.account_id
        assert first.account_session_id == owner.session_id
        assert first.installation_id == owner.installation_id
        first_attempts = [request for request in gateway.requests if request.body == first_body]
        assert first_attempts[-1].headers["X-Trace-Entrypoint"] == entrypoint_name

        gateway.mode = "accepted"
        assert service.pump() == 1
        assert outbox.diagnostics().pending == 0
        assert agent_work_runs == 1

        gateway.mode = "offline"
        restart_body = _otlp("run-restart", forged_entrypoint="desktop")
        assert _post(lease, restart_body) == 200
        restart_batch = outbox.peek_eligible()
        assert restart_batch is not None
        service.close()

        service, outbox = _open_service(root, owner, protector, uploader, clock)
        gateway.mode = "response_loss"
        assert service.pump() == 0
        gateway.mode = "duplicate"
        assert service.pump() == 1
        restart_attempts = [
            request
            for request in gateway.requests
            if request.headers["Idempotency-Key"] == restart_batch.batch_id
        ]
        assert len(restart_attempts) >= 3
        assert {request.body for request in restart_attempts} == {restart_body}
        assert {request.headers["Idempotency-Key"] for request in restart_attempts} == {
            restart_batch.batch_id
        }
        assert outbox.diagnostics().pending == 0
        assert outbox.lookup_receipt(restart_batch.batch_id).outcome == "duplicate"
        assert agent_work_runs == 1

        lease = service.open_ingress(entrypoint, f"{entrypoint_name}-token-rotation")
        gateway.mode = "offline"
        rotation_body = _otlp("run-token-rotation", forged_entrypoint="desktop")
        assert _post(lease, rotation_body) == 200
        rotation_batch = outbox.peek_eligible()
        assert rotation_batch is not None
        gateway.mode = "unauthorized_once"
        gateway.unauthorized_issued = False
        assert service.pump() == 1
        rotation_attempts = [
            request
            for request in gateway.requests
            if request.headers["Idempotency-Key"] == rotation_batch.batch_id
        ]
        assert rotation_attempts[-2].body == rotation_attempts[-1].body == rotation_body
        assert rotation_attempts[-2].headers["Authorization"] == "Bearer trace-token-initial"
        assert rotation_attempts[-1].headers["Authorization"] == "Bearer trace-token-rotated"
        assert credentials.current().installation_id == INSTALLATION_ID
        assert credential_source.calls[-1] is True
        assert outbox.diagnostics().pending == 0
        assert agent_work_runs == 1

        with pytest.raises(TraceOutboxUnavailable, match="owner mismatch"):
            TraceOutbox.open(root, owner=_owner("account-bob"), key_protector=protector)
    finally:
        service.close()
        shutil.rmtree(root)
