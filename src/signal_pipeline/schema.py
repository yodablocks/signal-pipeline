"""
schema.py — The canonical SignalEvent contract.

Every source maps to this. The agent (Hermes) consumes this, never raw source data.
Trust tiers:
    1 — chain-native  (cryptographically verifiable, sub-second latency)
    2 — indexed       (Dune, The Graph, prediction markets — minutes latency)
    3 — social        (Twitter/X, Discord, KOL calls — adversarial)
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class SignalType:
    FUNDING_RATE       = "funding_rate"
    OI_DOMINANCE       = "oi_dominance"
    LIQUIDATION        = "liquidation_cascade"
    WHALE_FLOW         = "whale_flow"
    SMART_MONEY        = "smart_money"
    GAS_VOLATILITY     = "gas_volatility"
    OUTCOME_PROB       = "outcome_prob"
    KOL_CALL           = "kol_call"
    # Options signals — sourced from deribit-options-flow
    PC_RATIO           = "pc_ratio"
    IV_SKEW            = "iv_skew"
    NET_PREMIUM        = "net_premium"
    MAX_PAIN_DISTANCE  = "max_pain_distance"


class Direction:
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class SourceType:
    CHAIN_NATIVE = "chain_native"   # tier 1
    INDEXED      = "indexed"        # tier 2
    PREDICTION   = "prediction"     # tier 2
    SOCIAL       = "social"         # tier 3


@dataclass
class SignalEvent:
    """
    Canonical signal representation. All sources must map to this before
    entering the store or the ranking layer.
    """

    # Identity
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    # Source
    source: str = ""
    source_type: str = ""

    # Subject
    asset: str = ""
    signal_type: str = ""

    # Content
    value: float = 0.0
    direction: str = Direction.NEUTRAL
    confidence: float = 1.0

    # Trust
    trust_tier: int = 2         # 1 | 2 | 3

    # Timestamps
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ingested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Agent context helpers
    position_relevant: bool = False
    summary: str = ""                           # pre-formatted for context window
    chart_series: dict[str, Any] | None = None  # chart-ready structured output

    # Validation
    validation_flags: list[str] = field(default_factory=list)
    is_valid: bool = True

    # Audit
    raw: dict[str, Any] = field(default_factory=dict)

    # Ranking (populated by ranking layer, not by source)
    score: float = 0.0
    rank: int = 0

    def flag(self, reason: str, blocking: bool = False) -> None:
        self.validation_flags.append(reason)
        if blocking:
            self.is_valid = False

    def age_seconds(self) -> float:
        now = datetime.now(timezone.utc)
        ts = self.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds()

    def __repr__(self) -> str:
        return (
            f"SignalEvent(asset={self.asset!r}, type={self.signal_type!r}, "
            f"dir={self.direction!r}, value={self.value:.4f}, "
            f"tier={self.trust_tier}, score={self.score:.3f}, valid={self.is_valid})"
        )
