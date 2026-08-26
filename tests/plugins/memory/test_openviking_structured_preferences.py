"""Focused tests for the structured user-preference memory experiment."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

import plugins.memory.openviking as openviking_module
from plugins.memory.openviking import OpenVikingMemoryProvider


@pytest.fixture(autouse=True)
def _clear_recall_environment(monkeypatch):
    """Keep legacy env overrides from changing config-only experiment tests."""
    for name in (
        "OPENVIKING_RECALL_LIMIT",
        "OPENVIKING_RECALL_SCORE_THRESHOLD",
        "OPENVIKING_RECALL_MAX_INJECTED_CHARS",
        "OPENVIKING_RECALL_TIMEOUT_SECONDS",
        "OPENVIKING_RECALL_REQUEST_TIMEOUT_SECONDS",
        "OPENVIKING_RECALL_FULL_READ_LIMIT",
        "OPENVIKING_RECALL_PREFER_ABSTRACT",
        "OPENVIKING_RECALL_RESOURCES",
    ):
        monkeypatch.delenv(name, raising=False)


def _provider_with_client(client=None) -> OpenVikingMemoryProvider:
    provider = OpenVikingMemoryProvider()
    provider._client = client or MagicMock()
    provider._agent = "hermes"
    return provider


def _experiment_config(**overrides):
    config = {
        "recall_limit": 2,
        "recall_score_threshold": 0.15,
        "recall_max_injected_chars": 4000,
        "recall_timeout_seconds": 4.0,
        "recall_request_timeout_seconds": 3.0,
        "recall_full_read_limit": 0,
        "recall_prefer_abstract": True,
        "recall_resources": False,
        "structured_preference_recall": True,
        "preference_recall_limit": 1,
    }
    config.update(overrides)
    return config


def test_preference_scope_is_open_vocabulary_and_unicode_safe():
    scope, error = openviking_module._normalize_preference_scope(
        "产物/LaTeX 方程/自定义新任务"
    )

    assert error is None
    assert scope == "产物/latex-方程/自定义新任务"


@pytest.mark.parametrize(
    "scope",
    (
        "../secrets",
        "/absolute/path",
        "viking://user/memories/preferences",
        "a//b",
        "a/%2e%2e/b",
    ),
)
def test_preference_scope_rejects_unsafe_paths(scope):
    normalized, error = openviking_module._normalize_preference_scope(scope)

    assert normalized == ""
    assert error


def test_viking_remember_writes_atomic_preference_to_open_scope():
    client = MagicMock()
    client.post.return_value = {"result": {"written_bytes": 231}}
    provider = _provider_with_client(client)

    result = json.loads(
        provider._tool_remember(
            {
                "category": "preference",
                "scope": "artifacts/LaTeX/equations",
                "applies_when": "producing LaTeX with display equations",
                "content": (
                    "Use the equation environment and do not wrap prose "
                    "at arbitrary source columns."
                ),
            }
        )
    )

    assert result["status"] == "stored"
    assert result["category"] == "preference"
    assert result["scope"] == "artifacts/latex/equations"
    assert result["uri"].startswith(
        "viking://user/peers/hermes/memories/preferences/"
        "artifacts/latex/equations/mem_"
    )
    assert result["uri"].endswith(".md")

    assert client.post.call_count == 2
    mkdir_path, mkdir_payload = client.post.call_args_list[0].args
    assert mkdir_path == "/api/v1/fs/mkdir"
    assert mkdir_payload == {
        "uri": (
            "viking://user/peers/hermes/memories/preferences/"
            "artifacts/latex/equations"
        )
    }

    path, payload = client.post.call_args_list[1].args
    assert path == "/api/v1/content/write"
    assert payload["uri"] == result["uri"]
    assert payload["mode"] == "create"
    assert "create_parents" not in payload
    assert "Scope: `artifacts/latex/equations`" in payload["content"]
    assert "Applies when: producing LaTeX with display equations" in payload["content"]
    assert "## Rule" in payload["content"]
    assert "equation environment" in payload["content"]


def test_viking_remember_requires_both_structured_preference_fields():
    provider = _provider_with_client()

    result = json.loads(
        provider._tool_remember(
            {
                "category": "preference",
                "scope": "artifacts/latex",
                "content": "Do not wrap prose arbitrarily.",
            }
        )
    )

    assert "applies_when is required" in result["error"]
    provider._client.post.assert_not_called()


def test_viking_remember_preserves_legacy_unstructured_payload():
    client = MagicMock()
    client.post.return_value = {"result": {"written_bytes": 12}}
    provider = _provider_with_client(client)

    result = json.loads(
        provider._tool_remember(
            {
                "category": "preference",
                "content": "User prefers concise answers.",
            }
        )
    )

    assert result["status"] == "stored"
    path, payload = client.post.call_args.args
    assert path == "/api/v1/content/write"
    assert payload == {
        "uri": result["uri"],
        "content": "User prefers concise answers.",
        "mode": "create",
    }
    assert "/memories/preferences/mem_" in result["uri"]
    assert "scope" not in result


def test_viking_remember_rejects_scope_on_non_preference_memory():
    provider = _provider_with_client()

    result = json.loads(
        provider._tool_remember(
            {
                "category": "entity",
                "scope": "projects/ansatz",
                "applies_when": "discussing the project",
                "content": "Ansatz is the current project.",
            }
        )
    )

    assert "only valid for category='preference'" in result["error"]
    provider._client.post.assert_not_called()


def test_experimental_recall_reserves_slot_for_relevant_preference(monkeypatch):
    calls = []

    class StubClient:
        def post(self, path, payload=None, **kwargs):
            calls.append((path, dict(payload or {}), kwargs))
            if str(payload.get("query", "")).startswith(
                openviking_module._PREFERENCE_QUERY_PREFIX
            ):
                return {
                    "result": {
                        "memories": [
                            {
                                "uri": (
                                    "viking://user/memories/preferences/"
                                    "artifacts/latex/equations.md"
                                ),
                                "category": "preferences",
                                "score": 0.35,
                                "level": 2,
                                "abstract": "Use the equation environment in LaTeX.",
                            },
                            {
                                "uri": "viking://user/memories/events/meeting.md",
                                "category": "events",
                                "score": 0.95,
                                "abstract": "A meeting happened.",
                            },
                        ]
                    }
                }
            return {
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/memories/events/high.md",
                            "category": "events",
                            "score": 0.90,
                            "abstract": "High-ranked project event.",
                        },
                        {
                            "uri": "viking://user/memories/entities/second.md",
                            "category": "entities",
                            "score": 0.80,
                            "abstract": "Second-ranked project entity.",
                        },
                    ]
                }
            }

    monkeypatch.setattr(
        openviking_module,
        "_load_hermes_openviking_config",
        lambda: _experiment_config(),
    )
    provider = _provider_with_client(StubClient())

    context = provider._search_prefetch_context(
        "Write a LaTeX derivation for this result",
        client=provider._client,
    )

    assert len(calls) == 2
    assert calls[0][1]["query"] == "Write a LaTeX derivation for this result"
    assert calls[1][1]["query"].startswith(
        openviking_module._PREFERENCE_QUERY_PREFIX
    )
    assert "Use the equation environment in LaTeX." in context
    assert "High-ranked project event." in context
    assert "Second-ranked project entity." not in context


def test_preference_quota_never_bypasses_relevance_threshold():
    selected = OpenVikingMemoryProvider._select_recall_candidates(
        [
            {
                "uri": "viking://user/memories/preferences/unrelated.md",
                "category": "preferences",
                "score": 0.10,
                "abstract": "Unrelated preference.",
            },
            {
                "uri": "viking://user/memories/events/relevant.md",
                "category": "events",
                "score": 0.70,
                "abstract": "Relevant task event.",
            },
        ],
        "current task",
        limit=2,
        score_threshold=0.15,
        preference_limit=1,
    )

    assert [item["category"] for item in selected] == ["events"]


def test_failed_experimental_query_keeps_baseline_recall(monkeypatch):
    class StubClient:
        def post(self, path, payload=None, **kwargs):
            if str(payload.get("query", "")).startswith(
                openviking_module._PREFERENCE_QUERY_PREFIX
            ):
                raise RuntimeError("preference query unavailable")
            return {
                "result": {
                    "memories": [
                        {
                            "uri": "viking://user/memories/events/baseline.md",
                            "category": "events",
                            "score": 0.75,
                            "abstract": "Baseline memory remains available.",
                        }
                    ]
                }
            }

    monkeypatch.setattr(
        openviking_module,
        "_load_hermes_openviking_config",
        lambda: _experiment_config(),
    )
    provider = _provider_with_client(StubClient())

    context = provider._search_prefetch_context(
        "Continue the current project task",
        client=provider._client,
    )

    assert "Baseline memory remains available." in context


def test_structured_preference_recall_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(
        openviking_module,
        "_load_hermes_openviking_config",
        lambda: {},
    )

    config = OpenVikingMemoryProvider()._recall_config()

    assert config["structured_preference_recall"] is False
    assert config["preference_limit"] == 3


def test_experiment_settings_are_config_only():
    schema = {
        item["key"]: item for item in OpenVikingMemoryProvider().get_config_schema()
    }

    assert schema["structured_preference_recall"]["default"] is False
    assert "env_var" not in schema["structured_preference_recall"]
    assert schema["preference_recall_limit"]["default"] == 3
    assert "env_var" not in schema["preference_recall_limit"]


@pytest.mark.parametrize(
    ("enabled", "guidance_expected"),
    ((False, False), (True, True)),
)
def test_structured_capture_guidance_is_gated(
    monkeypatch,
    enabled,
    guidance_expected,
):
    client = MagicMock()
    client.get.return_value = {"result": [{"uri": "viking://user/memories"}]}
    monkeypatch.setattr(
        openviking_module,
        "_load_hermes_openviking_config",
        lambda: _experiment_config(structured_preference_recall=enabled),
    )
    provider = _provider_with_client(client)

    prompt = provider.system_prompt_block()

    guidance = "one atomic rule, an open-ended scope"
    assert (guidance in prompt) is guidance_expected
    assert "Use viking_remember to store important facts" in prompt
