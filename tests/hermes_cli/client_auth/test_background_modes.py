from __future__ import annotations

import threading
import plistlib
from pathlib import Path

import pytest

from hermes_cli.client_auth.runtime import (
    AuthRequired,
    AuthState,
    LockedWaitingResult,
    RuntimeSnapshot,
    wait_until_authorized,
)


REPO_ROOT = Path(__file__).resolve().parents[3]


class _Consumer:
    def __init__(self, snapshot: RuntimeSnapshot) -> None:
        self.snapshot = snapshot
        self.boundaries: list[str] = []

    def require_authorized(self, boundary: str, *, expected):
        self.boundaries.append(boundary)
        return self.snapshot.require_authorized(
            boundary,
            expected=expected,
            now=101.0,
        )


class _Owner:
    def __init__(self, snapshots: list[RuntimeSnapshot]) -> None:
        self.snapshots = iter(snapshots)
        self.current = snapshots[0]
        self.consumer: _Consumer | None = None

    def refresh(self) -> RuntimeSnapshot:
        self.current = next(self.snapshots)
        return self.current

    def connect_consumer(self):
        self.consumer = _Consumer(self.current)
        return self.consumer


def test_locked_waiting_never_prompts_and_authorizes_before_return(monkeypatch):
    locked = RuntimeSnapshot.signed_out(reason="signed_out")
    authenticated = RuntimeSnapshot.new_authenticated(
        "alice",
        now=100.0,
        ttl=60.0,
    )
    owner = _Owner([locked, authenticated])
    states: list[AuthState] = []
    installed = []

    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda **_kwargs: owner,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.install_runtime_consumer",
        installed.append,
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("locked-waiting must never prompt"),
    )
    monkeypatch.setattr(
        "getpass.getpass",
        lambda _prompt: pytest.fail("locked-waiting must never prompt"),
    )

    result = wait_until_authorized(
        "service.gateway.start",
        stop_event=threading.Event(),
        on_state=lambda snapshot: states.append(snapshot.state),
        poll_seconds=0,
    )

    assert result is LockedWaitingResult.AUTHENTICATED
    assert states == [AuthState.SIGNED_OUT, AuthState.AUTHENTICATED]
    assert owner.consumer is not None
    assert owner.consumer.boundaries == ["service.gateway.start"]
    assert installed == [owner.consumer]


def test_locked_waiting_stops_cleanly_when_supervisor_stops(monkeypatch):
    stop = threading.Event()
    stop.set()
    monkeypatch.setattr(
        "hermes_cli.client_auth.runtime.connect_runtime_owner",
        lambda: (_ for _ in ()).throw(AuthRequired("runtime_unavailable")),
    )

    assert wait_until_authorized(
        "service.gateway.start",
        stop_event=stop,
        on_state=lambda _snapshot: pytest.fail("stopped waiter must not poll"),
    ) is LockedWaitingResult.OWNER_STOPPED


def test_runtime_wait_command_emits_only_redacted_state(monkeypatch, capsys):
    from hermes_cli.client_auth import runtime

    def wait(boundary, *, stop_event, on_state, poll_seconds=0.5):
        assert boundary == "service.gateway.start"
        assert not stop_event.is_set()
        assert poll_seconds == 0.5
        on_state(RuntimeSnapshot.signed_out(reason="signed_out"))
        return LockedWaitingResult.OWNER_STOPPED

    monkeypatch.setattr(runtime, "wait_until_authorized", wait)

    assert runtime.main(["wait", "service.gateway.start"]) == 0
    output = capsys.readouterr().out
    assert '"auth_state": "locked-waiting"' in output
    assert "run `ansatz login`" in output
    assert "cookie" not in output.casefold()
    assert "password" not in output.casefold()


def test_s6_auth_owner_orders_container_capabilities():
    auth = REPO_ROOT / "docker" / "s6-rc.d" / "hermes-auth-runtime"

    assert (auth / "type").read_text(encoding="utf-8") == "longrun\n"
    run = (auth / "run").read_text(encoding="utf-8")
    assert "s6-setuidgid hermes" in run
    assert "hermes_cli.client_auth.runtime owner" in run
    assert (REPO_ROOT / "docker" / "s6-rc.d" / "user" / "contents.d" / "hermes-auth-runtime").exists()
    for service in ("dashboard", "main-hermes"):
        dependency = REPO_ROOT / "docker" / "s6-rc.d" / service / "dependencies.d" / "hermes-auth-runtime"
        assert dependency.exists()


