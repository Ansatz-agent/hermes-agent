import itertools
import math
import random

import pytest

from plugins.context_engine.object_context.amortized_planner import (
    BATCH_POLICY_FIXED,
    FLUSH_AMORTIZED_CROSSING,
    FLUSH_EMERGENCY,
    FLUSH_FIXED_BATCH_SIZE,
    FLUSH_PENDING_CAPACITY,
    WAIT_BELOW_AMORTIZED_CROSSING,
    WAIT_EMPTY_PENDING,
    WAIT_FIXED_BATCH_INCOMPLETE,
    PendingDelta,
    plan_amortized_flush,
)


def _delta(
    delta_id: str,
    sequence: int,
    *,
    raw: int = 1_000,
    gain: int = 900,
    area: float = 0.0,
    raw_present: bool = True,
    eligible: bool = True,
) -> PendingDelta:
    return PendingDelta(
        delta_id=delta_id,
        success_sequence=sequence,
        raw_tokens=raw,
        gain_tokens=gain,
        wait_area=area,
        raw_present=raw_present,
        eligible=eligible,
    )


def _plan(pending, **overrides):
    kwargs = {
        "cache_read_weight": 0.1,
        "shared_cached_hot_tokens": 10_000,
        "hot_bucket_limit": 4,
        "hot_token_limit": 10_000,
    }
    kwargs.update(overrides)
    return plan_amortized_flush(pending, **kwargs)


def test_empty_pending_waits_even_if_emergency_or_zero_overhead():
    decision = _plan(
        [],
        shared_cached_hot_tokens=0,
        emergency=True,
    )

    assert decision.action == "wait"
    assert not decision.should_flush
    assert decision.reason == WAIT_EMPTY_PENDING
    assert decision.emergency_requested
    assert not decision.amortized_crossed
    assert decision.pending_delta_ids == ()
    assert decision.member_delta_ids == ()


def test_new_pending_entry_uses_projected_increment_before_waiting():
    candidate = _delta("new", 1, gain=900, area=0)

    at_equality = _plan(
        [candidate],
        shared_cached_hot_tokens=100,
    )

    assert at_equality.wait_loss_now == 0
    assert at_equality.wait_loss_increment == pytest.approx(90)
    assert at_equality.wait_loss_projected == pytest.approx(90)
    assert at_equality.shared_overhead == pytest.approx(90)
    assert at_equality.crossing_margin == pytest.approx(0)
    assert at_equality.reason == FLUSH_AMORTIZED_CROSSING


def test_wait_area_is_realized_only_after_a_successful_wait():
    new_entry = _delta("new", 1, gain=900, area=0)
    before_wait = _plan(
        [new_entry],
        shared_cached_hot_tokens=101,
    )
    after_one_successful_wait = _plan(
        [_delta("new", 1, gain=900, area=900)],
        shared_cached_hot_tokens=101,
    )

    assert before_wait.reason == WAIT_BELOW_AMORTIZED_CROSSING
    assert before_wait.wait_loss_projected == pytest.approx(90)
    assert after_one_successful_wait.wait_loss_now == pytest.approx(90)
    assert after_one_successful_wait.wait_loss_increment == pytest.approx(90)
    assert after_one_successful_wait.reason == FLUSH_AMORTIZED_CROSSING


def test_heterogeneous_gains_and_wait_areas_are_summed_before_weighting():
    decision = _plan(
        [
            _delta("old", 2, raw=5_000, gain=4_000, area=12_000),
            _delta("new", 5, raw=800, gain=300, area=600),
        ],
        cache_read_weight=0.25,
        shared_cached_hot_tokens=6_000,
    )

    assert decision.pending_raw_tokens == 5_800
    assert decision.pending_gain_tokens == 4_300
    assert decision.total_wait_area == pytest.approx(12_600)
    assert decision.wait_loss_now == pytest.approx(3_150)
    assert decision.wait_loss_increment == pytest.approx(1_075)
    assert decision.wait_loss_projected == pytest.approx(4_225)
    assert decision.shared_overhead == pytest.approx(4_500)
    assert decision.reason == WAIT_BELOW_AMORTIZED_CROSSING


