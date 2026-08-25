import json
import re
import sqlite3
from copy import deepcopy
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextDelta
from plugins.context_engine import load_context_engine
from plugins.context_engine.object_context.detection import detect_delta_objects
from plugins.context_engine.object_context.engine import ObjectContextEngine
from plugins.context_engine.object_context.models import DeltaState


class _DeterministicSummary:
    def generate(self, *, engine, record, contains, previous=None):
        del engine, contains, previous
        return f"Exact {record.object_type.value} payload for this conversation.", False


def _config(**overrides):
    object_config = {
        "hot_tail_max_deltas": 1,
        "hot_tail_token_budget_ratio": 0.01,
        "context_soft_limit_ratio": 0.75,
        "object_prefilter_min_tokens": 1,
        "min_absolute_saving_tokens": 1,
        "min_relative_saving_ratio": 0.0,
        "summary_max_tokens": 32,
        "wm_grace_deltas": 2,
        "recent_retrieval_active_deltas": 2,
        "retrieval_max_tokens_ratio": 0.9,
    }
    object_config.update(overrides)
    return {
        "compression": {"threshold": 0.5, "protect_last_n": 2},
        "context": {
            "engine": "object_context",
            "object_context": object_config,
        },
    }


class _EmptySessionDB:
    def get_messages_as_conversation(self, *args, **kwargs):
        return []

    def get_conversation_root(self, session_id):
        return "conv-a"


def _started_engine(tmp_path, **config_overrides):
    engine = ObjectContextEngine(
        config=_config(**config_overrides),
        summary_generator=_DeterministicSummary(),
    )
    engine.update_model("test-model", 100_000)
    engine.bind_session_state(_EmptySessionDB(), "session-a")
    engine.on_session_start(
        "session-a",
        hermes_home=str(tmp_path),
        conversation_id="conv-a",
        context_length=100_000,
    )
    return engine


def _delta(
    delta_id,
    kind,
    turn,
    sequence,
    messages,
):
    return ContextDelta(
        delta_id=delta_id,
        kind=kind,
        conversation_id="conv-a",
        session_id="session-a",
        turn_id=turn,
        sequence=sequence,
        messages=tuple(messages),
        inference_id=(f"{turn}:inference:{sequence}" if kind == "inference" else ""),
    )


def _large_json():
    return json.dumps(
        {f"field_{index}": "value-" + ("x" * 32) for index in range(400)},
        ensure_ascii=False,
    )


def _prepare_user_card(engine):
    raw = _large_json()
    user1 = {"role": "user", "content": raw, "timestamp": 1.0}
    assistant1 = {"role": "assistant", "content": "I received it.", "timestamp": 2.0}
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user1]))
    engine.on_delta_committed(
        _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant1])
    )
    engine.on_turn_complete([user1, assistant1], turn_id="turn-1", interrupted=False)
    user2 = {"role": "user", "content": "Use that data.", "timestamp": 3.0}
    engine.on_delta_committed(_delta("turn-2:user:0", "user", "turn-2", 0, [user2]))
    history = [user1, assistant1, user2]
    selected = engine.select_context(history)
    assert selected is not None
    match = re.search(r"object://obj_[a-f0-9]{24}@v1", selected[0]["content"])
    assert match is not None
    return raw, history, selected, match.group(0)


def test_bundled_engine_is_v1_and_exposes_only_exact_retrieval():
    engine = load_context_engine("object_context")
    assert isinstance(engine, ObjectContextEngine)
    assert isinstance(engine, ContextCompressor)
    assert engine.name == "object_context"
    assert {schema["name"] for schema in engine.get_tool_schemas()} == {
        "retrieve_object"
    }


