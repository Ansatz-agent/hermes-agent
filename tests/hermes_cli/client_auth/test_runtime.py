import base64
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.client_auth.client import (
    AuthServiceError,
    CookieRecord,
    ExplicitSessionRevocation,
    NativeSessionCredential,
    NativeSessionStatus,
    SessionRejected,
    SessionStatus,
    TraceCredential,
)
from hermes_cli.client_auth.backend_scope_protocol import CONTROL_ACK_PREFIX
from hermes_cli.client_auth.runtime import (
    LEASE_SECONDS,
    LOGIN_ATTEMPT_LIMIT,
    LockedWaitingResult,
    OWNER_IDLE_SECONDS,
    AuthRequired,
    AuthScope,
    AuthScopeChanged,
    AuthState,
    BackendScopeGrantState,
    BackendScopeTokenRegistry,
    BackendScopeTokenRejected,
    MemoryOwner,
    OwnerBroker,
    OwnerElectionContext,
    ProcessHardener,
    RuntimeConsumer,
    RuntimeSnapshot,
    CloudState,
    ValidationState,
    RemoteRuntimeOwner,
    SocketLivenessProbe,
    UnixEndpoint,
    WindowsNamedPipeEndpoint,
    VaultOwner,
    account_login,
    account_logout,
    account_status,
    account_trace_token,
    authorize_entrypoint,
    clear_entrypoint_owner,
    connect_runtime_owner,
    install_entrypoint_owner,
    install_runtime_consumer,
    is_local_auth_unavailable,
    require_authorized,
    resolve_owner,
    parse_backend_scope_token_registration,
    parse_trace_transport_registration,
    runtime_endpoint,
    start_runtime_owner,
    wait_until_authorized,
    _read_runtime_frame,
    _test_runtime_suffix,
)


INSTALLATION_ID = "11111111-1111-4111-8111-111111111111"
ACCOUNT_ID = "22222222-2222-4222-8222-222222222222"
NATIVE_SESSION_ID = "33333333-3333-4333-8333-333333333333"
BOB_ACCOUNT_ID = "44444444-4444-4444-8444-444444444444"
BOB_SESSION_ID = "55555555-5555-4555-8555-555555555555"


def _scope_bearer(seed: bytes = b"A") -> str:
    return base64.urlsafe_b64encode(seed * 32).decode("ascii").rstrip("=")


def _scope_control_id(seed: bytes) -> str:
    return base64.urlsafe_b64encode(seed * 16).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "error",
    [
        AuthRequired("invalid_response"),
        AuthRequired("rate_limited"),
        AuthRequired("runtime_unavailable"),
        AuthRequired("server_unavailable"),
        AuthRequired("vault_unavailable"),
        BackendScopeTokenRejected("expired"),
    ],
)
def test_local_auth_unavailable_classifier_accepts_only_retryable_failures(error):
    assert is_local_auth_unavailable(error)


@pytest.mark.parametrize(
    "reason",
    ["invalid_credentials", "session_expired", "session_rejected", "signed_out"],
)
def test_local_auth_unavailable_classifier_rejects_account_failures(reason):
    assert not is_local_auth_unavailable(AuthRequired(reason))


class _ControlTarget:
    def __init__(self, on_write=None):
        self.frames = []
        self.flushes = 0
        self._on_write = on_write

    def write(self, frame):
        if self._on_write is not None:
            self._on_write(frame)
        self.frames.append(frame)
        return len(frame)

    def flush(self):
        self.flushes += 1


def test_backend_scope_token_is_hashed_bounded_and_exactly_scope_bound():
    clock = FakeClock(100.0)
    current = AuthScope("0123456789abcdef0123456789abcdef", 7)

    def authorize(boundary, *, expected):
        assert boundary
        if expected != current:
            raise AuthRequired("runtime_unavailable")
        return expected

    registry = BackendScopeTokenRegistry(clock=clock, authorize=authorize)
    bearer = _scope_bearer()
    grant = registry.register(
        bearer,
        connection_id="local",
        expected=current,
        ttl_seconds=60,
    )

    assert grant.connection_id == "local"
    assert grant.auth == current
    assert grant.valid_until == 160.0
    assert bearer not in repr(registry._records)
    assert all(isinstance(digest, bytes) and len(digest) == 32 for digest in registry._records)
    assert registry.authorize(
        bearer,
        "dashboard.api.request",
        connection_id="local",
    ) == grant

    with pytest.raises(AuthRequired):
        registry.authorize(
            "local:0123456789abcdef0123456789abcdef:7",
            "dashboard.api.request",
            connection_id="local",
        )
    with pytest.raises(AuthRequired):
        registry.authorize(
            bearer,
            "dashboard.api.request",
            connection_id="remote-a",
        )


def test_backend_scope_token_expires_revokes_and_rejects_owner_epoch_change():
    clock = FakeClock(100.0)
    current = AuthScope("0123456789abcdef0123456789abcdef", 7)

    def authorize(_boundary, *, expected):
        if expected != current:
            raise AuthRequired("runtime_unavailable")
        return expected

    registry = BackendScopeTokenRegistry(clock=clock, authorize=authorize)
    bearer = _scope_bearer()
    grant = registry.register(
        bearer,
        connection_id="local",
        expected=current,
        ttl_seconds=30,
    )

    current = AuthScope(current.runtime_instance_id, current.epoch + 1)
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        registry.authorize_claim(grant.claim(), "dashboard.ws.message")

    current = grant.auth
    registry.revoke(connection_id="local", expected=grant.auth)
    with pytest.raises(BackendScopeTokenRejected, match="invalid_ws_claim"):
        registry.authorize_claim(grant.claim(), "dashboard.ws.message")

    replacement = registry.register(
        _scope_bearer(b"B"),
        connection_id="local",
        expected=current,
        ttl_seconds=30,
    )
    clock.now = replacement.valid_until
    with pytest.raises(BackendScopeTokenRejected, match="expired"):
        registry.authorize_claim(replacement.claim(), "dashboard.ws.message")


def test_scope_token_control_frame_is_closed_and_rejects_schema_drift():
    frame = {
        "version": 1,
        "operation": "register_scope_token",
        "bearer": _scope_bearer(),
        "connection_id": "local",
        "runtime_instance_id": "0123456789abcdef0123456789abcdef",
        "epoch": 7,
        "ttl_seconds": 45,
    }

    parsed = parse_backend_scope_token_registration(frame)
    assert parsed.connection_id == "local"
    assert parsed.auth == AuthScope(frame["runtime_instance_id"], 7)
    assert parsed.ttl_seconds == 45

    with pytest.raises(AuthRequired):
        parse_backend_scope_token_registration({**frame, "unknown": True})
    with pytest.raises(AuthRequired):
        parse_backend_scope_token_registration({**frame, "ttl_seconds": 61})


def test_trace_transport_control_frame_is_closed_and_loopback_only():
    frame = {
        "version": 1,
        "operation": "register_trace_transport",
        "endpoint": "http://127.0.0.1:49152/v1/traces",
        "authorization": "Bearer " + "a" * 43,
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "entrypoint": "desktop",
        "plugins_toml": "/opt/Ansatz/config/ansatz-voice-trace/plugins.toml",
    }

    parsed = parse_trace_transport_registration(frame)
    assert parsed.endpoint == frame["endpoint"]
    assert "a" * 43 not in repr(parsed)

    with pytest.raises(AuthRequired):
        parse_trace_transport_registration({**frame, "endpoint": "https://example.com/v1/traces"})
    with pytest.raises(AuthRequired):
        parse_trace_transport_registration({**frame, "unknown": True})


def test_running_backend_control_attaches_trace_transport_without_restart(monkeypatch):
    from agent import relay_runtime
    from hermes_cli.client_auth import runtime

    registrations = []
    monkeypatch.setattr(
        relay_runtime,
        "register_ansatz_product_trace_transport",
        lambda **transport: registrations.append(transport),
    )
    frame = json.dumps(
        {
            "version": 1,
            "operation": "register_trace_transport",
            "endpoint": "http://127.0.0.1:49152/v1/traces",
            "authorization": "Bearer " + "a" * 43,
            "installation_id": "11111111-1111-4111-8111-111111111111",
            "entrypoint": "desktop",
            "plugins_toml": "/opt/Ansatz/config/ansatz-voice-trace/plugins.toml",
        }
    ).encode() + b"\n"

    class Stream:
        sent = False

        def readline(self, _limit):
            if self.sent:
                return b""
            self.sent = True
            return frame

    target = _ControlTarget()
    runtime._run_backend_scope_token_control(Stream(), target)

    assert len(registrations) == 1
    assert registrations[0]["endpoint"] == "http://127.0.0.1:49152/v1/traces"
    assert target.frames == []


def test_invalid_trace_control_frame_isolated_from_scope_and_later_registration(
    monkeypatch,
):
    from agent import relay_runtime
    from hermes_cli.client_auth import runtime

    current = AuthScope("0123456789abcdef0123456789abcdef", 7)
    registry = BackendScopeTokenRegistry(authorize=lambda _boundary, *, expected: expected)
    monkeypatch.setattr(runtime, "backend_scope_tokens", registry)
    registered = []
    monkeypatch.setattr(
        relay_runtime,
        "register_ansatz_product_trace_transport",
        lambda **transport: registered.append(transport),
    )
    bearer = _scope_bearer()
    registration_id = _scope_control_id(b"R")
    scope_frame = {
        "version": 2,
        "operation": "register_scope_token",
        "registration_id": registration_id,
        "bearer": bearer,
        "connection_id": "local",
        "runtime_instance_id": current.runtime_instance_id,
        "epoch": current.epoch,
        "ttl_seconds": 1_800,
    }
    promote_frame = {
        "version": 2,
        "operation": "promote_scope_token",
        "transition_id": _scope_control_id(b"T"),
        "registration_id": registration_id,
        "previous_registration_id": None,
        "connection_id": "local",
        "runtime_instance_id": current.runtime_instance_id,
        "epoch": current.epoch,
        "overlap_seconds": 60,
    }
    trace_frame = {
        "version": 1,
        "operation": "register_trace_transport",
        "endpoint": "http://127.0.0.1:49152/v1/traces",
        "authorization": "Bearer " + "a" * 43,
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "entrypoint": "desktop",
        "plugins_toml": "/opt/Ansatz/config/ansatz-voice-trace/plugins.toml",
    }
    frames = [
        scope_frame,
        {**trace_frame, "endpoint": "https://invalid.example/v1/traces"},
        trace_frame,
        promote_frame,
    ]

    class ObservingStream:
        index = 0

        def readline(self, _limit):
            if self.index == 1:
                assert registry.probe(bearer).state is BackendScopeGrantState.CANDIDATE
            if self.index == 3:
                assert len(registered) == 1
            if self.index == 4:
                assert registry.authorize(
                    bearer,
                    "dashboard.api.request",
                ).state is BackendScopeGrantState.ACTIVE
                return b""
            frame = json.dumps(frames[self.index]).encode() + b"\n"
            self.index += 1
            return frame

    stream = ObservingStream()
    target = _ControlTarget()
    runtime._run_backend_scope_token_control(stream, target)
    assert stream.index == 4
    assert len(registered) == 1
    assert len(target.frames) == 2


def test_recoverable_promotion_rejection_keeps_control_reader_and_active_grant(
    monkeypatch,
):
    from hermes_cli.client_auth import runtime

    current = AuthScope("0123456789abcdef0123456789abcdef", 7)
    registry = BackendScopeTokenRegistry(
        authorize=lambda _boundary, *, expected: expected
    )
    monkeypatch.setattr(runtime, "backend_scope_tokens", registry)

    bearers = {name: _scope_bearer(name.encode("ascii")) for name in "ABCD"}
    registration_ids = {
        name: _scope_control_id(name.encode("ascii")) for name in "ABCD"
    }

    def register(name):
        return {
            "version": 2,
            "operation": "register_scope_token",
            "registration_id": registration_ids[name],
            "bearer": bearers[name],
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "ttl_seconds": 1_800,
        }

    def promote(name, previous):
        return {
            "version": 2,
            "operation": "promote_scope_token",
            "transition_id": _scope_control_id(name.lower().encode("ascii")),
            "registration_id": registration_ids[name],
            "previous_registration_id": (
                registration_ids[previous] if previous is not None else None
            ),
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "overlap_seconds": 60,
        }

    frames = [
        register("A"),
        promote("A", None),
        register("B"),
        promote("B", "A"),
        register("C"),
        # Simulate a lost B promotion ACK: the client still believes A is active.
        promote("C", "A"),
        register("D"),
        promote("D", "B"),
    ]

    class ObservingStream:
        def __init__(self):
            self.index = 0
            self.observed_final_active = False

        def readline(self, _limit):
            if self.index == len(frames):
                assert registry.authorize(
                    bearers["D"],
                    "dashboard.api.request",
                ).state is BackendScopeGrantState.ACTIVE
                self.observed_final_active = True
                return b""
            frame = json.dumps(frames[self.index]).encode("utf-8") + b"\n"
            self.index += 1
            return frame

    stream = ObservingStream()
    target = _ControlTarget()
    runtime._run_backend_scope_token_control(stream, target)

    assert stream.index == len(frames)
    assert stream.observed_final_active
    assert len(target.frames) == 7
    assert target.flushes == 7


def test_authoritative_scope_revocation_still_terminates_control_reader(
    monkeypatch,
):
    from hermes_cli.client_auth import runtime

    current = AuthScope("0123456789abcdef0123456789abcdef", 7)
    authorized = True

    def authorize(_boundary, *, expected):
        if not authorized:
            raise AuthRequired("runtime_unavailable")
        return expected

    registry = BackendScopeTokenRegistry(authorize=authorize)
    monkeypatch.setattr(runtime, "backend_scope_tokens", registry)
    registration_id = _scope_control_id(b"A")
    bearer = _scope_bearer(b"A")
    frames = [
        {
            "version": 2,
            "operation": "register_scope_token",
            "registration_id": registration_id,
            "bearer": bearer,
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "ttl_seconds": 1_800,
        },
        {
            "version": 2,
            "operation": "promote_scope_token",
            "transition_id": _scope_control_id(b"a"),
            "registration_id": registration_id,
            "previous_registration_id": None,
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "overlap_seconds": 60,
        },
        {
            "version": 2,
            "operation": "register_scope_token",
            "registration_id": _scope_control_id(b"B"),
            "bearer": _scope_bearer(b"B"),
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "ttl_seconds": 1_800,
        },
        {"unreachable": True},
    ]

    class RevokingStream:
        def __init__(self):
            self.index = 0

        def readline(self, _limit):
            nonlocal authorized
            if self.index == 2:
                authorized = False
            frame = json.dumps(frames[self.index]).encode("utf-8") + b"\n"
            self.index += 1
            return frame

    stream = RevokingStream()
    target = _ControlTarget()
    runtime._run_backend_scope_token_control(stream, target)

    assert stream.index == 3
    assert len(target.frames) == 2
    assert target.flushes == 2
    with pytest.raises(BackendScopeTokenRejected, match="unknown_token"):
        registry.authorize(bearer, "dashboard.api.request")


