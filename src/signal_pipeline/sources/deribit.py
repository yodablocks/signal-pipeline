"""
sources/deribit.py — Deribit options flow as a SignalSource.

Wraps github.com/yodablocks/deribit-options-flow (fetcher.py + processor.py).
Produces four SignalEvents per fetch:

    pc_ratio          — put/call OI ratio sentiment
    iv_skew           — 25-delta skew (put IV minus call IV), in percentage points
    net_premium       — net USD premium flow (calls minus puts)
    max_pain_distance — spot distance from max pain, as % of spot

All signals are trust_tier=2 (Deribit is a centralised exchange — indexed,
not chain-native). No auth required. Public API.

Supported assets: BTC, ETH only (Deribit's listed options universe).

Install dependency:
    pip install git+https://github.com/yodablocks/deribit-options-flow
    or drop fetcher.py + processor.py from that repo into your PYTHONPATH.

If the dependency is not available, fetch() returns [] and logs a warning.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from signal_pipeline.schema import (
    Direction,
    SignalEvent,
    SignalType,
    SourceType,
)
from signal_pipeline.sources.base import SignalSource

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dependency import — deribit-options-flow is an optional sibling repo.
# Graceful degradation: if not installed, source returns [].
# ---------------------------------------------------------------------------
try:
    from fetcher import fetch_options_summary, fetch_index_price
    from processor import build_signals
    _DERIBIT_AVAILABLE = True
except ImportError:
    try:
        # Fallback: installed as a package
        from deribit_options_flow.fetcher import fetch_options_summary, fetch_index_price
        from deribit_options_flow.processor import build_signals
        _DERIBIT_AVAILABLE = True
    except ImportError:
        _DERIBIT_AVAILABLE = False
        log.warning(
            "deribit-options-flow not found. DeribitSource will return []. "
            "Add fetcher.py + processor.py from github.com/yodablocks/deribit-options-flow "
            "to your PYTHONPATH, or install the package."
        )

# ---------------------------------------------------------------------------
# Asset → Deribit currency mapping
# ---------------------------------------------------------------------------
_ASSET_TO_CURRENCY: dict[str, str] = {
    "BTC":  "BTC",
    "ETH":  "ETH",
    "WBTC": "BTC",  # wrapped BTC tracks BTC options
}

# ---------------------------------------------------------------------------
# IV skew: convert decimal to percentage points for model compatibility.
# processor.py returns skew in decimal form (e.g. 0.19 = 19 vol points).
# model.py _vote_iv_skew expects percentage points (e.g. 19.0).
# Threshold in model: ±3pp — meaningful; ±10pp — strong.
# ---------------------------------------------------------------------------
_SKEW_DECIMAL_TO_PP = 100.0

# ---------------------------------------------------------------------------
# Max pain: model expects % distance from spot.
# positive = spot above max pain (bearish gravity)
# negative = spot below max pain (bullish gravity)
# Formula: (spot - max_pain) / spot * 100
# ---------------------------------------------------------------------------


def _direction_from_pc(pc: float) -> str:
    """P/C > 1.2 = put-heavy = bearish. < 0.7 = call-heavy = bullish."""
    if pc > 1.2:
        return Direction.BEARISH
    if pc < 0.7:
        return Direction.BULLISH
    return Direction.NEUTRAL


def _direction_from_skew(skew_pp: float) -> str:
    """Positive skew = put IV > call IV = fear = bearish."""
    if skew_pp > 3.0:
        return Direction.BEARISH
    if skew_pp < -3.0:
        return Direction.BULLISH
    return Direction.NEUTRAL


def _direction_from_net_premium(net: float) -> str:
    """Positive = net call buying = bullish."""
    if net > 0:
        return Direction.BULLISH
    if net < 0:
        return Direction.BEARISH
    return Direction.NEUTRAL


def _direction_from_max_pain_distance(pct: float) -> str:
    """
    Positive pct = spot above max pain → gravitational pull down → bearish.
    Negative pct = spot below max pain → gravitational pull up → bullish.
    Threshold: ±5% from spot considered meaningful (matches model.py).
    """
    if pct > 5.0:
        return Direction.BEARISH
    if pct < -5.0:
        return Direction.BULLISH
    return Direction.NEUTRAL


class DeribitSource(SignalSource):
    """
    Fetches Deribit options flow signals and maps them to the canonical
    SignalEvent schema for signal-pipeline.

    Four signals per fetch (one per model vote function):
        SignalType.PC_RATIO          — put/call OI ratio
        SignalType.IV_SKEW           — 25-delta IV skew in percentage points
        SignalType.NET_PREMIUM       — net USD premium flow
        SignalType.MAX_PAIN_DISTANCE — % distance of spot from max pain strike

    trust_tier=2: Deribit is a centralised exchange. Data is reliable but not
    cryptographically verifiable on-chain.
    """

    SOURCE_NAME = "deribit"
    SOURCE_TYPE = SourceType.INDEXED
    TRUST_TIER = 2

    def supported_assets(self) -> list[str]:
        return list(_ASSET_TO_CURRENCY.keys())

    async def fetch(self, asset: str) -> list[SignalEvent]:
        """
        Fetch options flow signals for the given asset.

        Returns [] if:
        - asset is not in supported_assets()
        - deribit-options-flow is not installed
        - any API call fails (logged, never raised)
        """
        currency = _ASSET_TO_CURRENCY.get(asset.upper())
        if not currency:
            log.debug("DeribitSource: %s not supported. Skipping.", asset)
            return []

        if not _DERIBIT_AVAILABLE:
            return []

        # fetch_options_summary and fetch_index_price are synchronous
        # (requests.get under the hood). Run them in a thread pool so
        # they don't block the event loop.
        import asyncio
        try:
            summaries, spot = await asyncio.gather(
                asyncio.to_thread(fetch_options_summary, currency),
                asyncio.to_thread(fetch_index_price, currency),
            )
        except Exception as exc:
            log.error("DeribitSource.fetch API error for %s: %s", asset, exc)
            return []

        if not summaries or spot <= 0:
            log.warning("DeribitSource: empty response for %s (spot=%.2f)", asset, spot)
            return []

        # build_signals is pure CPU — no I/O, safe to call directly.
        try:
            snapshot = build_signals(summaries, spot)
        except Exception as exc:
            log.error("DeribitSource.build_signals failed for %s: %s", asset, exc)
            return []

        now = datetime.now(timezone.utc)
        signals: list[SignalEvent] = []

        # ── 1. P/C Ratio ───────────────────────────────────────────────────
        pc = snapshot["pc_ratio_oi"]
        pc_direction = _direction_from_pc(pc)
        signals.append(SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.PC_RATIO,
            value=pc,
            direction=pc_direction,
            confidence=0.85,
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=(
                f"Deribit {asset} P/C ratio {pc:.3f} "
                f"({'put-heavy' if pc_direction == Direction.BEARISH else 'call-heavy' if pc_direction == Direction.BULLISH else 'balanced'}). "
                f"Call OI: {snapshot['total_call_oi']:.0f} BTC / Put OI: {snapshot['total_put_oi']:.0f} BTC."
            ),
            raw={
                "pc_ratio_oi":   pc,
                "pc_ratio_vol":  snapshot["pc_ratio_vol"],
                "total_call_oi": snapshot["total_call_oi"],
                "total_put_oi":  snapshot["total_put_oi"],
                "spot":          spot,
            },
        ))

        # ── 2. IV Skew ─────────────────────────────────────────────────────
        # processor.py returns skew in decimal (e.g. 0.19).
        # Convert to percentage points for model compatibility.
        skew_dec = snapshot["iv_skew"]
        skew_pp  = skew_dec * _SKEW_DECIMAL_TO_PP
        skew_direction = _direction_from_skew(skew_pp)
        signals.append(SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.IV_SKEW,
            value=skew_pp,      # model expects percentage points
            direction=skew_direction,
            confidence=0.85,
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=(
                f"Deribit {asset} IV skew {skew_pp:+.1f}pp "
                f"({'put premium' if skew_direction == Direction.BEARISH else 'call premium' if skew_direction == Direction.BULLISH else 'flat'}). "
                f"ATM IV: {snapshot['atm_iv'] * 100:.1f}%."
            ),
            raw={
                "iv_skew_dec": skew_dec,
                "iv_skew_pp":  skew_pp,
                "atm_iv":      snapshot["atm_iv"],
                "spot":        spot,
            },
        ))

        # ── 3. Net Premium Flow ────────────────────────────────────────────
        net = snapshot["net_premium"]
        prem_direction = _direction_from_net_premium(net)
        signals.append(SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.NET_PREMIUM,
            value=net,
            direction=prem_direction,
            confidence=0.80,    # volume data has slightly more noise than OI
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=(
                f"Deribit {asset} net premium flow ${net:+,.0f} "
                f"({'call buying' if prem_direction == Direction.BULLISH else 'put buying'}). "
                f"Call: ${snapshot['call_premium']:,.0f} / Put: ${snapshot['put_premium']:,.0f}."
            ),
            raw={
                "net_premium":  net,
                "call_premium": snapshot["call_premium"],
                "put_premium":  snapshot["put_premium"],
                "spot":         spot,
            },
        ))

        # ── 4. Max Pain Distance ───────────────────────────────────────────
        max_pain = snapshot["max_pain"]
        # Positive = spot above max pain = bearish gravity
        pain_pct = (spot - max_pain) / spot * 100.0
        pain_direction = _direction_from_max_pain_distance(pain_pct)
        signals.append(SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.MAX_PAIN_DISTANCE,
            value=pain_pct,     # model expects % distance, positive = spot above
            direction=pain_direction,
            confidence=0.75,    # max pain gravity is most reliable into expiry
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=(
                f"Deribit {asset} max pain ${max_pain:,.0f}. "
                f"Spot {pain_pct:+.1f}% {'above' if pain_pct > 0 else 'below'} max pain. "
                f"{'Bearish gravity.' if pain_direction == Direction.BEARISH else 'Bullish gravity.' if pain_direction == Direction.BULLISH else 'Spot near max pain.'}"
            ),
            raw={
                "max_pain":   max_pain,
                "spot":       spot,
                "pain_pct":   pain_pct,
            },
        ))

        log.info(
            "DeribitSource fetched 4 signals for %s "
            "(P/C=%.3f skew=%+.1fpp net_prem=$%+,.0f max_pain=%+.1f%%)",
            asset, pc, skew_pp, net, pain_pct,
        )
        return signals