def test_user_delta_exits_hot_tail_to_stable_in_place_card(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, first, object_ref = _prepare_user_card(engine)
    second = engine.select_context(history)

    assert engine._store.get_delta("turn-1:user:0").state == DeltaState.COMPRESSED
    assert raw not in first[0]["content"]
    assert object_ref in first[0]["content"]
    assert first[0]["content"] == second[0]["content"]
    assert first[1:] == history[1:]
    assert history[0]["content"] == raw
    assert engine.compression_count == 1


def test_select_context_records_content_free_projection_identity_and_latency(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, _ = _prepare_user_card(engine)
    engine.select_context(history)

    timeline = engine.get_projection_timeline()

    assert timeline["schema_version"] == 1
    assert timeline["conversation_id"] == "conv-a"
    assert timeline["session_id"] == "session-a"
    assert len(timeline["projections"]) == 2
    assert [event["projection_sequence"] for event in timeline["projections"]] == [
        1,
        2,
    ]
    assert {event["turn_id"] for event in timeline["projections"]} == {"turn-2"}
    assert len(
        {event["projection_id"] for event in timeline["projections"]}
    ) == 2
    for event in timeline["projections"]:
        metrics = event["metrics"]
        assert metrics["tokens_saved"] == (
            metrics["raw_context_tokens"] - metrics["rendered_context_tokens"]
        )
        assert metrics["projection_latency_ms"] >= 0
        assert event["legacy"] is False
    assert raw not in json.dumps(timeline)


def test_summarizer_receives_card_projection_not_automatic_raw_rehydration(
    tmp_path, monkeypatch
):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    captured = {}

    def fake_compress(self, messages, **kwargs):
        captured["messages"] = deepcopy(messages)
        captured["kwargs"] = kwargs
        return messages

    monkeypatch.setattr(ContextCompressor, "compress", fake_compress)
    result = engine.compress(history, current_tokens=9999)

    assert result == captured["messages"]
    assert object_ref in captured["messages"][0]["content"]
    assert raw not in captured["messages"][0]["content"]


def test_real_whole_history_compaction_summarizes_card_view_and_keeps_tail(
    tmp_path, monkeypatch
):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    messages = [
        *history,
        {"role": "assistant", "content": "Acknowledged.", "timestamp": 4.0},
        {"role": "user", "content": "Question three.", "timestamp": 5.0},
        {"role": "assistant", "content": "Answer three.", "timestamp": 6.0},
        {"role": "user", "content": "Question four.", "timestamp": 7.0},
        {"role": "assistant", "content": "Answer four.", "timestamp": 8.0},
        {"role": "user", "content": "Newest request.", "timestamp": 9.0},
    ]
    captured = {}
    engine.protect_first_n = 0
    engine.protect_last_n = 1
    engine.tail_token_budget = 20

    def summarize(turns, **kwargs):
        captured["turns"] = deepcopy(turns)
        captured["kwargs"] = kwargs
        return "The earlier exchange supplied structured data and discussed its use."

    monkeypatch.setattr(engine, "_generate_summary", summarize)
    result = engine.compress(messages, force=True)

    summarized_input = json.dumps(captured["turns"], ensure_ascii=False)
    assert object_ref in summarized_input
    assert raw not in summarized_input
    assert result[-1]["content"] == "Newest request."
    assert any(message.get("_compressed_summary") for message in result)
    assert all(
        message.get("role") != result[index - 1].get("role")
        for index, message in enumerate(result[1:], start=1)
        if message.get("role") in {"user", "assistant"}
        and result[index - 1].get("role") in {"user", "assistant"}
    )


def test_batch_scheduler_publishes_multiple_newly_cold_deltas_once(tmp_path):
    engine = _started_engine(tmp_path)
    raw_messages = []
    for index in range(1, 3):
        user = {
            "role": "user",
            "content": json.dumps({
                f"batch_{index}_{item}": "x" * 64 for item in range(160)
            }),
            "timestamp": float(index * 2 - 1),
        }
        assistant = {
            "role": "assistant",
            "content": f"Finished {index}.",
            "timestamp": float(index * 2),
        }
        engine._ingest_delta(
            _delta(f"turn-{index}:user:0", "user", f"turn-{index}", 0, [user]),
            schedule=False,
        )
        engine._ingest_delta(
            _delta(
                f"turn-{index}:inference:1",
                "inference",
                f"turn-{index}",
                1,
                [assistant],
            ),
            schedule=False,
        )
        raw_messages.extend((user, assistant))
    newest = {"role": "user", "content": "Newest.", "timestamp": 5.0}
    engine._ingest_delta(
        _delta("turn-3:user:0", "user", "turn-3", 0, [newest]), schedule=False
    )

    engine._schedule_newly_cold()

    assert engine._last_batch_size == 2
    assert engine.compression_count == 2
    assert all(
        engine._store.get_delta(f"turn-{index}:user:0").state == DeltaState.COMPRESSED
        for index in range(1, 3)
    )
    first = engine.select_context([*raw_messages, newest])
    second = engine.select_context([*raw_messages, newest])
    assert first == second
    assert json.dumps(first, ensure_ascii=False).count("<OBJECT_CARD>") == 2


def test_api_content_sidecar_projection_uses_clean_raw_identity(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    provider_request = deepcopy(history)
    provider_request[0]["content"] = (
        raw + "\n\n<ephemeral-memory>note</ephemeral-memory>"
    )

    selected = engine.select_context(provider_request, conversation_messages=history)

    assert selected is not None
    assert object_ref in selected[0]["content"]
    assert raw not in selected[0]["content"]
    assert selected[0]["content"].endswith("<ephemeral-memory>note</ephemeral-memory>")


def test_exact_retrieval_mounts_for_turn_then_becomes_retrieval_card(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, selected, object_ref = _prepare_user_card(engine)
    before_count = len(engine._store.list_objects("conv-a"))
    tool_call_id = "call-retrieve-1"
    assistant_call = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": "retrieve_object",
                    "arguments": json.dumps({
                        "object_ref": object_ref,
                        "reason": "Inspect exact values.",
                    }),
                },
            }
        ],
        "timestamp": 4.0,
    }
    result = engine.handle_tool_call(
        "retrieve_object",
        {"object_ref": object_ref, "reason": "Inspect exact values."},
        tool_call_id=tool_call_id,
    )
    assert json.loads(result)["retrieved_object"]["content"] == raw
    tool_result = {
        "role": "tool",
        "name": "retrieve_object",
        "tool_name": "retrieve_object",
        "tool_call_id": tool_call_id,
        "content": result,
        "timestamp": 5.0,
    }
    engine.on_delta_committed(
        _delta(
            "turn-2:inference:1",
            "inference",
            "turn-2",
            1,
            [assistant_call, tool_result],
        )
    )
    current_trace = [*history, assistant_call, tool_result]
    mounted = engine.select_context(current_trace)
    assert json.loads(mounted[-1]["content"])["retrieved_object"]["content"] == raw
    assert object_ref in mounted[0]["content"]
    assert len(engine._store.list_objects("conv-a")) == before_count

    engine.on_turn_complete(current_trace, turn_id="turn-2", interrupted=False)
    user3 = {"role": "user", "content": "Continue.", "timestamp": 6.0}
    engine.on_delta_committed(_delta("turn-3:user:0", "user", "turn-3", 0, [user3]))
    later = engine.select_context([*current_trace, user3])
    later_again = engine.select_context([*current_trace, user3])

    assert later_again == later
    assert raw not in later[-2]["content"]
    assert "<RETRIEVAL_CARD>" in later[-2]["content"]
    assert object_ref in later[-2]["content"]
    assert later[-2]["role"] == "tool"
    assert later[-2]["tool_call_id"] == tool_call_id
    assert later[-2]["name"] == "retrieve_object"
    assert object_ref in later[0]["content"]
    assert engine._store.get_object("conv-a", object_ref).content == raw


