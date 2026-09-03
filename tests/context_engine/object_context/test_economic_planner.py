from dataclasses import fields

import pytest

from plugins.context_engine.object_context.planner import (
    EMERGENCY_FLUSH,
    FLUSH_NET_POSITIVE,
    WAIT_BELOW_THRESHOLD,
    WAIT_NO_BASELINE,
    WAIT_RAW_UNSEEN,
    PrefixFacts,
    PreparedCandidate,
    PricingWeights,
    plan_economic_batch,
    plan_emergency_batch,
    resolve_pricing_weights,
    round_reusable_prefix,
    score_exact_batch,
)


def _candidate(
    delta_id,
    sequence,
    *,
    removed,
    offset,
    summary_cost=0.0,
):
    return PreparedCandidate(
        delta_id=delta_id,
        sequence=sequence,
        raw_tokens=removed + 100,
        projected_tokens=100,
        earliest_change_token_offset=offset,
        object_refs=(f"object://{delta_id}",),
        known_summary_cost_equivalent_tokens=summary_cost,
    )


def _prefix(*, prompt=50_000, reusable=0, available=True, granularity=1):
    return PrefixFacts(
        baseline_prompt_tokens=prompt,
        baseline_reusable_prefix_tokens=reusable,
        previous_success_available=available,
        cache_granularity_tokens=granularity,
    )


PRICING = PricingWeights(cache_read=0.10, cache_write=1.00, source="test")


def test_one_huge_positive_delta_forms_a_batch_of_one():
    decision = plan_economic_batch(
        [_candidate("huge", 1, removed=20_000, offset=10_000)],
        prefix=_prefix(reusable=10_000),
        pricing=PRICING,
        minimum_net_saving_tokens=1_000,
    )

    assert decision.decision_kind == "flush"
    assert decision.decision_reason == FLUSH_NET_POSITIVE
    assert decision.member_delta_ids == ("huge",)
    assert decision.winner.net_saving_equivalent_tokens == 20_000


def test_small_candidates_accumulate_without_a_count_trigger():
    decision = plan_economic_batch(
        [
            _candidate("a", 1, removed=400, offset=0),
            _candidate("b", 2, removed=400, offset=0),
            _candidate("c", 3, removed=400, offset=0),
        ],
        prefix=_prefix(),
        pricing=PRICING,
        minimum_net_saving_tokens=1_000,
    )

    assert decision.decision_kind == "flush"
    assert decision.member_delta_ids == ("a", "b", "c")
    assert decision.winner.gross_tokens_removed == 1_200


def test_competitive_scouts_cache_cliff_blocks_gross_positive_projection():
    decision = plan_economic_batch(
        [_candidate("competitive-scouts", 1, removed=4_167, offset=0)],
        prefix=_prefix(prompt=30_000, reusable=25_600),
        pricing=PRICING,
        minimum_net_saving_tokens=1_000,
    )

    score = decision.winner
    assert decision.decision_kind == "wait"
    assert decision.decision_reason == WAIT_BELOW_THRESHOLD
    assert score.gross_tokens_removed == 4_167
    assert score.cache_tokens_invalidated == 25_600
    assert score.cache_penalty_equivalent_tokens == pytest.approx(23_040)
    assert score.net_saving_equivalent_tokens == pytest.approx(-18_873)


def test_no_trustworthy_previous_request_waits_before_scoring():
    decision = plan_economic_batch(
        [_candidate("new", 1, removed=50_000, offset=0)],
        prefix=_prefix(available=False),
        pricing=PRICING,
        minimum_net_saving_tokens=1,
    )

    assert decision.decision_kind == "wait"
    assert decision.decision_reason == WAIT_NO_BASELINE
    assert decision.winner is None


def test_maximum_net_wins_not_earliest_threshold_crossing():
    decision = plan_economic_batch(
        [
            _candidate("tiny-old", 1, removed=2_000, offset=100),
            _candidate("large-late", 2, removed=5_000, offset=9_000),
        ],
        prefix=_prefix(prompt=20_000, reusable=10_000),
        pricing=PRICING,
        minimum_net_saving_tokens=1_000,
    )

    assert decision.decision_kind == "flush"
    assert decision.member_delta_ids == ("large-late",)
    assert decision.winner.net_saving_equivalent_tokens == pytest.approx(4_100)
    assert decision.ranked_batches[1].member_delta_ids == (
        "tiny-old",
        "large-late",
    )


