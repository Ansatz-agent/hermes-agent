"""Auxiliary LLM dispatches pass through the prompt-monitor seam."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent import auxiliary_client


class _SyncCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return "sync-response"


class _AsyncCompletions:
    def __init__(self):
        self.requests = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return "async-response"


def _client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_sync_completion_captures_same_request_before_dispatch(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "agent.prompt_monitor.capture_llm_request",
        lambda request, **metadata: captured.append((dict(request), metadata)),
    )
    completions = _SyncCompletions()
    request = {
        "model": "summary-model",
        "messages": [{"role": "user", "content": "summarize this Card"}],
        "timeout": 42,
    }

    response = auxiliary_client._relay_sync_completion(
        _client(completions),
        request,
        provider="custom",
        api_mode="chat_completions",
    )

    assert response == "sync-response"
    assert completions.requests == [request]
    assert captured[0][0] == request
    assert captured[0][1]["source"] == "auxiliary"
    assert captured[0][1]["provider"] == "custom"


@pytest.mark.asyncio
async def test_async_completion_captures_same_request_before_dispatch(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "agent.prompt_monitor.capture_llm_request",
        lambda request, **metadata: captured.append((dict(request), metadata)),
    )
    completions = _AsyncCompletions()
    request = {
        "model": "title-model",
        "messages": [{"role": "user", "content": "name this session"}],
    }

    response = await auxiliary_client._relay_async_completion(
        _client(completions),
        request,
        provider="custom",
        api_mode="chat_completions",
    )

    assert response == "async-response"
    assert completions.requests == [request]
    assert captured[0][0] == request
    assert captured[0][1]["source"] == "auxiliary"


def test_stream_completion_captures_same_request_before_dispatch(monkeypatch):
    captured = []
    monkeypatch.setattr(
        "agent.prompt_monitor.capture_llm_request",
        lambda request, **metadata: captured.append((dict(request), metadata)),
    )
    completions = _SyncCompletions()
    request = {
        "model": "aggregator-model",
        "messages": [{"role": "user", "content": "aggregate"}],
        "stream": True,
    }

    response = auxiliary_client._relay_sync_stream(
        _client(completions),
        request,
        provider="custom",
        api_mode="chat_completions",
    )

    assert response == "sync-response"
    assert completions.requests == [request]
    assert captured[0][0] == request
    assert captured[0][1]["source"] == "auxiliary"
