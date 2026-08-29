from __future__ import annotations

import base64
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field


DESKTOP_SCOPE_PROTOCOL_VERSION = 2
DESKTOP_SCOPE_TOKEN_TTL_SECONDS = 1_800
BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS = 60
BACKEND_SCOPE_CONTROL_FRAME_LIMIT = 4_096
CONTROL_ACK_PREFIX = "ANSATZ_SCOPE_CONTROL_V2 "

_BEARER_BYTES = 32
_CONTROL_ID_BYTES = 16
_REGISTER_KEYS = frozenset(
    {
    "version",
    "operation",
    "registration_id",
    "bearer",
    "connection_id",
    "runtime_instance_id",
    "epoch",
    "ttl_seconds",
    }
)
_PROMOTE_KEYS = frozenset(
    {
    "version",
    "operation",
    "transition_id",
    "registration_id",
    "previous_registration_id",
    "connection_id",
    "runtime_instance_id",
    "epoch",
    "overlap_seconds",
    }
)
_REGISTERED_ACK_KEYS = _REGISTER_KEYS - {"bearer"}
_PROMOTED_ACK_KEYS = _PROMOTE_KEYS


@dataclass(frozen=True)
class ScopeTokenRegistration:
    registration_id: str
    bearer: str = field(repr=False)
    connection_id: str
    runtime_instance_id: str
    epoch: int
    ttl_seconds: float


@dataclass(frozen=True)
class ScopeTokenPromotion:
    transition_id: str
    registration_id: str
    previous_registration_id: str | None
    connection_id: str
    runtime_instance_id: str
    epoch: int
    overlap_seconds: float


ScopeControlFrame = ScopeTokenRegistration | ScopeTokenPromotion


def parse_control_frame(value: object) -> ScopeControlFrame:
    if not isinstance(value, dict):
        raise ValueError("scope control frame must be an object")
    if value.get("version") != DESKTOP_SCOPE_PROTOCOL_VERSION:
        raise ValueError("unsupported scope control protocol")

    operation = value.get("operation")
    if operation == "register_scope_token":
        return _parse_registration(value)
    if operation == "promote_scope_token":
        return _parse_promotion(value)
    raise ValueError("unknown scope control operation")


def encode_control_ack(payload: Mapping[str, object]) -> bytes:
    if "bearer" in payload or "token_digest" in payload:
        raise ValueError("control ack contains a secret")

    operation = payload.get("operation")
    expected_keys = (
        _REGISTERED_ACK_KEYS
        if operation == "scope_token_registered"
        else _PROMOTED_ACK_KEYS
        if operation == "scope_token_promoted"
        else None
    )
    if expected_keys is None or set(payload) != expected_keys:
        raise ValueError("invalid control ack schema")
    if payload.get("version") != DESKTOP_SCOPE_PROTOCOL_VERSION:
        raise ValueError("invalid control ack protocol")

    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    encoded = f"{CONTROL_ACK_PREFIX}{body}\n".encode("ascii")
    if len(encoded) > BACKEND_SCOPE_CONTROL_FRAME_LIMIT:
        raise ValueError("control ack is too large")

    if operation == "scope_token_registered":
        _control_id(payload.get("registration_id"), "registration_id")
        _connection_id(payload.get("connection_id"))
        _runtime_instance_id(payload.get("runtime_instance_id"))
        _epoch(payload.get("epoch"))
        _bounded_seconds(
            payload.get("ttl_seconds"),
            maximum=DESKTOP_SCOPE_TOKEN_TTL_SECONDS,
            field_name="ttl_seconds",
        )
    else:
        _control_id(payload.get("transition_id"), "transition_id")
        _control_id(payload.get("registration_id"), "registration_id")
        previous = payload.get("previous_registration_id")
        if previous is not None:
            _control_id(previous, "previous_registration_id")
        _connection_id(payload.get("connection_id"))
        _runtime_instance_id(payload.get("runtime_instance_id"))
        _epoch(payload.get("epoch"))
        _bounded_seconds(
            payload.get("overlap_seconds"),
            maximum=BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
            field_name="overlap_seconds",
        )

    return encoded


def _parse_registration(value: dict[object, object]) -> ScopeTokenRegistration:
    if set(value) != _REGISTER_KEYS:
        raise ValueError("invalid scope token registration schema")
    registration_id = _control_id(value.get("registration_id"), "registration_id")
    bearer = _base64url(value.get("bearer"), _BEARER_BYTES, "bearer")
    connection_id = _connection_id(value.get("connection_id"))
    runtime_instance_id = _runtime_instance_id(value.get("runtime_instance_id"))
    epoch = _epoch(value.get("epoch"))
    ttl_seconds = _bounded_seconds(
        value.get("ttl_seconds"),
        maximum=DESKTOP_SCOPE_TOKEN_TTL_SECONDS,
        field_name="ttl_seconds",
    )
    return ScopeTokenRegistration(
        registration_id=registration_id,
        bearer=bearer,
        connection_id=connection_id,
        runtime_instance_id=runtime_instance_id,
        epoch=epoch,
        ttl_seconds=ttl_seconds,
    )


def _parse_promotion(value: dict[object, object]) -> ScopeTokenPromotion:
    if set(value) != _PROMOTE_KEYS:
        raise ValueError("invalid scope token promotion schema")
    previous = value.get("previous_registration_id")
    previous_registration_id = (
        None if previous is None else _control_id(previous, "previous_registration_id")
    )
    return ScopeTokenPromotion(
        transition_id=_control_id(value.get("transition_id"), "transition_id"),
        registration_id=_control_id(value.get("registration_id"), "registration_id"),
        previous_registration_id=previous_registration_id,
        connection_id=_connection_id(value.get("connection_id")),
        runtime_instance_id=_runtime_instance_id(value.get("runtime_instance_id")),
        epoch=_epoch(value.get("epoch")),
        overlap_seconds=_bounded_seconds(
            value.get("overlap_seconds"),
            maximum=BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
            field_name="overlap_seconds",
        ),
    )


def _control_id(value: object, field_name: str) -> str:
    return _base64url(value, _CONTROL_ID_BYTES, field_name)


def _base64url(value: object, size: int, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {field_name}")
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError):
        raise ValueError(f"invalid {field_name}") from None
    if len(decoded) != size or base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError(f"invalid {field_name}")
    return value


def _connection_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError("invalid connection_id")
    return value


def _runtime_instance_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("invalid runtime_instance_id")
    return value


def _epoch(value: object) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 <= value <= 2**53 - 1
    ):
        raise ValueError("invalid epoch")
    return value


def _bounded_seconds(value: object, *, maximum: float, field_name: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or not 0 < float(value) <= maximum
    ):
        raise ValueError(f"invalid {field_name}")
    return float(value)
