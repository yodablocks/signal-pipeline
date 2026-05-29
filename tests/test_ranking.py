"""Tests for ranking layer."""
import pytest
from signal_pipeline.schema import SignalEvent, SignalType, Direction, SourceType
from signal_pipeline.ranking import rank, deduplicate, score_event, select_top_n


def make_event(**kwargs) -> SignalEvent:
    defaults = dict(
        source="hyperliquid",
        source_type=SourceType.CHAIN_NATIVE,
        asset="BTC",
        signal_type=SignalType.FUNDING_RATE,
        value=50.0,
        direction=Direction.BEARISH,
        trust_tier=1,
        confidence=1.0,
        is_valid=True,
    )
    defaults.update(kwargs)
    return SignalEvent(**defaults)


def test_tier1_scores_higher_than_tier3_same_value():
    t1 = make_event(trust_tier=1)
    t3 = make_event(trust_tier=3)
    assert score_event(t1) > score_event(t3)


def test_position_relevant_boosts_score():
    relevant = make_event(position_relevant=True)
    irrelevant = make_event(position_relevant=False)
    relevant.score = score_event(relevant)
    irrelevant.score = score_event(irrelevant)
    assert relevant.score > irrelevant.score


def test_invalid_events_excluded_from_ranking():
    valid = make_event()
    invalid = make_event(is_valid=False)
    result = rank([valid, invalid])
    assert len(result) == 1
    assert result[0].id == valid.id


def test_position_assets_marks_relevance():
    e = make_event(asset="BTC")
    result = rank([e], position_assets={"BTC"})
    assert result[0].position_relevant is True


def test_non_position_asset_not_boosted():
    e = make_event(asset="ETH")
    result = rank([e], position_assets={"BTC"})
    assert result[0].position_relevant is False


def test_rank_assigned_correctly():
    events = [make_event(value=float(v)) for v in [10, 50, 30]]
    result = rank(events)
    ranks = [e.rank for e in result]
    assert ranks == sorted(ranks)
    assert ranks[0] == 1


def test_token_budget_limits_output():
    events = [make_event() for _ in range(50)]
    result = rank(events, token_budget=400)   # 400 / 80 tokens = 5 signals
    assert len(result) <= 5


def test_dedup_keeps_highest_score():
    # Two events with same dedup key — should keep the one with higher value
    e1 = make_event(value=10.0)
    e2 = make_event(value=90.0)
    e1.score = score_event(e1)
    e2.score = score_event(e2)
    result = deduplicate([e1, e2])
    assert len(result) == 1
    assert result[0].value == 90.0


def test_dedup_keeps_different_directions():
    bull = make_event(direction=Direction.BULLISH)
    bear = make_event(direction=Direction.BEARISH)
    bull.score = score_event(bull)
    bear.score = score_event(bear)
    result = deduplicate([bull, bear])
    assert len(result) == 2


def test_empty_input_returns_empty():
    assert rank([]) == []


def test_max_signals_hard_cap():
    events = [make_event() for _ in range(20)]
    result = rank(events, token_budget=100_000, max_signals=3)
    assert len(result) <= 3