def test_distinct_success_sequences_are_the_capacity_bucket_unit():
    decision = _plan(
        [
            _delta("a", 1, raw=10, gain=1),
            _delta("b", 1, raw=10, gain=1),
            _delta("c", 2, raw=10, gain=1),
        ],
        hot_bucket_limit=1,
        hot_token_limit=10_000,
        shared_cached_hot_tokens=10_000,
    )

    assert decision.pending_delta_count == 3
    assert decision.pending_bucket_count == 2
    assert not decision.pending_count_over
    assert decision.reason == WAIT_BELOW_AMORTIZED_CROSSING


def test_count_cap_fires_only_when_distinct_buckets_strictly_exceed_twice_hot():
    at_limit = [_delta(str(index), index, raw=10, gain=1) for index in range(4)]
    over_limit = at_limit + [_delta("4", 4, raw=10, gain=1)]

    equal = _plan(
        at_limit,
        hot_bucket_limit=2,
        hot_token_limit=10_000,
        shared_cached_hot_tokens=10_000,
    )
    over = _plan(
        over_limit,
        hot_bucket_limit=2,
        hot_token_limit=10_000,
        shared_cached_hot_tokens=10_000,
    )

    assert not equal.pending_count_over
    assert equal.reason == WAIT_BELOW_AMORTIZED_CROSSING
    assert over.pending_count_over
    assert not over.pending_tokens_over
    assert over.reason == FLUSH_PENDING_CAPACITY


def test_token_cap_fires_only_when_raw_tokens_strictly_exceed_twice_hot():
    equal = _plan(
        [_delta("equal", 1, raw=2_000, gain=1)],
        hot_token_limit=1_000,
        shared_cached_hot_tokens=10_000,
    )
    over = _plan(
        [_delta("over", 1, raw=2_001, gain=1)],
        hot_token_limit=1_000,
        shared_cached_hot_tokens=10_000,
    )

    assert not equal.pending_tokens_over
    assert equal.reason == WAIT_BELOW_AMORTIZED_CROSSING
    assert over.pending_tokens_over
    assert not over.pending_count_over
    assert over.reason == FLUSH_PENDING_CAPACITY


def test_count_and_token_capacity_flags_remain_independent_when_both_fire():
    pending = [_delta(str(index), index, raw=1_000, gain=1) for index in range(3)]
    decision = _plan(
        pending,
        hot_bucket_limit=1,
        hot_token_limit=1_000,
        shared_cached_hot_tokens=100_000,
    )

    assert decision.pending_count_over
    assert decision.pending_tokens_over
    assert decision.capacity_triggered
    assert decision.reason == FLUSH_PENDING_CAPACITY


def test_emergency_has_priority_but_other_trigger_flags_are_preserved():
    pending = [
        _delta(str(index), index, raw=1_000, gain=900, area=10_000)
        for index in range(3)
    ]
    decision = _plan(
        pending,
        emergency=True,
        hot_bucket_limit=1,
        hot_token_limit=1_000,
        shared_cached_hot_tokens=0,
    )

    assert decision.reason == FLUSH_EMERGENCY
    assert decision.emergency_requested
    assert decision.capacity_triggered
    assert decision.amortized_crossed


def test_capacity_has_priority_over_simultaneous_amortized_crossing():
    decision = _plan(
        [
            _delta("a", 1, raw=1_000, gain=900),
            _delta("b", 2, raw=1_001, gain=900),
        ],
        hot_token_limit=1_000,
        shared_cached_hot_tokens=0,
    )

    assert decision.pending_tokens_over
    assert decision.amortized_crossed
    assert decision.reason == FLUSH_PENDING_CAPACITY


def test_flush_all_returns_every_member_in_canonical_order():
    decision = _plan(
        [
            _delta("z", 9),
            _delta("b", 2),
            _delta("a", 2),
        ],
        shared_cached_hot_tokens=0,
    )

    assert decision.pending_delta_ids == ("a", "b", "z")
    assert decision.member_delta_ids == ("a", "b", "z")
    assert decision.member_bucket_sequences == (2, 9)


