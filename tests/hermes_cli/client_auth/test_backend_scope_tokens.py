import base64
import json
from dataclasses import replace

import pytest

from hermes_cli.client_auth.backend_scope_protocol import (
    BACKEND_SCOPE_CONTROL_FRAME_LIMIT,
    BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
    CONTROL_ACK_PREFIX,
    DESKTOP_SCOPE_TOKEN_TTL_SECONDS,
    DESKTOP_SCOPE_PROTOCOL_VERSION,
    ScopeTokenPromotion,
    ScopeTokenRegistration,
    encode_control_ack,
    parse_control_frame,
)
from hermes_cli.client_auth.runtime import (
    AuthScope,
    BackendScopeGrantState,
    BackendScopeTokenRegistry,
    BackendScopeTokenRejected,
)


RUNTIME_INSTANCE_ID = "0123456789abcdef0123456789abcdef"
AUTH_SCOPE = AuthScope(RUNTIME_INSTANCE_ID, 7)


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


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


def _registration(
    *,
    registration_byte: bytes,
    bearer_byte: bytes,
    ttl_seconds: float = 1_800,
) -> ScopeTokenRegistration:
    return ScopeTokenRegistration(
        registration_id=_base64url(16, registration_byte),
        bearer=_base64url(32, bearer_byte),
        connection_id="local",
        runtime_instance_id=RUNTIME_INSTANCE_ID,
        epoch=AUTH_SCOPE.epoch,
        ttl_seconds=ttl_seconds,
    )


def _promotion(
    registration: ScopeTokenRegistration,
    *,
    transition_byte: bytes,
    previous_registration_id: str | None,
    overlap_seconds: float = 60,
) -> ScopeTokenPromotion:
    return ScopeTokenPromotion(
        transition_id=_base64url(16, transition_byte),
        registration_id=registration.registration_id,
        previous_registration_id=previous_registration_id,
        connection_id=registration.connection_id,
        runtime_instance_id=registration.runtime_instance_id,
        epoch=registration.epoch,
        overlap_seconds=overlap_seconds,
    )


def _registry_fixture() -> tuple[BackendScopeTokenRegistry, _Clock]:
    clock = _Clock()

    def authorize(boundary: str, *, expected: AuthScope) -> AuthScope:
        assert boundary
        assert expected == AUTH_SCOPE
        return expected

    return BackendScopeTokenRegistry(clock=clock, authorize=authorize), clock


def _register_and_promote(
    registry: BackendScopeTokenRegistry,
    registration: ScopeTokenRegistration,
    *,
    transition_byte: bytes,
    previous_registration_id: str | None,
):
    registry.register_candidate(registration, expected=AUTH_SCOPE)
    return registry.promote(
        _promotion(
            registration,
            transition_byte=transition_byte,
            previous_registration_id=previous_registration_id,
        ),
        expected=AUTH_SCOPE,
    )


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
    assert DESKTOP_SCOPE_TOKEN_TTL_SECONDS == 1_800


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
        {**_registration_frame(), "epoch": 2**53},
        {**_registration_frame(), "ttl_seconds": 1_801},
        {**_promotion_frame(), "transition_id": _base64url(15, b"T")},
        {**_promotion_frame(), "previous_registration_id": "not-base64url"},
        {**_promotion_frame(), "overlap_seconds": 61},
    ],
)
def test_v2_control_contract_rejects_schema_drift_and_out_of_bounds_values(frame):
    with pytest.raises(ValueError):
        parse_control_frame(frame)


def test_candidate_cannot_authorize_business_until_promoted():
    registry, _clock = _registry_fixture()
    registration = _registration(registration_byte=b"A", bearer_byte=b"a")

    candidate = registry.register_candidate(registration, expected=AUTH_SCOPE)

    assert candidate.state is BackendScopeGrantState.CANDIDATE
    assert all(
        not hasattr(record, "bearer")
        for record in registry._registration_records.values()
    )
    assert registry.probe(registration.bearer).registration_id == candidate.registration_id
    with pytest.raises(BackendScopeTokenRejected, match="candidate_not_active"):
        registry.authorize(registration.bearer, "dashboard.api.request")


def test_probe_is_read_only_and_reports_the_completed_promotion_transition():
    registry, _clock = _registry_fixture()
    registration = _registration(registration_byte=b"A", bearer_byte=b"a")
    promotion = _promotion(
        registration,
        transition_byte=b"1",
        previous_registration_id=None,
    )
    candidate = registry.register_candidate(registration, expected=AUTH_SCOPE)
    candidate_snapshot = dict(registry._registrations)
    candidate_records = dict(registry._records)

    assert registry.probe(registration.bearer) == candidate
    assert registry.probe(registration.bearer) == candidate
    assert registry._registrations == candidate_snapshot
    assert registry._records == candidate_records

    active = registry.promote(promotion, expected=AUTH_SCOPE)
    active_snapshot = dict(registry._registrations)
    active_records = dict(registry._records)

    assert registry.probe(registration.bearer) == active
    assert active.state is BackendScopeGrantState.ACTIVE
    assert active.promoted_transition_id == promotion.transition_id
    assert registry._registrations == active_snapshot
    assert registry._records == active_records
    assert registration.bearer not in repr(active)


