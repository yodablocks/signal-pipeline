"""
validation.py — Trust and integrity checks on SignalEvents.

Runs before signals enter the store. Flagged signals are stored but
excluded from ranking by default (configurable). This preserves the
audit trail while protecting the agent context from bad data.

Validation philosophy:
  - Flag, don't drop. Dropping silently hides data quality problems.
  - Blocking flags mark is_valid=False (excluded from ranking).
  - Non-blocking flags are informational (included in ranking with penalty).
  - The agent never sees raw validation flags — they're for the data layer.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from datetime import datetime, timezone

from signal_pipeline.schema import SignalEvent, SignalType

log = logging.getLogger(__name__)

# Staleness thresholds in seconds per signal type.
# After this age, the signal is flagged stale (non-blocking by default).
STALENESS_THRESHOLDS: dict[str, float] = {
    SignalType.FUNDING_RATE:   3_600,    # 1 hour — funding periods range 1h-8h
    SignalType.OI_DOMINANCE:     300,    # 5 minutes
    SignalType.LIQUIDATION:       60,    # 1 minute — liquidations are time-sensitive
    SignalType.WHALE_FLOW:     1_800,    # 30 minutes — Dune indexed, some lag expected
    SignalType.SMART_MONEY:    1_800,
    SignalType.GAS_VOLATILITY:   300,
    SignalType.OUTCOME_PROB:   3_600,    # 1 hour — prediction markets move slowly
    SignalType.KOL_CALL:       1_800,    # 30 minutes — social signals go stale fast
}

# Oracle deviation limit for HIP-3 builder-controlled perps (per Hyperliquid spec).
HIP3_ORACLE_MAX_DEVIATION_PCT = 1.0

# Z-score threshold for anomaly detection (per asset + signal_type rolling window).
ANOMALY_ZSCORE_THRESHOLD = 3.0

# Instructable patterns to strip from social signals (basic sanitization).
# Extend as new injection patterns are identified.
_INJECTION_PATTERNS = [
    "ignore previous",
    "disregard",
    "new instruction",
    "system:",
    "assistant:",
    "[inst]",
    "forget",
]


def validate(event: SignalEvent) -> SignalEvent:
    """
    Run all validation checks on a SignalEvent in place.
    Returns the same event with validation_flags and is_valid populated.
    """
    _check_required_fields(event)
    _check_staleness(event)
    _check_confidence_range(event)
    _check_trust_tier(event)
    if event.source_type == "social":
        _sanitize_social(event)
    return event


def validate_batch(
    events: list[SignalEvent],
    reference_prices: dict[str, float] | None = None,
) -> list[SignalEvent]:
    """
    Validate a batch of events. Also runs cross-event anomaly detection.
    reference_prices: {asset: price} — used for oracle deviation checks.
    """
    for event in events:
        validate(event)
        if reference_prices and event.asset in reference_prices:
            _check_oracle_deviation(event, reference_prices[event.asset])

    _check_anomalies(events)
    return events


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def _check_required_fields(event: SignalEvent) -> None:
    missing = [
        f for f in ("source", "asset", "signal_type", "source_type")
        if not getattr(event, f, None)
    ]
    if missing:
        event.flag(f"missing_fields:{','.join(missing)}", blocking=True)


def _check_staleness(event: SignalEvent) -> None:
    threshold = STALENESS_THRESHOLDS.get(event.signal_type, 3_600)
    age = event.age_seconds()
    if age > threshold:
        event.flag(f"stale:{age:.0f}s_exceeds_{threshold:.0f}s_threshold")


def _check_confidence_range(event: SignalEvent) -> None:
    if not (0.0 <= event.confidence <= 1.0):
        event.flag(f"confidence_out_of_range:{event.confidence}", blocking=True)


def _check_trust_tier(event: SignalEvent) -> None:
    if event.trust_tier not in (1, 2, 3):
        event.flag(f"invalid_trust_tier:{event.trust_tier}", blocking=True)


def _check_oracle_deviation(event: SignalEvent, reference_price: float) -> None:
    """
    For funding rate signals, check if the implied mark price deviates
    excessively from a reference (e.g. HyperliquidX mark price).
    HIP-3 builder-controlled oracles can push manipulated prices.
    """
    if event.signal_type != SignalType.FUNDING_RATE:
        return
    mark = event.raw.get("mark_price") or event.raw.get("markPrice")
    if not mark:
        return
    try:
        deviation_pct = abs(float(mark) - reference_price) / reference_price * 100
    except (TypeError, ZeroDivisionError):
        return
    if deviation_pct > HIP3_ORACLE_MAX_DEVIATION_PCT:
        event.flag(
            f"oracle_deviation:{deviation_pct:.2f}%_exceeds_{HIP3_ORACLE_MAX_DEVIATION_PCT}%_limit",
            blocking=True,
        )
        log.warning(
            "Oracle deviation detected for %s/%s: %.2f%% (limit %.1f%%)",
            event.source, event.asset, deviation_pct, HIP3_ORACLE_MAX_DEVIATION_PCT,
        )


def _check_anomalies(events: list[SignalEvent]) -> None:
    """
    Modified Z-score anomaly detection using Median Absolute Deviation (MAD).
    MAD is robust to extreme outliers — unlike mean-based z-score, a single
    extreme value doesn't inflate the baseline and mask itself.

    Modified z-score = 0.6745 × (value - median) / MAD
    Flag threshold: ANOMALY_ZSCORE_THRESHOLD (default 3.0).
    Requires at least 3 events in the group to be meaningful.

    Reference: Iglewicz & Hoaglin (1993), "How to Detect and Handle Outliers".
    """
    groups: dict[tuple[str, str], list[SignalEvent]] = defaultdict(list)
    for e in events:
        groups[(e.asset, e.signal_type)].append(e)

    for (asset, stype), group in groups.items():
        if len(group) < 3:
            continue
        values = [e.value for e in group]
        sorted_vals = sorted(values)
        n = len(sorted_vals)
        median = (
            sorted_vals[n // 2] if n % 2
            else (sorted_vals[n // 2 - 1] + sorted_vals[n // 2]) / 2
        )
        deviations = sorted([abs(v - median) for v in values])
        mad = (
            deviations[n // 2] if n % 2
            else (deviations[n // 2 - 1] + deviations[n // 2]) / 2
        )
        if mad == 0:
            continue
        for event in group:
            modified_zscore = 0.6745 * abs(event.value - median) / mad
            if modified_zscore > ANOMALY_ZSCORE_THRESHOLD:
                event.flag(f"anomaly:modified_zscore={modified_zscore:.1f}")
                log.info(
                    "Anomaly flagged: %s/%s value=%.4f modified_zscore=%.1f",
                    asset, stype, event.value, modified_zscore,
                )


def _sanitize_social(event: SignalEvent) -> None:
    """
    Strip/flag potential prompt injection patterns from social signal summaries.
    Social signals (trust_tier=3) are fully adversarial.
    """
    text = (event.summary + " " + str(event.raw)).lower()
    for pattern in _INJECTION_PATTERNS:
        if pattern in text:
            event.flag(f"injection_pattern_detected:{pattern!r}", blocking=True)
            log.warning(
                "Potential injection pattern %r in social signal from %s. Blocked.",
                pattern, event.source,
            )
            break
