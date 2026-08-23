"""Regression test: the stdio TUI entry point prewarms the /model picker cache.

The classic CLI run() loop calls ``prewarm_picker_cache_async()`` during the
idle window after the banner, so the first ``/model`` open hits a warm
provider-models disk cache. The stdio TUI entry point (``entry.main()``) never
did — the first ``/model`` open in a TUI session blocked on serial /v1/models
fetches for every authenticated provider (#72021).

These tests pin the entrypoint wiring itself (the helper's own worker/once
guard is covered in ``tests/hermes_cli/test_picker_prewarm.py``):

- ``main()`` invokes ``hermes_cli.model_switch.prewarm_picker_cache_async``
  exactly once, AFTER the ``gateway.ready`` event is written (banner shown,
  user about to type — the idle window the prewarm is meant to fill).
- The startup path stays non-blocking: with the prewarm spied out, ``main()``
  proceeds into the stdin read loop and returns normally on EOF.
- A prewarm import/start failure is swallowed (fire-and-forget contract) and
  must not prevent ``main()`` from reaching the read loop.

Harness: same style as tests/test_tui_entry_mcp_owner.py — import
``tui_gateway.entry`` and monkeypatch its module attributes, running the real
``main()`` with stubbed I/O collaborators (no subprocess, no real gateway).
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import hermes_cli.model_switch as ms
from tui_gateway import entry


@dataclass(frozen=True)
class _Authenticated:
    state: str = "authenticated"
    username: str = "alice"
    runtime_instance_id: str = "0123456789abcdef0123456789abcdef"
    epoch: int = 1
    valid_until: float = 9999.0
    session_expires_at: str | None = None
    reason: str | None = None

    def public_dict(self):
        return self.__dict__.copy()


class _Auth:
    def status(self):
        return _Authenticated()

    def require(self, _boundary, _status):
        return None


def _run_startup(monkeypatch, events, *, prewarm=None):
    """Start the real auth shell + lazy gateway adapter, recording ordering.

    ``events`` receives ``("write", <event type>)`` for every emitted frame
    and ``("prewarm",)`` when the spy fires, in call order.
    """
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: None)
    gateway = entry._ServerGateway()
    gateway._sidecar_installed = True
    gateway._server = SimpleNamespace(
        _ensure_skin_watcher=lambda: None,
        _shutdown_sessions=lambda: None,
        resolve_skin=lambda: "default",
    )

    def emit(payload):
        params = payload.get("params") or {}
        events.append(("write", params.get("type") or payload.get("method")))
        return True

    # entry.main() imports the helper lazily from hermes_cli.model_switch,
    # so the spy must live on that module, not on entry.
    if prewarm is None:
        def prewarm():
            events.append(("prewarm",))
            return None  # fire-and-forget handle; never blocks

    monkeypatch.setattr(ms, "prewarm_picker_cache_async", prewarm)

    entry.AccountAuthShell(_Auth(), gateway, emit).start()


def test_main_prewarms_picker_cache_after_gateway_ready(monkeypatch):
    """main() must call the prewarm helper once, after gateway.ready is
    written, and still reach the stdin loop (returns on EOF = non-blocking)."""
    events: list[tuple] = []

    _run_startup(monkeypatch, events)

    prewarm_calls = [e for e in events if e[0] == "prewarm"]
    assert len(prewarm_calls) == 1, (
        f"main() must invoke prewarm_picker_cache_async exactly once, got {events!r}"
    )

    ready_idx = events.index(("write", "gateway.ready"))
    prewarm_idx = events.index(("prewarm",))
    assert ready_idx < prewarm_idx, (
        "prewarm must fire AFTER the gateway.ready write (idle window, "
        f"banner already shown); order was {events!r}"
    )


def test_main_survives_prewarm_failure(monkeypatch):
    """Fire-and-forget contract: a prewarm that raises at start must be
    swallowed and main() must still reach the read loop and exit cleanly."""
    events: list[tuple] = []

    def _boom():
        events.append(("prewarm",))
        raise RuntimeError("provider registry exploded")

    _run_startup(monkeypatch, events, prewarm=_boom)  # must not raise

    assert ("prewarm",) in events
    assert ("write", "gateway.ready") in events
