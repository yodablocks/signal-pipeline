# signal-pipeline

Source-agnostic signal ingestion, ranking, and directional scoring pipeline for crypto AI agents.

Fetches market signals from multiple sources, validates trust and data integrity, ranks by signal strength, scores a directional verdict, and assembles a token-budgeted JSON payload that an LLM agent (e.g. Hermes) can consume directly — no raw value parsing required.

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
sources/*.py        validation.py       store/memory.py     ranking.py          model.py            assembler.py
  fetch()      -->  validate_batch() --> save() / get() --> rank()          --> score()         --> assemble()
  per source        staleness, MAD       in-memory           score + dedup       directional         agent JSON
                    oracle dev,          ClickHouse-ready    top-N by budget     verdict +           with model
                    injection check      interface                               confluence          output block
```

Every source maps its raw data to a canonical `SignalEvent` dataclass (`schema.py`) before anything else touches it. The agent receives the assembler output — it never sees raw source data, scores, or validation flags.

**Ranking scoring formula:**

```
score = recency_weight × trust_weight × magnitude_weight × position_boost × confidence

recency_weight   = exp(-λ × age_seconds)      # per-signal-type decay
trust_weight     = {1: 1.0, 2: 0.7, 3: 0.4}
magnitude_weight = normalized value [0, 1]
position_boost   = 1.5 if asset in open positions, else 1.0
```

**Anomaly detection** uses Median Absolute Deviation (MAD), not z-score. A single extreme outlier (e.g. GRVT at funding rate cap) inflates the mean and masks itself in z-score. MAD uses the median as baseline. Reference: Iglewicz & Hoaglin (1993).

---

## Model layer

`model.py` combines ranked signals into a single directional verdict the agent can act on.

**Signal inputs:**

| Signal | Source | Type |
|--------|--------|------|
| Funding rate | Hyperliquid / perp panel | Chain-native |
| OI trend | Hyperliquid / perp panel | Chain-native |
| Liquidation cluster proximity | hl-liquidation-heatmap | Chain-native |
| P/C ratio | deribit-options-flow | Indexed |
| IV skew (25-delta) | deribit-options-flow | Indexed |
| Net premium flow | deribit-options-flow | Indexed |
| Max pain distance from spot | deribit-options-flow | Indexed |

**Design decisions:**

- **Options cluster**: P/C ratio, IV skew, net premium, and max pain are averaged into a single vote before scoring. Prevents Deribit from getting 4× weight over a single chain-native signal.
- **Equal weights**: `SIGNAL_WEIGHTS` in `model.py` uses `1.0` across all sources. **These weights are unvalidated — no backtesting data exists yet.** Replace with empirically derived weights once historical data is available.
- **Confluence over magnitude**: confidence is `magnitude × agreement_ratio`. A unanimous 4-source agreement outranks a single strong outlier.
- **Neutral band**: `|raw_score| ≤ 0.05` returns neutral. Prevents marginal noise from producing false directional calls.

**Output (included in agent payload as `model` block):**

```json
{
  "direction": "bearish",
  "confidence": 0.71,
  "confluence": {
    "bullish": 0,
    "bearish": 3,
    "neutral": 1,
    "agreement_ratio": 1.0,
    "summary": "0 bullish / 3 bearish / 1 neutral out of 4 sources"
  },
  "contributing_signals": [
    {
      "signal_type": "funding_rate",
      "source": "hyperliquid",
      "direction": "bearish",
      "strength": 0.75,
      "weight": 1.0,
      "weighted_contribution": -0.75,
      "reason": "funding 80.0% APR (crowded longs)"
    },
    {
      "signal_type": "options_cluster",
      "source": "deribit",
      "direction": "bearish",
      "strength": 0.6,
      "weight": 1.0,
      "weighted_contribution": -0.6,
      "reason": "options cluster bearish (P/C ratio 1.60 put-heavy; IV skew +8.0pp put premium)"
    }
  ],
  "explanation": "BTC directional model: BEARISH with high confidence (71.0%). Confluence: 0 bullish / 3 bearish / 1 neutral out of 4 sources. Key drivers: funding 80.0% APR (crowded longs), options cluster bearish. Agreement ratio: 100%.",
  "weight_disclaimer": "UNVALIDATED: equal weights used (no backtesting data). Do not treat confidence as a calibrated probability."
}
```

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
# Basic — top 10 signals for BTC, includes model verdict
signal-pipeline --asset BTC

# With position context — boosts signals for assets you're holding
signal-pipeline --asset BTC --top 10 --position long:50000

# Show per-signal model breakdown (to stderr, JSON payload unaffected)
signal-pipeline --asset BTC --explain

# Explain + position context
signal-pipeline --asset BTC --explain --position long:50000

# Short position, different asset
signal-pipeline --asset HYPE --top 5 --position short:25000

# Write to file instead of stdout
signal-pipeline --asset ETH --output payload.json

# Verbose — show source fetch counts and validation warnings
signal-pipeline --asset BTC --verbose

# Full options
signal-pipeline --help
```

### --explain output

`--explain` prints a human-readable model breakdown to stderr. The JSON payload on stdout is unchanged.

```
━━ Model output: BTC ━━
Direction  : BEARISH
Confidence : 71.0%
Confluence : 0 bullish / 3 bearish / 1 neutral out of 4 sources

Signal breakdown:
  ▼ [funding_rate        ] bearish  strength=0.75  weight=1.0  contrib=-0.750  | funding 80.0% APR (crowded longs)
  ▼ [liquidation_cascade ] bearish  strength=0.60  weight=1.0  contrib=-0.600  | liquidation cluster $6,000,000 above spot
  ▼ [options_cluster     ] bearish  strength=0.55  weight=1.0  contrib=-0.550  | options cluster bearish (P/C 1.60 put-heavy; IV skew +8.0pp)
  – [oi_dominance        ] neutral  strength=0.00  weight=1.0  contrib=+0.000  | OI trend neutral

Explanation: BTC directional model: BEARISH with high confidence (71.0%). ...
⚠  UNVALIDATED: equal weights used (no backtesting data). Do not treat confidence as a calibrated probability.
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
  "model": {
    "direction": "bearish",
    "confidence": 0.71,
    "confluence": {
      "bullish": 0,
      "bearish": 3,
      "neutral": 1,
      "agreement_ratio": 1.0,
      "summary": "0 bullish / 3 bearish / 1 neutral out of 4 sources"
    },
    "contributing_signals": [
      {
        "signal_type": "funding_rate",
        "source": "hyperliquid",
        "direction": "bearish",
        "strength": 0.75,
        "weight": 1.0,
        "weighted_contribution": -0.75,
        "reason": "funding 80.0% APR (crowded longs)"
      }
    ],
    "explanation": "BTC directional model: BEARISH with high confidence (71.0%). Confluence: 0 bullish / 3 bearish / 1 neutral out of 4 sources. Key drivers: funding 80.0% APR (crowded longs). Agreement ratio: 100%.",
    "weight_disclaimer": "UNVALIDATED: equal weights used (no backtesting data). Do not treat confidence as a calibrated probability."
  },
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

The validation, ranking, and assembly layers pick it up automatically.

To add a new signal type to the model, add a `_vote_yourtype()` function in `model.py` and register it in `_VOTE_FN`. If it's an options-style signal from the same underlying instrument, add it to `_OPTIONS_CLUSTER` so it gets averaged into the cluster rather than counted independently.

---

## Dune signals

| Signal | Query | Status | SQL |
|--------|-------|--------|-----|
| `gas_volatility` | [7610937](https://dune.com/queries/7610937) | Live | `docs/dune_queries/gas_volatility.sql` |
| `smart_money` | [7611082](https://dune.com/queries/7611082) | Live | `docs/dune_queries/smart_money.sql` |
| `whale_flow` | [7611264](https://dune.com/queries/7611264) | Live | `examples/dune_whale_flow.sql` |

**`gas_volatility`** — HyperEVM block gas coefficient of variation over a 1h rolling window. Proxy for network congestion and execution risk. High CV = bearish (elevated fees), low CV = bullish (calm network).

**`smart_money`** — Net USDC bridge flow of curated known wallets (Wintermute, Fasanara Capital, Abraxas Capital, James Wynn, and others) into/out of Hyperliquid over the last 24h. Source: `erc20_arbitrum.evt_Transfer`, near real-time. Net deposit = bullish positioning intent, net withdrawal = de-risking.

**`whale_flow`** — Net USDC flow of individual transfers >=\$500k through the Hyperliquid bridge, last 24h. Source: `erc20_arbitrum.evt_Transfer`, both native USDC and USDC.e. No wallet filter — size is the signal. Direction thresholds: ±\$5M (2-3 whale moves).

All three are global signals (not asset-specific). The Dune queries accept an `{{asset}}` parameter so rows are tagged correctly for the pipeline's asset filter.

To add more Dune signals: write a query that outputs `asset, value, direction_hint, summary` columns, save it on Dune, and add its ID to `QUERY_IDS` in `src/signal_pipeline/sources/dune.py`.

---

## Known limitations

- **Model weights are unvalidated.** Equal weights are an honest baseline. They must be replaced with backtested weights before the model output is used for live sizing decisions.
- **Options signals reflect derivatives markets, not HL spot directly.** P/C ratio and IV skew from Deribit are correlated with but not identical to Hyperliquid perp sentiment.
- **Social source is a stub.** `social.py` returns `[]`. Twitter/X API v2 is expensive; interface is defined for when it's implemented.
- **Polymarket market matching is fuzzy.** Gamma API local filtering catches most BTC/Bitcoin markets but niche assets may return zero matches or near-matches.
- **No persistence.** `MemoryStore` is process-local. A ClickHouse or TimescaleDB backend would need to implement `store/base.py` `SignalStore` ABC.
- **No streaming mode.** CLI is a one-shot fetch. A polling loop or WebSocket subscription mode would be the next step for real-time agent feeds.
