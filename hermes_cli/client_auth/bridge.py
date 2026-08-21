from __future__ import annotations

import json
import math
import sys
import time
from collections.abc import Callable, Mapping
from typing import BinaryIO

from hermes_cli.client_auth.runtime import (
    AuthRequired,
    account_login,
    account_logout,
    account_status,
    clear_entrypoint_owner,
    connect_runtime_owner,
    install_entrypoint_owner,
    start_runtime_owner,
)


PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 64 * 1024
MAX_REQUEST_ID_LENGTH = 64
MAX_USERNAME_LENGTH = 150
MAX_PASSWORD_LENGTH = 4096

_PUBLIC_KEYS = frozenset(
    {
        "state",
        "username",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "session_expires_at",
        "reason",
    }
)
_PUBLIC_STATES = frozenset({"checking", "authenticated", "signed_out", "locked"})
_SAFE_REASONS = frozenset(
    {
        "interactive_login_required",
        "invalid_credentials",
        "rate_limited",
        "runtime_unavailable",
        "server_unavailable",
        "session_expired",
        "session_rejected",
        "signed_out",
        "vault_unavailable",
    }
)


def _status(_params: Mapping[str, object]) -> dict[str, object]:
    snapshot = account_status()
    if snapshot.reason == "runtime_unavailable":
        raise AuthRequired("runtime_unavailable")
    return _validated_public_result(snapshot.public_dict())


def _login(params: Mapping[str, object]) -> dict[str, object]:
    username = params.get("username")
    password_text = params.get("password")
    if (
        not isinstance(username, str)
        or not username.strip()
        or len(username) > MAX_USERNAME_LENGTH
        or not isinstance(password_text, str)
        or not password_text
        or len(password_text) > MAX_PASSWORD_LENGTH
    ):
        raise ValueError("invalid login fields")
    try:
        password = bytearray(password_text.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("invalid login fields") from None
    if isinstance(params, dict):
        params["password"] = ""
    password_text = ""
    try:
        snapshot = account_login(username.strip(), password)
    finally:
        password[:] = b"\0" * len(password)
    return _validated_public_result(snapshot.public_dict())


def _logout(_params: Mapping[str, object]) -> dict[str, object]:
    return _validated_public_result(account_logout().public_dict())


METHODS: dict[str, Callable[[Mapping[str, object]], dict[str, object]]] = {
    "status": _status,
    "login": _login,
    "logout": _logout,
}
ALLOWED_PARAMS = {
    "status": frozenset(),
    "login": frozenset({"username", "password"}),
    "logout": frozenset(),
}


def dispatch(request: object) -> dict[str, object]:
    request_id = _safe_request_id(request)
    if (
        not isinstance(request, dict)
        or set(request) != {"version", "id", "method", "params"}
        or request.get("version") != PROTOCOL_VERSION
        or request_id is None
    ):
        return _error(None, "INVALID_REQUEST")

    method = request.get("method")
    params = request.get("params")
    if not isinstance(method, str) or method not in METHODS:
        return _error(request_id, "METHOD_NOT_ALLOWED")
    if not isinstance(params, dict) or set(params) != ALLOWED_PARAMS[method]:
        return _error(request_id, "INVALID_PARAMS")

    try:
        result = METHODS[method](params)
    except AuthRequired as error:
        reason = error.reason if error.reason in _SAFE_REASONS else "runtime_unavailable"
        return _error(request_id, "AUTH_REQUIRED", reason=reason)
    except ValueError:
        return _error(request_id, "INVALID_PARAMS")
    except BaseException:
        return _error(request_id, "INTERNAL_ERROR")
    return {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "result": result,
    }


def run_stream(source: BinaryIO, target: BinaryIO) -> None:
    while True:
        line = source.readline(MAX_LINE_BYTES + 1)
        if line == b"":
            return
        if len(line) > MAX_LINE_BYTES:
            if not line.endswith(b"\n"):
                _discard_line_remainder(source)
            response = _error(None, "LINE_TOO_LONG")
        else:
            try:
                request = json.loads(line)
            except (UnicodeError, ValueError):
                response = _error(None, "INVALID_REQUEST")
            else:
                response = dispatch(request)
        encoded = json.dumps(
            response,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        target.write(encoded + b"\n")
        target.flush()


def _discard_line_remainder(source: BinaryIO) -> None:
    while True:
        remainder = source.readline(MAX_LINE_BYTES + 1)
        if remainder == b"" or remainder.endswith(b"\n"):
            return


def _safe_request_id(request: object) -> str | None:
    if not isinstance(request, dict):
        return None
    value = request.get("id")
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAX_REQUEST_ID_LENGTH
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        return None
    return value


def _error(
    request_id: str | None,
    code: str,
    *,
    reason: str | None = None,
) -> dict[str, object]:
    details: dict[str, object] = {"code": code}
    if reason is not None:
        details["reason"] = reason
    return {
        "version": PROTOCOL_VERSION,
        "id": request_id,
        "error": details,
    }


def _validated_public_result(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != _PUBLIC_KEYS:
        raise RuntimeError("invalid public result")
    state = value.get("state")
    username = value.get("username")
    runtime_instance_id = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    valid_until = value.get("valid_until")
    session_expires_at = value.get("session_expires_at")
    reason = value.get("reason")
    if state not in _PUBLIC_STATES:
        raise RuntimeError("invalid public result")
    if username is not None and (
        not isinstance(username, str) or not username or len(username) > 150
    ):
        raise RuntimeError("invalid public result")
    if (
        not isinstance(runtime_instance_id, str)
        or not runtime_instance_id
        or len(runtime_instance_id) > 128
    ):
        raise RuntimeError("invalid public result")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise RuntimeError("invalid public result")
    if (
        not isinstance(valid_until, (int, float))
        or isinstance(valid_until, bool)
        or valid_until < 0
        or not math.isfinite(valid_until)
    ):
        raise RuntimeError("invalid public result")
    if session_expires_at is not None and (
        not isinstance(session_expires_at, str) or len(session_expires_at) > 128
    ):
        raise RuntimeError("invalid public result")
    if reason is not None and reason not in _SAFE_REASONS:
        raise RuntimeError("invalid public result")
    result = dict(value)
    if state == "authenticated":
        # Runtime leases use a process-local monotonic clock. Convert the
        # remaining duration to a Unix timestamp before returning it to a
        # Desktop process (including one reached over SSH), whose monotonic
        # clock has a different origin.
        remaining = max(0.0, float(valid_until) - time.monotonic())
        result["valid_until"] = time.time() + remaining
    return result


def main() -> int:
    try:
        owner = connect_runtime_owner(timeout=2.0)
    except AuthRequired:
        owner = start_runtime_owner(timeout=4.0, probe_first=False)
    install_entrypoint_owner(owner)
    try:
        run_stream(sys.stdin.buffer, sys.stdout.buffer)
    finally:
        clear_entrypoint_owner()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