def test_candidate_registration_is_idempotent_without_retaining_the_bearer():
    registry, _clock = _registry_fixture()
    first = _registration(registration_byte=b"A", bearer_byte=b"a")

    original = registry.register_candidate(first, expected=AUTH_SCOPE)
    duplicate = registry.register_candidate(first, expected=AUTH_SCOPE)

    assert duplicate == original
    assert first.bearer not in repr(registry.__dict__)
    with pytest.raises(BackendScopeTokenRejected, match="registration_conflict"):
        registry.register_candidate(
            replace(first, bearer=_base64url(32, b"b")),
            expected=AUTH_SCOPE,
        )


def test_candidate_registration_requires_the_authorizer_to_return_the_exact_scope():
    registration = _registration(registration_byte=b"A", bearer_byte=b"a")
    registry = BackendScopeTokenRegistry(
        authorize=lambda _boundary, *, expected: AuthScope(
            expected.runtime_instance_id,
            expected.epoch + 1,
        )
    )

    with pytest.raises(BackendScopeTokenRejected, match="scope_not_authorized"):
        registry.register_candidate(registration, expected=AUTH_SCOPE)


def test_promote_atomically_activates_candidate_and_bounds_old_overlap():
    registry, clock = _registry_fixture()
    first_registration = _registration(registration_byte=b"A", bearer_byte=b"a")
    first = _register_and_promote(
        registry,
        first_registration,
        transition_byte=b"1",
        previous_registration_id=None,
    )
    second_registration = _registration(registration_byte=b"B", bearer_byte=b"b")
    registry.register_candidate(second_registration, expected=AUTH_SCOPE)

    second = registry.promote(
        _promotion(
            second_registration,
            transition_byte=b"2",
            previous_registration_id=first.registration_id,
        ),
        expected=AUTH_SCOPE,
    )

    assert second.state is BackendScopeGrantState.ACTIVE
    assert registry.authorize(
        second_registration.bearer,
        "dashboard.api.request",
    ).state is BackendScopeGrantState.ACTIVE
    assert registry.authorize(
        first_registration.bearer,
        "dashboard.api.request",
    ).state is BackendScopeGrantState.OVERLAP
    clock.now += BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS
    with pytest.raises(BackendScopeTokenRejected, match="expired"):
        registry.authorize(first_registration.bearer, "dashboard.api.request")


def test_duplicate_transition_is_idempotent_but_conflicting_reuse_is_rejected():
    registry, _clock = _registry_fixture()
    registration = _registration(registration_byte=b"A", bearer_byte=b"a")
    promotion = _promotion(
        registration,
        transition_byte=b"1",
        previous_registration_id=None,
    )
    registry.register_candidate(registration, expected=AUTH_SCOPE)
    first = registry.promote(promotion, expected=AUTH_SCOPE)

    duplicate = registry.promote(promotion, expected=AUTH_SCOPE)

    assert duplicate.registration_id == first.registration_id
    with pytest.raises(BackendScopeTokenRejected, match="transition_conflict"):
        registry.promote(
            replace(promotion, overlap_seconds=30),
            expected=AUTH_SCOPE,
        )


def test_clear_rotates_backend_generation_and_invalidates_ws_claims():
    registry, clock = _registry_fixture()
    registration = _registration(registration_byte=b"A", bearer_byte=b"a")
    active = _register_and_promote(
        registry,
        registration,
        transition_byte=b"1",
        previous_registration_id=None,
    )
    claim = registry.ws_claim(active)

    with pytest.raises(BackendScopeTokenRejected, match="invalid_ws_claim"):
        registry.authorize_ws_claim(
            {**claim, "connection_id": "other-local-connection"},
            "dashboard.ws.message",
        )

    clock.now = active.valid_until
    assert registry.authorize_ws_claim(claim, "dashboard.ws.message") == AUTH_SCOPE

    registry.clear()

    with pytest.raises(BackendScopeTokenRejected, match="backend_generation_changed"):
        registry.authorize_ws_claim(claim, "dashboard.ws.message")


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


@pytest.mark.parametrize(
    "payload_update",
    [
        {"registration_id": _base64url(32, b"B")},
        {"connection_id": "local\nlog-injection"},
        {"runtime_instance_id": RUNTIME_INSTANCE_ID.upper()},
        {"epoch": 2**53},
        {"ttl_seconds": 1_801},
    ],
)
def test_control_ack_validates_every_field_instead_of_only_secret_key_names(
    payload_update,
):
    payload = {
        "version": 2,
        "operation": "scope_token_registered",
        "registration_id": _base64url(16, b"R"),
        "connection_id": "local",
        "runtime_instance_id": RUNTIME_INSTANCE_ID,
        "epoch": 7,
        "ttl_seconds": 1_800,
    }

    with pytest.raises(ValueError):
        encode_control_ack({**payload, **payload_update})


def test_registration_dataclass_requires_the_complete_scope():
    with pytest.raises(TypeError):
        ScopeTokenRegistration(
            registration_id=_base64url(16, b"R"),
            bearer=_base64url(32, b"B"),
        )
