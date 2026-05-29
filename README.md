# signal-pipeline

Source-agnostic signal ingestion and ranking pipeline for crypto AI agents.

Fetches market signals from multiple sources, validates trust and data integrity, ranks by signal strength, and assembles a token-budgeted JSON payload that an LLM agent (e.g. Hermes) can consume directly — no raw value parsing required.

Sits one layer above [perp-liquidity](https://github.com/yodablocks/perp-liquidity), which provides the raw fetchers for 8 perp DEXes.

---

## Trust tier model

Mixing signal sources without weighting is how agents get manipulated. Every signal carries an explicit trust tier applied as a score multiplier in the ranking layer.

| Tier | Label | Examples | Latency | Trust model |
|------|-------|----------|---------|-------------|
| 1 | Chain-native | Hyperliquid, perp-liquidity panel | Sub-second | Cryptographically verifiable |
| 2 | Indexed / prediction | Dune Analytics, Polymarket | Minutes | Depends on indexer or market integrity |
| 3 | Social | Twitter/X, Discord, KOL calls | Seconds | Fully adversarial |

Tier 3 signals can never outrank tier 1 signals of similar magnitude. The multipliers are `{1: 1.0, 2: 0.7, 3: 0.4}`.

---

## Architecture

```
sources/*.py        validation.py       store/memory.py     ranking.py          assembler.py
  fetch()      -->  validate_batch() --> save() / get() --> rank()          --> assemble()
  per source        staleness, MAD       in-memory           score + dedup       agent JSON
                    oracle dev,          ClickHouse-ready    top-N by budget
                    injection check      interface
```

Every source maps its raw data to a canonical `SignalEvent` dataclass (`schema.py`) before anything else touches it. The agent receives the assembler output — it never sees raw source data, scores, or validation flags.

**Scoring formula:**

```
score = recency_weight × trust_weight × magnitude_weight × position_boost × confidence

recency_weight   = exp(-λ × age_seconds)      # per-signal-type decay
trust_weight     = {1: 1.0, 2: 0.7, 3: 0.4}
magnitude_weight = normalized value [0, 1]
position_boost   = 1.5 if asset in open positions, else 1.0
```

**Anomaly detection** uses Median Absolute Deviation (MAD), not z-score. A single extreme outlier (e.g. GRVT at funding rate cap) inflates the mean and masks itself in z-score. MAD uses the median as baseline. Reference: Iglewicz & Hoaglin (1993).

---

## Install

```bash
pip install -e ".[dev]"
pip install -e ../perp-liquidity   # tier-1 signals from 8 perp DEXes
```

**Environment variables** (all optional — sources return `[]` without them):

```
DUNE_API_KEY=...           # Dune Analytics (tier-2 gas volatility signal)
TWITTER_BEARER_TOKEN=...   # Social source — stub, not yet implemented
```

A `.env` file is supported — `python-dotenv` loads it automatically on CLI startup:

```
DUNE_API_KEY=your_key_here
```

---

## CLI

```bash
# Basic — top 10 signals for BTC
signal-pipeline --asset BTC

# With position context — boosts signals for assets you're holding
signal-pipeline --asset BTC --top 10 --position long:50000

# Short position, different asset
signal-pipeline --asset HYPE --top 5 --position short:25000

# Write to file instead of stdout
signal-pipeline --asset ETH --output payload.json

# Verbose — show source fetch counts and validation warnings
signal-pipeline --asset BTC --verbose

# Full options
signal-pipeline --help
```

---

## Sample payload

Live output from `signal-pipeline --asset BTC --top 10 --position long:50000` (2026-05-29):

```json
{
  "asset": "BTC",
  "position_context": { "side": "long", "size_usd": 50000.0 },
  "fetched_at": "2026-05-29T13:33:35Z",
  "signal_count": 10,
  "signals": [
    {
      "rank": 1,
      "signal_type": "oi_dominance",
      "source": "hyperliquid",
      "trust_tier": 1,
      "direction": "bullish",
      "value": 64.64,
      "score": 0.9697,
      "position_relevant": true,
      "summary": "hyperliquid OI $2274M (64.6% market share). Rank 1/8."
    },
    {
      "rank": 2,
      "signal_type": "funding_rate",
      "source": "grvt",
      "trust_tier": 1,
      "direction": "bearish",
      "value": 10.95,
      "score": 0.7911,
      "position_relevant": true,
      "summary": "grvt funding APR +10.95% (shorts paid). Rank 8/8 in panel."
    },
    {
      "rank": 9,
      "signal_type": "funding_rate",
      "source": "edgex",
      "trust_tier": 1,
      "direction": "neutral",
      "value": -0.115,
      "score": 0.7496,
      "position_relevant": true,
      "summary": "edgex funding APR -0.12% (longs paid). Rank 1/8 in panel."
    },
    {
      "rank": 10,
      "signal_type": "outcome_prob",
      "source": "polymarket",
      "trust_tier": 2,
      "direction": "neutral",
      "value": 0.5,
      "score": 0.4294,
      "position_relevant": true,
      "summary": "Polymarket: 'Will bitcoin hit $1m before GTA VI?' — YES 50.0% implied probability."
    }
  ],
  "token_estimate": 950
}
```

Full sample: [`examples/payload_sample.json`](examples/payload_sample.json)

---

## Adding a new source

1. Create `src/signal_pipeline/sources/yourname.py`
2. Subclass `SignalSource` and implement `fetch()`:

```python
from signal_pipeline.sources.base import SignalSource
from signal_pipeline.schema import SignalEvent, SignalType, SourceType, Direction

class YourSource(SignalSource):
    SOURCE_NAME = "your_source"
    SOURCE_TYPE = SourceType.INDEXED   # or CHAIN_NATIVE / PREDICTION / SOCIAL
    TRUST_TIER = 2                     # 1, 2, or 3

    async def fetch(self, asset: str) -> list[SignalEvent]:
        # Must never raise — absorb errors and return []
        try:
            raw = await self._call_api(asset)
        except Exception as exc:
            log.error("YourSource.fetch failed: %s", exc)
            return []

        return [SignalEvent(
            source=self.SOURCE_NAME,
            source_type=self.SOURCE_TYPE,
            asset=asset,
            signal_type=SignalType.WHALE_FLOW,  # pick from schema.SignalType
            value=raw["amount"],
            direction=Direction.BULLISH,
            confidence=0.85,
            trust_tier=self.TRUST_TIER,
            summary=f"Your source: {raw['amount']:,.0f} USD net flow.",
            raw=raw,
        )]
```

3. Register it in `cli.py` `_default_sources()`.

The validation, ranking, and assembly layers pick it up automatically — no other changes needed.

---

## Dune signals

| Signal | Query | Status | SQL |
|--------|-------|--------|-----|
| `gas_volatility` | [7610937](https://dune.com/queries/7610937) | Live | `docs/dune_queries/gas_volatility.sql` |
| `smart_money` | — | Deferred | `docs/dune_queries/smart_money.sql` |
| `whale_flow` | — | Deferred | — |

`gas_volatility` measures HyperEVM block gas coefficient of variation over a 1h rolling window — a proxy for network congestion and execution risk. It is a network-level signal (not asset-specific): the Dune query accepts an `{{asset}}` parameter so the row is tagged correctly for the pipeline's asset filter.

To add more Dune signals: write a query that outputs `asset, value, direction_hint, summary` columns, save it on Dune, and add its ID to `QUERY_IDS` in `src/signal_pipeline/sources/dune.py`.

---

## Known limitations

- **`smart_money` deferred.** `hyperliquid.market_data` on Dune updates monthly — not suitable for a real-time positioning signal. Will revisit when fresher data is available.
- **`whale_flow` deferred.** Requires UTXO table access on Dune, which is more expensive to query.
- **Social source is a stub.** `social.py` returns `[]`. Twitter/X API v2 is expensive; interface is defined for when it's implemented.
- **Polymarket market matching is fuzzy.** Gamma API local filtering catches most BTC/Bitcoin markets but niche assets may return zero matches or near-matches.
- **No persistence.** `MemoryStore` is process-local. A ClickHouse or TimescaleDB backend would need to implement `store/base.py` `SignalStore` ABC.
- **No streaming mode.** CLI is a one-shot fetch. A polling loop or WebSocket subscription mode would be the next step for real-time agent feeds.
