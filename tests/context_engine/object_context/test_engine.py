import json
import re
import sqlite3
import uuid
from copy import deepcopy
from unittest.mock import patch

import pytest

from agent.context_compressor import ContextCompressor
from agent.context_engine import ContextDelta
from agent.model_metadata import estimate_messages_tokens_rough
from plugins.context_engine import load_context_engine
from plugins.context_engine.object_context.cards import parse_card_text
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
        "card_summary_enabled": True,
        "summary_max_tokens": 32,
        "wm_grace_deltas": 2,
        "recent_retrieval_active_deltas": 2,
        "retrieval_max_tokens_ratio": 0.9,
        # Most tests in this file isolate Card/retrieval mechanics. Treat cache
        # reads and writes equally so the V1.1 economic gate does not mask the
        # mechanism under test; planner cache-cliff behavior has its own suite.
        "economic_min_net_saving_tokens": 1,
        "economic_cache_read_ratio_fallback": 1.0,
        "economic_cache_write_ratio_fallback": 1.0,
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
    # The first successful request establishes raw exposure and the in-memory
    # prefix baseline; commit-time scheduling is intentionally absent in V1.1.
    assert engine.select_context([user1]) is None
    engine.update_from_response({})
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


def _retrieval_messages(engine, *, object_ref, call_id, reason, timestamp):
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "retrieve_object",
                    "arguments": json.dumps({
                        "object_ref": object_ref,
                        "reason": reason,
                    }),
                },
            }
        ],
        "timestamp": timestamp,
    }
    result = engine.handle_tool_call(
        "retrieve_object",
        {"object_ref": object_ref, "reason": reason},
        tool_call_id=call_id,
    )
    tool = {
        "role": "tool",
        "name": "retrieve_object",
        "tool_name": "retrieve_object",
        "tool_call_id": call_id,
        "content": result,
        "timestamp": timestamp + 1,
    }
    return assistant, tool


def test_bundled_engine_is_v1_and_exposes_only_exact_retrieval():
    engine = load_context_engine("object_context")
    assert isinstance(engine, ObjectContextEngine)
    assert isinstance(engine, ContextCompressor)
    assert engine.name == "object_context"
    assert {schema["name"] for schema in engine.get_tool_schemas()} == {
        "retrieve_object"
    }


def test_successful_selection_confirms_raw_exposure_once(tmp_path):
    engine = _started_engine(tmp_path)
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    delta_id = "turn-exposure:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-exposure", 0, [raw_message])
    )

    assert engine.select_context([raw_message]) is None
    assert engine._store.get_delta(delta_id).raw_seen_count == 0
    engine.update_from_response({})
    seen = engine._store.get_delta(delta_id)
    assert seen.raw_seen_count == 1
    assert seen.first_seen_request_sequence == 1
    assert seen.last_seen_request_sequence == 1

    # A duplicated response notification has no pending selection to consume.
    engine.update_from_response({})
    assert engine._store.get_delta(delta_id).raw_seen_count == 1


def test_retry_selection_replaces_unconfirmed_exposure_snapshot(tmp_path):
    engine = _started_engine(tmp_path)
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    delta_id = "turn-retry:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-retry", 0, [raw_message]))

    engine.select_context([raw_message])  # failed provider attempt: no update
    engine.select_context([raw_message])  # retry replaces the pending snapshot
    engine.update_from_response({"prompt_tokens": 100, "total_tokens": 100})

    seen = engine._store.get_delta(delta_id)
    assert seen.raw_seen_count == 1
    assert seen.first_seen_request_sequence == 2
    assert seen.last_seen_request_sequence == 2


def test_snapshot_read_failure_keeps_attempt_fence_until_terminal_outcome(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
    )
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    engine.on_delta_committed(
        _delta("turn-fence:user:0", "user", "turn-fence", 0, [raw_message])
    )

    with patch.object(
        engine._store,
        "list_deltas",
        side_effect=sqlite3.OperationalError("database is busy"),
    ):
        engine._snapshot_raw_exposure([raw_message])

    snapshot = engine._pending_raw_exposure
    assert snapshot is not None
    assert snapshot.raw_delta_ids == ()
    assert snapshot.selected_view == (raw_message,)

    # The unresolved physical attempt still fences publication.  A retry may
    # refresh its Raw/Pending snapshot, but it cannot create a second decision
    # or projection epoch until a terminal verdict releases the first attempt.
    before = len(engine._store.projection_decisions("conv-a"))
    assert engine.select_context([raw_message]) is None
    assert len(engine._store.projection_decisions("conv-a")) == before
    engine.confirm_response_rejected()
    assert engine._pending_raw_exposure is None


def test_retry_refreshes_dormant_pending_membership_before_accrual(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    delta_id = "turn-dormant:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-dormant", 0, [raw_message])
    )
    observed = engine._store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=str(uuid.uuid4()),
        raw_delta_ids=(delta_id,),
        min_raw_exposures=1,
    )
    engine._last_success_sequence = observed.success_sequence
    engine._rebalance_amortized_pending([raw_message])
    ledger = engine._store.get_pending_ledger(delta_id)
    assert ledger is not None

    # The failed first attempt did not carry this durable Pending Delta at all.
    engine._snapshot_raw_exposure([])
    first_attempt = engine._pending_raw_exposure
    assert first_attempt is not None
    assert first_attempt.pending_gains == ()

    # A rebuilt retry now carries it Raw. Publication stays fenced, but the
    # membership snapshot is refreshed so the accepted physical request earns
    # exactly its snapshotted g_i of waiting area.
    assert engine.select_context([raw_message]) is None
    retry_snapshot = engine._pending_raw_exposure
    assert retry_snapshot is not None
    assert retry_snapshot.pending_gains == (
        (delta_id, ledger.gain_tokens, ledger.ledger_generation),
    )
    engine.confirm_response_accepted()
    accrued = engine._store.get_pending_ledger(delta_id)
    assert accrued is not None
    assert accrued.wait_area_token_requests == ledger.gain_tokens


def test_accepted_observation_outbox_retries_at_session_end(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
    )
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    delta_id = "turn-outbox:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-outbox", 0, [raw_message])
    )
    assert engine.select_context([raw_message]) is None

    with patch.object(
        engine._store,
        "confirm_successful_request_observation",
        side_effect=sqlite3.OperationalError("database is busy"),
    ):
        engine.confirm_response_accepted()

    assert engine._store.get_delta(delta_id).raw_seen_count == 0
    assert len(engine._unconfirmed_success_observations) == 1

    engine.on_session_end("session-a", [raw_message])

    assert engine._unconfirmed_success_observations == []
    assert engine._store.get_delta(delta_id).raw_seen_count == 1
    [observation] = engine._store.request_observation_timeline("conv-a")
    assert observation["success_sequence"] == 1
    assert observation["raw_delta_count"] == 1


