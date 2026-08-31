from __future__ import annotations

import base64
import errno
import hashlib
import json
import math
import ntpath
import os
import re
import secrets
import select
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import urlsplit

from hermes_cli.cli_identity import CANONICAL_COMMAND

from hermes_cli.client_auth.client import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    AuthClient,
    AuthServiceError,
    CookieRecord,
    ExplicitSessionRevocation,
    NativeSessionCredential,
    SessionRejected,
    SessionStatus,
    TraceCredential,
)
from hermes_cli.client_auth.backend_scope_protocol import (
    BACKEND_SCOPE_CONTROL_FRAME_LIMIT,
    BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
    DESKTOP_SCOPE_PROTOCOL_VERSION,
    DESKTOP_SCOPE_TOKEN_TTL_SECONDS,
    ScopeTokenPromotion,
    ScopeTokenRegistration,
    encode_control_ack,
    parse_control_frame,
)

AUTH_EXIT_CODE = 20
LEASE_SECONDS = 60.0
# Unix timestamp for 9999-12-31T23:59:59Z. Durable native and migrated legacy
# credentials remain authorized until explicit revocation, but their public
# snapshots must stay JSON/JavaScript-safe and cannot carry infinity.
DURABLE_AUTHORIZATION_VALID_UNTIL = 253_402_300_799.0
LOGIN_ATTEMPT_LIMIT = 5
LOGIN_ATTEMPT_WINDOW_SECONDS = 60.0
OWNER_IDLE_SECONDS = 15.0 * 60.0
_LEGACY_BACKEND_SCOPE_TOKEN_TTL_SECONDS = 60.0
_BACKEND_SCOPE_TOKEN_BYTES = 32
_RUNTIME_REQUEST_TIMEOUT_SECONDS = 15.0
_RUNTIME_LOGIN_TIMEOUT_SECONDS = 70.0
_RUNTIME_RECOVERY_PROBE_SECONDS = 2.0
_RUNTIME_RECOVERY_START_SECONDS = 4.0
_RUNTIME_SERVER_READ_TIMEOUT_SECONDS = 5.0
_RUNTIME_LOGIN_OPERATION_WAIT_SECONDS = 15.0
_RUNTIME_LOGOUT_OPERATION_WAIT_SECONDS = 3.0
_AUTH_RUNTIME_NAMESPACE_ENV = "HERMES_AUTH_RUNTIME_NAMESPACE"
_AUTH_RUNTIME_NAMESPACE_PATTERN = re.compile(
    r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?",
    flags=re.ASCII,
)
_AUTH_KEYRING_SERVICE_ENV = "HERMES_AUTH_KEYRING_SERVICE"
_AUTH_LEGACY_KEYRING_SERVICE_ENV = "HERMES_AUTH_LEGACY_KEYRING_SERVICE"
_AUTH_KEYRING_SERVICE_PATTERN = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,126}[A-Za-z0-9])?",
    flags=re.ASCII,
)
_DEFAULT_AUTH_KEYRING_SERVICE = "cn.c2sml.hermes.remote-auth"
_EXPLICIT_TERMINAL_REASONS = frozenset(
    {"account_disabled", "account_revoked", "session_revoked"}
)
_EXTERNAL_AUTH_ENV = "ANSATZ_EXTERNAL_AUTH"
_EXTERNAL_AUTH_INSTANCE_ENV = "ANSATZ_EXTERNAL_AUTH_RUNTIME_INSTANCE_ID"
_EXTERNAL_AUTH_EPOCH_ENV = "ANSATZ_EXTERNAL_AUTH_EPOCH"
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _test_runtime_suffix() -> str:
    """Return a compact broker namespace only under the test isolation marker."""
    marker = os.environ.get("HERMES_TEST_ISOLATION", "")
    if not marker:
        return ""
    digest = hashlib.blake2s(marker.encode("utf-8"), digest_size=4).hexdigest()
    return f"-t{digest}"


def _validate_auth_runtime_namespace(value: str) -> str:
    if _AUTH_RUNTIME_NAMESPACE_PATTERN.fullmatch(value) is None:
        raise AuthRequired("runtime_unavailable")
    return value


def _auth_runtime_namespace() -> str | None:
    value = os.environ.get(_AUTH_RUNTIME_NAMESPACE_ENV)
    if value is None:
        return None
    return _validate_auth_runtime_namespace(value)


def _auth_runtime_namespace_suffix(namespace: str | None) -> str:
    if namespace is None:
        return ""
    validated = _validate_auth_runtime_namespace(namespace)
    digest = hashlib.blake2s(validated.encode("ascii"), digest_size=6).hexdigest()
    return f"-p{digest}"


def _validate_auth_keyring_service(value: str) -> str:
    if _AUTH_KEYRING_SERVICE_PATTERN.fullmatch(value) is None:
        raise AuthRequired("runtime_unavailable")
    return value


def _auth_keyring_services() -> tuple[str, str | None]:
    service = os.environ.get(_AUTH_KEYRING_SERVICE_ENV)
    legacy_service = os.environ.get(_AUTH_LEGACY_KEYRING_SERVICE_ENV)
    if service is None:
        if legacy_service is not None:
            raise AuthRequired("runtime_unavailable")
        return _DEFAULT_AUTH_KEYRING_SERVICE, None
    validated_service = _validate_auth_keyring_service(service)
    validated_legacy = (
        _validate_auth_keyring_service(legacy_service)
        if legacy_service is not None
        else None
    )
    if validated_legacy == validated_service:
        raise AuthRequired("runtime_unavailable")
    return validated_service, validated_legacy


class AuthState(StrEnum):
    CHECKING = "checking"
    AUTHENTICATED = "authenticated"
    SIGNED_OUT = "signed_out"
    LOCKED = "locked"


class ValidationState(StrEnum):
    """Online validation health, deliberately independent of local access."""

    UNKNOWN = "unknown"
    VALIDATING = "validating"
    ONLINE = "online"
    DEGRADED = "degraded"


class CloudState(StrEnum):
    """Cloud reachability without weakening the local account identity."""

    ACTIVE = "active"
    UNREACHABLE = "unreachable"
    REAUTH_REQUIRED = "reauth_required"


class LockedWaitingResult(StrEnum):
    AUTHENTICATED = "authenticated"
    OWNER_STOPPED = "owner_stopped"


@dataclass(frozen=True)
class NativeCredentialRecord:
    credential: NativeSessionCredential
    last_validated_at: str
    predecessor_principal_key: str | None = None


@dataclass(frozen=True)
class LegacyCredentialRecord:
    cookie_record: CookieRecord
    principal_key: str


@dataclass(frozen=True)
class RevocationTombstone:
    account_id: str
    session_id: str
    reason: str
    revoked_at: str


@dataclass(frozen=True)
class SignedOutTombstone:
    reason: str = "signed_out"


@dataclass(frozen=True)
class AuthScope:
    runtime_instance_id: str
    epoch: int


@dataclass(frozen=True)
class ConnectionScope:
    connection_id: str
    auth: AuthScope


class AuthRequired(RuntimeError):
    code = "AUTH_REQUIRED"

    def __init__(self, reason: str | None = None) -> None:
        normalized = reason or self.code
        super().__init__(normalized)
        self.reason = reason


def external_auth_enabled() -> bool:
    """Return whether an embedding desktop owns authentication for this process."""

    return os.environ.get(_EXTERNAL_AUTH_ENV, "").strip().lower() in _TRUE_ENV_VALUES


def external_auth_scope() -> AuthScope:
    """Build the desktop-owned scope used by the local backend guard."""

    instance = os.environ.get(_EXTERNAL_AUTH_INSTANCE_ENV, "0" * 32)
    epoch_text = os.environ.get(_EXTERNAL_AUTH_EPOCH_ENV, "0")
    try:
        epoch = int(epoch_text, 10)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    scope = AuthScope(instance, epoch)
    _validate_auth_scope(scope)
    return scope


class AuthScopeChanged(AuthRequired):
    """The caller's exact account scope no longer matches the owner."""


@dataclass(frozen=True)
class BackendScopeTokenRegistration:
    bearer: str
    connection_id: str
    auth: AuthScope
    ttl_seconds: float


@dataclass(frozen=True)
class TraceTransportRegistration:
    endpoint: str
    authorization: str = field(repr=False)
    installation_id: str = ""
    entrypoint: str = "desktop"
    plugins_toml: str = ""


@dataclass(frozen=True)
class BackendScopeWsClaim:
    connection_id: str
    runtime_instance_id: str
    epoch: int
    backend_generation: str


class BackendScopeTokenRejected(AuthRequired):
    code = "local_capability_rejected"

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.failure_phase = "pre_dispatch"


def local_capability_rejection_payload(
    error: BackendScopeTokenRejected,
) -> dict[str, object]:
    reason = "unknown" if error.reason == "unknown_token" else error.reason
    return {
        "detail": "Local capability rejected",
        "code": error.code,
        "reason": reason,
        "failure_phase": error.failure_phase,
        "retryable": True,
    }


def account_locked_payload() -> dict[str, object]:
    return {
        "detail": "Ansatz login required",
        "code": "account_locked",
        "hint": "Run `ansatz login` and retry.",
    }


_RECOVERABLE_LOCAL_AUTH_REASONS = frozenset(
    {
        "invalid_response",
        "rate_limited",
        "runtime_unavailable",
        "server_unavailable",
        "vault_unavailable",
    }
)


def is_local_auth_unavailable(error: AuthRequired) -> bool:
    """Return whether an auth denial is retryable without interactive login."""
    return not isinstance(error, AuthScopeChanged) and (
        isinstance(error, BackendScopeTokenRejected)
        or error.reason in _RECOVERABLE_LOCAL_AUTH_REASONS
    )


_RECOVERABLE_BACKEND_SCOPE_CONTROL_REJECTIONS = frozenset(
    {
        "candidate_not_available",
        "expired",
        "previous_registration_mismatch",
        "registration_conflict",
        "scope_mismatch",
        "transition_conflict",
    }
)


