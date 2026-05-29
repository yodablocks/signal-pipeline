"""
sources/perp.py — Wraps perp-liquidity as a SignalSource.

perp-liquidity (github.com/yodablocks/perp-liquidity) provides:
    FundingRate, OI, Liquidation dataclasses across 8 perp DEXes.

This source maps those to SignalEvent (trust_tier=1, chain-native).
perp-liquidity is a pip dependency — not reimplemented here.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from signal_pipeline.schema import (
    Direction,
    SignalEvent,
    SignalType,
    SourceType,
)
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)

# Attempt import — perp-liquidity must be installed separately.
try:
    from perp_liquidity.cli import run_analysis
    from perp_liquidity.fetchers.base import Coverage
    _PERP_AVAILABLE = True
except ImportError:
    _PERP_AVAILABLE = False
    log.warning(
        "perp-liquidity not installed. PerpSource will return []. "
        "Install with: pip install git+https://github.com/yodablocks/perp-liquidity"
    )


def _direction_from_apr(apr: float) -> str:
    """Negative funding = longs paid = bearish pressure relieved = mild bullish."""
    if apr < -1.0:
        return Direction.BULLISH
    if apr > 5.0:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _direction_from_liq_balance(long_usd: float, short_usd: float) -> str:
    """More longs liquidated = price dropped = bearish event."""
    if long_usd > short_usd * 1.5:
        return Direction.BEARISH
    if short_usd > long_usd * 1.5:
        return Direction.BULLISH
    return Direction.NEUTRAL


class PerpSource(SignalSource):
    """
    Fetches funding rates, OI, and liquidation signals from 8 perp DEXes
    via the perp-liquidity library.

    All signals are trust_tier=1 (chain-native REST/WS, public endpoints).
    """

    SOURCE_NAME = "perp_dex"
    SOURCE_TYPE = SourceType.CHAIN_NATIVE
    TRUST_TIER = 1

    def __init__(self, clip_usds: list[float] | None = None):
        self._clip_usds = clip_usds or [1_000, 10_000, 100_000, 500_000]

    async def fetch(self, asset: str) -> list[SignalEvent]:
        if not _PERP_AVAILABLE:
            return []

        try:
            report = await run_analysis(asset, clip_usds=self._clip_usds)
        except Exception as exc:
            log.error("PerpSource.fetch failed for %s: %s", asset, exc)
            return []

        signals: list[SignalEvent] = []
        now = datetime.now(timezone.utc)

        # --- Funding rate signals ---
        for row in report.funding:
            apr = row.funding_rate.apr_annualized
            direction = _direction_from_apr(apr)
            summary = (
                f"{row.funding_rate.venue} funding APR {apr:+.2f}% "
                f"({'longs paid' if apr < 0 else 'shorts paid'}). "
                f"Rank {row.rank}/8 in panel."
            )
            signals.append(SignalEvent(
                source=row.funding_rate.venue,
                source_type=SourceType.CHAIN_NATIVE,
                asset=asset,
                signal_type=SignalType.FUNDING_RATE,
                value=apr,
                direction=direction,
                confidence=1.0,
                trust_tier=self.TRUST_TIER,
                timestamp=now,
                ingested_at=now,
                summary=summary,
                chart_series={
                    "type": "bar",
                    "label": f"{row.funding_rate.venue} funding APR",
                    "value": apr,
                    "unit": "%",
                },
                raw={"venue": row.funding_rate.venue, "apr": apr, "rank": row.rank},
            ))

        # --- OI dominance signals ---
        for row in report.oi:
            share = row.market_share_pct or 0.0
            direction = Direction.BULLISH if share > 50 else Direction.NEUTRAL
            summary = (
                f"{row.open_interest.venue} OI ${row.open_interest.oi_usd / 1e6:.0f}M "
                f"({share:.1f}% market share). Rank {row.rank}/8."
            )
            signals.append(SignalEvent(
                source=row.open_interest.venue,
                source_type=SourceType.CHAIN_NATIVE,
                asset=asset,
                signal_type=SignalType.OI_DOMINANCE,
                value=share,
                direction=direction,
                confidence=1.0,
                trust_tier=self.TRUST_TIER,
                timestamp=now,
                ingested_at=now,
                summary=summary,
                chart_series={
                    "type": "bar",
                    "label": f"{row.open_interest.venue} OI share",
                    "value": share,
                    "unit": "%",
                },
                raw={
                    "venue": row.open_interest.venue,
                    "usd": row.open_interest.oi_usd,
                    "share": share,
                },
            ))

        # --- Liquidation signals (where available) ---
        if getattr(report, "liquidations", None):
            summary_liq = report.liquidations
            direction = _direction_from_liq_balance(
                summary_liq.long_usd, summary_liq.short_usd
            )
            total_m = summary_liq.total_usd / 1e6
            summary_str = (
                f"{summary_liq.count} liquidations totaling ${total_m:.2f}M. "
                f"Long: ${summary_liq.long_usd / 1e6:.2f}M / "
                f"Short: ${summary_liq.short_usd / 1e6:.2f}M."
            )
            signals.append(SignalEvent(
                source="perp_dex_panel",
                source_type=SourceType.CHAIN_NATIVE,
                asset=asset,
                signal_type=SignalType.LIQUIDATION,
                value=summary_liq.total_usd,
                direction=direction,
                confidence=0.8,
                trust_tier=self.TRUST_TIER,
                timestamp=now,
                ingested_at=now,
                summary=summary_str,
                raw={
                    "count": summary_liq.count,
                    "total_usd": summary_liq.total_usd,
                    "long_usd": summary_liq.long_usd,
                    "short_usd": summary_liq.short_usd,
                },
            ))

        log.info("PerpSource fetched %d signals for %s", len(signals), asset)
        return signals
