"""Ansatz product trace activation and export-only privacy policy.

The product mode is intentionally narrower than ordinary Hermes Relay usage:
it activates only when Electron supplies a valid epoch-scoped loopback
forwarder and the package-sealed configuration has its exact source hash.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PRODUCT_CONFIG_SHA256 = (
    "adfdb62a5242d85ac6b5c19062cda4d511f2f46152758a6ffcbc40aafa34e741"
)
PRODUCT_RUNTIME_MARKER = "ansatz-voice-trace-client/v1"
PRODUCT_CONFIG_RELATIVE = ("ansatz-voice-trace", "plugins.toml")
PRODUCT_ENDPOINT_PLACEHOLDER = "http://127.0.0.1:1/v1/traces"
PRODUCT_TRACE_PATH = "/v1/traces"

REDACTED = "[REDACTED]"
CYCLE_REDACTED = "[REDACTED:CYCLE]"
LIMIT_REDACTED = "[REDACTED:LIMIT]"

_MAX_DEPTH = 32
_MAX_CONTAINER_ITEMS = 2048
_MAX_STRING_CHARS = 1_048_576
_AUTHORIZATION_RE = re.compile(
    r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"
)
_API_KEY_VALUE_RE = re.compile(r"\b(?:sk|pk)-[A-Za-z0-9_-]{12,}\b")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_LOCAL_BEARER_RE = re.compile(r"Bearer [A-Za-z0-9_-]{43}\Z")


@dataclass(frozen=True)
class _ProductTraceTransport:
    endpoint: str
    authorization: str = field(repr=False)
    installation_id: str = ""
    entrypoint: str = "desktop"
    plugins_toml: str = ""


_TRANSPORT_LOCK = threading.RLock()
_REGISTERED_PRODUCT_TRANSPORT: _ProductTraceTransport | None = None

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "cookie",
        "setcookie",
        "password",
        "passwd",
        "pwd",
        "secret",
        "clientsecret",
        "apikey",
        "xapikey",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "sessiontoken",
        "privatekey",
        "secretaccesskey",
    }
)
_RAW_AUDIO_KEYS = frozenset(
    {
        "audio",
        "audiobase64",
        "audiobytes",
        "audiodata",
        "pcm",
        "pcmbase64",
        "pcmbytes",
        "wavbase64",
        "wavbytes",
        "waveform",
    }
)


def _normalized_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _product_config_path() -> Path | None:
    raw = _transport_value("plugins_toml", "HERMES_NEMO_RELAY_PLUGINS_TOML")
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_file() or tuple(resolved.parts[-2:]) != PRODUCT_CONFIG_RELATIVE:
        return None
    return resolved


def _valid_loopback_endpoint(raw: str) -> bool:
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "http"
        and parsed.hostname == "127.0.0.1"
        and port is not None
        and 1 <= port <= 65535
        and parsed.port != 1
        and parsed.path == PRODUCT_TRACE_PATH
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
    )


def _valid_installation_id(raw: str) -> bool:
    try:
        installation_id = uuid.UUID(raw)
    except (ValueError, AttributeError):
        return False
    return installation_id.version == 4 and str(installation_id) == raw.lower()


@lru_cache(maxsize=4)
def _load_sealed_product_config_cached(
    path_value: str,
    mtime_ns: int,
    size: int,
) -> dict[str, Any] | None:
    del mtime_ns, size
    path = Path(path_value)
    try:
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != PRODUCT_CONFIG_SHA256:
            return None
        config = tomllib.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return None
    marker = config.get("ansatz_product")
    if not isinstance(marker, dict):
        return None
    if marker != {
        "runtime_marker": PRODUCT_RUNTIME_MARKER,
        "schema_version": 1,
        "package_resource_sealed": True,
    }:
        return None
    return config


def _load_sealed_product_config(path: Path) -> dict[str, Any] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _load_sealed_product_config_cached(
        str(path),
        stat.st_mtime_ns,
        stat.st_size,
    )


def ansatz_product_trace_requested() -> bool:
    """Return whether a Desktop/Voice child declared any product trace state."""
    with _TRANSPORT_LOCK:
        if _REGISTERED_PRODUCT_TRANSPORT is not None:
            return True
    if any(
        os.environ.get(name, "").strip()
        for name in (
            "ANSATZ_TRACE_LOCAL_ENDPOINT",
            "ANSATZ_TRACE_LOCAL_AUTHORIZATION",
            "ANSATZ_TRACE_INSTALLATION_ID",
            "ANSATZ_TRACE_ENTRYPOINT",
        )
    ):
        return True

    # A sealed Ansatz product config must never fall through to ordinary
    # Relay config handling. Without this guard, a missing dynamic transport
    # leaves the placeholder endpoint active and the SDK repeatedly reports a
    # generic network error instead of failing closed until Desktop repairs
    # the transport.
    path = _product_config_path()
    return path is not None and _load_sealed_product_config(path) is not None


def ansatz_product_trace_enabled() -> bool:
    """Validate the sealed product marker and the epoch-local forwarder."""
    path = _product_config_path()
    if path is None or _load_sealed_product_config(path) is None:
        return False
    endpoint = _transport_value("endpoint", "ANSATZ_TRACE_LOCAL_ENDPOINT")
    authorization = _transport_value("authorization", "ANSATZ_TRACE_LOCAL_AUTHORIZATION")
    installation_id = _transport_value("installation_id", "ANSATZ_TRACE_INSTALLATION_ID")
    entrypoint = _transport_value("entrypoint", "ANSATZ_TRACE_ENTRYPOINT")
    return bool(
        _valid_loopback_endpoint(endpoint)
        and _LOCAL_BEARER_RE.fullmatch(authorization)
        and _valid_installation_id(installation_id)
        and entrypoint in {"desktop", "voice"}
    )


def product_plugins_config() -> dict[str, Any]:
    """Return the fixed full-OTLP config with validated epoch values applied."""
    if not ansatz_product_trace_enabled():
        raise RuntimeError("Ansatz product trace runtime validation failed")
    path = _product_config_path()
    assert path is not None
    loaded = _load_sealed_product_config(path)
    assert loaded is not None
    config = copy.deepcopy(loaded)
    config.pop("ansatz_product", None)
    components = config.get("components")
    if not isinstance(components, list) or len(components) != 1:
        raise RuntimeError("Ansatz product Relay component contract is invalid")
    component_config = components[0].get("config")
    endpoints = component_config["opentelemetry"]["endpoints"]
    if (
        len(endpoints) != 1
        or endpoints[0].get("endpoint") != PRODUCT_ENDPOINT_PLACEHOLDER
    ):
        raise RuntimeError("Ansatz product Relay endpoint contract is invalid")
    endpoints[0]["endpoint"] = _transport_value("endpoint", "ANSATZ_TRACE_LOCAL_ENDPOINT")
    endpoints[0]["resource_attributes"]["ansatz.installation.id"] = _transport_value(
        "installation_id", "ANSATZ_TRACE_INSTALLATION_ID"
    )
    return config


def product_trace_authorization() -> str:
    if not ansatz_product_trace_enabled():
        raise RuntimeError("Ansatz product trace runtime validation failed")
    return _transport_value("authorization", "ANSATZ_TRACE_LOCAL_AUTHORIZATION")


def register_product_trace_transport(
    *,
    endpoint: str,
    authorization: str,
    installation_id: str,
    entrypoint: str,
    plugins_toml: str,
) -> None:
    candidate = _ProductTraceTransport(
        endpoint=endpoint,
        authorization=authorization,
        installation_id=installation_id,
        entrypoint=entrypoint,
        plugins_toml=plugins_toml,
    )
    global _REGISTERED_PRODUCT_TRANSPORT
    with _TRANSPORT_LOCK:
        previous = _REGISTERED_PRODUCT_TRANSPORT
        _REGISTERED_PRODUCT_TRANSPORT = candidate
        if not ansatz_product_trace_enabled():
            _REGISTERED_PRODUCT_TRANSPORT = previous
            raise ValueError("invalid product trace transport")


def clear_registered_product_trace_transport_for_tests() -> None:
    global _REGISTERED_PRODUCT_TRANSPORT
    with _TRANSPORT_LOCK:
        _REGISTERED_PRODUCT_TRANSPORT = None


def _transport_value(attribute: str, environment: str) -> str:
    with _TRANSPORT_LOCK:
        transport = _REGISTERED_PRODUCT_TRANSPORT
        if transport is not None:
            return str(getattr(transport, attribute)).strip()
    return os.environ.get(environment, "").strip()


def _redact_string(value: str) -> str:
    if len(value) > _MAX_STRING_CHARS:
        value = value[:_MAX_STRING_CHARS] + LIMIT_REDACTED
    value = _PRIVATE_KEY_RE.sub(REDACTED, value)
    value = _AUTHORIZATION_RE.sub(REDACTED, value)
    return _API_KEY_VALUE_RE.sub(REDACTED, value)


def redact_trace_value(value: Any, key_path: Any = ()) -> Any:
    """Copy and sanitize one trace value without changing conversation state."""
    if isinstance(key_path, str):
        path = (key_path,)
    elif isinstance(key_path, Sequence):
        path = tuple(str(part) for part in key_path)
    else:
        path = ()
    return _redact(value, path, depth=0, active=set())


def _redact(
    value: Any,
    key_path: tuple[str, ...],
    *,
    depth: int,
    active: set[int],
) -> Any:
    key = _normalized_key(key_path[-1]) if key_path else ""
    if key in _SECRET_KEYS or key.endswith("privatekey"):
        return REDACTED
    if depth > _MAX_DEPTH:
        return LIMIT_REDACTED
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED
    if isinstance(value, str):
        if key in _RAW_AUDIO_KEYS and len(value) >= 256:
            return REDACTED
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value

    object_id = id(value)
    if object_id in active:
        return CYCLE_REDACTED
    active.add(object_id)
    try:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for index, (item_key, item) in enumerate(value.items()):
                if index >= _MAX_CONTAINER_ITEMS:
                    result[LIMIT_REDACTED] = LIMIT_REDACTED
                    break
                rendered_key = str(item_key)
                result[rendered_key] = _redact(
                    item,
                    (*key_path, rendered_key),
                    depth=depth + 1,
                    active=active,
                )
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            result = [
                _redact(
                    item,
                    (*key_path, str(index)),
                    depth=depth + 1,
                    active=active,
                )
                for index, item in enumerate(value)
                if index < _MAX_CONTAINER_ITEMS
            ]
            if len(value) > _MAX_CONTAINER_ITEMS:
                result.append(LIMIT_REDACTED)
            return result
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return _redact(
                    model_dump(mode="json", warnings=False),
                    key_path,
                    depth=depth + 1,
                    active=active,
                )
            except TypeError:
                return _redact(
                    model_dump(),
                    key_path,
                    depth=depth + 1,
                    active=active,
                )
        try:
            attributes = {
                str(item_key): item
                for item_key, item in vars(value).items()
                if not str(item_key).startswith("_")
            }
        except (TypeError, AttributeError):
            return _redact_string(str(value))
        return _redact(
            attributes or str(value),
            key_path,
            depth=depth + 1,
            active=active,
        )
    finally:
        active.remove(object_id)


__all__ = [
    "CYCLE_REDACTED",
    "LIMIT_REDACTED",
    "PRODUCT_CONFIG_SHA256",
    "REDACTED",
    "ansatz_product_trace_enabled",
    "ansatz_product_trace_requested",
    "product_plugins_config",
    "redact_trace_value",
]
