"""Fixtures shared across hermes_cli kanban tests."""

from __future__ import annotations

import pytest

from hermes_cli.client_auth import guard as _auth_guard


_production_enforce_raw_argv = _auth_guard.enforce_raw_argv


def _allow_pytest_collection(_argv):
    """Keep pytest's own argv from being interpreted as a Hermes launch."""


# Several legacy test modules import ``hermes_cli.main`` at module scope. The
# production module deliberately guards before heavyweight imports, so this
# test-only seam must be active during collection, before fixtures can run.
# Subprocess tests get a fresh interpreter and therefore still exercise the
# real production gate.
_auth_guard.enforce_raw_argv = _allow_pytest_collection


def pytest_collection_finish(session):
    del session
    if _auth_guard.enforce_raw_argv is _allow_pytest_collection:
        _auth_guard.enforce_raw_argv = _production_enforce_raw_argv


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Hermes profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_hermes_gate(request, monkeypatch):
    """Default ``_detect_concurrent_hermes_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``hermes.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``hermes`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_hermes_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        # main.py now enforces the production auth gate at import time, before
        # it exposes the update helper this fixture patches. This explicit
        # in-process test seam prevents pytest's own argv from being treated as
        # a Hermes launch. Subprocess guard tests do not inherit the monkeypatch.
        monkeypatch.setattr(_auth_guard, "enforce_raw_argv", lambda _argv: None)
        from hermes_cli import main as _cli_main
    except Exception:
        return
    # raising=False: under pytest's per-test spawn isolation, a concurrent
    # xdist worker importing a module that transitively touches hermes_cli.main
    # can briefly expose a partially-initialized module object here — one where
    # _detect_concurrent_hermes_instances isn't defined yet. A bare setattr
    # would raise AttributeError and error the (unrelated) test. The attribute
    # always exists once main.py finishes importing, so a no-op when it's
    # transiently absent is the correct, race-free default.
    monkeypatch.setattr(
        _cli_main,
        "_detect_concurrent_hermes_instances",
        lambda *_a, **_k: [],
        raising=False,
    )
