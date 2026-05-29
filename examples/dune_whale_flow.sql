-- dune_whale_flow.sql
-- Net large-wallet (>$500k per transfer) USDC flow into/out of Hyperliquid bridge, last 24h.
-- "Whale flow" = single transfers >= $500k USD. Below that threshold is noise, not conviction.
--
-- Source: erc20_arbitrum.evt_Transfer, pivoted on HL bridge address.
-- Same table as smart_money.sql — but no wallet filter. Size is the signal, not identity.
-- USDC contracts: native (0xaf88d065…) and bridged USDC.e (0xFF970A61…), both filtered.
--
-- Net aggregate threshold for direction_hint: ±$5M
-- (With $500k floor per tx, a single whale move clears $1M net easily — $5M is 2-3 whale moves)
--
-- Output columns (required by dune.py row contract):
--   asset          TEXT   — tagged with {{asset}} parameter (global HL bridge signal)
--   value          FLOAT  — net USD inflow last 24h (positive = deposits, negative = withdrawals)
--   direction_hint TEXT   — 'bullish' (net inflow >$5M), 'bearish' (net outflow >$5M), 'neutral'
--   summary        TEXT   — human-readable description for the agent

WITH hl_bridge AS (
    SELECT FROM_HEX('2Df1c51E09aECF9cacb7bc98cB1742757f163dF7') AS addr
),
usdc_contracts AS (
    SELECT contract_address FROM (VALUES
        (FROM_HEX('af88d065e77c8cC2239327C5EDb3A432268e5831')),  -- native USDC
        (FROM_HEX('FF970A61A04b1cA14834A43f5dE4533eBDDB5CC8'))   -- bridged USDC.e
    ) AS t(contract_address)
),
transfers AS (
    SELECT
        tx.value / 1e6                                          AS usdc_amount,
        CASE WHEN tx."to"   = (SELECT addr FROM hl_bridge)
             THEN tx.value / 1e6 ELSE 0 END                    AS deposit_usdc,
        CASE WHEN tx."from" = (SELECT addr FROM hl_bridge)
             THEN tx.value / 1e6 ELSE 0 END                    AS withdrawal_usdc
    FROM erc20_arbitrum.evt_Transfer AS tx
    WHERE tx.evt_block_time >= NOW() - INTERVAL '24' HOUR
      AND (
          tx."to"   = (SELECT addr FROM hl_bridge)
          OR tx."from" = (SELECT addr FROM hl_bridge)
      )
      AND tx.contract_address IN (SELECT contract_address FROM usdc_contracts)
      AND tx.value / 1e6 >= 500000   -- $500k floor — below this is noise, not conviction
),
aggregated AS (
    SELECT
        SUM(deposit_usdc)                           AS total_deposits,
        SUM(withdrawal_usdc)                        AS total_withdrawals,
        SUM(deposit_usdc) - SUM(withdrawal_usdc)    AS net_usd,
        COUNT(CASE WHEN deposit_usdc > 0 THEN 1 END)    AS deposit_count,
        COUNT(CASE WHEN withdrawal_usdc > 0 THEN 1 END) AS withdrawal_count
    FROM transfers
)
SELECT
    '{{asset}}'                                             AS asset,
    ROUND(net_usd, 0)                                       AS value,
    CASE
        WHEN net_usd >  5000000 THEN 'bullish'
        WHEN net_usd < -5000000 THEN 'bearish'
        ELSE                         'neutral'
    END                                                     AS direction_hint,
    CONCAT(
        'Whale bridge flow (>$500k, 24h): net ',
        CASE WHEN net_usd >= 0 THEN '+' ELSE '' END,
        CAST(CAST(ROUND(net_usd / 1e6, 2) AS DECIMAL(18,2)) AS VARCHAR), 'M USD. ',
        'Deposits: $', CAST(CAST(ROUND(total_deposits / 1e6, 1) AS DECIMAL(18,1)) AS VARCHAR), 'M (', CAST(deposit_count AS VARCHAR), ' txs). ',
        'Withdrawals: $', CAST(CAST(ROUND(total_withdrawals / 1e6, 1) AS DECIMAL(18,1)) AS VARCHAR), 'M (', CAST(withdrawal_count AS VARCHAR), ' txs).'
    )                                                       AS summary
FROM aggregated
