"""
ranking.py — Signal strength scoring, deduplication, and budget-aware selection.

The ranking layer sits between the store and the assembler.
It answers: "Given all valid signals for this asset and position context,
which top-N should enter the agent's context window?"

Scoring formula:
    score = recency_weight × trust_weight × magnitude_weight × position_boost

All weights are in [0, 1] except position_boost (1.0 or 1.5).
Final score is in [0, 1.5].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from signal_pipeline.schema import Direction, SignalEvent, SignalType

# ---------------------------------------------------------------------------
# Tunable parameters
# ---------------------------------------------------------------------------

# Recency decay rate per signal type (λ in exp(-λ × age_seconds)).
# Higher λ = faster decay. Calibrated to signal's natural update frequency.
RECENCY_LAMBDA: dict[str, float] = {
    SignalType.FUNDING_RATE:   1 / 3_600,    # half-life ~1h
    SignalType.OI_DOMINANCE:   1 / 300,      # half-life ~5min
    SignalType.LIQUIDATION:    1 / 60,       # half-life ~1min
    SignalType.WHALE_FLOW:     1 / 1_800,    # half-life ~30min
    SignalType.SMART_MONEY:    1 / 1_800,
    SignalType.GAS_VOLATILITY: 1 / 300,
    SignalType.OUTCOME_PROB:   1 / 3_600,
    SignalType.KOL_CALL:       1 / 900,      # half-life ~15min
}
DEFAULT_LAMBDA = 1 / 1_800

# Trust tier multipliers. Tier 3 (social) can never outrank tier 1 (chain-native).
TRUST_WEIGHTS: dict[int, float] = {1: 1.0, 2: 0.7, 3: 0.4}

# Position relevance boost.
POSITION_BOOST = 1.5

# Deduplication window in seconds: same (asset, signal_type, direction, source)
# within this window → keep highest score.
DEDUP_WINDOW_SECONDS = 300

# Approximate tokens per signal in the assembled payload.
# Used for budget-aware selection.
TOKENS_PER_SIGNAL = 80


# ---------------------------------------------------------------------------
# Magnitude normalization bounds per signal type
# Used to normalize raw values to [0, 1] for scoring.
# ---------------------------------------------------------------------------
MAGNITUDE_BOUNDS: dict[str, tuple[float, float]] = {
    SignalType.FUNDING_RATE:   (-200.0, 200.0),    # APR %
    SignalType.OI_DOMINANCE:   (0.0, 100.0),       # market share %
    SignalType.LIQUIDATION:    (0.0, 100_000_000),  # USD
    SignalType.WHALE_FLOW:     (-1e9, 1e9),         # USD net flow
    SignalType.SMART_MONEY:    (-1e9, 1e9),
    SignalType.GAS_VOLATILITY: (0.0, 100.0),        # % volatility proxy
    SignalType.OUTCOME_PROB:   (0.0, 1.0),          # probability
    SignalType.KOL_CALL:       (0.0, 1.0),          # credibility score
}


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _recency_weight(event: SignalEvent) -> float:
    lam = RECENCY_LAMBDA.get(event.signal_type, DEFAULT_LAMBDA)
    return math.exp(-lam * event.age_seconds())


def _trust_weight(event: SignalEvent) -> float:
    return TRUST_WEIGHTS.get(event.trust_tier, 0.4)


def _magnitude_weight(event: SignalEvent) -> float:
    bounds = MAGNITUDE_BOUNDS.get(event.signal_type)
    if not bounds:
        return 0.5
    lo, hi = bounds
    if hi == lo:
        return 0.5
    normalized = (event.value - lo) / (hi - lo)
    return max(0.0, min(1.0, normalized))


def _position_boost(event: SignalEvent) -> float:
    return POSITION_BOOST if event.position_relevant else 1.0


def score_event(event: SignalEvent) -> float:
    """Compute signal strength score. Higher = more relevant to agent."""
    return (
        _recency_weight(event)
        * _trust_weight(event)
        * _magnitude_weight(event)
        * _position_boost(event)
        * event.confidence
    )


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def _dedup_key(event: SignalEvent) -> tuple:
    """Events with the same key within DEDUP_WINDOW are duplicates."""
    return (event.asset, event.signal_type, event.direction, event.source)


def deduplicate(events: list[SignalEvent]) -> list[SignalEvent]:
    """
    Within DEDUP_WINDOW_SECONDS, keep the highest-scoring event per dedup key.
    Events outside the window are always kept (stale data, but not duplicates).
    """
    best: dict[tuple, SignalEvent] = {}

    for event in events:
        key = _dedup_key(event)
        existing = best.get(key)
        if existing is None:
            best[key] = event
        else:
            age_diff = abs(event.age_seconds() - existing.age_seconds())
            if age_diff <= DEDUP_WINDOW_SECONDS:
                if event.score > existing.score:
                    best[key] = event
            else:
                # Different time window — not a duplicate
                best[key] = event

    return list(best.values())


# ---------------------------------------------------------------------------
# Budget-aware selection
# ---------------------------------------------------------------------------

def select_top_n(
    events: list[SignalEvent],
    token_budget: int = 2_000,
    max_signals: int | None = None,
) -> list[SignalEvent]:
    """
    Select top-N signals by score within a token budget.
    Returns signals sorted by rank (rank 1 = highest score).
    """
    sorted_events = sorted(events, key=lambda e: e.score, reverse=True)

    selected: list[SignalEvent] = []
    tokens_used = 0

    for rank, event in enumerate(sorted_events, start=1):
        if max_signals and len(selected) >= max_signals:
            break
        if tokens_used + TOKENS_PER_SIGNAL > token_budget:
            break
        event.rank = rank
        selected.append(event)
        tokens_used += TOKENS_PER_SIGNAL

    return selected


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def rank(
    events: list[SignalEvent],
    position_assets: set[str] | None = None,
    token_budget: int = 2_000,
    max_signals: int | None = None,
) -> list[SignalEvent]:
    """
    Full ranking pipeline:
    1. Filter invalid events
    2. Mark position-relevant events
    3. Score all events
    4. Deduplicate
    5. Select top-N by token budget

    Args:
        events: all valid+ flagged events from the store
        position_assets: assets in the user's open positions (for relevance boost)
        token_budget: max tokens for signal context in agent payload
        max_signals: hard cap on number of signals regardless of budget
    """
    position_assets = position_assets or set()

    # 1. Filter invalid
    valid = [e for e in events if e.is_valid]

    # 2. Mark position relevance
    for event in valid:
        event.position_relevant = event.asset in position_assets

    # 3. Score
    for event in valid:
        event.score = score_event(event)

    # 4. Deduplicate
    deduped = deduplicate(valid)

    # 5. Select top-N
    return select_top_n(deduped, token_budget=token_budget, max_signals=max_signals)
