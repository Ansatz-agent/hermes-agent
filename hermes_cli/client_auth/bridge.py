from __future__ import annotations

import json
import math
import re
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import BinaryIO

from hermes_cli.client_auth.client import TraceCredential
from hermes_cli.client_auth.runtime import (
    AuthRequired,
    DURABLE_AUTHORIZATION_VALID_UNTIL,
    account_login,
    account_logout,
    account_status,
    account_trace_ingress_open,
    account_trace_token,
    clear_entrypoint_owner,
    connect_runtime_owner,
    install_entrypoint_owner,
    start_runtime_owner,
)


PROTOCOL_VERSION = 2
MAX_LINE_BYTES = 64 * 1024
MAX_REQUEST_ID_LENGTH = 64
MAX_USERNAME_LENGTH = 150
MAX_PASSWORD_LENGTH = 4096

_PUBLIC_KEYS = frozenset(
    {
        "state",
        "username",
        "account_id",
        "session_id",
        "installation_id",
        "principal_key",
        "predecessor_principal_key",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "cloud_state",
        "validation_state",
        "validation_reason",
        "last_validated_at",
        "legacy",
        "reason",
    }
)
_PUBLIC_STATES = frozenset({"checking", "authenticated", "signed_out", "locked"})
_VALIDATION_STATES = frozenset({"unknown", "validating", "online", "degraded"})
_CLOUD_STATES = frozenset({"active", "unreachable", "reauth_required"})
_SAFE_REASONS = frozenset(
    {
        "interactive_login_required",
        "invalid_credentials",
        "rate_limited",
        "runtime_unavailable",
        "server_unavailable",
        "session_expired",
        "session_rejected",
        "invalid_response",
        "invalid_session_credential",
        "signed_out",
        "session_revoked",
        "account_disabled",
        "account_revoked",
        "vault_unavailable",
    }
)
_VALIDATION_REASONS = frozenset(
    {
        "rate_limited",
        "runtime_unavailable",
        "server_unavailable",
        "session_expired",
        "session_rejected",
        "invalid_response",
        "invalid_session_credential",
        "session_revoked",
        "account_disabled",
        "account_revoked",
        "vault_unavailable",
    }
)
_TERMINAL_REASONS = frozenset(
    {"signed_out", "session_revoked", "account_disabled", "account_revoked"}
)
_INTERNAL_RESPONSE_REASONS = frozenset({"invalid_csrf", "invalid_redirect"})


def _status(params: Mapping[str, object]) -> dict[str, object]:
    # The desktop's validated installation/client context lets the owner
    # perform the silent legacy-to-native upgrade on the first successful
    # online validation without fabricating an identity.
    context = _native_context(params)
    snapshot = account_status(
        installation_id=context["installation_id"],
        client_version=context["client_version"],
    )
    if snapshot.reason == "runtime_unavailable":
        raise AuthRequired("runtime_unavailable")
    return _validated_public_result(_bridge_public_snapshot(snapshot.public_dict()))


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
        snapshot = account_login(
            username.strip(),
            password,
            installation_id=_native_context(params)["installation_id"],
            client_version=_native_context(params)["client_version"],
        )
    finally:
        password[:] = b"\0" * len(password)
    return _validated_public_result(_bridge_public_snapshot(snapshot.public_dict()))


def _logout(_params: Mapping[str, object]) -> dict[str, object]:
    return _validated_public_result(_bridge_public_snapshot(account_logout().public_dict()))


def _trace_token(params: Mapping[str, object]) -> dict[str, object]:
    installation_id = params.get("installation_id")
    client_version = params.get("client_version")
    telemetry_schema_version = params.get("telemetry_schema_version")
    if (
        not isinstance(installation_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            installation_id,
            re.IGNORECASE,
        )
        is None
        or not isinstance(client_version, str)
        or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", client_version)
        is None
        or not isinstance(telemetry_schema_version, str)
        or re.fullmatch(r"[1-9][0-9]{0,15}", telemetry_schema_version) is None
    ):
        raise ValueError("invalid trace token fields")
    credential = account_trace_token(
        installation_id=installation_id,
        client_version=client_version,
        telemetry_schema_version=telemetry_schema_version,
    )
    return _validated_trace_result(credential, installation_id=installation_id)