def test_fixed_policy_waits_below_n_even_when_dynamic_wq_has_crossed():
    decision = _plan(
        [_delta("a", 1), _delta("b", 2)],
        batch_policy=BATCH_POLICY_FIXED,
        fixed_batch_size=3,
        shared_cached_hot_tokens=0,
    )

    assert decision.amortized_crossed is True
    assert decision.action == "wait"
    assert decision.reason == WAIT_FIXED_BATCH_INCOMPLETE
    assert decision.member_delta_ids == ()
    assert decision.batch_policy == "fixed"
    assert decision.fixed_batch_size == 3


def test_fixed_policy_selects_exact_oldest_n_in_canonical_order():
    decision = _plan(
        [
            _delta("z", 3),
            _delta("b", 1),
            _delta("a", 1),
            _delta("c", 2),
        ],
        batch_policy=BATCH_POLICY_FIXED,
        fixed_batch_size=3,
        shared_cached_hot_tokens=100_000,
    )

    assert decision.reason == FLUSH_FIXED_BATCH_SIZE
    assert decision.member_delta_ids == ("a", "b", "c")
    assert decision.member_bucket_sequences == (1, 2)
    assert decision.pending_delta_ids == ("a", "b", "c", "z")


def test_fixed_policy_capacity_override_remains_flush_all():
    pending = [_delta(str(index), index, raw=10, gain=1) for index in range(5)]

    decision = _plan(
        pending,
        hot_bucket_limit=2,
        hot_token_limit=10_000,
        batch_policy=BATCH_POLICY_FIXED,
        fixed_batch_size=2,
        shared_cached_hot_tokens=100_000,
    )

    assert decision.reason == FLUSH_PENDING_CAPACITY
    assert decision.member_delta_ids == tuple(str(index) for index in range(5))


def test_wait_returns_pool_telemetry_but_no_publish_members():
    decision = _plan([_delta("raw", 3, gain=1)])

    assert decision.reason == WAIT_BELOW_AMORTIZED_CROSSING
    assert decision.pending_delta_ids == ("raw",)
    assert decision.member_delta_ids == ()
    assert decision.member_bucket_sequences == ()


@pytest.mark.parametrize(
    ("read_weight", "shared_tokens", "expected_reason"),
    [
        (0.0, 1, WAIT_BELOW_AMORTIZED_CROSSING),
        (0.0, 0, FLUSH_AMORTIZED_CROSSING),
        (1.0, 100_000, FLUSH_AMORTIZED_CROSSING),
    ],
)
def test_cache_read_weight_boundaries(read_weight, shared_tokens, expected_reason):
    decision = _plan(
        [_delta("x", 1, gain=1)],
        cache_read_weight=read_weight,
        shared_cached_hot_tokens=shared_tokens,
    )

    assert decision.reason == expected_reason
    assert decision.shared_overhead == pytest.approx(
        (1.0 - read_weight) * shared_tokens
    )


def test_large_immediate_gain_does_not_create_a_separate_v11_fast_path():
    decision = _plan(
        [_delta("large", 1, raw=100_000, gain=90_000)],
        cache_read_weight=0.001,
        shared_cached_hot_tokens=1_000_000,
        hot_token_limit=100_000,
    )

    assert not decision.capacity_triggered
    assert not decision.amortized_crossed
    assert decision.reason == WAIT_BELOW_AMORTIZED_CROSSING


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"cache_read_weight": -0.1}, ValueError),
        ({"cache_read_weight": 1.1}, ValueError),
        ({"cache_read_weight": math.nan}, ValueError),
        ({"cache_read_weight": True}, TypeError),
        ({"shared_cached_hot_tokens": -1}, ValueError),
        ({"shared_cached_hot_tokens": 1.5}, TypeError),
        ({"hot_bucket_limit": 0}, ValueError),
        ({"hot_token_limit": 0}, ValueError),
        ({"emergency": 1}, TypeError),
        ({"batch_policy": "other"}, ValueError),
        ({"batch_policy": 1}, TypeError),
        ({"fixed_batch_size": 0}, ValueError),
        ({"fixed_batch_size": True}, TypeError),
    ],
)
def test_invalid_planner_inputs_fail_fast(overrides, error):
    with pytest.raises(error):
        _plan([_delta("x", 1)], **overrides)


