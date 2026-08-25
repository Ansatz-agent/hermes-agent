from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from cli import HermesCLI
from hermes_cli.commands import resolve_command
from hermes_cli.object_context_command import (
    LEGACY_V0_KEYS,
    OBJECT_CONTEXT_ENGINE,
    PARAMETER_SPECS,
    ObjectContextCommandError,
    active_context_engine_monitor,
    active_context_engine_name,
    active_context_engine_status,
    parse_parameter_value,
    persisted_context_engine_telemetry,
    run_object_context_command,
)
from plugins.context_engine.object_context.store import ObjectContextStore


def _write_config(tmp_path, text: str) -> None:
    (tmp_path / "config.yaml").write_text(text, encoding="utf-8")


def _read_config(tmp_path) -> dict:
    return yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))


def test_registry_exposes_cli_only_command_and_aliases():
    command = resolve_command("object_context")
    assert command is not None
    assert command.cli_only is True
    assert set(command.subcommands) == {
        "status",
        "stats",
        "monitor",
        "on",
        "off",
        "set",
        "reset",
        "help",
    }
    assert resolve_command("object-context") is command
    assert resolve_command("oc") is command


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("hot_tail_max_deltas", "4", 4),
        ("object_prefilter_min_tokens", "+128", 128),
        ("hot_tail_token_budget_ratio", "0.15", 0.15),
        ("min_relative_saving_ratio", "0", 0.0),
        ("retrieval_max_tokens_ratio", "1", 1.0),
    ],
)
def test_parameter_parser_accepts_supported_values(name, raw, expected):
    value = parse_parameter_value(name, raw)
    assert value == expected
    assert type(value) is type(expected)


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("unknown", "1"),
        ("hot_tail_max_deltas", "1.5"),
        ("hot_tail_max_deltas", "true"),
        ("hot_tail_max_deltas", "0"),
        ("summary_max_tokens", "7"),
        ("hot_tail_token_budget_ratio", "0.009"),
        ("context_soft_limit_ratio", "1.1"),
        ("min_relative_saving_ratio", "nan"),
        ("retrieval_max_tokens_ratio", "inf"),
    ],
)
def test_parameter_parser_rejects_unknown_wrong_type_and_out_of_range(name, raw):
    with pytest.raises(ObjectContextCommandError):
        parse_parameter_value(name, raw)


def test_status_shows_effective_defaults_active_mismatch_and_v0_warning(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        "context:\n"
        "  engine: object_context\n"
        "  object_context:\n"
        "    min_code_chars: 160\n"
        "    max_read_lines: 300\n",
    )

    result = run_object_context_command("status", active_engine="compressor")
    text = "\n".join(result.lines)

    assert "Configured: ON (object_context)" in text
    assert "Active session: OFF (compressor)" in text
    assert "Restart pending" in text
    for name, spec in PARAMETER_SPECS.items():
        assert name in text
        assert str(spec.default) in text
    for key in LEGACY_V0_KEYS:
        assert key in text
    assert "Ignored V0 settings" in text


def _live_v1_status() -> dict:
    return {
        "object_context_version": 1,
        "object_context_available": True,
        "request_projection_count": 2,
        "last_request_metrics": {
            "raw_context_tokens": 10_000,
            "rendered_context_tokens": 3_000,
            "tokens_saved": 7_000,
            "hot_tail_tokens": 1_250,
        },
        "request_metric_totals": {
            "raw_context_tokens": 25_000,
            "rendered_context_tokens": 7_000,
            "tokens_saved": 18_000,
            "hot_tail_tokens": 2_500,
        },
        "metric_totals": {"retrieved_tokens": 1_200},
        "retrieval_count": 1,
        "working_memory_object_count": 3,
        "working_memory_bytes": 2_048,
    }


def _live_timeline() -> dict:
    return {
        "schema_version": 2,
        "conversation_id": "conversation-a",
        "session_id": "session-a",
        "projections": [
            {
                "projection_id": "projection-1",
                "projection_sequence": 1,
                "turn_id": "turn-a",
                "session_id": "session-a",
                "legacy": False,
                "metrics": {
                    "raw_context_tokens": 100,
                    "rendered_context_tokens": 40,
                    "tokens_saved": 60,
                    "projection_latency_ms": 1.5,
                },
            }
        ],
        "cache_requests": [
            {
                "cache_request_id": "session-a:cache:1",
                "request_sequence": 1,
                "turn_id": "turn-a",
                "session_id": "session-a",
                "metrics": {
                    "prompt_tokens": 100,
                    "uncached_input_tokens": 40,
                    "cache_read_tokens": 60,
                    "cache_write_tokens": 0,
                    "prompt_cache_hit_ratio": 0.6,
                },
            }
        ],
    }


