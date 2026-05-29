"""
sources/polymarket.py — Polymarket prediction market signals (trust_tier=2).

Maps YES/NO token prices to implied probability SignalEvents.
Used for:
  - Standalone outcome_prob signals
  - KOL call cross-reference (map KOL claim to closest market, compute credibility delta)

Uses Gamma API (gamma-api.polymarket.com) for market search — not the CLOB API,
which is for order placement and returns poorly filtered results.
No auth required for market data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx

from signal_pipeline.schema import (
    Direction,
    SignalEvent,
    SignalType,
    SourceType,
)
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _direction_from_prob(yes_price: float, threshold_bull: float = 0.65, threshold_bear: float = 0.35) -> str:
    if yes_price >= threshold_bull:
        return Direction.BULLISH
    if yes_price <= threshold_bear:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _summary(market: dict, asset: str, yes_price: float) -> str:
    question = market.get("question", "Unknown market")
    return (
        f"Polymarket: '{question}' — YES {yes_price:.1%} implied probability. "
        f"Relevant to {asset}."
    )


class PolymarketSource(SignalSource):
    """
    Fetches implied probability signals from Polymarket markets.

    Markets are matched to the requested asset by keyword search in
    market question text. Rough match — use with care on ambiguous assets.

    trust_tier=2: Polymarket is a decentralised prediction market but
    market matching is fuzzy and the feed is indexed, not chain-native.
    """

    SOURCE_NAME = "polymarket"
    SOURCE_TYPE = SourceType.PREDICTION
    TRUST_TIER = 2

    def __init__(self, http_client: httpx.AsyncClient | None = None):
        self._http = http_client

    async def fetch(self, asset: str) -> list[SignalEvent]:
        try:
            markets = await self._search_markets(asset)
        except Exception as exc:
            log.error("PolymarketSource.fetch failed for %s: %s", asset, exc)
            return []

        signals: list[SignalEvent] = []
        now = datetime.now(timezone.utc)

        for market in markets[:5]:      # cap at 5 markets per asset to control token budget
            # Gamma API: outcomePrices[0] = YES price, outcomePrices[1] = NO price
            try:
                yes_price = float((market.get("outcomePrices") or ["0.5"])[0])
            except (ValueError, IndexError, TypeError):
                yes_price = 0.5

            # Use last update time as signal timestamp
            updated_raw = market.get("updatedAt") or market.get("createdAt")
            try:
                event_ts = datetime.fromisoformat(str(updated_raw).replace("Z", "+00:00"))
            except Exception:
                event_ts = now

            direction = _direction_from_prob(yes_price)

            signals.append(SignalEvent(
                source="polymarket",
                source_type=SourceType.PREDICTION,
                asset=asset,
                signal_type=SignalType.OUTCOME_PROB,
                value=yes_price,
                direction=direction,
                confidence=0.9,
                trust_tier=self.TRUST_TIER,
                timestamp=event_ts,
                ingested_at=now,
                summary=_summary(market, asset, yes_price),
                chart_series={
                    "type": "gauge",
                    "label": market.get("question", ""),
                    "value": yes_price,
                    "unit": "probability",
                },
                raw=market,
            ))

        log.info("PolymarketSource fetched %d signals for %s", len(signals), asset)
        return signals

    async def _search_markets(self, asset: str) -> list[dict]:
        """
        Fetch active Polymarket markets and filter locally for the asset.

        Gamma API's `search` param does not filter by question text — it uses
        an unrelated relevance ranking. We fetch a broad set of active markets
        and filter by question text ourselves.
        """
        url = f"{GAMMA_BASE}/markets"
        params = {
            "active": True,
            "closed": False,
            "limit": 100,
        }

        if self._http:
            resp = await self._http.get(url, params=params, timeout=15)
        else:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params=params)

        resp.raise_for_status()
        data = resp.json()
        markets: list[dict] = data if isinstance(data, list) else data.get("data", [])

        # Common full-name aliases so "BTC" also matches "Bitcoin" in questions
        _ALIASES: dict[str, list[str]] = {
            "BTC": ["bitcoin"],
            "ETH": ["ethereum"],
            "SOL": ["solana"],
            "HYPE": ["hyperliquid"],
        }
        terms = {asset.lower()} | {a for a in _ALIASES.get(asset.upper(), [])}

        now = datetime.now(timezone.utc)
        result = []
        for m in markets:
            # Drop expired markets (Gamma uses endDateIso)
            end_raw = m.get("endDateIso") or m.get("endDate")
            if end_raw:
                try:
                    if datetime.fromisoformat(str(end_raw).replace("Z", "+00:00")) < now:
                        continue
                except Exception:
                    pass

            question = (m.get("question") or "").lower()
            if not any(term in question for term in terms):
                continue

            result.append(m)

        return result
