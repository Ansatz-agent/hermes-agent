"""A packaged Desktop update must provision its build runtime and finish the build."""

import os
import subprocess
from unittest.mock import Mock

import pytest

import hermes_constants
from hermes_cli import bundled_update, main, update_cmd


@pytest.fixture
def desktop_update(tmp_path, monkeypatch):
    desktop = tmp_path / "hermes-agent/apps/desktop"
    desktop.mkdir(parents=True)
    (desktop / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("ANSATZ_DESKTOP_UPDATE_APP", str(tmp_path / "Ansatz.app"))
    monkeypatch.setattr(main, "PROJECT_ROOT", desktop.parents[1])
    monkeypatch.setattr(main, "_desktop_build_needed", lambda *_a, **_k: True)
    monkeypatch.setattr(bundled_update, "prepare_desktop_rebuild", Mock())
    build = Mock(return_value=subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(main, "_run_logged_subprocess", build)
    return desktop, build


def test_missing_node_provisions_before_build_and_propagates_managed_path(desktop_update, monkeypatch, tmp_path):
    desktop, build = desktop_update
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(main, "_resolve_node_runtime_npm", lambda: None)
    node_bin = tmp_path / "node/bin"

    def provision():
        assert not build.called
        node_bin.mkdir(parents=True)
        return str(node_bin / "npm")

    bootstrap = Mock(side_effect=provision)
    monkeypatch.setattr(hermes_constants, "bootstrap_hermes_managed_node", bootstrap)
    update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=False)

    bootstrap.assert_called_once_with()
    build.assert_called_once()
    command = build.call_args.args[0]
    assert command[-3:] == ["desktop", "--build-only", "--force-build"]
    env = build.call_args.kwargs["env"]
    assert str(node_bin) in env["PATH"].split(os.pathsep)
    assert env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] == "1"


def test_failed_provisioning_stops_before_payload_and_build(desktop_update, monkeypatch):
    desktop, build = desktop_update
    monkeypatch.setattr(main, "_resolve_node_runtime_npm", lambda: None)
    monkeypatch.setattr(hermes_constants, "bootstrap_hermes_managed_node", lambda: None)

    with pytest.raises(RuntimeError, match="Node.js could not be prepared"):
        update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=False)

    bundled_update.prepare_desktop_rebuild.assert_not_called()
    build.assert_not_called()


def test_existing_npm_is_reused(desktop_update, monkeypatch):
    desktop, build = desktop_update
    monkeypatch.setattr(main, "_resolve_node_runtime_npm", lambda: "npm")
    bootstrap = Mock()
    monkeypatch.setattr(hermes_constants, "bootstrap_hermes_managed_node", bootstrap)
    update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=True)
    bootstrap.assert_not_called()
    build.assert_called_once()


def test_missing_source_is_distinguished_from_missing_node(desktop_update, monkeypatch):
    desktop, build = desktop_update
    (desktop / "package.json").unlink()
    bootstrap = Mock()
    monkeypatch.setattr(hermes_constants, "bootstrap_hermes_managed_node", bootstrap)
    with pytest.raises(RuntimeError, match="Desktop source is missing"):
        update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=False)
    bootstrap.assert_not_called()
    build.assert_not_called()


def test_packaged_update_build_failure_is_fatal_after_bounded_retry(desktop_update, monkeypatch, capsys):
    desktop, build = desktop_update
    monkeypatch.setattr(main, "_resolve_node_runtime_npm", lambda: "npm")
    build.return_value = subprocess.CompletedProcess([], 9, stdout="fixture build failure")
    with pytest.raises(RuntimeError, match="Desktop rebuild failed; the previous app was kept"):
        update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=True)
    assert build.call_count == 2
    out = capsys.readouterr().out
    assert "fixture build failure" in out
    assert "Desktop app up to date" not in out
    assert "non-fatal" not in out


def test_cli_without_desktop_does_not_install_node(desktop_update, monkeypatch):
    desktop, build = desktop_update
    monkeypatch.delenv("ANSATZ_DESKTOP_UPDATE_APP")
    monkeypatch.setattr(update_cmd, "_desktop_app_present", lambda _path: False)
    bootstrap = Mock()
    monkeypatch.setattr(hermes_constants, "bootstrap_hermes_managed_node", bootstrap)
    update_cmd._rebuild_desktop_after_update(desktop, had_desktop_app_before_update=False)
    bootstrap.assert_not_called()
    build.assert_not_called()
