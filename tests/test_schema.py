"""Tests for SignalEvent schema."""
import pytest
from datetime import datetime, timezone
from signal_pipeline.schema import SignalEvent, SignalType, Direction, SourceType


def make_event(**kwargs) -> SignalEvent:
    defaults = dict(
        source="hyperliquid",
        source_type=SourceType.CHAIN_NATIVE,
        asset="BTC",
        signal_type=SignalType.FUNDING_RATE,
        value=10.95,
        direction=Direction.BEARISH,
        trust_tier=1,
    )
    defaults.update(kwargs)
    return SignalEvent(**defaults)


def test_default_id_is_uuid():
    e = make_event()
    assert len(e.id) == 36
    assert e.id.count("-") == 4


def test_two_events_have_different_ids():
    assert make_event().id != make_event().id


def test_flag_non_blocking():
    e = make_event()
    e.flag("stale:3700s")
    assert "stale:3700s" in e.validation_flags
    assert e.is_valid is True


def test_flag_blocking():
    e = make_event()
    e.flag("missing_fields:asset", blocking=True)
    assert e.is_valid is False


def test_age_seconds_recent():
    e = make_event()
    assert e.age_seconds() < 1.0


def test_repr_contains_key_fields():
    e = make_event()
    r = repr(e)
    assert "BTC" in r
    assert "funding_rate" in r
    assert "bearish" in r


def test_raw_defaults_to_empty_dict():
    e = make_event()
    assert e.raw == {}


def test_chart_series_defaults_to_none():
    e = make_event()
    assert e.chart_series is None


def test_score_and_rank_default_zero():
    e = make_event()
    assert e.score == 0.0
    assert e.rank == 0
