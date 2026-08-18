import os
import socket
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli.client_auth.client import (
    AuthServiceError,
    CookieRecord,
    SessionStatus,
)
from hermes_cli.client_auth.runtime import (
    LEASE_SECONDS,
    LOGIN_ATTEMPT_LIMIT,
    OWNER_IDLE_SECONDS,
    AuthRequired,
    AuthScope,
    AuthState,
    MemoryOwner,
    OwnerBroker,
    OwnerElectionContext,
    ProcessHardener,
    RuntimeConsumer,
    RuntimeSnapshot,
    SocketLivenessProbe,
    UnixEndpoint,
    WindowsNamedPipeEndpoint,
    VaultOwner,
    authorize_entrypoint,
    clear_entrypoint_owner,
    connect_runtime_owner,
    install_entrypoint_owner,
    resolve_owner,
    runtime_endpoint,
    start_runtime_owner,
)


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
    remote = object()
    attempts = iter([AuthRequired("runtime_unavailable"), remote])

    def connect():
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
    assert endpoint.root.name == "ha"
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
