"""
assembler.py — Formats ranked signals as an agent-ready context payload.

The assembler is the final step before the agent sees the data.
It produces a structured JSON payload that Hermes (or any LLM agent)
can consume without needing to interpret raw signal values.

Key design decisions:
  - summary string is pre-formatted per signal — agent doesn't parse raw values
  - chart_series is structured for direct rendering (no agent-side transformation)
  - token_estimate is included so the caller can budget context window usage
  - validation_flags are excluded from the agent payload (internal data quality layer)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from signal_pipeline.ranking import TOKENS_PER_SIGNAL
from signal_pipeline.schema import SignalEvent


def assemble(
    signals: list[SignalEvent],
    asset: str,
    position_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build the agent-ready payload from a ranked list of SignalEvents.

    Args:
        signals: ranked list from ranking.rank() (rank 1 = highest priority)
        asset: primary asset being analyzed
        position_context: user's open position info, e.g. {"side": "long", "size_usd": 50000}

    Returns:
        dict ready for json.dumps() — this is what Hermes receives.
    """
    now = datetime.now(timezone.utc).isoformat()

    signal_dicts = []
    for event in signals:
        entry: dict[str, Any] = {
            "rank": event.rank,
            "signal_type": event.signal_type,
            "source": event.source,
            "trust_tier": event.trust_tier,
            "direction": event.direction,
            "value": round(event.value, 6),
            "confidence": round(event.confidence, 2),
            "score": round(event.score, 4),
            "position_relevant": event.position_relevant,
            "summary": event.summary,
            "timestamp": event.timestamp.isoformat() if event.timestamp else now,
        }
        if event.chart_series:
            entry["chart_series"] = event.chart_series
        signal_dicts.append(entry)

    token_estimate = len(signals) * TOKENS_PER_SIGNAL + 150  # 150 for envelope

    payload: dict[str, Any] = {
        "asset": asset,
        "position_context": position_context or {},
        "fetched_at": now,
        "signal_count": len(signals),
        "signals": signal_dicts,
        "token_estimate": token_estimate,
    }

    return payload


def assemble_json(
    signals: list[SignalEvent],
    asset: str,
    position_context: dict[str, Any] | None = None,
    indent: int | None = 2,
) -> str:
    """Convenience wrapper — returns JSON string."""
    return json.dumps(
        assemble(signals, asset, position_context),
        indent=indent,
        default=str,
    )