def test_scope_token_control_eof_revokes_every_registered_bearer(monkeypatch):
    from hermes_cli.client_auth import runtime

    promoted = threading.Event()
    release_eof = threading.Event()
    current = AuthScope("0123456789abcdef0123456789abcdef", 7)

    def authorize(_boundary, *, expected):
        assert expected == current
        return expected

    registry = BackendScopeTokenRegistry(authorize=authorize)
    monkeypatch.setattr(runtime, "backend_scope_tokens", registry)
    bearer = _scope_bearer()
    registration_id = _scope_control_id(b"R")
    transition_id = _scope_control_id(b"T")
    frames = [
        {
            "version": 2,
            "operation": "register_scope_token",
            "registration_id": registration_id,
            "bearer": bearer,
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "ttl_seconds": 1_800,
        },
        {
            "version": 2,
            "operation": "promote_scope_token",
            "transition_id": transition_id,
            "registration_id": registration_id,
            "previous_registration_id": None,
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "overlap_seconds": 60,
        },
    ]

    class BlockingStream:
        def __init__(self):
            self.index = 0

        def readline(self, _limit):
            if self.index < len(frames):
                frame = json.dumps(frames[self.index]).encode("utf-8") + b"\n"
                self.index += 1
                return frame
            release_eof.wait(timeout=2)
            return b""

    def observe_ack(frame):
        assert bearer.encode("ascii") not in frame
        payload = json.loads(
            frame.removeprefix(CONTROL_ACK_PREFIX.encode("ascii"))
        )
        if payload["operation"] == "scope_token_registered":
            assert set(payload) == {
                "version",
                "operation",
                "registration_id",
                "connection_id",
                "runtime_instance_id",
                "epoch",
                "ttl_seconds",
            }
            assert registry.probe(bearer).state is BackendScopeGrantState.CANDIDATE
        elif payload["operation"] == "scope_token_promoted":
            assert set(payload) == {
                "version",
                "operation",
                "transition_id",
                "registration_id",
                "previous_registration_id",
                "connection_id",
                "runtime_instance_id",
                "epoch",
                "overlap_seconds",
            }
            assert registry.authorize(
                bearer,
                "dashboard.api.request",
            ).state is BackendScopeGrantState.ACTIVE
            promoted.set()
        else:
            pytest.fail(f"unexpected control ACK: {payload}")

    target = _ControlTarget(on_write=observe_ack)

    control = threading.Thread(
        target=runtime._run_backend_scope_token_control,
        args=(BlockingStream(), target),
    )
    control.start()
    assert promoted.wait(timeout=2)
    grant = registry.authorize(bearer, "dashboard.api.request")
    claim = registry.ws_claim(grant)
    assert len(target.frames) == 2
    assert target.flushes == 2

    release_eof.set()
    control.join(timeout=2)
    assert not control.is_alive()
    with pytest.raises(BackendScopeTokenRejected):
        registry.authorize(bearer, "dashboard.api.request")
    with pytest.raises(BackendScopeTokenRejected, match="backend_generation_changed"):
        registry.authorize_ws_claim(claim, "dashboard.ws.message")


def status_at(*, server_second: int = 0, expiry_second: int = 120) -> SessionStatus:
    origin = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    return SessionStatus(
        sub="7",
        username="alice",
        role="user",
        server_time=(origin + timedelta(seconds=server_second)).isoformat(),
        session_expires_at=(origin + timedelta(seconds=expiry_second)).isoformat(),
        trace_dashboard_url="/traces/",
    )


def test_new_owner_uses_fresh_instance_and_old_scope_never_revives():
    first = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    old_scope = first.scope
    second = RuntimeSnapshot.new_authenticated("alice", now=20.0, ttl=60.0)

    assert first.runtime_instance_id != second.runtime_instance_id
    with pytest.raises(AuthRequired) as caught:
        second.require_authorized("tool", expected=old_scope, now=21.0)
    assert caught.value.reason == "runtime_unavailable"


def test_dead_liveness_connection_overrides_cached_authenticated_state():
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    consumer = RuntimeConsumer(state, liveness_probe=lambda: False)

    with pytest.raises(AuthRequired) as caught:
        consumer.require_authorized("gateway.request", now=11.0)

    assert caught.value.reason == "runtime_unavailable"
    assert consumer.snapshot().reason == "runtime_unavailable"


def test_expiry_and_epoch_comparison_fail_closed():
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)

    with pytest.raises(AuthScopeChanged, match="runtime_unavailable"):
        state.require_authorized(
            "worker",
            expected=AuthScope(state.runtime_instance_id, state.epoch + 1),
            now=11.0,
        )
    with pytest.raises(AuthRequired, match="session_expired"):
        state.require_authorized("worker", expected=state.scope, now=70.0)


def test_server_time_is_converted_to_a_bounded_monotonic_lease():
    state = RuntimeSnapshot.from_session_status(status_at(), now=500.0)

    assert state.valid_until == 500.0 + LEASE_SECONDS
    assert state.session_expires_at == status_at().session_expires_at


def test_absolute_expiry_shorter_than_lease_wins():
    state = RuntimeSnapshot.from_session_status(
        status_at(expiry_second=15),
        now=500.0,
    )

    assert state.valid_until == 515.0


def test_refresh_can_never_extend_the_original_absolute_expiry():
    original = RuntimeSnapshot.from_session_status(
        status_at(expiry_second=90),
        now=100.0,
    )
    response_claiming_later_expiry = status_at(server_second=30, expiry_second=300)

    refreshed = original.refreshed(response_claiming_later_expiry, now=130.0)

    assert refreshed.runtime_instance_id == original.runtime_instance_id
    assert refreshed.epoch == original.epoch
    assert refreshed.session_expires_at == original.session_expires_at
    assert refreshed.valid_until == 190.0


def test_non_positive_absolute_remaining_is_rejected():
    with pytest.raises(AuthRequired, match="session_expired"):
        RuntimeSnapshot.from_session_status(
            status_at(server_second=30, expiry_second=30),
            now=100.0,
        )


def test_boot_identity_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime._read_boot_id",
        lambda: "boot-a",
    )
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime._read_boot_id",
        lambda: "boot-b",
    )

    with pytest.raises(AuthRequired, match="session_expired"):
        state.require_authorized("tool", expected=state.scope, now=11.0)


def _assert_child_does_not_keep_owner_socket_alive() -> None:
    owner_socket, consumer_socket = socket.socketpair()
    probe = SocketLivenessProbe(consumer_socket)
    state = RuntimeSnapshot.new_authenticated("alice", now=10.0, ttl=60.0)
    consumer = RuntimeConsumer(state, liveness_probe=probe)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys; fd=int(sys.argv[1]); "
                    "\ntry: os.fstat(fd)"
                    "\nexcept OSError: raise SystemExit(0)"
                    "\nraise SystemExit(3)"
                ),
                str(owner_socket.fileno()),
            ],
            close_fds=True,
            check=False,
        )
        assert result.returncode == 0
        owner_socket.close()
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            consumer.require_authorized("child.boundary", now=11.0)
    finally:
        owner_socket.close()
        probe.close()


@pytest.mark.macos_only
def test_macos_child_cannot_inherit_owner_connection():
    _assert_child_does_not_keep_owner_socket_alive()


@pytest.mark.linux_only
def test_linux_child_cannot_inherit_owner_connection():
    _assert_child_does_not_keep_owner_socket_alive()


def test_socket_liveness_probe_marks_descriptor_non_inheritable():
    owner_socket, consumer_socket = socket.socketpair()
    probe = SocketLivenessProbe(consumer_socket)
    try:
        assert os.get_inheritable(consumer_socket.fileno()) is False
    finally:
        owner_socket.close()
        probe.close()


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeSecretBackend:
    def __init__(self, raw: str | None = None, *, fail_reads: bool = False, fail_writes: bool = False) -> None:
        self.raw = raw
        self.fail_reads = fail_reads
        self.fail_writes = fail_writes
        self.read_count = 0
        self.write_count = 0
        self.delete_count = 0

    def read(self) -> str | None:
        self.read_count += 1
        if self.fail_reads:
            raise RuntimeError("backend details must be redacted")
        return self.raw

    def write(self, raw: str) -> None:
        self.write_count += 1
        if self.fail_writes:
            raise RuntimeError("backend details must be redacted")
        self.raw = raw

    def delete(self) -> None:
        self.delete_count += 1
        self.raw = None


class FakeAuthClient:
    def __init__(self) -> None:
        self.record = CookieRecord(
            cookies={
                "__Host-ansatz_sessionid": "session-1",
                "__Host-ansatz_csrftoken": "csrf-1",
            },
            username="alice",
            session_expires_at=status_at().session_expires_at,
        )
        self.status_value = status_at()
        self.login_error: AuthServiceError | None = None
        self.status_error: AuthServiceError | None = None
        self.native_status_error: AuthServiceError | None = None
        self.logout_error: AuthServiceError | None = None
        self.login_calls = 0
        self.status_calls = 0
        self.native_status_calls = 0
        self.native_issue_calls = 0
        self.logout_calls = 0
        self.trace_token_calls: list[dict[str, str]] = []
        self.events: list[str] = []
        self.password_refs: list[bytearray] = []

    def login(self, username: str, password: bytearray) -> CookieRecord:
        self.login_calls += 1
        self.events.append("login")
        self.password_refs.append(password)
        assert username == "alice"
        assert password == bytearray(b"secret")
        if self.login_error is not None:
            raise self.login_error
        return self.record

    def status(self, cookies: dict[str, str]) -> SessionStatus:
        self.status_calls += 1
        assert cookies == self.record.cookies
        if self.status_error is not None:
            raise self.status_error
        return self.status_value

    def logout(self, cookies: dict[str, str]) -> None:
        self.logout_calls += 1
        assert cookies == self.record.cookies
        if self.logout_error is not None:
            raise self.logout_error

    def trace_token(self, cookies: dict[str, str], **params: str) -> TraceCredential:
        assert cookies == self.record.cookies
        self.trace_token_calls.append(params)
        return TraceCredential(
            access_token="trace-token-sentinel-1234567890",
            expires_at="2099-08-23T14:15:00+00:00",
            expires_in=900,
            installation_id=params["installation_id"],
        )

    def legacy_trace_token(self, cookies: dict[str, str], **params: str) -> TraceCredential:
        return self.trace_token(cookies, **params)

    def legacy_status(self, cookies: dict[str, str]) -> SessionStatus:
        return self.status(cookies)

    def legacy_logout(self, cookies: dict[str, str]) -> None:
        self.logout(cookies)

    def logout_client_session(self, _credential: NativeSessionCredential) -> None:
        self.logout_calls += 1

    def issue_client_session(
        self, cookies: dict[str, str], *, installation_id: str, client_version: str
    ) -> NativeSessionCredential:
        assert cookies == self.record.cookies
        assert client_version == "0.17.0"
        self.native_issue_calls += 1
        return NativeSessionCredential(
            account_id=ACCOUNT_ID,
            session_id=NATIVE_SESSION_ID,
            session_token="session-token-sentinel-1234567890",
            installation_id=installation_id,
            username="alice",
            issued_at="2026-08-24T12:00:00+00:00",
        )

    def client_session_status(
        self, credential: NativeSessionCredential
    ) -> NativeSessionStatus:
        assert credential.account_id == ACCOUNT_ID
        self.native_status_calls += 1
        if self.native_status_error is not None:
            raise self.native_status_error
        return NativeSessionStatus(
            account_id=ACCOUNT_ID,
            session_id=NATIVE_SESSION_ID,
            installation_id=credential.installation_id,
            username="alice",
            server_time="2026-08-24T12:00:00+00:00",
        )


class BlockingNativeAuthClient(FakeAuthClient):
    """Deterministically holds Alice validation while a mutation proceeds."""

    def __init__(self) -> None:
        super().__init__()
        self.validation_started = threading.Event()
        self.release_validation = threading.Event()
        self._last_username = "alice"

    def login(self, username: str, password: bytearray) -> CookieRecord:
        assert username in {"alice", "bob"}
        assert password == bytearray(b"secret")
        self._last_username = username
        return CookieRecord(
            cookies={
                "__Host-ansatz_sessionid": f"session-{username}",
                "__Host-ansatz_csrftoken": f"csrf-{username}",
            },
            username=username,
            session_expires_at=status_at().session_expires_at,
        )

    def issue_client_session(
        self, _cookies: dict[str, str], *, installation_id: str, client_version: str
    ) -> NativeSessionCredential:
        assert client_version == "0.17.0"
        account_id, session_id = (
            (ACCOUNT_ID, NATIVE_SESSION_ID)
            if self._last_username == "alice"
            else (BOB_ACCOUNT_ID, BOB_SESSION_ID)
        )
        return NativeSessionCredential(
            account_id=account_id,
            session_id=session_id,
            session_token=f"session-token-{self._last_username}-sentinel-1234567890",
            installation_id=installation_id,
            username=self._last_username,
            issued_at="2026-08-24T12:00:00+00:00",
        )

    def client_session_status(
        self, credential: NativeSessionCredential
    ) -> NativeSessionStatus:
        if credential.account_id == ACCOUNT_ID:
            self.validation_started.set()
            assert self.release_validation.wait(timeout=2)
        if self.native_status_error is not None:
            raise self.native_status_error
        return NativeSessionStatus(
            account_id=credential.account_id,
            session_id=credential.session_id,
            installation_id=credential.installation_id,
            username=credential.username,
            server_time="2026-08-24T12:00:00+00:00",
        )


def test_trace_token_waits_for_native_validation_client_call():
    class ConcurrencyDetectingClient(BlockingNativeAuthClient):
        def __init__(self):
            super().__init__()
            self.concurrent_trace_token = threading.Event()

        def trace_token(self, credential):
            assert isinstance(credential, NativeSessionCredential)
            if self.validation_started.is_set() and not self.release_validation.is_set():
                self.concurrent_trace_token.set()
            return TraceCredential(
                access_token="trace-token-sentinel-1234567890",
                expires_at="2099-08-23T14:15:00+00:00",
                expires_in=900,
                installation_id=credential.installation_id,
            )

    client = ConcurrencyDetectingClient()
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=FakeSecretBackend(),
        clock=FakeClock(),
        jitter=lambda _low, _high: 0.5,
    )
    owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    validation = threading.Thread(target=owner.validate_now)
    trace = threading.Thread(
        target=lambda: owner.trace_token(
            installation_id=INSTALLATION_ID,
            client_version="0.17.0",
            telemetry_schema_version="1",
        )
    )
    validation.start()
    assert client.validation_started.wait(timeout=1)
    trace.start()
    try:
        assert not client.concurrent_trace_token.wait(timeout=0.25)
    finally:
        client.release_validation.set()
        validation.join(timeout=2)
        trace.join(timeout=2)
    assert not validation.is_alive()
    assert not trace.is_alive()


def test_trace_token_rechecks_cloud_state_after_waiting_for_failed_validation():
    class FailingValidationClient(BlockingNativeAuthClient):
        def __init__(self):
            super().__init__()
            self.trace_calls = 0

        def trace_token(self, credential):
            self.trace_calls += 1
            return TraceCredential(
                access_token="must-not-be-issued",
                expires_at="2099-08-23T14:15:00+00:00",
                expires_in=900,
                installation_id=credential.installation_id,
            )

    client = FailingValidationClient()
    client.native_status_error = AuthServiceError("server_unavailable")
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=FakeSecretBackend(),
        clock=FakeClock(),
        jitter=lambda _low, _high: 0.5,
    )
    owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    validation = threading.Thread(target=owner.validate_now)
    trace_failures: list[AuthRequired] = []

    def issue_trace() -> None:
        try:
            owner.trace_token(
                installation_id=INSTALLATION_ID,
                client_version="0.17.0",
                telemetry_schema_version="1",
            )
        except AuthRequired as error:
            trace_failures.append(error)

    trace = threading.Thread(target=issue_trace)
    validation.start()
    assert client.validation_started.wait(timeout=1)
    trace.start()
    client.release_validation.set()
    validation.join(timeout=2)
    trace.join(timeout=2)

    assert [error.reason for error in trace_failures] == ["server_unavailable"]
    assert client.trace_calls == 0


class BlockingBobWriteBackend(FakeSecretBackend):
    """Stops immediately after Bob's blob reaches the backend."""

    def __init__(self, raw: str | None = None) -> None:
        super().__init__(raw=raw)
        self.bob_persisted = threading.Event()
        self.release_bob_persist = threading.Event()
        self.alice_stale_persisted = threading.Event()
        self._blocked = False

    def write(self, raw: str) -> None:
        super().write(raw)
        account_id = json.loads(raw).get("account_id")
        if self._blocked and account_id == ACCOUNT_ID:
            self.alice_stale_persisted.set()
        if not self._blocked and account_id == BOB_ACCOUNT_ID:
            self._blocked = True
            self.bob_persisted.set()
            assert self.release_bob_persist.wait(timeout=2)


class BlockingLegacyUpgradeClient(BlockingNativeAuthClient):
    def __init__(self) -> None:
        super().__init__()
        self.legacy_validation_started = threading.Event()
        self.release_legacy_validation = threading.Event()

    def legacy_status(self, _cookies: dict[str, str]) -> SessionStatus:
        self.legacy_validation_started.set()
        assert self.release_legacy_validation.wait(timeout=2)
        return status_at()

    def issue_client_session(
        self, cookies: dict[str, str], *, installation_id: str, client_version: str
    ) -> NativeSessionCredential:
        if cookies["__Host-ansatz_sessionid"] == "session-1":
            return NativeSessionCredential(
                account_id=ACCOUNT_ID,
                session_id=NATIVE_SESSION_ID,
                session_token="session-token-alice-sentinel-1234567890",
                installation_id=installation_id,
                username="alice",
                issued_at="2026-08-24T12:00:00+00:00",
            )
        return super().issue_client_session(
            cookies, installation_id=installation_id, client_version=client_version
        )


