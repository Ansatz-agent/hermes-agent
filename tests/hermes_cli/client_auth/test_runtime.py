import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from hermes_cli.client_auth.client import SessionStatus
from hermes_cli.client_auth.runtime import (
    LEASE_SECONDS,
    AuthRequired,
    AuthScope,
    RuntimeConsumer,
    RuntimeSnapshot,
    SocketLivenessProbe,
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
