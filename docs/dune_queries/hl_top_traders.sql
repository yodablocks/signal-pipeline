-- hl_top_traders.sql
-- Aggregates PnL and AUM of the top 100 Hyperliquid traders by volume.
-- Source: dune."swell-network".dataset_hyperliquid_top_users
--   Published by Liquid Labs — the team behind Faro and Hyperwave.
--   Mirrors the Hyperliquid leaderboard API: address, volume, pnl, account_value.
--
-- Signal semantics: aggregate PnL of the top-100 addresses indicates whether
-- the most active traders are currently profitable. Positive sum → smart money
-- winning → bullish conditions. Negative sum → top traders being squeezed → bearish.
-- This is a global cross-asset signal; it does not decompose by coin.
--
-- Output columns (required by dune.py row contract):
--   asset          TEXT  — '{{asset}}' parameter (global signal, tagged for routing)
--   value          FLOAT — sum(pnl) of top-100 traders (USD)
--   direction_hint TEXT  — 'bullish' / 'bearish' / 'neutral'
--   summary        TEXT  — pre-formatted for agent context window
--
-- Additional columns for raw store / MAD validation:
--   aggregate_pnl  FLOAT — same as value, explicit label
--   avg_pnl        FLOAT — mean PnL across sampled addresses
--   total_aum      FLOAT — sum(account_value) of top-100 (USD)
--   whale_count    INT   — number of addresses in the sample
--   pnl_winners    INT   — count of addresses with pnl > 0

WITH top_traders AS (
    SELECT
        address,
        volume,
        pnl,
        account_value
    FROM dune."swell-network".dataset_hyperliquid_top_users
    WHERE volume IS NOT NULL
      AND pnl IS NOT NULL
    ORDER BY volume DESC
    LIMIT 100
),
aggregated AS (
    SELECT
        SUM(pnl)                             AS aggregate_pnl,
        AVG(pnl)                             AS avg_pnl,
        SUM(account_value)                   AS total_aum,
        COUNT(*)                             AS whale_count,
        COUNT(CASE WHEN pnl > 0 THEN 1 END)  AS pnl_winners
    FROM top_traders
)
SELECT
    '{{asset}}'                              AS asset,
    ROUND(aggregate_pnl, 0)                  AS value,
    CASE
        WHEN aggregate_pnl >  5000000  THEN 'bullish'
        WHEN aggregate_pnl < -5000000  THEN 'bearish'
        ELSE                                'neutral'
    END                                      AS direction_hint,
    CONCAT(
        'HL top-100 traders (by volume): aggregate PnL ',
        CASE WHEN aggregate_pnl >= 0 THEN '+' ELSE '' END,
        CAST(CAST(ROUND(aggregate_pnl / 1e6, 2) AS DECIMAL(18,2)) AS VARCHAR), 'M USD. ',
        'Avg PnL: ',
        CASE WHEN avg_pnl >= 0 THEN '+' ELSE '' END,
        CAST(CAST(ROUND(avg_pnl / 1e3, 1) AS DECIMAL(18,1)) AS VARCHAR), 'K. ',
        'Total AUM: $', CAST(CAST(ROUND(total_aum / 1e6, 0) AS DECIMAL(18,0)) AS VARCHAR), 'M. ',
        'Winners: ', CAST(pnl_winners AS VARCHAR), '/', CAST(whale_count AS VARCHAR), '.'
    )                                        AS summary,
    ROUND(aggregate_pnl, 0)                  AS aggregate_pnl,
    ROUND(avg_pnl, 0)                        AS avg_pnl,
    ROUND(total_aum, 0)                      AS total_aum,
    CAST(whale_count AS INTEGER)             AS whale_count,
    CAST(pnl_winners AS INTEGER)             AS pnl_winners
FROM aggregated
