"""Pure cache-aware immediate-request planner for Object Context V1.1."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


FLUSH_NET_POSITIVE = "FLUSH_NET_POSITIVE"
WAIT_BELOW_THRESHOLD = "WAIT_BELOW_THRESHOLD"
WAIT_RAW_UNSEEN = "WAIT_RAW_UNSEEN"
WAIT_NO_BASELINE = "WAIT_NO_BASELINE"
NO_COMPRESSIBLE_OBJECTS = "NO_COMPRESSIBLE_OBJECTS"
SUMMARY_RECHECK_BELOW_THRESHOLD = "SUMMARY_RECHECK_BELOW_THRESHOLD"
EMERGENCY_FLUSH = "EMERGENCY_FLUSH"
PROJECTION_FAILED_RAW_FALLBACK = "PROJECTION_FAILED_RAW_FALLBACK"


@dataclass(frozen=True)
class PricingWeights:
    """Request-cost weights in uncached-input-equivalent tokens."""

    cache_read: float
    cache_write: float
    source: str
    version: str = ""
    uncached_input_usd_per_token: float | None = None


@dataclass(frozen=True)
class PrefixFacts:
    """Content-free facts about Q0 and its preceding successful request."""

    baseline_prompt_tokens: int
    baseline_reusable_prefix_tokens: int
    previous_success_available: bool
    cache_granularity_tokens: int = 1
    estimator_source: str = "rough_message_estimator"


@dataclass(frozen=True)
class PreparedCandidate:
    """Locally prepared marginal replacement for one atomic Delta."""

    delta_id: str
    sequence: int
    raw_tokens: int
    projected_tokens: int
    earliest_change_token_offset: int
    object_refs: tuple[str, ...] = ()
    known_summary_cost_equivalent_tokens: float = 0.0

    @property
    def marginal_tokens_removed(self) -> int:
        return max(0, int(self.raw_tokens) - int(self.projected_tokens))


@dataclass(frozen=True)
class BatchScore:
    """Complete score decomposition for one earliest-change alternative."""

    member_delta_ids: tuple[str, ...]
    member_object_refs: tuple[str, ...]
    earliest_changed_delta_id: str
    baseline_prompt_tokens: int
    candidate_prompt_tokens: int
    gross_tokens_removed: int
    baseline_reusable_prefix_tokens: int
    candidate_reusable_prefix_tokens: int
    cache_tokens_invalidated: int
    cache_penalty_equivalent_tokens: float
    known_summary_cost_equivalent_tokens: float
    net_saving_equivalent_tokens: float
    net_saving_usd: float | None


@dataclass(frozen=True)
class EconomicDecision:
    """Deterministic request-local decision plus ranked exact-rescore options."""

    decision_kind: str
    decision_reason: str
    candidate_count: int
    winner: BatchScore | None
    ranked_batches: tuple[BatchScore, ...]
    pricing_source: str
    pricing_version: str
    estimator_source: str

    @property
    def member_delta_ids(self) -> tuple[str, ...]:
        return self.winner.member_delta_ids if self.winner is not None else ()


def resolve_pricing_weights(
    *,
    uncached_input_price: float | None,
    cache_read_price: float | None,
    cache_write_price: float | None,
    source: str = "pricing_entry",
    version: str = "",
    fallback_cache_read: float = 0.10,
    fallback_cache_write: float = 1.00,
) -> PricingWeights:
    """Normalize technical price ratios without ever collapsing to zero work."""

    input_price = (
        float(uncached_input_price)
        if uncached_input_price is not None and uncached_input_price > 0
        else None
    )
    if input_price is None:
        return PricingWeights(
            cache_read=max(0.0, float(fallback_cache_read)),
            cache_write=max(0.0, float(fallback_cache_write)),
            source="configured_fallback",
            version=version,
        )
    read_weight = (
        max(0.0, float(cache_read_price) / input_price)
        if cache_read_price is not None and cache_read_price >= 0
        else max(0.0, float(fallback_cache_read))
    )
    write_weight = (
        max(0.0, float(cache_write_price) / input_price)
        if cache_write_price is not None and cache_write_price >= 0
        else max(0.0, float(fallback_cache_write))
    )
    return PricingWeights(
        cache_read=read_weight,
        cache_write=write_weight,
        source=source,
        version=version,
        uncached_input_usd_per_token=input_price / 1_000_000,
    )


def round_reusable_prefix(tokens: int, granularity_tokens: int) -> int:
    """Round a cacheable prefix down; never claim an unsupported partial block."""

    value = max(0, int(tokens))
    granularity = max(1, int(granularity_tokens))
    return value - (value % granularity)


def _score_batch(
    *,
    members: tuple[PreparedCandidate, ...],
    prefix: PrefixFacts,
    pricing: PricingWeights,
) -> BatchScore:
    earliest = members[0]
    gross = sum(candidate.marginal_tokens_removed for candidate in members)
    baseline_prefix = min(
        max(0, int(prefix.baseline_prompt_tokens)),
        round_reusable_prefix(
            prefix.baseline_reusable_prefix_tokens,
            prefix.cache_granularity_tokens,
        ),
    )
    candidate_prefix = min(
        baseline_prefix,
        round_reusable_prefix(
            earliest.earliest_change_token_offset,
            prefix.cache_granularity_tokens,
        ),
    )
    invalidated = max(0, baseline_prefix - candidate_prefix)
    rewrite_premium = max(0.0, pricing.cache_write - pricing.cache_read)
    cache_penalty = invalidated * rewrite_premium
    summary_cost = sum(
        max(0.0, candidate.known_summary_cost_equivalent_tokens)
        for candidate in members
    )
    net = gross * pricing.cache_write - cache_penalty - summary_cost
    usd = (
        net * pricing.uncached_input_usd_per_token
        if pricing.uncached_input_usd_per_token is not None
        else None
    )
    return BatchScore(
        member_delta_ids=tuple(candidate.delta_id for candidate in members),
        member_object_refs=tuple(
            dict.fromkeys(
                object_ref
                for candidate in members
                for object_ref in candidate.object_refs
            )
        ),
        earliest_changed_delta_id=earliest.delta_id,
        baseline_prompt_tokens=max(0, int(prefix.baseline_prompt_tokens)),
        candidate_prompt_tokens=max(
            0, int(prefix.baseline_prompt_tokens) - gross
        ),
        gross_tokens_removed=gross,
        baseline_reusable_prefix_tokens=baseline_prefix,
        candidate_reusable_prefix_tokens=candidate_prefix,
        cache_tokens_invalidated=invalidated,
        cache_penalty_equivalent_tokens=cache_penalty,
        known_summary_cost_equivalent_tokens=summary_cost,
        net_saving_equivalent_tokens=net,
        net_saving_usd=usd,
    )


def score_exact_batch(
    *,
    member_delta_ids: Iterable[str],
    member_object_refs: Iterable[str],
    earliest_changed_delta_id: str,
    baseline_prompt_tokens: int,
    candidate_prompt_tokens: int,
    baseline_reusable_prefix_tokens: int,
    candidate_reusable_prefix_tokens: int,
    pricing: PricingWeights,
    known_summary_cost_equivalent_tokens: float = 0.0,
) -> BatchScore:
    """Score fully rendered Q0/Qc views without reusing rough marginals."""

    baseline_tokens = max(0, int(baseline_prompt_tokens))
    candidate_tokens = max(0, int(candidate_prompt_tokens))
    gross = max(0, baseline_tokens - candidate_tokens)
    baseline_prefix = min(
        baseline_tokens, max(0, int(baseline_reusable_prefix_tokens))
    )
    candidate_prefix = min(
        candidate_tokens,
        baseline_prefix,
        max(0, int(candidate_reusable_prefix_tokens)),
    )
    invalidated = max(0, baseline_prefix - candidate_prefix)
    cache_penalty = invalidated * max(
        0.0, pricing.cache_write - pricing.cache_read
    )
    summary_cost = max(0.0, float(known_summary_cost_equivalent_tokens))
    net = gross * pricing.cache_write - cache_penalty - summary_cost
    return BatchScore(
        member_delta_ids=tuple(member_delta_ids),
        member_object_refs=tuple(dict.fromkeys(member_object_refs)),
        earliest_changed_delta_id=earliest_changed_delta_id,
        baseline_prompt_tokens=baseline_tokens,
        candidate_prompt_tokens=candidate_tokens,
        gross_tokens_removed=gross,
        baseline_reusable_prefix_tokens=baseline_prefix,
        candidate_reusable_prefix_tokens=candidate_prefix,
        cache_tokens_invalidated=invalidated,
        cache_penalty_equivalent_tokens=cache_penalty,
        known_summary_cost_equivalent_tokens=summary_cost,
        net_saving_equivalent_tokens=net,
        net_saving_usd=(
            net * pricing.uncached_input_usd_per_token
            if pricing.uncached_input_usd_per_token is not None
            else None
        ),
    )


def plan_economic_batch(
    candidates: Iterable[PreparedCandidate],
    *,
    prefix: PrefixFacts,
    pricing: PricingWeights,
    minimum_net_saving_tokens: float,
    minimum_net_saving_usd: float | None = None,
    unseen_candidate_count: int = 0,
    summary_recheck: bool = False,
) -> EconomicDecision:
    """Choose the maximum immediate-value sparse suffix, with no forecasting."""

    ordered = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.marginal_tokens_removed > 0
            ),
            key=lambda candidate: (candidate.sequence, candidate.delta_id),
        )
    )
    common = {
        "candidate_count": len(ordered),
        "pricing_source": pricing.source,
        "pricing_version": pricing.version,
        "estimator_source": prefix.estimator_source,
    }
    if not ordered:
        return EconomicDecision(
            decision_kind="wait",
            decision_reason=(
                WAIT_RAW_UNSEEN
                if unseen_candidate_count > 0
                else NO_COMPRESSIBLE_OBJECTS
            ),
            winner=None,
            ranked_batches=(),
            **common,
        )
    if not prefix.previous_success_available:
        return EconomicDecision(
            decision_kind="wait",
            decision_reason=WAIT_NO_BASELINE,
            winner=None,
            ranked_batches=(),
            **common,
        )

    alternatives = tuple(
        _score_batch(members=ordered[index:], prefix=prefix, pricing=pricing)
        for index in range(len(ordered))
    )
    ranked = tuple(
        sorted(
            alternatives,
            key=lambda score: (
                -score.net_saving_equivalent_tokens,
                -score.gross_tokens_removed,
                score.earliest_changed_delta_id,
            ),
        )
    )
    winner = ranked[0]
    crosses_tokens = (
        winner.net_saving_equivalent_tokens
        >= max(0.0, float(minimum_net_saving_tokens))
    )
    crosses_usd = (
        minimum_net_saving_usd is None
        or winner.net_saving_usd is None
        or winner.net_saving_usd >= max(0.0, float(minimum_net_saving_usd))
    )
    flush = crosses_tokens and crosses_usd
    return EconomicDecision(
        decision_kind="flush" if flush else "wait",
        decision_reason=(
            FLUSH_NET_POSITIVE
            if flush
            else (
                SUMMARY_RECHECK_BELOW_THRESHOLD
                if summary_recheck
                else WAIT_BELOW_THRESHOLD
            )
        ),
        winner=winner,
        ranked_batches=ranked,
        **common,
    )


def plan_emergency_batch(
    candidates: Iterable[PreparedCandidate],
    *,
    prefix: PrefixFacts,
    pricing: PricingWeights,
    target_prompt_tokens: int,
    unseen_candidate_count: int = 0,
) -> EconomicDecision:
    """Choose the viable sparse suffix with the least cache rewrite damage."""

    ordered = tuple(
        sorted(
            (
                candidate
                for candidate in candidates
                if candidate.marginal_tokens_removed > 0
            ),
            key=lambda candidate: (candidate.sequence, candidate.delta_id),
        )
    )
    common = {
        "candidate_count": len(ordered),
        "pricing_source": pricing.source,
        "pricing_version": pricing.version,
        "estimator_source": prefix.estimator_source,
    }
    if not ordered:
        return EconomicDecision(
            decision_kind="wait",
            decision_reason=(
                WAIT_RAW_UNSEEN
                if unseen_candidate_count > 0
                else NO_COMPRESSIBLE_OBJECTS
            ),
            winner=None,
            ranked_batches=(),
            **common,
        )
    target = max(0, int(target_prompt_tokens))
    alternatives = tuple(
        _score_batch(members=ordered[index:], prefix=prefix, pricing=pricing)
        for index in range(len(ordered))
    )
    viable = tuple(
        score for score in alternatives if score.candidate_prompt_tokens <= target
    )
    if not viable:
        return EconomicDecision(
            decision_kind="wait",
            decision_reason=(
                WAIT_RAW_UNSEEN
                if unseen_candidate_count > 0
                else WAIT_BELOW_THRESHOLD
            ),
            winner=max(
                alternatives,
                key=lambda score: (
                    score.gross_tokens_removed,
                    score.net_saving_equivalent_tokens,
                ),
            ),
            ranked_batches=(),
            **common,
        )
    ranked = tuple(
        sorted(
            viable,
            key=lambda score: (
                score.cache_penalty_equivalent_tokens,
                -score.net_saving_equivalent_tokens,
                score.candidate_prompt_tokens,
                score.earliest_changed_delta_id,
            ),
        )
    )
    return EconomicDecision(
        decision_kind="emergency",
        decision_reason=EMERGENCY_FLUSH,
        winner=ranked[0],
        ranked_batches=ranked,
        **common,
    )