def test_accepted_observation_outbox_keeps_original_profile_store(tmp_path):
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    engine = _started_engine(
        profile_a,
        scheduler="amortized_batch",
        card_summary_enabled=False,
    )
    raw_message = {
        "role": "user",
        "content": _large_json(),
        "timestamp": 1.0,
    }
    delta_id = "turn-profile-a:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-profile-a", 0, [raw_message])
    )
    assert engine.select_context([raw_message]) is None
    store_a = engine._store

    with patch.object(
        store_a,
        "confirm_successful_request_observation",
        side_effect=sqlite3.OperationalError("database is busy"),
    ):
        engine.confirm_response_accepted()
        # The lifecycle's first retry still fails; only then is the engine
        # rebound to another profile/store.
        engine.on_session_start(
            "session-b",
            hermes_home=str(profile_b),
            conversation_id="conv-b",
            context_length=100_000,
        )

    assert len(engine._unconfirmed_success_observations) == 1
    assert engine._store.path != store_a.path
    engine._flush_unconfirmed_success_observations()

    assert engine._unconfirmed_success_observations == []
    assert store_a.get_delta(delta_id).raw_seen_count == 1
    assert len(store_a.request_observation_timeline("conv-a")) == 1
    assert engine._store.request_observation_timeline("conv-a") == []


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


def test_final_assistant_delta_is_raw_until_a_later_successful_request(tmp_path):
    engine = _started_engine(tmp_path, card_summary_enabled=False)
    user = {"role": "user", "content": "Generate data.", "timestamp": 1.0}
    assistant = {
        "role": "assistant",
        "content": _large_json(),
        "timestamp": 2.0,
    }
    next_user = {"role": "user", "content": "Use it.", "timestamp": 3.0}
    delta_id = "turn-1:inference:1"
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
    assert engine.select_context([user]) is None
    engine.update_from_response({})
    engine.on_delta_committed(
        _delta(delta_id, "inference", "turn-1", 1, [assistant])
    )
    engine.on_turn_complete([user, assistant], turn_id="turn-1")
    engine.on_delta_committed(
        _delta("turn-2:user:0", "user", "turn-2", 0, [next_user])
    )

    first_later_request = engine.select_context([user, assistant, next_user])

    assert first_later_request is None
    unseen = engine._store.get_delta(delta_id)
    assert unseen.raw_seen_count == 0
    assert unseen.state == DeltaState.HOT
    engine.update_from_response({})

    second_later_request = engine.select_context([user, assistant, next_user])

    assert second_later_request is not None
    assert assistant["content"] not in json.dumps(
        second_later_request, ensure_ascii=False
    )
    assert engine._store.get_delta(delta_id).raw_seen_count == 1
    assert engine._store.get_delta(delta_id).state == DeltaState.COMPRESSED


def test_route_switch_requires_a_new_successful_request_baseline(tmp_path):
    engine = _started_engine(tmp_path, card_summary_enabled=False)
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    delta_id = "turn-1:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-1", 0, [user]))
    assert engine.select_context([user]) is None
    engine.update_from_response({})

    engine.update_model("different-route", 100_000, provider="different-provider")
    assert engine.select_context([user]) is None
    wait = engine._store.projection_decisions("conv-a")[-1]
    assert wait["decision_reason"] == "WAIT_NO_BASELINE"
    assert engine._store.get_delta(delta_id).state == DeltaState.HOT

    engine.update_from_response({})
    selected = engine.select_context([user])
    assert selected is not None
    assert raw not in selected[0]["content"]


@pytest.mark.parametrize(
    ("provider", "granularity"),
    [("custom", 1), ("openai", 128), ("anthropic", 1_024)],
)
def test_reusable_prefix_uses_provider_block_rounding(
    tmp_path, provider, granularity
):
    engine = _started_engine(tmp_path / provider)
    engine.update_model("test-model", 100_000, provider=provider)
    messages = [{"role": "user", "content": _large_json(), "timestamp": 1.0}]

    reusable = engine._rough_lcp_tokens(messages, deepcopy(messages))

    assert engine._cache_granularity_tokens() == granularity
    assert reusable > 0
    assert reusable % granularity == 0
    assert reusable <= estimate_messages_tokens_rough(messages)


def test_v12_active_turn_seen_delta_waits_then_flushes_on_amortized_crossing(
    tmp_path, monkeypatch
):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=2,
        hot_tail_max_tokens=100_000,
        amortized_cache_read_weight=0.10,
        economic_min_net_saving_tokens=1,
    )
    old = {"role": "user", "content": _large_json(), "timestamp": 1.0}
    tail = {
        "role": "assistant",
        "content": "stable tail " * 5_000,
        "timestamp": 2.0,
    }
    old_id = "turn-active:user:0"
    tail_id = "turn-active:inference:1"
    engine.on_delta_committed(
        _delta(old_id, "user", "turn-active", 0, [old])
    )

    assert engine.select_context([old]) is None
    engine.update_from_response({})
    engine.on_delta_committed(
        _delta(tail_id, "inference", "turn-active", 1, [tail])
    )
    assert engine._active_turn_id == "turn-active"

    # The tail is first exposed while the older Delta is still inside h=2.
    assert engine.select_context([old, tail]) is None
    engine.update_from_response({})

    # At the next prospective boundary the old Delta becomes Pending.  Force
    # the V1.1 immediate counterfactual flag true: V1.2 must still obey W/Q.
    monkeypatch.setattr(engine, "_exact_threshold_crossed", lambda _score: True)
    assert engine.select_context([old, tail]) is None
    ledger = engine._store.get_pending_ledger(old_id)
    assert ledger is not None
    assert ledger.wait_area_token_requests == 0
    assert engine._store.get_delta(old_id).state == DeltaState.COMPRESSION_ELIGIBLE
    wait = engine._store.projection_decisions("conv-a")[-1]
    assert wait["policy_version"] == "1.2"
    assert wait["decision_mode"] == "amortized"
    assert wait["decision_kind"] == "wait"
    assert wait["immediate_crossed"] is True
    assert wait["amortized_crossed"] is False
    assert wait["wait_loss_projected"] < wait[
        "shared_overhead_equivalent_tokens"
    ]
    assert wait["shared_cached_hot_tokens"] == max(
        0,
        wait["baseline_reusable_prefix_tokens"]
        - max(
            wait["candidate_reusable_prefix_tokens"],
            wait["hot_start_token_offset"],
        ),
    )
    assert wait["cache_granularity_tokens"] == 1_024

    # A failed provider attempt has no callback.  Its retry is fenced from a
    # second decision/publication and replaces the unconfirmed snapshot.
    wait_decision_count = len(engine._store.projection_decisions("conv-a"))
    assert engine.select_context([old, tail]) is None
    assert len(engine._store.projection_decisions("conv-a")) == wait_decision_count

    # Only the accepted retry Raw carry adds one snapshotted g_i to A_i.
    engine.update_from_response({})
    accrued = engine._store.get_pending_ledger(old_id)
    assert accrued.wait_area_token_requests == accrued.gain_tokens

    # The same active turn grants no infinite exemption.  Once the remaining
    # seen tail ages out, Q_shared becomes zero and the Pending Delta flushes.
    selected = engine.select_context([old, tail])
    assert selected is not None
    assert engine._store.get_delta(old_id).state == DeltaState.COMPRESSED
    assert engine._store.get_pending_ledger(old_id) is None
    assert engine._active_turn_id == "turn-active"

    # Treat an immediate second selection as provider retry: committed Cards
    # are reused and no second publication decision is emitted.
    decision_count = len(engine._store.projection_decisions("conv-a"))
    retry = engine.select_context([old, tail])
    assert retry == selected
    assert len(engine._store.projection_decisions("conv-a")) == decision_count


