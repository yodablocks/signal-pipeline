"""
tests/test_sources_dune.py — DuneSource unit tests.

Uses httpx.MockTransport to avoid real network calls.
Monkeypatches QUERY_IDS so tests run without live query IDs.
"""

import json
import pytest
import httpx

from signal_pipeline.schema import Direction, SignalEvent, SignalType, SourceType
from signal_pipeline.sources import dune as dune_module
from signal_pipeline.sources.dune import DuneSource


def _mock_dune_response(rows: list[dict]) -> httpx.Response:
    body = json.dumps({"result": {"rows": rows}})
    return httpx.Response(200, content=body.encode(), headers={"content-type": "application/json"})


def _make_transport(rows: list[dict]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return _mock_dune_response(rows)
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# whale_flow
# ---------------------------------------------------------------------------

WHALE_FLOW_ROW = {
    "asset": "BTC",
    "value": 12500000.0,
    "direction_hint": "bullish",
    "summary": "Whale bridge flow (>$500k, 24h): net +12.50M USD. Deposits: $18.0M (12 txs). Withdrawals: $5.5M (3 txs).",
}


@pytest.mark.asyncio
async def test_whale_flow_signal_event_mapping(monkeypatch):
    monkeypatch.setitem(dune_module.QUERY_IDS, SignalType.WHALE_FLOW, 9999999)

    transport = _make_transport([WHALE_FLOW_ROW])
    client = httpx.AsyncClient(transport=transport)
    source = DuneSource(api_key="test-key", http_client=client)

    signals = await source.fetch("BTC")

    whale = [s for s in signals if s.signal_type == SignalType.WHALE_FLOW]
    assert len(whale) == 1, f"Expected 1 whale_flow signal, got {len(whale)}"

    s: SignalEvent = whale[0]
    assert s.source == "dune"
    assert s.source_type == SourceType.INDEXED
    assert s.asset == "BTC"
    assert s.signal_type == SignalType.WHALE_FLOW
    assert s.value == 12500000.0
    assert s.direction == Direction.BULLISH
    assert s.trust_tier == 2
    assert s.confidence == 0.85
    assert "Whale bridge flow" in s.summary


@pytest.mark.asyncio
async def test_whale_flow_bearish_direction(monkeypatch):
    monkeypatch.setitem(dune_module.QUERY_IDS, SignalType.WHALE_FLOW, 9999999)

    row = {**WHALE_FLOW_ROW, "value": -8000000.0, "direction_hint": "bearish"}
    transport = _make_transport([row])
    client = httpx.AsyncClient(transport=transport)
    source = DuneSource(api_key="test-key", http_client=client)

    signals = await source.fetch("BTC")
    whale = [s for s in signals if s.signal_type == SignalType.WHALE_FLOW]
    assert whale[0].direction == Direction.BEARISH
    assert whale[0].value == -8000000.0


@pytest.mark.asyncio
async def test_whale_flow_neutral_direction(monkeypatch):
    monkeypatch.setitem(dune_module.QUERY_IDS, SignalType.WHALE_FLOW, 9999999)

    row = {**WHALE_FLOW_ROW, "value": 1000000.0, "direction_hint": "neutral"}
    transport = _make_transport([row])
    client = httpx.AsyncClient(transport=transport)
    source = DuneSource(api_key="test-key", http_client=client)

    signals = await source.fetch("BTC")
    whale = [s for s in signals if s.signal_type == SignalType.WHALE_FLOW]
    assert whale[0].direction == Direction.NEUTRAL


@pytest.mark.asyncio
async def test_whale_flow_skipped_when_query_id_zero():
    source = DuneSource(api_key="test-key")
    # QUERY_IDS[WHALE_FLOW] is 0 by default — should be skipped, no network call
    signals = await source.fetch("BTC")
    whale = [s for s in signals if s.signal_type == SignalType.WHALE_FLOW]
    assert whale == []


@pytest.mark.asyncio
async def test_no_api_key_returns_empty():
    source = DuneSource(api_key="")
    signals = await source.fetch("BTC")
    assert signals == []
