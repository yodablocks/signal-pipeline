"""
tests/test_sources_liquidation.py — Unit tests for LiquidationSource.

All tests are offline — no SQLite reads, no HTTP calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_pipeline.schema import Direction, SignalType
from signal_pipeline.sources.liquidation import (
    LiquidationSource,
    _build_clusters,
    _bucket,
    _nearest_cluster,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fill(px: float, notional: float, side: str = "long", ts: int = 1_700_000_000_000) -> dict:
    return {"px": px, "notional": notional, "side": side, "ts": ts}


# ---------------------------------------------------------------------------
# _bucket
# ---------------------------------------------------------------------------

class TestBucket:
    def test_rounds_down_to_nearest_500(self):
        assert _bucket(65_300, 500) == 65_000

    def test_exact_boundary(self):
        assert _bucket(65_000, 500) == 65_000

    def test_just_above_boundary(self):
        assert _bucket(65_001, 500) == 65_000

    def test_just_below_next(self):
        assert _bucket(65_499, 500) == 65_000


# ---------------------------------------------------------------------------
# _build_clusters
# ---------------------------------------------------------------------------

class TestBuildClusters:
    def test_groups_fills_by_bucket(self):
        fills = [
            _fill(65_100, 1_000_000),
            _fill(65_200, 500_000),   # same bucket as above
            _fill(66_000, 2_000_000),
        ]
        clusters = _build_clusters(fills, 500)
        assert clusters[65_000] == pytest.approx(1_500_000)
        assert clusters[66_000] == pytest.approx(2_000_000)

    def test_empty_fills_returns_empty(self):
        assert _build_clusters([], 500) == {}

    def test_bad_fill_skipped(self):
        fills = [{"px": "bad", "notional": 1000}, _fill(65_000, 500_000)]
        clusters = _build_clusters(fills, 500)
        assert len(clusters) == 1

    def test_missing_keys_skipped(self):
        fills = [{"notional": 1000}, _fill(65_000, 500_000)]
        clusters = _build_clusters(fills, 500)
        assert len(clusters) == 1


# ---------------------------------------------------------------------------
# _nearest_cluster
# ---------------------------------------------------------------------------

class TestNearestCluster:
    def _clusters(self) -> dict[int, float]:
        return {
            60_000: 2_000_000,  # 5k below spot — further
            66_000: 3_000_000,  # 1k above spot — closer
            90_000: 5_000_000,  # too far
        }

    def test_above_spot_is_bearish(self):
        # 66k cluster is closer above spot (65k) than 60k is below
        result = _nearest_cluster(self._clusters(), spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is not None
        direction, usd, price = result
        assert direction == Direction.BEARISH
        assert usd == pytest.approx(3_000_000)

    def test_below_spot_is_bullish(self):
        clusters = {62_000: 3_000_000}
        result = _nearest_cluster(clusters, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is not None
        direction, usd, _ = result
        assert direction == Direction.BULLISH

    def test_returns_closer_cluster(self):
        # above is closer (2k away) vs below (5k away)
        clusters = {
            63_000: 2_000_000,   # 2k below spot
            66_500: 2_000_000,   # 1.5k above spot — closer
        }
        result = _nearest_cluster(clusters, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is not None
        direction, _, _ = result
        assert direction == Direction.BEARISH

    def test_too_far_returns_none(self):
        clusters = {90_000: 5_000_000}
        result = _nearest_cluster(clusters, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=5.0)
        assert result is None

    def test_below_min_notional_returns_none(self):
        clusters = {66_000: 100_000}  # too small
        result = _nearest_cluster(clusters, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is None

    def test_empty_clusters_returns_none(self):
        result = _nearest_cluster({}, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is None

    def test_usd_notional_returned_correctly(self):
        clusters = {67_000: 4_500_000}
        result = _nearest_cluster(clusters, spot=65_000, bucket_size=500,
                                   min_usd=500_000, proximity_pct=10.0)
        assert result is not None
        _, usd, _ = result
        assert usd == pytest.approx(4_500_000)


# ---------------------------------------------------------------------------
# LiquidationSource.supported_assets
# ---------------------------------------------------------------------------

class TestSupportedAssets:
    def test_btc_supported(self):
        assert "BTC" in LiquidationSource().supported_assets()

    def test_eth_supported(self):
        assert "ETH" in LiquidationSource().supported_assets()

    def test_hype_supported(self):
        assert "HYPE" in LiquidationSource().supported_assets()

    def test_doge_not_supported(self):
        assert "DOGE" not in LiquidationSource().supported_assets()


# ---------------------------------------------------------------------------
# LiquidationSource.fetch — happy paths
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_liquidation(monkeypatch):
    import signal_pipeline.sources.liquidation as mod

    mod._HEATMAP_AVAILABLE = True
    mod.fetch_local_liquidations = MagicMock(return_value=[
        _fill(66_000, 3_000_000, "short"),  # 1k above spot (65k) — closer
        _fill(66_200, 1_500_000, "short"),  # same cluster above
        _fill(60_000, 2_000_000, "long"),   # 5k below spot — further
    ])
    return mod


@pytest.fixture
def mock_spot(monkeypatch):
    import signal_pipeline.sources.liquidation as mod
    mod._fetch_spot = AsyncMock(return_value=65_000.0)
    return mod


class TestLiquidationSourceFetch:
    @pytest.mark.asyncio
    async def test_returns_one_signal(self, mock_liquidation, mock_spot):
        source = LiquidationSource()
        events = await source.fetch("BTC")
        assert len(events) == 1

    @pytest.mark.asyncio
    async def test_signal_type_correct(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert events[0].signal_type == SignalType.LIQUIDATION

    @pytest.mark.asyncio
    async def test_trust_tier_1(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert events[0].trust_tier == 1

    @pytest.mark.asyncio
    async def test_source_name(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert events[0].source == "hl_liquidation"

    @pytest.mark.asyncio
    async def test_asset_correct(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert events[0].asset == "BTC"

    @pytest.mark.asyncio
    async def test_cluster_above_spot_is_bearish(self, mock_liquidation, mock_spot):
        # fills have a larger cluster above spot (4.5M) vs below (2M)
        events = await LiquidationSource().fetch("BTC")
        assert events[0].direction == Direction.BEARISH

    @pytest.mark.asyncio
    async def test_value_is_usd_notional(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert events[0].value > 0

    @pytest.mark.asyncio
    async def test_summary_non_empty(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert len(events[0].summary) > 0

    @pytest.mark.asyncio
    async def test_raw_contains_spot(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert "spot" in events[0].raw
        assert events[0].raw["spot"] == pytest.approx(65_000.0)

    @pytest.mark.asyncio
    async def test_confidence_set(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("BTC")
        assert 0.0 < events[0].confidence <= 1.0


# ---------------------------------------------------------------------------
# LiquidationSource.fetch — error handling
# ---------------------------------------------------------------------------

class TestLiquidationSourceErrors:
    @pytest.mark.asyncio
    async def test_unsupported_asset_returns_empty(self, mock_liquidation, mock_spot):
        events = await LiquidationSource().fetch("DOGE")
        assert events == []

    @pytest.mark.asyncio
    async def test_heatmap_unavailable_returns_empty(self, mock_liquidation, mock_spot):
        mock_liquidation._HEATMAP_AVAILABLE = False
        events = await LiquidationSource().fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_empty_fills_returns_empty(self, mock_liquidation, mock_spot):
        mock_liquidation.fetch_local_liquidations.return_value = []
        events = await LiquidationSource().fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_db_error_returns_empty(self, mock_liquidation, mock_spot):
        mock_liquidation.fetch_local_liquidations.side_effect = Exception("db locked")
        events = await LiquidationSource().fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_spot_unavailable_returns_empty(self, mock_liquidation, mock_spot):
        mock_spot._fetch_spot = AsyncMock(return_value=None)
        events = await LiquidationSource().fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_no_cluster_near_spot_returns_neutral(self, mock_liquidation, mock_spot):
        # All fills very far from spot
        import signal_pipeline.sources.liquidation as mod
        mod.fetch_local_liquidations.return_value = [
            _fill(90_000, 5_000_000, "short"),  # way above spot (65k)
        ]
        events = await LiquidationSource().fetch("BTC")
        assert len(events) == 1
        assert events[0].direction == Direction.NEUTRAL

    @pytest.mark.asyncio
    async def test_below_min_notional_returns_neutral(self, mock_liquidation, mock_spot):
        # Cluster too small
        import signal_pipeline.sources.liquidation as mod
        mod.fetch_local_liquidations.return_value = [
            _fill(66_000, 10_000, "short"),   # tiny notional
        ]
        events = await LiquidationSource().fetch("BTC")
        assert len(events) == 1
        assert events[0].direction == Direction.NEUTRAL