def blocking_native_owner_factory():
    clock = FakeClock()
    backend = FakeSecretBackend()
    client = BlockingNativeAuthClient()
    owner = VaultOwner(
        client,
        secret_backend=backend,
        clock=clock,
        jitter=lambda *_: 1.0,
    )
    return owner, backend, client, clock


def native_owner_factory(**backend_options):
    clock = FakeClock()
    backend = FakeSecretBackend(**backend_options)
    client = FakeAuthClient()
    owner = VaultOwner(
        client,
        secret_backend=backend,
        clock=clock,
        jitter=lambda _low, _high: 1.0,
    )
    return owner, backend, client, clock


@pytest.mark.parametrize(
    "reason",
    [
        "server_unavailable",
        "rate_limited",
        "invalid_response",
        "invalid_session_credential",
        "runtime_unavailable",
    ],
)
def test_native_validation_failure_preserves_scope_and_cached_authorization(reason):
    owner, backend, client, clock = native_owner_factory()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    client.native_status_error = AuthServiceError(reason)
    clock.now = owner.next_refresh_at

    degraded = owner.validate_now()

    assert degraded.state is AuthState.AUTHENTICATED
    assert degraded.scope == active.scope
    assert (degraded.account_id, degraded.session_id) == (
        active.account_id,
        active.session_id,
    )
    assert (degraded.validation_state, degraded.validation_reason) == (
        ValidationState.DEGRADED,
        reason,
    )
    assert backend.raw is not None


def test_desktop_local_continuity_is_explicit_and_does_not_enable_cloud_features():
    owner, _backend, client, clock = native_owner_factory()
    owner.enable_desktop_local_continuity()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    local = owner.connect_consumer(allow_local_continuity=True)
    strict = owner.connect_consumer(allow_local_continuity=False)
    client.native_status_error = AuthServiceError("server_unavailable")
    clock.now = owner.next_refresh_at

    degraded = owner.validate_now()

    assert degraded.state is AuthState.AUTHENTICATED
    assert degraded.cloud_state is CloudState.UNREACHABLE
    assert degraded.scope == active.scope
    assert local.require_authorized(
        "desktop.local.file",
        expected=active.scope,
    ) == active.scope
    with pytest.raises(AuthRequired, match="server_unavailable"):
        strict.require_authorized("cli.tool", expected=active.scope)
    with pytest.raises(AuthRequired, match="server_unavailable"):
        owner.trace_token(
            installation_id=INSTALLATION_ID,
            client_version="0.17.0",
            telemetry_schema_version="1",
        )


def test_consumer_cannot_request_local_continuity_before_desktop_owner_enables_it():
    owner, _backend, client, clock = native_owner_factory()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    requested = owner.connect_consumer(allow_local_continuity=True)
    client.native_status_error = AuthServiceError("server_unavailable")
    clock.now = owner.next_refresh_at

    owner.validate_now()

    with pytest.raises(AuthRequired, match="server_unavailable"):
        requested.require_authorized(
            "desktop.local.not-enabled",
            expected=active.scope,
        )


def test_reauth_required_keeps_only_explicit_desktop_local_authorization():
    active = RuntimeSnapshot.new_authenticated("alice", now=0.0, ttl=1.0)
    degraded = active.degraded("session_expired")

    assert degraded.cloud_state is CloudState.REAUTH_REQUIRED
    with pytest.raises(AuthRequired, match="session_expired"):
        degraded.require_authorized(
            "cli.after-expiry",
            expected=active.scope,
            now=2.0,
        )
    assert degraded.require_authorized(
        "desktop.local.after-expiry",
        expected=active.scope,
        now=2.0,
        allow_local_continuity=True,
    ) == active.scope