def _seed_persisted_timeline(tmp_path) -> None:
    store = ObjectContextStore(
        tmp_path / "context" / "object_context_v1.sqlite3"
    )
    store.record_metrics(
        "conversation-a",
        {
            "raw_context_tokens": 100,
            "rendered_context_tokens": 40,
            "tokens_saved": 60,
            "projection_latency_ms": 1.5,
        },
        metadata={
            "event": "request_projection",
            "projection_id": "persisted-projection-1",
            "projection_sequence": 1,
            "turn_id": "turn-a",
            "session_id": "session-a",
        },
    )
    store.record_metrics(
        "conversation-a",
        {
            "prompt_tokens": 100,
            "uncached_input_tokens": 40,
            "cache_read_tokens": 60,
            "cache_write_tokens": 0,
            "prompt_cache_hit_ratio": 0.6,
        },
        metadata={
            "event": "provider_cache_usage",
            "cache_request_id": "session-a:cache:1",
            "cache_request_sequence": 1,
            "turn_id": "turn-a",
            "session_id": "session-a",
        },
    )


def test_status_directly_summarizes_live_v1_savings(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path, "context:\n  engine: object_context\n")

    result = run_object_context_command(
        "status",
        active_engine="object_context",
        engine_status=_live_v1_status(),
    )
    text = "\n".join(result.lines)

    assert "Last projection: ~7,000 avoided (70.0%)" in text
    assert "Session:         ~18,000 avoided across 2 projected requests" in text
    assert "Details: /object_context stats" in text


def test_stats_displays_request_only_savings_retrieval_and_memory():
    result = run_object_context_command(
        "stats",
        active_engine="object_context",
        engine_status=_live_v1_status(),
    )
    text = "\n".join(result.lines)

    assert "Raw conversation view" in text and "~10,000" in text
    assert "Rendered V1 conversation view" in text and "~3,000" in text
    assert "Tokens avoided" in text and "~18,000" in text
    assert "Average avoided / request" in text and "~9,000" in text
    assert "Successful retrievals" in text and "1" in text
    assert "Retrieved payload tokens" in text and "~1,200" in text
    assert "Working Memory objects" in text and "3" in text
    assert "2.0 KiB" in text
    assert "already reflect retrieval projection" in text
    assert result.changed is False


def test_monitor_writes_current_conversation_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    result = run_object_context_command(
        "monitor",
        active_engine="object_context",
        engine_status=_live_v1_status(),
        monitor_timeline=_live_timeline(),
    )

    path = tmp_path / "logs" / "object-context-monitor"
    assert result.changed is False
    assert result.artifact_path
    assert str(path) in result.artifact_path
    assert "Projects: 1" in "\n".join(result.lines)
    assert "Opening the private local HTML dashboard" in "\n".join(result.lines)
    assert "Session Dynamics Monitor" in Path(result.artifact_path).read_text(
        encoding="utf-8"
    )


def test_monitor_explains_missing_data_without_writing_artifact():
    result = run_object_context_command(
        "monitor",
        active_engine="object_context",
        engine_status=_live_v1_status(),
        monitor_timeline={"projections": []},
    )

    assert result.artifact_path == ""
    assert "No request projection" in "\n".join(result.lines)


@pytest.mark.parametrize("active_engine", ["", "compressor"])
def test_stats_explains_when_live_v1_is_not_active(active_engine):
    result = run_object_context_command("stats", active_engine=active_engine)
    text = "\n".join(result.lines)

    assert "Unavailable" in text
    assert "/object_context on" in text


def test_on_and_off_persist_engine_without_dropping_unrelated_config(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        "model:\n"
        "  default: test-model\n"
        "context:\n"
        "  engine: compressor\n"
        "  object_context:\n"
        "    min_code_chars: 160\n",
    )

    enabled = run_object_context_command("on", active_engine="compressor")
    assert enabled.changed is True
    saved = _read_config(tmp_path)
    assert saved["context"]["engine"] == OBJECT_CONTEXT_ENGINE
    assert saved["context"]["object_context"]["min_code_chars"] == 160
    assert saved["model"]["default"] == "test-model"

    unchanged = run_object_context_command("enable", active_engine="compressor")
    assert unchanged.changed is False

    disabled = run_object_context_command("off", active_engine=OBJECT_CONTEXT_ENGINE)
    assert disabled.changed is True
    assert _read_config(tmp_path)["context"]["engine"] == "compressor"


