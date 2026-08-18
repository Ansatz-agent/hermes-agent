"""Test-only launcher for production entries behind an authenticated owner."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


_AUTHENTICATED_MODULE = r"""
import runpy
import sys

from hermes_cli.client_auth.runtime import (
    AuthRequired,
    OwnerBroker,
    RuntimeConsumer,
    RuntimeSnapshot,
    connect_runtime_owner,
    install_entrypoint_owner,
)


class _AuthenticatedOwner:
    def __init__(self):
        self._snapshot = RuntimeSnapshot.new_authenticated(
            "test-user",
            now=0.0,
            ttl=10**12,
        )

    def refresh(self):
        return self._snapshot

    def snapshot(self):
        return self._snapshot

    def connect_consumer(self, *, profile=None):
        del profile
        return RuntimeConsumer(
            self._snapshot,
            liveness_probe=lambda: True,
            clock=lambda: 0.0,
        )


module = sys.argv[1]
sys.argv = [module, *sys.argv[2:]]
try:
    active_owner = connect_runtime_owner()
    active_owner.authorize(
        "test.harness",
        expected=active_owner.snapshot().scope,
    )
except AuthRequired:
    active_owner = _AuthenticatedOwner()
    try:
        _broker = OwnerBroker.start(active_owner)
    except AuthRequired:
        active_owner = connect_runtime_owner()
install_entrypoint_owner(active_owner)
runpy.run_module(module, run_name="__main__")
"""


def authenticated_module_command(module: str, *args: str) -> list[str]:
    return [sys.executable, "-u", "-c", _AUTHENTICATED_MODULE, module, *args]


def authenticated_subprocess_environment(
    base: dict[str, str] | None = None,
) -> tuple[dict[str, str], Path]:
    environment = dict(os.environ if base is None else base)
    runtime_root = Path(tempfile.mkdtemp(prefix="ha-test-"))
    site_root = Path(__file__).resolve().parent / "auth_site"
    repo_root = Path(__file__).resolve().parents[3]
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(site_root), str(repo_root), existing) if part
    )
    environment["HERMES_AUTH_TEST_RUNTIME_ROOT"] = str(runtime_root)
    return environment, runtime_root