def test_internal_mount_interface_is_authorized_and_turn_scoped(tmp_path):
    engine = _started_engine(tmp_path)
    _, _, _, object_ref = _prepare_user_card(engine)

    lease = engine.mount_object(
        "turn-2",
        object_ref,
        reason="Internal exact use.",
        tool_call_id="internal-mount-1",
    )

    assert lease.turn_id == "turn-2"
    assert lease.object_ref == object_ref
    assert lease.expires_at == "turn_end"
    assert engine._store.list_leases("conv-a", "turn-2") == [lease]
    assert engine.unmount_turn("turn-2") == 1
    assert engine._store.list_leases("conv-a", "turn-2") == []
    with pytest.raises(RuntimeError, match="active real user turn"):
        engine.mount_object("turn-3", object_ref)


def test_retrieval_errors_are_explicit_and_never_return_guessed_content(tmp_path):
    engine = _started_engine(tmp_path)
    engine._active_turn_id = "turn"
    malformed = json.loads(
        engine.handle_tool_call(
            "retrieve_object", {"object_ref": "bad", "reason": "inspect"}
        )
    )
    missing = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {
                "object_ref": "object://obj_000000000000000000000000@v1",
                "reason": "inspect",
            },
        )
    )
    assert malformed["retrieval_error"]["code"] == "MALFORMED_OBJECT_REF"
    assert missing["retrieval_error"]["code"] == "OBJECT_NOT_FOUND"
    assert malformed["retrieval_error"]["exact_content_returned"] is False


