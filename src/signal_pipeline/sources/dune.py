"""
sources/dune.py — Dune Analytics REST API source (trust_tier=2, indexed).

Dune named explicitly in Faro Cycle 2 dev update as a provider.
Queries indexed on-chain data: whale flows, smart money positioning,
gas volatility proxy, and HL top trader sentiment.

Requires: DUNE_API_KEY environment variable.
Dune API docs: https://docs.dune.com/api-reference/executions/endpoint/get-query-result
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from signal_pipeline.schema import (
    Direction,
    SignalEvent,
    SignalType,
    SourceType,
)
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)

DUNE_BASE = "https://api.dune.com/api/v1"

# Dune query IDs — replace 0 with your saved query ID after pasting SQL into Dune.
# SQL files: docs/dune_queries/
# Each query must return columns: asset, value, direction_hint, summary
# gas_volatility.sql uses {{asset}} Dune parameter — set it when saving the query.
QUERY_IDS: dict[str, int] = {
    SignalType.WHALE_FLOW:      7611264,  # docs/dune_queries/whale_flow.sql — HL bridge, >$500k, 24h net
    SignalType.SMART_MONEY:     7611082,  # docs/dune_queries/smart_money.sql — HL bridge, curated wallets
    SignalType.GAS_VOLATILITY:  7610937,  # docs/dune_queries/gas_volatility.sql
    SignalType.HL_TOP_TRADERS:  0,        # docs/dune_queries/hl_top_traders.sql — REPLACE 0 with query ID
}


def _direction_from_hint(hint: str | None) -> str:
    if not hint:
        return Direction.NEUTRAL
    h = hint.lower()
    if h in ("buy", "inflow", "bullish", "long"):
        return Direction.BULLISH
    if h in ("sell", "outflow", "bearish", "short"):
        return Direction.BEARISH
    return Direction.NEUTRAL


# Confidence per signal type. HL_TOP_TRADERS is slightly lower than bridge-flow
# signals because the dataset refreshes daily (vs near-real-time bridge events).
_CONFIDENCE: dict[str, float] = {
    SignalType.WHALE_FLOW:     0.85,
    SignalType.SMART_MONEY:    0.85,
    SignalType.GAS_VOLATILITY: 0.85,
    SignalType.HL_TOP_TRADERS: 0.75,  # daily refresh, global signal
}


class DuneSource(SignalSource):
    """
    Fetches indexed on-chain signals from Dune Analytics.

    Poll-based (not streaming). Designed to be called on a configured interval
    (e.g. every 5 minutes) — not per-agent-prompt. Results cached by store.

    trust_tier=2: Dune indexes on-chain data but the indexer is a trusted third
    party, not a direct chain read. Treat accordingly in ranking.

    Queries
    -------
    whale_flow        — net USDC bridge flow from large wallets (>$500k, 24h)
    smart_money       — net USDC bridge flow from curated known wallets (24h)
    gas_volatility    — HyperEVM block gas volatility proxy
    hl_top_traders    — aggregate PnL of top-100 HL traders by volume
                        Source: dune."swell-network".dataset_hyperliquid_top_users
                        Published by Liquid Labs (Faro's parent company).
    """

    SOURCE_NAME = "dune"
    SOURCE_TYPE = SourceType.INDEXED
    TRUST_TIER = 2

    def __init__(self, api_key: str | None = None, http_client: httpx.AsyncClient | None = None):
        self._api_key = api_key or os.environ.get("DUNE_API_KEY", "")
        self._http = http_client

    async def fetch(self, asset: str) -> list[SignalEvent]:
        if not self._api_key:
            log.warning("DuneSource: DUNE_API_KEY not set, returning []")
            return []

        signals: list[SignalEvent] = []
        now = datetime.now(timezone.utc)

        for signal_type, query_id in QUERY_IDS.items():
            if query_id == 0:
                log.debug("DuneSource: query_id=0 for %s, skipping", signal_type)
                continue
            try:
                rows = await self._fetch_query(query_id)
            except Exception as exc:
                log.error("DuneSource.fetch query %d failed: %s", query_id, exc)
                continue

            for row in rows:
                row_asset = str(row.get("asset", "")).upper()
                # Global signals inject {{asset}} via Dune parameter — row_asset matches.
                # For any future per-asset signal types, filter by asset here.
                if signal_type not in (
                    SignalType.GAS_VOLATILITY,
                    SignalType.SMART_MONEY,
                    SignalType.WHALE_FLOW,
                    SignalType.HL_TOP_TRADERS,
                ):
                    if row_asset and row_asset != asset.upper():
                        continue

                value = float(row.get("value", 0.0))
                direction = _direction_from_hint(row.get("direction_hint"))
                summary = str(row.get("summary", f"Dune {signal_type} for {asset}"))
                confidence = _CONFIDENCE.get(signal_type, 0.85)

                # Attach extra columns to raw for MAD validation / downstream consumers.
                raw = dict(row)

                signals.append(SignalEvent(
                    source="dune",
                    source_type=SourceType.INDEXED,
                    asset=asset,
                    signal_type=signal_type,
                    value=value,
                    direction=direction,
                    confidence=confidence,
                    trust_tier=self.TRUST_TIER,
                    timestamp=now,
                    ingested_at=now,
                    summary=summary,
                    raw=raw,
                ))

        log.info("DuneSource fetched %d signals for %s", len(signals), asset)
        return signals

    async def _fetch_query(self, query_id: int) -> list[dict[str, Any]]:
        """Fetch latest results for a Dune query."""
        url = f"{DUNE_BASE}/query/{query_id}/results"
        headers = {"X-Dune-API-Key": self._api_key}

        if self._http:
            resp = await self._http.get(url, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, headers=headers)

        resp.raise_for_status()
        data = resp.json()
        return data.get("result", {}).get("rows", [])