def test_v12_fresh_tail_beyond_l0_is_excluded_from_shared_cache_cost(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    old = {"role": "user", "content": _large_json(), "timestamp": 1.0}
    fresh = {
        "role": "assistant",
        "content": "fresh response that has not been exposed",
        "timestamp": 2.0,
    }
    old_id = "turn-fresh:user:0"
    engine.on_delta_committed(_delta(old_id, "user", "turn-fresh", 0, [old]))
    assert engine.select_context([old]) is None
    engine.update_from_response({})
    engine.on_delta_committed(
        _delta(
            "turn-fresh:inference:1",
            "inference",
            "turn-fresh",
            1,
            [fresh],
        )
    )

    selected = engine.select_context([old, fresh])

    assert selected is not None
    assert engine._store.get_delta(old_id).state == DeltaState.COMPRESSED
    decision = engine._store.projection_decisions("conv-a")[-1]
    assert decision["hot_underexposed_count"] == 1
    assert decision["baseline_reusable_prefix_tokens"] <= decision[
        "hot_start_token_offset"
    ]
    assert decision["shared_cached_hot_tokens"] == 0
    assert decision["shared_overhead_equivalent_tokens"] == 0
    assert decision["amortized_crossed"] is True


def test_v12_new_cache_namespace_uses_cold_zero_shared_cost(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    raw = {"role": "user", "content": _large_json(), "timestamp": 1.0}
    delta_id = "turn-cold:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-cold", 0, [raw]))
    assert engine.select_context([raw]) is None
    engine.update_from_response({})

    previous_namespace = engine._cache_namespace_hash
    engine.update_model(
        "test-model",
        100_000,
        provider="openai",
        api_key="different-cache-account",
    )
    assert engine._cache_namespace_hash != previous_namespace
    assert engine._cache_baseline_state == "cold"
    assert engine._previous_successful_request_view is None

    selected = engine.select_context([raw])

    assert selected is not None
    assert engine._store.get_delta(delta_id).state == DeltaState.COMPRESSED
    decision = engine._store.projection_decisions("conv-a")[-1]
    assert decision["baseline_state"] == "cold"
    assert decision["shared_cached_hot_tokens"] == 0
    assert decision["shared_overhead_equivalent_tokens"] == 0
    assert decision["amortized_crossed"] is True
    assert "different-cache-account" not in json.dumps(decision)


def test_v12_pending_capacity_flushes_with_unknown_prefix_facts(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
        amortized_cache_read_weight=0.10,
    )
    messages = []
    delta_ids = []
    for index in range(3):
        message = {
            "role": "user",
            "content": _large_json().replace("field_0", f"field_{index}_0", 1),
            "timestamp": float(index + 1),
        }
        delta_id = f"turn-capacity-{index}:user:0"
        messages.append(message)
        delta_ids.append(delta_id)
        engine.on_delta_committed(
            _delta(delta_id, "user", f"turn-capacity-{index}", 0, [message])
        )
        observation = engine._store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=str(uuid.uuid4()),
            raw_delta_ids=(delta_id,),
            min_raw_exposures=1,
        )
        engine._last_success_sequence = observation.success_sequence

    engine._previous_successful_request_view = None
    engine._cache_baseline_state = "unknown"
    selected = engine.select_context(messages)

    assert selected is not None
    assert all(
        engine._store.get_delta(delta_id).state == DeltaState.COMPRESSED
        for delta_id in delta_ids
    )
    capacity = engine._store.projection_decisions("conv-a")[-1]
    assert capacity["decision_kind"] == "flush"
    assert capacity["decision_mode"] == "capacity"
    assert capacity["pending_bucket_count"] == 3
    assert capacity["pending_count_over"] is True
    assert capacity["pending_tokens_over"] is False
    assert capacity["baseline_state"] == "unknown"
    assert capacity["amortized_crossed"] is False
    assert capacity["member_delta_ids"] == delta_ids
    status = engine.get_status()
    assert status["object_context_version"] == "1.2"
    assert status["effective_scheduler"] == "amortized_batch"
    assert status["hot_tail_max_inferences"] == 1
    assert status["hot_tail_max_tokens"] == 100_000
    assert status["pending_max_inferences"] == 2
    assert status["pending_max_tokens"] == 200_000
    assert status["pending_delta_count"] == 0
    assert status["capacity_projection_count"] == 1


def test_v12_fixed_policy_publishes_oldest_n_and_leaves_remainder_pending(
    tmp_path,
):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        batch_policy="fixed",
        fixed_batch_size=2,
        card_summary_enabled=False,
        hot_tail_max_inferences=2,
        hot_tail_max_tokens=1_000_000,
    )
    engine.context_length = 10_000_000
    messages = []
    delta_ids = []
    for index in range(5):
        message = {
            "role": "user",
            "content": _large_json().replace(
                "field_0", f"fixed_field_{index}_0", 1
            ),
            "timestamp": float(index + 1),
        }
        delta_id = f"turn-fixed-{index}:user:0"
        messages.append(message)
        delta_ids.append(delta_id)
        engine.on_delta_committed(
            _delta(delta_id, "user", f"turn-fixed-{index}", 0, [message])
        )
        observation = engine._store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=str(uuid.uuid4()),
            raw_delta_ids=(delta_id,),
            min_raw_exposures=1,
        )
        engine._last_success_sequence = observation.success_sequence

    engine._cache_baseline_state = "unknown"
    engine._previous_successful_request_view = None
    selected = engine.select_context(messages)

    assert selected is not None
    assert [
        engine._store.get_delta(delta_id).state for delta_id in delta_ids
    ] == [
        DeltaState.COMPRESSED,
        DeltaState.COMPRESSED,
        DeltaState.COMPRESSION_ELIGIBLE,
        DeltaState.COMPRESSION_ELIGIBLE,
        DeltaState.HOT,
    ]
    remaining = engine._store.list_pending_ledgers("conv-a")
    assert [ledger.delta_id for ledger in remaining] == delta_ids[2:4]
    fixed = engine._store.projection_decisions("conv-a")[-1]
    assert fixed["decision_kind"] == "flush"
    assert fixed["decision_mode"] == "fixed"
    assert fixed["decision_reason"] == "FLUSH_FIXED_BATCH_SIZE"
    assert fixed["batch_policy"] == "fixed"
    assert fixed["fixed_batch_size"] == 2
    assert fixed["member_delta_ids"] == delta_ids[:2]
    assert fixed["pending_delta_count"] == 4
    assert fixed["amortized_crossed"] is False
    status = engine.get_status()
    assert status["batch_policy"] == "fixed"
    assert status["fixed_batch_size"] == 2
    assert status["last_compressed_batch_size"] == 2
    assert status["fixed_projection_count"] == 1
    assert status["pending_delta_count"] == 2


