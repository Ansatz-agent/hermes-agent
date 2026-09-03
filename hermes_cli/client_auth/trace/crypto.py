"""Account-bound encryption primitives for the durable Trace outbox."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Protocol


_KEY_BYTES = 32
_NONCE_BYTES = 12
_KEY_REFERENCE_PREFIX = b"ansatz-trace-key-v1:"


class TraceKeyUnavailable(RuntimeError):
    """The protected per-account Trace key cannot be used."""


class SecureTraceSecretStore(Protocol):
    """Minimal injected OS-protected storage boundary."""

    def available(self) -> bool: ...

    def read(self, name: str) -> bytes | None: ...

    def write(self, name: str, value: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class EncryptedRecord:
    nonce: bytes
    ciphertext: bytes

    def encode(self) -> bytes:
        if len(self.nonce) != _NONCE_BYTES:
            raise ValueError("encrypted Trace record nonce must be 96-bit")
        return b"\x01" + self.nonce + self.ciphertext

    @classmethod
    def decode(cls, raw: bytes) -> EncryptedRecord:
        if len(raw) < 1 + _NONCE_BYTES + 16 or raw[0] != 1:
            raise ValueError("invalid encrypted Trace record")
        return cls(nonce=raw[1 : 1 + _NONCE_BYTES], ciphertext=raw[1 + _NONCE_BYTES :])


class TraceKeyProtector:
    """Keep data keys in protected storage and persist only opaque references."""

    def __init__(self, store: SecureTraceSecretStore) -> None:
        self._store = store

    def available(self) -> bool:
        try:
            return bool(self._store.available())
        except Exception:
            return False

    def wrap(self, account_id: str, key: bytes) -> bytes:
        _require_key(key)
        name = _account_key_name(account_id)
        if not self.available():
            raise TraceKeyUnavailable("protected Trace key storage unavailable")
        try:
            self._store.write(name, key)
            observed = self._store.read(name)
        except Exception as exc:
            raise TraceKeyUnavailable("protected Trace key storage unavailable") from exc
        if observed != key:
            raise TraceKeyUnavailable("protected Trace key write could not be verified")
        return _key_reference(key)

    def unwrap(self, account_id: str, wrapped: bytes) -> bytes:
        name = _account_key_name(account_id)
        if not self.available():
            raise TraceKeyUnavailable("protected Trace key storage unavailable")
        try:
            key = self._store.read(name)
        except Exception as exc:
            raise TraceKeyUnavailable("protected Trace key storage unavailable") from exc
        if key is None or len(key) != _KEY_BYTES or _key_reference(key) != wrapped:
            raise TraceKeyUnavailable("protected Trace key missing or account mismatch")
        return key


def encrypt_record(
    key: bytes,
    metadata: dict[str, object],
    plaintext: bytes,
) -> EncryptedRecord:
    _require_key(key)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _authenticated_metadata(metadata))
    return EncryptedRecord(nonce=nonce, ciphertext=ciphertext)


def decrypt_record(
    key: bytes,
    metadata: dict[str, object],
    record: EncryptedRecord,
) -> bytes:
    _require_key(key)
    if len(record.nonce) != _NONCE_BYTES:
        raise ValueError("encrypted Trace record nonce must be 96-bit")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key).decrypt(
        record.nonce,
        record.ciphertext,
        _authenticated_metadata(metadata),
    )


def _authenticated_metadata(metadata: dict[str, object]) -> bytes:
    try:
        return json.dumps(
            metadata,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Trace metadata must be canonical JSON") from exc


def _account_key_name(account_id: str) -> str:
    if not isinstance(account_id, str) or not account_id:
        raise ValueError("Trace account identity is required")
    digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()
    return f"account-{digest}"


def _key_reference(key: bytes) -> bytes:
    return _KEY_REFERENCE_PREFIX + hashlib.sha256(key).digest()


def _require_key(key: bytes) -> None:
    if not isinstance(key, bytes) or len(key) != _KEY_BYTES:
        raise ValueError("Trace records require a 256-bit key")


__all__ = [
    "EncryptedRecord",
    "SecureTraceSecretStore",
    "TraceKeyProtector",
    "TraceKeyUnavailable",
    "decrypt_record",
    "encrypt_record",
]