def test_desktop_owner_restores_trusted_native_scope_offline_but_strict_consumer_stays_locked():
    first, backend, _client, _clock = native_owner_factory()
    active = first.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    restarted = VaultOwner(
        FakeAuthClient(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    restarted.enable_desktop_local_continuity()

    restored = restarted.refresh()

    assert restored.state is AuthState.AUTHENTICATED
    assert restored.cloud_state is CloudState.UNREACHABLE
    assert restored.username == "alice"
    assert restored.runtime_instance_id != active.runtime_instance_id
    assert restarted.connect_consumer(
        allow_local_continuity=True
    ).require_authorized(
        "desktop.local.restart",
        expected=restored.scope,
    ) == restored.scope
    with pytest.raises(AuthRequired, match="server_unavailable"):
        restarted.connect_consumer(
            allow_local_continuity=False
        ).require_authorized(
            "cli.restart",
            expected=restored.scope,
        )


def test_logout_persists_secret_free_tombstone_that_blocks_offline_restart():
    owner, backend, _client, _clock = native_owner_factory()
    owner.enable_desktop_local_continuity()
    owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )

    signed_out = owner.logout()

    payload = json.loads(backend.raw)
    assert payload == {"kind": "signed_out", "reason": "signed_out", "version": 3}
    assert "session_token" not in backend.raw
    restarted = VaultOwner(
        FakeAuthClient(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    restarted.enable_desktop_local_continuity()
    restored = restarted.refresh()
    assert restored.state is AuthState.SIGNED_OUT
    assert restored.cloud_state is None
    with pytest.raises(AuthRequired, match="signed_out"):
        restarted.connect_consumer(
            allow_local_continuity=True
        ).require_authorized("desktop.after-logout", expected=signed_out.scope)


def test_logout_removes_old_credential_when_tombstone_write_fails():
    owner, backend, _client, _clock = native_owner_factory()
    owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    backend.fail_writes = True

    signed_out = owner.logout()

    assert signed_out.state is AuthState.SIGNED_OUT
    assert backend.raw is None
    restarted = VaultOwner(
        FakeAuthClient(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    assert restarted.refresh().state is AuthState.SIGNED_OUT


def test_desktop_local_continuity_never_opens_first_install_or_unreadable_vault():
    first_install, _backend, _client, _clock = native_owner_factory()
    first_install.enable_desktop_local_continuity()
    snapshot = first_install.refresh()
    assert snapshot.state is AuthState.SIGNED_OUT
    assert snapshot.cloud_state is None

    unreadable, _backend, _client, _clock = native_owner_factory(fail_reads=True)
    unreadable.enable_desktop_local_continuity()
    with pytest.raises(AuthRequired, match="vault_unavailable"):
        unreadable.refresh()
    assert unreadable.snapshot().state is AuthState.LOCKED
    assert unreadable.snapshot().cloud_state is None


def test_authoritative_legacy_session_rejection_tombstones_and_locks_desktop_local_access():
    owner, backend, client, _clock = vault_owner_factory()
    owner.enable_desktop_local_continuity()
    active = owner.login("alice", bytearray(b"secret"))
    client.status_error = SessionRejected()

    with pytest.raises(AuthRequired, match="session_rejected"):
        owner.refresh()

    locked = owner.snapshot()
    assert locked.state is AuthState.LOCKED
    assert locked.cloud_state is None
    assert locked.epoch == active.epoch + 1
    assert json.loads(backend.raw) == {
        "kind": "signed_out",
        "reason": "session_rejected",
        "version": 3,
    }
    restarted = VaultOwner(
        FakeAuthClient(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    restarted.enable_desktop_local_continuity()
    assert restarted.refresh().state is AuthState.LOCKED


def test_native_cache_restores_before_network_after_process_restart():
    first, backend, client, _ = native_owner_factory()
    logged_in = first.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    client.native_status_error = AuthServiceError("server_unavailable")
    restarted = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )

    restored = restarted.refresh()

    assert restored.state is AuthState.AUTHENTICATED
    assert (restored.account_id, restored.session_id) == (
        logged_in.account_id,
        logged_in.session_id,
    )
    assert restored.runtime_instance_id != logged_in.runtime_instance_id
    assert client.native_status_calls == 0


def test_native_cache_durably_carries_legacy_predecessor_across_commit_restart_and_write_failure(
):
    backend = FakeSecretBackend()
    client = FakeAuthClient()
    first = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda low, high: (low + high) / 2,
    )
    legacy = first.login("alice", bytearray(b"secret"))

    native = first.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    assert native.predecessor_principal_key == legacy.principal_key
    assert json.loads(backend.raw)["predecessor_principal_key"] == legacy.principal_key

    restarted = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    assert restarted.refresh().predecessor_principal_key == legacy.principal_key

    replacement = first.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    assert replacement.predecessor_principal_key is None

    switched_backend = FakeSecretBackend()
    switched_client = FakeAuthClient()
    switched = VaultOwner(
        switched_client,
        secret_backend=switched_backend,
        clock=FakeClock(),
        jitter=lambda low, high: (low + high) / 2,
    )
    switched.login("alice", bytearray(b"secret"))
    switched_client.record = CookieRecord(
        cookies={
            "__Host-ansatz_sessionid": "different-bootstrap-session",
            "__Host-ansatz_csrftoken": "different-bootstrap-csrf",
        },
        username="alice",
        session_expires_at=status_at().session_expires_at,
    )
    switched_native = switched.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    assert switched_native.predecessor_principal_key is None

    failed_backend = FakeSecretBackend()
    failed = VaultOwner(
        client,
        secret_backend=failed_backend,
        clock=FakeClock(),
        jitter=lambda low, high: (low + high) / 2,
    )
    failed_legacy = failed.login("alice", bytearray(b"secret"))
    durable_legacy_blob = failed_backend.raw
    failed_backend.fail_writes = True
    with pytest.raises(AuthRequired):
        failed.login(
            "alice",
            bytearray(b"secret"),
            installation_id=INSTALLATION_ID,
            client_version="0.17.0",
        )
    assert failed_backend.raw == durable_legacy_blob
    failed_backend.fail_writes = False
    recovered = VaultOwner(
        client,
        secret_backend=failed_backend,
        clock=FakeClock(),
        jitter=lambda low, high: (low + high) / 2,
    )
    assert recovered.refresh().principal_key == failed_legacy.principal_key


def test_stale_native_validation_cannot_replace_newer_login_record():
    owner, backend, client, clock = blocking_native_owner_factory()
    owner.login(
        "alice", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    outcomes: list[RuntimeSnapshot] = []
    validation = threading.Thread(target=lambda: outcomes.append(owner.validate_now()))
    validation.start()
    assert client.validation_started.wait(timeout=2)

    bob = owner.login(
        "bob", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    client.release_validation.set()
    validation.join(timeout=2)

    assert outcomes == [bob]
    assert owner.snapshot() == bob
    assert json.loads(backend.raw)["account_id"] == BOB_ACCOUNT_ID
    restarted = VaultOwner(client, secret_backend=backend, clock=clock, jitter=lambda *_: 1.0)
    assert restarted.refresh().account_id == BOB_ACCOUNT_ID


def test_stale_native_validation_cannot_restore_credentials_after_logout():
    owner, backend, client, _ = blocking_native_owner_factory()
    owner.login(
        "alice", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    outcomes: list[RuntimeSnapshot] = []
    validation = threading.Thread(target=lambda: outcomes.append(owner.validate_now()))
    validation.start()
    assert client.validation_started.wait(timeout=2)

    signed_out = owner.logout()
    client.release_validation.set()
    validation.join(timeout=2)

    assert outcomes == [signed_out]
    assert json.loads(backend.raw) == {
        "kind": "signed_out",
        "reason": "signed_out",
        "version": 3,
    }
    restarted = VaultOwner(client, secret_backend=backend, clock=FakeClock(), jitter=lambda *_: 1.0)
    assert restarted.refresh().state is AuthState.SIGNED_OUT


def test_stale_explicit_revoke_cannot_tombstone_newer_identity():
    owner, backend, client, _ = blocking_native_owner_factory()
    alice = owner.login(
        "alice", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    client.native_status_error = ExplicitSessionRevocation(
        code="session_revoked",
        account_id=alice.account_id,
        session_id=alice.session_id,
        revoked_at="2026-08-24T12:00:00+00:00",
    )
    outcomes: list[RuntimeSnapshot] = []
    validation = threading.Thread(target=lambda: outcomes.append(owner.validate_now()))
    validation.start()
    assert client.validation_started.wait(timeout=2)

    bob = owner.login(
        "bob", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    client.release_validation.set()
    validation.join(timeout=2)

    assert outcomes == [bob]
    assert owner.snapshot() == bob
    assert json.loads(backend.raw)["account_id"] == BOB_ACCOUNT_ID


@pytest.mark.parametrize("explicit_revoke", [False, True])
def test_newer_login_commit_excludes_alice_validation_after_bob_persists(explicit_revoke):
    clock = FakeClock()
    backend = BlockingBobWriteBackend()
    client = BlockingNativeAuthClient()
    owner = VaultOwner(client, secret_backend=backend, clock=clock, jitter=lambda *_: 1.0)
    alice = owner.login(
        "alice", bytearray(b"secret"), installation_id=INSTALLATION_ID, client_version="0.17.0"
    )
    if explicit_revoke:
        client.native_status_error = ExplicitSessionRevocation(
            code="session_revoked",
            account_id=alice.account_id,
            session_id=alice.session_id,
            revoked_at="2026-08-24T12:00:00+00:00",
        )
    validation_results: list[RuntimeSnapshot] = []
    validation = threading.Thread(target=lambda: validation_results.append(owner.validate_now()))
    validation.start()
    assert client.validation_started.wait(timeout=2)
    login_results: list[RuntimeSnapshot] = []
    failures: list[BaseException] = []

    def login_bob() -> None:
        try:
            login_results.append(
                owner.login(
                    "bob",
                    bytearray(b"secret"),
                    installation_id=INSTALLATION_ID,
                    client_version="0.17.0",
                )
            )
        except BaseException as error:
            failures.append(error)

    bob_login = threading.Thread(target=login_bob)
    bob_login.start()
    assert backend.bob_persisted.wait(timeout=2)
    client.release_validation.set()
    assert not backend.alice_stale_persisted.wait(timeout=0.1)
    backend.release_bob_persist.set()
    bob_login.join(timeout=2)
    validation.join(timeout=2)

    assert failures == []
    assert len(login_results) == 1
    assert validation_results == login_results
    assert owner.snapshot() == login_results[0]
    assert json.loads(backend.raw)["account_id"] == BOB_ACCOUNT_ID
    restarted = VaultOwner(client, secret_backend=backend, clock=clock, jitter=lambda *_: 1.0)
    assert restarted.refresh().account_id == BOB_ACCOUNT_ID


def test_newer_native_login_commit_excludes_legacy_upgrade_after_bob_persists():
    clock = FakeClock()
    backend = BlockingBobWriteBackend(raw=legacy_v1_blob())
    client = BlockingLegacyUpgradeClient()
    owner = VaultOwner(client, secret_backend=backend, clock=clock, jitter=lambda *_: 1.0)
    owner.refresh()
    validation_results: list[RuntimeSnapshot] = []
    validation = threading.Thread(target=lambda: validation_results.append(owner.validate_now()))
    validation.start()
    assert client.legacy_validation_started.wait(timeout=2)
    login_results: list[RuntimeSnapshot] = []
    failures: list[BaseException] = []

    def login_bob() -> None:
        try:
            login_results.append(
                owner.login(
                    "bob",
                    bytearray(b"secret"),
                    installation_id=INSTALLATION_ID,
                    client_version="0.17.0",
                )
            )
        except BaseException as error:
            failures.append(error)

    bob_login = threading.Thread(target=login_bob)
    bob_login.start()
    assert backend.bob_persisted.wait(timeout=2)
    client.release_legacy_validation.set()
    assert not backend.alice_stale_persisted.wait(timeout=0.1)
    backend.release_bob_persist.set()
    bob_login.join(timeout=2)
    validation.join(timeout=2)

    assert failures == []
    assert validation_results == login_results
    assert owner.snapshot() == login_results[0]
    assert json.loads(backend.raw)["account_id"] == BOB_ACCOUNT_ID


def test_matching_explicit_revoke_writes_secret_free_tombstone_once():
    owner, backend, client, _ = native_owner_factory()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    client.native_status_error = ExplicitSessionRevocation(
        code="session_revoked",
        account_id=active.account_id,
        session_id=active.session_id,
        revoked_at="2026-08-24T12:00:00+00:00",
    )

    revoked = owner.validate_now()

    assert (revoked.state, revoked.reason, revoked.epoch) == (
        AuthState.LOCKED,
        "session_revoked",
        active.epoch + 1,
    )
    decoded = json.loads(backend.raw)
    assert decoded["kind"] == "revoked"
    assert "session-token-sentinel" not in backend.raw
    assert owner.validate_now() == revoked
    assert backend.write_count == 2


def test_explicit_revoke_still_locks_and_removes_credential_when_tombstone_write_fails():
    owner, backend, client, _clock = native_owner_factory()
    owner.enable_desktop_local_continuity()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    backend.fail_writes = True
    client.native_status_error = ExplicitSessionRevocation(
        code="session_revoked",
        account_id=active.account_id,
        session_id=active.session_id,
        revoked_at="2026-08-24T12:00:00+00:00",
    )

    revoked = owner.validate_now()

    assert revoked.state is AuthState.LOCKED
    assert revoked.reason == "session_revoked"
    assert revoked.cloud_state is None
    assert backend.raw is None
    with pytest.raises(AuthRequired, match="session_revoked"):
        owner.connect_consumer(
            allow_local_continuity=True
        ).require_authorized("desktop.after-revoke", expected=active.scope)


@pytest.mark.parametrize(
    "reason", ["account_disabled", "account_revoked", "session_revoked"]
)
def test_matching_explicit_revoke_reaches_attached_consumer_without_weakening_owner_identity(
    reason,
):
    owner, _, client, _ = native_owner_factory()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    consumer = owner.connect_consumer()
    client.native_status_error = ExplicitSessionRevocation(
        code=reason,
        account_id=active.account_id,
        session_id=active.session_id,
        revoked_at="2026-08-24T12:00:00+00:00",
    )

    revoked = owner.validate_now()

    assert revoked.runtime_instance_id == active.runtime_instance_id
    assert revoked.epoch == active.epoch + 1
    assert consumer.snapshot() == revoked
    with pytest.raises(AuthRequired, match=reason):
        consumer.require_authorized("tool.after-revoke", now=1.0)

    replacement = RuntimeSnapshot.new_authenticated("alice", now=2.0, ttl=60.0)
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        consumer.publish(replacement)
    assert consumer.snapshot().reason == "runtime_unavailable"


def legacy_v1_blob() -> str:
    return json.dumps(
        {
            "version": 1,
            "cookies": {
                "__Host-ansatz_sessionid": "session-1",
                "__Host-ansatz_csrftoken": "csrf-1",
            },
            "username": "alice",
            "session_expires_at": "2026-08-24T12:02:00+00:00",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_legacy_cookie_restores_offline_then_stays_legacy_online():
    raw = legacy_v1_blob()
    backend = FakeSecretBackend(raw=raw)
    client = FakeAuthClient()
    owner = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )

    restored = owner.refresh()

    assert restored.legacy is True
    assert restored.principal_key == "legacy:" + hashlib.sha256(raw.encode()).hexdigest()
    validated = owner.validate_now()
    assert validated.legacy is True
    assert validated.validation_state is ValidationState.ONLINE
    assert validated.principal_key == restored.principal_key
    assert client.native_issue_calls == 0
    migrated = json.loads(backend.raw)
    assert migrated["version"] == 2
    assert "cookies" in migrated


def test_background_legacy_validation_never_writes_a_native_record():
    class NativeWriteDetector(FakeSecretBackend):
        def write(self, raw: str) -> None:
            payload = json.loads(raw)
            assert payload.get("kind") != "native", (
                "background validation must not persist a native record"
            )
            super().write(raw)

    raw = legacy_v1_blob()
    backend = NativeWriteDetector(raw=raw)
    client = FakeAuthClient()
    owner = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 1.0,
    )
    restored = owner.refresh()
    migrated_raw = backend.raw

    validated = owner.validate_now()

    assert validated.scope == restored.scope
    assert validated.legacy is True
    assert validated.validation_state is ValidationState.ONLINE
    assert migrated_raw is not None
    assert migrated_raw != raw
    assert json.loads(migrated_raw)["version"] == 2
    assert backend.raw == migrated_raw


class RecordingHardener:
    def __init__(self, events: list[str] | None = None, *, fail: bool = False) -> None:
        self.events = events if events is not None else []
        self.fail = fail

    def apply_required(self) -> None:
        self.events.append("harden")
        if self.fail:
            raise AuthRequired("runtime_unavailable")


def vault_owner_factory(**backend_options):
    clock = FakeClock()
    backend = FakeSecretBackend(**backend_options)
    client = FakeAuthClient()
    owner = VaultOwner(
        client,
        secret_backend=backend,
        clock=clock,
        jitter=lambda _low, _high: 59.0,
    )
    return owner, backend, client, clock


def memory_owner_factory(*, hardener=None):
    clock = FakeClock()
    backend = FakeSecretBackend()
    client = FakeAuthClient()
    owner = MemoryOwner(
        client,
        hardener=hardener or RecordingHardener(client.events),
        secret_backend=backend,
        clock=clock,
        jitter=lambda _low, _high: 59.0,
    )
    return owner, backend, client, clock


@pytest.mark.parametrize("owner_factory", [vault_owner_factory, memory_owner_factory])
def test_owner_login_status_logout_and_rotation_are_identical(owner_factory):
    owner, secret_backend, _auth_client, clock = owner_factory()

    snapshot = owner.login("alice", bytearray(b"secret"))
    assert snapshot.state is AuthState.AUTHENTICATED
    assert secret_backend.read_count <= 1
    assert owner.next_refresh_at == 159.0

    clock.now = 130.0
    rotated = owner.refresh()
    assert rotated.runtime_instance_id == snapshot.runtime_instance_id
    assert rotated.epoch == snapshot.epoch

    signed_out = owner.logout()
    assert signed_out.state is AuthState.SIGNED_OUT
    assert signed_out.epoch == snapshot.epoch + 1
    assert json.loads(secret_backend.read()) == {
        "kind": "signed_out",
        "reason": "signed_out",
        "version": 3,
    }


@pytest.mark.parametrize("owner_factory", [vault_owner_factory, memory_owner_factory])
def test_owner_issues_trace_credentials_only_for_its_authenticated_cookie_owner(owner_factory):
    owner, _secret_backend, auth_client, _clock = owner_factory()
    params = {
        "installation_id": "11111111-1111-4111-8111-111111111111",
        "client_version": "0.17.0",
        "telemetry_schema_version": "1",
    }

    with pytest.raises(AuthRequired, match="signed_out"):
        owner.trace_token(**params)

    owner.login("alice", bytearray(b"secret"))
    credential = owner.trace_token(**params)
    assert credential.installation_id == params["installation_id"]
    assert auth_client.trace_token_calls == [params]

    owner.logout()
    with pytest.raises(AuthRequired, match="signed_out"):
        owner.trace_token(**params)


@pytest.mark.parametrize("owner_factory", [vault_owner_factory, memory_owner_factory])
def test_consumer_from_before_logout_cannot_revive_after_login(owner_factory):
    owner, _secret_backend, _auth_client, _clock = owner_factory()
    authenticated = owner.login("alice", bytearray(b"secret"))
    stale_consumer = owner.connect_consumer()

    signed_out = owner.logout()
    reauthenticated = owner.login("alice", bytearray(b"secret"))

    assert signed_out.runtime_instance_id != authenticated.runtime_instance_id
    assert reauthenticated.runtime_instance_id == signed_out.runtime_instance_id
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        stale_consumer.require_authorized("tool.next_boundary")


def test_memory_owner_rejects_authentication_when_required_hardening_fails():
    hardener = RecordingHardener(fail=True)
    owner, secret_backend, auth_client, _clock = memory_owner_factory(hardener=hardener)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        owner.login("alice", bytearray(b"secret"))

    assert auth_client.login_calls == 0
    assert secret_backend.write_count == 0


def test_memory_hardening_happens_before_cookie_acquisition():
    client = FakeAuthClient()
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(client.events),
        secret_backend=FakeSecretBackend(),
        clock=FakeClock(),
        jitter=lambda _low, _high: 59.0,
    )

    owner.login("alice", bytearray(b"secret"))

    assert client.events == ["harden", "login"]


def test_graphical_vault_failure_never_falls_back_to_memory_or_file(tmp_path):
    before = set(tmp_path.iterdir())
    owner, _secret_backend, _auth_client, _clock = vault_owner_factory(fail_writes=True)

    with pytest.raises(AuthRequired, match="vault_unavailable") as caught:
        owner.login("alice", bytearray(b"secret"))

    assert "backend details" not in repr(caught.value)
    assert set(tmp_path.iterdir()) == before


def test_graphical_vault_discards_an_incompatible_saved_session_and_signs_out():
    owner, secret_backend, auth_client, _clock = vault_owner_factory()
    secret_backend.raw = '{"legacy_session":"stale"}'

    snapshot = owner.refresh()

    assert snapshot.state is AuthState.SIGNED_OUT
    assert snapshot.reason is None
    assert secret_backend.raw is None
    assert secret_backend.delete_count == 1
    assert auth_client.status_calls == 0


def test_profiles_share_one_os_user_runtime_and_logout_revokes_both():
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    coder = owner.connect_consumer(profile="coder")
    writer = owner.connect_consumer(profile="writer")

    scope = owner.login("alice", bytearray(b"secret")).scope
    assert coder.require_authorized("profile.coder", expected=scope, now=101.0) == scope
    assert writer.require_authorized("profile.writer", expected=scope, now=101.0) == scope

    owner.logout()
    with pytest.raises(AuthRequired):
        coder.require_authorized("profile.coder", expected=scope, now=102.0)
    with pytest.raises(AuthRequired):
        writer.require_authorized("profile.writer", expected=scope, now=102.0)


def test_legacy_principal_key_is_stable_across_runtime_epoch_and_restart_but_isolates_credentials():
    owner, backend, client, _clock = vault_owner_factory()
    first = owner.login("alice", bytearray(b"secret"))

    assert first.principal_key is not None
    assert re.fullmatch(r"legacy:[0-9a-f]{64}", first.principal_key)
    assert "alice" not in first.principal_key
    assert "session-1" not in first.principal_key
    assert first.locked("server_unavailable", now=101.0).principal_key == first.principal_key

    restarted = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 58.0,
    ).refresh()
    assert restarted.runtime_instance_id != first.runtime_instance_id
    assert restarted.principal_key == first.principal_key

    other_client = FakeAuthClient()
    other_client.record = CookieRecord(
        cookies={
            "__Host-ansatz_sessionid": "session-2",
            "__Host-ansatz_csrftoken": "csrf-2",
        },
        username="alice",
        session_expires_at=status_at().session_expires_at,
    )
    other = VaultOwner(
        other_client,
        secret_backend=FakeSecretBackend(),
        clock=FakeClock(),
        jitter=lambda *_: 58.0,
    ).login("alice", bytearray(b"secret"))

    assert other.principal_key != first.principal_key


def _exact_v1_cookie_raw() -> str:
    return json.dumps(
        {
            "version": 1,
            "cookies": {
                "__Host-ansatz_sessionid": "session-1",
                "__Host-ansatz_csrftoken": "csrf-1",
            },
            "username": "alice",
            "session_expires_at": status_at().session_expires_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def test_exact_v1_credential_is_atomically_persisted_as_v2_once():
    raw = _exact_v1_cookie_raw()
    owner, backend, client, _clock = vault_owner_factory()
    backend.raw = raw

    restored = owner.refresh()
    migrated = json.loads(backend.raw or "null")
    expected_principal = "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    assert restored.state is AuthState.AUTHENTICATED
    assert restored.principal_key == expected_principal
    assert migrated == {
        "version": 2,
        "cookies": {
            "__Host-ansatz_sessionid": "session-1",
            "__Host-ansatz_csrftoken": "csrf-1",
        },
        "username": "alice",
        "session_expires_at": status_at().session_expires_at,
        "principal_key": expected_principal,
    }
    assert backend.write_count == 1

    restarted = VaultOwner(
        client,
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda *_: 58.0,
    ).refresh()
    assert restarted.principal_key == expected_principal
    assert backend.write_count == 1


def test_exact_v1_migration_write_failure_preserves_recoverable_v1_without_half_identity():
    raw = _exact_v1_cookie_raw()
    owner, backend, client, _clock = vault_owner_factory(fail_writes=True)
    backend.raw = raw

    with pytest.raises(AuthRequired, match="vault_unavailable") as caught:
        owner.refresh()

    assert backend.raw == raw
    assert backend.write_count == 1
    assert client.status_calls == 0
    assert owner.snapshot().state is AuthState.LOCKED
    assert owner.snapshot().principal_key is None
    assert "session-1" not in repr(caught.value)


def test_logout_locks_and_clears_secret_even_when_remote_logout_fails():
    owner, secret_backend, auth_client, _clock = vault_owner_factory()
    before = owner.login("alice", bytearray(b"secret"))
    auth_client.logout_error = AuthServiceError("server_unavailable")

    after = owner.logout()

    assert after.state is AuthState.SIGNED_OUT
    assert after.epoch == before.epoch + 1
    assert json.loads(secret_backend.read()) == {
        "kind": "signed_out",
        "reason": "signed_out",
        "version": 3,
    }


def test_native_refresh_failure_preserves_scope_without_extra_grace():
    owner, _secret_backend, auth_client, clock = native_owner_factory()
    authenticated = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    auth_client.native_status_error = AuthServiceError("server_unavailable")
    clock.now = owner.next_refresh_at

    refreshed = owner.validate_now()

    assert refreshed.state is AuthState.AUTHENTICATED
    assert refreshed.scope == authenticated.scope
    assert refreshed.validation_state is ValidationState.DEGRADED


def test_local_login_rate_limit_stops_repeated_invalid_credentials():
    owner, _secret_backend, auth_client, clock = vault_owner_factory()
    auth_client.login_error = AuthServiceError("invalid_credentials")

    for _ in range(LOGIN_ATTEMPT_LIMIT):
        with pytest.raises(AuthRequired, match="invalid_credentials"):
            owner.login("alice", bytearray(b"secret"))

    with pytest.raises(AuthRequired, match="rate_limited"):
        owner.login("alice", bytearray(b"secret"))
    assert auth_client.login_calls == LOGIN_ATTEMPT_LIMIT

    clock.now += 60.0
    with pytest.raises(AuthRequired, match="invalid_credentials"):
        owner.login("alice", bytearray(b"secret"))
    assert auth_client.login_calls == LOGIN_ATTEMPT_LIMIT + 1


def test_successful_login_clears_prior_local_failures():
    owner, _secret_backend, auth_client, _clock = vault_owner_factory()
    auth_client.login_error = AuthServiceError("invalid_credentials")
    for _ in range(LOGIN_ATTEMPT_LIMIT - 1):
        with pytest.raises(AuthRequired, match="invalid_credentials"):
            owner.login("alice", bytearray(b"secret"))

    auth_client.login_error = None
    owner.login("alice", bytearray(b"secret"))
    owner.logout()

    auth_client.login_error = AuthServiceError("invalid_credentials")
    with pytest.raises(AuthRequired, match="invalid_credentials"):
        owner.login("alice", bytearray(b"secret"))
    assert auth_client.login_calls == LOGIN_ATTEMPT_LIMIT + 1


def test_owner_exits_after_fifteen_minutes_without_authenticated_activity():
    owner, _secret_backend, _auth_client, clock = vault_owner_factory()
    consumer = owner.connect_consumer()
    scope = owner.login("alice", bytearray(b"secret")).scope
    clock.now = 101.0
    assert consumer.require_authorized("cli.start", expected=scope) == scope

    clock.now = 101.0 + OWNER_IDLE_SECONDS - 0.1
    assert owner.maintenance() is True

    clock.now = 101.0 + OWNER_IDLE_SECONDS
    assert owner.maintenance() is False
    assert owner.snapshot().state is AuthState.LOCKED
    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        consumer.require_authorized("tool", expected=scope)


def test_unauthenticated_consumer_does_not_prevent_owner_idle_exit():
    owner, _secret_backend, _auth_client, clock = memory_owner_factory()
    owner.connect_consumer()

    clock.now += OWNER_IDLE_SECONDS

    assert owner.maintenance() is False
    assert owner.snapshot().state is AuthState.LOCKED


class EntryPointOwnerDouble:
    def __init__(
        self,
        snapshot: RuntimeSnapshot,
        *,
        refresh_error: AuthRequired | None = None,
        login_error: AuthRequired | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._refresh_error = refresh_error
        self._login_error = login_error
        self.refresh_calls = 0
        self.login_calls: list[tuple[str, bytes]] = []

    def refresh(self) -> RuntimeSnapshot:
        self.refresh_calls += 1
        if self._refresh_error is not None:
            raise self._refresh_error
        return self._snapshot

    def snapshot(self) -> RuntimeSnapshot:
        return self._snapshot

    def login(self, username: str, password: bytearray) -> RuntimeSnapshot:
        self.login_calls.append((username, bytes(password)))
        if self._login_error is not None:
            raise self._login_error
        return self._snapshot


class AuthorizedEntryPointOwnerDouble(EntryPointOwnerDouble):
    def connect_consumer(self, *, profile=None):
        del profile
        snapshot = self.snapshot()

        class Consumer:
            def require_authorized(self, _boundary, *, expected):
                assert expected == snapshot.scope
                return expected

        return Consumer()

def test_account_status_recovers_stale_owner_once(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    recovered_snapshot = RuntimeSnapshot.signed_out(reason="signed_out")
    replacement = EntryPointOwnerDouble(recovered_snapshot)
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result is recovered_snapshot
    assert stale.refresh_calls == 1
    assert replacement.refresh_calls == 1
    assert starts == ["start"]


def test_account_status_recovers_owner_that_returns_unavailable_snapshot(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
    )
    recovered_snapshot = RuntimeSnapshot.signed_out()
    replacement = EntryPointOwnerDouble(recovered_snapshot)
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result is recovered_snapshot
    assert stale.refresh_calls == 1
    assert replacement.refresh_calls == 1
    assert starts == ["start"]


def test_account_status_recovery_never_overwrites_newer_owner(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    candidate = EntryPointOwnerDouble(RuntimeSnapshot.signed_out(reason="signed_out"))
    current_snapshot = RuntimeSnapshot.signed_out()
    current = EntryPointOwnerDouble(current_snapshot)
    install_entrypoint_owner(stale)

    def connect(**_kwargs):
        install_entrypoint_owner(current)
        return candidate

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        connect,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: pytest.fail(
            "a connected owner must prevent owner startup"
        ),
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result is current_snapshot
    assert stale.refresh_calls == 1
    assert current.refresh_calls == 1
    assert candidate.refresh_calls == 0


def test_concurrent_status_recovery_resolves_one_replacement(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement_snapshot = RuntimeSnapshot.signed_out()
    replacement = EntryPointOwnerDouble(replacement_snapshot)
    entered = threading.Event()
    release = threading.Event()
    connect_calls: list[str] = []
    start_calls: list[str] = []
    install_entrypoint_owner(stale)

    def connect(**_kwargs):
        connect_calls.append("connect")
        entered.set()
        assert release.wait(timeout=2)
        raise AuthRequired("runtime_unavailable")

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        connect,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: start_calls.append("start") or replacement,
    )
    results: list[RuntimeSnapshot] = []
    failures: list[BaseException] = []

    def status() -> None:
        try:
            results.append(account_status())
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=status) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        assert entered.wait(timeout=2)
        release.set()
        for thread in threads:
            thread.join(timeout=2)
            assert not thread.is_alive()
    finally:
        release.set()
        clear_entrypoint_owner()

    assert failures == []
    assert results == [replacement_snapshot, replacement_snapshot]
    assert connect_calls == ["connect"]
    assert start_calls == ["start"]


def test_account_status_stops_after_one_failed_owner_recovery(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result.state is AuthState.SIGNED_OUT
    assert result.reason == "runtime_unavailable"
    assert stale.refresh_calls == 1
    assert replacement.refresh_calls == 1
    assert starts == ["start"]


def test_account_status_preserves_non_runtime_failure_reason(monkeypatch):
    owner = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("server_unavailable"),
    )
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: pytest.fail("server failure must not reconnect"),
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result.reason == "server_unavailable"


def test_account_status_preserves_replacement_refresh_reason(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(),
        refresh_error=AuthRequired("rate_limited"),
    )
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: replacement,
    )
    try:
        result = account_status()
    finally:
        clear_entrypoint_owner()

    assert result.reason == "rate_limited"


def test_account_status_circuit_breaks_repeated_owner_start_failures(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )

    def fail_start(**_kwargs):
        starts.append("start")
        raise AuthRequired("runtime_unavailable")

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        fail_start,
    )
    try:
        first = account_status()
        second = account_status()
    finally:
        clear_entrypoint_owner()

    assert first.reason == "runtime_unavailable"
    assert second.reason == "runtime_unavailable"
    assert starts == ["start"]


def test_account_status_recovery_stays_inside_bridge_deadline(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement = EntryPointOwnerDouble(RuntimeSnapshot.signed_out())
    connect_timeouts: list[float] = []
    start_options: list[dict[str, object]] = []
    install_entrypoint_owner(stale)

    def connect(**kwargs):
        connect_timeouts.append(kwargs["timeout"])
        raise AuthRequired("runtime_unavailable")

    def start(**kwargs):
        start_options.append(kwargs)
        return replacement

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        connect,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        start,
    )
    try:
        account_status()
    finally:
        clear_entrypoint_owner()

    assert connect_timeouts == [2.0]
    assert start_options == [{"timeout": 4.0, "probe_first": False}]
    assert replacement.refresh_calls == 1


def test_account_status_recovery_revokes_the_stale_runtime_consumer(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement = EntryPointOwnerDouble(RuntimeSnapshot.signed_out())
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    install_entrypoint_owner(stale)
    install_runtime_consumer(
        RuntimeConsumer(
            authenticated,
            liveness_probe=lambda: True,
            clock=lambda: 101.0,
        )
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: replacement,
    )
    try:
        assert require_authorized("tool.before", expected=authenticated.scope)
        account_status()
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            require_authorized("tool.after", expected=authenticated.scope)
    finally:
        clear_entrypoint_owner()


def test_reconnecting_same_runtime_keeps_its_live_consumer(monkeypatch):
    shared_snapshot = RuntimeSnapshot.signed_out(reason="runtime_unavailable")
    stale = EntryPointOwnerDouble(
        shared_snapshot,
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    candidate = EntryPointOwnerDouble(shared_snapshot)
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    install_entrypoint_owner(stale)
    install_runtime_consumer(
        RuntimeConsumer(
            authenticated,
            liveness_probe=lambda: True,
            clock=lambda: 101.0,
        )
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: candidate,
    )
    try:
        account_status()
        assert require_authorized(
            "tool.same-runtime",
            expected=authenticated.scope,
        ) == authenticated.scope
    finally:
        clear_entrypoint_owner()


def test_account_login_recovers_stale_owner_before_submitting_password(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    replacement = EntryPointOwnerDouble(authenticated)
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    password = bytearray(b"password-sentinel")
    try:
        result = account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert result is authenticated
    assert stale.refresh_calls == 1
    assert stale.login_calls == []
    assert replacement.login_calls == [("alice", b"password-sentinel")]
    assert starts == ["start"]


def test_account_login_recovers_unavailable_snapshot_before_password(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        login_error=AuthRequired("runtime_unavailable"),
    )
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    replacement = EntryPointOwnerDouble(authenticated)
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    password = bytearray(b"password-sentinel")
    try:
        result = account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert result is authenticated
    assert stale.refresh_calls == 1
    assert stale.login_calls == []
    assert replacement.login_calls == [("alice", b"password-sentinel")]
    assert starts == ["start"]


@pytest.mark.parametrize(
    "reason",
    ["invalid_credentials", "rate_limited", "server_unavailable"],
)
def test_account_login_never_recovers_non_runtime_failures(monkeypatch, reason):
    owner = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason=reason),
        login_error=AuthRequired(reason),
    )
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda: pytest.fail("a non-runtime login failure must not reconnect"),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda: pytest.fail("a non-runtime login failure must not start an owner"),
    )
    password = bytearray(b"password-sentinel")
    try:
        with pytest.raises(AuthRequired, match=reason):
            account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert owner.login_calls == [("alice", b"password-sentinel")]


def test_account_login_stops_after_one_failed_owner_recovery(monkeypatch):
    stale = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    replacement = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        login_error=AuthRequired("runtime_unavailable"),
    )
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    password = bytearray(b"password-sentinel")
    try:
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert stale.refresh_calls == 1
    assert stale.login_calls == []
    assert replacement.login_calls == [("alice", b"password-sentinel")]
    assert starts == ["start"]


def test_account_login_uses_its_single_password_attempt_when_recovery_fails(
    monkeypatch,
):
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    owner = EntryPointOwnerDouble(
        authenticated,
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    password = bytearray(b"password-sentinel")
    try:
        result = account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert result is authenticated
    assert owner.login_calls == [("alice", b"password-sentinel")]


def test_account_login_never_replays_an_ambiguous_runtime_failure(monkeypatch):
    owner = EntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(),
        login_error=AuthRequired("runtime_unavailable"),
    )
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda: pytest.fail("an ambiguous login failure must not reconnect"),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda: pytest.fail("an ambiguous login failure must not start an owner"),
    )
    password = bytearray(b"password-sentinel")
    try:
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            account_login("alice", password)
    finally:
        password[:] = b"\0" * len(password)
        clear_entrypoint_owner()

    assert owner.refresh_calls == 1
    assert owner.login_calls == [("alice", b"password-sentinel")]


def test_account_logout_recovers_stale_owner_and_retries_once(monkeypatch):
    class LogoutOwnerDouble(EntryPointOwnerDouble):
        def __init__(self, snapshot, *, logout_error=None):
            super().__init__(snapshot)
            self.logout_error = logout_error
            self.logout_calls = 0

        def logout(self):
            self.logout_calls += 1
            if self.logout_error is not None:
                raise self.logout_error
            return self._snapshot

    stale = LogoutOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        logout_error=AuthRequired("runtime_unavailable"),
    )
    signed_out = RuntimeSnapshot.signed_out()
    replacement = LogoutOwnerDouble(signed_out)
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: replacement,
    )
    try:
        result = account_logout()
    finally:
        clear_entrypoint_owner()

    assert result is signed_out
    assert stale.logout_calls == 1
    assert replacement.logout_calls == 1


def test_account_logout_does_not_retry_non_runtime_failure(monkeypatch):
    class LogoutOwnerDouble(EntryPointOwnerDouble):
        def logout(self):
            raise AuthRequired("vault_unavailable")

    owner = LogoutOwnerDouble(RuntimeSnapshot.signed_out())
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: pytest.fail("non-runtime logout must not reconnect"),
    )
    try:
        with pytest.raises(AuthRequired, match="vault_unavailable"):
            account_logout()
    finally:
        clear_entrypoint_owner()


def test_account_trace_token_uses_the_installed_owner_without_persisting_credential():
    installation_id = "11111111-1111-4111-8111-111111111111"
    credential = TraceCredential(
        access_token="trace-token-sentinel-1234567890",
        expires_at="2099-08-23T14:15:00+00:00",
        expires_in=900,
        installation_id=installation_id,
    )

    class TraceOwnerDouble(EntryPointOwnerDouble):
        def __init__(self):
            super().__init__(
                RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
            )
            self.trace_calls = []

        def trace_token(self, **params):
            self.trace_calls.append(params)
            return credential

    owner = TraceOwnerDouble()
    install_entrypoint_owner(owner)
    try:
        result = account_trace_token(
            installation_id=installation_id,
            client_version="0.17.0",
            telemetry_schema_version="1",
        )
    finally:
        clear_entrypoint_owner()

    assert result is credential
    assert owner.trace_calls == [
        {
            "installation_id": installation_id,
            "client_version": "0.17.0",
            "telemetry_schema_version": "1",
        }
    ]


def test_remote_owner_uses_short_timeout_for_recovery_probe():
    snapshot = RuntimeSnapshot.signed_out()

    class Connection:
        def __init__(self):
            self.timeout = None
            self.sent = b""

        def settimeout(self, timeout):
            self.timeout = timeout

        def sendall(self, data):
            self.sent += data

        def recv(self, _size):
            return (
                json.dumps(
                    {
                        "version": 1,
                        "ok": True,
                        "snapshot": snapshot.public_dict(),
                    }
                ).encode("utf-8")
                + b"\n"
            )

        def close(self):
            return None

    connection = Connection()

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout == 2.0
            return connection

    owner = RemoteRuntimeOwner(Endpoint())

    assert owner.refresh(timeout=2.0) == snapshot
    assert connection.timeout is not None
    assert 0 < connection.timeout <= 2.0


def test_remote_owner_wipes_serialized_login_frame_after_send():
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)

    class Connection:
        def __init__(self):
            self.sent_reference = None

        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            self.sent_reference = data

        def recv(self, _size):
            return (
                json.dumps(
                    {
                        "version": 1,
                        "ok": True,
                        "snapshot": authenticated.public_dict(),
                    }
                ).encode("utf-8")
                + b"\n"
            )

        def close(self):
            return None

    connection = Connection()

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return connection

    owner = RemoteRuntimeOwner(Endpoint())
    password = bytearray(b"password-sentinel")
    try:
        assert owner.login("alice", password) == authenticated
    finally:
        password[:] = b"\0" * len(password)

    assert connection.sent_reference is not None
    assert bytes(connection.sent_reference).strip(b"\0") == b""


def test_remote_owner_trace_credential_protocol_is_exact_and_installation_bound():
    installation_id = "11111111-1111-4111-8111-111111111111"

    class Connection:
        def __init__(self):
            self.sent = b""

        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            self.sent = bytes(data)

        def recv(self, _size):
            return json.dumps(
                {
                    "version": 1,
                    "ok": True,
                    "credential": {
                        "access_token": "trace-token-sentinel-1234567890",
                        "expires_at": "2099-08-23T14:15:00+00:00",
                        "expires_in": 900,
                        "installation_id": installation_id,
                    },
                }
            ).encode("utf-8") + b"\n"

        def close(self):
            return None

    connection = Connection()

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return connection

    result = RemoteRuntimeOwner(Endpoint()).trace_token(
        installation_id=installation_id,
        client_version="0.17.0",
        telemetry_schema_version="1",
    )

    assert result.installation_id == installation_id
    assert json.loads(connection.sent) == {
        "version": 3,
        "operation": "trace_token",
        "installation_id": installation_id,
        "client_version": "0.17.0",
        "telemetry_schema_version": "1",
    }


def test_remote_owner_rejects_extra_trace_credential_fields_without_echoing_token():
    sentinel = "trace-token-sentinel-1234567890"

    class Connection:
        def settimeout(self, _timeout):
            return None

        def sendall(self, _data):
            return None

        def recv(self, _size):
            return json.dumps(
                {
                    "version": 1,
                    "ok": True,
                    "credential": {
                        "access_token": sentinel,
                        "expires_at": "2099-08-23T14:15:00+00:00",
                        "expires_in": 900,
                        "installation_id": "11111111-1111-4111-8111-111111111111",
                        "extra": True,
                    },
                }
            ).encode("utf-8") + b"\n"

        def close(self):
            return None

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return Connection()

    with pytest.raises(AuthRequired, match="runtime_unavailable") as caught:
        RemoteRuntimeOwner(Endpoint()).trace_token(
            installation_id="11111111-1111-4111-8111-111111111111",
            client_version="0.17.0",
            telemetry_schema_version="1",
        )

    assert sentinel not in repr(caught.value)


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_stalled_same_user_client_cannot_block_other_owner_requests():
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    with tempfile.TemporaryDirectory(prefix="ha-broker-stall-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="fedcba9876543210fedcba9876543210",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        stalled = endpoint.connect_current(timeout=1.0)
        try:
            remote = RemoteRuntimeOwner(endpoint)
            assert remote.refresh(timeout=1.0).state is AuthState.SIGNED_OUT
        finally:
            stalled.close()
            broker.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_owner_serializes_password_submissions_across_unix_clients():
    class BlockingInvalidClient(FakeAuthClient):
        def __init__(self):
            super().__init__()
            self._active = 0
            self._active_lock = threading.Lock()
            self.login_calls = 0
            self.first_entered = threading.Event()
            self.concurrent_entered = threading.Event()
            self.release = threading.Event()

        def login(self, username, password):
            with self._active_lock:
                self.login_calls += 1
                self._active += 1
                if self._active > 1:
                    self.concurrent_entered.set()
            self.first_entered.set()
            try:
                assert self.release.wait(timeout=2)
                raise AuthServiceError("invalid_credentials")
            finally:
                with self._active_lock:
                    self._active -= 1

    client = BlockingInvalidClient()
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=FakeSecretBackend(),
        clock=FakeClock(),
        jitter=lambda _low, _high: 59.0,
    )
    failures: list[BaseException] = []
    threads: list[threading.Thread] = []
    with tempfile.TemporaryDirectory(prefix="ha-bls-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="0123456789abcdef0123456789abcdef",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)

        def login() -> None:
            password = bytearray(b"secret")
            try:
                RemoteRuntimeOwner(endpoint).login("alice", password)
            except BaseException as error:
                failures.append(error)
            finally:
                password[:] = b"\0" * len(password)

        try:
            threads = [threading.Thread(target=login) for _ in range(2)]
            threads[0].start()
            assert client.first_entered.wait(timeout=1)
            threads[1].start()
            overlapped = client.concurrent_entered.wait(timeout=0.25)
        finally:
            client.release.set()
            for thread in threads:
                thread.join(timeout=2)
            broker.close()

    assert overlapped is False
    assert client.login_calls == 2
    assert len(failures) == 2
    assert all(
        isinstance(error, AuthRequired) and error.reason == "invalid_credentials"
        for error in failures
    )


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_login_waits_for_a_concurrent_logout_instead_of_rejecting_password():
    class BlockingLogoutClient(FakeAuthClient):
        def __init__(self):
            super().__init__()
            self.logout_entered = threading.Event()
            self.release_logout = threading.Event()

        def logout(self, cookies):
            self.logout_calls += 1
            assert cookies == self.record.cookies
            self.logout_entered.set()
            assert self.release_logout.wait(timeout=2)

    client = BlockingLogoutClient()
    backend = FakeSecretBackend()
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda _low, _high: 59.0,
    )
    owner.login("alice", bytearray(b"secret"))
    login_done = threading.Event()
    logout_failures: list[BaseException] = []
    login_failures: list[BaseException] = []
    login_results: list[RuntimeSnapshot] = []

    with tempfile.TemporaryDirectory(prefix="ha-bll-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="00112233445566778899aabbccddeeff",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        installed = connect_runtime_owner(endpoint=endpoint, timeout=1.0)
        install_entrypoint_owner(installed)

        def logout() -> None:
            try:
                RemoteRuntimeOwner(endpoint).logout()
            except BaseException as error:
                logout_failures.append(error)

        def login() -> None:
            password = bytearray(b"secret")
            try:
                login_results.append(account_login("alice", password))
            except BaseException as error:
                login_failures.append(error)
            finally:
                password[:] = b"\0" * len(password)
                login_done.set()

        logout_thread = threading.Thread(target=logout)
        login_thread = threading.Thread(target=login)
        try:
            logout_thread.start()
            assert client.logout_entered.wait(timeout=1)
            login_thread.start()
            login_completed_before_release = login_done.wait(timeout=0.25)
        finally:
            client.release_logout.set()
            logout_thread.join(timeout=2)
            login_thread.join(timeout=2)
            clear_entrypoint_owner()
            broker.close()

    assert login_completed_before_release is False
    assert logout_failures == []
    assert login_failures == []
    assert len(login_results) == 1
    assert login_results[0].state is AuthState.AUTHENTICATED
    assert client.login_calls == 2
    assert backend.raw is not None


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_stale_status_cannot_delete_a_concurrent_successful_login():
    old_record = CookieRecord(
        cookies={
            "__Host-ansatz_sessionid": "old-session",
            "__Host-ansatz_csrftoken": "old-csrf",
        },
        username="alice",
        session_expires_at=status_at().session_expires_at,
    )
    new_record = CookieRecord(
        cookies={
            "__Host-ansatz_sessionid": "new-session",
            "__Host-ansatz_csrftoken": "new-csrf",
        },
        username="alice",
        session_expires_at=status_at().session_expires_at,
    )

    class ReauthenticationRaceClient(FakeAuthClient):
        def __init__(self):
            super().__init__()
            self.record = old_record
            self.next_record = old_record
            self.block_old_status = False
            self.old_status_entered = threading.Event()
            self.release_old_status = threading.Event()

        def login(self, username, password):
            assert username == "alice"
            assert password == bytearray(b"secret")
            return self.next_record

        def status(self, cookies):
            if self.block_old_status and cookies == old_record.cookies:
                self.old_status_entered.set()
                assert self.release_old_status.wait(timeout=2)
                raise SessionRejected()
            assert cookies in (old_record.cookies, new_record.cookies)
            return status_at()

    client = ReauthenticationRaceClient()
    backend = FakeSecretBackend()
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda _low, _high: 59.0,
    )
    owner.login("alice", bytearray(b"secret"))
    client.next_record = new_record
    login_done = threading.Event()
    status_failures: list[BaseException] = []
    login_failures: list[BaseException] = []
    login_results: list[RuntimeSnapshot] = []
    with tempfile.TemporaryDirectory(prefix="ha-brl-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="abcdef0123456789abcdef0123456789",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        installed = connect_runtime_owner(endpoint=endpoint, timeout=1.0)
        install_entrypoint_owner(installed)
        client.block_old_status = True

        def refresh() -> None:
            try:
                RemoteRuntimeOwner(endpoint).refresh(timeout=1.0)
            except BaseException as error:
                status_failures.append(error)

        def login() -> None:
            password = bytearray(b"secret")
            try:
                login_results.append(account_login("alice", password))
            except BaseException as error:
                login_failures.append(error)
            finally:
                password[:] = b"\0" * len(password)
                login_done.set()

        refresh_thread = threading.Thread(target=refresh)
        login_thread = threading.Thread(target=login)
        try:
            refresh_thread.start()
            assert client.old_status_entered.wait(timeout=1)
            login_thread.start()
            login_completed_before_release = login_done.wait(timeout=0.25)
        finally:
            client.release_old_status.set()
            refresh_thread.join(timeout=2)
            login_thread.join(timeout=2)
            clear_entrypoint_owner()
            broker.close()

    assert login_completed_before_release is True
    assert login_failures == []
    assert len(login_results) == 1
    assert login_results[0].state is AuthState.AUTHENTICATED
    assert status_failures == []
    assert owner.snapshot().state is AuthState.AUTHENTICATED
    assert backend.raw is not None and "new-session" in backend.raw


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_rejected_unix_peer_does_not_stop_the_owner(monkeypatch):
    import hermes_cli.client_auth.runtime as runtime_module

    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    original_validate = runtime_module._validate_peer_uid
    rejected_once = False

    def reject_one_server_peer(connection):
        nonlocal rejected_once
        if threading.current_thread().name == "hermes-auth-owner" and not rejected_once:
            rejected_once = True
            raise AuthRequired("runtime_unavailable")
        return original_validate(connection)

    with tempfile.TemporaryDirectory(prefix="ha-peer-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="11223344556677889900aabbccddeeff",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        monkeypatch.setattr(runtime_module, "_validate_peer_uid", reject_one_server_peer)
        rejected = endpoint.connect_current(timeout=1.0)
        try:
            assert (
                RemoteRuntimeOwner(endpoint).refresh(timeout=1.0).state
                is AuthState.SIGNED_OUT
            )
        finally:
            rejected.close()
            broker.close()

    assert rejected_once is True


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_owner_recovers_when_one_worker_thread_cannot_start(monkeypatch):
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    original_start = threading.Thread.start
    failed = False

    def fail_one_worker(thread):
        nonlocal failed
        if thread.name == "hermes-auth-owner-client" and not failed:
            failed = True
            raise RuntimeError("thread details must be redacted")
        return original_start(thread)

    with tempfile.TemporaryDirectory(prefix="ha-bwf-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="0011aabb2233ccdd4455eeff66778899",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        monkeypatch.setattr(threading.Thread, "start", fail_one_worker)
        rejected = endpoint.connect_current(timeout=1.0)
        try:
            try:
                rejected.sendall(b'{"operation":"status","version":1}\n')
            except OSError:
                pass
            assert (
                RemoteRuntimeOwner(endpoint).refresh(timeout=1.0).state
                is AuthState.SIGNED_OUT
            )
        finally:
            rejected.close()
            broker.close()

    assert failed is True


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_owner_rejects_clients_above_its_worker_limit(monkeypatch):
    monkeypatch.setattr(OwnerBroker, "_MAX_WORKERS", 1, raising=False)
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    with tempfile.TemporaryDirectory(prefix="ha-bwl-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="99887766554433221100ffeeddccbbaa",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        stalled = endpoint.connect_current(timeout=1.0)
        try:
            deadline = time.monotonic() + 1.0
            while len(broker._connections) < 1 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert len(broker._connections) == 1
            with pytest.raises(AuthRequired, match="runtime_unavailable"):
                RemoteRuntimeOwner(endpoint).refresh(timeout=0.5)
        finally:
            stalled.close()
            broker.close()


def test_runtime_frame_timeout_is_a_total_deadline():
    reader, writer = socket.socketpair()
    stop = threading.Event()

    def drip() -> None:
        while not stop.wait(0.04):
            try:
                writer.sendall(b"x")
            except OSError:
                return

    sender = threading.Thread(target=drip)
    sender.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            _read_runtime_frame(reader, timeout=0.15)
    finally:
        stop.set()
        reader.close()
        writer.close()
        sender.join(timeout=1)

    assert time.monotonic() - started < 0.5


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_account_status_recovers_after_real_broker_disappears(monkeypatch):
    first_owner, _first_backend, _first_client, _first_clock = memory_owner_factory()
    second_owner, _second_backend, _second_client, _second_clock = (
        memory_owner_factory()
    )
    replacement_brokers: list[OwnerBroker] = []
    with tempfile.TemporaryDirectory(prefix="ha-broker-recover-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="00112233445566778899aabbccddeeff",
        )
        first_broker = OwnerBroker.start(first_owner, endpoint=endpoint)
        stale = connect_runtime_owner(endpoint=endpoint, timeout=1.0)
        install_entrypoint_owner(stale)
        first_broker.close()

        def start_replacement(**_kwargs):
            broker = OwnerBroker.start(second_owner, endpoint=endpoint)
            replacement_brokers.append(broker)
            return connect_runtime_owner(endpoint=endpoint, timeout=1.0)

        monkeypatch.setattr(
            "hermes_cli.client_auth.runtime.runtime_endpoint",
            lambda: endpoint,
        )
        monkeypatch.setattr(
            "hermes_cli.client_auth.runtime.start_runtime_owner",
            start_replacement,
        )
        try:
            result = account_status()
        finally:
            clear_entrypoint_owner()
            for broker in replacement_brokers:
                broker.close()

    assert result.state is AuthState.SIGNED_OUT
    assert result.reason is None
    assert len(replacement_brokers) == 1


def test_interactive_entrypoint_logs_in_once_and_wipes_password(monkeypatch):
    owner, _secret_backend, auth_client, _clock = memory_owner_factory()
    install_entrypoint_owner(owner)
    monkeypatch.setattr("builtins.input", lambda _prompt: "alice")
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "secret")
    try:
        scope = authorize_entrypoint("cli.start", interactive=True)
    finally:
        clear_entrypoint_owner()

    assert scope == owner.snapshot().scope
    assert auth_client.login_calls == 1
    assert auth_client.password_refs == [bytearray(b"\0" * 6)]


def test_noninteractive_entrypoint_never_prompts(monkeypatch):
    owner, _secret_backend, auth_client, _clock = memory_owner_factory()
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("noninteractive auth must not prompt"),
    )
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _prompt: pytest.fail("noninteractive auth must not prompt"),
    )
    try:
        with pytest.raises(AuthRequired, match="signed_out"):
            authorize_entrypoint("gateway.start", interactive=False)
    finally:
        clear_entrypoint_owner()

    assert auth_client.login_calls == 0


def test_interactive_entrypoint_recovers_dead_installed_owner(monkeypatch):
    stale = AuthorizedEntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    replacement = AuthorizedEntryPointOwnerDouble(authenticated)
    starts: list[str] = []
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: starts.append("start") or replacement,
    )
    try:
        scope = authorize_entrypoint("cli.start", interactive=True)
    finally:
        clear_entrypoint_owner()

    assert scope == authenticated.scope
    assert starts == ["start"]


def test_noninteractive_entrypoint_reconnects_but_never_starts_owner(monkeypatch):
    stale = AuthorizedEntryPointOwnerDouble(
        RuntimeSnapshot.signed_out(reason="runtime_unavailable"),
        refresh_error=AuthRequired("runtime_unavailable"),
    )
    authenticated = RuntimeSnapshot.new_authenticated("alice", now=100.0, ttl=60.0)
    replacement = AuthorizedEntryPointOwnerDouble(authenticated)
    install_entrypoint_owner(stale)
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: replacement,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        lambda **_kwargs: pytest.fail("a background entrypoint must not start owner"),
    )
    try:
        scope = authorize_entrypoint("gateway.start", interactive=False)
    finally:
        clear_entrypoint_owner()

    assert scope == authenticated.scope


def test_wait_until_authorized_attempts_automatic_owner_start_once(monkeypatch):
    stop = threading.Event()
    states: list[RuntimeSnapshot] = []
    starts: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )

    def fail_start(**_kwargs):
        starts.append("start")
        raise AuthRequired("runtime_unavailable")

    def on_state(snapshot):
        states.append(snapshot)
        if len(states) == 3:
            stop.set()

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.start_runtime_owner",
        fail_start,
    )

    result = wait_until_authorized(
        "backend.start",
        stop_event=stop,
        on_state=on_state,
        poll_seconds=0,
        start_owner_if_missing=True,
    )

    assert result is LockedWaitingResult.OWNER_STOPPED
    assert len(states) == 3
    assert starts == ["start"]


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_unix_broker_shares_login_authorization_and_revocation():
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    with tempfile.TemporaryDirectory(prefix="ha-broker-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="0123456789abcdef0123456789abcdef",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        try:
            remote = connect_runtime_owner(endpoint=endpoint)
            authenticated = remote.login("alice", bytearray(b"secret"))
            consumer = remote.connect_consumer()

            assert consumer.require_authorized(
                "child.start",
                expected=authenticated.scope,
            ) == authenticated.scope

            owner.logout()
            with pytest.raises(AuthRequired, match="signed_out"):
                consumer.require_authorized(
                    "child.next_boundary",
                    expected=authenticated.scope,
                )

            renewed = owner.login("alice", bytearray(b"secret"))
            assert renewed.scope != authenticated.scope
            with pytest.raises(AuthScopeChanged, match="runtime_unavailable"):
                consumer.require_authorized(
                    "child.stale_scope",
                    expected=authenticated.scope,
                )
        finally:
            broker.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_unix_broker_requires_owner_enable_and_consumer_flag_for_offline_local_access():
    owner, _backend, client, _clock = native_owner_factory()
    active = owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    with tempfile.TemporaryDirectory(prefix="ha-local-continuity-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="abcdef0123456789abcdef0123456789",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        try:
            remote = connect_runtime_owner(endpoint=endpoint)
            remote.enable_desktop_local_continuity()
            local = remote.connect_consumer(allow_local_continuity=True)
            strict = remote.connect_consumer(allow_local_continuity=False)
            client.native_status_error = AuthServiceError("server_unavailable")
            degraded = owner.validate_now()

            assert degraded.cloud_state is CloudState.UNREACHABLE
            assert local.require_authorized(
                "desktop.local.remote",
                expected=active.scope,
            ) == active.scope
            with pytest.raises(AuthRequired, match="server_unavailable"):
                strict.require_authorized(
                    "cli.remote",
                    expected=active.scope,
                )
        finally:
            broker.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_connect_rejects_owner_already_in_runtime_unavailable_transition():
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    owner.close()
    with tempfile.TemporaryDirectory(prefix="ha-broker-closing-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="ffeeddccbbaa99887766554433221100",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        try:
            with pytest.raises(AuthRequired, match="runtime_unavailable"):
                connect_runtime_owner(endpoint=endpoint, timeout=1.0)
        finally:
            broker.close()


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_entrypoint_connects_live_broker_when_no_owner_is_in_process(
    monkeypatch,
):
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    authenticated = owner.login("alice", bytearray(b"secret"))
    with tempfile.TemporaryDirectory(prefix="ha-broker-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="0123456789abcdef0123456789abcdef",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        monkeypatch.setattr(
            "hermes_cli.client_auth.runtime.runtime_endpoint",
            lambda: endpoint,
        )
        clear_entrypoint_owner()
        try:
            assert authorize_entrypoint(
                "child.start",
                interactive=False,
            ) == authenticated.scope
        finally:
            clear_entrypoint_owner()
            broker.close()


def test_entrypoint_service_failure_never_prompts(monkeypatch):
    owner, _secret_backend, auth_client, _clock = vault_owner_factory()
    owner.login("alice", bytearray(b"secret"))
    auth_client.status_error = AuthServiceError("server_unavailable")
    install_entrypoint_owner(owner)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("service failures must not prompt"),
    )
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _prompt: pytest.fail("service failures must not prompt"),
    )
    try:
        with pytest.raises(AuthRequired, match="server_unavailable"):
            authorize_entrypoint("cli.start", interactive=True)
    finally:
        clear_entrypoint_owner()


def test_owner_starter_detaches_without_forwarding_secret_environment(monkeypatch):
    captured = {}
    connect_timeouts: list[float] = []
    remote = object()
    attempts = iter([AuthRequired("runtime_unavailable"), remote])

    def connect(**kwargs):
        connect_timeouts.append(kwargs["timeout"])
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    class Process:
        pass

    def popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return Process()

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-forward")
    monkeypatch.setenv("HERMES_ACCOUNT_TOKEN", "must-not-forward")
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        connect,
    )
    monkeypatch.setattr("subprocess.Popen", popen)

    assert start_runtime_owner(timeout=1.0) is remote
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "hermes_cli.client_auth.runtime",
        "owner",
    ]
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert captured["close_fds"] is True
    assert "OPENAI_API_KEY" not in captured["env"]
    assert "HERMES_ACCOUNT_TOKEN" not in captured["env"]
    assert len(connect_timeouts) == 2
    assert all(0 < timeout <= 1.0 for timeout in connect_timeouts)