def test_v12_unknown_prefix_waits_for_normal_crossing_baseline(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    raw = {"role": "user", "content": _large_json(), "timestamp": 1.0}
    delta_id = "turn-unknown:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-unknown", 0, [raw])
    )
    observation = engine._store.confirm_successful_request_observation(
        conversation_id="conv-a",
        request_attempt_id=str(uuid.uuid4()),
        raw_delta_ids=(delta_id,),
        min_raw_exposures=1,
    )
    engine._last_success_sequence = observation.success_sequence
    engine._cache_baseline_state = "unknown"
    engine._previous_successful_request_view = None

    assert engine.select_context([raw]) is None

    assert engine._store.get_delta(delta_id).state == DeltaState.COMPRESSION_ELIGIBLE
    wait = engine._store.projection_decisions("conv-a")[-1]
    assert wait["decision_kind"] == "wait"
    assert wait["decision_mode"] == "amortized"
    assert wait["decision_reason"] == "WAIT_NO_BASELINE"
    assert wait["baseline_state"] == "unknown"
    assert wait["amortized_crossed"] is False


def test_v12_local_capacity_commit_failure_keeps_raw_ledgers(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    messages = []
    delta_ids = []
    for index in range(3):
        message = {
            "role": "user",
            "content": _large_json().replace("field_1", f"field_{index}_1", 1),
            "timestamp": float(index + 1),
        }
        delta_id = f"turn-capacity-fail-{index}:user:0"
        messages.append(message)
        delta_ids.append(delta_id)
        engine.on_delta_committed(
            _delta(
                delta_id,
                "user",
                f"turn-capacity-fail-{index}",
                0,
                [message],
            )
        )
        observation = engine._store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=str(uuid.uuid4()),
            raw_delta_ids=(delta_id,),
            min_raw_exposures=1,
        )
        engine._last_success_sequence = observation.success_sequence

    engine._previous_successful_request_view = None
    engine._cache_baseline_state = "unknown"
    with patch.object(
        engine._store,
        "publish_compressed_batch",
        side_effect=RuntimeError("commit failed"),
    ):
        assert engine.select_context(messages) is None

    assert all(
        engine._store.get_delta(delta_id).compressed_view is None
        for delta_id in delta_ids
    )
    assert {
        ledger.delta_id
        for ledger in engine._store.list_pending_ledgers("conv-a")
    } == set(delta_ids)
    failure = engine._store.projection_decisions("conv-a")[-1]
    assert failure["decision_kind"] == "wait"
    assert failure["decision_mode"] == "capacity"
    assert failure["decision_reason"] == "PROJECTION_FAILED_RAW_FALLBACK"

    # The fallback Q0 was the request actually accepted, so exactly that
    # immutable Pending snapshot accrues after the successful callback.
    engine.update_from_response({})
    assert all(
        ledger.wait_area_token_requests == ledger.gain_tokens
        for ledger in engine._store.list_pending_ledgers("conv-a")
    )


def test_v12_losing_concurrent_planner_rerenders_winning_cards(tmp_path):
    engine = _started_engine(
        tmp_path,
        scheduler="amortized_batch",
        card_summary_enabled=False,
        hot_tail_max_inferences=1,
        hot_tail_max_tokens=100_000,
    )
    messages = []
    delta_ids = []
    for index in range(3):
        message = {
            "role": "user",
            "content": _large_json().replace("field_2", f"field_{index}_2", 1),
            "timestamp": float(index + 1),
        }
        delta_id = f"turn-concurrent-{index}:user:0"
        messages.append(message)
        delta_ids.append(delta_id)
        engine.on_delta_committed(
            _delta(delta_id, "user", f"turn-concurrent-{index}", 0, [message])
        )
        observed = engine._store.confirm_successful_request_observation(
            conversation_id="conv-a",
            request_attempt_id=str(uuid.uuid4()),
            raw_delta_ids=(delta_id,),
            min_raw_exposures=1,
        )
        engine._last_success_sequence = observed.success_sequence

    engine._previous_successful_request_view = None
    engine._cache_baseline_state = "unknown"
    competing_store = type(engine._store)(engine._store.path)

    def competing_winner(batch, **kwargs):
        competing_store.publish_compressed_batch(batch, **kwargs)
        raise RuntimeError("losing planner observed a committed epoch")

    with patch.object(
        engine._store,
        "publish_compressed_batch",
        side_effect=competing_winner,
    ):
        selected = engine.select_context(messages)

    assert selected is not None
    assert all(
        engine._store.get_delta(delta_id).state == DeltaState.COMPRESSED
        for delta_id in delta_ids
    )
    assert all(
        raw_message["content"] not in selected_message["content"]
        for raw_message, selected_message in zip(messages, selected, strict=True)
    )
    decisions = engine._store.projection_decisions("conv-a")
    assert len(decisions) == 1
    assert decisions[0]["decision_kind"] == "flush"
    assert engine._last_failure == "amortized_concurrent_projection_adopted"


def test_exact_rerender_below_threshold_keeps_candidate_reconsiderable(
    tmp_path, monkeypatch
):
    engine = _started_engine(tmp_path, card_summary_enabled=False)
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    delta_id = "turn-1:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-1", 0, [user]))
    assert engine.select_context([user]) is None
    engine.update_from_response({})
    monkeypatch.setattr(
        engine,
        "_render_prepared_batch",
        lambda baseline, _batch: list(baseline),
    )

    assert engine.select_context([user]) is None

    delta = engine._store.get_delta(delta_id)
    decision = engine._store.projection_decisions("conv-a")[-1]
    assert delta.state == DeltaState.HOT
    assert delta.compressed_view is None
    assert decision["decision_kind"] == "wait"
    assert decision["decision_reason"] == "WAIT_BELOW_THRESHOLD"
    assert decision["gross_tokens_removed"] == 0


