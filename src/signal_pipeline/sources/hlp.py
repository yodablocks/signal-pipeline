"""
sources/hlp.py — Hyperliquid protocol vault positioning (trust_tier=1, chain-native).

Signals
-------
HLP_SENTIMENT
    Reads the HLP vault's live clearinghouseState from the HL info endpoint.
    HLP is HL's native market maker and liquidator. Its net position per coin
    is a contrarian signal: when HLP is net short, traders are net long (and
    vice versa). A strongly negative hlp_net_notional means retail is positioned
    long and HLP is absorbing — historically a bearish lean since HLP is the
    sophisticated counterparty. A strongly positive net notional means traders
    are positioned short — potential squeeze setup.

    value       = hlp_net_notional_usd (signed, USD)
    direction   = contrarian:
                    HLP net short  (value < -threshold) → traders long → BEARISH
                    HLP net long   (value > +threshold) → traders short → BULLISH
                    near zero                           → NEUTRAL
    confidence  = 0.90 (real-time chain-native, institutional vault)
    trust_tier  = 1

    The signal is intentionally contrarian — HLP IS the market. When it holds a
    large position, it is because it absorbed trader flow. The direction field
    reflects what traders are doing, not what HLP is doing.

TOP_TRADER_POSITIONING (optional, registry-based)
    Uses dune."swell-network".dataset_hyperliquid_top_users as an address
    registry (addy, id columns — e.g. id='top_btc' for BTC top traders).
    For each address in the registry matching the requested asset, fetches
    clearinghouseState and aggregates net notional.
    Disabled by default (requires Dune API key + registry addresses).
    Enable by passing registry_addresses to HLPSource.__init__.

Source
------
POST https://api.hyperliquid.xyz/info
{"type": "clearinghouseState", "user": "<address>"}

Response shape (relevant fields):
{
  "assetPositions": [
    {
      "position": {
        "coin": "BTC",
        "szi": "-12.5",          # signed size in coin units (negative = short)
        "entryPx": "67000.0",
        "positionValue": "837500.0",  # abs USD notional
        "unrealizedPnl": "...",
      },
      "type": "oneWay"
    }
  ],
  "marginSummary": {"accountValue": "...", ...}
}

net_notional_usd = szi × mark_px  (signed)
We use positionValue × sign(szi) to avoid needing a separate mark price call.
"""

from __future__ import annotations

import logging
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

HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# Canonical HLP vault address — the protocol-owned market maker / liquidator vault.
# Verified from Hyperliquid docs vaultSummaries response.
# https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint
HLP_VAULT_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

# Direction threshold: USD net notional that constitutes a meaningful signal.
# HLP routinely runs $10–100M+ per coin. Below $5M treat as noise.
HLP_DIRECTION_THRESHOLD_USD = 5_000_000


def _parse_asset_positions(
    data: dict[str, Any],
    asset: str,
) -> tuple[float, float, float]:
    """
    Parse clearinghouseState response for a specific asset.

    Returns
    -------
    net_notional_usd : float
        Signed net notional in USD. Negative = net short.
    gross_notional_usd : float
        Absolute notional in USD.
    account_value_usd : float
        Total vault AUM from marginSummary (always present).
    """
    account_value = float(
        data.get("marginSummary", {}).get("accountValue", 0.0)
    )
    positions = data.get("assetPositions", [])
    for entry in positions:
        pos = entry.get("position", {})
        if pos.get("coin", "").upper() != asset.upper():
            continue
        szi = float(pos.get("szi", 0.0))
        pos_value = float(pos.get("positionValue", 0.0))  # always positive
        sign = 1.0 if szi >= 0 else -1.0
        net_notional = pos_value * sign
        return net_notional, pos_value, account_value
    # Asset not in HLP's book — confirmed flat
    return 0.0, 0.0, account_value


def _direction_from_hlp_net(net_notional: float, threshold: float) -> str:
    """
    Contrarian mapping: HLP's position tells us what traders are doing.

    HLP net short  → traders net long  → bearish (crowded long)
    HLP net long   → traders net short → bullish (potential squeeze)
    """
    if net_notional < -threshold:
        return Direction.BEARISH
    if net_notional > threshold:
        return Direction.BULLISH
    return Direction.NEUTRAL


def _confidence_from_gross(gross_notional: float) -> float:
    """
    Scale confidence by gross notional. Larger HLP position = more signal.
    Caps at 0.90. Below $1M gross = 0.70 (thin position, low conviction).
    """
    if gross_notional >= 50_000_000:
        return 0.90
    if gross_notional >= 10_000_000:
        return 0.85
    if gross_notional >= 1_000_000:
        return 0.80
    return 0.70