def test_live_owner_is_reused_before_new_mode_election():
    existing = object()
    context = OwnerElectionContext(
        ssh_connection=True,
        containerized=True,
        graphical_session=False,
        platform="linux",
    )

    resolved = resolve_owner(
        context,
        live_owner=lambda: existing,
        vault_factory=lambda: pytest.fail("must not elect vault"),
        memory_factory=lambda: pytest.fail("must not elect memory"),
    )

    assert resolved is existing


@pytest.mark.parametrize(
    ("context", "expected"),
    [
        (OwnerElectionContext(False, False, True, "darwin"), "vault"),
        (OwnerElectionContext(False, False, True, "win32"), "vault"),
        (OwnerElectionContext(False, False, True, "linux"), "vault"),
        (OwnerElectionContext(True, False, False, "linux"), "memory"),
        (OwnerElectionContext(False, True, False, "linux"), "memory"),
        (OwnerElectionContext(False, False, False, "linux"), "memory"),
    ],
)
def test_owner_mode_election_is_fixed_by_runtime_context(context, expected):
    selected = resolve_owner(
        context,
        live_owner=lambda: None,
        vault_factory=lambda: "vault",
        memory_factory=lambda: "memory",
    )

    assert selected == expected


def test_detached_owner_preserves_only_product_auth_environment(monkeypatch):
    from hermes_cli.client_auth.runtime import _owner_process_environment

    monkeypatch.setenv("HERMES_HOME", "/Users/a/.ansatz-voice-trace-client")
    monkeypatch.setenv(
        "HERMES_AUTH_RUNTIME_NAMESPACE",
        "ansatz-voice-trace-client-auth-v1",
    )
    monkeypatch.setenv(
        "HERMES_AUTH_KEYRING_SERVICE",
        "cn.c2sml.ansatz.voice-trace-client.remote-auth",
    )
    monkeypatch.setenv(
        "HERMES_AUTH_LEGACY_KEYRING_SERVICE",
        "cn.c2sml.hermes.remote-auth",
    )
    monkeypatch.setenv("PROVIDER_API_KEY", "must-not-cross-owner-boundary")

    child_env = _owner_process_environment()

    assert child_env["HERMES_HOME"] == "/Users/a/.ansatz-voice-trace-client"
    assert (
        child_env["HERMES_AUTH_RUNTIME_NAMESPACE"]
        == "ansatz-voice-trace-client-auth-v1"
    )
    assert (
        child_env["HERMES_AUTH_KEYRING_SERVICE"]
        == "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    )
    assert (
        child_env["HERMES_AUTH_LEGACY_KEYRING_SERVICE"]
        == "cn.c2sml.hermes.remote-auth"
    )
    assert "PROVIDER_API_KEY" not in child_env


def test_keyring_backend_migrates_legacy_record_before_deleting_it(monkeypatch):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    account = "django-session"
    store = {(legacy_service, account): "cookie-record-sentinel"}
    events: list[tuple[str, str]] = []

    def get_password(service, requested_account):
        assert requested_account == account
        events.append(("get", service))
        return store.get((service, requested_account))

    def set_password(service, requested_account, raw):
        assert requested_account == account
        events.append(("set", service))
        store[(service, requested_account)] = raw

    def delete_password(service, requested_account):
        assert requested_account == account
        events.append(("delete", service))
        store.pop((service, requested_account), None)

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", set_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    backend = _KeyringSecretBackend(
        service=new_service,
        legacy_service=legacy_service,
    )

    assert backend.read() == "cookie-record-sentinel"
    assert events == [
        ("get", new_service),
        ("get", legacy_service),
        ("set", new_service),
        ("get", new_service),
        ("delete", legacy_service),
    ]
    assert store == {(new_service, account): "cookie-record-sentinel"}