def test_summary_disabled_skips_generator_and_keeps_retrievable_origin_card(
    tmp_path,
):
    engine = _started_engine(tmp_path, card_summary_enabled=False)

    with patch.object(
        engine._summary_generator,
        "generate",
        side_effect=AssertionError("summary generator must not run"),
    ):
        raw, _, selected, object_ref = _prepare_user_card(engine)

    payload = parse_card_text(selected[0]["content"])
    record = engine._store.get_object("conv-a", object_ref)
    status = engine.get_status()

    assert payload["schema_version"] == "1.1"
    assert "summary" not in payload
    assert payload["origin"] == {"role": "user"}
    assert record.summary == ""
    assert status["card_summary_enabled"] is False
    assert status["metric_totals"].get("card_summary_attempts", 0) == 0

    engine._active_turn_id = "turn-2"
    retrieved = json.loads(
        engine.handle_tool_call(
            "retrieve_object",
            {"object_ref": object_ref, "reason": "Inspect exact data."},
        )
    )
    assert retrieved["retrieved_object"]["content"] == raw


def test_select_context_records_content_free_projection_identity_and_latency(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, _ = _prepare_user_card(engine)
    engine.select_context(history)

    timeline = engine.get_projection_timeline()

    assert timeline["schema_version"] == 4
    assert timeline["conversation_id"] == "conv-a"
    assert timeline["session_id"] == "session-a"
    assert len(timeline["projections"]) == 3
    assert timeline["cache_requests"] == []
    assert timeline["economic_decisions"]
    assert len(timeline["request_observations"]) == 1
    [observation] = timeline["request_observations"]
    assert observation["outcome"] == "confirmed_success"
    assert observation["success_sequence"] == 1
    assert observation["raw_delta_count"] == 1
    assert re.fullmatch(r"[a-f0-9]{64}", observation["route_namespace_hash"])
    assert observation["request_attempt_id"] in {
        decision["request_attempt_id"]
        for decision in timeline["economic_decisions"]
    }
    assert not {"prompt", "message", "content", "raw_delta_ids"} & set(
        observation
    )
    assert all(
        not {"prompt", "message", "content", "card_text"} & set(decision)
        for decision in timeline["economic_decisions"]
    )
    assert [event["projection_sequence"] for event in timeline["projections"]] == [
        1,
        2,
        3,
    ]
    assert {event["turn_id"] for event in timeline["projections"]} == {
        "turn-1",
        "turn-2",
    }
    assert len(
        {event["projection_id"] for event in timeline["projections"]}
    ) == 3
    for event in timeline["projections"]:
        metrics = event["metrics"]
        assert metrics["tokens_saved"] == (
            metrics["raw_context_tokens"] - metrics["rendered_context_tokens"]
        )
        assert metrics["conversation_tokens_saved"] == (
            metrics["raw_conversation_tokens"]
            - metrics["rendered_conversation_tokens"]
        )
        assert metrics["projection_latency_ms"] >= 0
        assert event["legacy"] is False
    assert raw not in json.dumps(timeline)


def test_conversation_only_projection_metrics_exclude_system_prompt(tmp_path):
    engine = _started_engine(tmp_path)
    _, history, _, _ = _prepare_user_card(engine)
    system = {
        "role": "system",
        "content": "stable system instructions " * 2_000,
    }

    selected = engine.select_context(
        [system, *deepcopy(history)], conversation_messages=history
    )
    metrics = engine.get_projection_timeline()["projections"][-1]["metrics"]

    assert selected is not None
    assert metrics["raw_conversation_tokens"] == (
        estimate_messages_tokens_rough(history)
    )
    assert metrics["rendered_conversation_tokens"] == (
        estimate_messages_tokens_rough(selected[1:])
    )
    assert metrics["conversation_tokens_saved"] == (
        metrics["raw_conversation_tokens"]
        - metrics["rendered_conversation_tokens"]
    )
    assert metrics["raw_context_tokens"] > metrics["raw_conversation_tokens"]
    assert metrics["conversation_compression_ratio"] > metrics[
        "compression_ratio"
    ]


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


def test_request_planner_publishes_multiple_seen_deltas_in_one_epoch(tmp_path):
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

    request = [*raw_messages, newest]
    assert engine.select_context(request) is None
    engine.update_from_response({})
    first = engine.select_context(request)

    assert engine._last_batch_size == 2
    assert engine.compression_count == 2
    assert all(
        engine._store.get_delta(f"turn-{index}:user:0").state == DeltaState.COMPRESSED
        for index in range(1, 3)
    )
    epochs = {
        engine._store.get_delta(f"turn-{index}:user:0").projection_epoch_id
        for index in range(1, 3)
    }
    assert len(epochs) == 1
    assert next(iter(epochs)).startswith("epoch_")
    decisions = engine._store.projection_decisions("conv-a")
    flush = decisions[-1]
    assert flush["projection_epoch_id"] == next(iter(epochs))
    assert flush["decision_kind"] == "flush"
    assert flush["decision_mode"] == "normal"
    assert flush["decision_reason"] == "FLUSH_NET_POSITIVE"
    assert flush["member_delta_ids"] == [
        "turn-1:user:0",
        "turn-2:user:0",
    ]
    assert flush["gross_tokens_removed"] > 0
    assert flush["card_or_receipt_tokens"] > 0
    assert flush["net_saving_equivalent_tokens"] >= 1
    assert not {
        "prompt",
        "message",
        "content",
        "card_text",
        "retrieved_payload",
    } & set(flush)
    second = engine.select_context(request)
    assert first == second
    assert json.dumps(first, ensure_ascii=False).count("<OBJECT_CARD>") == 2


@pytest.mark.parametrize("scheduler", ["economic", "amortized_batch"])
def test_emergency_flush_is_separate_and_may_have_negative_normal_value(
    tmp_path, scheduler
):
    engine = _started_engine(
        tmp_path,
        scheduler=scheduler,
        card_summary_enabled=False,
        economic_min_net_saving_tokens=1_000_000,
        economic_cache_read_ratio_fallback=0.0,
        economic_cache_write_ratio_fallback=1.0,
        emergency_context_ratio=0.90,
    )
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    delta_id = "turn-1:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-1", 0, [user]))
    baseline_tokens = estimate_messages_tokens_rough([user])

    assert engine.select_context([user]) is None
    engine.update_from_response({})
    engine.context_length = max(1, int(baseline_tokens / 0.90))

    selected = engine.select_context([user])

    assert selected is not None
    assert raw not in selected[0]["content"]
    projected = engine._store.get_delta(delta_id)
    assert projected.state == DeltaState.COMPRESSED
    decisions = engine._store.projection_decisions("conv-a")
    emergency = decisions[-1]
    assert emergency["projection_epoch_id"] == projected.projection_epoch_id
    assert emergency["decision_kind"] == "emergency"
    assert emergency["decision_mode"] == "emergency"
    assert emergency["decision_reason"] == "EMERGENCY_FLUSH"
    assert emergency["net_saving_equivalent_tokens"] < 0
    if scheduler == "amortized_batch":
        assert emergency["policy_version"] == "1.2"
        assert emergency["emergency_triggered"] is True
        assert emergency["pending_delta_count"] == 0
        assert emergency["wait_loss_projected"] == 0
    status = engine.get_status()
    assert status["normal_projection_count"] == 0
    assert status["emergency_projection_count"] == 1


