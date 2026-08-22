"""Fail-closed download environment shared by install, repair and lazy tools.

The compact packaged policy is a build/runtime snapshot of the corresponding
entries in ``docs/security/hermes-managed-download-origins.json``. Tests keep
that snapshot exact so packaged installations do not need to ship docs or CI
evidence in order to enforce the same product policy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Final


MANAGED_DOWNLOAD_PHASES: Final = frozenset(
    {"auth-payload-build", "runtime-install", "repair", "update", "lazy-feature"}
)

_SAFE_ENV_KEYS: Final = frozenset(
    {
        "APPDATA",
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "LOGNAME",
        "PATH",
        "SHELL",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)

_POLICY_PATH: Final = Path(__file__).with_name("data") / "managed-download-origins.json"


@lru_cache(maxsize=1)
def _origin_entries() -> dict[str, dict[str, object]]:
    raw = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise RuntimeError("unsupported managed download policy schema")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("managed download policy entries are missing")
    result: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise RuntimeError("invalid managed download policy entry")
        result[entry["id"]] = entry
    return result


def _origin(entry_id: str, field: str) -> str:
    value = _origin_entries().get(entry_id, {}).get(field)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise RuntimeError(f"managed download origin {entry_id}.{field} is unavailable")
    return value


def managed_download_environment(
    phase: str,
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an immutable-by-copy, domestic-first subprocess environment.

    Only explicit OS/session variables cross the boundary. Package-manager
    configuration, Python injection, proxy redirects and model credentials from
    the parent process are discarded before registered values are installed.
    """

    if phase not in MANAGED_DOWNLOAD_PHASES:
        raise ValueError(f"unknown managed download phase: {phase}")

    inherited = os.environ if source is None else source
    env = {
        key: value
        for key, value in inherited.items()
        if key in _SAFE_ENV_KEYS and isinstance(value, str)
    }
    null_config = "NUL" if "SYSTEMROOT" in inherited or "WINDIR" in inherited else os.devnull

    python_primary = _origin("python-packages", "domestic_primary")
    python_secondary = _origin("python-packages", "domestic_secondary")
    npm_primary = _origin("npm-packages", "domestic_primary")
    node_primary = _origin("node-runtime", "domestic_primary")
    node_secondary = _origin("node-runtime", "domestic_secondary")
    electron_primary = _origin("electron-runtime", "domestic_primary")
    electron_secondary = _origin("electron-runtime", "domestic_secondary")
    playwright_primary = _origin("playwright-browser", "domestic_primary")
    playwright_secondary = _origin("playwright-browser", "domestic_secondary")

    env.update(
        {
            "UV_NO_CONFIG": "1",
            "UV_DEFAULT_INDEX": python_primary,
            "UV_INDEX": python_primary,
            "PIP_CONFIG_FILE": null_config,
            "PIP_INDEX_URL": python_primary,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_INPUT": "1",
            "HERMES_UV_FALLBACK_INDEX": python_secondary,
            "NPM_CONFIG_USERCONFIG": null_config,
            "NPM_CONFIG_REGISTRY": npm_primary,
            "npm_config_registry": npm_primary,
            "NPM_CONFIG_FUND": "false",
            "NPM_CONFIG_AUDIT": "false",
            "NODEJS_ORG_MIRROR": node_primary,
            "HERMES_NODE_MIRROR": node_primary,
            "HERMES_NODE_FALLBACK_MIRROR": node_secondary,
            "ELECTRON_MIRROR": electron_primary,
            "HERMES_ELECTRON_FALLBACK_MIRROR": electron_secondary,
            "ELECTRON_BUILDER_BINARIES_MIRROR": _origin(
                "electron-builder-binaries", "domestic_primary"
            ),
            "PLAYWRIGHT_DOWNLOAD_HOST": playwright_primary,
            "HERMES_PLAYWRIGHT_FALLBACK_MIRROR": playwright_secondary,
            "HF_HUB_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "CI": "1",
        }
    )
    return env


def managed_origin(entry_id: str, field: str = "domestic_primary") -> str:
    """Return a registered origin; unknown IDs/fields fail closed."""

    return _origin(entry_id, field)
