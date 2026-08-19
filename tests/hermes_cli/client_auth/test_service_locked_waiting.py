from __future__ import annotations

import json
from pathlib import Path

from hermes_cli.client_auth.runtime import (
    AuthState,
    RuntimeSnapshot,
    S6LifecycleAdapter,
)


def _slot(root: Path, name: str) -> None:
    (root / name).mkdir(parents=True)


def test_s6_lifecycle_starts_only_desired_slots_and_locks_all(tmp_path: Path):
    services = tmp_path / "services"
    home = tmp_path / "home"
    services.mkdir(); home.mkdir()
    _slot(services, "dashboard")
    _slot(services, "main-hermes")
    _slot(services, "gateway-default")
    _slot(services, "gateway-coder")
    _slot(services, "untrusted-arbitrary-service")
    (home / "gateway_state.json").write_text(
        json.dumps({"desired_state": "running"}),
        encoding="utf-8",
    )
    coder = home / "profiles" / "coder"
    coder.mkdir(parents=True)
    (coder / "gateway_state.json").write_text(
        json.dumps({"desired_state": "stopped"}),
        encoding="utf-8",
    )
    actions: list[tuple[str, str]] = []
    adapter = S6LifecycleAdapter(
        service_root=services,
        hermes_home=home,
        environment={"HERMES_DASHBOARD": "true"},
        signal_service=lambda service, action: actions.append((service, action)),
    )

    adapter.transition(RuntimeSnapshot.new_authenticated("alice", now=10, ttl=60))

    assert actions == [
        ("dashboard", "up"),
        ("gateway-coder", "down"),
        ("gateway-default", "up"),
        ("main-hermes", "down"),
    ]
    assert all(service != "untrusted-arbitrary-service" for service, _ in actions)

    actions.clear()
    adapter.transition(RuntimeSnapshot.signed_out(reason="signed_out"))
    assert actions == [
        ("dashboard", "down"),
        ("gateway-coder", "down"),
        ("gateway-default", "down"),
        ("main-hermes", "down"),
    ]


def test_s6_lifecycle_treats_malformed_intent_as_down(tmp_path: Path):
    services = tmp_path / "services"
    home = tmp_path / "home"
    services.mkdir(); home.mkdir()
    _slot(services, "gateway-default")
    (home / "gateway_state.json").write_text("not-json", encoding="utf-8")
    actions: list[tuple[str, str]] = []

    S6LifecycleAdapter(
        service_root=services,
        hermes_home=home,
        environment={},
        signal_service=lambda service, action: actions.append((service, action)),
    ).transition(RuntimeSnapshot.new_authenticated("alice", now=10, ttl=60))

    assert actions == [("gateway-default", "down")]


def test_s6_lifecycle_never_receives_or_persists_secrets(tmp_path: Path):
    services = tmp_path / "services"
    home = tmp_path / "home"
    services.mkdir(); home.mkdir()
    _slot(services, "gateway-default")
    adapter = S6LifecycleAdapter(
        service_root=services,
        hermes_home=home,
        environment={},
        signal_service=lambda _service, _action: None,
    )

    snapshot = RuntimeSnapshot.new_authenticated("alice", now=10, ttl=60)
    adapter.transition(snapshot)

    assert snapshot.state is AuthState.AUTHENTICATED
    assert "cookie" not in repr(adapter).casefold()
    assert "password" not in repr(adapter).casefold()
