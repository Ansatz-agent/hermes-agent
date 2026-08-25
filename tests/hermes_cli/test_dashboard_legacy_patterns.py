"""Legacy ``hermes`` cmdlines must stay discoverable across the ansatz migration.

Pre-upgrade dashboards/servers (and processes started via the compatibility
aliases) still run under the ``hermes`` command name. The update-time process
scan and the dashboard runtime parser must recognise both spellings, or those
processes silently survive updates with a stale backend in memory.
"""

import sys
from unittest import mock

import pytest

from hermes_cli import dashboard_procs
from hermes_cli.main import _parse_dashboard_runtime


@pytest.mark.skipif(sys.platform == "win32", reason="exercises the POSIX ps branch")
def test_scan_matches_both_ansatz_and_legacy_hermes_cmdlines(monkeypatch):
    stdout = "\n".join(
        [
            "  101 /venv/bin/ansatz dashboard --port 9119",
            "  102 /venv/bin/hermes dashboard --port 9119",
            "  103 /venv/bin/ansatz serve --port 0",
            "  104 /venv/bin/hermes serve --port 0",
            "  105 /venv/bin/hermes chat --query serve",
        ]
    )
    monkeypatch.setattr(
        dashboard_procs.subprocess,
        "run",
        lambda *args, **kwargs: mock.Mock(returncode=0, stdout=stdout),
    )

    pids = {pid for pid, _cmd in dashboard_procs._scan_dashboard_processes()}

    assert pids == {101, 102, 103, 104}


def test_parse_dashboard_runtime_accepts_legacy_hermes_aliases():
    assert _parse_dashboard_runtime("/venv/bin/hermes dashboard --port 9200") == (
        "dashboard",
        "127.0.0.1",
        9200,
    )
    assert _parse_dashboard_runtime("/venv/bin/hermes serve --host 0.0.0.0 --port 8000") == (
        "serve",
        "0.0.0.0",
        8000,
    )
    assert _parse_dashboard_runtime("/venv/bin/ansatz dashboard") == (
        "dashboard",
        "127.0.0.1",
        9119,
    )
    assert _parse_dashboard_runtime("/venv/bin/hermes chat") is None