def test_retrieval_rejects_cross_conversation_and_oversize_without_truncation(
    tmp_path,
):
    engine = _started_engine(tmp_path)
    foreign_content = _large_json()
    foreign_message = {
        "role": "user",
        "content": foreign_content,
        "timestamp": 101.0,
    }
    observed = ContextDelta(
        delta_id="foreign:user:0",
        kind="user",
        conversation_id="conv-b",
        session_id="session-b",
        turn_id="foreign-turn",
        sequence=0,
        messages=(foreign_message,),
    )
    foreign_delta = engine._store.register_delta(
        delta_id=observed.delta_id,
        conversation_id="conv-b",
        session_id="session-b",
        turn_id="foreign-turn",
        kind="user",
        inference_id="",
        turn_sequence=0,
        raw_view=observed.messages,
    )
    [foreign_detected] = detect_delta_objects(observed, min_tokens=1)
    foreign = engine._store.register_object(
        conversation_id="conv-b",
        session_id="session-b",
        delta=foreign_delta,
        detected=foreign_detected,
    )
    engine._active_turn_id = "turn-a"

    unauthorized = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {"object_ref": foreign.object_ref, "reason": "Inspect foreign data."},
        )
    )
    assert unauthorized["retrieval_error"]["code"] == "UNAUTHORIZED_OBJECT_REFERENCE"
    assert foreign_content not in json.dumps(unauthorized, ensure_ascii=False)

    _, _, _, local_ref = _prepare_user_card(engine)
    engine.context_length = 10
    too_large = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {"object_ref": local_ref, "reason": "Inspect the whole object."},
        )
    )
    assert too_large["retrieval_error"]["code"] == "OBJECT_TOO_LARGE"
    assert too_large["retrieval_error"]["exact_content_returned"] is False
    assert foreign_content not in json.dumps(too_large, ensure_ascii=False)


def test_hash_corruption_returns_integrity_error(tmp_path):
    engine = _started_engine(tmp_path)
    _, _, _, object_ref = _prepare_user_card(engine)
    record = engine._store.get_object("conv-a", object_ref)
    with sqlite3.connect(engine._store.path) as conn:
        conn.execute(
            "UPDATE blobs SET content = ? WHERE sha256 = ?",
            (sqlite3.Binary(b"corrupt"), record.sha256),
        )
    payload = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {"object_ref": object_ref, "reason": "inspect"},
            tool_call_id="call",
        )
    )
    assert payload["retrieval_error"]["code"] == "OBJECT_HASH_MISMATCH"


def test_tool_result_card_preserves_provider_pairing_and_surrounding_messages(tmp_path):
    engine = _started_engine(tmp_path)
    user1 = {"role": "user", "content": "Run it.", "timestamp": 1.0}
    tool_call_id = "call-large-tool"
    assistant = {
        "role": "assistant",
        "content": "Running now.",
        "tool_calls": [
            {
                "id": tool_call_id,
                "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
        "timestamp": 2.0,
    }
    output = "INFO step=%d metric=%d\n" * 600
    output = "".join(f"INFO step={index} metric={index * 2}\n" for index in range(600))
    tool = {
        "role": "tool",
        "name": "terminal",
        "tool_name": "terminal",
        "tool_call_id": tool_call_id,
        "content": output,
        "timestamp": 3.0,
    }
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user1]))
    engine.on_delta_committed(
        _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant, tool])
    )
    engine.on_turn_complete([user1, assistant, tool], turn_id="turn-1")
    next_user = {"role": "user", "content": "What happened?", "timestamp": 4.0}
    engine.on_delta_committed(_delta("turn-2:user:0", "user", "turn-2", 0, [next_user]))
    projected = engine.select_context([user1, assistant, tool, next_user])

    assert projected is not None
    assert projected[1] == assistant
    assert projected[2]["role"] == "tool"
    assert projected[2]["tool_call_id"] == tool_call_id
    assert projected[2]["name"] == "terminal"
    assert "<OBJECT_CARD>" in projected[2]["content"]
    assert output not in projected[2]["content"]