@pytest.mark.parametrize("scheduler", ["economic", "amortized_batch"])
def test_emergency_pressure_keeps_raw_unseen_delta_visible(tmp_path, scheduler):
    engine = _started_engine(
        tmp_path,
        scheduler=scheduler,
        card_summary_enabled=False,
        emergency_context_ratio=0.90,
    )
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    delta_id = "turn-1:user:0"
    engine.on_delta_committed(_delta(delta_id, "user", "turn-1", 0, [user]))
    engine.context_length = 10

    assert engine.select_context([user]) is None

    delta = engine._store.get_delta(delta_id)
    [decision] = engine._store.projection_decisions("conv-a")
    assert delta.state == DeltaState.HOT
    assert delta.raw_seen_count == 0
    assert decision["decision_kind"] == "wait"
    assert decision["decision_mode"] == "emergency"
    assert decision["decision_reason"] == "WAIT_RAW_UNSEEN"
    assert decision["member_delta_ids"] == []
    if scheduler == "amortized_batch":
        assert decision["policy_version"] == "1.2"
        assert decision["emergency_triggered"] is True
        assert decision["hot_underexposed_count"] == 1
        assert decision["pending_delta_count"] == 0


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


def test_outer_whitespace_transport_alignment_preserves_sidecar_and_spans(tmp_path):
    engine = _started_engine(tmp_path, card_summary_enabled=False)
    core = _large_json()
    raw = f"  \n{core}\n  "
    durable = {"role": "user", "content": raw, "timestamp": 1.0}
    transport = {
        "role": "user",
        "content": core + "\n\n<ephemeral-memory>note</ephemeral-memory>",
        "timestamp": 1.0,
    }
    delta_id = "turn-trim:user:0"
    engine.on_delta_committed(
        _delta(delta_id, "user", "turn-trim", 0, [durable])
    )

    # The first accepted request carries the durable payload after the host
    # removes only its outer whitespace.  That still counts as Raw exposure.
    assert engine.select_context([transport]) is None
    engine.update_from_response({})
    assert engine._store.get_delta(delta_id).raw_seen_count == 1

    selected = engine.select_context([transport])

    assert selected is not None
    assert core not in selected[0]["content"]
    assert "<OBJECT_CARD>" in selected[0]["content"]
    assert selected[0]["content"].endswith(
        "<ephemeral-memory>note</ephemeral-memory>"
    )


def test_ambiguous_nonprefix_trimmed_alignment_fails_closed():
    core = '{"value": 1}'
    raw = {"role": "user", "content": f"  {core}  ", "timestamp": 1.0}
    request = {
        "role": "user",
        "content": f"prefix {core} duplicate {core}",
        "timestamp": 1.0,
    }

    assert ObjectContextEngine._raw_message_present(request, raw) is False