def test_keyring_backend_keeps_legacy_record_when_migration_write_fails(monkeypatch):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    account = "django-session"
    store = {(legacy_service, account): "cookie-record-sentinel"}
    events: list[tuple[str, str]] = []

    def get_password(service, requested_account):
        events.append(("get", service))
        return store.get((service, requested_account))

    def fail_write(service, _requested_account, _raw):
        events.append(("set", service))
        raise RuntimeError("secure store unavailable")

    def delete_password(service, _requested_account):
        events.append(("delete", service))
        store.pop((service, account), None)

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", fail_write)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    backend = _KeyringSecretBackend(
        service=new_service,
        legacy_service=legacy_service,
    )

    with pytest.raises(RuntimeError, match="secure store unavailable"):
        backend.read()

    assert store == {(legacy_service, account): "cookie-record-sentinel"}
    assert events == [
        ("get", new_service),
        ("get", legacy_service),
        ("set", new_service),
    ]


def test_keyring_backend_removes_unverified_new_record_and_keeps_legacy(
    monkeypatch,
):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    account = "django-session"
    store = {(legacy_service, account): "cookie-record-sentinel"}
    events: list[tuple[str, str]] = []

    def get_password(service, requested_account):
        events.append(("get", service))
        return store.get((service, requested_account))

    def write_corrupt_record(service, requested_account, _raw):
        events.append(("set", service))
        store[(service, requested_account)] = "different-record"

    def delete_password(service, requested_account):
        events.append(("delete", service))
        store.pop((service, requested_account), None)

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "set_password", write_corrupt_record)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    backend = _KeyringSecretBackend(
        service=new_service,
        legacy_service=legacy_service,
    )

    with pytest.raises(RuntimeError, match="secure credential migration failed"):
        backend.read()

    assert events == [
        ("get", new_service),
        ("get", legacy_service),
        ("set", new_service),
        ("get", new_service),
        ("delete", new_service),
    ]
    assert store == {(legacy_service, account): "cookie-record-sentinel"}


