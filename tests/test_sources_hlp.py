"""
tests/test_sources_hlp.py — HLPSource unit tests.

Uses httpx.MockTransport to avoid real network calls.
Tests HLP_SENTIMENT signal and TOP_TRADER_POSITIONING registry path.
"""
from __future__ import annotations

import json
import pytest
import httpx

import sys
sys.path.insert(0, "/tmp/signal_pipeline_test/src")

from signal_pipeline.schema import Direction, SignalEvent, SignalType, SourceType
from signal_pipeline.sources.hlp import (
    HLPSource,
    HLP_VAULT_ADDRESS,
    HLP_DIRECTION_THRESHOLD_USD,
    _parse_asset_positions,
    _direction_from_hlp_net,
    _confidence_from_gross,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _hl_response(positions: list[dict]) -> dict:
    """Build a minimal clearinghouseState response."""
    return {
        "marginSummary": {
            "accountValue": "500000000.0",
            "totalNtlPos": "100000000.0",
        },
        "assetPositions": [
            {"position": p, "type": "oneWay"}
            for p in positions
        ],
    }


def _make_position(coin: str, szi: float, position_value: float) -> dict:
    return {
        "coin": coin,
        "szi": str(szi),
        "entryPx": "67000.0",
        "positionValue": str(position_value),
        "unrealizedPnl": "0.0",
    }


def _mock_transport(response_body: dict) -> httpx.MockTransport:
    body = json.dumps(response_body).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body,
            headers={"content-type": "application/json"}
        )
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Unit tests: pure functions
# ---------------------------------------------------------------------------

def test_parse_asset_positions_short():
    data = _hl_response([_make_position("BTC", -12.5, 837_500.0)])
    net, gross, aum = _parse_asset_positions(data, "BTC")
    assert net == pytest.approx(-837_500.0)
    assert gross == pytest.approx(837_500.0)
    assert aum == pytest.approx(500_000_000.0)


def test_parse_asset_positions_long():
    data = _hl_response([_make_position("BTC", 5.0, 335_000.0)])
    net, gross, aum = _parse_asset_positions(data, "BTC")
    assert net == pytest.approx(335_000.0)
    assert gross == pytest.approx(335_000.0)
    assert aum == pytest.approx(500_000_000.0)


def test_parse_asset_positions_flat():
    data = _hl_response([_make_position("ETH", 10.0, 21_000.0)])
    net, gross, aum = _parse_asset_positions(data, "BTC")
    assert net == 0.0
    assert gross == 0.0
    assert aum == pytest.approx(500_000_000.0)


def test_parse_asset_positions_case_insensitive():
    data = _hl_response([_make_position("BTC", -5.0, 300_000.0)])
    net, gross, aum = _parse_asset_positions(data, "btc")
    assert net == pytest.approx(-300_000.0)


def test_direction_hlp_short_is_bearish():
    # HLP net short → traders net long → bearish (crowded)
    d = _direction_from_hlp_net(-10_000_000, HLP_DIRECTION_THRESHOLD_USD)
    assert d == Direction.BEARISH


def test_direction_hlp_long_is_bullish():
    # HLP net long → traders net short → bullish (squeeze setup)
    d = _direction_from_hlp_net(10_000_000, HLP_DIRECTION_THRESHOLD_USD)
    assert d == Direction.BULLISH


def test_direction_below_threshold_is_neutral():
    d = _direction_from_hlp_net(1_000_000, HLP_DIRECTION_THRESHOLD_USD)
    assert d == Direction.NEUTRAL


def test_confidence_scaling():
    assert _confidence_from_gross(50_000_000) == 0.90
    assert _confidence_from_gross(10_000_000) == 0.85
    assert _confidence_from_gross(1_000_000) == 0.80
    assert _confidence_from_gross(500_000) == 0.70


