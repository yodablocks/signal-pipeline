"""
tests/test_model.py — Unit tests for the model layer.

Tests cover:
  - Individual vote functions (each signal type)
  - Options cluster averaging
  - Confluence measurement
  - Full score() integration
  - Edge cases: empty signals, no scoreable types, single signal
  - Direction thresholds (neutral band)
  - Explain output format
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from signal_pipeline.model import (
    ConfluenceResult,
    _collapse_funding_cluster,
    _collapse_oi_cluster,
    _collapse_options_cluster,
    _measure_confluence,
    _vote_funding_rate,
    _vote_iv_skew,
    _vote_liquidation,
    _vote_max_pain,
    _vote_net_premium,
    _vote_oi_trend,
    _vote_pc_ratio,
    score,
)
from signal_pipeline.schema import Direction, SignalEvent, SignalType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(
    signal_type: str,
    value: float,
    direction: str = Direction.NEUTRAL,
    confidence: float = 1.0,
    asset: str = "BTC",
    source: str = "test",
    trust_tier: int = 1,
    is_valid: bool = True,
) -> SignalEvent:
    return SignalEvent(
        asset=asset,
        signal_type=signal_type,
        value=value,
        direction=direction,
        confidence=confidence,
        source=source,
        trust_tier=trust_tier,
        timestamp=datetime.now(timezone.utc),
        is_valid=is_valid,
    )


# ---------------------------------------------------------------------------
# Vote functions — funding rate
# ---------------------------------------------------------------------------

class TestVoteFundingRate:
    # _vote_funding_rate now takes a float (panel median), not a SignalEvent.

    def test_strongly_positive_is_bearish(self):
        d, s, r = _vote_funding_rate(80.0)
        assert d == Direction.BEARISH
        assert s > 0.0
        assert "crowded longs" in r

    def test_strongly_negative_is_bullish(self):
        d, s, r = _vote_funding_rate(-80.0)
        assert d == Direction.BULLISH
        assert s > 0.0
        assert "crowded shorts" in r

    def test_near_zero_is_neutral(self):
        d, s, r = _vote_funding_rate(5.0)
        assert d == Direction.NEUTRAL
        assert s == 0.0

    def test_strength_capped_at_one(self):
        d, s, _ = _vote_funding_rate(999.0)
        assert s <= 1.0

    def test_threshold_boundary_positive(self):
        # Exactly at threshold (v == 20.0): strict >, so neutral.
        d, s, _ = _vote_funding_rate(20.0)
        assert d == Direction.NEUTRAL
        assert s == 0.0

    def test_threshold_boundary_negative(self):
        # Exactly at threshold (v == -20.0): strict <, so neutral.
        d, s, _ = _vote_funding_rate(-20.0)
        assert d == Direction.NEUTRAL
        assert s == 0.0


# ---------------------------------------------------------------------------
# Vote functions — OI trend
# ---------------------------------------------------------------------------

class TestVoteOITrend:
    def test_bullish_direction_passes_through(self):
        d, s, r = _vote_oi_trend(_event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, confidence=0.8))
        assert d == Direction.BULLISH
        assert s == pytest.approx(0.8)

    def test_bearish_direction_passes_through(self):
        d, s, _ = _vote_oi_trend(_event(SignalType.OI_DOMINANCE, 0.0, Direction.BEARISH, confidence=0.6))
        assert d == Direction.BEARISH

    def test_neutral_is_neutral(self):
        d, s, _ = _vote_oi_trend(_event(SignalType.OI_DOMINANCE, 0.0, Direction.NEUTRAL))
        assert d == Direction.NEUTRAL
        assert s == 0.0


# ---------------------------------------------------------------------------
# Vote functions — liquidation
# ---------------------------------------------------------------------------

class TestVoteLiquidation:
    def test_bearish_cluster_above_spot(self):
        d, s, r = _vote_liquidation(_event(SignalType.LIQUIDATION, 5_000_000, Direction.BEARISH))
        assert d == Direction.BEARISH
        assert s == pytest.approx(0.5)
        assert "above" in r

    def test_bullish_cluster_below_spot(self):
        d, s, r = _vote_liquidation(_event(SignalType.LIQUIDATION, 10_000_000, Direction.BULLISH))
        assert d == Direction.BULLISH
        assert s == pytest.approx(1.0)
        assert "below" in r

    def test_neutral_no_cluster(self):
        d, s, _ = _vote_liquidation(_event(SignalType.LIQUIDATION, 0.0, Direction.NEUTRAL))
        assert d == Direction.NEUTRAL
        assert s == 0.0

    def test_strength_capped(self):
        d, s, _ = _vote_liquidation(_event(SignalType.LIQUIDATION, 100_000_000, Direction.BEARISH))
        assert s <= 1.0


# ---------------------------------------------------------------------------
# Vote functions — P/C ratio
# ---------------------------------------------------------------------------

class TestVotePCRatio:
    def test_high_ratio_is_bearish(self):
        d, s, r = _vote_pc_ratio(_event("pc_ratio", 1.5))
        assert d == Direction.BEARISH
        assert s > 0.0
        assert "put-heavy" in r

    def test_low_ratio_is_bullish(self):
        d, s, r = _vote_pc_ratio(_event("pc_ratio", 0.5))
        assert d == Direction.BULLISH
        assert s > 0.0
        assert "call-heavy" in r

    def test_mid_range_is_neutral(self):
        d, s, _ = _vote_pc_ratio(_event("pc_ratio", 1.0))
        assert d == Direction.NEUTRAL
        assert s == 0.0


# ---------------------------------------------------------------------------
# Vote functions — IV skew
# ---------------------------------------------------------------------------

class TestVoteIVSkew:
    def test_positive_skew_is_bearish(self):
        d, s, r = _vote_iv_skew(_event("iv_skew", 8.0))
        assert d == Direction.BEARISH
        assert s > 0.0
        assert "put premium" in r

    def test_negative_skew_is_bullish(self):
        d, s, r = _vote_iv_skew(_event("iv_skew", -8.0))
        assert d == Direction.BULLISH
        assert "call premium" in r

    def test_flat_skew_is_neutral(self):
        d, s, _ = _vote_iv_skew(_event("iv_skew", 1.0))
        assert d == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# Vote functions — net premium
# ---------------------------------------------------------------------------

class TestVoteNetPremium:
    def test_bullish_direction_passes_through(self):
        d, s, r = _vote_net_premium(_event("net_premium", 3_000_000, Direction.BULLISH))
        assert d == Direction.BULLISH
        assert "call premium" in r

    def test_bearish_direction_passes_through(self):
        d, s, r = _vote_net_premium(_event("net_premium", -3_000_000, Direction.BEARISH))
        assert d == Direction.BEARISH
        assert "put premium" in r

    def test_neutral_flow(self):
        d, s, _ = _vote_net_premium(_event("net_premium", 0.0, Direction.NEUTRAL))
        assert d == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# Vote functions — max pain
# ---------------------------------------------------------------------------

class TestVoteMaxPain:
    def test_spot_well_above_is_bearish(self):
        d, s, r = _vote_max_pain(_event("max_pain_distance", 12.0))
        assert d == Direction.BEARISH
        assert s > 0.0
        assert "above max pain" in r

    def test_spot_well_below_is_bullish(self):
        d, s, r = _vote_max_pain(_event("max_pain_distance", -12.0))
        assert d == Direction.BULLISH
        assert "below max pain" in r

    def test_within_range_is_neutral(self):
        d, s, _ = _vote_max_pain(_event("max_pain_distance", 3.0))
        assert d == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# Options cluster averaging
# ---------------------------------------------------------------------------

class TestCollapseOptionsCluster:
    def test_unanimous_bullish(self):
        votes = [
            (Direction.BULLISH, 0.6, "call-heavy", "pc_ratio"),
            (Direction.BULLISH, 0.8, "call premium", "iv_skew"),
        ]
        d, s, r = _collapse_options_cluster(votes)
        assert d == Direction.BULLISH
        assert s == pytest.approx(0.7)  # avg of 0.6, 0.8 → 1.4/2 = 0.7

    def test_unanimous_bearish(self):
        votes = [
            (Direction.BEARISH, 0.5, "put-heavy", "pc_ratio"),
            (Direction.BEARISH, 0.5, "put premium", "iv_skew"),
        ]
        d, s, _ = _collapse_options_cluster(votes)
        assert d == Direction.BEARISH

    def test_split_returns_neutral(self):
        votes = [
            (Direction.BULLISH, 0.5, "x", "pc_ratio"),
            (Direction.BEARISH, 0.5, "y", "iv_skew"),
        ]
        d, s, _ = _collapse_options_cluster(votes)
        assert d == Direction.NEUTRAL

    def test_single_vote_preserved(self):
        votes = [(Direction.BULLISH, 0.9, "strong calls", "pc_ratio")]
        d, s, _ = _collapse_options_cluster(votes)
        assert d == Direction.BULLISH
        assert s == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Confluence
# ---------------------------------------------------------------------------

class TestMeasureConfluence:
    def _make_cs(self, direction):
        from signal_pipeline.model import ContributingSignal
        return ContributingSignal(
            signal_type="x", source="x", direction=direction,
            strength=0.5, weight=1.0, weighted_contribution=0.5, reason=""
        )

    def test_unanimous_bullish(self):
        cs = [self._make_cs(Direction.BULLISH)] * 4
        result = _measure_confluence(cs)
        assert result.bullish_count == 4
        assert result.bearish_count == 0
        assert result.agreement_ratio == 1.0

    def test_split_lowers_agreement(self):
        cs = [self._make_cs(Direction.BULLISH)] * 3 + [self._make_cs(Direction.BEARISH)]
        result = _measure_confluence(cs)
        assert result.agreement_ratio == pytest.approx(0.75)

    def test_all_neutral_gives_zero_agreement(self):
        cs = [self._make_cs(Direction.NEUTRAL)] * 3
        result = _measure_confluence(cs)
        assert result.agreement_ratio == 0.0


# ---------------------------------------------------------------------------
# Integration — score()
# ---------------------------------------------------------------------------

class TestScore:
    def _btc_events(self):
        """A set of events that should produce a clear bearish verdict."""
        return [
            _event(SignalType.FUNDING_RATE, 80.0),           # bearish: crowded longs
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BEARISH, confidence=0.7),
            _event(SignalType.LIQUIDATION, 8_000_000, Direction.BEARISH),
            _event("pc_ratio", 1.6),                         # bearish
            _event("iv_skew", 10.0),                         # bearish
        ]

    def test_bearish_confluence_produces_bearish(self):
        result = score(self._btc_events(), asset="BTC")
        assert result.direction == Direction.BEARISH
        assert result.confidence > 0.0
        assert result.asset == "BTC"

    def test_bullish_confluence_produces_bullish(self):
        events = [
            _event(SignalType.FUNDING_RATE, -80.0),          # bullish
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, confidence=0.8),
            _event(SignalType.LIQUIDATION, 8_000_000, Direction.BULLISH),
            _event("pc_ratio", 0.4),                         # bullish
            _event("iv_skew", -10.0),                        # bullish
        ]
        result = score(events, asset="BTC")
        assert result.direction == Direction.BULLISH

    def test_mixed_signals_reduce_confidence(self):
        # Two bearish vs two bullish → low confidence regardless of direction
        events = [
            _event(SignalType.FUNDING_RATE, 80.0),            # bearish
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, confidence=0.8),
            _event(SignalType.LIQUIDATION, 5_000_000, Direction.BULLISH),
            _event("pc_ratio", 1.5),                          # bearish
        ]
        result = score(events, asset="BTC")
        # Mixed confluence → confidence should be low
        assert result.confidence < 0.5

    def test_empty_events_returns_neutral(self):
        result = score([], asset="BTC")
        assert result.direction == Direction.NEUTRAL
        assert result.confidence == 0.0

    def test_invalid_events_excluded(self):
        events = [
            _event(SignalType.FUNDING_RATE, 80.0, is_valid=False),
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BEARISH, confidence=0.9),
        ]
        result = score(events, asset="BTC")
        # Only the OI trend event should count
        assert len(result.contributing_signals) == 1

    def test_wrong_asset_excluded(self):
        events = [
            _event(SignalType.FUNDING_RATE, 80.0, asset="ETH"),  # wrong asset
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BEARISH, asset="BTC"),
        ]
        result = score(events, asset="BTC")
        # Only the BTC OI event should count
        btc_signals = [cs for cs in result.contributing_signals]
        assert len(btc_signals) == 1

    def test_options_cluster_counted_once(self):
        """Four options signals should produce a single 'options_cluster' entry."""
        events = [
            _event("pc_ratio", 1.6),
            _event("iv_skew", 8.0),
            _event("net_premium", -2_000_000, Direction.BEARISH),
            _event("max_pain_distance", 10.0),
        ]
        result = score(events, asset="BTC")
        cluster_signals = [cs for cs in result.contributing_signals if cs.signal_type == "options_cluster"]
        assert len(cluster_signals) == 1

    def test_confidence_range(self):
        result = score(self._btc_events(), asset="BTC")
        assert 0.0 <= result.confidence <= 1.0

    def test_contributing_signals_populated(self):
        result = score(self._btc_events(), asset="BTC")
        assert len(result.contributing_signals) > 0
        for cs in result.contributing_signals:
            assert cs.direction in (Direction.BULLISH, Direction.BEARISH, Direction.NEUTRAL)
            assert 0.0 <= cs.strength <= 1.0

    def test_explanation_is_non_empty(self):
        result = score(self._btc_events(), asset="BTC")
        assert len(result.explanation) > 0
        assert "BTC" in result.explanation

    def test_to_dict_keys(self):
        result = score(self._btc_events(), asset="BTC")
        d = result.to_dict()
        assert "direction" in d
        assert "confidence" in d
        assert "confluence" in d
        assert "contributing_signals" in d
        assert "explanation" in d
        assert "weight_disclaimer" in d

    def test_explain_output_contains_signals(self):
        result = score(self._btc_events(), asset="BTC")
        explain_str = result.explain()
        assert "Direction" in explain_str
        assert "Confidence" in explain_str
        assert "UNVALIDATED" in explain_str
        assert len(explain_str.splitlines()) > 5

    def test_neutral_band(self):
        """Near-zero raw score lands in neutral, not marginal direction."""
        # Single low-confidence neutral OI signal
        events = [
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.NEUTRAL, confidence=0.1),
        ]
        result = score(events, asset="BTC")
        assert result.direction == Direction.NEUTRAL






# ---------------------------------------------------------------------------
# OI cluster collapse
# ---------------------------------------------------------------------------

class TestCollapseOICluster:
    def _oi_events(self, directions, asset="BTC"):
        return [
            _event(SignalType.OI_DOMINANCE, 0.0, direction=d, confidence=0.8,
                   asset=asset, source=f"venue_{i}")
            for i, d in enumerate(directions)
        ]

    def test_majority_bullish(self):
        events = self._oi_events([Direction.BULLISH, Direction.BULLISH, Direction.NEUTRAL])
        d, s, r, sources = _collapse_oi_cluster(events)
        assert d == Direction.BULLISH
        assert s > 0.0
        assert "bullish" in r

    def test_majority_bearish(self):
        events = self._oi_events([Direction.BEARISH, Direction.BEARISH, Direction.NEUTRAL])
        d, s, r, sources = _collapse_oi_cluster(events)
        assert d == Direction.BEARISH

    def test_split_is_neutral(self):
        events = self._oi_events([Direction.BULLISH, Direction.BEARISH])
        d, s, r, sources = _collapse_oi_cluster(events)
        assert d == Direction.NEUTRAL
        assert s == 0.0

    def test_all_neutral_is_neutral(self):
        events = self._oi_events([Direction.NEUTRAL, Direction.NEUTRAL, Direction.NEUTRAL])
        d, s, r, sources = _collapse_oi_cluster(events)
        assert d == Direction.NEUTRAL

    def test_sources_label_populated(self):
        events = self._oi_events([Direction.BULLISH, Direction.NEUTRAL])
        _, _, _, sources = _collapse_oi_cluster(events)
        assert len(sources) > 0

    def test_strength_capped_at_one(self):
        events = self._oi_events([Direction.BULLISH] * 5)
        _, s, _, _ = _collapse_oi_cluster(events)
        assert s <= 1.0


class TestScoreOICluster:
    def test_oi_cluster_counted_once(self):
        events = [
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, source="hl"),
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.NEUTRAL, source="paradex"),
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, source="apex"),
        ]
        result = score(events, asset="BTC")
        oi = [cs for cs in result.contributing_signals if cs.signal_type == "oi_cluster"]
        assert len(oi) == 1

    def test_majority_bullish_venues_produces_bullish(self):
        events = [
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, confidence=0.9, source="hl"),
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.BULLISH, confidence=0.8, source="paradex"),
            _event(SignalType.OI_DOMINANCE, 0.0, Direction.NEUTRAL, source="apex"),
        ]
        result = score(events, asset="BTC")
        oi = next(cs for cs in result.contributing_signals if cs.signal_type == "oi_cluster")
        assert oi.direction == Direction.BULLISH


# ---------------------------------------------------------------------------
# Funding cluster collapse
# ---------------------------------------------------------------------------

class TestCollapseFundingCluster:
    def _funding_events(self, values, asset="BTC"):
        return [
            _event(SignalType.FUNDING_RATE, v, asset=asset, source=f"venue_{i}")
            for i, v in enumerate(values)
        ]

    def test_neutral_panel_is_neutral(self):
        from signal_pipeline.model import _collapse_funding_cluster
        events = self._funding_events([0.1, 0.2, 0.1, 0.0, 0.1])
        d, s, r, sources = _collapse_funding_cluster(events)
        assert d == Direction.NEUTRAL
        assert s == 0.0

    def test_high_median_is_bearish(self):
        from signal_pipeline.model import _collapse_funding_cluster
        events = self._funding_events([10.0, 25.0, 30.0, 22.0, 28.0])
        d, s, r, sources = _collapse_funding_cluster(events)
        assert d == Direction.BEARISH
        assert s > 0.0
        assert "median" in r

    def test_outlier_does_not_drive_direction(self):
        from signal_pipeline.model import _collapse_funding_cluster
        # GRVT outlier at 11% — rest of panel near zero
        # median ~ 0.1% -> neutral
        events = self._funding_events([11.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.0])
        d, s, r, sources = _collapse_funding_cluster(events)
        assert d == Direction.NEUTRAL

    def test_reason_includes_range(self):
        from signal_pipeline.model import _collapse_funding_cluster
        events = self._funding_events([0.1, 11.0, 0.2])
        d, s, r, sources = _collapse_funding_cluster(events)
        assert "range" in r

    def test_sources_label_populated(self):
        from signal_pipeline.model import _collapse_funding_cluster
        events = self._funding_events([0.1, 0.2])
        _, _, _, sources = _collapse_funding_cluster(events)
        assert len(sources) > 0


class TestScoreFundingCluster:
    def test_funding_cluster_counted_once(self):
        events = [
            _event(SignalType.FUNDING_RATE, 0.1, asset="BTC", source="hl"),
            _event(SignalType.FUNDING_RATE, 0.2, asset="BTC", source="paradex"),
            _event(SignalType.FUNDING_RATE, 0.1, asset="BTC", source="apex"),
            _event(SignalType.FUNDING_RATE, 11.0, asset="BTC", source="grvt"),
        ]
        result = score(events, asset="BTC")
        cluster = [cs for cs in result.contributing_signals if cs.signal_type == "funding_cluster"]
        assert len(cluster) == 1

    def test_grvt_outlier_does_not_produce_bearish(self):
        events = [
            _event(SignalType.FUNDING_RATE, 11.0, asset="BTC", source="grvt"),
            _event(SignalType.FUNDING_RATE, 0.1, asset="BTC", source="hl"),
            _event(SignalType.FUNDING_RATE, 0.2, asset="BTC", source="paradex"),
            _event(SignalType.FUNDING_RATE, 0.1, asset="BTC", source="apex"),
        ]
        result = score(events, asset="BTC")
        cluster = next(cs for cs in result.contributing_signals if cs.signal_type == "funding_cluster")
        assert cluster.direction == Direction.NEUTRAL


# ---------------------------------------------------------------------------
# ConfluenceResult
# ---------------------------------------------------------------------------

class TestConfluenceResult:
    def test_dominant_direction_bullish(self):
        c = ConfluenceResult(bullish_count=3, bearish_count=1, neutral_count=0, total_sources=4)
        assert c.dominant_direction == Direction.BULLISH

    def test_dominant_direction_tie_is_neutral(self):
        c = ConfluenceResult(bullish_count=2, bearish_count=2, neutral_count=0, total_sources=4)
        assert c.dominant_direction == Direction.NEUTRAL

    def test_agreement_ratio_all_agree(self):
        c = ConfluenceResult(bullish_count=4, bearish_count=0, neutral_count=0, total_sources=4)
        assert c.agreement_ratio == 1.0

    def test_agreement_ratio_no_signal(self):
        c = ConfluenceResult()
        assert c.agreement_ratio == 0.0