def test_keyring_backend_logout_deletes_new_and_configured_legacy_services(
    monkeypatch,
):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    deleted: list[tuple[str, str]] = []

    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda service, account: deleted.append((service, account)),
    )
    backend = _KeyringSecretBackend(
        service=new_service,
        legacy_service=legacy_service,
    )

    backend.delete()

    assert deleted == [
        (new_service, "django-session"),
        (legacy_service, "django-session"),
    ]


def test_keyring_backend_uses_product_services_from_environment(monkeypatch):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    writes: list[tuple[str, str, str]] = []
    monkeypatch.setenv("HERMES_AUTH_KEYRING_SERVICE", new_service)
    monkeypatch.setenv("HERMES_AUTH_LEGACY_KEYRING_SERVICE", legacy_service)
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, account, raw: writes.append((service, account, raw)),
    )

    _KeyringSecretBackend().write("cookie-record-sentinel")

    assert writes == [
        (new_service, "django-session", "cookie-record-sentinel"),
    ]


def test_keyring_backend_prefers_new_record_and_removes_stale_legacy(monkeypatch):
    import keyring

    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    new_service = "cn.c2sml.ansatz.voice-trace-client.remote-auth"
    legacy_service = "cn.c2sml.hermes.remote-auth"
    account = "django-session"
    store = {
        (new_service, account): "new-cookie-record",
        (legacy_service, account): "old-cookie-record",
    }
    events: list[tuple[str, str]] = []

    def get_password(service, requested_account):
        events.append(("get", service))
        return store.get((service, requested_account))

    def delete_password(service, requested_account):
        events.append(("delete", service))
        store.pop((service, requested_account), None)

    monkeypatch.setattr(keyring, "get_password", get_password)
    monkeypatch.setattr(keyring, "delete_password", delete_password)
    backend = _KeyringSecretBackend(
        service=new_service,
        legacy_service=legacy_service,
    )

    assert backend.read() == "new-cookie-record"
    assert events == [("get", new_service), ("delete", legacy_service)]
    assert store == {(new_service, account): "new-cookie-record"}


@pytest.mark.parametrize(
    ("service", "legacy_service"),
    [
        ("contains/slash", "cn.c2sml.hermes.remote-auth"),
        ("cn.c2sml.ansatz.voice-trace-client.remote-auth", "contains whitespace"),
        ("cn.c2sml.same", "cn.c2sml.same"),
    ],
)
def test_keyring_backend_rejects_invalid_environment_services(
    service,
    legacy_service,
    monkeypatch,
):
    from hermes_cli.client_auth.runtime import _KeyringSecretBackend

    monkeypatch.setenv("HERMES_AUTH_KEYRING_SERVICE", service)
    monkeypatch.setenv("HERMES_AUTH_LEGACY_KEYRING_SERVICE", legacy_service)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        _KeyringSecretBackend()


def _run_native_hardener_subprocess() -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from hermes_cli.client_auth.runtime import ProcessHardener; "
                "ProcessHardener().apply_required(); "
                "import resource; "
                "assert resource.getrlimit(resource.RLIMIT_CORE) == (0, 0)"
            ),
        ],
        check=False,
        close_fds=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.macos_only
def test_macos_memory_owner_hardening_succeeds_in_isolated_process():
    result = _run_native_hardener_subprocess()

    assert result.returncode == 0, result.stderr


@pytest.mark.linux_only
def test_linux_memory_owner_hardening_succeeds_in_isolated_process():
    result = _run_native_hardener_subprocess()

    assert result.returncode == 0, result.stderr


def _assert_unix_endpoint_security(tmp_path) -> None:
    endpoint = UnixEndpoint.for_directory(
        tmp_path / "runtime",
        random_name="0123456789abcdef0123456789abcdef",
    )
    owner_lock = endpoint.acquire_owner_lock()
    server = endpoint.bind_owner(owner_lock)
    client = endpoint.connect_current()
    accepted = server.accept()
    try:
        assert endpoint.root.stat().st_mode & 0o777 == 0o700
        assert endpoint.pointer_path.stat().st_mode & 0o777 == 0o600
        assert endpoint.socket_path.stat().st_mode & 0o777 == 0o600
        assert os.get_inheritable(owner_lock.fileno()) is False
        assert os.get_inheritable(server.fileno()) is False
        assert os.get_inheritable(client.fileno()) is False
        assert os.get_inheritable(accepted.fileno()) is False
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            endpoint.acquire_owner_lock()
    finally:
        accepted.close()
        client.close()
        server.close()
        owner_lock.close()
    assert not endpoint.socket_path.exists()
    assert not endpoint.pointer_path.exists()


@pytest.mark.macos_only
def test_macos_unix_runtime_endpoint_enforces_permissions_and_peer_uid():
    with tempfile.TemporaryDirectory(prefix="ha-runtime-") as temporary:
        _assert_unix_endpoint_security(Path(temporary))


@pytest.mark.macos_only
def test_macos_runtime_endpoint_is_namespaced_for_test_isolation(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime._darwin_user_temp_dir",
        lambda: tmp_path,
    )
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "test-file-a")
    first = runtime_endpoint()
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "test-file-b")
    second = runtime_endpoint()
    monkeypatch.delenv("HERMES_TEST_ISOLATION")
    production = runtime_endpoint()

    assert first.root != second.root
    assert first.root.name.startswith("ha-t")
    assert second.root.name.startswith("ha-t")
    assert production.root == tmp_path / "ha"


@pytest.mark.macos_only
def test_macos_runtime_endpoint_isolated_by_product_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime._darwin_user_temp_dir",
        lambda: tmp_path,
    )
    monkeypatch.setenv(
        "HERMES_AUTH_RUNTIME_NAMESPACE",
        "ansatz-voice-trace-client-auth-v1",
    )
    product = runtime_endpoint()
    monkeypatch.delenv("HERMES_AUTH_RUNTIME_NAMESPACE")
    legacy = runtime_endpoint()

    assert product.root != legacy.root
    assert product.root.name.startswith("ha-")
    assert "hermes" not in product.root.name
    assert legacy.root.name.startswith("ha")


@pytest.mark.parametrize(
    "namespace",
    [
        "contains/slash",
        "contains\\backslash",
        "contains whitespace",
        "非ascii",
        "a" * 65,
    ],
)
def test_runtime_endpoint_rejects_invalid_product_namespace(namespace, monkeypatch):
    monkeypatch.setenv("HERMES_AUTH_RUNTIME_NAMESPACE", namespace)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        runtime_endpoint()


@pytest.mark.linux_only
def test_linux_unix_runtime_endpoint_enforces_permissions_and_peer_uid():
    with tempfile.TemporaryDirectory(prefix="ha-runtime-") as temporary:
        _assert_unix_endpoint_security(Path(temporary))


def test_unix_endpoint_fails_closed_when_socket_path_is_too_long(tmp_path):
    endpoint = UnixEndpoint.for_directory(
        tmp_path / ("long" * 30),
        random_name="0123456789abcdef0123456789abcdef",
    )
    owner_lock = endpoint.acquire_owner_lock()
    try:
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            endpoint.bind_owner(owner_lock)
    finally:
        owner_lock.close()


def test_unix_pointer_rejects_parent_directory_escape(tmp_path):
    endpoint = UnixEndpoint.for_directory(
        tmp_path / "runtime",
        random_name="0123456789abcdef0123456789abcdef",
    )
    endpoint.pointer_path.write_text(
        '{"version":1,"socket":"../outside.sock"}',
        encoding="utf-8",
    )
    endpoint.pointer_path.chmod(0o600)

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
        endpoint.connect_current()


@pytest.mark.macos_only
def test_macos_runtime_endpoint_uses_darwin_private_temp_and_short_path():
    endpoint = runtime_endpoint()

    assert isinstance(endpoint, UnixEndpoint)
    assert endpoint.root.name == f"ha{_test_runtime_suffix()}"
    assert endpoint.root.stat().st_mode & 0o777 == 0o700
    assert len(os.fsencode(endpoint.socket_path)) < 104


@pytest.mark.linux_only
def test_linux_runtime_endpoint_is_filesystem_scoped_not_abstract():
    endpoint = runtime_endpoint()

    assert isinstance(endpoint, UnixEndpoint)
    assert endpoint.root.stat().st_mode & 0o777 == 0o700
    assert not str(endpoint.socket_path).startswith("\x00")


@pytest.mark.windows_only
def test_windows_runtime_endpoint_is_sid_scoped_first_instance_pipe():
    endpoint = runtime_endpoint()

    assert isinstance(endpoint, WindowsNamedPipeEndpoint)
    assert endpoint.first_instance is True
    assert endpoint.pipe_name.startswith(r"\\.\pipe\hermes-auth-")
    assert endpoint.owner_sid