# ---------------------------------------------------------------------------
# Integration tests: HLPSource.fetch — HLP_SENTIMENT
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_hlp_sentiment_bearish():
    """HLP net short on BTC → traders long → BEARISH."""
    data = _hl_response([_make_position("BTC", -150.0, 10_050_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert len(hlp) == 1
    s: SignalEvent = hlp[0]
    assert s.source == "hlp_vault"
    assert s.source_type == SourceType.CHAIN_NATIVE
    assert s.asset == "BTC"
    assert s.signal_type == SignalType.HLP_SENTIMENT
    assert s.value == pytest.approx(-10_050_000.0)
    assert s.direction == Direction.BEARISH
    assert s.trust_tier == 1
    assert s.confidence == 0.85
    assert "net short" in s.summary or "short" in s.summary
    assert "Traders are net long" in s.summary


@pytest.mark.asyncio
async def test_hlp_sentiment_bullish():
    """HLP net long on BTC → traders short → BULLISH."""
    data = _hl_response([_make_position("BTC", 200.0, 13_400_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert hlp[0].direction == Direction.BULLISH
    assert hlp[0].value == pytest.approx(13_400_000.0)


@pytest.mark.asyncio
async def test_hlp_sentiment_neutral_below_threshold():
    """Small HLP position → NEUTRAL."""
    data = _hl_response([_make_position("BTC", 10.0, 670_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert hlp[0].direction == Direction.NEUTRAL
    assert hlp[0].confidence == 0.70


@pytest.mark.asyncio
async def test_hlp_sentiment_flat_position():
    """Asset not in HLP book → value=0, NEUTRAL, position_relevant=False."""
    data = _hl_response([_make_position("ETH", 50.0, 105_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert hlp[0].value == 0.0
    assert hlp[0].direction == Direction.NEUTRAL
    assert hlp[0].position_relevant is False
    assert "no open position" in hlp[0].summary
    assert "confirmed flat" in hlp[0].summary
    assert hlp[0].raw["is_flat"] is True


@pytest.mark.asyncio
async def test_hlp_sentiment_flat_includes_aum():
    """Flat summary includes vault AUM from marginSummary."""
    data = _hl_response([])  # empty positions, $500M AUM
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert hlp[0].raw["account_value_usd"] == pytest.approx(500_000_000.0)
    assert "0.50B" in hlp[0].summary  # $500M = $0.50B


@pytest.mark.asyncio
async def test_hlp_sentiment_raw_fields():
    """Raw dict must contain vault address, net/gross notional, side, is_flat, aum."""
    data = _hl_response([_make_position("BTC", -50.0, 3_350_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    raw = signals[0].raw
    assert raw["vault"] == HLP_VAULT_ADDRESS
    assert raw["net_notional_usd"] == pytest.approx(-3_350_000.0)
    assert raw["gross_notional_usd"] == pytest.approx(3_350_000.0)
    assert raw["side"] == "short"
    assert raw["is_flat"] is False
    assert raw["account_value_usd"] == pytest.approx(500_000_000.0)


@pytest.mark.asyncio
async def test_hlp_custom_vault_address():
    """Custom vault address is used in raw output."""
    custom = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    data = _hl_response([_make_position("ETH", 10.0, 21_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(vault_address=custom, http_client=client)

    signals = await source.fetch("ETH")
    assert signals[0].raw["vault"] == custom


@pytest.mark.asyncio
async def test_hlp_sentiment_fetch_error_returns_empty():
    """Network error → no signals returned, no exception raised."""
    def fail_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(fail_handler))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    assert signals == []


# ---------------------------------------------------------------------------
# Integration tests: TOP_TRADER_POSITIONING (registry path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_top_trader_positioning_bullish():
    """Registry addresses mostly long → TOP_TRADER_POSITIONING bullish."""
    responses = [
        _hl_response([_make_position("BTC", 5.0, 335_000.0)]),   # long $335k
        _hl_response([_make_position("BTC", 3.0, 201_000.0)]),   # long $201k
        _hl_response([_make_position("BTC", -1.0, 67_000.0)]),   # short $67k
    ]
    call_count = 0

    def multi_handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        body = json.dumps(responses[call_count % len(responses)]).encode()
        call_count += 1
        return httpx.Response(200, content=body,
                              headers={"content-type": "application/json"})

    registry = {"BTC": ["0xaaa", "0xbbb", "0xccc"]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(multi_handler))
    source = HLPSource(
        registry_addresses=registry,
        http_client=client,
        direction_threshold_usd=400_000,  # lower threshold for test
    )

    signals = await source.fetch("BTC")
    top = [s for s in signals if s.signal_type == SignalType.TOP_TRADER_POSITIONING]

    assert len(top) == 1
    s = top[0]
    assert s.source == "hlp_registry"
    assert s.source_type == SourceType.CHAIN_NATIVE
    assert s.trust_tier == 1
    assert s.confidence == 0.85
    # net = +335k + 201k - 67k = +469k > threshold 400k → BULLISH
    assert s.direction == Direction.BULLISH
    assert s.raw["long_count"] == 2
    assert s.raw["short_count"] == 1
    assert s.raw["addresses_queried"] == 3


@pytest.mark.asyncio
async def test_top_trader_positioning_not_present_without_registry():
    """No registry → only HLP_SENTIMENT signal, no TOP_TRADER_POSITIONING."""
    data = _hl_response([_make_position("BTC", -50.0, 3_350_000.0)])
    client = httpx.AsyncClient(transport=_mock_transport(data))
    source = HLPSource(http_client=client)

    signals = await source.fetch("BTC")
    top = [s for s in signals if s.signal_type == SignalType.TOP_TRADER_POSITIONING]
    hlp = [s for s in signals if s.signal_type == SignalType.HLP_SENTIMENT]

    assert top == []
    assert len(hlp) == 1
