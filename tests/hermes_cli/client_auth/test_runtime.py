import base64
import json
import os
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
    SessionRejected,
    SessionStatus,
    TraceCredential,
)
from hermes_cli.client_auth.runtime import (
    LEASE_SECONDS,
    LOGIN_ATTEMPT_LIMIT,
    LockedWaitingResult,
    OWNER_IDLE_SECONDS,
    AuthRequired,
    AuthScope,
    AuthState,
    BackendScopeTokenRegistry,
    MemoryOwner,
    OwnerBroker,
    OwnerElectionContext,
    ProcessHardener,
    RuntimeConsumer,
    RuntimeSnapshot,
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
    require_authorized,
    resolve_owner,
    parse_backend_scope_token_registration,
    runtime_endpoint,
    start_runtime_owner,
    wait_until_authorized,
    _read_runtime_frame,
    _test_runtime_suffix,
)


def _scope_bearer(seed: bytes = b"A") -> str:
    return base64.urlsafe_b64encode(seed * 32).decode("ascii").rstrip("=")


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
    with pytest.raises(AuthRequired):
        registry.authorize_claim(grant.claim(), "dashboard.ws.message")

    replacement = registry.register(
        _scope_bearer(b"B"),
        connection_id="local",
        expected=current,
        ttl_seconds=30,
    )
    clock.now = replacement.valid_until
    with pytest.raises(AuthRequired, match="session_expired"):
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


def test_scope_token_control_eof_revokes_every_registered_bearer(monkeypatch):
    from hermes_cli.client_auth import runtime

    registered = threading.Event()
    release_eof = threading.Event()
    current = AuthScope("0123456789abcdef0123456789abcdef", 7)

    def authorize(_boundary, *, expected):
        assert expected == current
        registered.set()
        return expected

    registry = BackendScopeTokenRegistry(authorize=authorize)
    monkeypatch.setattr(runtime, "backend_scope_tokens", registry)
    bearer = _scope_bearer()
    frame = json.dumps(
        {
            "version": 1,
            "operation": "register_scope_token",
            "bearer": bearer,
            "connection_id": "local",
            "runtime_instance_id": current.runtime_instance_id,
            "epoch": current.epoch,
            "ttl_seconds": 60,
        }
    ).encode("utf-8") + b"\n"

    class BlockingStream:
        def __init__(self):
            self.first = True

        def readline(self, _limit):
            if self.first:
                self.first = False
                return frame
            release_eof.wait(timeout=2)
            return b""

    control = threading.Thread(
        target=runtime._run_backend_scope_token_control,
        args=(BlockingStream(),),
    )
    control.start()
    assert registered.wait(timeout=2)
    assert registry.authorize(bearer, "dashboard.api.request").auth == current

    release_eof.set()
    control.join(timeout=2)
    assert not control.is_alive()
    with pytest.raises(AuthRequired):
        registry.authorize(bearer, "dashboard.api.request")


def status_at(*, server_second: int = 0, expiry_second: int = 120) -> SessionStatus:
    origin = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
    return SessionStatus(
        username="alice",
        server_time=(origin + timedelta(seconds=server_second)).isoformat(),
        session_expires_at=(origin + timedelta(seconds=expiry_second)).isoformat(),
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

    with pytest.raises(AuthRequired, match="runtime_unavailable"):
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
    def __init__(self, *, fail_reads: bool = False, fail_writes: bool = False) -> None:
        self.raw: str | None = None
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
                "agent_history_sessionid": "session-1",
                "agent_history_csrftoken": "csrf-1",
            },
            username="alice",
            session_expires_at=status_at().session_expires_at,
        )
        self.status_value = status_at()
        self.login_error: AuthServiceError | None = None
        self.status_error: AuthServiceError | None = None
        self.logout_error: AuthServiceError | None = None
        self.login_calls = 0
        self.status_calls = 0
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
    assert secret_backend.read() is None


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


def test_logout_locks_and_clears_secret_even_when_remote_logout_fails():
    owner, secret_backend, auth_client, _clock = vault_owner_factory()
    before = owner.login("alice", bytearray(b"secret"))
    auth_client.logout_error = AuthServiceError("server_unavailable")

    after = owner.logout()

    assert after.state is AuthState.SIGNED_OUT
    assert after.epoch == before.epoch + 1
    assert secret_backend.read() is None


def test_refresh_failure_revokes_scope_without_extra_grace():
    owner, _secret_backend, auth_client, clock = vault_owner_factory()
    authenticated = owner.login("alice", bytearray(b"secret"))
    auth_client.status_error = AuthServiceError("server_unavailable")
    clock.now = owner.next_refresh_at

    with pytest.raises(AuthRequired, match="server_unavailable"):
        owner.refresh()

    assert owner.snapshot().state is AuthState.LOCKED
    assert owner.snapshot().epoch == authenticated.epoch + 1
    assert owner.snapshot().valid_until == clock.now


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
        "version": 1,
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
            "agent_history_sessionid": "old-session",
            "agent_history_csrftoken": "old-csrf",
        },
        username="alice",
        session_expires_at=status_at().session_expires_at,
    )
    new_record = CookieRecord(
        cookies={
            "agent_history_sessionid": "new-session",
            "agent_history_csrftoken": "new-csrf",
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
