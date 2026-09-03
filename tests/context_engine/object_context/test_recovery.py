import json

from agent.context_engine import ContextDelta
from plugins.context_engine.object_context.engine import ObjectContextEngine


class _DeterministicSummary:
    def generate(self, *, engine, record, contains, previous=None):
        del engine, contains, previous
        return f"Exact {record.object_type.value} payload.", False


def _config():
    return {
        "compression": {"threshold": 0.99},
        "context": {
            "engine": "object_context",
            "object_context": {
                "hot_tail_max_deltas": 1,
                "hot_tail_token_budget_ratio": 0.01,
                "object_prefilter_min_tokens": 1,
                "min_absolute_saving_tokens": 1,
                "min_relative_saving_ratio": 0.0,
            },
        },
    }


class _HistoryDB:
    def __init__(self, messages):
        self.messages = messages

    def get_messages_as_conversation(self, *args, **kwargs):
        return [dict(message) for message in self.messages]

    def get_conversation_root(self, session_id):
        return "conversation-root"


def _start(tmp_path, db, *, session_id="parent", boundary_reason=""):
    engine = ObjectContextEngine(
        config=_config(), summary_generator=_DeterministicSummary()
    )
    engine.update_model("test-model", 100_000)
    engine.bind_session_state(db, session_id)
    kwargs = {
        "hermes_home": str(tmp_path),
        "conversation_id": "conversation-root",
    }
    if boundary_reason:
        kwargs.update(boundary_reason=boundary_reason, old_session_id="parent")
    engine.on_session_start(session_id, **kwargs)
    return engine


def test_restart_reconciles_missed_raw_trace_once_without_duplicate_occurrences(
    tmp_path,
):
    raw = json.dumps({f"field_{i}": "x" * 24 for i in range(100)})
    history = [
        {
            "role": "user",
            "content": raw,
            "timestamp": 1.0,
            "_row_id": 1,
        },
        {
            "role": "assistant",
            "content": "Acknowledged.",
            "timestamp": 2.0,
            "_row_id": 2,
        },
    ]
    db = _HistoryDB(history)
    first = _start(tmp_path, db)

    assert len(first._store.list_deltas("conversation-root")) == 2
    assert len(first._store.list_objects("conversation-root")) == 1
    [record] = first._store.list_objects("conversation-root")
    assert record.content == raw

    second = _start(tmp_path, db)
    assert len(second._store.list_deltas("conversation-root")) == 2
    assert len(second._store.list_objects("conversation-root")) == 1
    assert (
        second._store.get_object("conversation-root", record.object_ref).content == raw
    )


def test_reconciliation_matches_live_delta_despite_non_durable_runtime_fields(tmp_path):
    raw = json.dumps({f"key_{i}": i for i in range(100)})
    clean = {"role": "user", "content": raw, "timestamp": 1.0}
    db = _HistoryDB([{**clean, "_row_id": 1}])
    first = _start(tmp_path, _HistoryDB([]))
    first.on_delta_committed(
        ContextDelta(
            delta_id="live-turn:user:0",
            kind="user",
            conversation_id="conversation-root",
            session_id="parent",
            turn_id="live-turn",
            sequence=0,
            messages=({**clean, "_db_persisted": True},),
        )
    )
    assert len(first._store.list_objects("conversation-root")) == 1

    resumed = _start(tmp_path, db)
    assert len(resumed._store.list_deltas("conversation-root")) == 1
    assert len(resumed._store.list_objects("conversation-root")) == 1


def test_restart_does_not_confirm_an_unfinished_request_exposure(tmp_path):
    raw = json.dumps({f"field_{i}": "x" * 24 for i in range(100)})
    clean = {"role": "user", "content": raw, "timestamp": 1.0}
    durable = {**clean, "_row_id": 1}
    first = _start(tmp_path, _HistoryDB([]))
    first.on_delta_committed(
        ContextDelta(
            delta_id="pending:user:0",
            kind="user",
            conversation_id="conversation-root",
            session_id="parent",
            turn_id="pending",
            sequence=0,
            messages=(clean,),
        )
    )
    first.select_context([clean])
    assert first._store.get_delta("pending:user:0").raw_seen_count == 0

    resumed = _start(tmp_path, _HistoryDB([durable]))
    unseen = resumed._store.get_delta("pending:user:0")
    assert unseen is not None
    assert unseen.raw_seen_count == 0
    assert unseen.first_seen_request_sequence is None

    resumed.select_context([clean])
    resumed.update_from_response({})
    confirmed = resumed._store.get_delta("pending:user:0")
    assert confirmed.raw_seen_count == 1
    assert confirmed.first_seen_request_sequence == 1


def test_compression_session_rotation_keeps_conversation_authority_and_sequence(
    tmp_path,
):
    db = _HistoryDB([])
    engine = _start(tmp_path, db)
    raw = json.dumps({f"field_{i}": "value" for i in range(100)})
    engine.on_delta_committed(
        ContextDelta(
            delta_id="parent-turn:user:0",
            kind="user",
            conversation_id="conversation-root",
            session_id="parent",
            turn_id="parent-turn",
            sequence=0,
            messages=({"role": "user", "content": raw, "timestamp": 1.0},),
        )
    )
    [record] = engine._store.list_objects("conversation-root")
    prior_sequence = engine._store.list_deltas("conversation-root")[-1].global_sequence

    engine.on_session_start(
        "child",
        boundary_reason="compression",
        old_session_id="parent",
        conversation_id="conversation-root",
    )
    engine.on_delta_committed(
        ContextDelta(
            delta_id="child-turn:user:0",
            kind="user",
            conversation_id="conversation-root",
            session_id="child",
            turn_id="child-turn",
            sequence=0,
            messages=({"role": "user", "content": "continue", "timestamp": 2.0},),
        )
    )

    assert engine._conversation_id == "conversation-root"
    assert engine._object_session_id == "child"
    assert (
        engine._store.get_object("conversation-root", record.object_ref).content == raw
    )
    assert (
        engine._store.list_deltas("conversation-root")[-1].global_sequence
        == prior_sequence + 1
    )
