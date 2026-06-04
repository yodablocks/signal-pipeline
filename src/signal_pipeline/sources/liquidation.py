"""
sources/liquidation.py — Liquidation cluster proximity as a SignalSource.

Wraps github.com/yodablocks/hl-liquidation-heatmap (fetcher.py).

Reads inferred liquidation fills from the local SQLite written by the
hl-liquidation-heatmap streamer (stream.py). No HTTP calls — pure local
read. Returns [] if the DB does not exist or has no recent data.

Signal produced: SignalType.LIQUIDATION (one per asset)
    value     = USD notional in the nearest significant cluster
    direction = BEARISH if cluster is above spot (sell-side pressure)
                BULLISH if cluster is below spot (buy-side magnet)

The streamer must be running independently to populate the DB:
    cd ~/Coding_2026/hl-liquidation-heatmap
    python stream.py --coins BTC,ETH,SOL

trust_tier=1: data is inferred from chain-native activeAssetCtx WebSocket
(same source as the HL UI liquidation display). OI-delta inference, not
fill metadata — cryptographically verifiable at the chain level.

Install dependency:
    pip install git+https://github.com/yodablocks/hl-liquidation-heatmap
    or set LIQUIDATION_DB env var to point at the running streamer's SQLite.

Graceful degradation: returns [] if:
  - hl-liquidation-heatmap not installed
  - DB does not exist
  - No fills for the asset in the lookback window
  - Current spot price unavailable
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from signal_pipeline.schema import (
    Direction,
    SignalEvent,
    SignalType,
    SourceType,
)
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency import — hl-liquidation-heatmap is an optional sibling repo.
# ---------------------------------------------------------------------------
try:
    from fetcher import fetch_local_liquidations, DB_PATH as _DEFAULT_DB_PATH
    _HEATMAP_AVAILABLE = True
except ImportError:
    try:
        from hl_liquidation_heatmap.fetcher import fetch_local_liquidations, DB_PATH as _DEFAULT_DB_PATH
        _HEATMAP_AVAILABLE = True
    except ImportError:
        _HEATMAP_AVAILABLE = False
        _DEFAULT_DB_PATH = Path("liquidations.db")
        log.warning(
            "hl-liquidation-heatmap not found. LiquidationSource will return []. "
            "Add fetcher.py from github.com/yodablocks/hl-liquidation-heatmap "
            "to your PYTHONPATH, or install the package."
        )

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Lookback window for liquidation fills
_LOOKBACK_DAYS = int(os.getenv("LIQUIDATION_LOOKBACK_DAYS", "3"))

# Bucket size in USD — groups fills into price clusters
_BUCKET_SIZE = int(os.getenv("LIQUIDATION_BUCKET_SIZE", "500"))

# Minimum USD notional for a cluster to be considered significant
_MIN_CLUSTER_USD = float(os.getenv("LIQUIDATION_MIN_CLUSTER_USD", "500000"))

# Proximity threshold: how close (in % of spot) a cluster must be to be relevant.
# Clusters beyond this distance are ignored — they're too far to be a near-term magnet.
_PROXIMITY_PCT = float(os.getenv("LIQUIDATION_PROXIMITY_PCT", "5.0"))

# Asset → coin mapping for the heatmap DB
_ASSET_TO_COIN: dict[str, str] = {
    "BTC":  "BTC",
    "ETH":  "ETH",
    "SOL":  "SOL",
    "HYPE": "HYPE",
}


# ---------------------------------------------------------------------------
# Cluster detection
# ---------------------------------------------------------------------------

def _bucket(price: float, size: int) -> int:
    import math
    return int(math.floor(price / size) * size)


def _build_clusters(fills: list[dict], bucket_size: int) -> dict[int, float]:
    """
    Aggregate fill notional into price buckets.
    Returns {bucket_lower_bound: total_usd_notional}.
    """
    clusters: dict[int, float] = {}
    for f in fills:
        try:
            bkt = _bucket(float(f["px"]), bucket_size)
            clusters[bkt] = clusters.get(bkt, 0.0) + float(f["notional"])
        except (KeyError, ValueError, TypeError):
            continue
    return clusters


def _nearest_cluster(
    clusters: dict[int, float],
    spot: float,
    bucket_size: int,
    min_usd: float,
    proximity_pct: float,
) -> tuple[str, float, float] | None:
    """
    Find the nearest significant liquidation cluster to spot.

    Looks above and below spot separately. Returns the closer one
    if it meets the minimum notional and proximity thresholds.

    Returns: (direction, usd_notional, cluster_price) or None
    """
    max_distance = spot * proximity_pct / 100.0

    above: list[tuple[float, float, float]] = []  # (distance, notional, price)
    below: list[tuple[float, float, float]] = []

    for bkt, notional in clusters.items():
        if notional < min_usd:
            continue
        cluster_price = bkt + bucket_size / 2  # midpoint of bucket
        distance = abs(cluster_price - spot)
        if distance > max_distance:
            continue
        if cluster_price > spot:
            above.append((distance, notional, cluster_price))
        else:
            below.append((distance, notional, cluster_price))

    # Pick nearest significant cluster above and below
    nearest_above = min(above, key=lambda x: x[0]) if above else None
    nearest_below = min(below, key=lambda x: x[0]) if below else None

    if nearest_above and nearest_below:
        # Return the closer one
        if nearest_above[0] <= nearest_below[0]:
            return Direction.BEARISH, nearest_above[1], nearest_above[2]
        else:
            return Direction.BULLISH, nearest_below[1], nearest_below[2]
    if nearest_above:
        return Direction.BEARISH, nearest_above[1], nearest_above[2]
    if nearest_below:
        return Direction.BULLISH, nearest_below[1], nearest_below[2]
    return None


# ---------------------------------------------------------------------------
# Spot price fetch — uses Hyperliquid REST (no auth, no key)
# ---------------------------------------------------------------------------

async def _fetch_spot(asset: str) -> float | None:
    """
    Fetch current mark price from Hyperliquid REST API.
    Returns None on failure — caller handles gracefully.
    """
    import json
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                "https://api.hyperliquid.xyz/info",
                json={"type": "metaAndAssetCtxs"},
            )
            data = resp.json()
            # data = [meta, asset_ctxs]
            meta = data[0].get("universe", [])
            ctxs = data[1]
            for i, m in enumerate(meta):
                if m.get("name", "").upper() == asset.upper():
                    return float(ctxs[i].get("markPx", 0)) or None
    except Exception as exc:
        log.debug("_fetch_spot failed for %s: %s", asset, exc)
    return None


# ---------------------------------------------------------------------------
# Source adapter
# ---------------------------------------------------------------------------

class LiquidationSource(SignalSource):
    """
    Reads inferred liquidation fills from the hl-liquidation-heatmap SQLite
    and maps the nearest significant cluster to a SignalEvent.

    Requires the hl-liquidation-heatmap streamer to be running:
        python stream.py --coins BTC,ETH,SOL

    trust_tier=1: inferred from chain-native activeAssetCtx WebSocket.
    """

    SOURCE_NAME = "hl_liquidation"
    SOURCE_TYPE = SourceType.CHAIN_NATIVE
    TRUST_TIER = 1

    def supported_assets(self) -> list[str]:
        return list(_ASSET_TO_COIN.keys())

    async def fetch(self, asset: str) -> list[SignalEvent]:
        coin = _ASSET_TO_COIN.get(asset.upper())
        if not coin:
            log.debug("LiquidationSource: %s not supported.", asset)
            return []

        if not _HEATMAP_AVAILABLE:
            return []

        # 1. Read fills from SQLite — pure local read, no I/O blocking concern
        try:
            fills = await asyncio.to_thread(
                fetch_local_liquidations, coin, _LOOKBACK_DAYS
            )
        except Exception as exc:
            log.error("LiquidationSource.fetch DB read failed for %s: %s", asset, exc)
            return []

        if not fills:
            log.debug("LiquidationSource: no fills for %s in last %dd", asset, _LOOKBACK_DAYS)
            return []

        # 2. Get current spot price
        spot = await _fetch_spot(asset)
        if not spot:
            log.warning("LiquidationSource: could not fetch spot for %s", asset)
            return []

        # 3. Build clusters and find nearest
        clusters = _build_clusters(fills, _BUCKET_SIZE)
        result = _nearest_cluster(clusters, spot, _BUCKET_SIZE, _MIN_CLUSTER_USD, _PROXIMITY_PCT)

        if result is None:
            log.debug(
                "LiquidationSource: no significant cluster within %.1f%% of spot for %s",
                _PROXIMITY_PCT, asset,
            )
            return [_neutral_event(asset, spot, fills, clusters)]

        direction, usd_notional, cluster_price = result
        now = datetime.now(timezone.utc)
        label = "above" if direction == Direction.BEARISH else "below"

        pct_from_spot = abs(cluster_price - spot) / spot * 100

        event = SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.LIQUIDATION,
            value=usd_notional,
            direction=direction,
            confidence=0.75,    # OI-delta inference, not fill metadata — slightly uncertain
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=(
                f"HL liquidation cluster ${usd_notional:,.0f} {label} spot "
                f"(${cluster_price:,.0f}, {pct_from_spot:.1f}% away). "
                f"{'Sell-side pressure.' if direction == Direction.BEARISH else 'Buy-side magnet.'} "
                f"{len(fills)} inferred events in last {_LOOKBACK_DAYS}d."
            ),
            raw={
                "spot":          spot,
                "cluster_price": cluster_price,
                "cluster_usd":   usd_notional,
                "pct_from_spot": pct_from_spot,
                "direction":     direction,
                "fills_count":   len(fills),
                "lookback_days": _LOOKBACK_DAYS,
                "bucket_size":   _BUCKET_SIZE,
            },
        )

        log.info(
            "LiquidationSource: %s cluster $%,.0f %s spot (%.1f%% away) for %s",
            direction.upper(), usd_notional, label, pct_from_spot, asset,
        )
        return [event]


def _neutral_event(
    asset: str,
    spot: float,
    fills: list[dict],
    clusters: dict[int, float],
) -> SignalEvent:
    """Return a neutral signal when no cluster is close enough to spot."""
    now = datetime.now(timezone.utc)
    total_usd = sum(clusters.values())
    return SignalEvent(
        source="hl_liquidation",
        source_type=SourceType.CHAIN_NATIVE,
        asset=asset,
        signal_type=SignalType.LIQUIDATION,
        value=0.0,
        direction=Direction.NEUTRAL,
        confidence=0.75,
        trust_tier=1,
        timestamp=now,
        ingested_at=now,
        summary=(
            f"HL liquidation: no significant cluster within {_PROXIMITY_PCT:.0f}% of spot. "
            f"Total inferred notional (last {_LOOKBACK_DAYS}d): ${total_usd:,.0f}. "
            f"{len(fills)} events."
        ),
        raw={
            "spot":          spot,
            "fills_count":   len(fills),
            "total_usd":     total_usd,
            "lookback_days": _LOOKBACK_DAYS,
        },
    )
