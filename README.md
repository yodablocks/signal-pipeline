# signal-pipeline

Source-agnostic signal ingestion, ranking, and directional scoring pipeline for crypto AI agents.

Fetches market signals from multiple sources, validates trust and data integrity, ranks by signal strength, scores a directional verdict, and assembles a token-budgeted JSON payload that an LLM agent (e.g. Hermes) can consume directly — no raw value parsing required.

Sits one layer above [perp-liquidity](https://github.com/yodablocks/perp-liquidity), which provides the raw fetchers for 8 perp DEXes.

---

## Trust tier model

Mixing signal sources without weighting is how agents get manipulated. Every signal carries an explicit trust tier applied as a score multiplier in the ranking layer.

| Tier | Label | Examples | Latency | Trust model |
|------|-------|----------|---------|-------------|
| 1 | Chain-native | Hyperliquid, perp-liquidity panel, HLP vault | Sub-second | Cryptographically verifiable |
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

| Signal | Source | Cluster | Type |
|--------|--------|---------|------|
| Funding rate (8 venues) | perp-liquidity panel | `funding_cluster` | Chain-native |
| OI dominance (8 venues) | perp-liquidity panel | `oi_cluster` | Chain-native |
| Liquidation cluster proximity | hl-liquidation-heatmap | `liquidation_cascade` | Chain-native |
| HLP vault positioning | HL info API | `hlp_sentiment` | Chain-native |
| P/C ratio | deribit-options-flow | `options_cluster` | Indexed |
| IV skew (25-delta) | deribit-options-flow | `options_cluster` | Indexed |
| Net premium flow | deribit-options-flow | `options_cluster` | Indexed |
| Max pain distance from spot | deribit-options-flow | `options_cluster` | Indexed |

**Design decisions:**

- **Funding cluster**: All 8 venue funding rates are collapsed into a single `funding_cluster` vote using the **panel median**. Median is used for consistency with MAD anomaly detection — a single venue stuck at an extreme rate (e.g. GRVT at 11% APR cap) does not distort the panel picture.
- **OI cluster**: All 8 venue OI dominance signals are collapsed into a single `oi_cluster` vote using **majority direction**. Majority vote is used (not median) because OI dominance is categorical, not a continuous value that can be meaningfully averaged.
- **Options cluster**: P/C ratio, IV skew, net premium, and max pain are averaged into a single `options_cluster` vote. Prevents Deribit from getting 4× weight over a single chain-native signal.
- **Liquidation cluster**: nearest significant liquidation cluster to spot, inferred from OI-delta on the HL `activeAssetCtx` WebSocket. Cluster above spot = bearish (sell-side pressure). Cluster below = bullish (buy-side magnet). Requires the `hl-liquidation-heatmap` streamer to be running to populate the local SQLite.
- **HLP sentiment**: Contrarian signal from the HLP vault's live position. HLP is HL's native market maker and liquidator — its net position is the inverse of aggregate trader flow. HLP net short = traders crowded long = bearish. HLP net long = traders net short = squeeze setup. When HLP is flat (`position_relevant=False`), the signal abstains from the model vote. Confidence scales with gross notional: ≥$50M → 0.90, ≥$10M → 0.85, ≥$1M → 0.80, <$1M → 0.70.
- **Model scores all validated events**: `score()` receives all validated signals, not the ranking-capped top-N. Ranking controls the agent payload (token budget). Scoring is independent.
- **Equal weights**: `SIGNAL_WEIGHTS` in `model.py` uses `1.0` across all cluster sources. **These weights are unvalidated — no backtesting data exists yet.** Replace with empirically derived weights once historical data is available.
- **Confluence over magnitude**: confidence is `magnitude × agreement_ratio`. A unanimous 3-source agreement outranks a single strong outlier.
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
      "signal_type": "hlp_sentiment",
      "source": "hlp_vault",
      "direction": "bearish",
      "strength": 0.85,
      "weight": 1.0,
      "weighted_contribution": -0.85,
      "reason": "HLP vault BTC position: net -42.5M USD (short), gross 42.5M USD. Traders are net long → bearish."
    }
  ],
  "explanation": "BTC directional model: BEARISH with high confidence (71.0%). Confluence: 0 bullish / 3 bearish / 1 neutral out of 4 sources.",
  "weight_disclaimer": "UNVALIDATED: equal weights used (no backtesting data). Do not treat confidence as a calibrated probability."
}
```

---

## Install

```bash
pip install -e ".[dev]"
pip install -e ../perp-liquidity          # tier-1 signals from 8 perp DEXes
pip install -e ../deribit-options-flow    # options flow signals
pip install -e ../hl-liquidation-heatmap  # liquidation cluster signals
```

The liquidation source requires the streamer to be running in a separate terminal to populate data:

```bash
cd ../hl-liquidation-heatmap
python stream.py --coins BTC,ETH,SOL
```

The streamer writes inferred liquidation events to a local SQLite (`liquidations.db`). The signal-pipeline reads from it. Without the streamer, `LiquidationSource` returns `[]` and the `liquidation_cascade` cluster abstains from the model vote.

**Environment variables** (all optional — sources return `[]` without them):

```
DUNE_API_KEY=...              # Dune Analytics (tier-2 signals)
TWITTER_BEARER_TOKEN=...      # Social source — stub, not yet implemented
LIQUIDATION_DB=...            # Path to hl-liquidation-heatmap SQLite (default: liquidations.db)
LIQUIDATION_LOOKBACK_DAYS=3   # How many days of fills to consider (default: 3)
LIQUIDATION_MIN_CLUSTER_USD=500000  # Minimum USD notional for a cluster (default: 500k)
LIQUIDATION_PROXIMITY_PCT=5.0       # Max % distance from spot to consider (default: 5%)
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

