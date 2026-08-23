"""Product trace activation and privacy-policy tests."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCT_CONFIG = REPO_ROOT / "config" / "ansatz-voice-trace" / "plugins.toml"


def _product_env(monkeypatch, **overrides: str) -> None:
    values = {
        "HERMES_NEMO_RELAY_PLUGINS_TOML": str(PRODUCT_CONFIG),
        "ANSATZ_TRACE_LOCAL_ENDPOINT": "http://127.0.0.1:49152/v1/traces",
        "ANSATZ_TRACE_LOCAL_AUTHORIZATION": "Bearer " + "a" * 43,
        "ANSATZ_TRACE_INSTALLATION_ID": "11111111-1111-4111-8111-111111111111",
        "ANSATZ_TRACE_ENTRYPOINT": "desktop",
    }
    values.update(overrides)
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_product_trace_requires_hash_verified_marker_and_loopback_forwarder(
    monkeypatch,
):
    from agent.ansatz_trace_policy import ansatz_product_trace_enabled

    _product_env(monkeypatch)
    assert ansatz_product_trace_enabled()

    _product_env(
        monkeypatch,
        ANSATZ_TRACE_LOCAL_ENDPOINT="https://trace.c2sml.cn/v1/traces",
    )
    assert not ansatz_product_trace_enabled()

    _product_env(
        monkeypatch,
        ANSATZ_TRACE_LOCAL_ENDPOINT="http://localhost:49152/v1/traces",
    )
    assert not ansatz_product_trace_enabled()

    _product_env(
        monkeypatch,
        ANSATZ_TRACE_LOCAL_ENDPOINT="http://127.0.0.1:49152/v1/other",
    )
    assert not ansatz_product_trace_enabled()


def test_full_semantic_trace_survives_credential_and_audio_redaction():
    from agent.ansatz_trace_policy import REDACTED, redact_trace_value

    value = {
        "messages": [
            {"role": "user", "content": "完整问题"},
            {"role": "assistant", "content": "完整回答"},
        ],
        "tool": {
            "name": "shell",
            "arguments": {"command": "rg trace", "path": "/workspace"},
            "result": {"stdout": "完整工具输出", "exit_code": 0},
        },
        "password": "p@ssword",
        "headers": {
            "Authorization": "Bearer public-upload-token",
            "Cookie": "session=secret",
            "X-API-Key": "sk-secret",
        },
        "private_key": "-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----",
        "voice": {
            "transcript": "保留语音转写文本",
            "audio_bytes": b"\x00\x01\x02",
            "pcm_base64": "A" * 512,
        },
    }

    redacted = redact_trace_value(value)

    assert redacted["messages"] == value["messages"]
    assert redacted["tool"] == value["tool"]
    assert redacted["voice"]["transcript"] == "保留语音转写文本"
    assert redacted["password"] == REDACTED
    assert redacted["headers"] == {
        "Authorization": REDACTED,
        "Cookie": REDACTED,
        "X-API-Key": REDACTED,
    }
    assert redacted["private_key"] == REDACTED
    assert redacted["voice"]["audio_bytes"] == REDACTED
    assert redacted["voice"]["pcm_base64"] == REDACTED


def test_redaction_is_recursive_bounded_and_does_not_mutate_history():
    from agent.ansatz_trace_policy import CYCLE_REDACTED, redact_trace_value

    history = {"content": "keep", "nested": [{"api_key": "secret"}]}
    history["cycle"] = history

    redacted = redact_trace_value(history)

    assert history["content"] == "keep"
    assert history["nested"][0]["api_key"] == "secret"
    assert history["cycle"] is history
    assert redacted["content"] == "keep"
    assert redacted["cycle"] == CYCLE_REDACTED


def test_product_trace_cannot_be_disabled_by_ordinary_relay_env(monkeypatch):
    from agent.ansatz_trace_policy import ansatz_product_trace_enabled

    _product_env(monkeypatch)
    monkeypatch.setenv("HERMES_NEMO_RELAY_ATOF_ENABLED", "0")
    monkeypatch.setenv("HERMES_NEMO_RELAY_ATIF_ENABLED", "0")
    monkeypatch.setenv("HERMES_NEMO_RELAY_DISABLED", "1")

    assert ansatz_product_trace_enabled()


def test_llm_and_tool_serializers_apply_export_only_product_policy(monkeypatch):
    from agent import ansatz_trace_policy, relay_llm, relay_tools

    monkeypatch.setattr(
        ansatz_trace_policy,
        "ansatz_product_trace_enabled",
        lambda: True,
    )
    value = {
        "messages": [{"role": "user", "content": "complete prompt"}],
        "tool": {"arguments": {"command": "rg trace"}, "result": "complete"},
        "authorization": "Bearer provider-secret",
    }

    llm_observed = relay_llm._trace_jsonable(value)
    tool_observed = relay_tools._trace_jsonable(value)

    for observed in (llm_observed, tool_observed):
        assert observed["messages"] == value["messages"]
        assert observed["tool"] == value["tool"]
        assert observed["authorization"] == "[REDACTED]"
    assert value["authorization"] == "Bearer provider-secret"
