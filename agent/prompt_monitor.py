"""Redacted, profile-scoped snapshots of finalized LLM request payloads.

The prompt monitor is deliberately a passive observer.  Callers hand it the
same kwargs they are about to give a provider adapter; this module serializes a
copy, removes transport/auth-only fields, redacts recognized secrets, and
writes one atomic JSON snapshot.  It never mutates the live request object.

Capture is disabled by default and configured under
``logging.prompt_monitor`` in ``config.yaml``.  Snapshots are consumed by the
``hermes prompt-monitor`` command from a separate terminal so stdout-based
protocols (TUI JSON-RPC, gateways, ACP) are not corrupted by debug output.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger(__name__)

PROMPT_MONITOR_SCHEMA_VERSION = 1
PROMPT_MONITOR_DIRNAME = "prompt-monitor"
PROMPT_MONITOR_GLOB = "prompt_*.json"

_DEFAULT_MAX_FILES = 100
_MAX_MAX_FILES = 10_000
_TRANSPORT_ONLY_KEYS = frozenset(
    {
        "timeout",
        "http_client",
        "headers",
        "extra_headers",
        # Private in-process MoA facade handoff; never part of the HTTP body.
        "_moa_prepared_request",
    }
)
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SEQUENCE = itertools.count()
_SEQUENCE_LOCK = threading.Lock()


@dataclass(frozen=True)
class PromptMonitorSettings:
    """Resolved prompt-monitor settings for the active Hermes profile."""

    enabled: bool = False
    include_auxiliary: bool = True
    max_files: int = _DEFAULT_MAX_FILES


def load_prompt_monitor_settings(
    config: Optional[Mapping[str, Any]] = None,
) -> PromptMonitorSettings:
    """Read ``logging.prompt_monitor`` without mutating cached config state."""

    try:
        if config is None:
            from hermes_cli.config import load_config_readonly

            config = load_config_readonly()
        logging_cfg = config.get("logging", {}) if isinstance(config, Mapping) else {}
        raw = logging_cfg.get("prompt_monitor", {}) if isinstance(logging_cfg, Mapping) else {}
        if isinstance(raw, bool):
            return PromptMonitorSettings(enabled=raw)
        if not isinstance(raw, Mapping):
            return PromptMonitorSettings()

        max_files_raw = raw.get("max_files", _DEFAULT_MAX_FILES)
        try:
            max_files = int(max_files_raw)
        except (TypeError, ValueError):
            max_files = _DEFAULT_MAX_FILES
        max_files = max(1, min(max_files, _MAX_MAX_FILES))
        return PromptMonitorSettings(
            enabled=bool(raw.get("enabled", False)),
            include_auxiliary=bool(raw.get("include_auxiliary", True)),
            max_files=max_files,
        )
    except Exception:
        logger.debug("Could not load prompt monitor settings", exc_info=True)
        return PromptMonitorSettings()


def prompt_monitor_directory(hermes_home: Optional[Path] = None) -> Path:
    """Return the profile-scoped prompt snapshot directory."""

    home = Path(hermes_home) if hermes_home is not None else get_hermes_home()
    return home / "logs" / PROMPT_MONITOR_DIRNAME


def _safe_label(value: Any, *, fallback: str) -> str:
    label = _SAFE_LABEL_RE.sub("_", str(value or "").strip()).strip("._-")
    return (label[:80] or fallback)


def _next_snapshot_name(source: str) -> str:
    now = datetime.now().astimezone()
    timestamp = now.strftime("%Y%m%d_%H%M%S_%f")
    with _SEQUENCE_LOCK:
        sequence = next(_SEQUENCE)
    return (
        f"prompt_{timestamp}_{os.getpid()}_{sequence:06d}_"
        f"{_safe_label(source, fallback='llm')}.json"
    )


def _request_body_copy(request_kwargs: Mapping[str, Any]) -> dict[str, Any]:
    """Create a JSON-safe copy while dropping non-prompt transport objects."""

    filtered = {
        str(key): value
        for key, value in request_kwargs.items()
        if key not in _TRANSPORT_ONLY_KEYS and value is not None
    }
    # A JSON round-trip creates an independent structure without relying on
    # deepcopy support from provider SDK objects.  ``default=str`` preserves a
    # complete diagnostic representation for uncommon scalar wrapper types.
    return json.loads(json.dumps(filtered, ensure_ascii=False, default=str))


def _redacted_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Redact after serialization so secrets nested anywhere are covered."""

    from agent.redact import redact_sensitive_text

    serialized = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return json.loads(redact_sensitive_text(serialized, force=True))


def _ensure_private_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        # Existing directories may predate the monitor or have been created
        # under a permissive umask. Prompt snapshots deserve an explicit mode.
        os.chmod(directory, 0o700)


def _enforce_retention(directory: Path, max_files: int) -> None:
    """Best-effort count-based retention after a successful atomic write."""

    try:
        paths = sorted(directory.glob(PROMPT_MONITOR_GLOB))
        for stale in paths[:-max_files]:
            try:
                stale.unlink()
            except OSError:
                logger.debug("Could not remove old prompt snapshot %s", stale)
    except OSError:
        logger.debug("Could not enforce prompt monitor retention", exc_info=True)


def capture_llm_request(
    request_kwargs: Mapping[str, Any],
    *,
    source: str,
    session_id: Optional[str] = None,
    provider: Optional[str] = None,
    api_mode: Optional[str] = None,
    task: Optional[str] = None,
    attempt: Optional[int] = None,
    request_id: Optional[str] = None,
    reason: str = "provider_dispatch",
    settings: Optional[PromptMonitorSettings] = None,
    hermes_home: Optional[Path] = None,
) -> Optional[Path]:
    """Persist one finalized provider request, or no-op when capture is off.

    This function is fail-open by design: monitoring must never prevent an LLM
    call.  The live ``request_kwargs`` mapping is read only and never modified.
    """

    resolved = settings or load_prompt_monitor_settings()
    if not resolved.enabled:
        return None
    if source == "auxiliary" and not resolved.include_auxiliary:
        return None

    try:
        body = _request_body_copy(request_kwargs)
        captured_at = datetime.now().astimezone().isoformat(timespec="microseconds")
        payload = _redacted_payload(
            {
                "schema_version": PROMPT_MONITOR_SCHEMA_VERSION,
                "captured_at": captured_at,
                "source": source,
                "session_id": str(session_id or ""),
                "provider": str(provider or ""),
                "model": str(body.get("model") or ""),
                "api_mode": str(api_mode or ""),
                "task": str(task or ""),
                "attempt": int(attempt) if attempt is not None else None,
                "request_id": str(request_id or ""),
                "reason": reason,
                "request": {"body": body},
            }
        )
        directory = prompt_monitor_directory(hermes_home)
        _ensure_private_directory(directory)
        path = directory / _next_snapshot_name(source)
        atomic_json_write(path, payload, mode=0o600, default=str)
        _enforce_retention(directory, resolved.max_files)
        return path
    except Exception:
        # Never include request data in this warning. A serialization failure
        # may itself involve sensitive provider objects.
        logger.warning("Prompt monitor could not persist an LLM request", exc_info=True)
        return None


__all__ = [
    "PROMPT_MONITOR_DIRNAME",
    "PROMPT_MONITOR_GLOB",
    "PROMPT_MONITOR_SCHEMA_VERSION",
    "PromptMonitorSettings",
    "capture_llm_request",
    "load_prompt_monitor_settings",
    "prompt_monitor_directory",
]
