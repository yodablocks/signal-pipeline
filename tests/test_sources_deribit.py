"""
tests/test_sources_deribit.py — Unit tests for DeribitSource.

Tests are fully offline — no Deribit API calls.
The deribit-options-flow dependency (fetcher.py + processor.py) is mocked.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from signal_pipeline.schema import Direction, SignalType
from signal_pipeline.sources.deribit import (
    DeribitSource,
    _direction_from_max_pain_distance,
    _direction_from_net_premium,
    _direction_from_pc,
    _direction_from_skew,
)


# ---------------------------------------------------------------------------
# Canonical snapshot fixture
# Mirrors the output shape of processor.build_signals()
# ---------------------------------------------------------------------------

def _snapshot(
    pc_ratio_oi: float = 0.85,
    pc_ratio_vol: float = 0.80,
    iv_skew: float = 0.05,          # decimal — 5 vol points
    net_premium: float = 1_500_000,
    call_premium: float = 4_000_000,
    put_premium: float = 2_500_000,
    atm_iv: float = 0.70,
    max_pain: float = 70_000.0,
    total_call_oi: float = 50_000.0,
    total_put_oi: float = 42_500.0,
    spot: float = 75_000.0,
) -> dict:
    return {
        "pc_ratio_oi":   pc_ratio_oi,
        "pc_ratio_vol":  pc_ratio_vol,
        "iv_skew":       iv_skew,
        "net_premium":   net_premium,
        "call_premium":  call_premium,
        "put_premium":   put_premium,
        "atm_iv":        atm_iv,
        "max_pain":      max_pain,
        "total_call_oi": total_call_oi,
        "total_put_oi":  total_put_oi,
        "spot_price":    spot,
        "ts":            1_700_000_000_000,
        "gamma_walls":   [],
        "term_structure": [],
    }


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------

class TestDirectionFromPC:
    def test_above_threshold_bearish(self):
        assert _direction_from_pc(1.5) == Direction.BEARISH

    def test_below_threshold_bullish(self):
        assert _direction_from_pc(0.5) == Direction.BULLISH

    def test_mid_range_neutral(self):
        assert _direction_from_pc(1.0) == Direction.NEUTRAL

    def test_exact_upper_threshold(self):
        # 1.2 is the boundary — strictly > 1.2 is bearish
        assert _direction_from_pc(1.2) == Direction.NEUTRAL

    def test_exact_lower_threshold(self):
        # 0.7 is the boundary — strictly < 0.7 is bullish
        assert _direction_from_pc(0.7) == Direction.NEUTRAL


class TestDirectionFromSkew:
    def test_positive_skew_bearish(self):
        assert _direction_from_skew(8.0) == Direction.BEARISH

    def test_negative_skew_bullish(self):
        assert _direction_from_skew(-8.0) == Direction.BULLISH

    def test_flat_skew_neutral(self):
        assert _direction_from_skew(1.0) == Direction.NEUTRAL

    def test_exact_upper_threshold(self):
        assert _direction_from_skew(3.0) == Direction.NEUTRAL

    def test_exact_lower_threshold(self):
        assert _direction_from_skew(-3.0) == Direction.NEUTRAL


class TestDirectionFromNetPremium:
    def test_positive_is_bullish(self):
        assert _direction_from_net_premium(1_000_000) == Direction.BULLISH

    def test_negative_is_bearish(self):
        assert _direction_from_net_premium(-500_000) == Direction.BEARISH

    def test_zero_is_neutral(self):
        assert _direction_from_net_premium(0.0) == Direction.NEUTRAL


class TestDirectionFromMaxPainDistance:
    def test_spot_well_above_is_bearish(self):
        assert _direction_from_max_pain_distance(10.0) == Direction.BEARISH

    def test_spot_well_below_is_bullish(self):
        assert _direction_from_max_pain_distance(-10.0) == Direction.BULLISH

    def test_spot_close_is_neutral(self):
        assert _direction_from_max_pain_distance(2.0) == Direction.NEUTRAL

    def test_exact_upper_threshold(self):
        assert _direction_from_max_pain_distance(5.0) == Direction.NEUTRAL

    def test_exact_lower_threshold(self):
        assert _direction_from_max_pain_distance(-5.0) == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# DeribitSource.supported_assets()
# ---------------------------------------------------------------------------

class TestSupportedAssets:
    def test_btc_supported(self):
        assert "BTC" in DeribitSource().supported_assets()

    def test_eth_supported(self):
        assert "ETH" in DeribitSource().supported_assets()

    def test_hype_not_supported(self):
        assert "HYPE" not in DeribitSource().supported_assets()

    def test_sol_not_supported(self):
        assert "SOL" not in DeribitSource().supported_assets()


# ---------------------------------------------------------------------------
# DeribitSource.fetch() — happy path
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_deribit(monkeypatch):
    """Patch the deribit-options-flow imports used inside deribit.py."""
    import signal_pipeline.sources.deribit as mod

    mod._DERIBIT_AVAILABLE = True
    mod.fetch_options_summary = MagicMock(return_value=[{"instrument_name": "BTC-stub"}])
    mod.fetch_index_price = MagicMock(return_value=75_000.0)
    mod.build_signals = MagicMock(return_value=_snapshot())
    return mod


class TestDeribitSourceFetch:
    @pytest.mark.asyncio
    async def test_returns_four_signals(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert len(events) == 4

    @pytest.mark.asyncio
    async def test_signal_types_correct(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        types = {e.signal_type for e in events}
        assert types == {
            SignalType.PC_RATIO,
            SignalType.IV_SKEW,
            SignalType.NET_PREMIUM,
            SignalType.MAX_PAIN_DISTANCE,
        }

    @pytest.mark.asyncio
    async def test_all_signals_for_correct_asset(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(e.asset == "BTC" for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_trust_tier_2(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(e.trust_tier == 2 for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_source_is_deribit(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(e.source == "deribit" for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_valid(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(e.is_valid for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_have_summary(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(len(e.summary) > 0 for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_have_raw(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(len(e.raw) > 0 for e in events)

    @pytest.mark.asyncio
    async def test_all_signals_have_timestamp(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert all(e.timestamp is not None for e in events)

    # ── PC Ratio signal specifics ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_pc_ratio_value(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        pc = next(e for e in events if e.signal_type == SignalType.PC_RATIO)
        assert pc.value == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_pc_ratio_neutral_at_085(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        pc = next(e for e in events if e.signal_type == SignalType.PC_RATIO)
        assert pc.direction == Direction.NEUTRAL

    @pytest.mark.asyncio
    async def test_pc_ratio_bearish_when_high(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(pc_ratio_oi=1.5)
        source = DeribitSource()
        events = await source.fetch("BTC")
        pc = next(e for e in events if e.signal_type == SignalType.PC_RATIO)
        assert pc.direction == Direction.BEARISH

    @pytest.mark.asyncio
    async def test_pc_ratio_bullish_when_low(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(pc_ratio_oi=0.5)
        source = DeribitSource()
        events = await source.fetch("BTC")
        pc = next(e for e in events if e.signal_type == SignalType.PC_RATIO)
        assert pc.direction == Direction.BULLISH

    # ── IV Skew signal specifics ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_iv_skew_converted_to_pp(self, mock_deribit):
        # processor returns 0.05 decimal → adapter should produce 5.0 pp
        mock_deribit.build_signals.return_value = _snapshot(iv_skew=0.05)
        source = DeribitSource()
        events = await source.fetch("BTC")
        skew = next(e for e in events if e.signal_type == SignalType.IV_SKEW)
        assert skew.value == pytest.approx(5.0)

    @pytest.mark.asyncio
    async def test_iv_skew_bearish_when_positive_pp(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(iv_skew=0.10)  # 10pp
        source = DeribitSource()
        events = await source.fetch("BTC")
        skew = next(e for e in events if e.signal_type == SignalType.IV_SKEW)
        assert skew.direction == Direction.BEARISH

    @pytest.mark.asyncio
    async def test_iv_skew_bullish_when_negative_pp(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(iv_skew=-0.08)  # -8pp
        source = DeribitSource()
        events = await source.fetch("BTC")
        skew = next(e for e in events if e.signal_type == SignalType.IV_SKEW)
        assert skew.direction == Direction.BULLISH

    @pytest.mark.asyncio
    async def test_iv_skew_neutral_when_flat(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(iv_skew=0.01)  # 1pp
        source = DeribitSource()
        events = await source.fetch("BTC")
        skew = next(e for e in events if e.signal_type == SignalType.IV_SKEW)
        assert skew.direction == Direction.NEUTRAL

    # ── Net Premium signal specifics ───────────────────────────────────────

    @pytest.mark.asyncio
    async def test_net_premium_value(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        prem = next(e for e in events if e.signal_type == SignalType.NET_PREMIUM)
        assert prem.value == pytest.approx(1_500_000.0)

    @pytest.mark.asyncio
    async def test_net_premium_bullish_when_positive(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("BTC")
        prem = next(e for e in events if e.signal_type == SignalType.NET_PREMIUM)
        assert prem.direction == Direction.BULLISH

    @pytest.mark.asyncio
    async def test_net_premium_bearish_when_negative(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot(net_premium=-2_000_000)
        source = DeribitSource()
        events = await source.fetch("BTC")
        prem = next(e for e in events if e.signal_type == SignalType.NET_PREMIUM)
        assert prem.direction == Direction.BEARISH

    # ── Max Pain Distance signal specifics ────────────────────────────────

    @pytest.mark.asyncio
    async def test_max_pain_distance_calculation(self, mock_deribit):
        # spot=75000, max_pain=70000 → distance = (75000-70000)/75000*100 = 6.67%
        source = DeribitSource()
        events = await source.fetch("BTC")
        pain = next(e for e in events if e.signal_type == SignalType.MAX_PAIN_DISTANCE)
        assert pain.value == pytest.approx((75_000 - 70_000) / 75_000 * 100, rel=1e-3)

    @pytest.mark.asyncio
    async def test_max_pain_bearish_when_spot_above(self, mock_deribit):
        # spot well above max pain → bearish gravity
        source = DeribitSource()
        events = await source.fetch("BTC")
        pain = next(e for e in events if e.signal_type == SignalType.MAX_PAIN_DISTANCE)
        assert pain.direction == Direction.BEARISH

    @pytest.mark.asyncio
    async def test_max_pain_bullish_when_spot_below(self, mock_deribit):
        # max_pain=82000, spot=75000 → distance = (75000-82000)/75000*100 = -9.3%
        mock_deribit.build_signals.return_value = _snapshot(max_pain=82_000)
        source = DeribitSource()
        events = await source.fetch("BTC")
        pain = next(e for e in events if e.signal_type == SignalType.MAX_PAIN_DISTANCE)
        assert pain.direction == Direction.BULLISH

    @pytest.mark.asyncio
    async def test_max_pain_neutral_when_near(self, mock_deribit):
        # max_pain=74000, spot=75000 → distance ≈ 1.3% (within ±5% threshold)
        mock_deribit.build_signals.return_value = _snapshot(max_pain=74_000)
        source = DeribitSource()
        events = await source.fetch("BTC")
        pain = next(e for e in events if e.signal_type == SignalType.MAX_PAIN_DISTANCE)
        assert pain.direction == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# DeribitSource.fetch() — error handling
# ---------------------------------------------------------------------------

class TestDeribitSourceErrors:
    @pytest.mark.asyncio
    async def test_unsupported_asset_returns_empty(self, mock_deribit):
        source = DeribitSource()
        events = await source.fetch("HYPE")
        assert events == []

    @pytest.mark.asyncio
    async def test_dependency_unavailable_returns_empty(self, mock_deribit):
        mock_deribit._DERIBIT_AVAILABLE = False
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self, mock_deribit):
        # Both fetches run via asyncio.to_thread — mock the underlying sync fn
        mock_deribit.fetch_options_summary.side_effect = RuntimeError("timeout")
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_empty_summaries_returns_empty(self, mock_deribit):
        mock_deribit.fetch_options_summary.return_value = []
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_zero_spot_returns_empty(self, mock_deribit):
        mock_deribit.fetch_index_price.return_value = 0.0
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_build_signals_error_returns_empty(self, mock_deribit):
        mock_deribit.build_signals.side_effect = ValueError("bad data")
        source = DeribitSource()
        events = await source.fetch("BTC")
        assert events == []

    @pytest.mark.asyncio
    async def test_eth_supported(self, mock_deribit):
        mock_deribit.build_signals.return_value = _snapshot()
        source = DeribitSource()
        events = await source.fetch("ETH")
        assert len(events) == 4
        assert all(e.asset == "ETH" for e in events)


# ---------------------------------------------------------------------------
# Confidence values
# ---------------------------------------------------------------------------

class TestConfidenceValues:
    @pytest.mark.asyncio
    async def test_pc_ratio_confidence(self, mock_deribit):
        events = await DeribitSource().fetch("BTC")
        pc = next(e for e in events if e.signal_type == SignalType.PC_RATIO)
        assert pc.confidence == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_iv_skew_confidence(self, mock_deribit):
        events = await DeribitSource().fetch("BTC")
        skew = next(e for e in events if e.signal_type == SignalType.IV_SKEW)
        assert skew.confidence == pytest.approx(0.85)

    @pytest.mark.asyncio
    async def test_net_premium_confidence(self, mock_deribit):
        events = await DeribitSource().fetch("BTC")
        prem = next(e for e in events if e.signal_type == SignalType.NET_PREMIUM)
        assert prem.confidence == pytest.approx(0.80)

    @pytest.mark.asyncio
    async def test_max_pain_confidence(self, mock_deribit):
        events = await DeribitSource().fetch("BTC")
        pain = next(e for e in events if e.signal_type == SignalType.MAX_PAIN_DISTANCE)
        assert pain.confidence == pytest.approx(0.75)