def test_multimodal_alignment_is_exact_except_for_trailing_text_sidecars():
    raw = {
        "role": "user",
        "timestamp": 1.0,
        "content": [
            {"type": "text", "text": "inspect this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
            {
                "type": "file",
                "name": "report.csv",
                "mime_type": "text/csv",
                "file_content": "a,b\n1,2",
            },
        ],
    }

    with_guidance = deepcopy(raw)
    with_guidance["content"].append({
        "type": "text",
        "text": "\n\nrequest-only MoA guidance",
    })
    assert ObjectContextEngine._raw_message_present(with_guidance, raw) is True

    changed_image = deepcopy(raw)
    changed_image["content"][1]["image_url"]["url"] = "data:image/png;base64,BBBB"
    assert ObjectContextEngine._raw_message_present(changed_image, raw) is False

    changed_file = deepcopy(raw)
    changed_file["content"][2]["name"] = "other.csv"
    assert ObjectContextEngine._raw_message_present(changed_file, raw) is False

    non_text_sidecar = deepcopy(raw)
    non_text_sidecar["content"].append({
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,CCCC"},
    })
    assert ObjectContextEngine._raw_message_present(non_text_sidecar, raw) is False


def test_exact_retrieval_is_raw_once_then_becomes_economic_receipt(tmp_path):
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
    engine.update_from_response({})

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
    receipt = json.loads(
        later[-2]["content"]
        .removeprefix("<RETRIEVAL_CARD>\n")
        .removesuffix("\n</RETRIEVAL_CARD>")
    )
    assert receipt == {
        "action": "retrieved",
        "object_ref": object_ref,
        "schema_version": "1.1",
        "status": "success",
    }
    assert object_ref in later[0]["content"]
    assert engine._store.get_object("conv-a", object_ref).content == raw


def test_turn_completion_alone_does_not_force_retrieval_receipt(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    assistant, tool = _retrieval_messages(
        engine,
        object_ref=object_ref,
        call_id="call-unseen-retrieval",
        reason="Inspect before turn completion.",
        timestamp=4.0,
    )
    delta_id = "turn-2:inference:unseen-retrieval"
    engine.on_delta_committed(
        _delta(delta_id, "inference", "turn-2", 1, [assistant, tool])
    )
    trace = [*history, assistant, tool]

    engine.on_turn_complete(trace, turn_id="turn-2", interrupted=False)
    projected = engine.build_context(trace)

    assert "<RETRIEVAL_CARD>" not in projected[-1]["content"]
    assert json.loads(projected[-1]["content"])["retrieved_object"]["content"] == raw
    delta = engine._store.get_delta(delta_id)
    assert delta.state == DeltaState.HOT
    assert delta.raw_seen_count == 0
    assert engine._store.list_leases("conv-a", "turn-2") == []


def test_repeated_retrievals_are_independent_deltas_for_the_same_object(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    object_count = len(engine._store.list_objects("conv-a"))

    assistant1, tool1 = _retrieval_messages(
        engine,
        object_ref=object_ref,
        call_id="call-repeat-1",
        reason="First exact inspection.",
        timestamp=4.0,
    )
    delta1 = "turn-2:inference:repeat-1"
    engine.on_delta_committed(
        _delta(delta1, "inference", "turn-2", 1, [assistant1, tool1])
    )
    trace1 = [*history, assistant1, tool1]
    raw_request1 = engine.select_context(trace1)
    assert json.loads(raw_request1[-1]["content"])["retrieved_object"]["content"] == raw
    engine.update_from_response({})
    engine.on_turn_complete(trace1, turn_id="turn-2", interrupted=False)

    user3 = {"role": "user", "content": "Inspect it again.", "timestamp": 6.0}
    engine.on_delta_committed(_delta("turn-3:user:0", "user", "turn-3", 0, [user3]))
    trace_with_first = [*trace1, user3]
    first_receipt_request = engine.select_context(trace_with_first)
    assert "<RETRIEVAL_CARD>" in first_receipt_request[-2]["content"]
    engine.update_from_response({})

    assistant2, tool2 = _retrieval_messages(
        engine,
        object_ref=object_ref,
        call_id="call-repeat-2",
        reason="Second exact inspection.",
        timestamp=7.0,
    )
    delta2 = "turn-3:inference:repeat-2"
    engine.on_delta_committed(
        _delta(delta2, "inference", "turn-3", 1, [assistant2, tool2])
    )
    trace2 = [*trace_with_first, assistant2, tool2]
    raw_request2 = engine.select_context(trace2)
    assert "<RETRIEVAL_CARD>" in raw_request2[-4]["content"]
    assert json.loads(raw_request2[-1]["content"])["retrieved_object"]["content"] == raw
    engine.update_from_response({})
    engine.on_turn_complete(trace2, turn_id="turn-3", interrupted=False)

    user4 = {"role": "user", "content": "Continue.", "timestamp": 9.0}
    engine.on_delta_committed(_delta("turn-4:user:0", "user", "turn-4", 0, [user4]))
    final_request = engine.select_context([*trace2, user4])
    receipts = [
        message
        for message in final_request
        if message.get("role") == "tool"
        and message.get("name") == "retrieve_object"
    ]

    assert len(receipts) == 2
    assert all("<RETRIEVAL_CARD>" in message["content"] for message in receipts)
    projected1 = engine._store.get_delta(delta1)
    projected2 = engine._store.get_delta(delta2)
    assert projected1.state == projected2.state == DeltaState.COMPRESSED
    assert projected1.raw_seen_count == projected2.raw_seen_count == 1
    assert projected1.projection_epoch_id != projected2.projection_epoch_id
    assert len(engine._store.list_objects("conv-a")) == object_count
    assert engine._store.retrieval_count_for_ref("conv-a", object_ref) == 2


def test_failed_retrieval_delta_never_becomes_success_receipt(tmp_path):
    engine = _started_engine(tmp_path)
    _, history, _, _ = _prepare_user_card(engine)
    call_id = "call-failed-retrieval"
    bad_ref = "object://obj_000000000000000000000000@v1"
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": "retrieve_object",
                    "arguments": json.dumps({
                        "object_ref": bad_ref,
                        "reason": "This must fail.",
                    }),
                },
            }
        ],
        "timestamp": 4.0,
    }
    error = engine.handle_tool_call(
        "retrieve_object",
        {"object_ref": bad_ref, "reason": "This must fail."},
        tool_call_id=call_id,
    )
    tool = {
        "role": "tool",
        "name": "retrieve_object",
        "tool_name": "retrieve_object",
        "tool_call_id": call_id,
        "content": error,
        "timestamp": 5.0,
    }
    delta_id = "turn-2:inference:failed-retrieval"
    engine.on_delta_committed(
        _delta(delta_id, "inference", "turn-2", 1, [assistant, tool])
    )
    trace = [*history, assistant, tool]
    engine.select_context(trace)
    engine.update_from_response({})
    engine.on_turn_complete(trace, turn_id="turn-2", interrupted=False)
    user3 = {"role": "user", "content": "Continue.", "timestamp": 6.0}
    engine.on_delta_committed(_delta("turn-3:user:0", "user", "turn-3", 0, [user3]))
    later = engine.select_context([*trace, user3])

    assert json.loads(later[-2]["content"])["retrieval_error"]["code"] == "OBJECT_NOT_FOUND"
    assert "<RETRIEVAL_CARD>" not in later[-2]["content"]
    assert engine._store.retrieval_event_for_tool_call("conv-a", call_id) is None
    assert engine._store.get_delta(delta_id).state == DeltaState.HOT


def test_retrieval_receipt_survives_restart_and_cold_archive_resolution(tmp_path):
    engine = _started_engine(tmp_path)
    raw, history, _, object_ref = _prepare_user_card(engine)
    assistant, tool = _retrieval_messages(
        engine,
        object_ref=object_ref,
        call_id="call-restart-retrieval",
        reason="Persist the receipt.",
        timestamp=4.0,
    )
    delta_id = "turn-2:inference:restart-retrieval"
    engine.on_delta_committed(
        _delta(delta_id, "inference", "turn-2", 1, [assistant, tool])
    )
    trace = [*history, assistant, tool]
    engine.select_context(trace)
    engine.update_from_response({})
    engine.on_turn_complete(trace, turn_id="turn-2", interrupted=False)
    user3 = {"role": "user", "content": "Continue.", "timestamp": 6.0}
    engine.on_delta_committed(_delta("turn-3:user:0", "user", "turn-3", 0, [user3]))
    trace_next = [*trace, user3]
    projected = engine.select_context(trace_next)
    receipt_bytes = projected[-2]["content"]
    assert "<RETRIEVAL_CARD>" in receipt_bytes

    resumed = _started_engine(tmp_path)
    resumed_projection = resumed.build_context(trace_next)
    assert resumed_projection[-2]["content"] == receipt_bytes
    assert resumed._store.get_delta(delta_id).projection_epoch_id

    resumed._store.update_activity(
        conversation_id="conv-a",
        current_delta=100,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=0,
    )
    resumed._store.update_activity(
        conversation_id="conv-a",
        current_delta=101,
        active_refs=set(),
        recent_access_deltas=0,
        grace_deltas=0,
    )
    assert resumed._store.archive_evictable("conv-a") == [object_ref]
    assert resumed.resolve_object(object_ref) == "cold_archive"

    resumed._active_turn_id = "turn-cold-reload"
    reloaded = json.loads(
        resumed.handle_tool_call(
            "retrieve_object",
            {"object_ref": object_ref, "reason": "Reload from Cold Archive."},
            tool_call_id="call-cold-reload",
        )
    )
    assert reloaded["retrieved_object"]["content"] == raw
    assert resumed.resolve_object(object_ref) == "working_memory"


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
                "function": {
                    "name": "terminal",
                    "arguments": json.dumps(
                        {
                            "command": "python check.py --verbose",
                            "workdir": "/tmp/project",
                        }
                    ),
                },
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
    assert engine.select_context([user1]) is None
    engine.update_from_response({})
    engine.on_delta_committed(
        _delta("turn-1:inference:1", "inference", "turn-1", 1, [assistant, tool])
    )
    assert engine.select_context([user1, assistant, tool]) is None
    engine.update_from_response({})
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
    card = parse_card_text(projected[2]["content"])
    assert card["origin"] == {
        "operation": "execute",
        "role": "tool",
        "target": "/tmp/project",
        "tool": "terminal",
    }
    assert "python check.py" not in projected[2]["content"]


