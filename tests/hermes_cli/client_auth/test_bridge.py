from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from hermes_cli.client_auth.bridge import dispatch, main, run_stream
from hermes_cli.client_auth.client import TraceCredential
from hermes_cli.client_auth.runtime import AuthRequired


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 1, "id": "1", "method": "signup", "params": {}},
        {"version": 1, "id": "1", "method": "exec", "params": {"command": "id"}},
    ],
)
def test_bridge_rejects_every_non_contract_operation(payload):
    response = dispatch(payload)

    assert response["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert "cookie" not in json.dumps(response).casefold()


def test_bridge_rejects_extra_login_parameters():
    response = dispatch(
        {
            "version": 1,
            "id": "1",
            "method": "login",
            "params": {
                "username": "alice",
                "password": "secret",
                "url": "https://evil.example",
            },
        }
    )

    assert response["error"]["code"] == "INVALID_PARAMS"
    assert "evil" not in json.dumps(response)


def test_login_response_contains_scope_but_no_secret(monkeypatch):
    captured: list[bytearray] = []

    def login(username: str, password: bytearray):
        assert username == "alice"
        assert password == bytearray(b"secret")
        captured.append(password)
        return SimpleNamespace(
            public_dict=lambda: {
                "state": "authenticated",
                "username": "alice",
                "runtime_instance_id": "runtime-1",
                "epoch": 2,
                "valid_until": 60.0,
                "session_expires_at": "2026-08-18T13:00:00+00:00",
                "reason": None,
            }
        )

    monkeypatch.setattr("hermes_cli.client_auth.bridge.account_login", login)
    response = dispatch(
        {
            "version": 1,
            "id": "1",
            "method": "login",
            "params": {"username": "alice", "password": "secret"},
        }
    )

    assert set(response["result"]) == {
        "state",
        "username",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "session_expires_at",
        "reason",
    }
    serialized = json.dumps(response)
    assert "secret" not in serialized
    assert "cookie" not in serialized.casefold()
    assert captured == [bytearray(b"\0" * 6)]


def test_bridge_translates_runtime_lease_to_unix_epoch(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.account_status",
        lambda: SimpleNamespace(
            reason=None,
            public_dict=lambda: {
                "state": "authenticated",
                "username": "alice",
                "runtime_instance_id": "runtime-1",
                "epoch": 2,
                "valid_until": 160.0,
                "session_expires_at": "2026-08-18T13:00:00+00:00",
                "reason": None,
            }
        ),
    )
    monkeypatch.setattr("hermes_cli.client_auth.bridge.time.monotonic", lambda: 100.0)
    monkeypatch.setattr("hermes_cli.client_auth.bridge.time.time", lambda: 1_800_000_000.0)

    response = dispatch(
        {"version": 1, "id": "1", "method": "status", "params": {}}
    )

    assert response["result"]["valid_until"] == 1_800_000_060.0


def test_bridge_redacts_runtime_exception_text(monkeypatch):
    def fail():
        raise RuntimeError("__Host-ansatz_sessionid=do-not-leak")

    monkeypatch.setattr("hermes_cli.client_auth.bridge.account_status", fail)

    response = dispatch(
        {"version": 1, "id": "1", "method": "status", "params": {}}
    )

    assert response["error"] == {"code": "INTERNAL_ERROR"}
    assert "sessionid" not in json.dumps(response).casefold()


def test_bridge_escalates_exhausted_local_runtime_recovery(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.account_status",
        lambda: SimpleNamespace(
            reason="runtime_unavailable",
            public_dict=lambda: pytest.fail(
                "an unavailable snapshot must not look like a successful status"
            ),
        ),
    )

    response = dispatch(
        {"version": 1, "id": "1", "method": "status", "params": {}}
    )

    assert response["error"] == {
        "code": "AUTH_REQUIRED",
        "reason": "runtime_unavailable",
    }


def test_stream_rejects_malformed_and_oversized_lines_without_echoing_input():
    oversized_secret = "secret-sentinel" * 5000
    source = io.BytesIO(
        b"not-json\n"
        + json.dumps({"password": oversized_secret}).encode("utf-8")
        + b"\n"
    )
    target = io.BytesIO()

    run_stream(source, target)

    responses = [json.loads(line) for line in target.getvalue().splitlines()]
    assert [response["error"]["code"] for response in responses] == [
        "INVALID_REQUEST",
        "LINE_TOO_LONG",
    ]
    assert oversized_secret.encode("utf-8") not in target.getvalue()


def test_request_id_and_schema_are_bounded():
    response = dispatch(
        {
            "version": 1,
            "id": "x" * 65,
            "method": "status",
            "params": {},
        }
    )

    assert response == {
        "version": 1,
        "id": None,
        "error": {"code": "INVALID_REQUEST"},
    }


def test_bridge_starts_detached_owner_before_serving_stream(monkeypatch):
    remote = object()
    events: list[str] = []

    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.connect_runtime_owner",
        lambda **_kwargs: (_ for _ in ()).throw(
            AuthRequired("runtime_unavailable")
        ),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.start_runtime_owner",
        lambda **_kwargs: events.append("detached-owner") or remote,
        raising=False,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.install_entrypoint_owner",
        lambda owner: events.append("install") if owner is remote else None,
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.clear_entrypoint_owner",
        lambda: events.append("clear"),
    )
    monkeypatch.setattr(
        "hermes_cli.client_auth.bridge.run_stream",
        lambda _source, _target: events.append("stream"),
    )

    assert main() == 0
    assert events == ["detached-owner", "install", "stream", "clear"]


def test_trace_token_bridge_uses_exact_request_and_never_exposes_session_cookies(monkeypatch):
    installation_id = "11111111-1111-4111-8111-111111111111"

    def issue(**params):
        assert params == {
            "installation_id": installation_id,
            "client_version": "0.17.0",
            "telemetry_schema_version": "1",
        }
        return TraceCredential(
            access_token="trace-token-sentinel-1234567890",
            expires_at="2099-08-23T14:15:00+00:00",
            expires_in=900,
            installation_id=installation_id,
        )

    monkeypatch.setattr("hermes_cli.client_auth.bridge.account_trace_token", issue)
    response = dispatch(
        {
            "version": 1,
            "id": "1",
            "method": "trace_token",
            "params": {
                "installation_id": installation_id,
                "client_version": "0.17.0",
                "telemetry_schema_version": "1",
            },
        }
    )

    assert response["result"] == {
        "access_token": "trace-token-sentinel-1234567890",
        "expires_at": "2099-08-23T14:15:00+00:00",
        "expires_in": 900,
        "installation_id": installation_id,
    }
    assert "cookie" not in json.dumps(response).casefold()


@pytest.mark.parametrize(
    "params",
    [
        {
            "installation_id": "not-a-uuid",
            "client_version": "0.17.0",
            "telemetry_schema_version": "1",
        },
        {
            "installation_id": "11111111-1111-4111-8111-111111111111",
            "client_version": "0.17.0",
            "telemetry_schema_version": "0",
        },
        {
            "installation_id": "11111111-1111-4111-8111-111111111111",
            "client_version": "0.17.0",
            "telemetry_schema_version": "1",
            "extra": True,
        },
    ],
)
def test_trace_token_bridge_rejects_invalid_or_extra_parameters(params):
    response = dispatch(
        {"version": 1, "id": "1", "method": "trace_token", "params": params}
    )

    assert response["error"]["code"] == "INVALID_PARAMS"