def test_summary_cost_is_zero_by_default_and_explicit_when_known():
    local = plan_economic_batch(
        [_candidate("local", 1, removed=2_000, offset=0)],
        prefix=_prefix(),
        pricing=PRICING,
        minimum_net_saving_tokens=1,
    )
    summarized = plan_economic_batch(
        [_candidate("summary", 1, removed=2_000, offset=0, summary_cost=600)],
        prefix=_prefix(),
        pricing=PRICING,
        minimum_net_saving_tokens=1,
    )

    assert local.winner.known_summary_cost_equivalent_tokens == 0
    assert local.winner.net_saving_equivalent_tokens == 2_000
    assert summarized.winner.known_summary_cost_equivalent_tokens == 600
    assert summarized.winner.net_saving_equivalent_tokens == 1_400


def test_unknown_pricing_uses_conservative_configured_fallback():
    weights = resolve_pricing_weights(
        uncached_input_price=None,
        cache_read_price=None,
        cache_write_price=None,
        fallback_cache_read=0.12,
        fallback_cache_write=1.05,
    )

    assert weights == PricingWeights(
        cache_read=0.12,
        cache_write=1.05,
        source="configured_fallback",
    )


def test_known_prices_become_technical_ratios_and_usd_unit():
    weights = resolve_pricing_weights(
        uncached_input_price=2.0,
        cache_read_price=0.2,
        cache_write_price=2.5,
        source="official",
        version="v1",
    )

    assert weights.cache_read == pytest.approx(0.1)
    assert weights.cache_write == pytest.approx(1.25)
    assert weights.uncached_input_usd_per_token == pytest.approx(0.000002)


def test_cache_granularity_always_rounds_reusable_prefix_down():
    assert round_reusable_prefix(9_999, 1_024) == 9_216
    decision = plan_economic_batch(
        [_candidate("block", 1, removed=2_000, offset=8_999)],
        prefix=_prefix(reusable=9_999, granularity=1_024),
        pricing=PRICING,
        minimum_net_saving_tokens=1,
    )
    assert decision.winner.baseline_reusable_prefix_tokens == 9_216
    assert decision.winner.candidate_reusable_prefix_tokens == 8_192


def test_exact_rescore_uses_rendered_prompt_and_lcp_facts():
    score = score_exact_batch(
        member_delta_ids=("a", "b"),
        member_object_refs=("object://a", "object://b", "object://a"),
        earliest_changed_delta_id="a",
        baseline_prompt_tokens=20_000,
        candidate_prompt_tokens=15_000,
        baseline_reusable_prefix_tokens=12_000,
        candidate_reusable_prefix_tokens=8_000,
        pricing=PRICING,
        known_summary_cost_equivalent_tokens=100,
    )

    assert score.gross_tokens_removed == 5_000
    assert score.cache_tokens_invalidated == 4_000
    assert score.cache_penalty_equivalent_tokens == pytest.approx(3_600)
    assert score.net_saving_equivalent_tokens == pytest.approx(1_300)
    assert score.member_object_refs == ("object://a", "object://b")


def test_online_planner_schema_contains_no_future_or_retrieval_prediction():
    names = {
        field.name
        for record in (PreparedCandidate, PrefixFacts)
        for field in fields(record)
    }
    assert not names & {
        "expected_future_requests",
        "max_payback_requests",
        "retrieval_probability",
        "predicted_retrieval_calls",
    }


def test_emergency_can_flush_negative_value_to_restore_request_viability():
    decision = plan_emergency_batch(
        [_candidate("cache-cliff", 1, removed=4_167, offset=0)],
        prefix=_prefix(prompt=30_000, reusable=25_600, available=False),
        pricing=PRICING,
        target_prompt_tokens=26_000,
    )

    assert decision.decision_kind == "emergency"
    assert decision.decision_reason == EMERGENCY_FLUSH
    assert decision.member_delta_ids == ("cache-cliff",)
    assert decision.winner.candidate_prompt_tokens == 25_833
    assert decision.winner.net_saving_equivalent_tokens == pytest.approx(-18_873)


def test_emergency_chooses_viable_suffix_with_least_cache_damage():
    decision = plan_emergency_batch(
        [
            _candidate("early", 1, removed=3_000, offset=0),
            _candidate("late", 2, removed=5_000, offset=9_000),
        ],
        prefix=_prefix(prompt=20_000, reusable=10_000),
        pricing=PRICING,
        target_prompt_tokens=16_000,
    )

    assert decision.decision_kind == "emergency"
    assert decision.member_delta_ids == ("late",)
    assert decision.winner.candidate_prompt_tokens == 15_000
    assert decision.winner.cache_tokens_invalidated == 1_000


def test_emergency_never_hides_only_raw_unseen_content():
    decision = plan_emergency_batch(
        [],
        prefix=_prefix(prompt=100_000, available=False),
        pricing=PRICING,
        target_prompt_tokens=90_000,
        unseen_candidate_count=1,
    )

    assert decision.decision_kind == "wait"
    assert decision.decision_reason == WAIT_RAW_UNSEEN
    assert decision.winner is None
