from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from hermes_cli.client_auth.bridge import dispatch, run_stream


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


def test_bridge_redacts_runtime_exception_text(monkeypatch):
    def fail():
        raise RuntimeError("agent_history_sessionid=do-not-leak")

    monkeypatch.setattr("hermes_cli.client_auth.bridge.account_status", fail)

    response = dispatch(
        {"version": 1, "id": "1", "method": "status", "params": {}}
    )

    assert response["error"] == {"code": "INTERNAL_ERROR"}
    assert "sessionid" not in json.dumps(response).casefold()


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
