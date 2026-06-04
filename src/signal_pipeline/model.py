"""
model.py — Directional scoring model for the signal pipeline.

Combines ALL validated SignalEvents into a single directional verdict
that an AI trading agent can consume without interpreting raw values.

Output per asset:
    direction   : bullish | bearish | neutral
    confidence  : 0.0–1.0
    contributing_signals : list of what drove the score
    explanation : human-readable string for the agent

Design decisions (document honestly):
    - Equal weights across signal types.
      UNVALIDATED — no backtesting data exists yet.
      Weights must be revised once historical data is available.
    - Confluence is the primary confidence driver.
      A unanimous agreement outranks a single strong signal.
    - Options signals (P/C ratio, IV skew, net premium, max pain)
      are collapsed into a single cluster vote — prevents Deribit
      from getting 4× weight vs a single chain-native signal.
    - Funding rate signals from multiple venues are collapsed into a
      single funding_cluster vote using the MEDIAN APR across venues.
      Median is used (not mean) for consistency with MAD anomaly detection
      — a single venue at an extreme rate (e.g. GRVT at 11% APR cap)
      does not distort the panel picture.
    - OI dominance and liquidation cluster proximity are independent signals.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from signal_pipeline.schema import Direction, SignalEvent, SignalType


# ---------------------------------------------------------------------------
# Signal type → directional vote logic
# Each function returns: (direction: str, strength: float, reason: str)
# strength is in [0, 1].  direction is bullish | bearish | neutral.
# ---------------------------------------------------------------------------

def _vote_funding_rate(value: float) -> tuple[str, float, str]:
    """
    Funding rate APR (single value — called with the panel median).
    Strongly positive → crowded longs → bearish lean.
    Strongly negative → crowded shorts → bullish lean.
    Threshold: ±20% APR considered meaningful.
    NOTE: threshold is a heuristic, not empirically validated.
    """
    v = value
    if v > 20.0:
        strength = min(1.0, (v - 20.0) / 80.0)
        return Direction.BEARISH, strength, f"funding panel median {v:.1f}% APR (crowded longs)"
    if v < -20.0:
        strength = min(1.0, (-v - 20.0) / 80.0)
        return Direction.BULLISH, strength, f"funding panel median {v:.1f}% APR (crowded shorts)"
    return Direction.NEUTRAL, 0.0, f"funding panel median {v:.1f}% APR (neutral zone)"


def _vote_oi_trend(event: SignalEvent) -> tuple[str, float, str]:
    """
    OI dominance / trend.
    direction field already set by the source adapter.
    """
    strength = min(1.0, event.confidence)
    if event.direction == Direction.BULLISH:
        return Direction.BULLISH, strength, f"OI trend bullish (confidence {event.confidence:.2f})"
    if event.direction == Direction.BEARISH:
        return Direction.BEARISH, strength, f"OI trend bearish (confidence {event.confidence:.2f})"
    return Direction.NEUTRAL, 0.0, "OI trend neutral"


def _vote_liquidation(event: SignalEvent) -> tuple[str, float, str]:
    """
    Liquidation cluster proximity.
    Cluster above spot → sell-side pressure, bearish.
    Cluster below spot → buy-side magnet, bullish.
    event.value = estimated USD at risk in nearest cluster.
    """
    if event.direction == Direction.NEUTRAL:
        return Direction.NEUTRAL, 0.0, "no significant liquidation cluster nearby"
    usd = event.value
    strength = min(1.0, usd / 10_000_000)  # $10M = full strength
    label = "above" if event.direction == Direction.BEARISH else "below"
    return (
        event.direction,
        strength,
        f"liquidation cluster ${usd:,.0f} {label} spot",
    )


def _vote_pc_ratio(event: SignalEvent) -> tuple[str, float, str]:
    """
    Put/Call ratio (Deribit).
    >1.2 → put-heavy → bearish. <0.7 → call-heavy → bullish.
    NOTE: thresholds from market convention, not backtested on HL perps.
    """
    v = event.value
    if v > 1.2:
        strength = min(1.0, (v - 1.2) / 0.8)
        return Direction.BEARISH, strength, f"P/C ratio {v:.2f} (put-heavy)"
    if v < 0.7:
        strength = min(1.0, (0.7 - v) / 0.5)
        return Direction.BULLISH, strength, f"P/C ratio {v:.2f} (call-heavy)"
    return Direction.NEUTRAL, 0.0, f"P/C ratio {v:.2f} (balanced)"


def _vote_iv_skew(event: SignalEvent) -> tuple[str, float, str]:
    """
    IV skew (25-delta put IV minus call IV, in percentage points, Deribit).
    Positive → puts more expensive → downside fear → bearish.
    Negative → calls more expensive → upside demand → bullish.
    Threshold: ±3pp meaningful.
    NOTE: reflects BTC/ETH derivatives, not HL spot directly.
    """
    v = event.value
    if v > 3.0:
        strength = min(1.0, (v - 3.0) / 10.0)
        return Direction.BEARISH, strength, f"IV skew +{v:.1f}pp (put premium)"
    if v < -3.0:
        strength = min(1.0, (-v - 3.0) / 10.0)
        return Direction.BULLISH, strength, f"IV skew {v:.1f}pp (call premium)"
    return Direction.NEUTRAL, 0.0, f"IV skew {v:.1f}pp (flat)"


def _vote_net_premium(event: SignalEvent) -> tuple[str, float, str]:
    """
    Net options premium flow (USD, Deribit).
    direction already set by source adapter.
    """
    strength = min(1.0, abs(event.value) / 5_000_000)  # $5M = full strength
    if event.direction == Direction.BULLISH:
        return Direction.BULLISH, strength, f"net call premium ${event.value:+,.0f}"
    if event.direction == Direction.BEARISH:
        return Direction.BEARISH, strength, f"net put premium ${event.value:+,.0f}"
    return Direction.NEUTRAL, 0.0, "options premium flow neutral"


def _vote_max_pain(event: SignalEvent) -> tuple[str, float, str]:
    """
    Max pain distance from spot (%).
    Positive = spot above max pain → gravity pulls down → bearish.
    Negative = spot below max pain → gravity pulls up → bullish.
    Threshold: ±5% meaningful.
    NOTE: most relevant into expiry, not mid-cycle.
    """
    v = event.value
    if v > 5.0:
        strength = min(1.0, (v - 5.0) / 15.0)
        return Direction.BEARISH, strength, f"spot {v:.1f}% above max pain"
    if v < -5.0:
        strength = min(1.0, (-v - 5.0) / 15.0)
        return Direction.BULLISH, strength, f"spot {v:.1f}% below max pain"
    return Direction.NEUTRAL, 0.0, f"spot {v:+.1f}% from max pain (in range)"


# ---------------------------------------------------------------------------
# Signal type → vote function dispatch (for single-event signals)
# ---------------------------------------------------------------------------

_VOTE_FN: dict[str, Any] = {
    SignalType.OI_DOMINANCE:        _vote_oi_trend,
    SignalType.LIQUIDATION:         _vote_liquidation,
    SignalType.PC_RATIO:            _vote_pc_ratio,
    SignalType.IV_SKEW:             _vote_iv_skew,
    SignalType.NET_PREMIUM:         _vote_net_premium,
    SignalType.MAX_PAIN_DISTANCE:   _vote_max_pain,
    # legacy string keys — kept for backward compatibility
    "pc_ratio":          _vote_pc_ratio,
    "iv_skew":           _vote_iv_skew,
    "net_premium":       _vote_net_premium,
    "max_pain_distance": _vote_max_pain,
}

# Options signals come from the same underlying instrument.
# Collapsed into a single cluster vote.
_OPTIONS_CLUSTER = {
    SignalType.PC_RATIO, SignalType.IV_SKEW,
    SignalType.NET_PREMIUM, SignalType.MAX_PAIN_DISTANCE,
    "pc_ratio", "iv_skew", "net_premium", "max_pain_distance",
}

# OI dominance signals come from multiple venues.
# Collapsed into a single oi_cluster vote using majority direction.
# Majority vote is used (not median) — OI dominance is categorical,
# not a continuous value that can be meaningfully averaged.
_OI_CLUSTER = {SignalType.OI_DOMINANCE}

# Funding rate signals come from multiple venues.
# Collapsed into a single funding_cluster vote using the panel median.
# Median is used for consistency with MAD anomaly detection:
# a single venue at an extreme rate does not distort the panel picture.
_FUNDING_CLUSTER = {SignalType.FUNDING_RATE}


# ---------------------------------------------------------------------------
# Scoring weights
# UNVALIDATED — equal weights as honest baseline.
# Replace with empirically derived weights once backtesting is available.
# ---------------------------------------------------------------------------

SIGNAL_WEIGHTS: dict[str, float] = {
    "funding_cluster":  1.0,   # all venues → single vote
    "oi_cluster":       1.0,   # all venues → single vote
    SignalType.LIQUIDATION:     1.0,
    "options_cluster":          1.0,   # all options signals → single vote
}

_WEIGHT_DISCLAIMER = (
    "UNVALIDATED: equal weights used (no backtesting data). "
    "Do not treat confidence as a calibrated probability."
)


# ---------------------------------------------------------------------------
# Confluence detection
# ---------------------------------------------------------------------------

@dataclass
class ConfluenceResult:
    """How many independent sources agree on a direction."""
    bullish_count: int = 0
    bearish_count: int = 0
    neutral_count: int = 0
    total_sources: int = 0

    @property
    def dominant_direction(self) -> str:
        if self.bullish_count > self.bearish_count:
            return Direction.BULLISH
        if self.bearish_count > self.bullish_count:
            return Direction.BEARISH
        return Direction.NEUTRAL

    @property
    def agreement_ratio(self) -> float:
        """Fraction of non-neutral sources that agree with dominant direction."""
        non_neutral = self.bullish_count + self.bearish_count
        if non_neutral == 0:
            return 0.0
        dominant = max(self.bullish_count, self.bearish_count)
        return dominant / non_neutral

    def summary(self) -> str:
        return (
            f"{self.bullish_count} bullish / {self.bearish_count} bearish / "
            f"{self.neutral_count} neutral out of {self.total_sources} sources"
        )


# ---------------------------------------------------------------------------
# Contributing signal record
# ---------------------------------------------------------------------------

@dataclass
class ContributingSignal:
    signal_type: str
    source: str
    direction: str
    strength: float
    weight: float
    weighted_contribution: float
    reason: str


# ---------------------------------------------------------------------------
# Model output
# ---------------------------------------------------------------------------

@dataclass
class ModelOutput:
    """
    The model's directional verdict for a single asset.
    This is what gets injected into the agent payload.
    """
    asset: str
    direction: str
    confidence: float
    confluence: ConfluenceResult
    contributing_signals: list[ContributingSignal]
    explanation: str
    weight_disclaimer: str = field(default=_WEIGHT_DISCLAIMER)

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "confidence": round(self.confidence, 3),
            "confluence": {
                "bullish": self.confluence.bullish_count,
                "bearish": self.confluence.bearish_count,
                "neutral": self.confluence.neutral_count,
                "agreement_ratio": round(self.confluence.agreement_ratio, 2),
                "summary": self.confluence.summary(),
            },
            "contributing_signals": [
                {
                    "signal_type": cs.signal_type,
                    "source": cs.source,
                    "direction": cs.direction,
                    "strength": round(cs.strength, 3),
                    "weight": cs.weight,
                    "weighted_contribution": round(cs.weighted_contribution, 3),
                    "reason": cs.reason,
                }
                for cs in self.contributing_signals
            ],
            "explanation": self.explanation,
            "weight_disclaimer": self.weight_disclaimer,
        }

    def explain(self) -> str:
        lines = [
            f"━━ Model output: {self.asset} ━━",
            f"Direction  : {self.direction.upper()}",
            f"Confidence : {self.confidence:.1%}",
            f"Confluence : {self.confluence.summary()}",
            "",
            "Signal breakdown:",
        ]
        for cs in sorted(
            self.contributing_signals, key=lambda x: abs(x.weighted_contribution), reverse=True
        ):
            arrow = "▲" if cs.direction == Direction.BULLISH else ("▼" if cs.direction == Direction.BEARISH else "–")
            lines.append(
                f"  {arrow} [{cs.signal_type:<20}] {cs.direction:<8} "
                f"strength={cs.strength:.2f}  weight={cs.weight:.1f}  "
                f"contrib={cs.weighted_contribution:+.3f}  | {cs.reason}"
            )
        lines += [
            "",
            f"Explanation: {self.explanation}",
            f"⚠  {self.weight_disclaimer}",
        ]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core model function
# ---------------------------------------------------------------------------

def score(
    events: list[SignalEvent],
    asset: str,
) -> ModelOutput:
    """
    Combine SignalEvents into a directional verdict.

    Receives ALL validated events for the asset — not the ranking-capped
    top-N. The ranking layer controls what goes into the agent payload
    (token budget). The model layer scores everything.

    Algorithm:
        1. Collect all valid events for the asset.
        2. Funding rate events (multiple venues) → median APR → single
           funding_cluster vote. Median resists extreme venue outliers.
        3. Options signals (P/C, IV skew, net premium, max pain) →
           averaged into a single options_cluster vote.
        4. Remaining signal types (OI, liquidation) → independent votes.
        5. Apply equal weights (unvalidated).
        6. Confidence = magnitude × confluence agreement ratio.
    """
    asset_events = [e for e in events if e.asset.upper() == asset.upper() and e.is_valid]

    contributing: list[ContributingSignal] = []
    funding_events: list[SignalEvent] = []
    oi_events: list[SignalEvent] = []
    options_votes: list[tuple[str, float, str, str]] = []

    for event in asset_events:
        if event.signal_type in _FUNDING_CLUSTER:
            funding_events.append(event)
            continue

        if event.signal_type in _OI_CLUSTER:
            oi_events.append(event)
            continue

        if event.signal_type in _OPTIONS_CLUSTER:
            fn = _VOTE_FN.get(event.signal_type)
            if fn:
                direction, strength, reason = fn(event)
                options_votes.append((direction, strength, reason, event.signal_type))
            continue

        fn = _VOTE_FN.get(event.signal_type)
        if fn is None:
            continue

        direction, strength, reason = fn(event)
        weight = SIGNAL_WEIGHTS.get(event.signal_type, 1.0)
        signed = (
            strength if direction == Direction.BULLISH
            else -strength if direction == Direction.BEARISH
            else 0.0
        )
        contributing.append(ContributingSignal(
            signal_type=event.signal_type,
            source=event.source,
            direction=direction,
            strength=strength,
            weight=weight,
            weighted_contribution=signed * weight,
            reason=reason,
        ))

    # ── Collapse funding cluster → single median vote ──────────────────────
    if funding_events:
        direction, strength, reason, sources = _collapse_funding_cluster(funding_events)
        weight = SIGNAL_WEIGHTS["funding_cluster"]
        signed = (
            strength if direction == Direction.BULLISH
            else -strength if direction == Direction.BEARISH
            else 0.0
        )
        contributing.append(ContributingSignal(
            signal_type="funding_cluster",
            source=sources,
            direction=direction,
            strength=strength,
            weight=weight,
            weighted_contribution=signed * weight,
            reason=reason,
        ))

    # ── Collapse OI cluster → single majority vote ───────────────────────────
    if oi_events:
        direction, strength, reason, sources = _collapse_oi_cluster(oi_events)
        weight = SIGNAL_WEIGHTS["oi_cluster"]
        signed = (
            strength if direction == Direction.BULLISH
            else -strength if direction == Direction.BEARISH
            else 0.0
        )
        contributing.append(ContributingSignal(
            signal_type="oi_cluster",
            source=sources,
            direction=direction,
            strength=strength,
            weight=weight,
            weighted_contribution=signed * weight,
            reason=reason,
        ))

    # ── Collapse options cluster → single averaged vote ────────────────────
    if options_votes:
        direction, strength, reason = _collapse_options_cluster(options_votes)
        weight = SIGNAL_WEIGHTS["options_cluster"]
        signed = (
            strength if direction == Direction.BULLISH
            else -strength if direction == Direction.BEARISH
            else 0.0
        )
        contributing.append(ContributingSignal(
            signal_type="options_cluster",
            source="deribit",
            direction=direction,
            strength=strength,
            weight=weight,
            weighted_contribution=signed * weight,
            reason=reason,
        ))

    if not contributing:
        return _no_signal_output(asset)

    total_weight = sum(cs.weight for cs in contributing)
    if total_weight == 0:
        return _no_signal_output(asset)

    raw_score = sum(cs.weighted_contribution for cs in contributing) / total_weight
    confluence = _measure_confluence(contributing)

    if raw_score > 0.05:
        direction = Direction.BULLISH
    elif raw_score < -0.05:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    base_confidence = min(1.0, abs(raw_score))
    confidence = (
        base_confidence * confluence.agreement_ratio
        if confluence.agreement_ratio > 0
        else base_confidence * 0.3
    )

    explanation = _build_explanation(asset, direction, confidence, confluence, contributing)

    return ModelOutput(
        asset=asset,
        direction=direction,
        confidence=round(confidence, 3),
        confluence=confluence,
        contributing_signals=contributing,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Cluster collapse helpers
# ---------------------------------------------------------------------------

def _collapse_funding_cluster(
    events: list[SignalEvent],
) -> tuple[str, float, str, str]:
    """
    Collapse multiple venue funding rate events into a single panel vote.

    Uses the MEDIAN APR across all venues — consistent with MAD philosophy.
    A single venue at an extreme rate (e.g. GRVT stuck at 11% APR cap)
    does not shift the panel picture.

    Returns: (direction, strength, reason, source_label)
    source_label is a comma-separated list of venue names.
    """
    values = [e.value for e in events]
    sources = ", ".join(sorted({e.source for e in events}))
    n = len(values)

    median_apr = statistics.median(values)
    direction, strength, reason = _vote_funding_rate(median_apr)

    # Annotate with panel context
    reason = (
        f"{reason} | panel median across {n} venues "
        f"(range {min(values):.1f}%–{max(values):.1f}% APR)"
    )
    return direction, strength, reason, sources


def _collapse_oi_cluster(
    events: list[SignalEvent],
) -> tuple[str, float, str, str]:
    """
    Collapse multiple venue OI dominance events into a single panel vote.

    Uses majority direction across venues — OI dominance is categorical
    (bullish/bearish/neutral per venue), not a continuous value.
    Strength = average confidence of venues voting for the majority direction.

    Returns: (direction, strength, reason, source_label)
    """
    bull = [e for e in events if e.direction == Direction.BULLISH]
    bear = [e for e in events if e.direction == Direction.BEARISH]
    sources = ", ".join(sorted({e.source for e in events}))
    n = len(events)

    if len(bull) > len(bear):
        strength = sum(e.confidence for e in bull) / n
        reason = (
            f"OI panel: {len(bull)}/{n} venues bullish "
            f"(avg confidence {strength:.2f})"
        )
        return Direction.BULLISH, min(1.0, strength), reason, sources
    if len(bear) > len(bull):
        strength = sum(e.confidence for e in bear) / n
        reason = (
            f"OI panel: {len(bear)}/{n} venues bearish "
            f"(avg confidence {strength:.2f})"
        )
        return Direction.BEARISH, min(1.0, strength), reason, sources

    reason = f"OI panel: split ({len(bull)} bullish / {len(bear)} bearish / {n - len(bull) - len(bear)} neutral)"
    return Direction.NEUTRAL, 0.0, reason, sources


def _collapse_options_cluster(
    votes: list[tuple[str, float, str, str]],
) -> tuple[str, float, str]:
    """
    Average multiple options signal votes into a single cluster vote.
    Returns: (direction, avg_strength, summary_reason)
    """
    bull_sum = sum(s for d, s, _, _ in votes if d == Direction.BULLISH)
    bear_sum = sum(s for d, s, _, _ in votes if d == Direction.BEARISH)
    n = len(votes)
    reasons = [r for _, _, r, _ in votes if r]
    reason_str = "; ".join(reasons[:3])

    if bull_sum > bear_sum:
        return Direction.BULLISH, bull_sum / n, f"options cluster bullish ({reason_str})"
    if bear_sum > bull_sum:
        return Direction.BEARISH, bear_sum / n, f"options cluster bearish ({reason_str})"
    return Direction.NEUTRAL, 0.0, f"options cluster mixed ({reason_str})"


def _measure_confluence(contributing: list[ContributingSignal]) -> ConfluenceResult:
    result = ConfluenceResult(total_sources=len(contributing))
    for cs in contributing:
        if cs.direction == Direction.BULLISH:
            result.bullish_count += 1
        elif cs.direction == Direction.BEARISH:
            result.bearish_count += 1
        else:
            result.neutral_count += 1
    return result


def _build_explanation(
    asset: str,
    direction: str,
    confidence: float,
    confluence: ConfluenceResult,
    contributing: list[ContributingSignal],
) -> str:
    dominant = [cs for cs in contributing if cs.direction == direction]
    top = sorted(dominant, key=lambda x: abs(x.weighted_contribution), reverse=True)[:3]
    drivers = ", ".join(cs.reason for cs in top) if top else "no dominant signal"
    conf_label = "high" if confidence > 0.65 else ("moderate" if confidence > 0.35 else "low")
    return (
        f"{asset} directional model: {direction.upper()} with {conf_label} confidence ({confidence:.1%}). "
        f"Confluence: {confluence.summary()}. "
        f"Key drivers: {drivers}. "
        f"Agreement ratio: {confluence.agreement_ratio:.0%}."
    )


def _no_signal_output(asset: str) -> ModelOutput:
    return ModelOutput(
        asset=asset,
        direction=Direction.NEUTRAL,
        confidence=0.0,
        confluence=ConfluenceResult(),
        contributing_signals=[],
        explanation=f"No scoreable signals available for {asset}.",
    )