def test_set_persists_typed_override_without_implicitly_enabling_engine(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path, "context:\n  engine: compressor\n")

    first = run_object_context_command(
        "set hot_tail_max_deltas 4", active_engine="compressor"
    )
    second = run_object_context_command(
        "set hot_tail_token_budget_ratio 0.15", active_engine="compressor"
    )
    saved = _read_config(tmp_path)

    assert first.changed is True
    assert second.changed is True
    assert saved["context"]["engine"] == "compressor"
    assert saved["context"]["object_context"]["hot_tail_max_deltas"] == 4
    assert saved["context"]["object_context"]["hot_tail_token_budget_ratio"] == 0.15
    assert any("does not toggle V1" in line for line in second.lines)


def test_invalid_set_never_mutates_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = "context:\n  engine: object_context\n"
    _write_config(tmp_path, original)

    with pytest.raises(ObjectContextCommandError):
        run_object_context_command("set hot_tail_max_deltas 0")
    with pytest.raises(ObjectContextCommandError):
        run_object_context_command("set typo_parameter 1")

    assert (tmp_path / "config.yaml").read_text(encoding="utf-8") == original


def test_reset_one_then_all_removes_supported_and_v0_keys_but_preserves_future(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        "context:\n"
        "  engine: object_context\n"
        "  object_context:\n"
        "    hot_tail_max_deltas: 2\n"
        "    summary_max_tokens: 32\n"
        "    min_code_chars: 160\n"
        "    max_read_lines: 300\n"
        "    future_setting: keep-me\n",
    )

    one = run_object_context_command("reset hot_tail_max_deltas")
    assert one.changed is True
    saved = _read_config(tmp_path)
    assert "hot_tail_max_deltas" not in saved["context"]["object_context"]
    assert saved["context"]["object_context"]["summary_max_tokens"] == 32

    all_result = run_object_context_command("reset all")
    assert all_result.changed is True
    saved = _read_config(tmp_path)
    assert saved["context"]["object_context"] == {"future_setting": "keep-me"}


def test_reset_specific_legacy_v0_key_is_supported(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(
        tmp_path,
        "context:\n"
        "  engine: object_context\n"
        "  object_context:\n"
        "    min_code_chars: 160\n",
    )

    result = run_object_context_command("reset min_code_chars")

    assert result.changed is True
    assert "Removed ignored V0 setting" in result.lines[0]
    assert "object_context" not in _read_config(tmp_path)["context"]


def test_active_engine_name_uses_engine_contract_and_builtin_fallback():
    assert active_context_engine_name(None) == ""
    assert active_context_engine_name(
        SimpleNamespace(context_compressor=SimpleNamespace(name="object_context"))
    ) == "object_context"

    ContextCompressor = type("ContextCompressor", (), {})
    assert active_context_engine_name(
        SimpleNamespace(context_compressor=ContextCompressor())
    ) == "compressor"


def test_active_engine_status_uses_public_contract_and_fails_closed():
    expected = _live_v1_status()
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(get_status=lambda: expected)
    )
    broken = SimpleNamespace(
        context_compressor=SimpleNamespace(
            get_status=lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        )
    )

    assert active_context_engine_status(agent) == expected
    assert active_context_engine_status(None) is None
    assert active_context_engine_status(broken) is None


def test_active_engine_monitor_uses_content_free_public_contract_and_fails_closed():
    expected = _live_timeline()
    agent = SimpleNamespace(
        context_compressor=SimpleNamespace(get_projection_timeline=lambda: expected)
    )
    broken = SimpleNamespace(
        context_compressor=SimpleNamespace(
            get_projection_timeline=lambda: (_ for _ in ()).throw(
                RuntimeError("boom")
            )
        )
    )

    assert active_context_engine_monitor(agent) == expected
    assert active_context_engine_monitor(None) is None
    assert active_context_engine_monitor(broken) is None


