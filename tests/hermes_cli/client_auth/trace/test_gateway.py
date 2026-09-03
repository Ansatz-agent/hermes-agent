"""Strict Trace Gateway upload contracts."""

from __future__ import annotations

def _batch():
    from hermes_cli.client_auth.trace.identity import TraceEntrypoint
    from hermes_cli.client_auth.trace.outbox import DurableTraceBatch

    return DurableTraceBatch(
        batch_id="11111111-1111-4111-8111-111111111111",
        dedupe_key="d" * 64,
        body=b"protobuf-otlp",
        entrypoint=TraceEntrypoint.DASHBOARD,
        hermes_session_id="hermes-session-1",
        run_id="run-1",
        installation_id="22222222-2222-4222-8222-222222222222",
        account_id="account-alice",
        account_session_id="auth-session-alice",
        sequence=1,
        created_at_ms=1,
        next_retry_at_ms=1,
        attempt_count=0,
        payload_sha256="8c3d9f5a6d39dc4adb26d87cdf50a92e1553ece9a087890b093cd96d75826e6e",
    )


class Provider:
    def __init__(self) -> None:
        self.calls: list[bool] = []
        self.invalidations = 0

    def current(self, *, force: bool = False):
        from hermes_cli.client_auth.client import TraceCredential

        self.calls.append(force)
        return TraceCredential(
            access_token="refreshed" if force else "initial",
            expires_at="2026-09-02T01:00:00Z",
            expires_in=3600,
            installation_id="22222222-2222-4222-8222-222222222222",
        )

    def invalidate(self) -> None:
        self.invalidations += 1


def test_exact_headers_original_protobuf_and_matching_receipts_only() -> None:
    from hermes_cli.client_auth.trace.gateway import (
        Accepted,
        GatewayResponse,
        GatewayUploader,
        Retry,
    )

    requests = []

    def transport(request):
        requests.append(request)
        return GatewayResponse(
            status=202,
            headers={
                "x-trace-batch-id": request.headers["Idempotency-Key"],
                "x-trace-receipt": "accepted",
            },
        )

    uploader = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=Provider(),
        transport=transport,
        now_ms=lambda: 1_000,
    )
    result = uploader.send(_batch())

    assert isinstance(result, Accepted)
    assert requests[0].body == b"protobuf-otlp"
    assert requests[0].headers == {
        "Authorization": "Bearer initial",
        "Content-Type": "application/x-protobuf",
        "Idempotency-Key": "11111111-1111-4111-8111-111111111111",
        "X-Hermes-Session-Id": "hermes-session-1",
        "X-Telemetry-Schema-Version": "1",
        "X-Trace-Entrypoint": "dashboard",
        "X-Trace-Payload-Sha256": "8c3d9f5a6d39dc4adb26d87cdf50a92e1553ece9a087890b093cd96d75826e6e",
        "X-Trace-Run-Id": "run-1",
    }

    def mismatch(request):
        return GatewayResponse(
            status=202,
            headers={"x-trace-batch-id": "wrong", "x-trace-receipt": "accepted"},
        )

    failed = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=Provider(),
        transport=mismatch,
        now_ms=lambda: 1_000,
    ).send(_batch())
    assert isinstance(failed, Retry)
    assert failed.reason == "missing_or_mismatched_receipt"


def test_retry_policy_handles_network_429_5xx_and_bounded_retry_after() -> None:
    from hermes_cli.client_auth.trace.gateway import (
        GatewayResponse,
        GatewayUploader,
        Retry,
        next_retry,
    )

    assert next_retry(attempt=0, now_ms=10, retry_after="120", random=lambda: 0.5) == 120_010
    assert next_retry(attempt=20, now_ms=10, retry_after=None, random=lambda: 1) == 300_010

    for response in (
        OSError("offline"),
        GatewayResponse(status=429, headers={"retry-after": "120"}),
        GatewayResponse(status=503, headers={}),
    ):
        def transport(_request, response=response):
            if isinstance(response, Exception):
                raise response
            return response

        result = GatewayUploader(
            url="https://trace.example/v1/traces",
            credential_provider=Provider(),
            transport=transport,
            now_ms=lambda: 10,
            random=lambda: 0.5,
        ).send(_batch())
        assert isinstance(result, Retry)


def test_401_forces_one_refresh_and_resends_identical_batch() -> None:
    from hermes_cli.client_auth.trace.gateway import Accepted, GatewayResponse, GatewayUploader

    provider = Provider()
    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            return GatewayResponse(status=401, headers={})
        return GatewayResponse(
            status=202,
            headers={
                "x-trace-batch-id": request.headers["Idempotency-Key"],
                "x-trace-receipt": "duplicate",
            },
        )

    result = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=provider,
        transport=transport,
        now_ms=lambda: 1_000,
    ).send(_batch())

    assert isinstance(result, Accepted)
    assert result.receipt.outcome == "duplicate"
    assert provider.calls == [False, True]
    assert provider.invalidations == 1
    assert requests[0].body == requests[1].body
    assert requests[0].headers["Idempotency-Key"] == requests[1].headers["Idempotency-Key"]


def test_structured_403_is_terminal_only_for_matching_account_and_session() -> None:
    from hermes_cli.client_auth.trace.gateway import (
        GatewayResponse,
        GatewayUploader,
        Retry,
        TerminalRevocation,
    )

    def response(account_id: str):
        return GatewayResponse(
            status=403,
            headers={},
            json_body={
                "code": "session_revoked",
                "account_id": account_id,
                "session_id": "auth-session-alice",
                "revoked_at": "2026-09-02T00:00:00Z",
            },
        )

    matching = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=Provider(),
        transport=lambda _: response("account-alice"),
    ).send(_batch())
    mismatched = GatewayUploader(
        url="https://trace.example/v1/traces",
        credential_provider=Provider(),
        transport=lambda _: response("account-bob"),
    ).send(_batch())

    assert isinstance(matching, TerminalRevocation)
    assert isinstance(mismatched, Retry)