class BackendScopeGrantState(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    OVERLAP = "overlap"


@dataclass(frozen=True)
class BackendScopeGrant:
    registration_id: str
    connection_id: str
    auth: AuthScope
    state: BackendScopeGrantState
    valid_until: float
    token_digest: str
    promoted_transition_id: str | None = None

    def claim(self) -> dict[str, object]:
        """Return the schema-v1 bearer-bound claim for compatibility tests."""
        return {
            "connection_id": self.connection_id,
            "runtime_instance_id": self.auth.runtime_instance_id,
            "epoch": self.auth.epoch,
            "valid_until": self.valid_until,
            "token_digest": self.token_digest,
        }


@dataclass(frozen=True)
class _BackendScopeTransition:
    promotion: ScopeTokenPromotion
    grant: BackendScopeGrant


@dataclass(frozen=True)
class _BackendScopeRegistrationRecord:
    registration_id: str
    connection_id: str
    auth: AuthScope
    ttl_seconds: float
    token_digest: str


class BackendScopeTokenRegistry:
    """Process-local, hashed bearer grants for Desktop backend traffic."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        authorize: Callable[..., AuthScope] | None = None,
    ) -> None:
        self._clock = clock
        self._authorize = authorize or (
            lambda boundary, *, expected: require_authorized(
                boundary,
                expected=expected,
            )
        )
        self._lock = threading.RLock()
        self._records: dict[bytes, BackendScopeGrant] = {}
        self._registrations: dict[str, BackendScopeGrant] = {}
        self._registration_records: dict[
            str, _BackendScopeRegistrationRecord
        ] = {}
        self._transitions: dict[str, _BackendScopeTransition] = {}
        self._generation_scopes: set[ConnectionScope] = set()
        self._active_registration_id: str | None = None
        self._backend_generation = secrets.token_hex(16)

    @property
    def backend_generation(self) -> str:
        with self._lock:
            return self._backend_generation

    def register_candidate(
        self,
        registration: ScopeTokenRegistration,
        *,
        expected: AuthScope,
    ) -> BackendScopeGrant:
        registration = _validated_scope_token_registration(registration)
        scope = AuthScope(registration.runtime_instance_id, registration.epoch)
        if scope != expected:
            raise BackendScopeTokenRejected("scope_mismatch")
        self._require_scope_authorized(
            "backend.scope_token.register",
            expected=expected,
        )
        now = self._clock()
        digest = hashlib.sha256(registration.bearer.encode("ascii")).digest()
        registration_record = _BackendScopeRegistrationRecord(
            registration_id=registration.registration_id,
            connection_id=registration.connection_id,
            auth=scope,
            ttl_seconds=registration.ttl_seconds,
            token_digest=digest.hex(),
        )
        with self._lock:
            self._prune_locked(now)
            existing_record = self._registration_records.get(
                registration.registration_id
            )
            if existing_record is not None:
                if existing_record != registration_record:
                    raise BackendScopeTokenRejected("registration_conflict")
                existing = self._registrations.get(registration.registration_id)
                if existing is None:
                    raise BackendScopeTokenRejected("expired")
                return existing
            if digest in self._records:
                raise BackendScopeTokenRejected("registration_conflict")
            for grant in tuple(self._registrations.values()):
                if (
                    grant.state is BackendScopeGrantState.CANDIDATE
                    and grant.connection_id == registration.connection_id
                    and grant.auth == scope
                ):
                    self._remove_grant_locked(grant)
            grant = BackendScopeGrant(
                registration_id=registration.registration_id,
                connection_id=registration.connection_id,
                auth=scope,
                state=BackendScopeGrantState.CANDIDATE,
                valid_until=now + registration.ttl_seconds,
                token_digest=digest.hex(),
            )
            self._records[digest] = grant
            self._registrations[grant.registration_id] = grant
            self._registration_records[grant.registration_id] = registration_record
            self._generation_scopes.add(
                ConnectionScope(registration.connection_id, scope)
            )
            return grant

    def promote(
        self,
        promotion: ScopeTokenPromotion,
        *,
        expected: AuthScope,
    ) -> BackendScopeGrant:
        promotion = _validated_scope_token_promotion(promotion)
        scope = AuthScope(promotion.runtime_instance_id, promotion.epoch)
        if scope != expected:
            raise BackendScopeTokenRejected("scope_mismatch")
        self._require_scope_authorized(
            "backend.scope_token.promote",
            expected=expected,
        )
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            completed = self._transitions.get(promotion.transition_id)
            if completed is not None:
                if completed.promotion != promotion:
                    raise BackendScopeTokenRejected("transition_conflict")
                return completed.grant

            candidate = self._registrations.get(promotion.registration_id)
            if candidate is None or candidate.state is not BackendScopeGrantState.CANDIDATE:
                raise BackendScopeTokenRejected("candidate_not_available")
            if (
                candidate.connection_id != promotion.connection_id
                or candidate.auth != scope
            ):
                raise BackendScopeTokenRejected("scope_mismatch")
            if promotion.previous_registration_id != self._active_registration_id:
                raise BackendScopeTokenRejected("previous_registration_mismatch")

            previous: BackendScopeGrant | None = None
            if promotion.previous_registration_id is not None:
                previous = self._registrations.get(
                    promotion.previous_registration_id
                )
                if (
                    previous is None
                    or previous.state is not BackendScopeGrantState.ACTIVE
                    or previous.connection_id != promotion.connection_id
                    or previous.auth != scope
                ):
                    raise BackendScopeTokenRejected("previous_registration_mismatch")

            if previous is not None:
                overlap = replace(
                    previous,
                    state=BackendScopeGrantState.OVERLAP,
                    valid_until=min(
                        previous.valid_until,
                        now + promotion.overlap_seconds,
                    ),
                )
                self._store_grant_locked(overlap)

            active = replace(
                candidate,
                state=BackendScopeGrantState.ACTIVE,
                promoted_transition_id=promotion.transition_id,
            )
            self._store_grant_locked(active)
            self._active_registration_id = active.registration_id
            self._transitions[promotion.transition_id] = _BackendScopeTransition(
                promotion=promotion,
                grant=active,
            )
            return active

    def probe(self, bearer: str) -> BackendScopeGrant:
        grant = self._lookup_bearer(bearer)
        self._require_scope_authorized(
            "backend.scope_token.probe",
            expected=grant.auth,
        )
        return grant

    def register(
        self,
        bearer: str,
        *,
        connection_id: str,
        expected: AuthScope,
        ttl_seconds: float,
    ) -> BackendScopeGrant:
        """Compatibility helper for tests and callers migrated in later tasks."""
        registration = ScopeTokenRegistration(
            registration_id=secrets.token_urlsafe(16),
            bearer=bearer,
            connection_id=connection_id,
            runtime_instance_id=expected.runtime_instance_id,
            epoch=expected.epoch,
            ttl_seconds=float(ttl_seconds),
        )
        self.register_candidate(registration, expected=expected)
        return self.promote(
            ScopeTokenPromotion(
                transition_id=secrets.token_urlsafe(16),
                registration_id=registration.registration_id,
                previous_registration_id=self._active_registration_id,
                connection_id=connection_id,
                runtime_instance_id=expected.runtime_instance_id,
                epoch=expected.epoch,
                overlap_seconds=BACKEND_SCOPE_TOKEN_OVERLAP_SECONDS,
            ),
            expected=expected,
        )

    def authorize(
        self,
        bearer: str,
        boundary: str,
        *,
        connection_id: str | None = None,
    ) -> BackendScopeGrant:
        grant = self._lookup_bearer(bearer)
        if connection_id is not None and grant.connection_id != connection_id:
            raise BackendScopeTokenRejected("connection_mismatch")
        return self._authorize_grant(grant, boundary)

    def authorize_claim(
        self,
        claim: object,
        boundary: str,
    ) -> BackendScopeGrant:
        legacy_claim = _grant_from_claim(claim)
        try:
            digest = bytes.fromhex(legacy_claim.token_digest)
        except ValueError:
            raise BackendScopeTokenRejected("invalid_ws_claim") from None
        if len(digest) != hashlib.sha256().digest_size:
            raise BackendScopeTokenRejected("invalid_ws_claim")
        with self._lock:
            current = self._records.get(digest)
        if (
            current is None
            or current.connection_id != legacy_claim.connection_id
            or current.auth != legacy_claim.auth
            or current.valid_until != legacy_claim.valid_until
            or current.token_digest != legacy_claim.token_digest
        ):
            raise BackendScopeTokenRejected("invalid_ws_claim")
        return self._authorize_grant(current, boundary)

    def ws_claim(self, grant: BackendScopeGrant) -> dict[str, object]:
        now = self._clock()
        digest = bytes.fromhex(grant.token_digest)
        with self._lock:
            current = self._records.get(digest)
            if current is None or current.registration_id != grant.registration_id:
                raise BackendScopeTokenRejected("unknown_token")
            if now >= current.valid_until:
                self._remove_grant_locked(current)
                raise BackendScopeTokenRejected("expired")
            if current.state is BackendScopeGrantState.CANDIDATE:
                raise BackendScopeTokenRejected("candidate_not_active")
            generation = self._backend_generation
        self._require_scope_authorized(
            "backend.scope_token.ws_claim",
            expected=current.auth,
        )
        return {
            "connection_id": current.connection_id,
            "runtime_instance_id": current.auth.runtime_instance_id,
            "epoch": current.auth.epoch,
            "backend_generation": generation,
        }

    def authorize_ws_claim(
        self,
        claim: object,
        boundary: str,
    ) -> AuthScope:
        if not isinstance(claim, dict) or set(claim) != {
            "connection_id",
            "runtime_instance_id",
            "epoch",
            "backend_generation",
        }:
            raise BackendScopeTokenRejected("invalid_ws_claim")
        connection_id = claim.get("connection_id")
        generation = claim.get("backend_generation")
        scope = AuthScope(
            claim.get("runtime_instance_id"),  # type: ignore[arg-type]
            claim.get("epoch"),  # type: ignore[arg-type]
        )
        try:
            _validate_connection_id(connection_id)  # type: ignore[arg-type]
            _validate_auth_scope(scope)
        except AuthRequired:
            raise BackendScopeTokenRejected("invalid_ws_claim") from None
        if (
            not isinstance(generation, str)
            or re.fullmatch(r"[0-9a-f]{32}", generation) is None
        ):
            raise BackendScopeTokenRejected("invalid_ws_claim")
        with self._lock:
            if generation != self._backend_generation:
                raise BackendScopeTokenRejected("backend_generation_changed")
            if ConnectionScope(connection_id, scope) not in self._generation_scopes:
                raise BackendScopeTokenRejected("invalid_ws_claim")
        self._require_scope_authorized(boundary, expected=scope)
        return scope

    def revoke(self, *, connection_id: str, expected: AuthScope) -> None:
        _validate_connection_id(connection_id)
        _validate_auth_scope(expected)
        with self._lock:
            connection_scope = ConnectionScope(connection_id, expected)
            doomed = [
                grant
                for grant in self._registrations.values()
                if grant.connection_id == connection_id and grant.auth == expected
            ]
            for grant in doomed:
                self._remove_grant_locked(grant)
            self._registration_records = {
                registration_id: record
                for registration_id, record in self._registration_records.items()
                if ConnectionScope(record.connection_id, record.auth) != connection_scope
            }
            self._transitions = {
                transition_id: transition
                for transition_id, transition in self._transitions.items()
                if ConnectionScope(
                    transition.promotion.connection_id,
                    AuthScope(
                        transition.promotion.runtime_instance_id,
                        transition.promotion.epoch,
                    ),
                )
                != connection_scope
            }
            if connection_scope in self._generation_scopes:
                self._generation_scopes.remove(connection_scope)
                self._backend_generation = secrets.token_hex(16)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
            self._registrations.clear()
            self._registration_records.clear()
            self._transitions.clear()
            self._generation_scopes.clear()
            self._active_registration_id = None
            self._backend_generation = secrets.token_hex(16)

    def _authorize_grant(
        self,
        grant: BackendScopeGrant,
        boundary: str,
    ) -> BackendScopeGrant:
        now = self._clock()
        if now >= grant.valid_until:
            with self._lock:
                self._remove_grant_locked(grant)
            raise BackendScopeTokenRejected("expired")
        if grant.state is BackendScopeGrantState.CANDIDATE:
            raise BackendScopeTokenRejected("candidate_not_active")
        self._require_scope_authorized(boundary, expected=grant.auth)
        return grant

    def _prune_locked(self, now: float) -> None:
        expired = [
            grant
            for grant in self._registrations.values()
            if now >= grant.valid_until
        ]
        for grant in expired:
            self._remove_grant_locked(grant)

    def _lookup_bearer(self, bearer: str) -> BackendScopeGrant:
        try:
            _validate_backend_scope_bearer(bearer)
        except AuthRequired:
            raise BackendScopeTokenRejected("unknown_token") from None
        digest = hashlib.sha256(bearer.encode("ascii")).digest()
        now = self._clock()
        with self._lock:
            grant = self._records.get(digest)
            if grant is None:
                raise BackendScopeTokenRejected("unknown_token")
            if now >= grant.valid_until:
                self._remove_grant_locked(grant)
                raise BackendScopeTokenRejected("expired")
            return grant

    def _require_scope_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope,
    ) -> AuthScope:
        try:
            authorized = self._authorize(boundary, expected=expected)
        except AuthRequired as error:
            if isinstance(error, AuthScopeChanged):
                raise BackendScopeTokenRejected("scope_not_authorized") from None
            raise
        if authorized != expected:
            raise BackendScopeTokenRejected("scope_not_authorized")
        return authorized

    def _store_grant_locked(self, grant: BackendScopeGrant) -> None:
        digest = bytes.fromhex(grant.token_digest)
        self._records[digest] = grant
        self._registrations[grant.registration_id] = grant

    def _remove_grant_locked(self, grant: BackendScopeGrant) -> None:
        digest = bytes.fromhex(grant.token_digest)
        self._records.pop(digest, None)
        self._registrations.pop(grant.registration_id, None)
        if self._active_registration_id == grant.registration_id:
            self._active_registration_id = None


def _validated_scope_token_registration(
    registration: ScopeTokenRegistration,
) -> ScopeTokenRegistration:
    if not isinstance(registration, ScopeTokenRegistration):
        raise BackendScopeTokenRejected("invalid_registration")
    try:
        parsed = parse_control_frame(
            {
                "version": DESKTOP_SCOPE_PROTOCOL_VERSION,
                "operation": "register_scope_token",
                "registration_id": registration.registration_id,
                "bearer": registration.bearer,
                "connection_id": registration.connection_id,
                "runtime_instance_id": registration.runtime_instance_id,
                "epoch": registration.epoch,
                "ttl_seconds": registration.ttl_seconds,
            }
        )
    except (UnicodeError, ValueError):
        raise BackendScopeTokenRejected("invalid_registration") from None
    if not isinstance(parsed, ScopeTokenRegistration):
        raise BackendScopeTokenRejected("invalid_registration")
    return parsed


def _validated_scope_token_promotion(
    promotion: ScopeTokenPromotion,
) -> ScopeTokenPromotion:
    if not isinstance(promotion, ScopeTokenPromotion):
        raise BackendScopeTokenRejected("invalid_promotion")
    try:
        parsed = parse_control_frame(
            {
                "version": DESKTOP_SCOPE_PROTOCOL_VERSION,
                "operation": "promote_scope_token",
                "transition_id": promotion.transition_id,
                "registration_id": promotion.registration_id,
                "previous_registration_id": promotion.previous_registration_id,
                "connection_id": promotion.connection_id,
                "runtime_instance_id": promotion.runtime_instance_id,
                "epoch": promotion.epoch,
                "overlap_seconds": promotion.overlap_seconds,
            }
        )
    except (UnicodeError, ValueError):
        raise BackendScopeTokenRejected("invalid_promotion") from None
    if not isinstance(parsed, ScopeTokenPromotion):
        raise BackendScopeTokenRejected("invalid_promotion")
    return parsed


def parse_backend_scope_token_registration(
    value: object,
) -> BackendScopeTokenRegistration:
    expected_keys = {
        "version",
        "operation",
        "bearer",
        "connection_id",
        "runtime_instance_id",
        "epoch",
        "ttl_seconds",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AuthRequired("runtime_unavailable")
    if value.get("version") != 1 or value.get("operation") != "register_scope_token":
        raise AuthRequired("runtime_unavailable")
    bearer = value.get("bearer")
    connection_id = value.get("connection_id")
    runtime_instance_id = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    ttl_seconds = value.get("ttl_seconds")
    if not isinstance(bearer, str) or not isinstance(connection_id, str):
        raise AuthRequired("runtime_unavailable")
    auth = AuthScope(runtime_instance_id, epoch)  # type: ignore[arg-type]
    _validate_backend_scope_bearer(bearer)
    _validate_connection_id(connection_id)
    _validate_auth_scope(auth)
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not 0 < float(ttl_seconds) <= _LEGACY_BACKEND_SCOPE_TOKEN_TTL_SECONDS
    ):
        raise AuthRequired("runtime_unavailable")
    return BackendScopeTokenRegistration(
        bearer=bearer,
        connection_id=connection_id,
        auth=auth,
        ttl_seconds=float(ttl_seconds),
    )


def parse_trace_transport_registration(value: object) -> TraceTransportRegistration:
    expected_keys = {
        "version",
        "operation",
        "endpoint",
        "authorization",
        "installation_id",
        "entrypoint",
        "plugins_toml",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AuthRequired("runtime_unavailable")
    if value.get("version") != 1 or value.get("operation") != "register_trace_transport":
        raise AuthRequired("runtime_unavailable")

    endpoint = value.get("endpoint")
    authorization = value.get("authorization")
    installation_id = value.get("installation_id")
    entrypoint = value.get("entrypoint")
    plugins_toml = value.get("plugins_toml")
    if not all(
        isinstance(item, str)
        for item in (endpoint, authorization, installation_id, entrypoint, plugins_toml)
    ):
        raise AuthRequired("runtime_unavailable")
    assert isinstance(endpoint, str)
    assert isinstance(authorization, str)
    assert isinstance(installation_id, str)
    assert isinstance(entrypoint, str)
    assert isinstance(plugins_toml, str)

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
        identity = uuid.UUID(installation_id)
    except (ValueError, AttributeError):
        raise AuthRequired("runtime_unavailable") from None
    if not (
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and 1 <= port <= 65535
        and parsed.path == "/v1/traces"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", authorization)
        and identity.version == 4
        and str(identity) == installation_id.lower()
        and entrypoint == "desktop"
        and plugins_toml.replace("\\", "/").endswith(
            "/ansatz-voice-trace/plugins.toml"
        )
        and len(plugins_toml) <= 2_048
    ):
        raise AuthRequired("runtime_unavailable")

    return TraceTransportRegistration(
        endpoint=endpoint,
        authorization=authorization,
        installation_id=installation_id,
        entrypoint=entrypoint,
        plugins_toml=plugins_toml,
    )


def _validate_backend_scope_bearer(bearer: str) -> None:
    if not isinstance(bearer, str) or len(bearer) != 43:
        raise AuthRequired("runtime_unavailable")
    try:
        decoded = base64.b64decode(
            bearer + "=",
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeError):
        raise AuthRequired("runtime_unavailable") from None
    if len(decoded) != _BACKEND_SCOPE_TOKEN_BYTES:
        raise AuthRequired("runtime_unavailable")


def _validate_connection_id(connection_id: str) -> None:
    if (
        not isinstance(connection_id, str)
        or not 0 < len(connection_id) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in connection_id)
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_auth_scope(scope: AuthScope) -> None:
    if not isinstance(scope, AuthScope):
        raise AuthRequired("runtime_unavailable")
    if (
        len(scope.runtime_instance_id) != 32
        or any(character not in "0123456789abcdef" for character in scope.runtime_instance_id)
        or not isinstance(scope.epoch, int)
        or isinstance(scope.epoch, bool)
        or not 0 <= scope.epoch <= 2**53 - 1
    ):
        raise AuthRequired("runtime_unavailable")


@dataclass(frozen=True)
class _LegacyBackendScopeClaim:
    connection_id: str
    auth: AuthScope
    valid_until: float
    token_digest: str


def _grant_from_claim(value: object) -> _LegacyBackendScopeClaim:
    expected_keys = {
        "connection_id",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "token_digest",
    }
    if not isinstance(value, dict) or set(value) != expected_keys:
        raise AuthRequired("runtime_unavailable")
    connection_id = value.get("connection_id")
    auth = AuthScope(
        value.get("runtime_instance_id"),  # type: ignore[arg-type]
        value.get("epoch"),  # type: ignore[arg-type]
    )
    valid_until = value.get("valid_until")
    token_digest = value.get("token_digest")
    if not isinstance(connection_id, str):
        raise AuthRequired("runtime_unavailable")
    _validate_connection_id(connection_id)
    _validate_auth_scope(auth)
    if (
        not isinstance(valid_until, (int, float))
        or isinstance(valid_until, bool)
        or not isinstance(token_digest, str)
        or len(token_digest) != 64
    ):
        raise AuthRequired("runtime_unavailable")
    return _LegacyBackendScopeClaim(
        connection_id=connection_id,
        auth=auth,
        valid_until=float(valid_until),
        token_digest=token_digest,
    )


@dataclass(frozen=True)
class RuntimeSnapshot:
    state: AuthState
    epoch: int
    valid_until: float
    runtime_instance_id: str
    boot_id: str
    username: str | None
    session_expires_at: str | None
    reason: str | None
    cloud_state: CloudState | None = None
    account_id: str | None = None
    session_id: str | None = None
    installation_id: str | None = None
    principal_key: str | None = None
    predecessor_principal_key: str | None = None
    validation_state: ValidationState = ValidationState.UNKNOWN
    validation_reason: str | None = None
    last_validated_at: str | None = None
    legacy: bool = False

    @classmethod
    def new_authenticated(
        cls,
        username: str,
        *,
        now: float,
        ttl: float,
    ) -> RuntimeSnapshot:
        if not username or ttl <= 0:
            raise AuthRequired("session_expired")
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=1,
            valid_until=now + ttl,
            runtime_instance_id=secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=username,
            session_expires_at=None,
            reason=None,
            cloud_state=CloudState.ACTIVE,
        )

    @classmethod
    def from_session_status(
        cls,
        status: SessionStatus,
        *,
        now: float,
        runtime_instance_id: str | None = None,
        epoch: int = 1,
        principal_key: str | None = None,
    ) -> RuntimeSnapshot:
        valid_until = _lease_deadline(status, now=now)
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=epoch,
            valid_until=valid_until,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=status.username,
            session_expires_at=status.session_expires_at,
            reason=None,
            cloud_state=CloudState.ACTIVE,
            principal_key=principal_key,
            legacy=_is_legacy_principal_key(principal_key),
        )

    @classmethod
    def signed_out(
        cls,
        *,
        epoch: int = 0,
        runtime_instance_id: str | None = None,
        reason: str | None = None,
    ) -> RuntimeSnapshot:
        return cls(
            state=AuthState.SIGNED_OUT,
            epoch=epoch,
            valid_until=0.0,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=None,
            session_expires_at=None,
            reason=reason,
            cloud_state=None,
        )

    @classmethod
    def from_native_credential(
        cls,
        record: NativeCredentialRecord,
        *,
        runtime_instance_id: str | None = None,
        epoch: int = 1,
    ) -> RuntimeSnapshot:
        credential = record.credential
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=epoch,
            valid_until=DURABLE_AUTHORIZATION_VALID_UNTIL,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=credential.username,
            session_expires_at=None,
            reason=None,
            cloud_state=CloudState.UNREACHABLE,
            account_id=credential.account_id,
            session_id=credential.session_id,
            installation_id=credential.installation_id,
            principal_key=f"account:{credential.account_id}",
            predecessor_principal_key=record.predecessor_principal_key,
            validation_state=ValidationState.UNKNOWN,
            last_validated_at=record.last_validated_at,
        )

    @classmethod
    def from_legacy_record(
        cls,
        record: LegacyCredentialRecord,
        *,
        runtime_instance_id: str | None = None,
        epoch: int = 1,
    ) -> RuntimeSnapshot:
        return cls(
            state=AuthState.AUTHENTICATED,
            epoch=epoch,
            valid_until=DURABLE_AUTHORIZATION_VALID_UNTIL,
            runtime_instance_id=runtime_instance_id or secrets.token_hex(16),
            boot_id=_read_boot_id(),
            username=record.cookie_record.username,
            session_expires_at=record.cookie_record.session_expires_at,
            reason=None,
            cloud_state=CloudState.UNREACHABLE,
            principal_key=record.principal_key,
            validation_state=ValidationState.UNKNOWN,
            legacy=True,
        )

    @property
    def scope(self) -> AuthScope:
        return AuthScope(self.runtime_instance_id, self.epoch)

    def refreshed(self, status: SessionStatus, *, now: float) -> RuntimeSnapshot:
        if self.state is not AuthState.AUTHENTICATED or status.username != self.username:
            raise AuthRequired("session_rejected")
        if self.session_expires_at is None:
            absolute_cap = None
            absolute_text = status.session_expires_at
        else:
            current_expiry = _parse_aware_datetime(self.session_expires_at)
            response_expiry = _parse_aware_datetime(status.session_expires_at)
            if current_expiry <= response_expiry:
                absolute_cap = current_expiry
                absolute_text = self.session_expires_at
            else:
                absolute_cap = response_expiry
                absolute_text = status.session_expires_at
        return replace(
            self,
            valid_until=_lease_deadline(status, now=now, absolute_cap=absolute_cap),
            session_expires_at=absolute_text,
            reason=None,
            cloud_state=CloudState.ACTIVE,
        )

    def locked(self, reason: str, *, now: float) -> RuntimeSnapshot:
        return replace(
            self,
            state=AuthState.LOCKED,
            epoch=self.epoch + 1,
            valid_until=now,
            runtime_instance_id=(
                self.runtime_instance_id
                if reason in _EXPLICIT_TERMINAL_REASONS
                else secrets.token_hex(16)
            ),
            reason=reason,
            cloud_state=None,
            validation_state=ValidationState.DEGRADED,
            validation_reason=reason,
        )

    def validating(self) -> RuntimeSnapshot:
        return replace(self, validation_state=ValidationState.VALIDATING, validation_reason=None)

    def degraded(self, reason: str) -> RuntimeSnapshot:
        cloud_state = (
            CloudState.REAUTH_REQUIRED
            if reason in {"session_expired", "session_rejected"}
            else CloudState.UNREACHABLE
        )
        return replace(
            self,
            cloud_state=cloud_state,
            validation_state=ValidationState.DEGRADED,
            validation_reason=reason,
        )

    def online(self, *, last_validated_at: str) -> RuntimeSnapshot:
        return replace(
            self,
            cloud_state=CloudState.ACTIVE,
            validation_state=ValidationState.ONLINE,
            validation_reason=None,
            last_validated_at=last_validated_at,
        )

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope,
        now: float,
        allow_local_continuity: bool = False,
    ) -> AuthScope:
        if not boundary:
            raise AuthRequired("runtime_unavailable")
        if self.state is not AuthState.AUTHENTICATED:
            raise AuthRequired(self.reason or "signed_out")
        if _read_boot_id() != self.boot_id:
            raise AuthRequired("session_expired")
        if self.cloud_state is not CloudState.ACTIVE:
            if not allow_local_continuity:
                fallback = (
                    "session_expired"
                    if self.cloud_state is CloudState.REAUTH_REQUIRED
                    else "server_unavailable"
                )
                raise AuthRequired(self.validation_reason or fallback)
        elif self.principal_key is None and now >= self.valid_until:
            raise AuthRequired("session_expired")
        if expected != self.scope:
            raise AuthScopeChanged("runtime_unavailable")
        return self.scope

    def public_dict_v1(self) -> dict[str, object]:
        """The seven-key wire shape protocol-v1 peers validate strictly."""
        return {
            "state": self.state.value,
            "username": self.username,
            "runtime_instance_id": self.runtime_instance_id,
            "epoch": self.epoch,
            "valid_until": self.valid_until,
            "session_expires_at": self.session_expires_at,
            "reason": self.reason,
        }

    def public_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "username": self.username,
            "runtime_instance_id": self.runtime_instance_id,
            "epoch": self.epoch,
            "valid_until": self.valid_until,
            "session_expires_at": self.session_expires_at,
            "reason": self.reason,
            "cloud_state": (
                self.cloud_state.value if self.cloud_state is not None else None
            ),
            "account_id": self.account_id,
            "session_id": self.session_id,
            "installation_id": self.installation_id,
            "principal_key": self.principal_key,
            "predecessor_principal_key": self.predecessor_principal_key,
            "validation_state": self.validation_state.value,
            "validation_reason": self.validation_reason,
            "last_validated_at": self.last_validated_at,
            "legacy": self.legacy,
        }

    def public_dict_v2(self) -> dict[str, object]:
        """Protocol-v2 continuity shape, before cloud availability was added."""
        value = self.public_dict()
        value.pop("cloud_state")
        return value


_unix_lock_registry_guard = threading.Lock()
_held_unix_locks: set[Path] = set()


@dataclass(frozen=True)
class WindowsNamedPipeEndpoint:
    pipe_name: str
    owner_sid: str
    first_instance: bool

    @classmethod
    def for_current_sid(
        cls,
        *,
        first_instance: bool,
        runtime_namespace: str | None = None,
    ) -> WindowsNamedPipeEndpoint:
        if os.name != "nt" or not first_instance:
            raise AuthRequired("runtime_unavailable")
        owner_sid = _windows_current_sid()
        # Named pipes are global to the user's SID. Include the product/auth
        # namespace so an updated detached owner cannot be mistaken for a
        # previous wire-contract owner that may still be running.
        namespace_suffix = _auth_runtime_namespace_suffix(runtime_namespace)
        compact_sid = hashlib.blake2s(
            owner_sid.encode("ascii"),
            digest_size=16,
        ).hexdigest() + namespace_suffix + _test_runtime_suffix()
        return cls(
            pipe_name=rf"\\.\pipe\hermes-auth-{compact_sid}",
            owner_sid=owner_sid,
            first_instance=True,
        )

    def bind_owner(self) -> WindowsOwnerServer:
        if os.name != "nt" or not self.first_instance:
            raise AuthRequired("runtime_unavailable")
        try:
            import ntsecuritycon
            import pywintypes
            import win32api
            import win32con
            import win32pipe
            import win32security

            owner = win32security.ConvertStringSidToSid(self.owner_sid)
            system = win32security.CreateWellKnownSid(
                win32security.WinLocalSystemSid,
                None,
            )
            dacl = win32security.ACL()
            access = ntsecuritycon.GENERIC_READ | ntsecuritycon.GENERIC_WRITE
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, owner)
            dacl.AddAccessAllowedAce(win32security.ACL_REVISION, access, system)
            descriptor = win32security.SECURITY_DESCRIPTOR()
            descriptor.SetSecurityDescriptorDacl(True, dacl, False)
            attributes = pywintypes.SECURITY_ATTRIBUTES()
            attributes.SECURITY_DESCRIPTOR = descriptor
            attributes.bInheritHandle = False
            handle = win32pipe.CreateNamedPipe(
                self.pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX | 0x00080000,
                win32pipe.PIPE_TYPE_BYTE
                | win32pipe.PIPE_READMODE_BYTE
                | win32pipe.PIPE_WAIT
                | 0x00000008,
                1,
                65_536,
                65_536,
                0,
                attributes,
            )
            win32api.SetHandleInformation(
                handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
        except Exception:
            try:
                handle.Close()
            except (AttributeError, UnboundLocalError):
                pass
            raise AuthRequired("runtime_unavailable") from None
        return WindowsOwnerServer(self, handle)

    def connect_current(self) -> WindowsPipeConnection:
        if os.name != "nt":
            raise AuthRequired("runtime_unavailable")
        try:
            import win32api
            import win32con
            import win32file

            handle = win32file.CreateFile(
                self.pipe_name,
                win32con.GENERIC_READ | win32con.GENERIC_WRITE,
                0,
                None,
                win32con.OPEN_EXISTING,
                0x00100000 | 0x00010000,
                None,
            )
            win32api.SetHandleInformation(
                handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
        except Exception:
            try:
                handle.Close()
            except (AttributeError, UnboundLocalError):
                pass
            raise AuthRequired("runtime_unavailable") from None
        return WindowsPipeConnection(handle)


class WindowsPipeConnection:
    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._closed = False

    @property
    def handle(self) -> object:
        return self._handle

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._handle.Close()  # type: ignore[attr-defined]
        except Exception:
            return

    def recv(self, size: int) -> bytes:
        if self._closed or size <= 0:
            return b""
        try:
            import win32file

            _status, data = win32file.ReadFile(self._handle, size)
            return bytes(data)
        except Exception:
            raise AuthRequired("runtime_unavailable") from None

    def sendall(self, data: bytes) -> None:
        if self._closed:
            raise AuthRequired("runtime_unavailable")
        try:
            import win32file

            win32file.WriteFile(self._handle, data)
        except Exception:
            raise AuthRequired("runtime_unavailable") from None


class WindowsOwnerServer:
    def __init__(self, endpoint: WindowsNamedPipeEndpoint, handle: object) -> None:
        self._endpoint = endpoint
        self._handle: object | None = handle
        self._closed = False

    def accept(self) -> WindowsPipeConnection:
        if self._closed or self._handle is None:
            raise AuthRequired("runtime_unavailable")
        try:
            import pywintypes
            import win32api
            import win32con
            import win32pipe
            import win32security

            try:
                win32pipe.ConnectNamedPipe(self._handle, None)
            except pywintypes.error as error:
                if error.winerror != 535:
                    raise
            win32security.ImpersonateNamedPipeClient(self._handle)
            try:
                token = win32security.OpenThreadToken(
                    win32api.GetCurrentThread(),
                    win32security.TOKEN_QUERY,
                    True,
                )
                try:
                    peer_sid = win32security.ConvertSidToStringSid(
                        win32security.GetTokenInformation(
                            token,
                            win32security.TokenUser,
                        )[0]
                    )
                finally:
                    token.Close()
            finally:
                win32security.RevertToSelf()
            if peer_sid != self._endpoint.owner_sid:
                raise AuthRequired("runtime_unavailable")
            win32api.SetHandleInformation(
                self._handle,
                win32con.HANDLE_FLAG_INHERIT,
                0,
            )
            connection = WindowsPipeConnection(self._handle)
            self._handle = None
        except Exception:
            self.close()
            raise AuthRequired("runtime_unavailable") from None
        return connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            import win32pipe

            if self._handle is not None:
                win32pipe.DisconnectNamedPipe(self._handle)
        except Exception:
            pass
        try:
            if self._handle is not None:
                self._handle.Close()  # type: ignore[attr-defined]
        except Exception:
            pass


@dataclass(frozen=True)
class UnixEndpoint:
    root: Path
    socket_path: Path
    pointer_path: Path
    lock_path: Path

    @classmethod
    def for_directory(cls, root: Path, *, random_name: str) -> UnixEndpoint:
        _validate_random_name(random_name)
        _ensure_private_directory(root)
        compact_name = hashlib.blake2s(
            random_name.encode("ascii"),
            digest_size=10,
        ).hexdigest()
        return cls(
            root=root,
            socket_path=root / f"h-{compact_name}.s",
            pointer_path=root / "current.json",
            lock_path=root / "owner.lock",
        )

    @classmethod
    def for_current_user(
        cls,
        *,
        random_name: str,
        forbid_abstract: bool,
        darwin_user_temp: bool,
        runtime_namespace: str | None = None,
    ) -> UnixEndpoint:
        namespace_suffix = _auth_runtime_namespace_suffix(runtime_namespace)
        if sys.platform.startswith("linux"):
            if not forbid_abstract:
                raise AuthRequired("runtime_unavailable")
            runtime_root = _linux_runtime_root(runtime_namespace=runtime_namespace)
        elif sys.platform == "darwin":
            if not darwin_user_temp:
                raise AuthRequired("runtime_unavailable")
            runtime_root = (
                _darwin_user_temp_dir()
                / f"ha{namespace_suffix}{_test_runtime_suffix()}"
            )
        else:
            raise AuthRequired("runtime_unavailable")
        return cls.for_directory(runtime_root, random_name=random_name)

    def acquire_owner_lock(self) -> UnixOwnerLock:
        try:
            import fcntl

            fd = _open_private_file(self.lock_path, os.O_RDWR | os.O_CREAT)
            resolved = self.lock_path.resolve(strict=True)
            with _unix_lock_registry_guard:
                if resolved in _held_unix_locks:
                    os.close(fd)
                    raise AuthRequired("runtime_unavailable")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise AuthRequired("runtime_unavailable") from None
                _held_unix_locks.add(resolved)
            return UnixOwnerLock(fd, resolved)
        except ImportError:
            raise AuthRequired("runtime_unavailable") from None

    def bind_owner(self, owner_lock: UnixOwnerLock) -> UnixOwnerServer:
        if owner_lock.closed or owner_lock.path != self.lock_path.resolve(strict=True):
            raise AuthRequired("runtime_unavailable")
        if self.socket_path.exists() or self.socket_path.is_symlink():
            raise AuthRequired("runtime_unavailable")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.set_inheritable(False)
        try:
            if len(os.fsencode(self.socket_path)) >= 104:
                raise AuthRequired("runtime_unavailable")
            listener.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            _validate_socket_file(self.socket_path)
            listener.listen(16)
            self._write_pointer()
        except Exception:
            listener.close()
            try:
                self.socket_path.unlink()
            except OSError:
                pass
            raise
        return UnixOwnerServer(self, listener)

    def connect_current(
        self,
        *,
        timeout: float = _RUNTIME_REQUEST_TIMEOUT_SECONDS,
    ) -> socket.socket:
        socket_path = self._read_pointer()
        _validate_socket_file(socket_path)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.set_inheritable(False)
        connection.settimeout(timeout)
        try:
            connection.connect(str(socket_path))
            _validate_peer_uid(connection)
        except (AuthRequired, OSError):
            connection.close()
            raise AuthRequired("runtime_unavailable") from None
        return connection

    def _write_pointer(self) -> None:
        payload = json.dumps(
            {"version": 1, "socket": self.socket_path.name},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        temp_path = self.root / f".current-{secrets.token_hex(8)}.tmp"
        fd = -1
        try:
            fd = _open_private_file(
                temp_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            os.write(fd, payload)
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.replace(temp_path, self.pointer_path)
            os.chmod(self.pointer_path, 0o600, follow_symlinks=False)
            _validate_private_regular_file(self.pointer_path)
        except Exception:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise AuthRequired("runtime_unavailable") from None

    def _read_pointer(self) -> Path:
        fd = -1
        try:
            fd = _open_private_file(self.pointer_path, os.O_RDONLY, create=False)
            details = os.fstat(fd)
            if details.st_size <= 0 or details.st_size > 512:
                raise AuthRequired("runtime_unavailable")
            raw = os.read(fd, 513)
        except (AuthRequired, OSError):
            raise AuthRequired("runtime_unavailable") from None
        finally:
            if fd >= 0:
                os.close(fd)
        try:
            payload = json.loads(raw)
        except (UnicodeError, ValueError):
            raise AuthRequired("runtime_unavailable") from None
        if not isinstance(payload, dict) or set(payload) != {"version", "socket"}:
            raise AuthRequired("runtime_unavailable")
        name = payload.get("socket")
        if payload.get("version") != 1 or not isinstance(name, str):
            raise AuthRequired("runtime_unavailable")
        _validate_socket_name(name)
        candidate = self.root / name
        if candidate.parent != self.root:
            raise AuthRequired("runtime_unavailable")
        return candidate


class UnixOwnerLock:
    def __init__(self, fd: int, path: Path) -> None:
        self._fd = fd
        self.path = path
        self.closed = False
        os.set_inheritable(fd, False)

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            import fcntl

            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            with _unix_lock_registry_guard:
                _held_unix_locks.discard(self.path)


class UnixOwnerServer:
    def __init__(self, endpoint: UnixEndpoint, listener: socket.socket) -> None:
        self._endpoint = endpoint
        self._listener = listener
        self._closed = False

    def fileno(self) -> int:
        return self._listener.fileno()

    def accept(self) -> socket.socket:
        transient_accept_errors = {
            errno.EAGAIN,
            errno.ECONNABORTED,
            errno.EINTR,
            errno.EWOULDBLOCK,
        }
        while True:
            try:
                connection, _ = self._listener.accept()
            except OSError as error:
                if not self._closed and error.errno in transient_accept_errors:
                    continue
                raise AuthRequired("runtime_unavailable") from None
            try:
                connection.set_inheritable(False)
                _validate_peer_uid(connection)
            except (AuthRequired, OSError):
                try:
                    connection.close()
                except OSError:
                    pass
                continue
            return connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._listener.close()
        try:
            if self._endpoint._read_pointer() == self._endpoint.socket_path:
                self._endpoint.pointer_path.unlink()
        except (AuthRequired, OSError):
            pass
        try:
            self._endpoint.socket_path.unlink()
        except OSError:
            pass


class SocketLivenessProbe:
    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._connection.set_inheritable(False)
        self._closed = False

    def __call__(self) -> bool:
        if self._closed:
            return False
        try:
            readable, _, _ = select.select([self._connection], [], [], 0)
            if not readable:
                return True
            return self._connection.recv(1, socket.MSG_PEEK) != b""
        except (OSError, ValueError):
            return False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


class RuntimeConsumer:
    def __init__(
        self,
        snapshot: RuntimeSnapshot,
        *,
        liveness_probe: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
        on_authorized: Callable[[], None] | None = None,
        allow_local_continuity: bool = False,
    ) -> None:
        self._snapshot = snapshot
        self._liveness_probe = liveness_probe
        self._clock = clock
        self._on_authorized = on_authorized or (lambda: None)
        self._allow_local_continuity = allow_local_continuity
        self._lock = threading.RLock()

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            if snapshot.runtime_instance_id != self._snapshot.runtime_instance_id:
                self._snapshot = self._snapshot.locked(
                    "runtime_unavailable",
                    now=self._clock(),
                )
                raise AuthRequired("runtime_unavailable")
            if snapshot.epoch < self._snapshot.epoch:
                raise AuthRequired("runtime_unavailable")
            self._snapshot = snapshot

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope | None = None,
        now: float | None = None,
    ) -> AuthScope:
        checked_at = self._clock() if now is None else now
        if not self._liveness_probe():
            with self._lock:
                if self._snapshot.state is AuthState.AUTHENTICATED:
                    self._snapshot = self._snapshot.locked(
                        "runtime_unavailable",
                        now=checked_at,
                    )
            raise AuthRequired("runtime_unavailable")
        snapshot = self.snapshot()
        scope = snapshot.require_authorized(
            boundary,
            expected=expected or snapshot.scope,
            now=checked_at,
            allow_local_continuity=self._allow_local_continuity,
        )
        self._on_authorized()
        return scope


class _SecretBackend(Protocol):
    def read(self) -> str | None: ...

    def write(self, raw: str) -> None: ...

    def delete(self) -> None: ...


class _Hardener(Protocol):
    def apply_required(self) -> None: ...


class _MemorySecretBackend:
    def __init__(self) -> None:
        self._raw: str | None = None

    def read(self) -> str | None:
        return self._raw

    def write(self, raw: str) -> None:
        self._raw = raw

    def delete(self) -> None:
        self._raw = None


class _KeyringSecretBackend:
    SERVICE = _DEFAULT_AUTH_KEYRING_SERVICE
    ACCOUNT = "django-session"

    def __init__(
        self,
        *,
        service: str | None = None,
        legacy_service: str | None = None,
    ) -> None:
        if service is None:
            if legacy_service is not None:
                raise AuthRequired("runtime_unavailable")
            service, legacy_service = _auth_keyring_services()
        else:
            service = _validate_auth_keyring_service(service)
            if legacy_service is not None:
                legacy_service = _validate_auth_keyring_service(legacy_service)
            if legacy_service == service:
                raise AuthRequired("runtime_unavailable")
        self._service = service
        self._legacy_service = legacy_service

    def read(self) -> str | None:
        import keyring

        raw = keyring.get_password(self._service, self.ACCOUNT)
        if raw is not None:
            if self._legacy_service is not None:
                self._delete_service(keyring, self._legacy_service)
            return raw
        if self._legacy_service is None:
            return None
        legacy_raw = keyring.get_password(self._legacy_service, self.ACCOUNT)
        if legacy_raw is None:
            return None
        keyring.set_password(self._service, self.ACCOUNT, legacy_raw)
        if keyring.get_password(self._service, self.ACCOUNT) != legacy_raw:
            self._delete_service(keyring, self._service)
            raise RuntimeError("secure credential migration failed")
        self._delete_service(keyring, self._legacy_service)
        return legacy_raw

    def write(self, raw: str) -> None:
        import keyring

        keyring.set_password(self._service, self.ACCOUNT, raw)

    def delete(self) -> None:
        import keyring

        self._delete_service(keyring, self._service)
        if self._legacy_service is not None:
            self._delete_service(keyring, self._legacy_service)

    def _delete_service(self, keyring, service: str) -> None:
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(service, self.ACCOUNT)
        except PasswordDeleteError:
            return


class ProcessHardener:
    def apply_required(self) -> None:
        if sys.platform.startswith("linux"):
            _linux_prctl_dumpable_zero()
            _disable_core_dumps()
            return
        if sys.platform == "darwin":
            _darwin_deny_attach()
            _disable_core_dumps()
            return
        if os.name == "nt":
            _windows_disable_wer_dump()
            _windows_apply_process_mitigations()
            return
        raise AuthRequired("runtime_unavailable")


class S6LifecycleAdapter:
    """Apply auth transitions to the fixed container capability slots."""

    _STATIC_SERVICES = frozenset({"dashboard", "main-hermes"})
    _TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

    def __init__(
        self,
        *,
        service_root: Path,
        hermes_home: Path,
        environment: dict[str, str],
        signal_service: Callable[[str, str], None] | None = None,
    ) -> None:
        self._service_root = service_root
        self._hermes_home = hermes_home
        self._dashboard_enabled = (
            environment.get("HERMES_DASHBOARD", "").strip().lower()
            in self._TRUE_ENV_VALUES
        )
        self._multiplex_profiles = (
            environment.get("GATEWAY_MULTIPLEX_PROFILES", "").strip().lower()
            in self._TRUE_ENV_VALUES
        )
        self._signal_service = signal_service or self._signal_s6_service

    def transition(self, snapshot: RuntimeSnapshot) -> None:
        authenticated = snapshot.state is AuthState.AUTHENTICATED
        for service in self._capability_services():
            desired = authenticated and self._desired_running(service)
            try:
                self._signal_service(service, "up" if desired else "down")
            except Exception:
                # The in-process and per-request guards remain authoritative.
                # Lifecycle signaling is best-effort so a broken s6 slot can
                # never prevent publication of a locked transition.
                continue

    def _capability_services(self) -> list[str]:
        services: set[str] = set()
        try:
            entries = tuple(self._service_root.iterdir())
        except OSError:
            return []
        for entry in entries:
            name = entry.name
            if name in self._STATIC_SERVICES:
                services.add(name)
                continue
            if not name.startswith("gateway-"):
                continue
            profile = name.removeprefix("gateway-")
            try:
                from hermes_cli.service_manager import validate_profile_name

                validate_profile_name(profile)
            except (ImportError, ValueError):
                continue
            services.add(name)
        return sorted(services)

    def _desired_running(self, service: str) -> bool:
        if service == "dashboard":
            return self._dashboard_enabled
        if service == "main-hermes":
            return False
        profile = service.removeprefix("gateway-")
        if self._multiplex_profiles and profile != "default":
            return False
        profile_dir = (
            self._hermes_home
            if profile == "default"
            else self._hermes_home / "profiles" / profile
        )
        state_file = profile_dir / "gateway_state.json"
        try:
            details = state_file.lstat()
            if not stat.S_ISREG(details.st_mode) or details.st_size > 65_536:
                return False
            value = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return False
        if not isinstance(value, dict):
            return False
        desired = value.get("desired_state")
        if desired is not None:
            return desired == "running"
        return value.get("gateway_state") in {"running", "draining", "degraded"}

    def _signal_s6_service(self, service: str, action: str) -> None:
        flag = {"down": "-d", "up": "-u"}.get(action)
        if flag is None:
            raise AuthRequired("runtime_unavailable")
        result = subprocess.run(
            ["/command/s6-svc", flag, str(self._service_root / service)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            raise AuthRequired("runtime_unavailable")


class _OwnerCore:
    def __init__(
        self,
        client: AuthClient,
        *,
        secret_backend: _SecretBackend,
        hardener: _Hardener | None,
        vault_required: bool,
        clock: Callable[[], float],
        jitter: Callable[[float, float], float],
        on_transition: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        self._client = client
        self._secret_backend = secret_backend
        self._hardener = hardener
        self._vault_required = vault_required
        self._clock = clock
        self._jitter = jitter
        self._snapshot = RuntimeSnapshot.signed_out()
        self._on_transition = on_transition
        self._record: CookieRecord | NativeCredentialRecord | LegacyCredentialRecord | RevocationTombstone | SignedOutTombstone | None = None
        self._record_loaded = False
        self._consumers: list[RuntimeConsumer] = []
        self._local_continuity_enabled = False
        self._alive = True
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._next_refresh_at: float | None = None
        self._validation_failures = 0
        self._failed_login_attempts: list[float] = []
        self._last_authenticated_activity = self._clock()
        if self._on_transition is not None:
            self._on_transition(self._snapshot)

    @property
    def next_refresh_at(self) -> float | None:
        with self._lock:
            return self._next_refresh_at

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def enable_desktop_local_continuity(self) -> RuntimeSnapshot:
        with self._lock:
            self._local_continuity_enabled = True
            return self._snapshot

    def connect_consumer(
        self,
        *,
        profile: str | None = None,
        allow_local_continuity: bool = False,
    ) -> RuntimeConsumer:
        del profile
        with self._lock:
            consumer = RuntimeConsumer(
                self._snapshot,
                liveness_probe=lambda: self._alive,
                clock=self._clock,
                on_authorized=self._record_authenticated_activity,
                allow_local_continuity=(
                    self._local_continuity_enabled and allow_local_continuity
                ),
            )
            self._consumers.append(consumer)
            return consumer

    def login(
        self,
        username: str,
        password: bytearray,
        *,
        installation_id: str | None = None,
        client_version: str | None = None,
    ) -> RuntimeSnapshot:
        """Log in and persist a native credential when native context is supplied.

        The no-context path remains only for older broker callers while they are
        migrated by Task 9; it retains the schema-v1 compatibility behavior.
        """
        if installation_id is None and client_version is None:
            return self._login_legacy(username, password)
        if not isinstance(installation_id, str) or not isinstance(client_version, str):
            raise AuthRequired("runtime_unavailable")
        self._check_login_rate_limit()
        self._prepare_cookie_acquisition()
        previous = self._load_record()
        try:
            cookie = self._client.login(username, password)
            credential = self._client.issue_client_session(
                cookie.cookies,
                installation_id=installation_id,
                client_version=client_version,
            )
        except AuthServiceError as error:
            if error.reason == "invalid_credentials":
                self._record_failed_login()
            self._lock_with_reason(error.reason)
            raise AuthRequired(error.reason) from None
        now = self._clock()
        record = NativeCredentialRecord(
            credential=credential,
            last_validated_at=credential.issued_at,
            predecessor_principal_key=_proven_legacy_predecessor(
                previous,
                cookie,
                expected_installation_id=installation_id,
                issued_installation_id=credential.installation_id,
            ),
        )
        try:
            # This is the local mutation commit. It deliberately shares the
            # validation mutation lock so no old validation can overwrite the
            # credential store after this write but before publication.
            with self._lock:
                self._secret_backend.write(_encode_native_blob(record))
                if _decode_credential_blob(self._secret_backend.read() or "") != record:
                    raise AuthRequired("runtime_unavailable")
                self._record = record
                self._record_loaded = True
                self._failed_login_attempts.clear()
                self._last_authenticated_activity = now
                self._validation_failures = 0
                snapshot = RuntimeSnapshot.from_native_credential(
                    record,
                    runtime_instance_id=self._snapshot.runtime_instance_id,
                    epoch=self._snapshot.epoch + 1,
                ).online(last_validated_at=record.last_validated_at)
                self._publish_locked(snapshot)
                self._schedule_validation_locked(now)
        except Exception:
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            self._lock_with_reason(reason)
            self._best_effort_remote_logout(cookie)
            raise AuthRequired(reason) from None
        return snapshot

    def _login_legacy(self, username: str, password: bytearray) -> RuntimeSnapshot:
        self._check_login_rate_limit()
        self._prepare_cookie_acquisition()
        try:
            record = self._client.login(username, password)
            status = self._client.status(record.cookies)
        except AuthServiceError as error:
            if error.reason == "invalid_credentials":
                self._record_failed_login()
            self._lock_with_reason(error.reason)
            raise AuthRequired(error.reason) from None
        if status.username != record.username or status.username != username:
            self._best_effort_remote_logout(record)
            self._lock_with_reason("session_rejected")
            raise AuthRequired("session_rejected")
        now = self._clock()
        record = replace(record, principal_key=f"legacy:{secrets.token_hex(32)}")
        with self._lock:
            try:
                self._secret_backend.write(_encode_cookie_blob(record))
            except Exception:
                reason = (
                    "vault_unavailable" if self._vault_required else "runtime_unavailable"
                )
                self._publish_locked(self._snapshot.locked(reason, now=now))
                self._next_refresh_at = None
            else:
                reason = None
            if reason is not None:
                snapshot = None
            else:
                self._record = record
                self._record_loaded = True
                self._failed_login_attempts.clear()
                self._last_authenticated_activity = now
                snapshot = RuntimeSnapshot.from_session_status(
                    status,
                    now=now,
                    runtime_instance_id=self._snapshot.runtime_instance_id,
                    epoch=self._snapshot.epoch + 1,
                    principal_key=record.principal_key,
                )
                self._publish_locked(snapshot)
                self._schedule_locked(now)
        if snapshot is None:
            self._best_effort_remote_logout(record)
            raise AuthRequired(reason) from None
        return snapshot

    def refresh(
        self,
        *,
        installation_id: str | None = None,
        client_version: str | None = None,
    ) -> RuntimeSnapshot:
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            if isinstance(installation_id, str) and isinstance(client_version, str):
                record = self._load_record()

                if isinstance(record, LegacyCredentialRecord):
                    return self._validate_now_locked(
                        native_context=(installation_id, client_version)
                    )
            return self._refresh_once()
        finally:
            self._refresh_lock.release()

    def trace_token(
        self,
        *,
        installation_id: str,
        client_version: str,
        telemetry_schema_version: str,
    ) -> TraceCredential:
        # Keep record loading and the credential request in the same refresh
        # generation.  Validation replaces NativeCredentialRecord instances;
        # loading before acquiring this lock creates a race where a valid
        # trace request observes the old object and fails the identity check.
        with self._refresh_lock:
            record = self._load_record()
            with self._lock:
                if (
                    record is None
                    or self._record is not record
                    or self._snapshot.state is not AuthState.AUTHENTICATED
                ):
                    raise AuthRequired("signed_out")
                if self._snapshot.cloud_state is not CloudState.ACTIVE:
                    fallback = (
                        "session_expired"
                        if self._snapshot.cloud_state is CloudState.REAUTH_REQUIRED
                        else "server_unavailable"
                    )
                    raise AuthRequired(self._snapshot.validation_reason or fallback)
                self._last_authenticated_activity = self._clock()

            try:
                with self._lock:
                    current_record = self._record
                    if (
                        isinstance(record, NativeCredentialRecord)
                        and isinstance(current_record, NativeCredentialRecord)
                        and current_record.credential == record.credential
                    ):
                        record = current_record
                    elif current_record is not record:
                        raise AuthRequired("signed_out")
                    if self._snapshot.state is not AuthState.AUTHENTICATED:
                        raise AuthRequired("signed_out")
                    if self._snapshot.cloud_state is not CloudState.ACTIVE:
                        fallback = (
                            "session_expired"
                            if self._snapshot.cloud_state
                            is CloudState.REAUTH_REQUIRED
                            else "server_unavailable"
                        )
                        raise AuthRequired(
                            self._snapshot.validation_reason or fallback
                        )
                if isinstance(record, NativeCredentialRecord):
                    credential = self._client.trace_token(record.credential)
                elif isinstance(record, LegacyCredentialRecord):
                    credential = self._client.legacy_trace_token(
                        record.cookie_record.cookies,
                        installation_id=installation_id,
                        client_version=client_version,
                        telemetry_schema_version=telemetry_schema_version,
                    )
                elif isinstance(record, CookieRecord):
                    credential = self._client.legacy_trace_token(
                        record.cookies,
                        installation_id=installation_id,
                        client_version=client_version,
                        telemetry_schema_version=telemetry_schema_version,
                    )
                else:
                    raise AuthRequired("signed_out")
            except AuthServiceError as error:
                if isinstance(error, SessionRejected):
                    with self._lock:
                        if self._record is record:
                            tombstone = SignedOutTombstone(error.reason)
                            self._persist_signed_out_tombstone_locked(tombstone)
                            self._record = tombstone
                            self._record_loaded = True
                            self._publish_locked(
                                self._snapshot.locked(error.reason, now=self._clock())
                            )
                            self._next_refresh_at = None
                raise AuthRequired(error.reason) from None
            with self._lock:
                if (
                    self._record is not record
                    or self._snapshot.state is not AuthState.AUTHENTICATED
                ):
                    raise AuthRequired("signed_out")
            return credential

    def _refresh_once(self) -> RuntimeSnapshot:
        record = self._load_record()
        if record is None:
            return self.snapshot()
        if isinstance(
            record,
            (
                NativeCredentialRecord,
                LegacyCredentialRecord,
                RevocationTombstone,
                SignedOutTombstone,
            ),
        ):
            return self.snapshot()
        try:
            status = self._client.status(record.cookies)
        except AuthServiceError as error:
            with self._lock:
                if self._record is not record:
                    return self._snapshot
                if isinstance(error, SessionRejected):
                    tombstone = SignedOutTombstone(error.reason)
                    self._persist_signed_out_tombstone_locked(tombstone)
                    self._record = tombstone
                    self._record_loaded = True
                elif (
                    self._local_continuity_enabled
                    and self._snapshot.state is AuthState.AUTHENTICATED
                ):
                    self._validation_failures += 1
                    self._publish_locked(self._snapshot.degraded(error.reason))
                    self._schedule_validation_locked(self._clock())
                    return self._snapshot
                now = self._clock()
                locked = self._snapshot.locked(error.reason, now=now)
                self._publish_locked(locked)
                self._next_refresh_at = None
            raise AuthRequired(error.reason) from None

        now = self._clock()
        with self._lock:
            if self._record is not record:
                return self._snapshot
            current = self._snapshot
            try:
                if current.state is AuthState.AUTHENTICATED:
                    snapshot = current.refreshed(status, now=now)
                else:
                    snapshot = RuntimeSnapshot.from_session_status(
                        status,
                        now=now,
                        runtime_instance_id=current.runtime_instance_id,
                        epoch=current.epoch + 1,
                        principal_key=record.principal_key,
                    )
            except AuthRequired as error:
                self._publish_locked(current.locked(error.reason or error.code, now=now))
                self._next_refresh_at = None
                raise
            shortened_record = CookieRecord(
                cookies=dict(record.cookies),
                username=status.username,
                session_expires_at=snapshot.session_expires_at or status.session_expires_at,
                principal_key=record.principal_key,
            )
            if shortened_record != record:
                try:
                    self._secret_backend.write(_encode_cookie_blob(shortened_record))
                except Exception:
                    reason = (
                        "vault_unavailable" if self._vault_required else "runtime_unavailable"
                    )
                    self._publish_locked(current.locked(reason, now=now))
                    self._next_refresh_at = None
                    raise AuthRequired(reason) from None
                self._record = shortened_record
            self._publish_locked(snapshot)
            self._schedule_locked(now)
            return snapshot

    def logout(self) -> RuntimeSnapshot:
        with self._lock:
            record = self._record
            current = self._snapshot
            signed_out = RuntimeSnapshot.signed_out(
                epoch=current.epoch + 1,
                reason="signed_out",
            )
            tombstone = SignedOutTombstone()
            persisted = self._persist_signed_out_tombstone_locked(tombstone)
            self._record = tombstone
            self._record_loaded = True
            self._next_refresh_at = None
            self._publish_locked(signed_out)
        if record is not None:
            self._best_effort_remote_logout(record)
        if not persisted:
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            raise AuthRequired(reason)
        return signed_out

    def close(self) -> None:
        now = self._clock()
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            self._next_refresh_at = None
            self._publish_locked(self._snapshot.locked("runtime_unavailable", now=now))

    def maintenance(self) -> bool:
        now = self._clock()
        with self._lock:
            if not self._alive:
                return False
            if now - self._last_authenticated_activity >= OWNER_IDLE_SECONDS:
                self.close()
                return False
            refresh_due = (
                self._next_refresh_at is not None and now >= self._next_refresh_at
            )
            record = self._record
        if refresh_due:
            # An in-process legacy CookieRecord refreshes through the cookie
            # refresh path; validate_now() would return without rescheduling
            # and leave the due timestamp in the past, busy-looping at the
            # broker's 2 Hz maintenance tick.
            if isinstance(record, (NativeCredentialRecord, LegacyCredentialRecord)):
                self.validate_now()
            else:
                self.refresh()
        return True

    def _record_authenticated_activity(self) -> None:
        with self._lock:
            if self._alive and self._snapshot.state is AuthState.AUTHENTICATED:
                self._last_authenticated_activity = self._clock()

    def _check_login_rate_limit(self) -> None:
        now = self._clock()
        with self._lock:
            cutoff = now - LOGIN_ATTEMPT_WINDOW_SECONDS
            self._failed_login_attempts = [
                attempt for attempt in self._failed_login_attempts if attempt > cutoff
            ]
            limited = len(self._failed_login_attempts) >= LOGIN_ATTEMPT_LIMIT
        if limited:
            self._lock_with_reason("rate_limited")
            raise AuthRequired("rate_limited")

    def _record_failed_login(self) -> None:
        with self._lock:
            self._failed_login_attempts.append(self._clock())

    def _prepare_cookie_acquisition(self) -> None:
        if self._hardener is None:
            return
        try:
            self._hardener.apply_required()
        except AuthRequired:
            self._lock_with_reason("runtime_unavailable")
            raise
        except Exception:
            self._lock_with_reason("runtime_unavailable")
            raise AuthRequired("runtime_unavailable") from None

    def _load_record(
        self,
    ) -> CookieRecord | NativeCredentialRecord | LegacyCredentialRecord | RevocationTombstone | SignedOutTombstone | None:
        needs_migration = False
        with self._lock:
            if self._record_loaded:
                return self._record
        try:
            raw = self._secret_backend.read()
        except Exception:
            reason = "vault_unavailable" if self._vault_required else "runtime_unavailable"
            with self._lock:
                if self._record_loaded:
                    return self._record
                locked = self._snapshot.locked(reason, now=self._clock())
                self._publish_locked(locked)
                self._next_refresh_at = None
            raise AuthRequired(reason) from None
        if raw:
            try:
                decoded = _decode_credential_blob(raw)
                if isinstance(decoded, CookieRecord):
                    needs_migration = json.loads(raw).get("version") == 1
                    record = LegacyCredentialRecord(
                        cookie_record=decoded,
                        principal_key=(
                            decoded.principal_key
                            if _is_legacy_principal_key(decoded.principal_key)
                            else "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
                        ),
                    )
                else:
                    record = decoded
            except AuthRequired:
                try:
                    self._secret_backend.delete()
                except Exception:
                    reason = (
                        "vault_unavailable"
                        if self._vault_required
                        else "runtime_unavailable"
                    )
                    with self._lock:
                        if self._record_loaded:
                            return self._record
                        locked = self._snapshot.locked(reason, now=self._clock())
                        self._publish_locked(locked)
                        self._next_refresh_at = None
                    raise AuthRequired(reason) from None
                record = None
        else:
            record = None
        with self._lock:
            if self._record_loaded:
                return self._record
            if isinstance(record, LegacyCredentialRecord) and needs_migration:
                try:
                    self._secret_backend.write(_encode_cookie_blob(record.cookie_record))
                except Exception:
                    reason = (
                        "vault_unavailable"
                        if self._vault_required
                        else "runtime_unavailable"
                    )
                    locked = self._snapshot.locked(reason, now=self._clock())
                    self._publish_locked(locked)
                    self._next_refresh_at = None
                    raise AuthRequired(reason) from None
            self._record = record
            self._record_loaded = True
            if isinstance(record, NativeCredentialRecord):
                self._publish_locked(RuntimeSnapshot.from_native_credential(record))
                self._validation_failures = 0
                self._schedule_validation_locked(self._clock())
            elif isinstance(record, LegacyCredentialRecord):
                self._publish_locked(RuntimeSnapshot.from_legacy_record(record))
                self._validation_failures = 0
                self._schedule_validation_locked(self._clock())
            elif isinstance(record, RevocationTombstone):
                self._publish_locked(self._snapshot.locked(record.reason, now=self._clock()))
                self._next_refresh_at = None
            elif isinstance(record, SignedOutTombstone):
                if record.reason == "signed_out":
                    self._publish_locked(
                        RuntimeSnapshot.signed_out(reason="signed_out")
                    )
                else:
                    self._publish_locked(
                        self._snapshot.locked(record.reason, now=self._clock())
                    )
                self._next_refresh_at = None
            return record

    def validate_now(self) -> RuntimeSnapshot:
        """Validate a cached principal without making outages terminal locally."""
        if not self._refresh_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            return self._validate_now_locked()
        finally:
            self._refresh_lock.release()

    def _validate_now_locked(
        self,
        native_context: tuple[str, str] | None = None,
    ) -> RuntimeSnapshot:
        record = self._load_record()
        if not isinstance(record, (NativeCredentialRecord, LegacyCredentialRecord)):
            return self.snapshot()
        with self._lock:
            if self._record is not record or self._snapshot.state is not AuthState.AUTHENTICATED:
                return self._snapshot
            self._publish_locked(self._snapshot.validating())
        try:
            if isinstance(record, NativeCredentialRecord):
                status = self._client.client_session_status(record.credential)
                if (
                    status.account_id != record.credential.account_id
                    or status.session_id != record.credential.session_id
                    or status.installation_id != record.credential.installation_id
                    or status.username != record.credential.username
                ):
                    raise AuthServiceError("invalid_response")
                updated = NativeCredentialRecord(
                    record.credential,
                    status.server_time,
                    record.predecessor_principal_key,
                )
                with self._lock:
                    if self._record is not record:
                        return self._snapshot
                    self._replace_record_atomically(updated)
                    self._record = updated
                    snapshot = self._snapshot.online(last_validated_at=status.server_time)
                    self._validation_failures = 0
                    self._publish_locked(snapshot)
                    self._schedule_validation_locked(self._clock())
                    return self._snapshot

            status = self._client.legacy_status(record.cookie_record.cookies)
            if status.username != record.cookie_record.username:
                raise AuthServiceError("invalid_response")

            if native_context is not None:
                # A foreground caller supplied its real, already-validated
                # installation/client context, so the proven upgrade can run:
                # never fabricate an installation id here.
                installation_id, client_version = native_context
                credential = self._client.issue_client_session(
                    record.cookie_record.cookies,
                    installation_id=installation_id,
                    client_version=client_version,
                )
                if (
                    credential.installation_id != installation_id
                    or credential.username != record.cookie_record.username
                ):
                    raise AuthServiceError("invalid_response")
                updated = NativeCredentialRecord(
                    credential,
                    status.server_time,
                    record.principal_key,
                )
                with self._lock:
                    if self._record is not record:
                        return self._snapshot
                    self._replace_record_atomically(updated)
                    self._record = updated
                    snapshot = RuntimeSnapshot.from_native_credential(
                        updated,
                        runtime_instance_id=self._snapshot.runtime_instance_id,
                        epoch=self._snapshot.epoch,
                    ).online(last_validated_at=status.server_time)
                    self._validation_failures = 0
                    self._publish_locked(snapshot)
                    self._schedule_validation_locked(self._clock())
                    return self._snapshot

            # Background validation never mints a native session with a
            # fabricated installation id: without a caller-supplied context
            # the record stays legacy-only.
            with self._lock:
                if self._record is not record:
                    return self._snapshot
                snapshot = self._snapshot.online(last_validated_at=status.server_time)
                self._validation_failures = 0
                self._publish_locked(snapshot)
                self._schedule_validation_locked(self._clock())
                return self._snapshot
        except ExplicitSessionRevocation as error:
            return self._handle_explicit_revocation(record, error)
        except (AuthServiceError, AuthRequired) as error:
            reason = error.reason or "runtime_unavailable"
        except Exception:
            reason = "runtime_unavailable"
        else:
            return self.snapshot()
        with self._lock:
            if self._record is record and self._snapshot.state is AuthState.AUTHENTICATED:
                self._validation_failures += 1
                self._publish_locked(self._snapshot.degraded(reason))
                self._schedule_validation_locked(self._clock())
            return self._snapshot

    def _handle_explicit_revocation(
        self,
        record: NativeCredentialRecord | LegacyCredentialRecord,
        error: ExplicitSessionRevocation,
    ) -> RuntimeSnapshot:
        with self._lock:
            current = self._snapshot
            if (
                not isinstance(record, NativeCredentialRecord)
                or self._record is not record
                or current.account_id != error.account_id
                or current.session_id != error.session_id
            ):
                return self._snapshot
            tombstone = RevocationTombstone(
                account_id=error.account_id,
                session_id=error.session_id,
                reason=error.code,
                revoked_at=error.revoked_at,
            )
            try:
                self._secret_backend.write(_encode_tombstone_blob(tombstone))
                if _decode_credential_blob(self._secret_backend.read() or "") != tombstone:
                    raise AuthRequired("runtime_unavailable")
            except Exception:
                try:
                    self._secret_backend.delete()
                except Exception:
                    pass
            self._record = tombstone
            self._record_loaded = True
            self._next_refresh_at = None
            self._publish_locked(current.locked(error.code, now=self._clock()))
            return self._snapshot

    def _replace_record_atomically(self, record: NativeCredentialRecord) -> None:
        """Write/read-back v2 before replacing a v1 record in memory."""
        raw = _encode_native_blob(record)
        previous: str | None
        try:
            previous = self._secret_backend.read()
        except Exception:
            raise AuthServiceError("vault_unavailable") from None
        try:
            self._secret_backend.write(raw)
            if _decode_credential_blob(self._secret_backend.read() or "") != record:
                raise AuthRequired("runtime_unavailable")
        except Exception:
            if previous is not None:
                try:
                    self._secret_backend.write(previous)
                except Exception:
                    pass
            raise AuthServiceError("vault_unavailable") from None

    def _persist_signed_out_tombstone_locked(
        self,
        tombstone: SignedOutTombstone,
    ) -> bool:
        """Commit logout denial, falling back to removing the old credential."""
        try:
            self._secret_backend.write(_encode_signed_out_tombstone_blob(tombstone))
            if _decode_credential_blob(self._secret_backend.read() or "") != tombstone:
                raise AuthRequired("runtime_unavailable")
            return True
        except Exception:
            try:
                self._secret_backend.delete()
            except Exception:
                return False
            return True

    def _schedule_validation_locked(self, now: float) -> None:
        # Clamp the exponent before exponentiation: a long outage would
        # otherwise overflow float conversion and kill the owner broker.
        cap = min(300.0, 2.0 ** min(self._validation_failures, 16))
        delay = self._jitter(0.0, cap)
        if not isinstance(delay, (float, int)) or isinstance(delay, bool) or not 0.0 <= delay <= cap:
            delay = cap
        self._next_refresh_at = now + float(delay)

    def _schedule_locked(self, now: float) -> None:
        delay = self._jitter(57.0, 60.0)
        if not 57.0 <= delay <= 60.0:
            self._publish_locked(self._snapshot.locked("runtime_unavailable", now=now))
            self._next_refresh_at = None
            raise AuthRequired("runtime_unavailable")
        self._next_refresh_at = min(now + delay, self._snapshot.valid_until)

    def _lock_with_reason(self, reason: str) -> RuntimeSnapshot:
        now = self._clock()
        with self._lock:
            locked = self._snapshot.locked(reason, now=now)
            self._publish_locked(locked)
            self._next_refresh_at = None
            return locked

    def _publish_locked(self, snapshot: RuntimeSnapshot) -> None:
        if self._on_transition is not None:
            self._on_transition(snapshot)
        self._snapshot = snapshot
        retained: list[RuntimeConsumer] = []
        for consumer in self._consumers:
            try:
                consumer.publish(snapshot)
            except AuthRequired:
                continue
            retained.append(consumer)
        self._consumers = retained

    def _best_effort_remote_logout(
        self,
        record: CookieRecord | NativeCredentialRecord | LegacyCredentialRecord | RevocationTombstone | SignedOutTombstone,
    ) -> None:
        try:
            if isinstance(record, NativeCredentialRecord):
                self._client.logout_client_session(record.credential)
            elif isinstance(record, LegacyCredentialRecord):
                self._client.legacy_logout(record.cookie_record.cookies)
            elif isinstance(record, CookieRecord):
                self._client.legacy_logout(record.cookies)
        except AuthServiceError:
            return


class VaultOwner(_OwnerCore):
    SERVICE = _KeyringSecretBackend.SERVICE
    ACCOUNT = _KeyringSecretBackend.ACCOUNT

    def __init__(
        self,
        client: AuthClient,
        *,
        secret_backend: _SecretBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] | None = None,
    ) -> None:
        random_source = secrets.SystemRandom()
        super().__init__(
            client,
            secret_backend=secret_backend or _KeyringSecretBackend(),
            hardener=None,
            vault_required=True,
            clock=clock,
            jitter=jitter or random_source.uniform,
            on_transition=None,
        )


class MemoryOwner(_OwnerCore):
    def __init__(
        self,
        client: AuthClient,
        *,
        hardener: _Hardener,
        secret_backend: _SecretBackend | None = None,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[float, float], float] | None = None,
        on_transition: Callable[[RuntimeSnapshot], None] | None = None,
    ) -> None:
        random_source = secrets.SystemRandom()
        super().__init__(
            client,
            secret_backend=secret_backend or _MemorySecretBackend(),
            hardener=hardener,
            vault_required=False,
            clock=clock,
            jitter=jitter or random_source.uniform,
            on_transition=on_transition,
        )


class _EntryPointOwner(Protocol):
    def refresh(self) -> RuntimeSnapshot: ...

    def login(
        self,
        username: str,
        password: bytearray,
        *,
        installation_id: str | None = None,
        client_version: str | None = None,
    ) -> RuntimeSnapshot: ...

    def logout(self) -> RuntimeSnapshot: ...

    def trace_token(
        self,
        *,
        installation_id: str,
        client_version: str,
        telemetry_schema_version: str,
    ) -> TraceCredential: ...

    def snapshot(self) -> RuntimeSnapshot: ...

    def enable_desktop_local_continuity(self) -> RuntimeSnapshot: ...

    def connect_consumer(
        self,
        *,
        profile: str | None = None,
        allow_local_continuity: bool = False,
    ) -> RuntimeConsumer: ...


_RUNTIME_FRAME_LIMIT = 65_536
_RUNTIME_PROTOCOL_VERSION = 3
_RUNTIME_SUPPORTED_PROTOCOL_VERSIONS = (1, 2, 3)
_TRACE_INSTALLATION_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class RemoteRuntimeConsumer:
    def __init__(
        self,
        owner: RemoteRuntimeOwner,
        snapshot: RuntimeSnapshot,
        *,
        allow_local_continuity: bool = False,
    ) -> None:
        self._owner = owner
        self._snapshot = snapshot
        self._allow_local_continuity = allow_local_continuity
        self._lock = threading.RLock()

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def publish(self, snapshot: RuntimeSnapshot) -> None:
        with self._lock:
            self._snapshot = snapshot

    def require_authorized(
        self,
        boundary: str,
        *,
        expected: AuthScope | None = None,
        now: float | None = None,
    ) -> AuthScope:
        del now
        current = self.snapshot()
        required = expected or current.scope
        snapshot = self._owner.authorize(
            boundary,
            expected=required,
            allow_local_continuity=self._allow_local_continuity,
        )
        self.publish(snapshot)
        return snapshot.scope


class _OwnerProtocolRejected(Exception):
    """An old owner rejected our protocol version before executing anything."""


class RemoteRuntimeOwner:
    """A same-OS-user client for the single native auth owner."""

    def __init__(self, endpoint: UnixEndpoint | WindowsNamedPipeEndpoint) -> None:
        self._endpoint = endpoint
        self._snapshot = RuntimeSnapshot.signed_out(reason="runtime_unavailable")
        self._lock = threading.RLock()
        self._protocol_version = _RUNTIME_PROTOCOL_VERSION

    def refresh(
        self,
        *,
        timeout: float = _RUNTIME_REQUEST_TIMEOUT_SECONDS,
        installation_id: str | None = None,
        client_version: str | None = None,
    ) -> RuntimeSnapshot:
        def params() -> dict[str, object]:
            request: dict[str, object] = {"operation": "status"}

            # An old owner cannot perform the upgrade and rejects unknown
            # request keys, so the context rides only on protocol v2.
            if (
                self._protocol_version >= 2
                and installation_id is not None
                and client_version is not None
            ):
                request.update(
                    installation_id=installation_id,
                    client_version=client_version,
                )
            return request

        while True:
            try:
                return self._exchange(self._encode_request(params()), timeout=timeout)
            except _OwnerProtocolRejected:
                if not self._downgrade_protocol():
                    raise AuthRequired("runtime_unavailable") from None

    def snapshot(self) -> RuntimeSnapshot:
        with self._lock:
            return self._snapshot

    def login(
        self,
        username: str,
        password: bytearray,
        *,
        installation_id: str | None = None,
        client_version: str | None = None,
    ) -> RuntimeSnapshot:
        if (installation_id is None) != (client_version is None):
            raise AuthRequired("runtime_unavailable")
        if installation_id is not None and (
            _TRACE_INSTALLATION_ID.fullmatch(installation_id) is None
            or not isinstance(client_version, str)
            or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", client_version) is None
        ):
            raise AuthRequired("runtime_unavailable")
        try:
            password_text = password.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            raise AuthRequired("invalid_credentials") from None
        request: dict[str, object] = {
            "operation": "login",
            "username": username,
            "password": password_text,
        }
        if installation_id is not None and client_version is not None:
            request.update(
                installation_id=installation_id,
                client_version=client_version,
            )
        password_text = ""
        try:
            while True:
                encoded = self._encode_request(request)
                try:
                    try:
                        return self._exchange(
                            encoded,
                            timeout=_RUNTIME_LOGIN_TIMEOUT_SECONDS,
                        )
                    except _OwnerProtocolRejected:
                        if not self._downgrade_protocol():
                            raise AuthRequired("runtime_unavailable") from None
                finally:
                    encoded[:] = b"\0" * len(encoded)
        finally:
            request["password"] = ""

    def logout(self) -> RuntimeSnapshot:
        return self._request({"operation": "logout"})

    def enable_desktop_local_continuity(self) -> RuntimeSnapshot:
        return self._request({"operation": "enable_desktop_local_continuity"})

    def trace_token(
        self,
        *,
        installation_id: str,
        client_version: str,
        telemetry_schema_version: str,
    ) -> TraceCredential:
        request: dict[str, object] = {
            "operation": "trace_token",
            "installation_id": installation_id,
            "client_version": client_version,
            "telemetry_schema_version": telemetry_schema_version,
        }
        while True:
            try:
                response = self._exchange_response(
                    self._encode_request(request),
                    timeout=_RUNTIME_REQUEST_TIMEOUT_SECONDS,
                )
                break
            except _OwnerProtocolRejected:
                if not self._downgrade_protocol():
                    raise AuthRequired("runtime_unavailable") from None
        if set(response) != {"version", "ok", "credential"}:
            raise AuthRequired("runtime_unavailable")
        return _trace_credential_from_wire(
            response.get("credential"),
            expected_installation_id=installation_id,
        )

    def connect_consumer(
        self,
        *,
        profile: str | None = None,
        allow_local_continuity: bool = False,
    ) -> RemoteRuntimeConsumer:
        del profile
        return RemoteRuntimeConsumer(
            self,
            self.snapshot(),
            allow_local_continuity=allow_local_continuity,
        )

    def authorize(
        self,
        boundary: str,
        *,
        expected: AuthScope,
        allow_local_continuity: bool = False,
    ) -> RuntimeSnapshot:
        return self._request(
            {
                "operation": "authorize",
                "boundary": boundary,
                "expected": {
                    "runtime_instance_id": expected.runtime_instance_id,
                    "epoch": expected.epoch,
                },
                "allow_local_continuity": allow_local_continuity,
            }
        )

    def _request(
        self,
        params: dict[str, object],
        *,
        timeout: float = _RUNTIME_REQUEST_TIMEOUT_SECONDS,
    ) -> RuntimeSnapshot:
        while True:
            try:
                return self._exchange(self._encode_request(params), timeout=timeout)
            except _OwnerProtocolRejected:
                if not self._downgrade_protocol():
                    raise AuthRequired("runtime_unavailable") from None

    def _downgrade_protocol(self) -> bool:
        with self._lock:
            if self._protocol_version <= 1:
                return False
            self._protocol_version -= 1
            return True

    def _encode_request(self, params: dict[str, object]) -> bytearray:
        request = {
            "version": self._protocol_version,
            **params,
        }
        encoded = bytearray(
            (
                json.dumps(request, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        )
        if len(encoded) > _RUNTIME_FRAME_LIMIT:
            encoded[:] = b"\0" * len(encoded)
            raise AuthRequired("runtime_unavailable")
        return encoded

    def _exchange(
        self,
        encoded: bytearray,
        *,
        timeout: float,
    ) -> RuntimeSnapshot:
        response = self._exchange_response(encoded, timeout=timeout)
        if set(response) != {"version", "ok", "snapshot"}:
            raise AuthRequired("runtime_unavailable")
        snapshot = _snapshot_from_public(response.get("snapshot"))
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _exchange_response(
        self,
        encoded: bytearray,
        *,
        timeout: float,
    ) -> dict[str, object]:
        if timeout <= 0:
            encoded[:] = b"\0" * len(encoded)
            raise AuthRequired("runtime_unavailable")
        if isinstance(self._endpoint, WindowsNamedPipeEndpoint):
            connection = self._endpoint.connect_current()
        else:
            connection: Any = self._endpoint.connect_current(timeout=timeout)
        try:
            set_timeout = getattr(connection, "settimeout", None)
            if callable(set_timeout):
                set_timeout(timeout)
            connection.sendall(encoded)
            encoded[:] = b"\0" * len(encoded)
            raw = _read_runtime_frame(
                connection,
                timeout=(
                    None
                    if isinstance(self._endpoint, WindowsNamedPipeEndpoint)
                    else timeout
                ),
            )
        except (AuthRequired, OSError, TimeoutError):
            raise AuthRequired("runtime_unavailable") from None
        finally:
            encoded[:] = b"\0" * len(encoded)
            connection.close()
        try:
            response = json.loads(raw)
        except (UnicodeError, ValueError):
            raise AuthRequired("runtime_unavailable") from None
        if (
            not isinstance(response, dict)
            or response.get("version") not in _RUNTIME_SUPPORTED_PROTOCOL_VERSIONS
        ):
            raise AuthRequired("runtime_unavailable")
        if response.get("ok") is not True:
            reason = response.get("reason")
            if not isinstance(reason, str) or not reason:
                reason = "runtime_unavailable"
            # An old owner rejects an unknown protocol version before it
            # executes the operation, always answering a version-1 error
            # frame. Signal the caller to retry once on protocol v1.
            if (
                self._protocol_version > 1
                and response.get("version") == 1
                and reason == "runtime_unavailable"
            ):
                raise _OwnerProtocolRejected()
            if reason == "scope_changed":
                raise AuthScopeChanged("runtime_unavailable")
            raise AuthRequired(reason)
        return response


class OwnerBroker:
    """Bounded native IPC broker for one VaultOwner or MemoryOwner."""

    _MAX_WORKERS = 32

    def __init__(
        self,
        owner: _EntryPointOwner,
        endpoint: UnixEndpoint | WindowsNamedPipeEndpoint,
        server: UnixOwnerServer | WindowsOwnerServer,
        owner_lock: UnixOwnerLock | None,
    ) -> None:
        self._owner = owner
        self._endpoint = endpoint
        self._server = server
        self._owner_lock = owner_lock
        self._stop = threading.Event()
        self._close_lock = threading.Lock()
        self._closed = False
        self._server_lock = threading.RLock()
        self._connections_lock = threading.RLock()
        self._connections: set[Any] = set()
        self._operation_lock = threading.Lock()
        self._worker_slots = threading.BoundedSemaphore(self._MAX_WORKERS)
        self._thread = threading.Thread(
            target=self._serve,
            name="hermes-auth-owner",
            daemon=True,
        )
        self._maintenance_thread = threading.Thread(
            target=self._maintain,
            name="hermes-auth-maintenance",
            daemon=True,
        )

    @classmethod
    def start(
        cls,
        owner: _EntryPointOwner,
        *,
        endpoint: UnixEndpoint | WindowsNamedPipeEndpoint | None = None,
    ) -> OwnerBroker:
        selected = endpoint or runtime_endpoint()
        owner_lock: UnixOwnerLock | None = None
        if isinstance(selected, UnixEndpoint):
            owner_lock = selected.acquire_owner_lock()
            try:
                server = selected.bind_owner(owner_lock)
            except Exception:
                owner_lock.close()
                raise
        else:
            server = selected.bind_owner()
        broker = cls(owner, selected, server, owner_lock)
        broker._thread.start()
        broker._maintenance_thread.start()
        return broker

    @property
    def endpoint(self) -> UnixEndpoint | WindowsNamedPipeEndpoint:
        return self._endpoint

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            with self._server_lock:
                self._server.close()
            with self._connections_lock:
                connections = tuple(self._connections)
            for connection in connections:
                try:
                    connection.close()
                except Exception:
                    continue
            if self._owner_lock is not None:
                self._owner_lock.close()

    def wait(self) -> None:
        self._stop.wait()

    def _serve(self) -> None:
        try:
            while not self._stop.is_set():
                with self._server_lock:
                    server = self._server
                try:
                    connection = server.accept()
                except AuthRequired:
                    return

                if isinstance(self._endpoint, WindowsNamedPipeEndpoint):
                    self._serve_connection(connection)
                    if self._stop.is_set():
                        return
                    try:
                        replacement_server = self._endpoint.bind_owner()
                    except AuthRequired:
                        return
                    with self._server_lock:
                        if self._stop.is_set():
                            replacement_server.close()
                            return
                        self._server = replacement_server
                    continue

                if not self._worker_slots.acquire(blocking=False):
                    try:
                        connection.sendall(
                            b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
                        )
                    except Exception:
                        pass
                    finally:
                        connection.close()
                    continue

                with self._connections_lock:
                    if self._stop.is_set():
                        connection.close()
                        self._worker_slots.release()
                        return
                    self._connections.add(connection)
                try:
                    worker = threading.Thread(
                        target=self._serve_connection,
                        args=(connection, True),
                        name="hermes-auth-owner-client",
                        daemon=True,
                    )
                    worker.start()
                except BaseException:
                    connection.close()
                    with self._connections_lock:
                        self._connections.discard(connection)
                    self._worker_slots.release()
                    continue
        finally:
            self._stop.set()

    def _serve_connection(
        self,
        connection: Any,
        worker_slot_acquired: bool = False,
    ) -> None:
        try:
            try:
                timeout = (
                    _RUNTIME_SERVER_READ_TIMEOUT_SECONDS
                    if isinstance(self._endpoint, UnixEndpoint)
                    else None
                )
                raw = _read_runtime_frame(connection, timeout=timeout)
                response = self._dispatch(raw)
                encoded = (
                    json.dumps(response, sort_keys=True, separators=(",", ":"))
                    + "\n"
                ).encode("utf-8")
                if len(encoded) > _RUNTIME_FRAME_LIMIT:
                    raise AuthRequired("runtime_unavailable")
                connection.sendall(encoded)
            except Exception:
                try:
                    connection.sendall(
                        b'{"ok":false,"reason":"runtime_unavailable","version":1}\n'
                    )
                except Exception:
                    pass
        finally:
            connection.close()
            with self._connections_lock:
                self._connections.discard(connection)
            if worker_slot_acquired:
                self._worker_slots.release()

    def _maintain(self) -> None:
        maintenance = getattr(self._owner, "maintenance", None)
        while not self._stop.wait(0.5):
            if maintenance is None:
                continue
            try:
                keep_running = maintenance()
                if keep_running is False:
                    self.close()
                    return
            except AuthRequired:
                continue
            except Exception:
                self.close()
                return

    def _dispatch(self, raw: bytes) -> dict[str, object]:
        try:
            request = json.loads(raw)
        except (UnicodeError, ValueError):
            return _runtime_error("runtime_unavailable")
        if not isinstance(request, dict):
            return _runtime_error("runtime_unavailable")
        version = request.get("version")
        if version not in _RUNTIME_SUPPORTED_PROTOCOL_VERSIONS:
            return _runtime_error("runtime_unavailable")
        operation = request.get("operation")
        operation_locked = False
        if (
            isinstance(self._endpoint, UnixEndpoint)
            and operation in {"login", "logout", "trace_token"}
        ):
            wait_seconds = (
                _RUNTIME_LOGIN_OPERATION_WAIT_SECONDS
                if operation in {"login", "trace_token"}
                else _RUNTIME_LOGOUT_OPERATION_WAIT_SECONDS
            )
            operation_locked = self._operation_lock.acquire(timeout=wait_seconds)
            if not operation_locked:
                if operation == "login":
                    request["password"] = ""
                return _runtime_error("runtime_unavailable", version=version)
        try:
            if operation == "status" and set(request) == {"version", "operation"}:
                snapshot = self._owner.refresh()
            elif operation == "status" and set(request) == {
                "version",
                "operation",
                "installation_id",
                "client_version",
            }:
                installation_id = request.get("installation_id")
                client_version = request.get("client_version")
                if (
                    not isinstance(installation_id, str)
                    or _TRACE_INSTALLATION_ID.fullmatch(installation_id) is None
                    or not isinstance(client_version, str)
                    or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", client_version) is None
                ):
                    raise AuthRequired("runtime_unavailable")
                snapshot = self._owner.refresh(
                    installation_id=installation_id,
                    client_version=client_version,
                )
            elif operation == "logout" and set(request) == {"version", "operation"}:
                snapshot = self._owner.logout()  # type: ignore[attr-defined]
            elif version >= 3 and operation == "enable_desktop_local_continuity" and set(request) == {
                "version",
                "operation",
            }:
                snapshot = self._owner.enable_desktop_local_continuity()
            elif operation == "login" and set(request) in (
                {"version", "operation", "username", "password"},
                {
                    "version",
                    "operation",
                    "username",
                    "password",
                    "installation_id",
                    "client_version",
                },
            ):
                username = request.get("username")
                password_text = request.get("password")
                installation_id = request.get("installation_id")
                client_version = request.get("client_version")
                if (
                    not isinstance(username, str)
                    or not isinstance(password_text, str)
                    or (installation_id is None) != (client_version is None)
                    or (
                        installation_id is not None
                        and (
                            not isinstance(installation_id, str)
                            or _TRACE_INSTALLATION_ID.fullmatch(installation_id) is None
                            or not isinstance(client_version, str)
                            or re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", client_version) is None
                        )
                    )
                ):
                    raise AuthRequired("invalid_credentials")
                password = bytearray(password_text.encode("utf-8"))
                request["password"] = ""
                password_text = ""
                try:
                    snapshot = self._owner.login(
                        username,
                        password,
                        installation_id=installation_id,
                        client_version=client_version,
                    )
                finally:
                    password[:] = b"\0" * len(password)
            elif version >= 3 and operation == "authorize" and set(request) == {
                "version",
                "operation",
                "boundary",
                "expected",
                "allow_local_continuity",
            }:
                boundary = request.get("boundary")
                expected = _scope_from_wire(request.get("expected"))
                allow_local_continuity = request.get("allow_local_continuity")
                if (
                    not isinstance(boundary, str)
                    or not 0 < len(boundary) <= 256
                    or not isinstance(allow_local_continuity, bool)
                ):
                    raise AuthRequired("runtime_unavailable")
                snapshot = self._owner.snapshot()
                consumer = self._owner.connect_consumer(
                    allow_local_continuity=allow_local_continuity
                )
                consumer.require_authorized(boundary, expected=expected)
            elif version <= 2 and operation == "authorize" and set(request) == {
                "version",
                "operation",
                "boundary",
                "expected",
            }:
                boundary = request.get("boundary")
                expected = _scope_from_wire(request.get("expected"))
                if not isinstance(boundary, str) or not 0 < len(boundary) <= 256:
                    raise AuthRequired("runtime_unavailable")
                snapshot = self._owner.snapshot()
                consumer = self._owner.connect_consumer()
                consumer.require_authorized(boundary, expected=expected)
            elif operation == "trace_token" and set(request) == {
                "version",
                "operation",
                "installation_id",
                "client_version",
                "telemetry_schema_version",
            }:
                installation_id = request.get("installation_id")
                client_version = request.get("client_version")
                telemetry_schema_version = request.get("telemetry_schema_version")
                if not all(
                    isinstance(value, str)
                    for value in (
                        installation_id,
                        client_version,
                        telemetry_schema_version,
                    )
                ):
                    raise AuthRequired("runtime_unavailable")
                credential = self._owner.trace_token(
                    installation_id=installation_id,
                    client_version=client_version,
                    telemetry_schema_version=telemetry_schema_version,
                )
                return {
                    "version": version,
                    "ok": True,
                    "credential": _trace_credential_to_wire(credential),
                }
            else:
                raise AuthRequired("runtime_unavailable")
        except AuthScopeChanged:
            return _runtime_error("scope_changed", version=version)
        except AuthRequired as error:
            return _runtime_error(error.reason or error.code, version=version)
        except Exception:
            return _runtime_error("runtime_unavailable", version=version)
        finally:
            if operation_locked:
                self._operation_lock.release()
        return {
            "version": version,
            "ok": True,
            "snapshot": (
                snapshot.public_dict()
                if version >= 3
                else (
                    snapshot.public_dict_v2()
                    if version == 2
                    else snapshot.public_dict_v1()
                )
            ),
        }


def connect_runtime_owner(
    *,
    endpoint: UnixEndpoint | WindowsNamedPipeEndpoint | None = None,
    timeout: float = _RUNTIME_REQUEST_TIMEOUT_SECONDS,
) -> RemoteRuntimeOwner:
    remote = RemoteRuntimeOwner(endpoint or runtime_endpoint())
    snapshot = remote.refresh(timeout=timeout)
    if (
        snapshot.state is not AuthState.AUTHENTICATED
        and snapshot.reason == "runtime_unavailable"
    ):
        raise AuthRequired("runtime_unavailable")
    return remote


def start_runtime_owner(
    *,
    timeout: float = 5.0,
    probe_first: bool = True,
) -> RemoteRuntimeOwner:
    """Start the fixed auth-only owner process and wait for its native endpoint."""
    deadline = time.monotonic() + max(0.1, timeout)
    if probe_first:
        try:
            return connect_runtime_owner(
                timeout=min(
                    _RUNTIME_RECOVERY_PROBE_SECONDS,
                    max(0.1, deadline - time.monotonic()),
                )
            )
        except AuthRequired:
            pass
    if time.monotonic() >= deadline:
        raise AuthRequired("runtime_unavailable")
    environment = _owner_process_environment()
    owner_executable = _runtime_owner_executable(sys.executable)
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": environment,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                owner_executable,
                "-m",
                "hermes_cli.client_auth.runtime",
                "owner",
            ],
            **kwargs,
        )
    except Exception:
        raise AuthRequired("runtime_unavailable") from None

    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            return connect_runtime_owner(
                timeout=min(
                    _RUNTIME_RECOVERY_PROBE_SECONDS,
                    max(0.05, remaining),
                )
            )
        except AuthRequired:
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
    raise AuthRequired("runtime_unavailable")


def _runtime_owner_executable(
    executable: str,
    *,
    is_windows: bool | None = None,
    is_file: Callable[[str], bool] = os.path.isfile,
) -> str:
    """Use the GUI Python sibling for the detached Windows auth owner.

    A Windows venv ``python.exe`` is a launcher shim.  Even with
    ``DETACHED_PROCESS`` the shim can re-exec a console interpreter and leave
    a visible cmd window behind.  The auth owner has no terminal UI, so the
    sibling ``pythonw.exe`` keeps the broker detached without a console.
    """

    windows = os.name == "nt" if is_windows is None else is_windows
    if not windows:
        return executable

    name = ntpath.basename(executable).casefold()
    if name == "pythonw.exe" or name != "python.exe":
        return executable

    candidate = ntpath.join(ntpath.dirname(executable), "pythonw.exe")
    return candidate if is_file(candidate) else executable


def _owner_process_environment() -> dict[str, str]:
    exact = {
        "APPDATA",
        "DISPLAY",
        "HERMES_AUTH_KEYRING_SERVICE",
        "HERMES_AUTH_LEGACY_KEYRING_SERVICE",
        "HERMES_AUTH_RUNTIME_NAMESPACE",
        "HERMES_HOME",
        "HOME",
        "KUBERNETES_SERVICE_HOST",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "SSH_CONNECTION",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_RUNTIME_DIR",
        "container",
    }
    return {
        key: value
        for key, value in os.environ.items()
        if key in exact and isinstance(value, str)
    }


def _owner_election_context() -> OwnerElectionContext:
    ssh_connection = bool(os.environ.get("SSH_CONNECTION"))
    containerized = bool(
        os.environ.get("container")
        or os.environ.get("KUBERNETES_SERVICE_HOST")
        or os.path.exists("/.dockerenv")
    )
    graphical_session = not ssh_connection and not containerized and (
        sys.platform in {"darwin", "win32"}
        or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    )
    return OwnerElectionContext(
        ssh_connection=ssh_connection,
        containerized=containerized,
        graphical_session=graphical_session,
        platform=sys.platform,
    )


def _create_elected_owner() -> VaultOwner | MemoryOwner:
    context = _owner_election_context()

    def memory_owner() -> MemoryOwner:
        transition: Callable[[RuntimeSnapshot], None] | None = None
        service_root = Path("/run/service")
        if context.containerized and service_root.is_dir():
            transition = S6LifecycleAdapter(
                service_root=service_root,
                hermes_home=Path(os.environ.get("HERMES_HOME", "/opt/data")),
                environment=dict(os.environ),
            ).transition
        return MemoryOwner(
            AuthClient(),
            hardener=ProcessHardener(),
            on_transition=transition,
        )

    return resolve_owner(
        context,
        live_owner=lambda: None,
        vault_factory=lambda: VaultOwner(AuthClient()),
        memory_factory=memory_owner,
    )


def run_owner_service() -> int:
    try:
        connect_runtime_owner(timeout=_RUNTIME_RECOVERY_PROBE_SECONDS)
    except AuthRequired:
        pass
    else:
        return 0
    owner = _create_elected_owner()
    try:
        broker = OwnerBroker.start(owner)
    except AuthRequired:
        return 0
    install_entrypoint_owner(owner)
    try:
        broker.wait()
    finally:
        clear_entrypoint_owner()
        broker.close()
        owner.close()
    return 0


def _read_runtime_frame(
    connection: Any,
    *,
    timeout: float | None = None,
) -> bytes:
    if timeout is not None and timeout <= 0:
        raise TimeoutError
    deadline = time.monotonic() + timeout if timeout is not None else None
    data = bytearray()
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            set_timeout = getattr(connection, "settimeout", None)
            if callable(set_timeout):
                set_timeout(remaining)
        chunk = connection.recv(min(4096, _RUNTIME_FRAME_LIMIT + 1 - len(data)))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError
        if not chunk:
            raise AuthRequired("runtime_unavailable")
        data.extend(chunk)
        if len(data) > _RUNTIME_FRAME_LIMIT:
            raise AuthRequired("runtime_unavailable")
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise AuthRequired("runtime_unavailable")
            return bytes(data[:newline])


def _scope_from_wire(value: object) -> AuthScope:
    if not isinstance(value, dict) or set(value) != {
        "runtime_instance_id",
        "epoch",
    }:
        raise AuthRequired("runtime_unavailable")
    instance = value.get("runtime_instance_id")
    epoch = value.get("epoch")
    if not isinstance(instance, str) or len(instance) != 32:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AuthRequired("runtime_unavailable")
    return AuthScope(instance, epoch)


def _snapshot_from_public(value: object) -> RuntimeSnapshot:
    legacy_keys = {
        "state",
        "username",
        "runtime_instance_id",
        "epoch",
        "valid_until",
        "session_expires_at",
        "reason",
    }
    continuity_keys = legacy_keys | {
        "account_id",
        "session_id",
        "installation_id",
        "principal_key",
        "predecessor_principal_key",
        "validation_state",
        "validation_reason",
        "last_validated_at",
        "legacy",
    }
    cloud_keys = continuity_keys | {"cloud_state"}
    value_keys = frozenset(value) if isinstance(value, dict) else frozenset()
    if not isinstance(value, dict) or value_keys not in {
        frozenset(legacy_keys),
        frozenset(continuity_keys),
        frozenset(cloud_keys),
    }:
        raise AuthRequired("runtime_unavailable")
    try:
        state = AuthState(value["state"])
    except (KeyError, TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    username = value["username"]
    instance = value["runtime_instance_id"]
    epoch = value["epoch"]
    valid_until = value["valid_until"]
    expires = value["session_expires_at"]
    reason = value["reason"]
    principal_key = value.get("principal_key")
    predecessor_principal_key = value.get("predecessor_principal_key")
    if username is not None and not isinstance(username, str):
        raise AuthRequired("runtime_unavailable")
    if not isinstance(instance, str) or len(instance) != 32:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        raise AuthRequired("runtime_unavailable")
    if (
        not isinstance(valid_until, (int, float))
        or isinstance(valid_until, bool)
        or not math.isfinite(valid_until)
    ):
        raise AuthRequired("runtime_unavailable")
    if expires is not None and not isinstance(expires, str):
        raise AuthRequired("runtime_unavailable")
    if reason is not None and not isinstance(reason, str):
        raise AuthRequired("runtime_unavailable")
    if value_keys == legacy_keys:
        return RuntimeSnapshot(
            state=state,
            epoch=epoch,
            valid_until=float(valid_until),
            runtime_instance_id=instance,
            boot_id=_read_boot_id(),
            username=username,
            session_expires_at=expires,
            reason=reason,
            cloud_state=(
                CloudState.ACTIVE if state is AuthState.AUTHENTICATED else None
            ),
        )
    account_id = value["account_id"]
    session_id = value["session_id"]
    installation_id = value["installation_id"]
    principal_key = value["principal_key"]
    validation_reason = value["validation_reason"]
    last_validated_at = value["last_validated_at"]
    legacy = value["legacy"]
    if any(
        item is not None and not isinstance(item, str)
        for item in (
            account_id,
            session_id,
            installation_id,
            principal_key,
            predecessor_principal_key,
            validation_reason,
            last_validated_at,
        )
    ) or not isinstance(legacy, bool):
        raise AuthRequired("runtime_unavailable")
    try:
        validation_state = ValidationState(value["validation_state"])
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if value_keys == cloud_keys:
        cloud_value = value["cloud_state"]
        try:
            cloud_state = (
                CloudState(cloud_value) if cloud_value is not None else None
            )
        except (TypeError, ValueError):
            raise AuthRequired("runtime_unavailable") from None
    else:
        cloud_state = (
            CloudState.ACTIVE
            if state is AuthState.AUTHENTICATED
            and validation_state is ValidationState.ONLINE
            else (
                CloudState.UNREACHABLE
                if state is AuthState.AUTHENTICATED
                else None
            )
        )
    if (state is AuthState.AUTHENTICATED) != (cloud_state is not None):
        raise AuthRequired("runtime_unavailable")
    if (
        validation_state is ValidationState.ONLINE
        and cloud_state is not CloudState.ACTIVE
    ):
        raise AuthRequired("runtime_unavailable")
    if all(item is None for item in (account_id, session_id, installation_id, principal_key)):
        if legacy:
            raise AuthRequired("runtime_unavailable")
    elif legacy:
        if any(item is not None for item in (account_id, session_id, installation_id)) or not _is_legacy_principal_key(principal_key):
            raise AuthRequired("runtime_unavailable")
    else:
        try:
            for identifier in (account_id, session_id, installation_id):
                if not isinstance(identifier, str):
                    raise AuthRequired("runtime_unavailable")
                _validate_uuid4(identifier)
        except AuthRequired:
            raise AuthRequired("runtime_unavailable") from None
        if principal_key != f"account:{account_id}":
            raise AuthRequired("runtime_unavailable")
    return RuntimeSnapshot(
        state=state,
        epoch=epoch,
        valid_until=float(valid_until),
        runtime_instance_id=instance,
        boot_id=_read_boot_id(),
        username=username,
        session_expires_at=expires,
        reason=reason,
        cloud_state=cloud_state,
        account_id=account_id,
        session_id=session_id,
        installation_id=installation_id,
        principal_key=principal_key,
        predecessor_principal_key=predecessor_principal_key,
        validation_state=validation_state,
        validation_reason=validation_reason,
        last_validated_at=last_validated_at,
        legacy=legacy,
    )


def _runtime_error(reason: str, *, version: int = 1) -> dict[str, object]:
    return {
        "version": version,
        "ok": False,
        "reason": reason,
    }


_entrypoint_owner_lock = threading.RLock()
_entrypoint_owner: _EntryPointOwner | None = None
_owner_recovery_lock = threading.Lock()
_automatic_owner_start_allowed = True


def install_entrypoint_owner(owner: _EntryPointOwner) -> None:
    global _entrypoint_owner
    with _entrypoint_owner_lock:
        _entrypoint_owner = owner


def clear_entrypoint_owner() -> None:
    global _automatic_owner_start_allowed, _entrypoint_owner
    # Lock order is recovery -> entrypoint -> consumer throughout this module.
    with _owner_recovery_lock:
        with _entrypoint_owner_lock:
            _entrypoint_owner = None
            clear_runtime_consumer()
        _automatic_owner_start_allowed = True


def _adopt_entrypoint_owner(owner: _EntryPointOwner) -> _EntryPointOwner:
    global _entrypoint_owner
    with _entrypoint_owner_lock:
        if _entrypoint_owner is None:
            _entrypoint_owner = owner
        return _entrypoint_owner


def _refresh_entrypoint_owner(
    owner: _EntryPointOwner,
    *,
    timeout: float = _RUNTIME_RECOVERY_PROBE_SECONDS,
    installation_id: str | None = None,
    client_version: str | None = None,
) -> RuntimeSnapshot:
    if installation_id is not None and client_version is not None:
        if isinstance(owner, RemoteRuntimeOwner):
            return owner.refresh(
                timeout=timeout,
                installation_id=installation_id,
                client_version=client_version,
            )
        return owner.refresh(
            installation_id=installation_id,
            client_version=client_version,
        )
    if isinstance(owner, RemoteRuntimeOwner):
        return owner.refresh(timeout=timeout)
    return owner.refresh()


def _safe_status_failure(reason: str | None) -> RuntimeSnapshot:
    safe_reasons = {
        "invalid_credentials",
        "rate_limited",
        "server_unavailable",
        "session_expired",
        "session_rejected",
        "signed_out",
        "vault_unavailable",
    }
    return RuntimeSnapshot.signed_out(
        reason=reason if reason in safe_reasons else "runtime_unavailable"
    )


def _recover_entrypoint_owner(
    failed_owner: _EntryPointOwner,
    *,
    allow_start: bool = True,
    force_start: bool = False,
) -> _EntryPointOwner:
    global _automatic_owner_start_allowed, _entrypoint_owner
    with _owner_recovery_lock:
        with _entrypoint_owner_lock:
            current = _entrypoint_owner
            if current is not None and current is not failed_owner:
                return current

        try:
            candidate = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired as error:
            if error.reason != "runtime_unavailable":
                raise
            if (
                not allow_start
                or not force_start
                and not _automatic_owner_start_allowed
            ):
                raise
            try:
                candidate = start_runtime_owner(
                    timeout=_RUNTIME_RECOVERY_START_SECONDS,
                    probe_first=False,
                )
            except AuthRequired:
                _automatic_owner_start_allowed = False
                raise

        with _entrypoint_owner_lock:
            current = _entrypoint_owner
            if current is not None and current is not failed_owner:
                return current
            failed_instance = failed_owner.snapshot().runtime_instance_id
            candidate_instance = candidate.snapshot().runtime_instance_id
            if candidate_instance != failed_instance:
                clear_runtime_consumer()
            _entrypoint_owner = candidate
            _automatic_owner_start_allowed = True
            return candidate


def authorize_entrypoint(boundary: str, *, interactive: bool) -> AuthScope:
    if external_auth_enabled():
        return external_auth_scope()

    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired:
            if not interactive:
                raise
            owner = start_runtime_owner(
                timeout=_RUNTIME_RECOVERY_START_SECONDS,
                probe_first=False,
            )
        owner = _adopt_entrypoint_owner(owner)

    recoverable = {"session_rejected", "session_expired", "signed_out"}
    try:
        snapshot = _refresh_entrypoint_owner(owner)
    except AuthRequired as error:
        if error.reason == "runtime_unavailable":
            owner = _recover_entrypoint_owner(
                owner,
                allow_start=interactive,
                force_start=interactive,
            )
            snapshot = _refresh_entrypoint_owner(owner)
        elif not interactive or error.reason not in recoverable:
            raise
        else:
            snapshot = owner.snapshot()

    if snapshot.state is not AuthState.AUTHENTICATED:
        reason = snapshot.reason or "signed_out"
        if not interactive or reason in {
            "rate_limited",
            "server_unavailable",
            "invalid_response",
            "runtime_unavailable",
            "vault_unavailable",
        }:
            raise AuthRequired(reason)
        try:
            username = input("Username: ").strip()
            import getpass

            password_text = getpass.getpass("Password: ")
        except (EOFError, KeyboardInterrupt):
            raise AuthRequired("signed_out") from None
        password = bytearray(password_text.encode("utf-8"))
        password_text = ""
        try:
            snapshot = owner.login(username, password)
        finally:
            password[:] = b"\0" * len(password)

    if os.environ.get("HERMES_DESKTOP") == "1":
        consumer = owner.connect_consumer(allow_local_continuity=True)
    else:
        consumer = owner.connect_consumer()
    scope = consumer.require_authorized(
        boundary,
        expected=snapshot.scope,
    )
    with _entrypoint_owner_lock:
        if _entrypoint_owner is not owner:
            raise AuthRequired("runtime_unavailable")
        install_runtime_consumer(consumer)
    return scope


def _trace_credential_to_wire(credential: TraceCredential) -> dict[str, object]:
    return {
        "access_token": credential.access_token,
        "expires_at": credential.expires_at,
        "expires_in": credential.expires_in,
        "installation_id": credential.installation_id,
    }


def _trace_credential_from_wire(
    value: object,
    *,
    expected_installation_id: str,
) -> TraceCredential:
    if not isinstance(value, dict) or set(value) != {
        "access_token",
        "expires_at",
        "expires_in",
        "installation_id",
    }:
        raise AuthRequired("runtime_unavailable")
    access_token = value.get("access_token")
    expires_at = value.get("expires_at")
    expires_in = value.get("expires_in")
    installation_id = value.get("installation_id")
    if (
        not isinstance(access_token, str)
        or not 20 <= len(access_token) <= 4096
        or any(character in access_token for character in "\r\n")
        or not isinstance(expires_at, str)
        or len(expires_at) > 128
        or not isinstance(expires_in, int)
        or isinstance(expires_in, bool)
        or not 1 <= expires_in <= 900
        or installation_id != expected_installation_id
        or not isinstance(installation_id, str)
        or _TRACE_INSTALLATION_ID.fullmatch(installation_id) is None
    ):
        raise AuthRequired("runtime_unavailable")
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        expiry.tzinfo is None
        or expiry.utcoffset() is None
        or expiry <= datetime.now(tz=expiry.tzinfo)
    ):
        raise AuthRequired("runtime_unavailable")
    return TraceCredential(
        access_token=access_token,
        expires_at=expires_at,
        expires_in=expires_in,
        installation_id=installation_id,
    )


def account_status(
    *,
    installation_id: str | None = None,
    client_version: str | None = None,
) -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired as error:
            return _safe_status_failure(error.reason)
        owner = _adopt_entrypoint_owner(owner)
    try:
        snapshot = _refresh_entrypoint_owner(
            owner,
            installation_id=installation_id,
            client_version=client_version,
        )
    except AuthRequired as error:
        if error.reason == "runtime_unavailable":
            try:
                owner = _recover_entrypoint_owner(owner)
                return _refresh_entrypoint_owner(
                    owner,
                    installation_id=installation_id,
                    client_version=client_version,
                )
            except AuthRequired as recovery_error:
                return _safe_status_failure(recovery_error.reason)
        return _safe_status_failure(error.reason)
    if (
        snapshot.state is not AuthState.AUTHENTICATED
        and snapshot.reason == "runtime_unavailable"
    ):
        try:
            owner = _recover_entrypoint_owner(owner)
            return _refresh_entrypoint_owner(
                owner,
                installation_id=installation_id,
                client_version=client_version,
            )
        except AuthRequired as error:
            return _safe_status_failure(error.reason)
    return snapshot


def account_login(
    username: str,
    password: bytearray,
    *,
    installation_id: str | None = None,
    client_version: str | None = None,
) -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired:
            owner = start_runtime_owner(
                timeout=_RUNTIME_RECOVERY_START_SECONDS,
                probe_first=False,
            )
        owner = _adopt_entrypoint_owner(owner)
    try:
        snapshot = _refresh_entrypoint_owner(owner)
    except AuthRequired as error:
        if error.reason == "runtime_unavailable":
            try:
                owner = _recover_entrypoint_owner(owner, force_start=True)
            except AuthRequired:
                pass
    else:
        if (
            snapshot.state is not AuthState.AUTHENTICATED
            and snapshot.reason == "runtime_unavailable"
        ):
            try:
                owner = _recover_entrypoint_owner(owner, force_start=True)
            except AuthRequired:
                pass
    if installation_id is None and client_version is None:
        return owner.login(username, password)
    return owner.login(
        username,
        password,
        installation_id=installation_id,
        client_version=client_version,
    )


def account_logout() -> RuntimeSnapshot:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired:
            return RuntimeSnapshot.signed_out()
        owner = _adopt_entrypoint_owner(owner)
    try:
        return owner.logout()
    except AuthRequired as error:
        if error.reason != "runtime_unavailable":
            raise
        replacement = _recover_entrypoint_owner(owner, force_start=True)
        return replacement.logout()


def account_trace_token(
    *,
    installation_id: str,
    client_version: str,
    telemetry_schema_version: str,
) -> TraceCredential:
    with _entrypoint_owner_lock:
        owner = _entrypoint_owner
    if owner is None:
        try:
            owner = connect_runtime_owner(
                timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
            )
        except AuthRequired as error:
            raise AuthRequired(error.reason or "signed_out") from None
        owner = _adopt_entrypoint_owner(owner)
    return owner.trace_token(
        installation_id=installation_id,
        client_version=client_version,
        telemetry_schema_version=telemetry_schema_version,
    )


@dataclass(frozen=True)
class OwnerElectionContext:
    ssh_connection: bool
    containerized: bool
    graphical_session: bool
    platform: str


_Owner = TypeVar("_Owner")


def resolve_owner(
    context: OwnerElectionContext,
    *,
    live_owner: Callable[[], _Owner | None],
    vault_factory: Callable[[], _Owner],
    memory_factory: Callable[[], _Owner],
) -> _Owner:
    existing = live_owner()
    if existing is not None:
        return existing
    if context.ssh_connection or context.containerized:
        return memory_factory()
    if context.graphical_session and context.platform in {"darwin", "linux", "win32"}:
        return vault_factory()
    return memory_factory()


def runtime_endpoint() -> UnixEndpoint | WindowsNamedPipeEndpoint:
    if os.name == "nt":
        return WindowsNamedPipeEndpoint.for_current_sid(
            first_instance=True,
            runtime_namespace=_auth_runtime_namespace(),
        )
    runtime_namespace = _auth_runtime_namespace()
    return UnixEndpoint.for_current_user(
        random_name=secrets.token_hex(16),
        forbid_abstract=sys.platform.startswith("linux"),
        darwin_user_temp=sys.platform == "darwin",
        runtime_namespace=runtime_namespace,
    )


def _encode_cookie_blob(record: CookieRecord) -> str:
    if not _is_legacy_principal_key(record.principal_key):
        raise AuthRequired("runtime_unavailable")

    payload = {
        "version": 2,
        "cookies": dict(record.cookies),
        "username": record.username,
        "session_expires_at": record.session_expires_at,
        "principal_key": record.principal_key,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_cookie_blob(raw: str) -> CookieRecord:
    if not isinstance(raw, str) or not raw or len(raw) > 16_384:
        raise AuthRequired("runtime_unavailable")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    version = payload.get("version") if isinstance(payload, dict) else None
    legacy_keys = {
        "version",
        "cookies",
        "username",
        "session_expires_at",
    }
    current_keys = legacy_keys | {"principal_key"}
    if not isinstance(payload, dict) or (
        (version == 1 and set(payload) != legacy_keys)
        or (version == 2 and set(payload) != current_keys)
        or version not in {1, 2}
    ):
        raise AuthRequired("runtime_unavailable")
    cookies = payload.get("cookies")
    username = payload.get("username")
    session_expires_at = payload.get("session_expires_at")
    principal_key = payload.get("principal_key")
    if not isinstance(cookies, dict) or set(cookies) != {SESSION_COOKIE, CSRF_COOKIE}:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(username, str) or not username:
        raise AuthRequired("runtime_unavailable")
    if not isinstance(session_expires_at, str) or not session_expires_at:
        raise AuthRequired("runtime_unavailable")
    normalized: dict[str, str] = {}
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        value = cookies.get(name)
        if not isinstance(value, str) or not value or any(char in value for char in "\r\n;"):
            raise AuthRequired("runtime_unavailable")
        normalized[name] = value
    _parse_aware_datetime(session_expires_at)
    if version == 1:
        principal_key = "legacy:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()
    elif not _is_legacy_principal_key(principal_key):
        raise AuthRequired("runtime_unavailable")

    return CookieRecord(normalized, username, session_expires_at, principal_key)


def _is_legacy_principal_key(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"legacy:[0-9a-f]{64}", value) is not None


def _proven_legacy_predecessor(
    previous: CookieRecord | NativeCredentialRecord | LegacyCredentialRecord | RevocationTombstone | SignedOutTombstone | None,
    bootstrap: CookieRecord,
    *,
    expected_installation_id: str,
    issued_installation_id: str,
) -> str | None:
    if expected_installation_id != issued_installation_id:
        return None
    if isinstance(previous, LegacyCredentialRecord):
        prior_cookie = previous.cookie_record
        principal_key = previous.principal_key
    elif isinstance(previous, CookieRecord):
        prior_cookie = previous
        principal_key = previous.principal_key
    else:
        return None
    if not _is_legacy_principal_key(principal_key):
        return None

    def digest(record: CookieRecord) -> bytes:
        return hashlib.sha256(
            b"\0".join(
                record.cookies[name].encode("utf-8")
                for name in (SESSION_COOKIE, CSRF_COOKIE)
            )
        ).digest()

    return principal_key if secrets.compare_digest(digest(prior_cookie), digest(bootstrap)) else None


def _encode_native_blob(record: NativeCredentialRecord) -> str:
    credential = record.credential
    return json.dumps(
        {
            "version": 3,
            "kind": "native",
            "account_id": credential.account_id,
            "session_id": credential.session_id,
            "session_token": credential.session_token,
            "installation_id": credential.installation_id,
            "username": credential.username,
            "issued_at": credential.issued_at,
            "last_validated_at": record.last_validated_at,
            "predecessor_principal_key": record.predecessor_principal_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_tombstone_blob(tombstone: RevocationTombstone) -> str:
    return json.dumps(
        {
            "version": 2,
            "kind": "revoked",
            "account_id": tombstone.account_id,
            "session_id": tombstone.session_id,
            "reason": tombstone.reason,
            "revoked_at": tombstone.revoked_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _encode_signed_out_tombstone_blob(tombstone: SignedOutTombstone) -> str:
    return json.dumps(
        {
            "version": 3,
            "kind": "signed_out",
            "reason": tombstone.reason,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_credential_blob(
    raw: str,
) -> CookieRecord | NativeCredentialRecord | RevocationTombstone | SignedOutTombstone:
    if not isinstance(raw, str) or not raw or len(raw) > 16_384:
        raise AuthRequired("runtime_unavailable")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if not isinstance(payload, dict):
        raise AuthRequired("runtime_unavailable")
    if payload.get("version") in {1, 2} and "cookies" in payload:
        return _decode_cookie_blob(raw)
    if payload.get("version") not in {2, 3} or not isinstance(payload.get("kind"), str):
        raise AuthRequired("runtime_unavailable")
    if payload["kind"] == "signed_out":
        if set(payload) != {"version", "kind", "reason"} or payload.get(
            "version"
        ) != 3:
            raise AuthRequired("runtime_unavailable")
        reason = payload["reason"]
        if reason not in {"signed_out", "session_rejected", "session_expired"}:
            raise AuthRequired("runtime_unavailable")
        return SignedOutTombstone(reason)
    if payload["kind"] == "revoked":
        if set(payload) != {
            "version", "kind", "account_id", "session_id", "reason", "revoked_at"
        }:
            raise AuthRequired("runtime_unavailable")
        account_id = payload["account_id"]
        session_id = payload["session_id"]
        reason = payload["reason"]
        revoked_at = payload["revoked_at"]
        if not all(isinstance(value, str) and value for value in (account_id, session_id, reason, revoked_at)):
            raise AuthRequired("runtime_unavailable")
        _validate_uuid4(account_id)
        _validate_uuid4(session_id)
        _parse_aware_datetime(revoked_at)
        return RevocationTombstone(account_id, session_id, reason, revoked_at)
    native_v2_keys = {
        "version", "kind", "account_id", "session_id", "session_token",
        "installation_id", "username", "issued_at", "last_validated_at",
    }
    native_v3_keys = native_v2_keys | {"predecessor_principal_key"}
    if payload["kind"] != "native" or frozenset(payload) not in {
        frozenset(native_v2_keys),
        frozenset(native_v3_keys),
    }:
        raise AuthRequired("runtime_unavailable")
    predecessor_principal_key = payload.get("predecessor_principal_key")
    if predecessor_principal_key is not None and not _is_legacy_principal_key(
        predecessor_principal_key
    ):
        raise AuthRequired("runtime_unavailable")
    fields = (
        payload["account_id"], payload["session_id"], payload["session_token"],
        payload["installation_id"], payload["username"], payload["issued_at"],
        payload["last_validated_at"],
    )
    if not all(isinstance(value, str) and value for value in fields):
        raise AuthRequired("runtime_unavailable")
    account_id, session_id, token, installation_id, username, issued_at, last_validated_at = fields
    _validate_uuid4(account_id)
    _validate_uuid4(session_id)
    _validate_uuid4(installation_id)
    if len(token) < 32 or len(token) > 128 or re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise AuthRequired("runtime_unavailable")
    _parse_aware_datetime(issued_at)
    _parse_aware_datetime(last_validated_at)
    return NativeCredentialRecord(
        NativeSessionCredential(
            account_id=account_id,
            session_id=session_id,
            session_token=token,
            installation_id=installation_id,
            username=username,
            issued_at=issued_at,
        ),
        last_validated_at,
        predecessor_principal_key,
    )


def _validate_uuid4(value: str) -> None:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise AuthRequired("runtime_unavailable") from None
    if parsed.version != 4 or str(parsed) != value.lower():
        raise AuthRequired("runtime_unavailable")


_consumer_lock = threading.RLock()
_consumer: RuntimeConsumer | None = None
_backend_scope_control_lock = threading.Lock()
_backend_scope_control_thread: threading.Thread | None = None


def install_runtime_consumer(consumer: RuntimeConsumer) -> None:
    global _consumer
    with _consumer_lock:
        _consumer = consumer


def clear_runtime_consumer() -> None:
    global _consumer
    with _consumer_lock:
        _consumer = None


def require_authorized(
    boundary: str,
    *,
    expected: AuthScope | None = None,
) -> AuthScope:
    if external_auth_enabled():
        # Ansatz owns the account login. Keep the desktop's scoped bearer
        # registry, but do not start or query Hermes's separate account owner.
        return expected or external_auth_scope()

    with _consumer_lock:
        consumer = _consumer
    if consumer is None:
        raise AuthRequired("runtime_unavailable")
    return consumer.require_authorized(boundary, expected=expected)


def wait_until_authorized(
    boundary: str,
    *,
    stop_event: threading.Event,
    on_state: Callable[[RuntimeSnapshot], None],
    poll_seconds: float = 0.5,
    start_owner_if_missing: bool = False,
) -> LockedWaitingResult:
    """Wait without prompting until the shared owner grants a fresh scope."""
    if not boundary or poll_seconds < 0:
        raise AuthRequired("runtime_unavailable")
    owner: RemoteRuntimeOwner | None = None
    start_attempted = False
    while not stop_event.is_set():
        if owner is None:
            try:
                owner = connect_runtime_owner(
                    timeout=_RUNTIME_RECOVERY_PROBE_SECONDS,
                )
            except AuthRequired as error:
                if (
                    start_owner_if_missing
                    and not start_attempted
                    and error.reason == "runtime_unavailable"
                ):
                    start_attempted = True
                    try:
                        owner = start_runtime_owner(
                            timeout=_RUNTIME_RECOVERY_START_SECONDS,
                            probe_first=False,
                        )
                    except AuthRequired as start_error:
                        snapshot = RuntimeSnapshot.signed_out(
                            reason=start_error.reason or start_error.code,
                        )
                    else:
                        snapshot = owner.snapshot()
                else:
                    snapshot = RuntimeSnapshot.signed_out(
                        reason=error.reason or error.code,
                    )
                on_state(snapshot)
                stop_event.wait(poll_seconds)
                continue
            else:
                start_attempted = False
        try:
            snapshot = owner.refresh()
        except AuthRequired as error:
            if error.reason == "runtime_unavailable":
                owner = None
            snapshot = RuntimeSnapshot.signed_out(
                reason=error.reason or error.code,
            )
        on_state(snapshot)
        if snapshot.state is AuthState.AUTHENTICATED and owner is not None:
            if os.environ.get("HERMES_DESKTOP") == "1":
                consumer = owner.connect_consumer(allow_local_continuity=True)
            else:
                consumer = owner.connect_consumer()
            consumer.require_authorized(boundary, expected=snapshot.scope)
            install_runtime_consumer(consumer)  # type: ignore[arg-type]
            return LockedWaitingResult.AUTHENTICATED
        stop_event.wait(poll_seconds)
    return LockedWaitingResult.OWNER_STOPPED


backend_scope_tokens = BackendScopeTokenRegistry()


def register_backend_scope_token(value: object) -> BackendScopeGrant:
    registration = parse_control_frame(value)
    if not isinstance(registration, ScopeTokenRegistration):
        raise BackendScopeTokenRejected("invalid_registration")
    expected = AuthScope(
        registration.runtime_instance_id,
        registration.epoch,
    )
    return backend_scope_tokens.register_candidate(
        registration,
        expected=expected,
    )


def register_backend_trace_transport(value: object) -> None:
    registration = parse_trace_transport_registration(value)
    from agent.relay_runtime import register_ansatz_product_trace_transport

    register_ansatz_product_trace_transport(
        endpoint=registration.endpoint,
        authorization=registration.authorization,
        installation_id=registration.installation_id,
        entrypoint=registration.entrypoint,
        plugins_toml=registration.plugins_toml,
    )


def _run_backend_scope_token_control(source: Any, target: Any) -> None:
    try:
        while True:
            try:
                raw = source.readline(BACKEND_SCOPE_CONTROL_FRAME_LIMIT + 1)
            except (OSError, ValueError):
                break
            if not raw:
                break
            if len(raw) > BACKEND_SCOPE_CONTROL_FRAME_LIMIT or not raw.endswith(b"\n"):
                break
            try:
                value = json.loads(raw)
            except (UnicodeError, ValueError):
                break
            if isinstance(value, dict) and value.get("operation") == "register_trace_transport":
                try:
                    register_backend_trace_transport(value)
                except Exception:
                    # Trace is optional to local conversation. A malformed
                    # or transiently failed Trace registration cannot revoke
                    # already-installed backend scope grants or terminate the
                    # stdin control loop; a later idempotent frame may recover.
                    continue
            else:
                try:
                    frame = parse_control_frame(value)
                    expected = AuthScope(
                        frame.runtime_instance_id,
                        frame.epoch,
                    )
                    if isinstance(frame, ScopeTokenRegistration):
                        backend_scope_tokens.register_candidate(
                            frame,
                            expected=expected,
                        )
                        ack = {
                            "version": DESKTOP_SCOPE_PROTOCOL_VERSION,
                            "operation": "scope_token_registered",
                            "registration_id": frame.registration_id,
                            "connection_id": frame.connection_id,
                            "runtime_instance_id": frame.runtime_instance_id,
                            "epoch": frame.epoch,
                            "ttl_seconds": frame.ttl_seconds,
                        }
                    elif isinstance(frame, ScopeTokenPromotion):
                        backend_scope_tokens.promote(
                            frame,
                            expected=expected,
                        )
                        ack = {
                            "version": DESKTOP_SCOPE_PROTOCOL_VERSION,
                            "operation": "scope_token_promoted",
                            "transition_id": frame.transition_id,
                            "registration_id": frame.registration_id,
                            "previous_registration_id": frame.previous_registration_id,
                            "connection_id": frame.connection_id,
                            "runtime_instance_id": frame.runtime_instance_id,
                            "epoch": frame.epoch,
                            "overlap_seconds": frame.overlap_seconds,
                        }
                    else:  # pragma: no cover - closed union defensive guard
                        raise BackendScopeTokenRejected("invalid_control_frame")
                    target.write(encode_control_ack(ack))
                    target.flush()
                except BackendScopeTokenRejected as error:
                    if (
                        error.reason
                        in _RECOVERABLE_BACKEND_SCOPE_CONTROL_REJECTIONS
                    ):
                        # A rejected candidate/promotion receives no ACK. The
                        # parent times out and retries while the existing
                        # active grant and this control reader remain usable.
                        continue
                    break
                except (AuthRequired, OSError, UnicodeError, ValueError):
                    break
    finally:
        backend_scope_tokens.clear()


def start_backend_scope_token_control(
    stream: Any | None = None,
    target: Any | None = None,
) -> threading.Thread:
    """Read the Desktop-only token protocol from inherited stdin.

    The raw bearer exists only in the bounded registration frame and the
    caller's request object. The registry stores its SHA-256 digest. EOF or
    malformed input revokes every grant for this backend process.
    """
    global _backend_scope_control_thread
    selected_source = stream if stream is not None else sys.stdin.buffer
    selected_target = target if target is not None else sys.stdout.buffer
    with _backend_scope_control_lock:
        running = _backend_scope_control_thread
        if running is not None and running.is_alive():
            return running
        thread = threading.Thread(
            target=_run_backend_scope_token_control,
            args=(selected_source, selected_target),
            daemon=True,
            name="hermes-backend-scope-control",
        )
        _backend_scope_control_thread = thread
        thread.start()
        return thread


def _lease_deadline(
    status: SessionStatus,
    *,
    now: float,
    absolute_cap: datetime | None = None,
) -> float:
    server_time = _parse_aware_datetime(status.server_time)
    response_expiry = _parse_aware_datetime(status.session_expires_at)
    expiry = min(response_expiry, absolute_cap) if absolute_cap is not None else response_expiry
    absolute_remaining = (expiry - server_time).total_seconds()
    if absolute_remaining <= 0:
        raise AuthRequired("session_expired")
    return now + min(LEASE_SECONDS, absolute_remaining)


def _parse_aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthRequired("runtime_unavailable")
    return parsed


def _validate_random_name(value: str) -> None:
    if len(value) != 32 or any(character not in "0123456789abcdef" for character in value):
        raise AuthRequired("runtime_unavailable")


def _validate_socket_name(value: str) -> None:
    if not value.startswith("h-") or not value.endswith(".s"):
        raise AuthRequired("runtime_unavailable")
    compact = value[2:-2]
    if len(compact) != 20 or any(
        character not in "0123456789abcdef" for character in compact
    ):
        raise AuthRequired("runtime_unavailable")
    if Path(value).name != value:
        raise AuthRequired("runtime_unavailable")


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_private_regular_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise AuthRequired("runtime_unavailable")


def _validate_socket_file(path: Path) -> None:
    try:
        details = path.lstat()
    except OSError:
        raise AuthRequired("runtime_unavailable") from None
    if (
        not stat.S_ISSOCK(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o600
    ):
        raise AuthRequired("runtime_unavailable")


def _open_private_file(
    path: Path,
    requested_flags: int,
    *,
    create: bool = True,
) -> int:
    flags = requested_flags
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if create and requested_flags & os.O_CREAT:
        mode = 0o600
    else:
        mode = 0
    try:
        fd = os.open(path, flags, mode)
        os.set_inheritable(fd, False)
        details = os.fstat(fd)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o600
        ):
            raise AuthRequired("runtime_unavailable")
        return fd
    except Exception:
        try:
            os.close(fd)
        except (OSError, UnboundLocalError):
            pass
        raise AuthRequired("runtime_unavailable") from None


def _validate_peer_uid(connection: socket.socket) -> None:
    try:
        if sys.platform == "darwin":
            import ctypes

            libc = ctypes.CDLL(None, use_errno=True)
            getpeereid = libc.getpeereid
            getpeereid.argtypes = [
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_uint),
                ctypes.POINTER(ctypes.c_uint),
            ]
            getpeereid.restype = ctypes.c_int
            peer_uid_value = ctypes.c_uint()
            peer_gid_value = ctypes.c_uint()
            if getpeereid(
                connection.fileno(),
                ctypes.byref(peer_uid_value),
                ctypes.byref(peer_gid_value),
            ) != 0:
                raise OSError(ctypes.get_errno(), "getpeereid failed")
            peer_uid = peer_uid_value.value
        elif sys.platform.startswith("linux") and hasattr(socket, "SO_PEERCRED"):
            raw = connection.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _peer_pid, peer_uid, _peer_gid = struct.unpack("3i", raw)
        else:
            raise AuthRequired("runtime_unavailable")
    except (AttributeError, OSError, struct.error):
        raise AuthRequired("runtime_unavailable") from None
    if peer_uid != os.getuid():
        raise AuthRequired("runtime_unavailable")


def _linux_runtime_root(*, runtime_namespace: str | None = None) -> Path:
    test_suffix = _test_runtime_suffix()
    namespace_suffix = _auth_runtime_namespace_suffix(runtime_namespace)
    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        base = Path(configured)
        try:
            details = base.lstat()
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        if (
            not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.getuid()
            or stat.S_IMODE(details.st_mode) != 0o700
        ):
            raise AuthRequired("runtime_unavailable")
        if runtime_namespace is None:
            return base / f"hermes-remote-auth{test_suffix}"
        return base / f"ha{namespace_suffix}{test_suffix}"
    if runtime_namespace is None:
        return Path(tempfile.gettempdir()) / (
            f"hermes-remote-auth-{os.getuid()}{test_suffix}"
        )
    return Path(tempfile.gettempdir()) / (
        f"ha-{os.getuid()}{namespace_suffix}{test_suffix}"
    )


def _darwin_user_temp_dir() -> Path:
    if sys.platform != "darwin":
        raise AuthRequired("runtime_unavailable")
    try:
        import ctypes

        darwin_user_temp_dir = 65_537
        libc = ctypes.CDLL(None, use_errno=True)
        confstr = libc.confstr
        confstr.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t]
        confstr.restype = ctypes.c_size_t
        size = confstr(darwin_user_temp_dir, None, 0)
        if size <= 1 or size > 4096:
            raise OSError("invalid Darwin user temporary directory size")
        buffer = ctypes.create_string_buffer(size)
        if confstr(darwin_user_temp_dir, buffer, size) != size:
            raise OSError("Darwin user temporary directory changed")
        path = Path(os.fsdecode(buffer.value))
        details = path.lstat()
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    if (
        not path.is_absolute()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o700
    ):
        raise AuthRequired("runtime_unavailable")
    return path


def _windows_current_sid() -> str:
    if os.name != "nt":
        raise AuthRequired("runtime_unavailable")
    try:
        import win32api
        import win32security

        token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_QUERY,
        )
        try:
            sid = win32security.GetTokenInformation(
                token,
                win32security.TokenUser,
            )[0]
            owner_sid = win32security.ConvertSidToStringSid(sid)
        finally:
            token.Close()
    except Exception:
        raise AuthRequired("runtime_unavailable") from None
    if not isinstance(owner_sid, str) or not owner_sid.startswith("S-1-"):
        raise AuthRequired("runtime_unavailable")
    return owner_sid


def _disable_core_dumps() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if resource.getrlimit(resource.RLIMIT_CORE) != (0, 0):
            raise OSError("core dump limit was not applied")
    except (ImportError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _linux_prctl_dumpable_zero() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(4, 0, 0, 0, 0) != 0 or prctl(3, 0, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl failed")
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _darwin_deny_attach() -> None:
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        ptrace = libc.ptrace
        ptrace.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        ptrace.restype = ctypes.c_int
        if ptrace(31, 0, None, 0) != 0:
            raise OSError(ctypes.get_errno(), "ptrace failed")
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _windows_disable_wer_dump() -> None:
    try:
        import ctypes

        no_ui = 0x20
        wer = ctypes.WinDLL("wer", use_last_error=True)
        wer.WerSetFlags.argtypes = [ctypes.c_uint]
        wer.WerSetFlags.restype = ctypes.c_long
        if wer.WerSetFlags(no_ui) != 0:
            raise OSError(ctypes.get_last_error(), "WerSetFlags failed")
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


def _windows_apply_process_mitigations() -> None:
    try:
        import ctypes
        from ctypes import wintypes

        class _ExtensionPointPolicy(ctypes.Structure):
            _fields_ = [("flags", wintypes.DWORD)]

        process_extension_point_disable_policy = 6
        disable_extension_points = 0x1
        policy = _ExtensionPointPolicy(disable_extension_points)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        apply_policy = kernel32.SetProcessMitigationPolicy
        apply_policy.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t]
        apply_policy.restype = wintypes.BOOL
        if not apply_policy(
            process_extension_point_disable_policy,
            ctypes.byref(policy),
            ctypes.sizeof(policy),
        ):
            raise OSError(ctypes.get_last_error(), "process mitigation failed")
    except (AttributeError, ImportError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None


@lru_cache(maxsize=1)
def _read_boot_id() -> str:
    if sys.platform.startswith("linux"):
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            )
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        normalized = boot_id.strip()
        if not normalized:
            raise AuthRequired("runtime_unavailable")
        return normalized
    if sys.platform == "darwin":
        try:
            record = os.stat("/var/run/utmpx", follow_symlinks=False)
        except OSError:
            raise AuthRequired("runtime_unavailable") from None
        birth_time_ns = getattr(record, "st_birthtime_ns", None)
        if birth_time_ns is None:
            birth_time = getattr(record, "st_birthtime", None)
            birth_time_ns = int(birth_time * 1_000_000_000) if birth_time else None
        if (
            not stat.S_ISREG(record.st_mode)
            or record.st_uid != 0
            or not birth_time_ns
        ):
            raise AuthRequired("runtime_unavailable")
        return f"darwin:{record.st_dev}:{record.st_ino}:{birth_time_ns}"
    if os.name == "nt":
        return _windows_boot_id()
    raise AuthRequired("runtime_unavailable")


def _windows_boot_id() -> str:
    try:
        import ctypes

        uptime_ms = int(ctypes.windll.kernel32.GetTickCount64())
    except (AttributeError, OSError, ValueError):
        raise AuthRequired("runtime_unavailable") from None
    approximate_boot = int((time.time() - (uptime_ms / 1000.0)) // 10)
    return f"windows:{approximate_boot}"


_LOCKED_WAIT_BOUNDARIES = frozenset(
    {
        "container.dashboard.start",
        "container.gateway.start",
        "container.main.start",
        "service.cron.start",
        "service.gateway.start",
        "service.kanban.start",
        "service.web.start",
    }
)


def _run_locked_wait(boundary: str) -> int:
    if boundary not in _LOCKED_WAIT_BOUNDARIES:
        return 2
    stop = threading.Event()
    last_state: tuple[AuthState, str | None] | None = None

    def report(snapshot: RuntimeSnapshot) -> None:
        nonlocal last_state
        current = (snapshot.state, snapshot.reason)
        if current == last_state:
            return
        last_state = current
        payload = {
            "auth_state": (
                "authenticated"
                if snapshot.state is AuthState.AUTHENTICATED
                else "locked-waiting"
            ),
            "reason": snapshot.reason,
            "guidance": f"run `{CANONICAL_COMMAND} login`",
        }
        print(json.dumps(payload, sort_keys=True), flush=True)

    try:
        wait_until_authorized(
            boundary,
            stop_event=stop,
            on_state=report,
        )
    except KeyboardInterrupt:
        stop.set()
    return 0


def _notify_service_manager(snapshot: RuntimeSnapshot) -> None:
    """Keep a systemd service healthy while its capability is locked."""
    endpoint = os.environ.get("NOTIFY_SOCKET", "")
    if not endpoint:
        return
    address = ("\0" + endpoint[1:]) if endpoint.startswith("@") else endpoint
    state = (
        "authenticated"
        if snapshot.state is AuthState.AUTHENTICATED
        else "locked-waiting"
    )
    payload = f"READY=1\nWATCHDOG=1\nSTATUS=auth_state={state}".encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
            notifier.sendto(payload, address)
    except OSError:
        # The request-boundary guard remains authoritative if the optional
        # service-manager status channel is unavailable.
        return


def _run_locked_service(arguments: list[str]) -> int:
    profile: str | None = None
    if len(arguments) in {2, 3} and arguments[:2] == ["service", "gateway"]:
        profile = arguments[2] if len(arguments) == 3 else None
        boundary = "service.gateway.start"
        target = [sys.executable, "-m", "hermes_cli.main"]
        if profile is not None:
            target.extend(["-p", profile])
        target.extend(["gateway", "run"])
    elif arguments == ["service", "kanban"]:
        boundary = "service.kanban.start"
        target = [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "kanban",
            "daemon",
            "--force",
            "--interval",
            "60",
        ]
    else:
        return 2
    if profile is not None:
        try:
            from hermes_cli.service_manager import validate_profile_name

            validate_profile_name(profile)
        except (ImportError, ValueError):
            return 2

    stop = threading.Event()
    last: tuple[AuthState, str | None] | None = None

    def report(snapshot: RuntimeSnapshot) -> None:
        nonlocal last
        _notify_service_manager(snapshot)
        current = (snapshot.state, snapshot.reason)
        if current == last:
            return
        last = current
        print(
            json.dumps(
                {
                    "auth_state": (
                        "authenticated"
                        if snapshot.state is AuthState.AUTHENTICATED
                        else "locked-waiting"
                    ),
                    "reason": snapshot.reason,
                    "guidance": f"run `{CANONICAL_COMMAND} login`",
                },
                sort_keys=True,
            ),
            flush=True,
        )

    try:
        result = wait_until_authorized(
            boundary,
            stop_event=stop,
            on_state=report,
            start_owner_if_missing=True,
        )
    except KeyboardInterrupt:
        return 0
    if result is not LockedWaitingResult.AUTHENTICATED:
        return 0
    os.execv(sys.executable, target)
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments == ["owner"]:
        return run_owner_service()
    if len(arguments) == 2 and arguments[0] == "wait":
        return _run_locked_wait(arguments[1])
    if arguments[:1] == ["service"]:
        return _run_locked_service(arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