def test_persisted_telemetry_resolves_resumed_session_conversation_root(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    session_db = SimpleNamespace(
        get_conversation_root=lambda session_id: (
            "conversation-a" if session_id == "resumed-session" else session_id
        ),
        get_session_title=lambda session_id: {
            "session-a": "Latest continuation title",
            "conversation-a": "Root title",
        }.get(session_id),
    )

    persisted = persisted_context_engine_telemetry(
        session_db, "resumed-session"
    )

    assert persisted is not None
    status, timeline = persisted
    assert status["object_context_available"] is True
    assert status["request_projection_count"] == 1
    assert timeline["source"] == "persisted"
    assert timeline["conversation_id"] == "conversation-a"
    assert timeline["session_id"] == "resumed-session"
    assert timeline["title"] == "Latest continuation title"
    assert timeline["projections"][0]["turn_id"] == "turn-a"
    assert timeline["cache_requests"][0]["metrics"]["cache_read_tokens"] == 60


def test_persisted_telemetry_does_not_borrow_another_conversations_metrics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)

    assert persisted_context_engine_telemetry(
        SimpleNamespace(get_conversation_root=lambda _session_id: "conversation-b"),
        "resumed-session",
    ) is None


def test_persisted_monitor_telemetry_contains_every_projection_session(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    store = ObjectContextStore(
        tmp_path / "context" / "object_context_v1.sqlite3"
    )
    store.record_metrics(
        "conversation-b",
        {
            "raw_context_tokens": 200,
            "rendered_context_tokens": 80,
            "tokens_saved": 120,
        },
        metadata={
            "event": "request_projection",
            "projection_id": "persisted-projection-b",
            "projection_sequence": 1,
            "turn_id": "turn-b",
            "session_id": "session-b",
        },
    )

    persisted = persisted_context_engine_telemetry(
        SimpleNamespace(
            get_conversation_root=lambda _session_id: "conversation-a",
            get_session_title=lambda session_id: {
                "session-a": "Alpha title",
                "session-b": "Beta title",
            }.get(session_id),
        ),
        "resumed-session",
        include_all_sessions=True,
    )

    assert persisted is not None
    _status, timeline = persisted
    assert timeline["schema_version"] == 2
    assert timeline["active_conversation_id"] == "conversation-a"
    assert timeline["session_id"] == "resumed-session"
    assert {item["conversation_id"] for item in timeline["sessions"]} == {
        "conversation-a",
        "conversation-b",
    }
    assert sum(len(item["projections"]) for item in timeline["sessions"]) == 2
    assert sum(len(item["cache_requests"]) for item in timeline["sessions"]) == 1
    assert {
        item["conversation_id"]: item["title"] for item in timeline["sessions"]
    } == {
        "conversation-a": "Alpha title",
        "conversation-b": "Beta title",
    }


def test_all_session_monitor_remains_available_when_current_root_has_no_metrics(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)

    persisted = persisted_context_engine_telemetry(
        SimpleNamespace(
            get_conversation_root=lambda _session_id: "conversation-without-metrics"
        ),
        "new-session",
        include_all_sessions=True,
    )

    assert persisted is not None
    status, timeline = persisted
    assert status["request_projection_count"] == 0
    assert timeline["projections"] == []
    assert timeline["cache_requests"] == []
    assert [item["conversation_id"] for item in timeline["sessions"]] == [
        "conversation-a"
    ]


def test_process_command_dispatches_original_arguments_to_handler():
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj._pending_resume_sessions = None

    with patch.object(cli_obj, "_handle_object_context_command") as handler:
        assert cli_obj.process_command(
            "/object_context set hot_tail_max_deltas 4"
        ) is True

    handler.assert_called_once_with("/object_context set hot_tail_max_deltas 4")


def test_cli_handler_prints_safe_validation_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path, "context:\n  engine: compressor\n")
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None

    cli_obj._handle_object_context_command(
        "/object_context set hot_tail_max_deltas nope"
    )

    output = capsys.readouterr().out
    assert "requires a finite integer" in output
    assert _read_config(tmp_path)["context"]["engine"] == "compressor"


def test_cli_handler_prints_live_stats_from_active_engine(capsys):
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            name="object_context",
            get_status=_live_v1_status,
        )
    )

    cli_obj._handle_object_context_command("/object_context stats")

    output = capsys.readouterr().out
    assert "Object Context V1 Token Savings" in output
    assert "~18,000" in output


