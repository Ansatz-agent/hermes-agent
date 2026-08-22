from plugins.context_engine.object_context.models import DeltaRecord, DeltaState
from plugins.context_engine.object_context.state import recompute_hot_tail


def _delta(sequence, *, turn="old", tokens=100, state=DeltaState.HOT):
    return DeltaRecord(
        delta_id=f"delta-{sequence}",
        conversation_id="conv",
        session_id="session",
        turn_id=turn,
        kind="inference",
        inference_id=f"inference-{sequence}",
        turn_sequence=sequence,
        global_sequence=sequence,
        raw_token_count=tokens,
        state=state,
        raw_view=({"role": "assistant", "content": str(sequence)},),
    )


def test_count_and_token_budget_select_from_newest_backwards():
    decision = recompute_hot_tail(
        [_delta(1), _delta(2), _delta(3), _delta(4)],
        active_turn_id="",
        max_deltas=3,
        token_budget=220,
        current_prompt_tokens=0,
        context_soft_limit=750,
    )
    assert decision.hot_delta_ids == ("delta-3", "delta-4")
    assert decision.newly_cold_delta_ids == ("delta-1", "delta-2")
    assert decision.hot_tokens == 200


def test_active_reasoning_chain_is_preserved_even_over_budget():
    decision = recompute_hot_tail(
        [
            _delta(1, turn="prior"),
            _delta(2, turn="live", tokens=500),
            _delta(3, turn="live", tokens=500),
        ],
        active_turn_id="live",
        max_deltas=1,
        token_budget=100,
        current_prompt_tokens=0,
        context_soft_limit=750,
    )
    assert decision.hot_delta_ids == ("delta-2", "delta-3")
    assert decision.newly_cold_delta_ids == ("delta-1",)


def test_soft_pressure_shrinks_only_non_active_hot_tail():
    decision = recompute_hot_tail(
        [_delta(i, tokens=40) for i in range(1, 7)],
        active_turn_id="",
        max_deltas=6,
        token_budget=1000,
        current_prompt_tokens=800,
        context_soft_limit=750,
    )
    assert decision.pressure_applied is True
    assert decision.hot_delta_ids == ("delta-4", "delta-5", "delta-6")


def test_terminal_states_are_never_reprocessed_as_hot():
    decision = recompute_hot_tail(
        [
            _delta(1, state=DeltaState.COMPRESSED),
            _delta(2, state=DeltaState.COMPRESSION_SKIPPED),
            _delta(3),
        ],
        active_turn_id="",
        max_deltas=1,
        token_budget=100,
        current_prompt_tokens=0,
        context_soft_limit=750,
    )
    assert decision.hot_delta_ids == ("delta-3",)
    assert decision.newly_cold_delta_ids == ()
