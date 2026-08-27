from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
import threading
import time
from collections.abc import Iterable
from typing import BinaryIO

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from hermes_cli.client_auth.backend_scope_protocol import (
    CONTROL_ACK_PREFIX,
    DESKTOP_SCOPE_PROTOCOL_VERSION,
)
from hermes_cli.client_auth.runtime import (
    BackendScopeTokenRegistry,
    BackendScopeTokenRejected,
    _run_backend_scope_token_control,
    local_capability_rejection_payload,
)
import hermes_cli.client_auth.runtime as auth_runtime


LOG_SENTINEL = "desktop-scope-fixture-log-sentinel"


AckSelector = tuple[str, int]
_ACK_OPERATIONS = frozenset({"scope_token_registered", "scope_token_promoted"})


def _ack_selectors(value: str) -> frozenset[AckSelector]:
    if not value:
        return frozenset()
    parsed: set[AckSelector] = set()
    for item in value.split(","):
        try:
            operation, raw_occurrence = item.rsplit(":", 1)
            occurrence = int(raw_occurrence)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(
                "ACK selectors must use operation:positive-occurrence"
            ) from None
        if operation not in _ACK_OPERATIONS or occurrence <= 0:
            raise argparse.ArgumentTypeError(
                "ACK selectors must use operation:positive-occurrence"
            )
        parsed.add((operation, occurrence))
    return frozenset(parsed)


class FaultInjectingAckTarget:
    """Manipulate encoded product ACKs without reimplementing control state."""

    def __init__(
        self,
        target: BinaryIO,
        *,
        ack_delay_ms: int,
        ack_delay_selectors: frozenset[AckSelector],
        drop_ack_selectors: frozenset[AckSelector],
        duplicate_acks: bool,
        out_of_order_acks: bool,
    ) -> None:
        self._target = target
        self._ack_delay_ms = ack_delay_ms
        self._ack_delay_selectors = ack_delay_selectors
        self._drop_ack_selectors = drop_ack_selectors
        self._duplicate_acks = duplicate_acks
        self._out_of_order_acks = out_of_order_acks
        self._lock = threading.Lock()
        self._ack_occurrences = {operation: 0 for operation in _ACK_OPERATIONS}
        self._previous_ack: bytes | None = None

    def write(self, value: bytes) -> int:
        if not value.startswith(CONTROL_ACK_PREFIX.encode("ascii")):
            raise ValueError("fixture target only accepts product scope ACKs")

        payload = json.loads(value[len(CONTROL_ACK_PREFIX) :])
        operation = payload.get("operation")
        if operation not in _ACK_OPERATIONS:
            raise ValueError("fixture target received an unknown product ACK")
        self._ack_occurrences[operation] += 1
        selector = (operation, self._ack_occurrences[operation])
        should_delay = self._ack_delay_ms > 0 and (
            not self._ack_delay_selectors or selector in self._ack_delay_selectors
        )
        if should_delay:
            time.sleep(self._ack_delay_ms / 1_000)

        if selector in self._drop_ack_selectors:
            self._previous_ack = value
            return len(value)

        with self._lock:
            if self._out_of_order_acks and self._previous_ack is not None:
                self._target.write(self._previous_ack)
            self._target.write(value)
            if self._duplicate_acks:
                self._target.write(value)
            self._target.flush()

        self._previous_ack = value
        return len(value)

    def flush(self) -> None:
        with self._lock:
            self._target.flush()

    def public_line(self, value: str) -> None:
        encoded = f"{value}\n".encode("utf-8")
        with self._lock:
            self._target.write(encoded)
            self._target.flush()


def _authorized(registry: BackendScopeTokenRegistry, request: Request, boundary: str):
    bearer = request.headers.get("X-Hermes-Session-Token", "")
    return registry.authorize(bearer, boundary)


def _rejected(error: BackendScopeTokenRejected) -> JSONResponse:
    return JSONResponse(
        status_code=401, content=local_capability_rejection_payload(error)
    )