def test_cli_handler_opens_monitor_webpage_with_local_file_uri(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            name="object_context",
            get_status=_live_v1_status,
            get_projection_timeline=_live_timeline,
        )
    )

    with patch("webbrowser.open", return_value=True) as browser_open:
        cli_obj._handle_object_context_command("/object_context monitor")

    output = capsys.readouterr().out
    assert "Session Dynamics Monitor" in output
    assert "Browser launch was unavailable" not in output
    uri = browser_open.call_args.args[0]
    assert uri.startswith("file://")
    assert uri.endswith(".html")
    browser_open.assert_called_once_with(uri, new=2)


def test_cli_handler_prints_dashboard_path_fallback_when_browser_is_unavailable(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = SimpleNamespace(
        context_compressor=SimpleNamespace(
            name="object_context",
            get_status=_live_v1_status,
            get_projection_timeline=_live_timeline,
        )
    )

    with patch("webbrowser.open", return_value=False):
        cli_obj._handle_object_context_command("/object_context monitor")

    output = capsys.readouterr().out
    assert "Dashboard:" in output
    assert "Browser launch was unavailable" in output


def test_cli_handler_monitors_resumed_session_before_lazy_agent_startup(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None
    cli_obj.session_id = "resumed-session"
    cli_obj._session_db = SimpleNamespace(
        get_conversation_root=lambda _session_id: "conversation-a",
        get_session_title=lambda session_id: {
            "session-a": "Alpha title",
            "conversation-b": "Beta title",
        }.get(session_id),
    )

    with patch("webbrowser.open", return_value=True) as browser_open:
        cli_obj._handle_object_context_command("/object_context monitor")

    output = capsys.readouterr().out
    assert "active session is unknown" not in output
    assert "Projects: 1" in output
    assert "Dashboard:" in output
    browser_open.assert_called_once()


def test_cli_monitor_lists_all_sessions_and_writes_stable_dashboard(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    store = ObjectContextStore(
        tmp_path / "context" / "object_context_v1.sqlite3"
    )
    store.record_metrics(
        "conversation-b",
        {
            "raw_context_tokens": 50,
            "rendered_context_tokens": 20,
            "tokens_saved": 30,
        },
        metadata={
            "event": "request_projection",
            "projection_id": "projection-b",
            "turn_id": "turn-b",
        },
    )
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None
    cli_obj.session_id = "resumed-session"
    cli_obj._session_db = SimpleNamespace(
        get_conversation_root=lambda _session_id: "conversation-a",
        get_session_title=lambda session_id: {
            "session-a": "Alpha title",
            "conversation-b": "Beta title",
        }.get(session_id),
    )

    with patch("webbrowser.open", return_value=True):
        cli_obj._handle_object_context_command("/oc monitor")

    output = capsys.readouterr().out
    assert "Sessions: 2" in output
    assert "Projects: 2 total · 1 selected" in output
    dashboard = (
        tmp_path
        / "logs"
        / "object-context-monitor"
        / "object_context_monitor_all_sessions.html"
    )
    assert dashboard.is_file()
    dashboard_html = dashboard.read_text(encoding="utf-8")
    assert "conversation-b" in dashboard_html
    assert "Alpha title" in dashboard_html
    assert "Beta title" in dashboard_html
    assert "全部 CSV" in dashboard_html


def test_cli_handler_reads_resumed_session_stats_before_lazy_agent_startup(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None
    cli_obj.session_id = "resumed-session"
    cli_obj._session_db = SimpleNamespace(
        get_conversation_root=lambda _session_id: "conversation-a"
    )

    cli_obj._handle_object_context_command("/object_context stats")

    output = capsys.readouterr().out
    assert "Object Context V1 Token Savings" in output
    assert "Projected requests" in output
    assert "1" in output


def test_slash_worker_shape_monitors_resumed_session_without_live_agent(
    tmp_path, monkeypatch
):
    from tui_gateway.slash_worker import _run

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_persisted_timeline(tmp_path)
    cli_obj = HermesCLI.__new__(HermesCLI)
    cli_obj.agent = None
    cli_obj.session_id = "resumed-session"
    cli_obj._session_db = SimpleNamespace(
        get_conversation_root=lambda _session_id: "conversation-a"
    )
    cli_obj._pending_resume_sessions = None

    with patch("webbrowser.open", return_value=False):
        output = _run(cli_obj, "object_context monitor")

    assert "active session is unknown" not in output
    assert "Projects: 1" in output
    assert "Dashboard:" in output