---

## Sample payload

Live output from `signal-pipeline --asset BTC --top 10 --position long:50000` (2026-06-04):

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
      "signal_type": "hlp_sentiment",
      "source": "hlp_vault",
      "trust_tier": 1,
      "direction": "neutral",
      "value": 0.0,
      "score": 0.0,
      "position_relevant": false,
      "summary": "HLP vault BTC: no open position (confirmed flat, total AUM $0.31B). Not a directional signal."
    }
  ],
  "model": { "...": "..." },
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
            signal_type=SignalType.WHALE_FLOW,
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

---

## Dune signals

| Signal | Query | Status | SQL |
|--------|-------|--------|-----|
| `gas_volatility` | [7610937](https://dune.com/queries/7610937) | Live | `docs/dune_queries/gas_volatility.sql` |
| `smart_money` | [7611082](https://dune.com/queries/7611082) | Live | `docs/dune_queries/smart_money.sql` |
| `whale_flow` | [7611264](https://dune.com/queries/7611264) | Live | `examples/dune_whale_flow.sql` |
| `hl_top_traders` | pending | Needs query ID | `docs/dune_queries/hl_top_traders.sql` |

**`gas_volatility`** — HyperEVM block gas coefficient of variation over a 1h rolling window. Proxy for network congestion and execution risk.

**`smart_money`** — Net USDC bridge flow of curated known wallets (Wintermute, Fasanara Capital, Abraxas Capital, James Wynn, and others) into/out of Hyperliquid over 24h. Source: `erc20_arbitrum.evt_Transfer`.

**`whale_flow`** — Net USDC flow of individual transfers ≥$500k through the Hyperliquid bridge, last 24h. No wallet filter — size is the signal.

**`hl_top_traders`** — Aggregate PnL of the top-100 Hyperliquid traders by volume. Source: `dune."swell-network".dataset_hyperliquid_top_users` — published by Liquid Labs (Faro's parent company). Positive aggregate PnL = top traders winning = bullish conditions. Direction thresholds: ±$5M. To activate: paste `docs/dune_queries/hl_top_traders.sql` into Dune, save the query, and replace `0` in `QUERY_IDS[SignalType.HL_TOP_TRADERS]` in `sources/dune.py` with the assigned query ID.

All Dune signals are global (not asset-specific). Queries accept an `{{asset}}` parameter so rows are tagged correctly for the pipeline's asset filter. To add more: write a query outputting `asset, value, direction_hint, summary` columns and add its ID to `QUERY_IDS` in `sources/dune.py`.

---

## HLP vault signal

`sources/hlp.py` reads the HLP vault's live position from the Hyperliquid info endpoint.

```
POST https://api.hyperliquid.xyz/info
{"type": "clearinghouseState", "user": "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"}
```

No API key required. The endpoint is public. Suitable for polling every 1–5 minutes.

**Signal: `HLP_SENTIMENT`** — contrarian. HLP absorbs trader flow as market maker and liquidator. Its net position is the inverse of aggregate trader positioning.

| HLP position | Trader position | Signal |
|---|---|---|
| Net short > $5M | Crowded long | `bearish` |
| Net long > $5M | Net short | `bullish` (squeeze setup) |
| Flat or < $5M | Noise | `neutral`, `position_relevant=False` |

**Signal: `TOP_TRADER_POSITIONING`** — optional, disabled by default. Pass `registry_addresses` to `HLPSource` to activate. Uses `dune."swell-network".dataset_hyperliquid_top_users` as a wallet whitelist, fetches `clearinghouseState` for each address, and aggregates net notional. Direction follows the traders (not contrarian).

---

## Known limitations

- **Model weights are unvalidated.** Equal weights are an honest baseline. They must be replaced with backtested weights before the model output is used for live sizing decisions.
- **Options signals reflect derivatives markets, not HL spot directly.** P/C ratio and IV skew from Deribit are correlated with but not identical to Hyperliquid perp sentiment.
- **Social source is a stub.** `social.py` returns `[]`. Interface is defined for when it's implemented.
- **Polymarket market matching is fuzzy.** Gamma API local filtering catches most BTC/Bitcoin markets but niche assets may return zero matches.
- **No persistence.** `MemoryStore` is process-local. A ClickHouse or TimescaleDB backend would need to implement `store/base.py` `SignalStore` ABC.
- **Liquidation source requires a running streamer.** `LiquidationSource` reads from a local SQLite populated by `hl-liquidation-heatmap/stream.py`. No reliable free HTTP endpoint exists for historical HL liquidation data.
- **No streaming mode.** CLI is a one-shot fetch. A polling loop or WebSocket subscription mode would be the next step for real-time agent feeds.
- **Dune external datasets for HL are largely stale.** All community-uploaded HL datasets (21shares, ramiro_mata, etc.) stopped updating between March 2025 and April 2026. The pipeline's Dune queries run against live Dune-native tables (Arbitrum bridge events, HyperEVM blocks) — not external datasets.
