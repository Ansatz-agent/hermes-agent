"""Host contract for real-user and inference Context Deltas."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Dict, List

from agent.context_engine import ContextDelta, ContextEngine
from agent.conversation_loop import _notify_context_engine_delta_committed


class _BaseEngine(ContextEngine):
    @property
    def name(self) -> str:
        return "base"

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        return None

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return False

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: int = None,
        focus_topic: str = None,
    ) -> List[Dict[str, Any]]:
        return messages


class _CapturingEngine(_BaseEngine):
    def __init__(self) -> None:
        self.deltas: list[ContextDelta] = []

    def on_delta_committed(self, delta: ContextDelta) -> None:
        self.deltas.append(delta)


class _DeferredCapturingEngine(_CapturingEngine):
    defers_response_success_until_inference_commit = True

    def __init__(self) -> None:
        super().__init__()
        self.accepted_notifications = 0

    def confirm_response_accepted(self) -> None:
        self.accepted_notifications += 1


def _agent(engine: ContextEngine) -> SimpleNamespace:
    return SimpleNamespace(
        context_compressor=engine,
        session_id="segment-2",
        _gateway_session_key="conversation-key",
        _conversation_root_id=lambda: "conversation-root",
    )


def test_base_noop_is_skipped_without_allocating_delta_state():
    agent = _agent(_BaseEngine())

    _notify_context_engine_delta_committed(
        agent,
        kind="user",
        turn_id="turn-1",
        sequence=0,
        delta_messages=[{"role": "user", "content": "hello"}],
        logger=logging.getLogger(__name__),
    )

    assert not hasattr(agent, "_context_engine_committed_delta_ids")


def test_delta_is_deep_copied_identified_and_deduplicated():
    engine = _CapturingEngine()
    agent = _agent(engine)
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "working"}],
        "tool_calls": [
            {
                "id": "call-1",
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
    }
    tool_result = {
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "large result",
    }
    usage = {"input_tokens": 120, "output_tokens": 10}

    for _ in range(2):
        _notify_context_engine_delta_committed(
            agent,
            kind="inference",
            turn_id="turn-1",
            sequence=3,
            inference_id="inference-3",
            delta_messages=[message, tool_result],
            logger=logging.getLogger(__name__),
            source_start_index=7,
            usage=usage,
        )

    assert len(engine.deltas) == 1
    delta = engine.deltas[0]
    assert delta.delta_id == "turn-1:inference:3"
    assert delta.kind == "inference"
    assert delta.conversation_id == "conversation-root"
    assert delta.session_id == "segment-2"
    assert delta.turn_id == "turn-1"
    assert delta.sequence == 3
    assert delta.inference_id == "inference-3"
    assert delta.source_start_index == 7
    assert delta.usage == usage
    assert [item["role"] for item in delta.messages] == ["assistant", "tool"]

    message["content"][0]["text"] = "mutated"
    message["tool_calls"][0]["function"]["name"] = "changed"
    tool_result["content"] = "changed"
    usage["input_tokens"] = 999

    assert delta.messages[0]["content"][0]["text"] == "working"
    assert delta.messages[0]["tool_calls"][0]["function"]["name"] == "terminal"
    assert delta.messages[1]["content"] == "large result"
    assert delta.usage == {"input_tokens": 120, "output_tokens": 10}


def test_user_and_inference_sequences_have_distinct_ids():
    engine = _CapturingEngine()
    agent = _agent(engine)

    _notify_context_engine_delta_committed(
        agent,
        kind="user",
        turn_id="turn-2",
        sequence=0,
        delta_messages=[{"role": "user", "content": "code"}],
        logger=logging.getLogger(__name__),
    )
    _notify_context_engine_delta_committed(
        agent,
        kind="inference",
        turn_id="turn-2",
        sequence=1,
        delta_messages=[{"role": "assistant", "content": "answer"}],
        logger=logging.getLogger(__name__),
    )

    assert [delta.delta_id for delta in engine.deltas] == [
        "turn-2:user:0",
        "turn-2:inference:1",
    ]
    assert [delta.kind for delta in engine.deltas] == ["user", "inference"]


def test_deferred_success_is_confirmed_once_at_inference_commit_only():
    engine = _DeferredCapturingEngine()
    agent = _agent(engine)

    _notify_context_engine_delta_committed(
        agent,
        kind="user",
        turn_id="turn-deferred",
        sequence=0,
        delta_messages=[{"role": "user", "content": "question"}],
        logger=logging.getLogger(__name__),
    )
    for _ in range(2):
        _notify_context_engine_delta_committed(
            agent,
            kind="inference",
            turn_id="turn-deferred",
            sequence=1,
            delta_messages=[{"role": "assistant", "content": "accepted"}],
            logger=logging.getLogger(__name__),
        )

    assert engine.accepted_notifications == 1
    assert [delta.delta_id for delta in engine.deltas] == [
        "turn-deferred:user:0",
        "turn-deferred:inference:1",
    ]


def test_preconfirmed_inference_delta_does_not_repeat_success_callback():
    engine = _DeferredCapturingEngine()
    agent = _agent(engine)

    _notify_context_engine_delta_committed(
        agent,
        kind="inference",
        turn_id="turn-preconfirmed",
        sequence=1,
        delta_messages=[{"role": "assistant", "content": "accepted"}],
        logger=logging.getLogger(__name__),
        response_already_confirmed=True,
    )

    assert engine.accepted_notifications == 0
    assert [delta.delta_id for delta in engine.deltas] == [
        "turn-preconfirmed:inference:1"
    ]


def test_declared_synthetic_run_cannot_emit_user_or_inference_deltas():
    engine = _CapturingEngine()
    agent = _agent(engine)
    agent._context_engine_real_turn_id = ""

    for kind, sequence, role in (("user", 0, "user"), ("inference", 1, "assistant")):
        _notify_context_engine_delta_committed(
            agent,
            kind=kind,
            turn_id="synthetic-turn",
            sequence=sequence,
            delta_messages=[{"role": role, "content": "runtime scaffolding"}],
            logger=logging.getLogger(__name__),
        )

    assert engine.deltas == []
    assert not hasattr(agent, "_context_engine_committed_delta_ids")

    agent._context_engine_real_turn_id = "real-turn"
    _notify_context_engine_delta_committed(
        agent,
        kind="user",
        turn_id="real-turn",
        sequence=0,
        delta_messages=[{"role": "user", "content": "human input"}],
        logger=logging.getLogger(__name__),
    )
    assert [delta.delta_id for delta in engine.deltas] == ["real-turn:user:0"]


def test_hook_failure_is_fail_open_and_not_marked_committed(caplog):
    class _FailingEngine(_BaseEngine):
        def on_delta_committed(self, delta: ContextDelta) -> None:
            raise RuntimeError("backend unavailable")

    agent = _agent(_FailingEngine())

    with caplog.at_level(logging.WARNING):
        _notify_context_engine_delta_committed(
            agent,
            kind="user",
            turn_id="turn-3",
            sequence=0,
            delta_messages=[{"role": "user", "content": "hello"}],
            logger=logging.getLogger(__name__),
        )

    assert agent._context_engine_committed_delta_ids == set()
    assert "on_delta_committed hook failed" in caplog.text
