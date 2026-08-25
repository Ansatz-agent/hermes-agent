"""Universal, content-free per-request telemetry for the session monitor."""

from __future__ import annotations

from hermes_state import SessionDB


def _event(sequence: int, *, turn_id: str = "turn-1") -> dict:
    return {
        "conversation_id": "conversation-1",
        "api_request_id": f"turn-1:api:{sequence}",
        "turn_id": turn_id,
        "request_sequence": sequence,
        "started_at": 1_700_000_000 + sequence,
        "duration_ms": 1250 + sequence,
        "model": "model-a",
        "billing_provider": "provider-a",
        "input_tokens": 100 * sequence,
        "output_tokens": 10 * sequence,
        "cache_read_tokens": 50 * sequence,
        "cache_write_tokens": 5 * sequence,
        "reasoning_tokens": 3 * sequence,
    }


def test_request_events_persist_independently_of_context_engine(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli")
        db.queue_token_counts(
            "session-1",
            input_tokens=100,
            output_tokens=10,
            cache_read_tokens=50,
            cache_write_tokens=5,
            reasoning_tokens=3,
            api_call_count=1,
            model="model-a",
            billing_provider="provider-a",
            request_event=_event(1),
        )
        db.queue_token_counts(
            "session-1",
            input_tokens=200,
            output_tokens=20,
            cache_read_tokens=100,
            cache_write_tokens=10,
            reasoning_tokens=6,
            api_call_count=1,
            model="model-a",
            billing_provider="provider-a",
            request_event=_event(2, turn_id="turn-2"),
        )

        assert db.flush_token_counts()
        timeline = db.request_usage_timeline("conversation-1")

        assert [event["request_sequence"] for event in timeline] == [1, 2]
        assert [event["turn_id"] for event in timeline] == ["turn-1", "turn-2"]
        assert timeline[0]["metrics"] == {
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 50,
            "cache_write_tokens": 5,
            "reasoning_tokens": 3,
            "prompt_tokens": 155,
            "total_tokens": 165,
            "api_duration_ms": 1251.0,
        }
        assert timeline[1]["metrics"]["total_tokens"] == 330
        assert db.get_session("session-1")["api_call_count"] == 2
    finally:
        db.close()


def test_request_table_has_no_content_bearing_columns_and_cascades(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli")
        db.update_token_counts(
            "session-1",
            input_tokens=1,
            api_call_count=1,
            request_event=_event(1),
        )
        with db._read_ctx() as conn:
            columns = {
                str(row["name"])
                for row in conn.execute(
                    "PRAGMA table_info('session_request_usage')"
                ).fetchall()
            }
        assert not columns.intersection(
            {"prompt", "content", "message", "messages", "response", "tool_calls"}
        )

        assert db.delete_session("session-1") is True
        assert db.request_usage_timeline("conversation-1") == []
    finally:
        db.close()


def test_content_free_turn_boundaries_exclude_display_only_user_events(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session-1", "cli")
        db.append_message("session-1", "user", "secret one", timestamp=10.0)
        db.append_message(
            "session-1",
            "user",
            "synthetic secret",
            timestamp=11.0,
            display_kind="auto_continue",
        )
        db.append_message("session-1", "assistant", "secret answer", timestamp=12.0)
        db.append_message("session-1", "user", "secret two", timestamp=20.0)

        boundaries = db.content_free_turn_boundaries("session-1")

        assert [item["started_at"] for item in boundaries] == [10.0, 20.0]
        assert all(set(item) == {"turn_id", "started_at"} for item in boundaries)
        assert "secret" not in repr(boundaries)
    finally:
        db.close()
