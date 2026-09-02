"""Encrypted Trace record and OS-protected key contracts."""

from __future__ import annotations

import os

import pytest


class MemorySecureStore:
    def __init__(self, *, available: bool = True) -> None:
        self.enabled = available
        self.values: dict[str, bytes] = {}

    def available(self) -> bool:
        return self.enabled

    def read(self, name: str) -> bytes | None:
        if not self.enabled:
            raise RuntimeError("secure store unavailable")
        return self.values.get(name)

    def write(self, name: str, value: bytes) -> None:
        if not self.enabled:
            raise RuntimeError("secure store unavailable")
        self.values[name] = value


def _metadata(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "account_id": "account-alice",
        "batch_id": "batch-1",
        "entrypoint": "cli",
        "sequence": 1,
    }
    value.update(overrides)
    return value


def test_aes256_gcm_round_trip_has_unique_96_bit_nonces_and_no_plaintext() -> None:
    from hermes_cli.client_auth.trace.crypto import decrypt_record, encrypt_record

    key = os.urandom(32)
    plaintext = b"private-otlp-payload-sentinel"
    first = encrypt_record(key, _metadata(), plaintext)
    second = encrypt_record(key, _metadata(), plaintext)

    assert len(first.nonce) == 12
    assert first.nonce != second.nonce
    assert plaintext not in first.encode()
    assert decrypt_record(key, _metadata(), first) == plaintext
    assert decrypt_record(key, _metadata(), second) == plaintext


def test_authenticated_metadata_or_ciphertext_tampering_fails_closed() -> None:
    from cryptography.exceptions import InvalidTag

    from hermes_cli.client_auth.trace.crypto import (
        EncryptedRecord,
        decrypt_record,
        encrypt_record,
    )

    key = os.urandom(32)
    record = encrypt_record(key, _metadata(), b"otlp")

    with pytest.raises(InvalidTag):
        decrypt_record(key, _metadata(entrypoint="desktop"), record)

    corrupted = EncryptedRecord(
        nonce=record.nonce,
        ciphertext=record.ciphertext[:-1] + bytes([record.ciphertext[-1] ^ 1]),
    )
    with pytest.raises(InvalidTag):
        decrypt_record(key, _metadata(), corrupted)


def test_trace_key_protector_is_account_bound_and_detects_key_loss() -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector, TraceKeyUnavailable

    store = MemorySecureStore()
    protector = TraceKeyProtector(store)
    key = os.urandom(32)

    wrapped = protector.wrap("account-alice", key)

    assert key not in wrapped
    assert protector.unwrap("account-alice", wrapped) == key
    with pytest.raises(TraceKeyUnavailable):
        protector.unwrap("account-bob", wrapped)

    store.values.clear()
    with pytest.raises(TraceKeyUnavailable):
        protector.unwrap("account-alice", wrapped)


def test_secure_store_unavailable_never_returns_or_persists_fallback_key() -> None:
    from hermes_cli.client_auth.trace.crypto import TraceKeyProtector, TraceKeyUnavailable

    store = MemorySecureStore(available=False)
    protector = TraceKeyProtector(store)

    assert not protector.available()
    with pytest.raises(TraceKeyUnavailable):
        protector.wrap("account-alice", os.urandom(32))
    assert store.values == {}


def test_runtime_keyring_adapter_round_trips_only_bytes_in_separate_namespace(
    monkeypatch,
) -> None:
    import keyring

    from hermes_cli.client_auth.runtime import KeyringTraceSecretStore

    values: dict[tuple[str, str], str] = {}

    class Backend:
        priority = 1

    monkeypatch.setattr(keyring, "get_keyring", lambda: Backend())
    monkeypatch.setattr(keyring, "get_password", lambda service, name: values.get((service, name)))
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda service, name, value: values.__setitem__((service, name), value),
    )
    store = KeyringTraceSecretStore()
    name = "account-" + "a" * 64

    assert store.available()
    assert store.read(name) is None
    store.write(name, b"x" * 32)

    assert store.read(name) == b"x" * 32
    assert values == {
        ("cn.c2sml.hermes.trace-data-keys", name): "eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHh4eHg="
    }


@pytest.mark.parametrize("key", [b"", b"short", b"x" * 31, b"x" * 33])
def test_encryption_rejects_non_aes256_keys(key: bytes) -> None:
    from hermes_cli.client_auth.trace.crypto import encrypt_record

    with pytest.raises(ValueError, match="256-bit"):
        encrypt_record(key, _metadata(), b"otlp")
