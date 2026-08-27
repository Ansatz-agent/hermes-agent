import base64
import json

import pytest

from hermes_cli.client_auth.backend_scope_protocol import (
    BACKEND_SCOPE_CONTROL_FRAME_LIMIT,
    BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
    BACKEND_SCOPE_TOKEN_TTL_SECONDS,
    CONTROL_ACK_PREFIX,
    DESKTOP_SCOPE_PROTOCOL_VERSION,
    ScopeTokenPromotion,
    ScopeTokenRegistration,
    encode_control_ack,
    parse_control_frame,
)


RUNTIME_INSTANCE_ID = "0123456789abcdef0123456789abcdef"


def _base64url(size: int, byte: bytes) -> str:
    return base64.urlsafe_b64encode(byte * size).decode("ascii").rstrip("=")


def _registration_frame() -> dict[str, object]:
    return {
        "version": 2,
        "operation": "register_scope_token",
        "registration_id": _base64url(16, b"R"),
        "bearer": _base64url(32, b"B"),
        "connection_id": "local",
        "runtime_instance_id": RUNTIME_INSTANCE_ID,
        "epoch": 7,
        "ttl_seconds": 1_800,
    }


def _promotion_frame() -> dict[str, object]:
    return {
        "version": 2,
        "operation": "promote_scope_token",
        "transition_id": _base64url(16, b"T"),
        "registration_id": _base64url(16, b"R"),
        "previous_registration_id": _base64url(16, b"P"),
        "connection_id": "local",
        "runtime_instance_id": RUNTIME_INSTANCE_ID,
        "epoch": 7,
        "overlap_seconds": 60,
    }


def test_v2_registration_contract_is_strict_and_keeps_bearer_out_of_repr():
    frame = _registration_frame()

    parsed = parse_control_frame(frame)

    assert parsed == ScopeTokenRegistration(
        registration_id=frame["registration_id"],
        bearer=frame["bearer"],
        connection_id="local",
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        epoch=7,
        ttl_seconds=1_800.0,
    )
    assert frame["bearer"] not in repr(parsed)
    assert DESKTOP_SCOPE_PROTOCOL_VERSION == 2
    assert BACKEND_SCOPE_TOKEN_TTL_SECONDS == 1_800


def test_v2_promotion_contract_accepts_a_null_or_exact_previous_registration():
    frame = _promotion_frame()

    parsed = parse_control_frame(frame)

    assert parsed == ScopeTokenPromotion(
        transition_id=frame["transition_id"],
        registration_id=frame["registration_id"],
        previous_registration_id=frame["previous_registration_id"],
        connection_id="local",
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        epoch=7,
        overlap_seconds=60.0,
    )
    assert parse_control_frame({**frame, "previous_registration_id": None}).previous_registration_id is None
    assert BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS == 60


@pytest.mark.parametrize(
    "frame",
    [
        {**_registration_frame(), "unknown": True},
        {**_registration_frame(), "version": 1},
        {**_registration_frame(), "operation": "unknown"},
        {**_registration_frame(), "registration_id": _base64url(15, b"R")},
        {**_registration_frame(), "bearer": _base64url(31, b"B")},
        {**_registration_frame(), "connection_id": "local\nlog-injection"},
        {**_registration_frame(), "runtime_instance_id": RUNTIME_INSTANCE_ID.upper()},
        {**_registration_frame(), "epoch": True},
        {**_registration_frame(), "ttl_seconds": 1_801},
        {**_promotion_frame(), "transition_id": _base64url(15, b"T")},
        {**_promotion_frame(), "previous_registration_id": "not-base64url"},
        {**_promotion_frame(), "overlap_seconds": 61},
    ],
)
def test_v2_control_contract_rejects_schema_drift_and_out_of_bounds_values(frame):
    with pytest.raises(ValueError):
        parse_control_frame(frame)


def test_control_ack_is_bounded_ascii_and_cannot_contain_replayable_secrets():
    payload = {
        "version": 2,
        "operation": "scope_token_registered",
        "registration_id": _base64url(16, b"R"),
        "connection_id": "local",
        "runtime_instance_id": RUNTIME_INSTANCE_ID,
        "epoch": 7,
        "ttl_seconds": 1_800,
    }

    encoded = encode_control_ack(payload)

    assert encoded.startswith(CONTROL_ACK_PREFIX.encode("ascii"))
    assert encoded.endswith(b"\n")
    assert len(encoded) <= BACKEND_SCOPE_CONTROL_FRAME_LIMIT
    assert json.loads(encoded.removeprefix(CONTROL_ACK_PREFIX.encode("ascii"))) == payload
    assert b"bearer" not in encoded
    assert b"token_digest" not in encoded

    with pytest.raises(ValueError, match="secret"):
        encode_control_ack({**payload, "bearer": _base64url(32, b"B")})
    with pytest.raises(ValueError, match="secret"):
        encode_control_ack({**payload, "token_digest": "a" * 64})
    with pytest.raises(ValueError, match="large"):
        encode_control_ack(
            {**payload, "connection_id": "x" * BACKEND_SCOPE_CONTROL_FRAME_LIMIT}
        )