class HLPSource(SignalSource):
    """
    Fetches HLP vault positioning from the Hyperliquid info endpoint.

    trust_tier=1: direct chain-native REST call, no intermediary.
    Real-time — suitable for polling every 1–5 minutes.

    Parameters
    ----------
    vault_address : str
        HLP vault address. Defaults to the canonical HLP address.
    registry_addresses : dict[str, list[str]] | None
        Optional mapping of asset_tag → [wallet_address, ...] from
        dune."swell-network".dataset_hyperliquid_top_users.
        If provided, also fetches TOP_TRADER_POSITIONING signals.
        Format: {"BTC": ["0xabc...", "0xdef..."], "ETH": [...]}
    http_client : httpx.AsyncClient | None
        Optional injected client for testing.
    direction_threshold_usd : float
        Net notional threshold below which direction = NEUTRAL.
    """

    SOURCE_NAME = "hlp"
    SOURCE_TYPE = SourceType.CHAIN_NATIVE
    TRUST_TIER = 1

    def __init__(
        self,
        vault_address: str = HLP_VAULT_ADDRESS,
        registry_addresses: dict[str, list[str]] | None = None,
        http_client: httpx.AsyncClient | None = None,
        direction_threshold_usd: float = HLP_DIRECTION_THRESHOLD_USD,
    ):
        self._vault = vault_address
        self._registry = registry_addresses or {}
        self._http = http_client
        self._threshold = direction_threshold_usd

    async def fetch(self, asset: str) -> list[SignalEvent]:
        signals: list[SignalEvent] = []
        now = datetime.now(timezone.utc)

        # --- HLP_SENTIMENT ---
        try:
            hlp_data = await self._fetch_clearinghouse(self._vault)
            net, gross, aum = _parse_asset_positions(hlp_data, asset)
            direction = _direction_from_hlp_net(net, self._threshold)
            confidence = _confidence_from_gross(gross)
            is_flat = gross == 0.0

            if is_flat:
                aum_b = aum / 1_000_000_000
                summary = (
                    f"HLP vault {asset}: no open position "
                    f"(confirmed flat, total AUM ${aum_b:.2f}B). "
                    f"Not a directional signal."
                )
            else:
                net_m = net / 1_000_000
                gross_m = gross / 1_000_000
                side = "short" if net < 0 else "long"
                summary = (
                    f"HLP vault {asset} position: net {net_m:+.1f}M USD ({side}), "
                    f"gross {gross_m:.1f}M USD. "
                    f"Traders are net {'long' if net < 0 else 'short'} → {direction}."
                )

            side = "short" if net < 0 else ("long" if net > 0 else "flat")

            signals.append(SignalEvent(
                source="hlp_vault",
                source_type=SourceType.CHAIN_NATIVE,
                asset=asset,
                signal_type=SignalType.HLP_SENTIMENT,
                value=net,
                direction=direction,
                confidence=confidence,
                trust_tier=self.TRUST_TIER,
                timestamp=now,
                ingested_at=now,
                position_relevant=not is_flat,  # flat = confirmed data, not a vote
                summary=summary,
                raw={
                    "vault": self._vault,
                    "net_notional_usd": net,
                    "gross_notional_usd": gross,
                    "account_value_usd": aum,
                    "side": side,
                    "is_flat": is_flat,
                },
            ))
        except Exception as exc:
            log.error("HLPSource: HLP_SENTIMENT fetch failed for %s: %s", asset, exc)

        # --- TOP_TRADER_POSITIONING (registry-based, optional) ---
        addrs = self._registry.get(asset.upper(), [])
        if addrs:
            try:
                signals += await self._fetch_top_trader_positioning(
                    asset, addrs, now
                )
            except Exception as exc:
                log.error(
                    "HLPSource: TOP_TRADER_POSITIONING failed for %s: %s", asset, exc
                )

        log.info("HLPSource fetched %d signals for %s", len(signals), asset)
        return signals

    async def _fetch_top_trader_positioning(
        self,
        asset: str,
        addresses: list[str],
        now: datetime,
    ) -> list[SignalEvent]:
        """
        Fetch clearinghouseState for each registry address and aggregate.
        Returns a single TOP_TRADER_POSITIONING SignalEvent.
        """
        total_net = 0.0
        total_gross = 0.0
        long_count = 0
        short_count = 0
        flat_count = 0

        for addr in addresses:
            try:
                data = await self._fetch_clearinghouse(addr)
                net, gross, _ = _parse_asset_positions(data, asset)
                total_net += net
                total_gross += gross
                if net > 0:
                    long_count += 1
                elif net < 0:
                    short_count += 1
                else:
                    flat_count += 1
            except Exception as exc:
                log.warning("HLPSource: registry addr %s failed: %s", addr, exc)

        direction = _direction_from_hlp_net(total_net, self._threshold)
        # Top trader signal is NOT contrarian — direction follows their position
        if total_net > self._threshold:
            direction = Direction.BULLISH
        elif total_net < -self._threshold:
            direction = Direction.BEARISH
        else:
            direction = Direction.NEUTRAL

        n = len(addresses)
        summary = (
            f"HL top traders {asset}: {long_count}/{n} long, "
            f"{short_count}/{n} short, {flat_count}/{n} flat. "
            f"Aggregate net {total_net / 1e6:+.1f}M USD → {direction}."
        )

        return [SignalEvent(
            source="hlp_registry",
            source_type=SourceType.CHAIN_NATIVE,
            asset=asset,
            signal_type=SignalType.TOP_TRADER_POSITIONING,
            value=total_net,
            direction=direction,
            confidence=0.85,
            trust_tier=self.TRUST_TIER,
            timestamp=now,
            ingested_at=now,
            summary=summary,
            raw={
                "addresses_queried": n,
                "long_count": long_count,
                "short_count": short_count,
                "flat_count": flat_count,
                "total_net_usd": total_net,
                "total_gross_usd": total_gross,
            },
        )]

    async def _fetch_clearinghouse(self, address: str) -> dict[str, Any]:
        """POST /info clearinghouseState for a single address."""
        payload = {"type": "clearinghouseState", "user": address}

        if self._http:
            resp = await self._http.post(
                HL_INFO_URL,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
        else:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    HL_INFO_URL,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

        resp.raise_for_status()
        return resp.json()