async def _payload(request: Request) -> dict[str, object]:
    try:
        value = await request.json()
    except (UnicodeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


async def _delay(value: dict[str, object]) -> None:
    raw = value.get("delay_ms", 0)
    delay_ms = raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0
    await asyncio.sleep(max(0.0, min(float(delay_ms), 2_000.0)) / 1_000)


def create_app(registry: BackendScopeTokenRegistry) -> FastAPI:
    app = FastAPI()
    counts = {"config_writes": 0, "model_requests": 0, "trace_uploads": 0}
    ws_messages: list[str] = []

    @app.get("/api/auth/scope-token-probe")
    async def probe(request: Request):
        try:
            grant = registry.probe(request.headers.get("X-Hermes-Session-Token", ""))
        except BackendScopeTokenRejected as error:
            return _rejected(error)
        return {
            "protocol_version": DESKTOP_SCOPE_PROTOCOL_VERSION,
            "registration_id": grant.registration_id,
            "connection_id": grant.connection_id,
            "runtime_instance_id": grant.auth.runtime_instance_id,
            "epoch": grant.auth.epoch,
            "state": grant.state.value,
            "promoted_transition_id": grant.promoted_transition_id,
        }

    @app.get("/api/status")
    async def status(request: Request):
        try:
            _authorized(registry, request, "fixture.status")
        except BackendScopeTokenRejected as error:
            return _rejected(error)
        return {**counts, "ws_messages": list(ws_messages)}

    @app.put("/api/config")
    async def config(request: Request):
        try:
            _authorized(registry, request, "fixture.config")
        except BackendScopeTokenRejected as error:
            return _rejected(error)
        value = await _payload(request)
        await _delay(value)
        counts["config_writes"] += 1
        return {"ok": True, "write": counts["config_writes"]}

    @app.post("/api/model")
    async def model(request: Request):
        try:
            _authorized(registry, request, "fixture.model")
        except BackendScopeTokenRejected as error:
            return _rejected(error)
        value = await _payload(request)
        if value.get("provider_401") is True:
            return JSONResponse(
                status_code=401,
                content={
                    "code": "provider_unauthorized",
                    "detail": "provider rejected fixture request",
                },
            )
        await _delay(value)
        counts["model_requests"] += 1
        return {"ok": True, "request": counts["model_requests"]}

    @app.post("/v1/traces")
    async def traces(request: Request):
        try:
            _authorized(registry, request, "fixture.trace")
        except BackendScopeTokenRejected as error:
            return _rejected(error)
        value = await _payload(request)
        await _delay(value)
        counts["trace_uploads"] += 1
        return {"ok": True, "upload": counts["trace_uploads"]}

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        protocols = [
            item.strip()
            for item in websocket.headers.get("sec-websocket-protocol", "").split(",")
        ]
        if len(protocols) != 2 or protocols[0] != "ansatz.scope.v2":
            await websocket.close(code=4_401)
            return
        try:
            grant = registry.authorize(protocols[1], "fixture.ws.connect")
            claim = registry.ws_claim(grant)
        except BackendScopeTokenRejected:
            await websocket.close(code=4_401)
            return
        await websocket.accept(subprotocol="ansatz.scope.v2")
        try:
            while True:
                message = await websocket.receive_text()
                registry.authorize_ws_claim(claim, "fixture.ws.message")
                ws_messages.append(message)
                await websocket.send_text(message)
        except (BackendScopeTokenRejected, WebSocketDisconnect):
            return

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ack-delay-ms", type=int, default=0)
    parser.add_argument(
        "--ack-delay-selectors", type=_ack_selectors, default=frozenset()
    )
    parser.add_argument(
        "--drop-ack-selectors", type=_ack_selectors, default=frozenset()
    )
    parser.add_argument("--duplicate-acks", action="store_true")
    parser.add_argument("--out-of-order-acks", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    registry = BackendScopeTokenRegistry(
        authorize=lambda _boundary, *, expected: expected
    )
    auth_runtime.backend_scope_tokens = registry
    target = FaultInjectingAckTarget(
        sys.stdout.buffer,
        ack_delay_ms=max(0, options.ack_delay_ms),
        ack_delay_selectors=options.ack_delay_selectors,
        drop_ack_selectors=options.drop_ack_selectors,
        duplicate_acks=options.duplicate_acks,
        out_of_order_acks=options.out_of_order_acks,
    )
    control = threading.Thread(
        target=_run_backend_scope_token_control,
        args=(sys.stdin.buffer, target),
        name="scope-control",
        daemon=True,
    )
    control.start()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    target.public_line(LOG_SENTINEL)
    target.public_line(
        f"HERMES_BACKEND_READY port={port} desktop_scope_protocol={DESKTOP_SCOPE_PROTOCOL_VERSION}"
    )
    config = uvicorn.Config(
        create_app(registry),
        host="127.0.0.1",
        port=port,
        access_log=False,
        lifespan="off",
        log_level="critical",
    )
    uvicorn.Server(config).run(sockets=[listener])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
