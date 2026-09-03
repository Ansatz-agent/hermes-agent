"""Pure bounded-Hot-Tail partitioning for Object Context V1.2."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class TailDelta:
    """One Raw-present Delta in baseline token order.

    ``eligibility_success_sequence`` is absent until the Delta has reached the
    configured Raw-exposure count. Existing Pending members are never admitted
    back into Hot Tail. Only projectable members can be promoted into Pending;
    other cold members remain stable Raw outside the protected tail.
    """

    delta_id: str
    global_sequence: int
    start_token_offset: int
    eligibility_success_sequence: int | None
    projectable: bool
    already_pending: bool = False

    @property
    def underexposed(self) -> bool:
        return self.eligibility_success_sequence is None


@dataclass(frozen=True)
class HotTailPartition:
    """Content-free result of one request-boundary Hot-Tail rebalance."""

    protected_delta_ids: tuple[str, ...]
    promoted_delta_ids: tuple[str, ...]
    cold_stable_delta_ids: tuple[str, ...]
    hot_start_token_offset: int
    hot_tokens: int
    hot_overflow_tokens: int
    underexposed_delta_count: int
    seen_hot_delta_count: int
    seen_hot_bucket_count: int
    prospective_success_sequence: int


def partition_hot_tail(
    entries: Iterable[TailDelta],
    *,
    latest_success_sequence: int,
    baseline_prompt_tokens: int,
    hot_bucket_limit: int,
    hot_token_limit: int,
) -> HotTailPartition:
    """Partition Raw-present Deltas under count/age and token constraints.

    The request being prepared is the prospective next success boundary and
    consumes one of ``hot_bucket_limit`` positions. A seen boundary leaves Hot
    when its age at that prospective boundary is at least the limit. Token
    pressure then removes complete oldest eligible success buckets. Mandatory
    underexposed entries remain protected even when they make the cap
    impossible, and the irreducible excess is returned explicitly.
    """

    if isinstance(latest_success_sequence, bool) or not isinstance(
        latest_success_sequence, int
    ):
        raise TypeError("latest_success_sequence must be an integer")
    if latest_success_sequence < 0:
        raise ValueError("latest_success_sequence must be >= 0")
    for name, value, minimum in (
        ("baseline_prompt_tokens", baseline_prompt_tokens, 0),
        ("hot_bucket_limit", hot_bucket_limit, 1),
        ("hot_token_limit", hot_token_limit, 1),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"{name} must be an integer")
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")

    ordered = tuple(
        sorted(
            entries, key=lambda item: (item.start_token_offset, item.global_sequence)
        )
    )
    seen_ids: set[str] = set()
    previous_offset = -1
    for item in ordered:
        if not isinstance(item, TailDelta):
            raise TypeError("entries must be TailDelta instances")
        if not item.delta_id:
            raise ValueError("delta_id must be non-empty")
        if item.delta_id in seen_ids:
            raise ValueError(f"duplicate delta_id: {item.delta_id}")
        seen_ids.add(item.delta_id)
        if item.global_sequence < 0:
            raise ValueError("global_sequence must be >= 0")
        if not 0 <= item.start_token_offset <= baseline_prompt_tokens:
            raise ValueError("start_token_offset is outside the baseline")
        if item.start_token_offset < previous_offset:
            raise AssertionError("internal ordering failure")
        previous_offset = item.start_token_offset
        if (
            item.eligibility_success_sequence is not None
            and item.eligibility_success_sequence < 1
        ):
            raise ValueError("eligibility_success_sequence must be positive")
        if not isinstance(item.projectable, bool) or not isinstance(
            item.already_pending, bool
        ):
            raise TypeError("projectable/already_pending must be bool")
        if item.underexposed and item.already_pending:
            raise ValueError("underexposed Delta cannot already be Pending")

    prospective = latest_success_sequence + 1
    protected: set[str] = set()
    promoted: set[str] = set()
    cold_stable: set[str] = set()

    for item in ordered:
        if item.already_pending:
            continue
        if item.underexposed:
            protected.add(item.delta_id)
            continue
        age = prospective - int(item.eligibility_success_sequence or 0)
        if age < hot_bucket_limit:
            protected.add(item.delta_id)
        elif item.projectable:
            promoted.add(item.delta_id)
        else:
            cold_stable.add(item.delta_id)

    by_id = {item.delta_id: item for item in ordered}

    def hot_facts() -> tuple[int, int]:
        if not protected:
            return baseline_prompt_tokens, 0
        start = min(by_id[delta_id].start_token_offset for delta_id in protected)
        return start, max(0, baseline_prompt_tokens - start)

    hot_start, hot_tokens = hot_facts()
    while hot_tokens > hot_token_limit:
        movable = [
            item
            for item in ordered
            if item.delta_id in protected and not item.underexposed
        ]
        if not movable:
            break
        oldest_bucket = min(
            int(item.eligibility_success_sequence or 0) for item in movable
        )
        for item in movable:
            if item.eligibility_success_sequence != oldest_bucket:
                continue
            protected.remove(item.delta_id)
            if item.projectable:
                promoted.add(item.delta_id)
            else:
                cold_stable.add(item.delta_id)
        hot_start, hot_tokens = hot_facts()

    protected_order = tuple(
        item.delta_id for item in ordered if item.delta_id in protected
    )
    promoted_order = tuple(
        item.delta_id
        for item in sorted(
            ordered,
            key=lambda item: (
                int(item.eligibility_success_sequence or 0),
                item.global_sequence,
                item.delta_id,
            ),
        )
        if item.delta_id in promoted
    )
    cold_order = tuple(
        item.delta_id for item in ordered if item.delta_id in cold_stable
    )
    seen_hot = [
        by_id[delta_id]
        for delta_id in protected_order
        if not by_id[delta_id].underexposed
    ]
    return HotTailPartition(
        protected_delta_ids=protected_order,
        promoted_delta_ids=promoted_order,
        cold_stable_delta_ids=cold_order,
        hot_start_token_offset=hot_start,
        hot_tokens=hot_tokens,
        hot_overflow_tokens=max(0, hot_tokens - hot_token_limit),
        underexposed_delta_count=sum(
            1 for delta_id in protected_order if by_id[delta_id].underexposed
        ),
        seen_hot_delta_count=len(seen_hot),
        seen_hot_bucket_count=len({
            int(item.eligibility_success_sequence or 0) for item in seen_hot
        }),
        prospective_success_sequence=prospective,
    )


__all__ = ["HotTailPartition", "TailDelta", "partition_hot_tail"]
