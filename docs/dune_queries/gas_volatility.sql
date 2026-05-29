-- gas_volatility.sql
-- Measures HyperEVM block gas volatility as a proxy for network congestion.
-- Higher volatility = elevated execution risk / priority fee pressure.
--
-- Output columns (required by dune.py row contract):
--   asset          TEXT   — hardcoded to {{asset}} parameter so dune.py filter passes
--   value          FLOAT  — coefficient of variation of gas_used over the last 1 hour
--   direction_hint TEXT   — 'bullish' (low vol, calm), 'bearish' (high vol, congested), or 'neutral'
--   summary        TEXT   — human-readable description for the agent
--
-- HyperEVM tables: hyperevm.blocks
-- Columns used: gas_used, gas_limit, base_fee_per_gas, time

WITH recent_blocks AS (
    SELECT
        gas_used,
        base_fee_per_gas
    FROM hyperevm.blocks
    WHERE time >= NOW() - INTERVAL '1' HOUR
      AND gas_used > 0
),
stats AS (
    SELECT
        COUNT(*)                        AS block_count,
        AVG(gas_used)                   AS mean_gas,
        STDDEV(gas_used)                AS stddev_gas,
        AVG(base_fee_per_gas)           AS mean_base_fee,
        STDDEV(base_fee_per_gas)        AS stddev_base_fee,
        MAX(gas_used)                   AS max_gas,
        MIN(gas_used)                   AS min_gas
    FROM recent_blocks
),
computed AS (
    SELECT
        block_count,
        mean_gas,
        stddev_gas,
        -- Coefficient of variation: normalised volatility [0, 1] range
        CASE
            WHEN mean_gas > 0 THEN LEAST(stddev_gas / mean_gas, 1.0)
            ELSE 0.0
        END AS cv_gas,
        mean_base_fee,
        stddev_base_fee
    FROM stats
)
SELECT
    '{{asset}}'                                                 AS asset,
    ROUND(cv_gas, 4)                                            AS value,
    CASE
        WHEN cv_gas >= 0.5  THEN 'bearish'
        WHEN cv_gas <= 0.15 THEN 'bullish'
        ELSE                     'neutral'
    END                                                         AS direction_hint,
    CONCAT(
        'HyperEVM gas volatility (CV) over last 1h: ',
        CAST(ROUND(cv_gas * 100, 1) AS VARCHAR), '%. ',
        'Blocks sampled: ', CAST(block_count AS VARCHAR), '. ',
        'Mean gas: ', CAST(ROUND(mean_gas / 1e6, 2) AS VARCHAR), 'M. ',
        'Base fee avg: ', CAST(ROUND(mean_base_fee, 2) AS VARCHAR), ' gwei.'
    )                                                           AS summary
FROM computed
