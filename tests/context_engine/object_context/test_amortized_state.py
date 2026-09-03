import itertools

import pytest

from plugins.context_engine.object_context.amortized_state import (
    TailDelta,
    partition_hot_tail,
)


def _entry(
    delta_id,
    sequence,
    start,
    eligible_at,
    *,
    projectable=True,
    pending=False,
):
    return TailDelta(
        delta_id=delta_id,
        global_sequence=sequence,
        start_token_offset=start,
        eligibility_success_sequence=eligible_at,
        projectable=projectable,
        already_pending=pending,
    )


def _partition(entries, **overrides):
    kwargs = {
        "latest_success_sequence": 4,
        "baseline_prompt_tokens": 10_000,
        "hot_bucket_limit": 4,
        "hot_token_limit": 10_000,
    }
    kwargs.update(overrides)
    return partition_hot_tail(entries, **kwargs)


def test_prospective_request_consumes_one_hot_boundary_position():
    result = _partition([
        _entry("cold", 1, 0, 1),
        _entry("hot-2", 2, 1_000, 2),
        _entry("hot-4", 3, 2_000, 4),
    ])

    assert result.prospective_success_sequence == 5
    assert result.promoted_delta_ids == ("cold",)
    assert result.protected_delta_ids == ("hot-2", "hot-4")


def test_age_boundary_is_strict_inside_and_cold_at_h():
    inside = _partition([_entry("x", 1, 0, 2)], latest_success_sequence=4)
    at_limit = _partition([_entry("x", 1, 0, 1)], latest_success_sequence=4)

    assert inside.protected_delta_ids == ("x",)
    assert at_limit.promoted_delta_ids == ("x",)


def test_token_limit_removes_complete_oldest_success_bucket():
    result = _partition(
        [
            _entry("a", 1, 1_000, 3),
            _entry("b", 2, 2_000, 3),
            _entry("c", 3, 8_000, 4),
        ],
        baseline_prompt_tokens=10_000,
        hot_token_limit=3_000,
    )

    assert result.promoted_delta_ids == ("a", "b")
    assert result.protected_delta_ids == ("c",)
    assert result.hot_start_token_offset == 8_000
    assert result.hot_tokens == 2_000


def test_nonprojectable_cold_delta_leaves_hot_without_entering_pending():
    result = _partition([
        _entry("stable", 1, 0, 1, projectable=False),
        _entry("hot", 2, 9_000, 4),
    ])

    assert result.cold_stable_delta_ids == ("stable",)
    assert result.promoted_delta_ids == ()
    assert result.protected_delta_ids == ("hot",)


def test_existing_pending_never_reenters_hot():
    result = _partition([
        _entry("pending", 1, 0, 4, pending=True),
        _entry("hot", 2, 8_000, 4),
    ])

    assert "pending" not in result.protected_delta_ids
    assert "pending" not in result.promoted_delta_ids
    assert result.protected_delta_ids == ("hot",)


def test_underexposed_is_immutable_and_reports_irreducible_token_overflow():
    result = _partition(
        [
            _entry("unseen", 1, 0, None),
            _entry("seen", 2, 5_000, 4),
        ],
        baseline_prompt_tokens=20_000,
        hot_token_limit=1_000,
    )

    assert result.protected_delta_ids == ("unseen",)
    assert result.promoted_delta_ids == ("seen",)
    assert result.underexposed_delta_count == 1
    assert result.hot_tokens == 20_000
    assert result.hot_overflow_tokens == 19_000


def test_empty_tail_starts_at_request_end():
    result = _partition([], baseline_prompt_tokens=12_345)

    assert result.hot_start_token_offset == 12_345
    assert result.hot_tokens == 0
    assert result.hot_overflow_tokens == 0


def test_seen_hot_counts_distinct_success_boundaries():
    result = _partition([
        _entry("a", 1, 7_000, 3),
        _entry("b", 2, 8_000, 3),
        _entry("c", 3, 9_000, 4),
    ])

    assert result.seen_hot_delta_count == 3
    assert result.seen_hot_bucket_count == 2


def test_result_is_independent_of_input_permutation():
    source = [
        _entry("a", 1, 1_000, 1),
        _entry("b", 2, 8_000, 3),
        _entry("c", 3, 9_000, None),
    ]
    outputs = {
        (
            _partition(permutation).protected_delta_ids,
            _partition(permutation).promoted_delta_ids,
        )
        for permutation in itertools.permutations(source)
    }

    assert outputs == {(("b", "c"), ("a",))}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"latest_success_sequence": -1},
        {"baseline_prompt_tokens": -1},
        {"hot_bucket_limit": 0},
        {"hot_token_limit": 0},
    ],
)
def test_invalid_limits_fail_fast(kwargs):
    with pytest.raises((TypeError, ValueError)):
        _partition([], **kwargs)


def test_invalid_or_duplicate_entries_fail_fast():
    with pytest.raises(ValueError, match="duplicate"):
        _partition([
            _entry("same", 1, 0, 1),
            _entry("same", 2, 1, 2),
        ])
    with pytest.raises(ValueError, match="underexposed"):
        _partition([_entry("bad", 1, 0, None, pending=True)])
    with pytest.raises(ValueError, match="outside"):
        _partition([_entry("bad", 1, 20_000, 1)])