def test_seen_tool_result_can_project_inside_the_same_active_turn(tmp_path):
    engine = _started_engine(tmp_path, card_summary_enabled=False)
    user = {"role": "user", "content": "Run a long check.", "timestamp": 1.0}
    call_id = "call-active-loop"
    assistant = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "terminal", "arguments": "{}"},
            }
        ],
        "timestamp": 2.0,
    }
    output = "".join(
        f"INFO active-step={index} payload={'x' * 48}\n" for index in range(600)
    )
    tool = {
        "role": "tool",
        "name": "terminal",
        "tool_name": "terminal",
        "tool_call_id": call_id,
        "content": output,
        "timestamp": 3.0,
    }
    delta_id = "turn-1:inference:1"
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
    assert engine.select_context([user]) is None
    engine.update_from_response({})
    engine.on_delta_committed(
        _delta(delta_id, "inference", "turn-1", 1, [assistant, tool])
    )
    active_trace = [user, assistant, tool]
    assert engine.select_context(active_trace) is None
    engine.update_from_response({})
    assert engine._active_turn_id == "turn-1"

    projected = engine.select_context(active_trace)

    assert projected is not None
    assert projected[1] == assistant
    assert projected[2]["tool_call_id"] == call_id
    assert "<OBJECT_CARD>" in projected[2]["content"]
    assert output not in projected[2]["content"]
    assert engine._store.get_delta(delta_id).state == DeltaState.COMPRESSED


@pytest.mark.parametrize("failure_target", ["extract_structure", "build_card"])
def test_prepare_failure_keeps_raw_and_marks_delta_failed(tmp_path, failure_target):
    engine = _started_engine(tmp_path)
    raw = _large_json()
    user = {"role": "user", "content": raw, "timestamp": 1.0}
    assistant = {"role": "assistant", "content": "Done.", "timestamp": 2.0}
    next_user = {"role": "user", "content": "Continue.", "timestamp": 3.0}
    engine.on_delta_committed(_delta("turn-1:user:0", "user", "turn-1", 0, [user]))
    assert engine.select_context([user]) is None
    engine.update_from_response({})
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
    assert (
        json.loads(failures[-1]["metadata_json"])["stage"]
        == "economic_candidate_prepare"
    )
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
    assert healthy.select_context([user]) is None
    healthy.update_from_response({})
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
        assert healthy.select_context([user, assistant, next_user]) is None
    failed = healthy._store.get_delta("turn-1:user:0")
    assert failed.state == DeltaState.COMPRESSION_FAILED
    assert failed.compressed_view is None
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
        "enabled",
        "scheduler",
        "hot_tail_max_inferences",
        "hot_tail_max_tokens",
            "amortized_cache_read_weight",
            "batch_policy",
            "fixed_batch_size",
        "min_raw_exposures",
        "economic_min_net_saving_tokens",
        "economic_min_net_saving_usd",
        "economic_cache_read_ratio_fallback",
        "economic_cache_write_ratio_fallback",
        "emergency_context_ratio",
        "object_prefilter_min_tokens",
        "min_absolute_saving_tokens",
        "min_relative_saving_ratio",
        "card_summary_enabled",
        "summary_max_tokens",
        "wm_grace_deltas",
        "recent_retrieval_active_deltas",
        "retrieval_max_tokens_ratio",
    }
    engine = ObjectContextEngine(
        config=DEFAULT_CONFIG, summary_generator=_DeterministicSummary()
    )
    assert engine.scheduler == "economic"
    assert engine.hot_tail_max_inferences == 4
    assert engine.hot_tail_max_tokens == 12_800
    assert engine.pending_max_inferences == 8
    assert engine.pending_max_tokens == 25_600
    assert engine.amortized_cache_read_weight == 0.10
    assert engine.min_raw_exposures == 1
    assert engine.economic_min_net_saving_tokens == 1000
    assert engine.economic_min_net_saving_usd is None
    assert engine.emergency_context_ratio == 0.90
    assert engine.card_summary_enabled is configured["card_summary_enabled"]
    assert engine.card_summary_enabled is False
    assert engine.summary_max_tokens == configured["summary_max_tokens"]


def test_v11_config_clamps_raw_exposure_and_normalizes_scheduler(tmp_path):
    engine = _started_engine(
        tmp_path,
        min_raw_exposures=0,
        scheduler="fixed",
        economic_min_net_saving_usd=0.25,
        emergency_context_ratio=0.95,
    )

    assert engine.min_raw_exposures == 1
    assert engine.scheduler == "economic"
    assert engine.economic_min_net_saving_usd == 0.25
    assert engine.emergency_context_ratio == 0.95


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
    engine.update_from_response(
        {
            "prompt_tokens": 1_000,
            "completion_tokens": 100,
            "total_tokens": 1_100,
            "input_tokens": 200,
            "output_tokens": 100,
            "cache_read_tokens": 750,
            "cache_write_tokens": 50,
        }
    )
    # Missing provider usage is a compression-verdict signal, not a measured
    # zero-hit request.
    engine.update_from_response({})
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
        "raw_conversation_tokens",
        "rendered_conversation_tokens",
        "conversation_tokens_saved",
        "conversation_compression_ratio",
        "hot_tail_tokens",
        "card_tokens",
        "card_summary_attempts",
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
        "prompt_tokens",
        "uncached_input_tokens",
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
    assert status["metric_totals"]["card_summary_attempts"] >= 1
    assert status["request_projection_count"] == 2
    assert status["request_metric_totals"]["tokens_saved"] > 0
    assert status["last_request_metrics"]["raw_context_tokens"] > (
        status["last_request_metrics"]["rendered_context_tokens"]
    )
    cache_timeline = engine.get_projection_timeline()["cache_requests"]
    assert len(cache_timeline) == 1
    assert cache_timeline[0]["turn_id"] == "turn-2"
    assert cache_timeline[0]["metrics"] == {
        "prompt_tokens": 1_000.0,
        "uncached_input_tokens": 200.0,
        "cache_read_tokens": 750.0,
        "cache_write_tokens": 50.0,
        "prompt_cache_hit_ratio": 0.75,
    }
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
    assert live["request_projection_count"] == 2

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