@pytest.mark.parametrize("failure_target", ["extract_structure", "build_card"])
def test_prepare_failure_keeps_raw_and_marks_delta_failed(tmp_path, failure_target):
    engine = _started_engine(tmp_path)
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    assistant = {"role": "assistant", "content": "Done.", "timestamp": 2.0}
    next_user = {"role": "user", "content": "Continue.", "timestamp": 3.0}
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
    with patch(
        f"plugins.context_engine.object_context.engine.{failure_target}",
        side_effect=RuntimeError(f"{failure_target} failed"),
    ):
        engine.on_delta_committed(
            _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant])
        )
        engine.on_turn_complete([user, assistant], turn_id="turn-1")
        engine.on_delta_committed(
            _delta("turn-2:user:0", "user", "turn-2", 0, [next_user])
        )
        projected = engine.select_context([user, assistant, next_user])

    failed = engine._store.get_delta("turn-1:user:0")
    assert failed.state == DeltaState.COMPRESSION_FAILED
    assert failed.compressed_view is None
    assert projected is None
    assert user["content"] == raw
    failures = [
        row
        for row in engine._store.metrics("conv-a")
        if row["name"] == "compression_failures"
    ]
    assert failures
    assert json.loads(failures[-1]["metadata_json"])["stage"] == "prepare_or_validate"
    assert engine.compress_delta("turn-1:user:0") is True
    completed_count = engine.compression_count
    assert engine.compress_delta("turn-1:user:0") is True
    assert engine.compression_count == completed_count
    assert raw not in engine.build_context([user, assistant, next_user])[0]["content"]


def test_object_store_and_atomic_commit_failures_never_publish_cards(tmp_path):
    engine = _started_engine(tmp_path)
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    assistant = {"role": "assistant", "content": "Done.", "timestamp": 2.0}
    next_user = {"role": "user", "content": "Continue.", "timestamp": 3.0}
    with patch.object(
        engine._store, "register_object", side_effect=RuntimeError("disk full")
    ):
        engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
        engine.on_delta_committed(
            _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant])
        )
        engine.on_turn_complete([user, assistant], turn_id="turn-1")
        engine.on_delta_committed(
            _delta("turn-2:user:0", "user", "turn-2", 0, [next_user])
        )
    assert engine.select_context([user, assistant, next_user]) is None
    assert raw == user["content"]
    assert engine._store.list_objects("conv-a") == []

    healthy = _started_engine(tmp_path / "commit")
    healthy.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
    with patch.object(
        healthy._store,
        "publish_compressed_batch",
        side_effect=RuntimeError("commit failed"),
    ):
        healthy.on_delta_committed(
            _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant])
        )
        healthy.on_turn_complete([user, assistant], turn_id="turn-1")
        healthy.on_delta_committed(
            _delta("turn-2:user:0", "user", "turn-2", 0, [next_user])
        )
    failed = healthy._store.get_delta("turn-1:user:0")
    assert failed.state == DeltaState.COMPRESSION_FAILED
    assert failed.compressed_view is None
    assert healthy.select_context([user, assistant, next_user]) is None
    assert healthy.compress_delta("turn-1:user:0") is True
    assert raw not in healthy.build_context([user, assistant, next_user])[0]["content"]


