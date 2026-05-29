"""Tests for assembler output format."""
import json
import pytest
from signal_pipeline.schema import SignalEvent, SignalType, Direction, SourceType
from signal_pipeline.assembler import assemble, assemble_json


def make_ranked_event(rank: int = 1, **kwargs) -> SignalEvent:
    defaults = dict(
        source="hyperliquid",
        source_type=SourceType.CHAIN_NATIVE,
        asset="BTC",
        signal_type=SignalType.FUNDING_RATE,
        value=-0.44,
        direction=Direction.BULLISH,
        trust_tier=1,
        confidence=1.0,
        score=0.85,
        rank=rank,
        summary="HL funding APR -0.44% — longs paid.",
        is_valid=True,
    )
    defaults.update(kwargs)
    return SignalEvent(**defaults)


def test_assemble_returns_dict():
    result = assemble([make_ranked_event()], asset="BTC")
    assert isinstance(result, dict)


def test_assemble_top_level_keys():
    result = assemble([make_ranked_event()], asset="BTC")
    for key in ("asset", "fetched_at", "signal_count", "signals", "token_estimate"):
        assert key in result


def test_assemble_signal_count():
    events = [make_ranked_event(rank=i) for i in range(1, 4)]
    result = assemble(events, asset="BTC")
    assert result["signal_count"] == 3


def test_assemble_signal_keys():
    result = assemble([make_ranked_event()], asset="BTC")
    signal = result["signals"][0]
    for key in ("rank", "signal_type", "source", "trust_tier", "direction",
                "value", "confidence", "score", "summary", "position_relevant"):
        assert key in signal


def test_assemble_excludes_validation_flags():
    e = make_ranked_event()
    e.flag("stale:3700s")
    result = assemble([e], asset="BTC")
    signal = result["signals"][0]
    assert "validation_flags" not in signal


def test_assemble_includes_chart_series_when_present():
    e = make_ranked_event()
    e.chart_series = {"type": "bar", "value": -0.44}
    result = assemble([e], asset="BTC")
    assert "chart_series" in result["signals"][0]


def test_assemble_omits_chart_series_when_none():
    e = make_ranked_event()
    result = assemble([e], asset="BTC")
    assert "chart_series" not in result["signals"][0]


def test_assemble_position_context_included():
    ctx = {"side": "long", "size_usd": 50000}
    result = assemble([make_ranked_event()], asset="BTC", position_context=ctx)
    assert result["position_context"] == ctx


def test_assemble_json_is_valid_json():
    payload = assemble_json([make_ranked_event()], asset="BTC")
    parsed = json.loads(payload)
    assert parsed["asset"] == "BTC"


def test_token_estimate_is_positive():
    result = assemble([make_ranked_event()], asset="BTC")
    assert result["token_estimate"] > 0


def test_empty_signals_valid_payload():
    result = assemble([], asset="ETH")
    assert result["signal_count"] == 0
    assert result["signals"] == []
