"""Deterministic Delta Hot Tail policy for Context Compression V1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .models import DeltaRecord, DeltaState


@dataclass(frozen=True)
class HotTailDecision:
    hot_delta_ids: tuple[str, ...]
    newly_cold_delta_ids: tuple[str, ...]
    hot_tokens: int
    pressure_applied: bool


def recompute_hot_tail(
    deltas: Sequence[DeltaRecord],
    *,
    active_turn_id: str,
    max_deltas: int,
    token_budget: int,
    current_prompt_tokens: int,
    context_soft_limit: int,
) -> HotTailDecision:
    """Select the newest raw Deltas while preserving the live reasoning chain."""

    max_deltas = max(1, int(max_deltas))
    token_budget = max(1, int(token_budget))
    pressure = context_soft_limit > 0 and current_prompt_tokens >= context_soft_limit
    if pressure:
        max_deltas = max(1, max_deltas // 2)
        token_budget = max(1, token_budget // 2)

    candidates = [delta for delta in deltas if delta.state == DeltaState.HOT]
    keep: set[str] = {
        delta.delta_id
        for delta in candidates
        if active_turn_id and delta.turn_id == active_turn_id
    }
    hot_tokens = sum(
        delta.raw_token_count for delta in candidates if delta.delta_id in keep
    )
    non_active_count = 0
    for delta in reversed(candidates):
        if delta.delta_id in keep:
            continue
        if non_active_count >= max_deltas:
            continue
        if keep or non_active_count:
            if hot_tokens + delta.raw_token_count > token_budget:
                continue
        keep.add(delta.delta_id)
        hot_tokens += delta.raw_token_count
        non_active_count += 1

    if not keep and candidates:
        newest = candidates[-1]
        keep.add(newest.delta_id)
        hot_tokens = newest.raw_token_count

    cold = tuple(delta.delta_id for delta in candidates if delta.delta_id not in keep)
    hot = tuple(delta.delta_id for delta in candidates if delta.delta_id in keep)
    return HotTailDecision(
        hot_delta_ids=hot,
        newly_cold_delta_ids=cold,
        hot_tokens=hot_tokens,
        pressure_applied=pressure,
    )
