from __future__ import annotations

from dataclasses import dataclass, replace

from tui_gateway import entry


@dataclass(frozen=True)
class _Status:
    state: str
    username: str | None = None
    runtime_instance_id: str = "0123456789abcdef0123456789abcdef"
    epoch: int = 1
    valid_until: float = 0.0
    session_expires_at: str | None = None
    reason: str | None = "signed_out"

    def public_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "username": self.username,
            "runtime_instance_id": self.runtime_instance_id,
            "epoch": self.epoch,
            "valid_until": self.valid_until,
            "session_expires_at": self.session_expires_at,
            "reason": self.reason,
        }


class _AuthRuntime:
    def __init__(self, status: _Status) -> None:
        self.current = status
        self.require_calls: list[str] = []

    def status(self) -> _Status:
        return self.current

    def login(self, username: str, password: bytearray) -> _Status:
        assert username == "alice"
        assert bytes(password) == b"correct horse"
        self.current = replace(
            self.current,
            state="authenticated",
            username="alice",
            epoch=self.current.epoch + 1,
            valid_until=9999.0,
            reason=None,
        )
        return self.current

    def logout(self) -> _Status:
        self.current = replace(
            self.current,
            state="signed_out",
            username=None,
            epoch=self.current.epoch + 1,
            valid_until=0.0,
            reason="signed_out",
        )
        return self.current

    def require(self, boundary: str, status: _Status) -> None:
        self.require_calls.append(boundary)
        if status.state != "authenticated":
            raise entry.AuthRequired(status.reason or "signed_out")


class _Gateway:
    def __init__(self) -> None:
        self.dispatch_calls: list[dict] = []
        self.start_calls = 0
        self.stop_calls = 0

    def dispatch(self, request: dict) -> dict:
        self.dispatch_calls.append(request)
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {"ok": True}}

    def start(self) -> None:
        self.start_calls += 1

    def stop(self) -> None:
        self.stop_calls += 1


def _request(method: str, params: object = None, request_id: str = "r1") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {} if params is None else params,
    }


def test_locked_start_emits_only_auth_status_and_does_not_start_gateway() -> None:
    emitted: list[dict] = []
    runtime = _AuthRuntime(_Status("locked", reason="session_expired"))
    gateway = _Gateway()
    shell = entry.AccountAuthShell(runtime, gateway, emitted.append)

    shell.start()

    assert emitted == [
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "auth.status",
                "payload": runtime.current.public_dict(),
            },
        }
    ]
    assert gateway.start_calls == 0
    assert gateway.dispatch_calls == []


def test_locked_shell_allows_only_three_auth_methods_before_capabilities() -> None:
    emitted: list[dict] = []
    runtime = _AuthRuntime(_Status("signed_out"))
    gateway = _Gateway()
    shell = entry.AccountAuthShell(runtime, gateway, emitted.append)
    shell.start()

    for method in (
        "session.create",
        "prompt.submit",
        "session.list",
        "model.options",
        "tools.list",
        "mcp.servers.list",
        "file.attach",
    ):
        response = shell.dispatch(_request(method, request_id=method))
        assert response == {
            "jsonrpc": "2.0",
            "id": method,
            "error": {
                "code": 20,
                "message": "AUTH_REQUIRED",
                "data": {"reason": "signed_out"},
            },
        }

    assert gateway.start_calls == 0
    assert gateway.dispatch_calls == []

    status = shell.dispatch(_request("auth.status"))
    assert status["result"]["state"] == "signed_out"


def test_login_publishes_scope_then_starts_gateway_once_and_logout_locks_it() -> None:
    emitted: list[dict] = []
    runtime = _AuthRuntime(_Status("signed_out"))
    gateway = _Gateway()
    shell = entry.AccountAuthShell(runtime, gateway, emitted.append)
    shell.start()

    response = shell.dispatch(
        _request(
            "auth.login",
            {"username": "alice", "password": "correct horse"},
        )
    )

    assert response["result"]["state"] == "authenticated"
    assert emitted[-2]["params"] == {
        "type": "auth.changed",
        "payload": runtime.current.public_dict(),
    }
    assert emitted[-1]["params"]["type"] == "gateway.ready"
    assert gateway.start_calls == 1

    allowed = shell.dispatch(_request("session.create", {"cols": 80}))
    assert allowed["result"] == {"ok": True}
    assert runtime.require_calls[-1] == "tui.agent"

    logout = shell.dispatch(_request("auth.logout"))
    assert logout["result"]["state"] == "signed_out"
    assert gateway.stop_calls == 1

    denied = shell.dispatch(_request("session.create"))
    assert denied["error"]["code"] == 20
    assert len(gateway.dispatch_calls) == 1


def test_auth_rpc_schemas_are_fixed_and_never_echo_password() -> None:
    runtime = _AuthRuntime(_Status("signed_out"))
    gateway = _Gateway()
    shell = entry.AccountAuthShell(runtime, gateway, lambda _frame: None)
    shell.start()

    malformed = shell.dispatch(
        _request(
            "auth.login",
            {"username": "alice", "password": "secret", "server": "other"},
        )
    )

    assert malformed["error"]["code"] == -32602
    assert "secret" not in repr(malformed)
    assert runtime.current.state == "signed_out"


def test_owner_epoch_change_poll_locks_gateway_without_waiting_for_an_rpc() -> None:
    emitted: list[dict] = []
    runtime = _AuthRuntime(
        _Status(
            "authenticated",
            username="alice",
            valid_until=9999.0,
            reason=None,
        )
    )
    gateway = _Gateway()
    shell = entry.AccountAuthShell(runtime, gateway, emitted.append)
    shell.start()
    assert gateway.start_calls == 1

    runtime.current = replace(
        runtime.current,
        epoch=runtime.current.epoch + 1,
        state="locked",
        valid_until=0.0,
        reason="session_rejected",
    )
    shell.poll()

    assert gateway.stop_calls == 1
    assert emitted[-1]["params"] == {
        "type": "auth.changed",
        "payload": runtime.current.public_dict(),
    }


def test_server_dispatch_denies_locked_capability_before_handler(monkeypatch) -> None:
    from hermes_cli.client_auth.runtime import AuthRequired
    from tui_gateway import server

    called: list[str] = []
    monkeypatch.setitem(
        server._methods,
        "session.create",
        lambda _rid, _params: called.append("handler") or {"result": {}},
    )
    monkeypatch.setattr(
        server,
        "require_authorized",
        lambda _boundary: (_ for _ in ()).throw(AuthRequired("session_expired")),
    )

    response = server.dispatch(_request("session.create"))

    assert response == {
        "jsonrpc": "2.0",
        "id": "r1",
        "error": {
            "code": 20,
            "message": "AUTH_REQUIRED",
            "data": {"reason": "session_expired"},
        },
    }
    assert called == []


def test_server_auth_status_bypasses_capability_guard(monkeypatch) -> None:
    from hermes_cli.client_auth.runtime import AuthRequired
    from tui_gateway import server

    status = _Status("signed_out")
    monkeypatch.setattr(server, "account_status", lambda: status, raising=False)
    monkeypatch.setattr(
        server,
        "require_authorized",
        lambda _boundary: (_ for _ in ()).throw(AuthRequired("signed_out")),
    )

    response = server.dispatch(_request("auth.status"))

    assert response == {
        "jsonrpc": "2.0",
        "id": "r1",
        "result": status.public_dict(),
    }
