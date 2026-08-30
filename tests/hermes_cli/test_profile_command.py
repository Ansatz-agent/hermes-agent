from __future__ import annotations

import json

import pytest

from hermes_cli.profile_command import (
    ProfileBrowseError,
    format_profile_category,
    read_profile_payload,
)


class FakeBrowseProvider:
    def __init__(self, payload):
        self.payload = payload
        self.initialize_kwargs = None
        self.shutdown_called = False

    @staticmethod
    def get_tool_schemas():
        return [{"name": "profile_browse"}]

    def initialize(self, **kwargs):
        self.initialize_kwargs = kwargs

    def handle_tool_call(self, name, args):
        assert name == "profile_browse"
        assert args == {"scope": "latex"}
        return json.dumps(self.payload)

    def shutdown(self):
        self.shutdown_called = True


def test_read_profile_payload_uses_temporary_read_only_provider_before_agent_init(
    monkeypatch, tmp_path
):
    payload = {
        "resolved_scope": "profile://preferences/workflow/artifacts/latex",
        "scope_found": True,
        "categories": [],
        "items": [],
    }
    provider = FakeBrowseProvider(payload)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "profile_memory"}},
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "plugins.memory.load_memory_provider", lambda *args, **kwargs: provider
    )

    result = read_profile_payload(agent=None, session_id="cli-session", scope="latex")

    assert result == payload
    assert provider.initialize_kwargs == {
        "session_id": "cli-session",
        "hermes_home": str(tmp_path),
        "platform": "cli",
        "agent_context": "primary",
        "read_only": True,
    }
    assert provider.shutdown_called is True


def test_read_profile_payload_reports_provider_errors_and_still_shuts_down(
    monkeypatch, tmp_path
):
    provider = FakeBrowseProvider(
        {
            "error": "profile category is ambiguous",
            "candidates": ["profile://one/latex", "profile://two/latex"],
        }
    )
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "profile_memory"}},
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "plugins.memory.load_memory_provider", lambda *args, **kwargs: provider
    )

    with pytest.raises(ProfileBrowseError, match="profile://one/latex"):
        read_profile_payload(agent=None, scope="latex")

    assert provider.shutdown_called is True


def test_read_profile_payload_wraps_provider_initialization_failures(
    monkeypatch, tmp_path
):
    provider = FakeBrowseProvider({})

    def fail_initialize(**_kwargs):
        raise OSError("database unavailable")

    provider.initialize = fail_initialize
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"memory": {"provider": "profile_memory"}},
    )
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        "plugins.memory.load_memory_provider", lambda *args, **kwargs: provider
    )

    with pytest.raises(ProfileBrowseError, match="database unavailable"):
        read_profile_payload(agent=None)

    assert provider.shutdown_called is True


def test_missing_category_format_points_back_to_directory():
    lines = format_profile_category(
        {"scope_found": False, "categories": [], "items": []}, "unknown"
    )

    rendered = "\n".join(lines)
    assert "Profile category not found: unknown" in rendered
    assert "Use /profile" in rendered
