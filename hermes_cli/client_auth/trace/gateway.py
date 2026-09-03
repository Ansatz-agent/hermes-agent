"""Strict idempotent upload client for the Trace Gateway."""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Protocol

from hermes_cli.client_auth.client import TraceCredential

from .outbox import DurableTraceBatch, TraceReceipt


@dataclass(frozen=True, slots=True)
class GatewayRequest:
    url: str
    headers: dict[str, str]
    body: bytes
    timeout_seconds: float = 10.0


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    status: int
    headers: Mapping[str, str]
    json_body: object | None = None


@dataclass(frozen=True, slots=True)
class Accepted:
    receipt: TraceReceipt


@dataclass(frozen=True, slots=True)
class Retry:
    reason: str
    next_retry_at_ms: int
    status: int | None = None


@dataclass(frozen=True, slots=True)
class Quarantine:
    reason: str
    status: int | None = None


@dataclass(frozen=True, slots=True)
class TerminalRevocation:
    code: str
    account_id: str
    session_id: str
    revoked_at: str


class CredentialProvider(Protocol):
    def current(self, *, force: bool = False) -> TraceCredential: ...

    def invalidate(self) -> None: ...


class TraceCredentialProvider:
    """Cache Trace credentials and collapse concurrent refreshes to one flight."""

    def __init__(
        self,
        fetch: Callable[[bool], TraceCredential],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._monotonic = monotonic
        self._condition = threading.Condition()
        self._credential: TraceCredential | None = None
        self._valid_until = 0.0
        self._refreshing = False

    def current(self, *, force: bool = False) -> TraceCredential:
        with self._condition:
            while self._refreshing:
                self._condition.wait()
                if self._credential is not None and self._valid_until > self._monotonic():
                    return self._credential
            if not force and self._credential is not None and self._valid_until > self._monotonic():
                return self._credential
            self._refreshing = True
        try:
            credential = self._fetch(force)
        except Exception:
            with self._condition:
                self._refreshing = False
                self._condition.notify_all()
            raise
        with self._condition:
            self._credential = credential
            self._valid_until = self._monotonic() + max(0, credential.expires_in - 30)
            self._refreshing = False
            self._condition.notify_all()
            return credential

    def invalidate(self) -> None:
        with self._condition:
            self._credential = None
            self._valid_until = 0.0


class GatewayUploader:
    def __init__(
        self,
        *,
        url: str,
        credential_provider: CredentialProvider,
        transport: Callable[[GatewayRequest], GatewayResponse] | None = None,
        now_ms: Callable[[], int] = lambda: int(time.time() * 1000),
        random: Callable[[], float] = __import__("random").random,
    ) -> None:
        self._url = url
        self._credentials = credential_provider
        self._transport = transport or _httpx_transport
        self._now_ms = now_ms
        self._random = random

    def send(self, batch: DurableTraceBatch) -> Accepted | Retry | Quarantine | TerminalRevocation:
        if hashlib.sha256(batch.body).hexdigest() != batch.payload_sha256:
            return Quarantine("payload_digest_mismatch")
        try:
            credential = self._credentials.current()
        except Exception:
            return self._retry("credential_unavailable", batch.attempt_count)
        if credential.installation_id != batch.installation_id:
            return self._retry("credential_installation_mismatch", batch.attempt_count)
        request = self._request(batch, credential)
        try:
            response = self._transport(request)
        except Exception:
            return self._retry("network_unavailable", batch.attempt_count)
        if response.status == 401:
            self._credentials.invalidate()
            try:
                refreshed = self._credentials.current(force=True)
            except Exception:
                return self._retry("credential_unavailable", batch.attempt_count, status=401)
            if refreshed.installation_id != batch.installation_id:
                return self._retry("credential_installation_mismatch", batch.attempt_count, status=401)
            try:
                response = self._transport(self._request(batch, refreshed))
            except Exception:
                return self._retry("network_unavailable", batch.attempt_count)
        return self._classify(batch, response)

    def _classify(
        self,
        batch: DurableTraceBatch,
        response: GatewayResponse,
    ) -> Accepted | Retry | Quarantine | TerminalRevocation:
        if 200 <= response.status < 300:
            batch_id = _header(response.headers, "x-trace-batch-id")
            outcome = _header(response.headers, "x-trace-receipt")
            if batch_id != batch.batch_id or outcome not in {"accepted", "duplicate"}:
                return self._retry(
                    "missing_or_mismatched_receipt",
                    batch.attempt_count,
                    status=response.status,
                )
            return Accepted(
                TraceReceipt(
                    batch_id=batch.batch_id,
                    outcome=outcome,
                    received_at=datetime.fromtimestamp(
                        self._now_ms() / 1000,
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                )
            )
        if response.status == 403:
            revocation = _matching_revocation(batch, response.json_body)
            if revocation is not None:
                return revocation
            return self._retry("unconfirmed_forbidden", batch.attempt_count, status=403)
        if response.status in {400, 409, 413, 415}:
            return Quarantine(f"gateway_{response.status}", status=response.status)
        retry_after = _header(response.headers, "retry-after")
        return self._retry(
            f"gateway_{response.status}",
            batch.attempt_count,
            status=response.status,
            retry_after=retry_after,
        )

    def _request(self, batch: DurableTraceBatch, credential: TraceCredential) -> GatewayRequest:
        return GatewayRequest(
            url=self._url,
            body=batch.body,
            headers={
                "Authorization": f"Bearer {credential.access_token}",
                "Content-Type": "application/x-protobuf",
                "Idempotency-Key": batch.batch_id,
                "X-Hermes-Session-Id": batch.hermes_session_id,
                "X-Telemetry-Schema-Version": "1",
                "X-Trace-Entrypoint": batch.entrypoint.value,
                "X-Trace-Payload-Sha256": batch.payload_sha256,
                "X-Trace-Run-Id": batch.run_id,
            },
        )

    def _retry(
        self,
        reason: str,
        attempt: int,
        *,
        status: int | None = None,
        retry_after: str | None = None,
    ) -> Retry:
        now = self._now_ms()
        return Retry(
            reason=reason,
            status=status,
            next_retry_at_ms=next_retry(
                attempt=attempt,
                now_ms=now,
                retry_after=retry_after,
                random=self._random,
            ),
        )


def next_retry(
    *,
    attempt: int,
    now_ms: int,
    retry_after: str | None,
    random: Callable[[], float],
) -> int:
    if attempt < 0 or now_ms < 0:
        raise ValueError("invalid Trace retry input")
    cap = min(300_000, 1_000 * (2 ** min(attempt, 9)))
    sample = random()
    normalized = min(1.0, max(0.0, sample)) if isinstance(sample, (int, float)) else 0.0
    delay = int(cap * normalized)
    if retry_after is not None and retry_after.strip().isdigit():
        seconds = int(retry_after.strip())
        if seconds <= 86_400:
            delay = max(delay, seconds * 1000)
    return now_ms + delay


def _matching_revocation(
    batch: DurableTraceBatch,
    body: object,
) -> TerminalRevocation | None:
    if not isinstance(body, dict):
        return None
    code = body.get("code")
    account_id = body.get("account_id")
    session_id = body.get("session_id")
    revoked_at = body.get("revoked_at")
    if (
        code not in {"account_disabled", "account_revoked", "session_revoked"}
        or account_id != batch.account_id
        or session_id != batch.account_session_id
        or not isinstance(revoked_at, str)
        or not revoked_at
    ):
        return None
    return TerminalRevocation(
        code=code,
        account_id=account_id,
        session_id=session_id,
        revoked_at=revoked_at,
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value.strip()
    return None


def _httpx_transport(request: GatewayRequest) -> GatewayResponse:
    import httpx

    response = httpx.post(
        request.url,
        headers=request.headers,
        content=request.body,
        timeout=request.timeout_seconds,
    )
    try:
        json_body = response.json()
    except (ValueError, TypeError):
        json_body = None
    return GatewayResponse(
        status=response.status_code,
        headers=dict(response.headers),
        json_body=json_body,
    )


__all__ = [
    "Accepted",
    "GatewayRequest",
    "GatewayResponse",
    "GatewayUploader",
    "Quarantine",
    "Retry",
    "TerminalRevocation",
    "TraceCredentialProvider",
    "next_retry",
]