@pytest.mark.parametrize(
    "candidate",
    [
        PendingDelta("", 1, 100, 50),
        PendingDelta("negative-sequence", -1, 100, 50),
        PendingDelta("no-raw", 1, 0, 1),
        PendingDelta("no-gain", 1, 100, 0),
        PendingDelta("too-much-gain", 1, 100, 101),
        PendingDelta("negative-area", 1, 100, 50, wait_area=-1),
        PendingDelta("nan-area", 1, 100, 50, wait_area=math.nan),
        PendingDelta("not-present", 1, 100, 50, raw_present=False),
        PendingDelta("underexposed", 1, 100, 50, eligible=False),
    ],
)
def test_invalid_or_noneligible_pending_deltas_are_rejected(candidate):
    with pytest.raises((TypeError, ValueError)):
        _plan([candidate])


def test_duplicate_delta_ids_are_rejected():
    with pytest.raises(ValueError, match="duplicate pending delta_id"):
        _plan([_delta("same", 1), _delta("same", 2)])


def test_non_delta_entries_are_rejected():
    with pytest.raises(TypeError, match="PendingDelta"):
        _plan([object()])


def test_iterables_are_consumed_once_and_ordering_is_deterministic():
    source = [_delta("c", 3), _delta("a", 1), _delta("b", 2)]
    decision = _plan(
        (candidate for candidate in source),
        shared_cached_hot_tokens=0,
    )

    assert decision.member_delta_ids == ("a", "b", "c")


def test_canonical_output_is_independent_of_input_permutation():
    source = [_delta("c", 2), _delta("a", 1), _delta("b", 2)]
    outputs = {
        _plan(permutation, shared_cached_hot_tokens=0).member_delta_ids
        for permutation in itertools.permutations(source)
    }

    assert outputs == {("a", "b", "c")}


def test_seeded_property_score_and_priority_contract():
    rng = random.Random(20260829)
    for case in range(250):
        hot_buckets = rng.randint(1, 8)
        hot_tokens = rng.randint(100, 10_000)
        read_weight = rng.random()
        shared = rng.randint(0, 50_000)
        emergency = rng.choice((False, False, False, True))
        pending = [
            _delta(
                f"{case}-{index}",
                rng.randint(0, 2 * hot_buckets + 3),
                raw=(raw := rng.randint(1, 8_000)),
                gain=rng.randint(1, raw),
                area=rng.random() * 30_000,
            )
            for index in range(rng.randint(0, 12))
        ]

        decision = _plan(
            pending,
            cache_read_weight=read_weight,
            shared_cached_hot_tokens=shared,
            hot_bucket_limit=hot_buckets,
            hot_token_limit=hot_tokens,
            emergency=emergency,
        )

        expected_buckets = len({item.success_sequence for item in pending})
        expected_raw = sum(item.raw_tokens for item in pending)
        expected_gain = sum(item.gain_tokens for item in pending)
        expected_area = math.fsum(item.wait_area for item in pending)
        expected_count_over = expected_buckets > 2 * hot_buckets
        expected_tokens_over = expected_raw > 2 * hot_tokens
        expected_now = read_weight * expected_area
        expected_increment = read_weight * expected_gain
        expected_projected = expected_now + expected_increment
        expected_overhead = (1.0 - read_weight) * shared
        expected_crossed = bool(pending) and expected_projected >= expected_overhead

        assert decision.pending_bucket_count == expected_buckets
        assert decision.pending_raw_tokens == expected_raw
        assert decision.wait_loss_now == pytest.approx(expected_now)
        assert decision.wait_loss_increment == pytest.approx(expected_increment)
        assert decision.wait_loss_projected == pytest.approx(expected_projected)
        assert decision.shared_overhead == pytest.approx(expected_overhead)
        assert decision.pending_count_over == expected_count_over
        assert decision.pending_tokens_over == expected_tokens_over
        assert decision.amortized_crossed == expected_crossed

        if not pending:
            expected_reason = WAIT_EMPTY_PENDING
        elif emergency:
            expected_reason = FLUSH_EMERGENCY
        elif expected_count_over or expected_tokens_over:
            expected_reason = FLUSH_PENDING_CAPACITY
        elif expected_crossed:
            expected_reason = FLUSH_AMORTIZED_CROSSING
        else:
            expected_reason = WAIT_BELOW_AMORTIZED_CROSSING
        assert decision.reason == expected_reason
        assert decision.should_flush == (expected_reason.startswith("FLUSH_"))