def _trace_ingress_open(params: Mapping[str, object]) -> dict[str, object]:
    entrypoint = params.get("entrypoint")
    consumer_id = params.get("consumer_id")
    if (
        entrypoint not in {"cli", "dashboard", "desktop", "voice"}
        or not isinstance(consumer_id, str)
        or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._:-]{0,127}", consumer_id) is None
    ):
        raise ValueError("invalid trace ingress fields")
    registration = account_trace_ingress_open(
        entrypoint=entrypoint,
        consumer_id=consumer_id,
    )
    return {
        "endpoint": registration.endpoint,
        "authorization": registration.authorization,
        "installation_id": registration.installation_id,
        "entrypoint": registration.entrypoint,
        "plugins_toml": registration.plugins_toml,
    }


METHODS: dict[str, Callable[[Mapping[str, object]], dict[str, object]]] = {
    "status": _status,
    "login": _login,
    "logout": _logout,
    "trace_token": _trace_token,
    "trace_ingress_open": _trace_ingress_open,
}
ALLOWED_PARAMS = {
    "status": frozenset({"installation_id", "client_version"}),
    "login": frozenset({"username", "password", "installation_id", "client_version"}),
    "logout": frozenset(),
    "trace_token": frozenset(
        {"installation_id", "client_version", "telemetry_schema_version"}
    ),
    "trace_ingress_open": frozenset({"entrypoint", "consumer_id"}),
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
        if method in {"status", "login"}:
            _native_context(params)
        result = METHODS[method](params)
    except AuthRequired as error:
        reason = error.reason
        if reason in _INTERNAL_RESPONSE_REASONS:
            # A malformed authentication response is a service anomaly, not a
            # dead local runtime; reporting it as runtime_unavailable would
            # send the desktop into pointless bridge recovery.
            reason = "invalid_response"
        if reason not in _SAFE_REASONS:
            reason = "runtime_unavailable"
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
    account_id = value.get("account_id")
    session_id = value.get("session_id")
    installation_id = value.get("installation_id")
    runtime_instance_id = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    valid_until = value.get("valid_until")
    cloud_state = value.get("cloud_state")
    validation_state = value.get("validation_state")
    validation_reason = value.get("validation_reason")
    last_validated_at = value.get("last_validated_at")
    legacy = value.get("legacy")
    reason = value.get("reason")
    principal_key = value.get("principal_key")
    predecessor_principal_key = value.get("predecessor_principal_key")
    if state not in _PUBLIC_STATES:
        raise RuntimeError("invalid public result")
    if username is not None and (
        not isinstance(username, str) or not username or len(username) > 150
    ):
        raise RuntimeError("invalid public result")
    if any(
        item is not None and (not isinstance(item, str) or not item or len(item) > 256)
        for item in (
            account_id,
            session_id,
            installation_id,
            principal_key,
            predecessor_principal_key,
        )
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
    if validation_state not in _VALIDATION_STATES:
        raise RuntimeError("invalid public result")
    if cloud_state is not None and cloud_state not in _CLOUD_STATES:
        raise RuntimeError("invalid public result")
    if (state == "authenticated") != (cloud_state is not None):
        raise RuntimeError("invalid public result")
    if validation_state == "online" and cloud_state != "active":
        raise RuntimeError("invalid public result")
    if validation_reason is not None and validation_reason not in _VALIDATION_REASONS:
        raise RuntimeError("invalid public result")
    if last_validated_at is not None and (
        not isinstance(last_validated_at, str) or not last_validated_at or len(last_validated_at) > 128
    ):
        raise RuntimeError("invalid public result")
    if not isinstance(legacy, bool):
        raise RuntimeError("invalid public result")
    if reason is not None and reason not in _SAFE_REASONS:
        raise RuntimeError("invalid public result")
    if state == "locked" and reason not in _TERMINAL_REASONS:
        # A non-terminal lock (rate limit, vault or server outage) is a
        # legitimate transient state: surface it as AUTH_REQUIRED with its
        # safe reason instead of an INTERNAL_ERROR.
        raise AuthRequired(reason if reason in _SAFE_REASONS else "runtime_unavailable")
    if not _has_consistent_public_identity(
        state=state,
        account_id=account_id,
        session_id=session_id,
        installation_id=installation_id,
        principal_key=principal_key,
        legacy=legacy,
    ):
        raise RuntimeError("invalid public result")
    if predecessor_principal_key is not None and (
        legacy is not False
        or re.fullmatch(r"legacy:[0-9a-f]{64}", predecessor_principal_key) is None
    ):
        raise RuntimeError("invalid public result")
    result = dict(value)
    if state == "authenticated" and valid_until != DURABLE_AUTHORIZATION_VALID_UNTIL:
        # Runtime leases use a process-local monotonic clock. Convert the
        # remaining duration to a Unix timestamp before returning it to a
        # Desktop process (including one reached over SSH), whose monotonic
        # clock has a different origin. Durable native and legacy principals
        # already carry the finite Unix sentinel used across process boundaries.
        remaining = max(0.0, float(valid_until) - time.monotonic())
        result["valid_until"] = time.time() + remaining
    return result


def _has_consistent_public_identity(
    *,
    state: object,
    account_id: object,
    session_id: object,
    installation_id: object,
    principal_key: object,
    legacy: object,
) -> bool:
    if all(value is None for value in (account_id, session_id, installation_id, principal_key)):
        return state != "authenticated" and legacy is False
    if legacy is True:
        return (
            account_id is None
            and session_id is None
            and installation_id is None
            and isinstance(principal_key, str)
            and re.fullmatch(r"legacy:[0-9a-f]{64}", principal_key) is not None
        )
    uuid4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    return (
        isinstance(account_id, str)
        and re.fullmatch(uuid4, account_id) is not None
        and isinstance(session_id, str)
        and re.fullmatch(uuid4, session_id) is not None
        and isinstance(installation_id, str)
        and re.fullmatch(uuid4, installation_id) is not None
        and principal_key == f"account:{account_id}"
    )


def _native_context(params: Mapping[str, object]) -> dict[str, str]:
    installation_id = params.get("installation_id")
    client_version = params.get("client_version")
    if (
        not isinstance(installation_id, str)
        or re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            installation_id,
            re.IGNORECASE,
        )
        is None
        or not isinstance(client_version, str)
        or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", client_version)
        is None
    ):
        raise ValueError("invalid native client context")
    return {"installation_id": installation_id, "client_version": client_version}


def _bridge_public_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("invalid public result")
    result = {key: value.get(key) for key in _PUBLIC_KEYS}
    if result["reason"] in _INTERNAL_RESPONSE_REASONS:
        result["reason"] = "invalid_response"
    if result["validation_reason"] in _INTERNAL_RESPONSE_REASONS:
        result["validation_reason"] = "invalid_response"
    return result


def _validated_trace_result(
    credential: object,
    *,
    installation_id: str,
) -> dict[str, object]:
    if not isinstance(credential, TraceCredential):
        raise RuntimeError("invalid trace credential")
    if (
        credential.installation_id != installation_id
        or not 20 <= len(credential.access_token) <= 4096
        or any(character in credential.access_token for character in "\r\n")
        or not 1 <= credential.expires_in <= 900
        or len(credential.expires_at) > 128
    ):
        raise RuntimeError("invalid trace credential")
    try:
        expiry = datetime.fromisoformat(credential.expires_at)
    except ValueError:
        raise RuntimeError("invalid trace credential") from None
    if (
        expiry.tzinfo is None
        or expiry.utcoffset() is None
        or expiry <= datetime.now(tz=expiry.tzinfo)
    ):
        raise RuntimeError("invalid trace credential")
    return {
        "access_token": credential.access_token,
        "expires_at": credential.expires_at,
        "expires_in": credential.expires_in,
        "installation_id": credential.installation_id,
    }


def main() -> int:
    try:
        owner = connect_runtime_owner(timeout=2.0)
    except AuthRequired:
        owner = start_runtime_owner(timeout=4.0, probe_first=False)
    try:
        owner.enable_desktop_local_continuity()
    except AuthRequired:
        owner = start_runtime_owner(timeout=4.0, probe_first=False)
        owner.enable_desktop_local_continuity()
    install_entrypoint_owner(owner)
    try:
        run_stream(sys.stdin.buffer, sys.stdout.buffer)
    finally:
        clear_entrypoint_owner()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
