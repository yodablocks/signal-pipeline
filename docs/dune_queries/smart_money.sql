-- smart_money.sql
-- Proxy for smart money positioning on Hyperliquid perps.
--
-- True per-wallet trade data is not available on Dune — hyperliquid.market_data
-- provides aggregated market metrics per coin per hour.
--
-- Signal: assets where OI is growing AND funding is positive = longs accumulating
-- (rational actors are paying to stay long = conviction). The inverse flags
-- shorts accumulating. This is the best available proxy for directional bias
-- without wallet-level data.
--
-- Output columns (required by dune.py row contract):
--   asset          TEXT   — token symbol (e.g. BTC, ETH, SOL)
--   value          FLOAT  — 24h OI change in USD (positive = longs building)
--   direction_hint TEXT   — 'bullish', 'bearish', or 'neutral'
--   summary        TEXT   — human-readable description for the agent

WITH latest AS (
    SELECT
        coin                                AS asset,
        mark_px,
        open_interest,
        funding,
        day_ntl_vlm,
        time,
        ROW_NUMBER() OVER (PARTITION BY coin ORDER BY time DESC) AS rn
    FROM hyperliquid.market_data
    WHERE time >= NOW() - INTERVAL '2' HOUR
),
prev AS (
    SELECT
        coin                                AS asset,
        open_interest,
        mark_px,
        time,
        ROW_NUMBER() OVER (PARTITION BY coin ORDER BY time DESC) AS rn
    FROM hyperliquid.market_data
    WHERE time >= NOW() - INTERVAL '26' HOUR
      AND time <  NOW() - INTERVAL '24' HOUR
),
combined AS (
    SELECT
        l.asset,
        l.mark_px,
        l.funding,
        l.day_ntl_vlm,
        -- OI change in USD over ~24h
        (l.open_interest - p.open_interest) * l.mark_px   AS oi_change_usd,
        l.open_interest * l.mark_px                        AS oi_usd
    FROM latest l
    JOIN prev p ON l.asset = p.asset AND l.rn = 1 AND p.rn = 1
    WHERE l.mark_px > 0
      AND l.day_ntl_vlm > 1000000   -- filter dust markets
)
SELECT
    asset,
    ROUND(oi_change_usd, 0)                                 AS value,
    CASE
        WHEN oi_change_usd >  1000000 AND funding >= 0 THEN 'bullish'
        WHEN oi_change_usd < -1000000 AND funding <= 0 THEN 'bearish'
        ELSE                                                 'neutral'
    END                                                     AS direction_hint,
    CONCAT(
        'HL OI change 24h: ',
        CASE WHEN oi_change_usd >= 0 THEN '+' ELSE '' END,
        CAST(ROUND(oi_change_usd / 1e6, 2) AS VARCHAR), 'M USD. ',
        'Current OI: $', CAST(ROUND(oi_usd / 1e6, 1) AS VARCHAR), 'M. ',
        'Funding: ', CAST(ROUND(funding * 100, 4) AS VARCHAR), '%. ',
        'Daily volume: $', CAST(ROUND(day_ntl_vlm / 1e6, 1) AS VARCHAR), 'M.'
    )                                                       AS summary
FROM combined
ORDER BY ABS(oi_change_usd) DESC