def test_store_parser_renderer_and_resolver_fail_open_to_raw(tmp_path):
    with patch(
        "plugins.context_engine.object_context.engine.ObjectContextStore",
        side_effect=RuntimeError("store unavailable"),
    ):
        unavailable = ObjectContextEngine(
            config=_config(), summary_generator=_DeterministicSummary()
        )
        unavailable.bind_session_state(_EmptySessionDB(), "session-a")
        unavailable.on_session_start(
            "session-a",
            hermes_home=str(tmp_path / "unavailable"),
            conversation_id="conv-a",
        )
    raw_message = {"role": "user", "content": _large_json(), "timestamp": 1.0}
    assert unavailable._store is None
    assert unavailable.select_context([raw_message]) is None
    assert unavailable.get_status()["object_context_available"] is False

    parser = _started_engine(tmp_path / "parser")
    assistant = {"role": "assistant", "content": "Done.", "timestamp": 2.0}
    next_user = {"role": "user", "content": "Continue.", "timestamp": 3.0}
    with patch(
        "plugins.context_engine.object_context.engine.detect_delta_objects",
        side_effect=RuntimeError("parser crashed"),
    ):
        parser.on_delta_committed(
            _delta("turn-1:user:0", "user", "turn-1", 0, [raw_message])
        )
        parser.on_delta_committed(
            _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant])
        )
        parser.on_turn_complete([raw_message, assistant], turn_id="turn-1")
        parser.on_delta_committed(
            _delta("turn-2:user:0", "user", "turn-2", 0, [next_user])
        )
    assert parser.select_context([raw_message, assistant, next_user]) is None
    assert parser._store.list_objects("conv-a") == []

    renderer = _started_engine(tmp_path / "renderer")
    raw, history, _, object_ref = _prepare_user_card(renderer)
    with patch(
        "plugins.context_engine.object_context.engine.project_compressed_messages",
        side_effect=RuntimeError("render failed"),
    ):
        assert renderer.select_context(history) is None
    assert history[0]["content"] == raw
    assert any(
        row["name"] == "compression_failures"
        and json.loads(row["metadata_json"]).get("stage") == "renderer"
        for row in renderer._store.metrics("conv-a")
    )

    renderer._active_turn_id = "turn-2"
    with patch.object(
        renderer._store, "get_object", side_effect=OSError("resolver failed")
    ):
        failure = json.loads(
            renderer.handle_tool_call(
                "retrieve_object",
                {"object_ref": object_ref, "reason": "Inspect exact data."},
            )
        )
    assert failure["retrieval_error"]["code"] == "RESOLVER_FAILURE"
    assert raw not in json.dumps(failure, ensure_ascii=False)


def test_default_config_exposes_every_object_context_setting():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    configured = DEFAULT_CONFIG["context"]["object_context"]
    assert set(configured) == {
        "hot_tail_max_deltas",
        "hot_tail_token_budget_ratio",
        "context_soft_limit_ratio",
        "object_prefilter_min_tokens",
        "min_absolute_saving_tokens",
        "min_relative_saving_ratio",
        "summary_max_tokens",
        "wm_grace_deltas",
        "recent_retrieval_active_deltas",
        "retrieval_max_tokens_ratio",
    }
    engine = ObjectContextEngine(
        config=DEFAULT_CONFIG, summary_generator=_DeterministicSummary()
    )
    assert engine.hot_tail_max_deltas == configured["hot_tail_max_deltas"]
    assert engine.summary_max_tokens == configured["summary_max_tokens"]


def test_active_root_scanning_and_gc_only_run_at_lifecycle_boundaries(tmp_path):
    engine = _started_engine(tmp_path)
    _, history, selected, object_ref = _prepare_user_card(engine)
    root_message = {
        "role": "assistant",
        "content": "root metadata",
        "workspace": {"source": object_ref},
        "artifact": {"derived_from": object_ref},
        "tool_calls": [
            {
                "id": "pending",
                "type": "function",
                "function": {
                    "name": "use_object",
                    "arguments": json.dumps({"object_ref": object_ref}),
                },
            }
        ],
    }
    assert engine._refs_in_messages([root_message]) == {object_ref}
    engine._last_rendered_refs = {object_ref}
    counts = engine._run_activity_gc()
    assert counts["active"] == 1

    with patch.object(engine, "_run_activity_gc", wraps=engine._run_activity_gc) as gc:
        engine.select_context([*history, root_message])
        assert gc.call_count == 0
        engine.on_turn_complete(
            [*history, root_message], turn_id="turn-2", interrupted=False
        )
        assert gc.call_count == 1
    assert object_ref in json.dumps(selected, ensure_ascii=False)


