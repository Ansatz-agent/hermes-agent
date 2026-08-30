import json
import re
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB
from plugins.context_engine.object_context.engine import ObjectContextEngine


class _DeterministicSummary:
    def generate(self, *, engine, record, contains, previous=None):
        del engine, contains, previous
        return f"Exact {record.object_type.value} payload for this conversation.", False


def _config():
    return {
        "agent": {"api_max_retries": 1},
        "compression": {"threshold": 0.99, "protect_last_n": 2},
        "auxiliary": {"title_generation": {"enabled": False}},
        "context": {
            "engine": "object_context",
            "object_context": {
                "hot_tail_max_deltas": 1,
                "hot_tail_token_budget_ratio": 0.01,
                "context_soft_limit_ratio": 0.75,
                "object_prefilter_min_tokens": 1,
                "min_absolute_saving_tokens": 1,
                "min_relative_saving_ratio": 0.0,
                "summary_max_tokens": 32,
                "wm_grace_deltas": 20,
                "recent_retrieval_active_deltas": 20,
                "retrieval_max_tokens_ratio": 0.9,
            },
        },
    }


def _large_json():
    return json.dumps(
        {f"field_{index}": "value-" + ("x" * 32) for index in range(400)},
        ensure_ascii=False,
    )


def _tool_definition(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _tool_call(name, arguments, call_id):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _response(content="", *, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test-model",
        usage=None,
    )


def _agent(tmp_path, *, tools=()):
    cfg = _config()
    db = SessionDB(tmp_path / "state.db")
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.agent_init.get_hermes_home", return_value=tmp_path),
        patch("agent.model_metadata.get_model_context_length", return_value=100_000),
        patch(
            "run_agent.get_tool_definitions",
            return_value=[_tool_definition(name) for name in tools],
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="test-model",
            api_key="test-key-not-a-real-secret",
            base_url="https://example.invalid/v1",
            session_id="host-e2e",
            session_db=db,
            platform="subagent",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    assert isinstance(agent.context_compressor, ObjectContextEngine)
    agent.context_compressor._summary_generator = _DeterministicSummary()
    return agent, db


def test_real_host_user_card_exact_retrieval_same_turn_and_next_turn_unload(tmp_path):
    agent, db = _agent(tmp_path)
    raw = _large_json()
    observed_requests = []
    state = {"call": 0, "object_ref": ""}

    def provider(**kwargs):
        state["call"] += 1
        messages = kwargs["messages"]
        observed_requests.append(messages)
        if state["call"] == 1:
            assert raw in messages[-1]["content"]
            return _response("Stored the structured payload.")
        if state["call"] == 2:
            rendered = json.dumps(messages, ensure_ascii=False)
            assert raw not in rendered
            assert "<OBJECT_CARD>" in rendered
            [state["object_ref"]] = list(
                dict.fromkeys(re.findall(r"object://obj_[a-f0-9]{24}@v1", rendered))
            )
            return _response(
                tool_calls=[
                    _tool_call(
                        "retrieve_object",
                        {
                            "object_ref": state["object_ref"],
                            "reason": "Inspect exact field values.",
                        },
                        "retrieve-call-1",
                    )
                ],
                finish_reason="tool_calls",
            )
        if state["call"] == 3:
            retrieval = next(
                message
                for message in messages
                if message.get("role") == "tool"
                and message.get("tool_call_id") == "retrieve-call-1"
            )
            assert (
                json.loads(retrieval["content"])["retrieved_object"]["content"] == raw
            )
            assert state["object_ref"] in json.dumps(messages[1], ensure_ascii=False)
            return _response("Used the exact retrieved object.")
        if state["call"] == 4:
            rendered = json.dumps(messages, ensure_ascii=False)
            assert raw not in rendered
            assert "<RETRIEVAL_CARD>" in rendered
            assert state["object_ref"] in rendered
            return _response("Continued without sticky retrieval.")
        raise AssertionError("unexpected provider call")

    agent.client.chat.completions.create.side_effect = provider
    durable_trace = []
    compaction_input = {}
    compacted = []
    try:
        first = agent.run_conversation(raw)
        second = agent.run_conversation(
            "Inspect the exact values now.",
            conversation_history=first["messages"],
        )
        third = agent.run_conversation(
            "Continue without loading it again.",
            conversation_history=second["messages"],
        )
        durable_trace = db.get_messages_as_conversation(
            "host-e2e", include_ancestors=True, include_inactive=True
        )
        engine = agent.context_compressor
        engine.protect_first_n = 0
        engine.protect_last_n = 1
        engine.tail_token_budget = 20

        def summarize(turns, **kwargs):
            compaction_input["turns"] = json.loads(
                json.dumps(turns, ensure_ascii=False)
            )
            compaction_input["kwargs"] = kwargs
            return "Earlier turns supplied, retrieved, and used structured data."

        with patch.object(engine, "_generate_summary", side_effect=summarize):
            compacted = engine.compress(third["messages"], force=True)
    finally:
        db.close()

    assert first["final_response"] == "Stored the structured payload."
    assert second["final_response"] == "Used the exact retrieved object."
    assert third["final_response"] == "Continued without sticky retrieval."
    assert len(observed_requests) == 4
    engine = agent.context_compressor
    record = engine._store.get_object("host-e2e", state["object_ref"])
    assert record is not None and record.content == raw
    assert engine._store.path == tmp_path / "context" / "object_context_v1.sqlite3"
    assert any(
        message.get("role") == "user" and message.get("content") == raw
        for message in durable_trace
    )
    assert all(
        "<OBJECT_CARD>" not in str(message.get("content") or "")
        for message in durable_trace
    )
    compaction_json = json.dumps(compaction_input["turns"], ensure_ascii=False)
    assert "<OBJECT_CARD>" in compaction_json
    assert raw not in compaction_json
    assert any(message.get("_compressed_summary") for message in compacted)
    compacted_json = json.dumps(compacted, ensure_ascii=False)
    assert state["object_ref"] in compacted_json
    assert "Inspect exact field values." in compacted_json
    assert raw not in compacted_json
    assert any(
        message.get("role") == "user"
        and message.get("content") == "Continue without loading it again."
        for message in compacted
    )


def test_real_host_multiple_tool_calls_form_one_delta_and_keep_pairing(tmp_path):
    agent, db = _agent(tmp_path, tools=("web_search",))
    large_result = json.dumps({
        f"row_{index}": "result-" + ("z" * 40) for index in range(500)
    })
    tool_calls = [
        _tool_call("web_search", {"query": "alpha"}, "search-call-1"),
        _tool_call("web_search", {"query": "beta"}, "search-call-2"),
    ]
    captured_second_turn = {}
    responses = [
        _response(tool_calls=tool_calls, finish_reason="tool_calls"),
        _response("Both searches completed."),
    ]

    def provider(**kwargs):
        if responses:
            return responses.pop(0)
        captured_second_turn.update(messages=kwargs["messages"])
        return _response("Reviewed the search cards.")

    agent.client.chat.completions.create.side_effect = provider

    with patch("run_agent.handle_function_call", return_value=large_result):
        try:
            first = agent.run_conversation("Run both searches.")
            second = agent.run_conversation(
                "Review what happened.", conversation_history=first["messages"]
            )
        finally:
            db.close()

    assert first["final_response"] == "Both searches completed."
    assert second["final_response"] == "Reviewed the search cards."
    engine = agent.context_compressor
    deltas = engine._store.list_deltas("host-e2e")
    tool_deltas = [
        delta
        for delta in deltas
        if [message.get("role") for message in delta.raw_view]
        == ["assistant", "tool", "tool"]
    ]
    assert len(tool_deltas) == 1
    assert {
        message["tool_call_id"]
        for message in tool_deltas[0].raw_view
        if message.get("role") == "tool"
    } == {"search-call-1", "search-call-2"}

    request = captured_second_turn["messages"]
    assistant = next(message for message in request if message.get("tool_calls"))
    results = [message for message in request if message.get("role") == "tool"]
    assert {call["id"] for call in assistant["tool_calls"]} == {
        "search-call-1",
        "search-call-2",
    }
    assert {message["tool_call_id"] for message in results} == {
        "search-call-1",
        "search-call-2",
    }
    assert all("<OBJECT_CARD>" in message["content"] for message in results)
    assert all(large_result not in message["content"] for message in results)


def test_provider_retry_commits_one_user_and_one_successful_inference_delta(tmp_path):
    agent, db = _agent(tmp_path)
    agent._api_max_retries = 2
    invalid = SimpleNamespace(choices=[], model="test-model", error=None, message=None)
    agent.client.chat.completions.create.side_effect = [
        invalid,
        _response("Succeeded after retry."),
    ]

    try:
        with patch("agent.conversation_loop.jittered_backoff", return_value=0):
            result = agent.run_conversation("Retry this provider request.")
    finally:
        db.close()

    assert result["final_response"] == "Succeeded after retry."
    deltas = agent.context_compressor._store.list_deltas("host-e2e")
    assert [delta.kind for delta in deltas] == ["user", "inference"]
    assert len({delta.delta_id for delta in deltas}) == 2
    assert deltas[0].raw_view[0]["content"] == "Retry this provider request."
    assert deltas[1].raw_view[0]["content"] == "Succeeded after retry."


def test_real_host_synthetic_auto_continue_creates_no_v1_delta(tmp_path):
    agent, db = _agent(tmp_path)
    agent.client.chat.completions.create.return_value = _response(
        "Synthetic recovery completed."
    )

    try:
        result = agent.run_conversation(
            "[System recovery note: continue the interrupted tool flow.]",
            persist_user_display_kind="auto_continue",
        )
    finally:
        db.close()

    assert result["final_response"] == "Synthetic recovery completed."
    assert agent.context_compressor._store.list_deltas("host-e2e") == []


def test_real_host_verification_continuations_share_original_real_turn(tmp_path):
    agent, db = _agent(tmp_path)
    agent.client.chat.completions.create.side_effect = [
        _response("Candidate answer before verification."),
        _response("Verified final answer."),
    ]

    with (
        patch("agent.verification_stop.verify_on_stop_enabled", return_value=True),
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["Run the required verification now.", None],
        ),
    ):
        try:
            result = agent.run_conversation("Make and verify the change.")
        finally:
            db.close()

    assert result["final_response"] == "Verified final answer."
    deltas = agent.context_compressor._store.list_deltas("host-e2e")
    assert [delta.kind for delta in deltas] == ["user", "inference", "inference"]
    assert len({delta.turn_id for delta in deltas}) == 1
    assert [delta.turn_sequence for delta in deltas] == [0, 1, 2]
    delta_text = json.dumps(
        [list(delta.raw_view) for delta in deltas], ensure_ascii=False
    )
    assert "Candidate answer before verification." in delta_text
    assert "Verified final answer." in delta_text
    assert "Run the required verification now." not in delta_text
