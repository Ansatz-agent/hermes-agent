"""Pure bounded-pending planner for Object Context V1.2.

The planner deliberately owns no persistence, rendering, cache-prefix
estimation, or publication side effects.  Its input is the already validated
pending pool plus one exact shared-cache fact, and its output is a complete,
content-free decision record.  In particular, accumulated waiting area is a
timing signal only; it is never added back to compression value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable


FLUSH_EMERGENCY = "FLUSH_EMERGENCY"
FLUSH_PENDING_CAPACITY = "FLUSH_PENDING_CAPACITY"
FLUSH_AMORTIZED_CROSSING = "FLUSH_AMORTIZED_CROSSING"
FLUSH_FIXED_BATCH_SIZE = "FLUSH_FIXED_BATCH_SIZE"
WAIT_EMPTY_PENDING = "WAIT_EMPTY_PENDING"
WAIT_BELOW_AMORTIZED_CROSSING = "WAIT_BELOW_AMORTIZED_CROSSING"
WAIT_FIXED_BATCH_INCOMPLETE = "WAIT_FIXED_BATCH_INCOMPLETE"
BATCH_POLICY_DYNAMIC = "dynamic"
BATCH_POLICY_FIXED = "fixed"
BATCH_POLICIES = frozenset({BATCH_POLICY_DYNAMIC, BATCH_POLICY_FIXED})


@dataclass(frozen=True)
class PendingDelta:
    """One Raw-seen, eligible Delta in the ordered V1.2 pending pool.

    ``success_sequence`` identifies the successful provider-inference bucket
    that owns this Delta.  Deltas from the same causal bucket share the same
    sequence, so capacity counts distinct sequences rather than objects.

    ``wait_area`` is the unpriced area ``A_i`` accumulated while the Delta was
    pending.  A successful qualifying inference adds ``gain_tokens`` to it;
    the planner applies the cache-read weight exactly once at decision time.
    """

    delta_id: str
    success_sequence: int
    raw_tokens: int
    gain_tokens: int
    wait_area: float = 0.0
    raw_present: bool = True
    eligible: bool = True


@dataclass(frozen=True)
class AmortizedDecision:
    """A side-effect-free V1.2 decision and its full score telemetry."""

    action: str
    reason: str
    emergency_requested: bool
    pending_count_over: bool
    pending_tokens_over: bool
    capacity_triggered: bool
    amortized_crossed: bool
    batch_policy: str
    fixed_batch_size: int
    pending_delta_ids: tuple[str, ...]
    member_delta_ids: tuple[str, ...]
    member_bucket_sequences: tuple[int, ...]
    pending_delta_count: int
    pending_bucket_count: int
    pending_raw_tokens: int
    pending_gain_tokens: int
    total_wait_area: float
    cache_read_weight: float
    shared_cached_hot_tokens: int
    hot_bucket_limit: int
    hot_token_limit: int
    pending_bucket_limit: int
    pending_token_limit: int
    wait_loss_now: float
    wait_loss_increment: float
    wait_loss_projected: float
    shared_overhead: float
    crossing_margin: float

    @property
    def should_flush(self) -> bool:
        """Whether the caller should transactionally publish all members."""

        return self.action == "flush"


def _require_int(value: object, *, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _require_finite_number(
    value: object,
    *,
    name: str,
    minimum: float,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    if result < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return result


def _validate_pending_delta(candidate: PendingDelta) -> None:
    if not isinstance(candidate, PendingDelta):
        raise TypeError("pending entries must be PendingDelta instances")
    if not isinstance(candidate.delta_id, str) or not candidate.delta_id:
        raise ValueError("delta_id must be a non-empty string")
    _require_int(
        candidate.success_sequence,
        name=f"success_sequence for {candidate.delta_id}",
        minimum=0,
    )
    raw = _require_int(
        candidate.raw_tokens,
        name=f"raw_tokens for {candidate.delta_id}",
        minimum=1,
    )
    gain = _require_int(
        candidate.gain_tokens,
        name=f"gain_tokens for {candidate.delta_id}",
        minimum=1,
    )
    if gain > raw:
        raise ValueError(
            f"gain_tokens for {candidate.delta_id} must not exceed raw_tokens"
        )
    _require_finite_number(
        candidate.wait_area,
        name=f"wait_area for {candidate.delta_id}",
        minimum=0.0,
    )
    if not isinstance(candidate.raw_present, bool):
        raise TypeError(f"raw_present for {candidate.delta_id} must be bool")
    if not isinstance(candidate.eligible, bool):
        raise TypeError(f"eligible for {candidate.delta_id} must be bool")
    if not candidate.raw_present or not candidate.eligible:
        raise ValueError(
            f"pending Delta {candidate.delta_id} must be Raw-present and eligible"
        )


def plan_amortized_flush(
    pending: Iterable[PendingDelta],
    *,
    cache_read_weight: float,
    shared_cached_hot_tokens: int,
    hot_bucket_limit: int,
    hot_token_limit: int,
    emergency: bool = False,
    batch_policy: str = BATCH_POLICY_DYNAMIC,
    fixed_batch_size: int = 4,
) -> AmortizedDecision:
    """Plan one V1.2 action at a provider-request boundary.

    The primary-reason order is emergency, pending capacity, projected W/Q
    crossing or fixed-count readiness, then wait. Dynamic actions flush all
    eligible Pending Deltas. Fixed actions select the oldest configured count;
    emergency and capacity remain flush-all safety actions. Empty pending
    always waits because there is no legal publication batch. Independent
    flags remain populated even when a higher-priority reason wins.

    ``shared_cached_hot_tokens`` is an exact, block-rounded prefix fact supplied
    by the caller.  Active/fresh tokens must already be excluded from it.
    """

    read_weight = _require_finite_number(
        cache_read_weight,
        name="cache_read_weight",
        minimum=0.0,
        maximum=1.0,
    )
    shared_tokens = _require_int(
        shared_cached_hot_tokens,
        name="shared_cached_hot_tokens",
        minimum=0,
    )
    hot_buckets = _require_int(
        hot_bucket_limit,
        name="hot_bucket_limit",
        minimum=1,
    )
    hot_tokens = _require_int(
        hot_token_limit,
        name="hot_token_limit",
        minimum=1,
    )
    if not isinstance(emergency, bool):
        raise TypeError("emergency must be bool")
    if not isinstance(batch_policy, str):
        raise TypeError("batch_policy must be a string")
    normalized_batch_policy = batch_policy.strip().casefold()
    if normalized_batch_policy not in BATCH_POLICIES:
        raise ValueError(
            "batch_policy must be one of: " + ", ".join(sorted(BATCH_POLICIES))
        )
    normalized_fixed_batch_size = _require_int(
        fixed_batch_size,
        name="fixed_batch_size",
        minimum=1,
    )

    received = tuple(pending)
    for candidate in received:
        _validate_pending_delta(candidate)
    ordered = tuple(
        sorted(
            received,
            key=lambda candidate: (
                candidate.success_sequence,
                candidate.delta_id,
            ),
        )
    )
    seen_ids: set[str] = set()
    for candidate in ordered:
        if candidate.delta_id in seen_ids:
            raise ValueError(f"duplicate pending delta_id: {candidate.delta_id}")
        seen_ids.add(candidate.delta_id)

    delta_ids = tuple(candidate.delta_id for candidate in ordered)
    bucket_sequences = tuple(
        sorted({candidate.success_sequence for candidate in ordered})
    )
    delta_count = len(ordered)
    bucket_count = len(bucket_sequences)
    raw_tokens = sum(candidate.raw_tokens for candidate in ordered)
    gain_tokens = sum(candidate.gain_tokens for candidate in ordered)
    total_wait_area = math.fsum(float(candidate.wait_area) for candidate in ordered)

    pending_bucket_limit = 2 * hot_buckets
    pending_token_limit = 2 * hot_tokens
    count_over = bucket_count > pending_bucket_limit
    tokens_over = raw_tokens > pending_token_limit
    capacity_triggered = count_over or tokens_over

    wait_loss_now = read_weight * total_wait_area
    wait_loss_increment = read_weight * gain_tokens
    wait_loss_projected = wait_loss_now + wait_loss_increment
    shared_overhead = (1.0 - read_weight) * shared_tokens
    crossing_margin = wait_loss_projected - shared_overhead
    amortized_crossed = delta_count > 0 and wait_loss_projected >= shared_overhead

    fixed_batch_ready = delta_count >= normalized_fixed_batch_size
    if delta_count == 0:
        action = "wait"
        reason = WAIT_EMPTY_PENDING
    elif emergency:
        action = "flush"
        reason = FLUSH_EMERGENCY
    elif capacity_triggered:
        action = "flush"
        reason = FLUSH_PENDING_CAPACITY
    elif normalized_batch_policy == BATCH_POLICY_FIXED and fixed_batch_ready:
        action = "flush"
        reason = FLUSH_FIXED_BATCH_SIZE
    elif normalized_batch_policy == BATCH_POLICY_DYNAMIC and amortized_crossed:
        action = "flush"
        reason = FLUSH_AMORTIZED_CROSSING
    else:
        action = "wait"
        reason = (
            WAIT_FIXED_BATCH_INCOMPLETE
            if normalized_batch_policy == BATCH_POLICY_FIXED
            else WAIT_BELOW_AMORTIZED_CROSSING
        )

    if action != "flush":
        members = ()
    elif (
        normalized_batch_policy == BATCH_POLICY_FIXED
        and reason == FLUSH_FIXED_BATCH_SIZE
    ):
        members = delta_ids[:normalized_fixed_batch_size]
    else:
        members = delta_ids
    member_id_set = set(members)
    member_buckets = tuple(
        sorted({
            candidate.success_sequence
            for candidate in ordered
            if candidate.delta_id in member_id_set
        })
    )
    return AmortizedDecision(
        action=action,
        reason=reason,
        emergency_requested=emergency,
        pending_count_over=count_over,
        pending_tokens_over=tokens_over,
        capacity_triggered=capacity_triggered,
        amortized_crossed=amortized_crossed,
        batch_policy=normalized_batch_policy,
        fixed_batch_size=normalized_fixed_batch_size,
        pending_delta_ids=delta_ids,
        member_delta_ids=members,
        member_bucket_sequences=member_buckets,
        pending_delta_count=delta_count,
        pending_bucket_count=bucket_count,
        pending_raw_tokens=raw_tokens,
        pending_gain_tokens=gain_tokens,
        total_wait_area=total_wait_area,
        cache_read_weight=read_weight,
        shared_cached_hot_tokens=shared_tokens,
        hot_bucket_limit=hot_buckets,
        hot_token_limit=hot_tokens,
        pending_bucket_limit=pending_bucket_limit,
        pending_token_limit=pending_token_limit,
        wait_loss_now=wait_loss_now,
        wait_loss_increment=wait_loss_increment,
        wait_loss_projected=wait_loss_projected,
        shared_overhead=shared_overhead,
        crossing_margin=crossing_margin,
    )


__all__ = [
    "BATCH_POLICIES",
    "BATCH_POLICY_DYNAMIC",
    "BATCH_POLICY_FIXED",
    "AmortizedDecision",
    "FLUSH_AMORTIZED_CROSSING",
    "FLUSH_EMERGENCY",
    "FLUSH_FIXED_BATCH_SIZE",
    "FLUSH_PENDING_CAPACITY",
    "PendingDelta",
    "WAIT_BELOW_AMORTIZED_CROSSING",
    "WAIT_EMPTY_PENDING",
    "WAIT_FIXED_BATCH_INCOMPLETE",
    "plan_amortized_flush",
]