def test_container_capability_scripts_wait_before_exec():
    dashboard = (REPO_ROOT / "docker" / "s6-rc.d" / "dashboard" / "run").read_text(encoding="utf-8")
    wrapper = (REPO_ROOT / "docker" / "main-wrapper.sh").read_text(encoding="utf-8")

    assert "runtime wait container.dashboard.start" in dashboard
    assert dashboard.index("runtime wait") < dashboard.index("hermes dashboard")
    assert "runtime wait container.main.start" in wrapper
    assert wrapper.index("runtime wait") < wrapper.index("drop hermes")


def test_runtime_service_waits_then_execs_fixed_gateway(monkeypatch):
    from hermes_cli.client_auth import runtime

    calls = []
    monkeypatch.setattr(
        runtime,
        "wait_until_authorized",
        lambda boundary, **kwargs: calls.append(("wait", boundary, kwargs))
        or LockedWaitingResult.AUTHENTICATED,
    )
    monkeypatch.setattr(
        runtime.os,
        "execv",
        lambda executable, argv: calls.append(("exec", executable, argv)),
    )

    assert runtime.main(["service", "gateway", "coder"]) == 0
    assert calls[0][0:2] == ("wait", "service.gateway.start")
    assert calls[0][2]["start_owner_if_missing"] is True
    assert calls[1] == (
        "exec",
        runtime.sys.executable,
        [
            runtime.sys.executable,
            "-m",
            "hermes_cli.main",
            "-p",
            "coder",
            "gateway",
            "run",
        ],
    )


def test_runtime_service_waits_then_execs_fixed_kanban(monkeypatch):
    from hermes_cli.client_auth import runtime

    calls = []
    monkeypatch.setattr(
        runtime,
        "wait_until_authorized",
        lambda boundary, **kwargs: calls.append(("wait", boundary, kwargs))
        or LockedWaitingResult.AUTHENTICATED,
    )
    monkeypatch.setattr(
        runtime.os,
        "execv",
        lambda executable, argv: calls.append(("exec", executable, argv)),
    )

    assert runtime.main(["service", "kanban"]) == 0
    assert calls[0][0:2] == ("wait", "service.kanban.start")
    assert calls[1] == (
        "exec",
        runtime.sys.executable,
        [
            runtime.sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "daemon",
            "--force",
            "--interval",
            "60",
        ],
    )


def test_locked_gateway_service_reports_healthy_wait_to_systemd(monkeypatch):
    from hermes_cli.client_auth import runtime

    notified: list[AuthState] = []

    def wait(_boundary, *, on_state, **_kwargs):
        on_state(RuntimeSnapshot.signed_out(reason="signed_out"))
        on_state(RuntimeSnapshot.new_authenticated("alice", now=100, ttl=60))
        return LockedWaitingResult.AUTHENTICATED

    monkeypatch.setattr(runtime, "wait_until_authorized", wait)
    monkeypatch.setattr(
        runtime,
        "_notify_service_manager",
        lambda snapshot: notified.append(snapshot.state),
    )
    monkeypatch.setattr(runtime.os, "execv", lambda _executable, _argv: None)

    assert runtime.main(["service", "gateway"]) == 0
    assert notified == [AuthState.SIGNED_OUT, AuthState.AUTHENTICATED]


def test_generated_host_services_enter_locked_waiting_without_secrets(
    monkeypatch,
    tmp_path: Path,
):
    import hermes_cli.gateway as gateway
    import hermes_cli.gateway_windows as gateway_windows

    home = tmp_path / ".hermes" / "profiles" / "coder"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_python_path", lambda: "/opt/hermes/bin/python")
    monkeypatch.setattr(gateway, "_stable_service_working_dir", lambda: str(home))

    unit = gateway.generate_systemd_unit()
    assert (
        "ExecStart=/opt/hermes/bin/python -m hermes_cli.client_auth.runtime "
        "service gateway coder"
    ) in unit
    assert "Restart=always" in unit

    plist = plistlib.loads(gateway.generate_launchd_plist().encode("utf-8"))
    assert plist["ProgramArguments"] == [
        "/opt/hermes/bin/python",
        "-m",
        "hermes_cli.client_auth.runtime",
        "service",
        "gateway",
        "coder",
    ]

    script = gateway_windows._build_gateway_cmd_script(
        r"C:\\Hermes\\python.exe",
        r"C:\\Hermes",
        r"C:\\Users\\alice\\.hermes\\profiles\\coder",
        "--profile coder",
    )
    assert (
        "hermes_cli.client_auth.runtime service gateway coder" in script
    )

    combined = "\n".join((unit, repr(plist), script)).casefold()
    for forbidden in ("password", "csrf", "cookie", "bearer"):
        assert forbidden not in combined