def test_metrics_and_status_cover_v1_without_leaking_content_or_store_path(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    engine.update_from_response({
        "prompt_tokens": 1_000,
        "completion_tokens": 100,
        "total_tokens": 1_100,
        "input_tokens": 1_000,
        "output_tokens": 100,
        "cache_read_tokens": 750,
        "cache_write_tokens": 50,
    })
    for index in range(2):
        result = json.loads(
            engine.handle_tool_call(
                "retrieve_object",
                {"object_ref": object_ref, "reason": f"Inspect pass {index}."},
                tool_call_id=f"metric-retrieve-{index}",
            )
        )
        assert result["retrieved_object"]["content"] == raw
    json.loads(
        engine.handle_tool_call(
            "retrieve_object", {"object_ref": "bad", "reason": "fail"}
        )
    )
    engine.on_turn_complete(history, turn_id="turn-2", interrupted=False)
    next_user = {"role": "user", "content": "Use it again.", "timestamp": 9.0}
    engine.on_delta_committed(_delta("turn-3:user:0", "user", "turn-3", 0, [next_user]))
    consecutive = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {"object_ref": object_ref, "reason": "Inspect in the next turn."},
            tool_call_id="metric-retrieve-next-turn",
        )
    )
    assert consecutive["retrieved_object"]["content"] == raw
    engine.on_turn_complete([*history, next_user], turn_id="turn-3", interrupted=False)

    names = {row["name"] for row in engine._store.metrics("conv-a")}
    assert {
        "raw_context_tokens",
        "rendered_context_tokens",
        "hot_tail_tokens",
        "card_tokens",
        "tokens_saved",
        "compression_ratio",
        "compression_latency_ms",
        "prompt_prefix_rewrite_events",
        "prompt_prefix_rewritten_deltas",
        "objects_detected",
        "objects_externalized",
        "objects_skipped_small",
        "retrieval_count",
        "retrieval_failures",
        "retrieval_latency_ms",
        "retrieved_tokens",
        "repeated_retrieval_rate",
        "consecutive_turn_retrievals",
        "turns_object_remained_mounted",
        "working_memory_object_count",
        "working_memory_bytes",
        "active_object_count",
        "inactive_candidate_count",
        "evictable_object_count",
        "exact_recovery_hash_pass_rate",
        "cache_read_tokens",
        "cache_write_tokens",
        "prompt_cache_hit_ratio",
    } <= names
    failure_rows = [
        row
        for row in engine._store.metrics("conv-a")
        if row["name"] == "retrieval_failures"
    ]
    assert json.loads(failure_rows[-1]["metadata_json"])["code"] == (
        "MALFORMED_OBJECT_REF"
    )
    status = engine.get_status()
    assert status["retrieval_count"] == 3
    assert status["objects_never_retrieved"] == 0
    assert status["retrieval_overhead"] > 0
    assert status["metric_totals"]["consecutive_turn_retrievals"] == 1
    assert status["request_projection_count"] == 1
    assert status["request_metric_totals"]["tokens_saved"] > 0
    assert status["last_request_metrics"]["raw_context_tokens"] > (
        status["last_request_metrics"]["rendered_context_tokens"]
    )
    encoded = json.dumps(status, ensure_ascii=False)
    assert raw not in encoded
    assert str(tmp_path) not in encoded


def test_status_bar_savings_are_in_memory_and_restore_on_resume(tmp_path):
    engine = _started_engine(tmp_path)
    assert engine.get_status_bar_metrics() == {
        "object_context_active": True,
        "last_tokens_saved": 0,
        "last_reduction_percent": 0.0,
        "session_tokens_saved": 0,
        "session_reduction_percent": 0.0,
        "request_projection_count": 0,
    }

    _prepare_user_card(engine)
    live = engine.get_status_bar_metrics()
    assert live["last_tokens_saved"] > 0
    assert live["last_reduction_percent"] > 0
    assert live["session_tokens_saved"] == live["last_tokens_saved"]
    assert live["request_projection_count"] == 1

    with patch.object(
        engine._store,
        "aggregate_status",
        side_effect=AssertionError("status bar repaint must not query SQLite"),
    ):
        assert engine.get_status_bar_metrics() == live

    resumed = _started_engine(tmp_path)
    restored = resumed.get_status_bar_metrics()
    assert restored == live

    resumed.on_session_reset()
    assert resumed.get_status_bar_metrics() == {}


def test_real_agent_init_uses_profile_scoped_v1_store_and_injects_tool(tmp_path):
    cfg = _config()
    with (
        patch("hermes_cli.config.load_config", return_value=cfg),
        patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("agent.agent_init.get_hermes_home", return_value=tmp_path),
        patch("agent.model_metadata.get_model_context_length", return_value=100_000),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        from run_agent import AIAgent

        agent = AIAgent(
            model="test-model",
            api_key="test-key-not-a-real-secret",
            base_url="https://example.invalid/v1",
            session_id="session-v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    assert isinstance(agent.context_compressor, ObjectContextEngine)
    assert agent.context_compressor._store.path == (
        tmp_path / "context" / "object_context_v1.sqlite3"
    )
    assert "retrieve_object" in agent.valid_tool_names
