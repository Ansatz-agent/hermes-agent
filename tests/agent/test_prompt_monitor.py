"""Contracts for passive finalized-LLM-request monitoring."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from agent.prompt_monitor import (
    PromptMonitorSettings,
    capture_llm_request,
    load_prompt_monitor_settings,
    prompt_monitor_directory,
)
from hermes_cli.prompt_monitor import monitor_prompts


def _request(secret: str = "sk-test-not-a-real-prompt-monitor-key-1234567890") -> dict:
    return {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "stable instructions"},
            {
                "role": "user",
                "content": (
                    "<object-card id=\"obj_123\">contains: Python code</object-card>\n"
                    f"credential={secret}"
                ),
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "retrieve_object",
                    "parameters": {"type": "object"},
                },
            }
        ],
        "timeout": 30,
        "extra_headers": {"Authorization": f"Bearer {secret}"},
        "_moa_prepared_request": object(),
    }


def test_settings_are_disabled_by_default_and_clamp_retention():
    assert load_prompt_monitor_settings({}) == PromptMonitorSettings()

    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert load_prompt_monitor_settings(DEFAULT_CONFIG) == PromptMonitorSettings()

    settings = load_prompt_monitor_settings(
        {
            "logging": {
                "prompt_monitor": {
                    "enabled": True,
                    "include_auxiliary": False,
                    "max_files": 0,
                }
            }
        }
    )
    assert settings == PromptMonitorSettings(
        enabled=True,
        include_auxiliary=False,
        max_files=1,
    )


def test_capture_is_disabled_without_side_effects(tmp_path: Path):
    result = capture_llm_request(
        _request(),
        source="main",
        settings=PromptMonitorSettings(enabled=False),
        hermes_home=tmp_path,
    )
    assert result is None
    assert not prompt_monitor_directory(tmp_path).exists()


def test_capture_preserves_prompt_view_redacts_and_drops_transport_fields(
    tmp_path: Path,
):
    secret = "sk-test-not-a-real-prompt-monitor-key-1234567890"
    request = _request(secret)
    original_messages = json.loads(json.dumps(request["messages"]))

    path = capture_llm_request(
        request,
        source="main",
        session_id="../../session",
        provider="custom",
        api_mode="chat_completions",
        task="conversation",
        attempt=2,
        request_id="req-1",
        settings=PromptMonitorSettings(enabled=True, max_files=10),
        hermes_home=tmp_path,
    )

    assert path is not None and path.exists()
    payload = json.loads(path.read_text(encoding="utf-8"))
    body = payload["request"]["body"]
    rendered = json.dumps(payload, ensure_ascii=False)
    assert payload["source"] == "main"
    assert payload["session_id"] == "../../session"
    assert payload["attempt"] == 2
    assert payload["request_id"] == "req-1"
    assert "<object-card" in body["messages"][1]["content"]
    assert body["tools"][0]["function"]["name"] == "retrieve_object"
    assert secret not in rendered
    assert "timeout" not in body
    assert "extra_headers" not in body
    assert "_moa_prepared_request" not in body
    # Monitoring must not rewrite the live provider request.
    assert request["messages"] == original_messages
    assert request["timeout"] == 30

    if stat.S_IMODE(path.stat().st_mode):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_auxiliary_capture_can_be_excluded(tmp_path: Path):
    path = capture_llm_request(
        _request(),
        source="auxiliary",
        settings=PromptMonitorSettings(
            enabled=True,
            include_auxiliary=False,
        ),
        hermes_home=tmp_path,
    )
    assert path is None
    assert not prompt_monitor_directory(tmp_path).exists()


def test_capture_retains_only_configured_number_of_complete_snapshots(tmp_path: Path):
    settings = PromptMonitorSettings(enabled=True, max_files=2)
    for index in range(4):
        path = capture_llm_request(
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": f"turn-{index}"}],
            },
            source="main",
            attempt=index,
            settings=settings,
            hermes_home=tmp_path,
        )
        assert path is not None

    paths = sorted(prompt_monitor_directory(tmp_path).glob("prompt_*.json"))
    assert len(paths) == 2
    attempts = [json.loads(path.read_text())["attempt"] for path in paths]
    assert attempts == [2, 3]


def test_cli_once_prints_full_request_body(monkeypatch, tmp_path: Path, capsys):
    settings = PromptMonitorSettings(enabled=True, max_files=10)
    capture_llm_request(
        {
            "model": "test-model",
            "messages": [
                {
                    "role": "user",
                    "content": "<object-card id=\"obj_demo\">demo Card</object-card>",
                }
            ],
        },
        source="main",
        session_id="session-demo",
        settings=settings,
        hermes_home=tmp_path,
    )
    monkeypatch.setattr(
        "hermes_cli.prompt_monitor.load_prompt_monitor_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        "hermes_cli.prompt_monitor.prompt_monitor_directory",
        lambda: prompt_monitor_directory(tmp_path),
    )

    rc = monitor_prompts(once=True)

    output = capsys.readouterr().out
    assert rc == 0
    assert "LLM PROMPT" in output
    assert "session-demo" in output
    assert "<object-card" in output
    assert "demo Card" in output


def test_cli_refuses_to_watch_when_capture_is_disabled(monkeypatch, capsys):
    monkeypatch.setattr(
        "hermes_cli.prompt_monitor.load_prompt_monitor_settings",
        lambda: PromptMonitorSettings(enabled=False),
    )

    assert monitor_prompts(once=True) == 2
    output = capsys.readouterr().out
    assert "logging.prompt_monitor.enabled true" in output
