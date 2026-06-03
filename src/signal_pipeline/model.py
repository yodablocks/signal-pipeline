"""
model.py — Directional scoring model for the signal pipeline.

Combines ranked SignalEvents into a single directional verdict
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
      A unanimous 7-signal agreement outranks a single strong signal.
    - Options signals (P/C ratio, IV skew, net premium, max pain)
      are treated as a single source cluster to prevent double-counting.
    - Funding rate and OI trend are independent chain-native signals (tier 1).
    - Liquidation cluster proximity is a tier-1 risk signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from signal_pipeline.schema import Direction, SignalEvent, SignalType


# ---------------------------------------------------------------------------
# Signal type → directional vote logic
# Each function returns: (direction: str, strength: float, reason: str)
# strength is in [0, 1].  direction is bullish | bearish | neutral.
# ---------------------------------------------------------------------------

def _vote_funding_rate(event: SignalEvent) -> tuple[str, float, str]:
    """
    Funding rate APR.
    Strongly positive funding → crowded longs → bearish lean.
    Strongly negative funding → crowded shorts → bullish lean.
    Near-zero → neutral.
    Threshold: ±20% APR considered meaningful.
    NOTE: threshold is a heuristic, not empirically validated.
    """
    v = event.value
    if v > 20.0:
        strength = min(1.0, (v - 20.0) / 80.0)
        return Direction.BEARISH, strength, f"funding {v:.1f}% APR (crowded longs)"
    if v < -20.0:
        strength = min(1.0, (-v - 20.0) / 80.0)
        return Direction.BULLISH, strength, f"funding {v:.1f}% APR (crowded shorts)"
    return Direction.NEUTRAL, 0.0, f"funding {v:.1f}% APR (neutral zone)"


def _vote_oi_trend(event: SignalEvent) -> tuple[str, float, str]:
    """
    OI dominance / trend.
    Rising OI + price up → bullish confirmation.
    Rising OI + price down → bearish confirmation.
    direction field already set by the source adapter.
    Use event.direction directly.
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
    event.direction set by hl-liquidation-heatmap adapter.
    event.value = estimated USD at risk in nearest cluster.
    """
    if event.direction == Direction.NEUTRAL:
        return Direction.NEUTRAL, 0.0, "no significant liquidation cluster nearby"
    usd = event.value
    strength = min(1.0, usd / 10_000_000)  # normalised: $10M = full strength
    label = "above" if event.direction == Direction.BEARISH else "below"
    return (
        event.direction,
        strength,
        f"liquidation cluster ${usd:,.0f} {label} spot",
    )


def _vote_pc_ratio(event: SignalEvent) -> tuple[str, float, str]:
    """
    Put/Call ratio (Deribit).
    >1.2 → elevated put buying → bearish sentiment.
    <0.7 → elevated call buying → bullish sentiment.
    0.7–1.2 → neutral.
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
    IV skew (25-delta put IV minus call IV, Deribit).
    Positive skew → puts more expensive → downside fear → bearish.
    Negative skew → calls more expensive → upside demand → bullish.
    Threshold: ±3 vol points considered meaningful.
    NOTE: options skew reflects BTC/ETH derivatives, not HL spot directly.
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
    Positive (net call buying) → bullish.
    Negative (net put buying) → bearish.
    event.direction already set by source adapter.
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
    Spot well above max pain → gravity pulls down → bearish.
    Spot well below max pain → gravity pulls up → bullish.
    Threshold: ±5% from spot considered meaningful.
    NOTE: max pain gravity is most relevant into expiry, not mid-cycle.
    """
    v = event.value  # positive = spot above max pain
    if v > 5.0:
        strength = min(1.0, (v - 5.0) / 15.0)
        return Direction.BEARISH, strength, f"spot {v:.1f}% above max pain"
    if v < -5.0:
        strength = min(1.0, (-v - 5.0) / 15.0)
        return Direction.BULLISH, strength, f"spot {v:.1f}% below max pain"
    return Direction.NEUTRAL, 0.0, f"spot {v:+.1f}% from max pain (in range)"


# ---------------------------------------------------------------------------
# Signal type → vote function dispatch
# ---------------------------------------------------------------------------

_VOTE_FN: dict[str, Any] = {
    SignalType.FUNDING_RATE:    _vote_funding_rate,
    SignalType.OI_DOMINANCE:    _vote_oi_trend,
    SignalType.LIQUIDATION:     _vote_liquidation,
    "pc_ratio":                 _vote_pc_ratio,
    "iv_skew":                  _vote_iv_skew,
    "net_premium":              _vote_net_premium,
    "max_pain_distance":        _vote_max_pain,
}

# Options signals come from the same underlying instrument.
# Count them as a single cluster to avoid over-weighting Deribit.
_OPTIONS_CLUSTER = {"pc_ratio", "iv_skew", "net_premium", "max_pain_distance"}


# ---------------------------------------------------------------------------
# Scoring weights
# UNVALIDATED — equal weights as honest baseline.
# Replace with empirically derived weights once backtesting is available.
# ---------------------------------------------------------------------------

SIGNAL_WEIGHTS: dict[str, float] = {
    SignalType.FUNDING_RATE:    1.0,
    SignalType.OI_DOMINANCE:    1.0,
    SignalType.LIQUIDATION:     1.0,
    "options_cluster":          1.0,   # whole options group = 1 vote
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
        """
        Human-readable breakdown for --explain output.
        Shows each signal's vote and contribution.
        """
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
    Combine ranked SignalEvents into a directional verdict.

    Algorithm:
        1. For each event, call the appropriate vote function.
        2. Options signals (P/C, IV skew, net premium, max pain) are
           averaged into a single cluster vote to prevent double-counting.
        3. Apply equal weights (unvalidated).
        4. Sum weighted scores: bullish = positive, bearish = negative.
        5. Derive direction from sign of sum; confidence from magnitude
           scaled by confluence agreement ratio.

    Args:
        events: ranked signals from ranking.rank() for a single asset
        asset:  asset symbol (e.g. "BTC")

    Returns:
        ModelOutput with direction, confidence, contributing signals, explanation
    """
    asset_events = [e for e in events if e.asset.upper() == asset.upper() and e.is_valid]

    # ---- collect individual votes ----
    contributing: list[ContributingSignal] = []
    options_votes: list[tuple[str, float, str, str]] = []  # (direction, strength, reason, signal_type)
    seen_independent: set[str] = set()

    for event in asset_events:
        fn = _VOTE_FN.get(event.signal_type)
        if fn is None:
            continue

        direction, strength, reason = fn(event)

        if event.signal_type in _OPTIONS_CLUSTER:
            # Buffer for cluster averaging
            options_votes.append((direction, strength, reason, event.signal_type))
        else:
            # Independent signal
            weight = SIGNAL_WEIGHTS.get(event.signal_type, 1.0)
            signed = strength if direction == Direction.BULLISH else (-strength if direction == Direction.BEARISH else 0.0)
            contributing.append(ContributingSignal(
                signal_type=event.signal_type,
                source=event.source,
                direction=direction,
                strength=strength,
                weight=weight,
                weighted_contribution=signed * weight,
                reason=reason,
            ))
            seen_independent.add(event.signal_type)

    # ---- collapse options cluster into a single vote ----
    if options_votes:
        avg_direction, avg_strength, cluster_reason = _average_options_cluster(options_votes)
        weight = SIGNAL_WEIGHTS["options_cluster"]
        signed = avg_strength if avg_direction == Direction.BULLISH else (-avg_strength if avg_direction == Direction.BEARISH else 0.0)
        contributing.append(ContributingSignal(
            signal_type="options_cluster",
            source="deribit",
            direction=avg_direction,
            strength=avg_strength,
            weight=weight,
            weighted_contribution=signed * weight,
            reason=cluster_reason,
        ))

    if not contributing:
        return _no_signal_output(asset)

    # ---- aggregate ----
    total_weight = sum(cs.weight for cs in contributing)
    if total_weight == 0:
        return _no_signal_output(asset)

    raw_score = sum(cs.weighted_contribution for cs in contributing) / total_weight

    # ---- confluence ----
    confluence = _measure_confluence(contributing)

    # ---- derive direction + confidence ----
    if raw_score > 0.05:
        direction = Direction.BULLISH
    elif raw_score < -0.05:
        direction = Direction.BEARISH
    else:
        direction = Direction.NEUTRAL

    # Confidence: magnitude × confluence agreement.
    # Pure magnitude without confluence is misleading (single outlier signal).
    base_confidence = min(1.0, abs(raw_score))
    confidence = base_confidence * confluence.agreement_ratio if confluence.agreement_ratio > 0 else base_confidence * 0.3

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
# Helpers
# ---------------------------------------------------------------------------

def _average_options_cluster(
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
    reason_str = "; ".join(reasons[:3])  # cap at 3 for readability

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