@pytest.mark.windows_only
def test_windows_named_pipe_restricts_dacl_verifies_sid_and_inheritance():
    import win32api
    import win32con
    import win32security

    endpoint = runtime_endpoint()
    assert isinstance(endpoint, WindowsNamedPipeEndpoint)
    server = endpoint.bind_owner()
    accepted: list[object] = []
    failures: list[BaseException] = []

    def accept_current_user() -> None:
        try:
            accepted.append(server.accept())
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=accept_current_user)
    thread.start()
    client = endpoint.connect_current()
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert failures == []
    assert len(accepted) == 1
    server_connection = accepted[0]
    try:
        assert (
            win32api.GetHandleInformation(client.handle)
            & win32con.HANDLE_FLAG_INHERIT
        ) == 0
        assert (
            win32api.GetHandleInformation(server_connection.handle)
            & win32con.HANDLE_FLAG_INHERIT
        ) == 0
        descriptor = win32security.GetSecurityInfo(
            server_connection.handle,
            win32security.SE_KERNEL_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = descriptor.GetSecurityDescriptorDacl()
        granted_sids = {
            win32security.ConvertSidToStringSid(dacl.GetAce(index)[2])
            for index in range(dacl.GetAceCount())
        }
        system_sid = win32security.ConvertSidToStringSid(
            win32security.CreateWellKnownSid(
                win32security.WinLocalSystemSid,
                None,
            )
        )
        assert granted_sids == {endpoint.owner_sid, system_sid}
        with pytest.raises(AuthRequired, match="runtime_unavailable"):
            endpoint.bind_owner()
    finally:
        client.close()
        server_connection.close()
        server.close()


def test_native_validation_preserves_predecessor_principal_key_in_persisted_blob():
    from hermes_cli.client_auth.runtime import (
        NativeCredentialRecord,
        _decode_credential_blob,
        _encode_native_blob,
    )

    predecessor = "legacy:" + "a" * 64
    credential = NativeSessionCredential(
        account_id=ACCOUNT_ID,
        session_id=NATIVE_SESSION_ID,
        session_token="session-token-sentinel-1234567890",
        installation_id=INSTALLATION_ID,
        username="alice",
        issued_at="2026-08-24T12:00:00+00:00",
    )
    record = NativeCredentialRecord(credential, "2026-08-24T11:00:00+00:00", predecessor)
    backend = FakeSecretBackend(raw=_encode_native_blob(record))
    owner = MemoryOwner(
        FakeAuthClient(),
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda _low, high: high,
    )

    snapshot = owner.validate_now()

    assert snapshot.validation_state is ValidationState.ONLINE
    assert snapshot.predecessor_principal_key == predecessor
    persisted = _decode_credential_blob(backend.raw)
    assert isinstance(persisted, NativeCredentialRecord)
    assert persisted.predecessor_principal_key == predecessor
    assert persisted.last_validated_at == "2026-08-24T12:00:00+00:00"


def test_background_legacy_validation_stays_legacy_without_minting_native_session():
    from hermes_cli.client_auth.runtime import _decode_credential_blob, _encode_cookie_blob

    client = FakeAuthClient()
    legacy_cookie = CookieRecord(
        cookies=dict(client.record.cookies),
        username="alice",
        session_expires_at=status_at().session_expires_at,
        principal_key="legacy:" + "b" * 64,
    )
    backend = FakeSecretBackend(raw=_encode_cookie_blob(legacy_cookie))
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda low, _high: low,
    )

    snapshot = owner.validate_now()

    assert client.native_issue_calls == 0
    assert snapshot.state is AuthState.AUTHENTICATED
    assert snapshot.legacy is True
    assert snapshot.validation_state is ValidationState.ONLINE
    assert snapshot.account_id is None and snapshot.installation_id is None
    assert snapshot.principal_key == legacy_cookie.principal_key
    persisted = _decode_credential_blob(backend.raw)
    assert isinstance(persisted, CookieRecord)
    assert persisted.principal_key == legacy_cookie.principal_key


def test_validation_backoff_clamps_exponent_during_long_outage():
    client = FakeAuthClient()
    clock = FakeClock(100.0)
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=FakeSecretBackend(),
        clock=clock,
        jitter=lambda _low, high: high,
    )
    owner.login(
        "alice",
        bytearray(b"secret"),
        installation_id=INSTALLATION_ID,
        client_version="0.17.0",
    )
    client.native_status_error = AuthServiceError("server_unavailable")

    for _ in range(1100):
        owner.validate_now()

    snapshot = owner.snapshot()
    assert snapshot.validation_state is ValidationState.DEGRADED
    due = owner.next_refresh_at
    assert due is not None
    assert clock.now < due <= clock.now + 300.0


def test_legacy_cookie_maintenance_refreshes_on_schedule_without_busy_looping():
    client = FakeAuthClient()
    clock = FakeClock(100.0)
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=FakeSecretBackend(),
        clock=clock,
        jitter=lambda low, _high: low,
    )
    owner.login("alice", bytearray(b"secret"))
    assert client.status_calls == 1
    due = owner.next_refresh_at
    assert due is not None and due > clock.now

    for _ in range(4):
        clock.now += 0.5
        assert owner.maintenance() is True
    assert client.status_calls == 1

    clock.now = due
    assert owner.maintenance() is True
    assert client.status_calls == 2
    assert client.native_status_calls == 0
    assert client.native_issue_calls == 0
    rescheduled = owner.next_refresh_at
    assert rescheduled is not None and rescheduled > clock.now

    clock.now += 0.5
    assert owner.maintenance() is True
    assert client.status_calls == 2


LEGACY_SNAPSHOT_WIRE_KEYS = {
    "state",
    "username",
    "runtime_instance_id",
    "epoch",
    "valid_until",
    "session_expires_at",
    "reason",
}


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_owner_broker_versions_snapshot_shape_for_rolling_upgrade():
    owner, _secret_backend, _auth_client, _clock = memory_owner_factory()
    with tempfile.TemporaryDirectory(prefix="ha-proto-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="aabbccdd00112233445566778899eeff",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        try:

            def roundtrip(version: object) -> dict[str, object]:
                connection = endpoint.connect_current(timeout=1.0)
                try:
                    connection.sendall(
                        json.dumps({"version": version, "operation": "status"}).encode("utf-8")
                        + b"\n"
                    )
                    raw = _read_runtime_frame(connection, timeout=1.0)
                finally:
                    connection.close()
                return json.loads(raw)

            v1 = roundtrip(1)
            assert v1["version"] == 1
            assert v1["ok"] is True
            assert set(v1["snapshot"]) == LEGACY_SNAPSHOT_WIRE_KEYS

            v2 = roundtrip(2)
            assert v2["version"] == 2
            assert v2["ok"] is True
            assert LEGACY_SNAPSHOT_WIRE_KEYS < set(v2["snapshot"])
            assert "principal_key" in v2["snapshot"]
            assert "cloud_state" not in v2["snapshot"]

            v3 = roundtrip(3)
            assert v3["version"] == 3
            assert v3["ok"] is True
            assert v3["snapshot"]["cloud_state"] is None

            for invalid in (4, "1", None):
                rejected = roundtrip(invalid)
                assert rejected["ok"] is False
        finally:
            broker.close()


def test_remote_owner_falls_back_to_protocol_v1_for_an_old_owner():
    signed_out = RuntimeSnapshot.signed_out()
    legacy_wire = {
        key: signed_out.public_dict()[key] for key in LEGACY_SNAPSHOT_WIRE_KEYS
    }
    sent_frames: list[bytes] = []

    class OldOwnerConnection:
        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            sent_frames.append(bytes(data))

        def recv(self, _size):
            request = json.loads(sent_frames[-1])
            if request.get("version") != 1:
                return b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
            return (
                json.dumps({"version": 1, "ok": True, "snapshot": legacy_wire}).encode("utf-8")
                + b"\n"
            )

        def close(self):
            return None

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return OldOwnerConnection()

    owner = RemoteRuntimeOwner(Endpoint())

    first = owner.refresh(timeout=1.0)
    assert first.state is AuthState.SIGNED_OUT
    versions = [json.loads(frame)["version"] for frame in sent_frames]
    assert versions == [3, 2, 1], "client must negotiate down to an old v1 owner"

    owner.refresh(timeout=1.0)
    versions = [json.loads(frame)["version"] for frame in sent_frames]
    assert versions == [3, 2, 1, 1], "the downgrade must be sticky for the owner connection"


def test_remote_owner_falls_back_to_protocol_v2_without_misreading_cloud_state():
    signed_out = RuntimeSnapshot.signed_out()
    v2_wire = signed_out.public_dict()
    v2_wire.pop("cloud_state")
    sent_frames: list[bytes] = []

    class V2OwnerConnection:
        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            sent_frames.append(bytes(data))

        def recv(self, _size):
            request = json.loads(sent_frames[-1])
            if request.get("version") != 2:
                return b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
            return (
                json.dumps({"version": 2, "ok": True, "snapshot": v2_wire}).encode(
                    "utf-8"
                )
                + b"\n"
            )

        def close(self):
            return None

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return V2OwnerConnection()

    owner = RemoteRuntimeOwner(Endpoint())

    snapshot = owner.refresh(timeout=1.0)

    assert snapshot.state is AuthState.SIGNED_OUT
    assert snapshot.cloud_state is None
    assert [json.loads(frame)["version"] for frame in sent_frames] == [3, 2]


def _seeded_legacy_backend(client: FakeAuthClient, principal_key: str) -> FakeSecretBackend:
    from hermes_cli.client_auth.runtime import _encode_cookie_blob

    return FakeSecretBackend(
        raw=_encode_cookie_blob(
            CookieRecord(
                cookies=dict(client.record.cookies),
                username="alice",
                session_expires_at=status_at().session_expires_at,
                principal_key=principal_key,
            )
        )
    )


def test_status_refresh_with_real_context_upgrades_legacy_record_atomically():
    from hermes_cli.client_auth.runtime import NativeCredentialRecord, _decode_credential_blob

    client = FakeAuthClient()
    legacy_key = "legacy:" + "c" * 64
    backend = _seeded_legacy_backend(client, legacy_key)
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda low, _high: low,
    )

    restored = owner.refresh()
    assert restored.legacy is True
    assert client.native_issue_calls == 0

    upgraded = owner.refresh(installation_id=INSTALLATION_ID, client_version="0.17.0")

    assert client.native_issue_calls == 1
    assert upgraded.state is AuthState.AUTHENTICATED
    assert upgraded.legacy is False
    assert upgraded.account_id == ACCOUNT_ID
    assert upgraded.installation_id == INSTALLATION_ID
    assert upgraded.predecessor_principal_key == legacy_key
    assert upgraded.validation_state is ValidationState.ONLINE
    assert upgraded.runtime_instance_id == restored.runtime_instance_id
    assert upgraded.epoch == restored.epoch

    persisted = _decode_credential_blob(backend.raw)
    assert isinstance(persisted, NativeCredentialRecord)
    assert persisted.credential.installation_id == INSTALLATION_ID
    assert persisted.predecessor_principal_key == legacy_key

    assert owner.validate_now().legacy is False
    assert client.native_issue_calls == 1


def test_context_refresh_outage_keeps_legacy_local_authorization_until_recovery():
    from hermes_cli.client_auth.runtime import _decode_credential_blob

    class FlakyUpgradeClient(FakeAuthClient):
        def __init__(self) -> None:
            super().__init__()
            self.fail_issue = False

        def issue_client_session(self, cookies, *, installation_id, client_version):
            if self.fail_issue:
                raise AuthServiceError("server_unavailable")
            return super().issue_client_session(
                cookies, installation_id=installation_id, client_version=client_version
            )

    client = FlakyUpgradeClient()
    legacy_key = "legacy:" + "d" * 64
    backend = _seeded_legacy_backend(client, legacy_key)
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=backend,
        clock=FakeClock(),
        jitter=lambda low, _high: low,
    )

    client.status_error = AuthServiceError("server_unavailable")
    degraded = owner.refresh(installation_id=INSTALLATION_ID, client_version="0.17.0")
    assert degraded.state is AuthState.AUTHENTICATED
    assert degraded.legacy is True
    assert degraded.validation_state is ValidationState.DEGRADED
    assert client.native_issue_calls == 0

    client.status_error = None
    client.fail_issue = True
    still_legacy = owner.refresh(installation_id=INSTALLATION_ID, client_version="0.17.0")
    assert still_legacy.state is AuthState.AUTHENTICATED
    assert still_legacy.legacy is True
    assert still_legacy.validation_state is ValidationState.DEGRADED
    assert isinstance(_decode_credential_blob(backend.raw), CookieRecord)

    client.fail_issue = False
    upgraded = owner.refresh(installation_id=INSTALLATION_ID, client_version="0.17.0")
    assert upgraded.legacy is False
    assert upgraded.installation_id == INSTALLATION_ID
    assert upgraded.predecessor_principal_key == legacy_key


def test_runtime_never_fabricates_installation_ids():
    import hermes_cli.client_auth.runtime as runtime_module

    source = Path(runtime_module.__file__).read_text()
    assert "uuid.uuid4" not in source


@pytest.mark.skipif(os.name == "nt", reason="Unix runtime protocol")
def test_owner_broker_status_with_native_context_performs_legacy_upgrade():
    client = FakeAuthClient()
    legacy_key = "legacy:" + "e" * 64
    owner = MemoryOwner(
        client,
        hardener=RecordingHardener(),
        secret_backend=_seeded_legacy_backend(client, legacy_key),
        clock=FakeClock(),
        jitter=lambda low, _high: low,
    )
    with tempfile.TemporaryDirectory(prefix="ha-upg-") as temporary:
        endpoint = UnixEndpoint.for_directory(
            Path(temporary),
            random_name="00112233445566778899aabbccddeeff",
        )
        broker = OwnerBroker.start(owner, endpoint=endpoint)
        try:

            def roundtrip(request: dict[str, object]) -> dict[str, object]:
                connection = endpoint.connect_current(timeout=1.0)
                try:
                    connection.sendall(json.dumps(request).encode("utf-8") + b"\n")
                    raw = _read_runtime_frame(connection, timeout=1.0)
                finally:
                    connection.close()
                return json.loads(raw)

            malformed = roundtrip(
                {
                    "version": 2,
                    "operation": "status",
                    "installation_id": "not-a-uuid",
                    "client_version": "0.17.0",
                }
            )
            assert malformed["ok"] is False
            assert client.native_issue_calls == 0

            upgraded = roundtrip(
                {
                    "version": 2,
                    "operation": "status",
                    "installation_id": INSTALLATION_ID,
                    "client_version": "0.17.0",
                }
            )
            assert upgraded["ok"] is True
            assert upgraded["snapshot"]["legacy"] is False
            assert upgraded["snapshot"]["installation_id"] == INSTALLATION_ID
            assert upgraded["snapshot"]["predecessor_principal_key"] == legacy_key
            assert client.native_issue_calls == 1
        finally:
            broker.close()


def test_remote_owner_sends_native_context_only_on_protocol_v2_or_newer():
    signed_out = RuntimeSnapshot.signed_out()
    legacy_wire = {
        key: signed_out.public_dict()[key] for key in LEGACY_SNAPSHOT_WIRE_KEYS
    }
    sent_frames: list[bytes] = []

    class OldOwnerConnection:
        def settimeout(self, _timeout):
            return None

        def sendall(self, data):
            sent_frames.append(bytes(data))

        def recv(self, _size):
            request = json.loads(sent_frames[-1])
            if request.get("version") != 1 or set(request) != {"version", "operation"}:
                return b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
            return (
                json.dumps({"version": 1, "ok": True, "snapshot": legacy_wire}).encode("utf-8")
                + b"\n"
            )

        def close(self):
            return None

    class Endpoint:
        def connect_current(self, *, timeout):
            assert timeout > 0
            return OldOwnerConnection()

    owner = RemoteRuntimeOwner(Endpoint())
    result = owner.refresh(timeout=1.0, installation_id=INSTALLATION_ID, client_version="0.17.0")

    assert result.state is AuthState.SIGNED_OUT
    requests = [json.loads(frame) for frame in sent_frames]
    assert [request["version"] for request in requests] == [3, 2, 1]
    for request in requests[:2]:
        assert request["installation_id"] == INSTALLATION_ID
        assert request["client_version"] == "0.17.0"
    assert set(requests[2]) == {"version", "operation"}, (
        "an old owner cannot upgrade, so the v1 fallback must drop the context"
    )
