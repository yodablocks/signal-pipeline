-- smart_money.sql
-- Tracks net USDC bridge flow of known smart money wallets into/out of Hyperliquid.
-- Bridge deposit (wallet → HL bridge) = capital entering to trade = bullish intent.
-- Bridge withdrawal (HL bridge → wallet) = de-risking or profit taking = bearish intent.
--
-- Data source: erc20_arbitrum.evt_Transfer (near real-time on Dune)
-- HL bridge address: 2Df1c51E09aECF9cacb7bc98cB1742757f163dF7
-- Reference: dune.com/queries/4858738
--
-- Output columns (required by dune.py row contract):
--   asset          TEXT   — tagged with {{asset}} parameter (global HL flow signal)
--   value          FLOAT  — net USDC deposited in last 24h (positive = net inflow)
--   direction_hint TEXT   — 'bullish' (net deposit), 'bearish' (net withdrawal), 'neutral'
--   summary        TEXT   — human-readable description for the agent

WITH hl_bridge AS (
    SELECT FROM_HEX('2Df1c51E09aECF9cacb7bc98cB1742757f163dF7') AS addr
),
wallets AS (
    SELECT wallet_address, name_tag FROM (VALUES
        (FROM_HEX('f3F496C9486BE5924a93D67e98298733Bb47057c'), 'MELENIA'),
        (FROM_HEX('45d26f28196d226497130c4bac709d808fed4029'), 'Whale 27M short'),
        (FROM_HEX('20c2d95a3dfdca9e9ad12794d5fa6fad99da44f5'), 'Resolv USR'),
        (FROM_HEX('ecb63caa47c7c4e77f60f1ce858cf28dc2b82b00'), 'Wintermute'),
        (FROM_HEX('8cc94dc843e1ea7a19805e0cca43001123512b6a'), 'Whale 7M'),
        (FROM_HEX('8af700ba841f30e0a3fcb0ee4c4a9d223e1efa05'), 'Whale 13M'),
        (FROM_HEX('e4d31c2541a9ce596419879b1a46ffc7cd202c62'), 'Eric'),
        (FROM_HEX('7fdafde5cfb5465924316eced2d3715494c517d1'), 'Fasanara Capital'),
        (FROM_HEX('8589cd7a7b9e0c3ea3552facb8ff11b3135baad7'), 'James Wynn 2'),
        (FROM_HEX('bc476160765ff70cbd78d5a63aaa885555d575f1'), 'James Wynn 1'),
        (FROM_HEX('5078c2fbea2b2ad61bc840bc023e35fce56bedb6'), 'James Wynn'),
        (FROM_HEX('1f250Df59A777d61Cb8bd043c12970F3AFE4F925'), 'Aguila Trade'),
        (FROM_HEX('b83de012dba672c76a7dbbbf3e459cb59d7d6e36'), 'Abraxas Capital 2'),
        (FROM_HEX('cB92C5988b1D4f145a7B481690051F03EaD23a13'), 'Abraxas Capital'),
        (FROM_HEX('5b5d51203a0f9079f8aeb098a6523a13F298C060'), 'Abraxas Capital 3'),
        (FROM_HEX('020ca66c30bec2c4fe3861a94e4db4a498a35872'), 'Machi Big Brother'),
        (FROM_HEX('1d52fe9bde2694f6172192381111a91e24304397'), '0xtyle.eth'),
        (FROM_HEX('77375a8c9d13bf79afb2a87f1b0ac1dfd5f5bf66'), 'Investor.eth'),
        (FROM_HEX('bbbc35dfac3a00a03a8fde3540eca4f0e15c5e64'), '0xbbbc')
    ) AS t(wallet_address, name_tag)
),
transfers AS (
    SELECT
        tx.evt_block_time                           AS tx_time,
        tx."from"                                   AS sender,
        tx."to"                                     AS receiver,
        tx.value / 1e6                              AS usdc_amount,
        -- deposit: wallet → bridge
        CASE WHEN tx."to" = (SELECT addr FROM hl_bridge)
             THEN tx.value / 1e6 ELSE 0 END         AS deposit_usdc,
        -- withdrawal: bridge → wallet
        CASE WHEN tx."from" = (SELECT addr FROM hl_bridge)
             THEN tx.value / 1e6 ELSE 0 END         AS withdrawal_usdc
    FROM erc20_arbitrum.evt_Transfer AS tx
    WHERE tx.evt_block_time >= NOW() - INTERVAL '24' HOUR
      AND (
          tx."to"   = (SELECT addr FROM hl_bridge)
          OR tx."from" = (SELECT addr FROM hl_bridge)
      )
      AND (
          tx."from" IN (SELECT wallet_address FROM wallets)
          OR tx."to"   IN (SELECT wallet_address FROM wallets)
      )
),
aggregated AS (
    SELECT
        SUM(deposit_usdc)                           AS total_deposits,
        SUM(withdrawal_usdc)                        AS total_withdrawals,
        SUM(deposit_usdc) - SUM(withdrawal_usdc)    AS net_flow,
        COUNT(*)                                    AS tx_count,
        COUNT(DISTINCT CASE
            WHEN deposit_usdc > 0 THEN sender
            WHEN withdrawal_usdc > 0 THEN receiver
        END)                                        AS wallet_count
    FROM transfers
)
SELECT
    '{{asset}}'                                     AS asset,
    ROUND(net_flow, 0)                              AS value,
    CASE
        WHEN net_flow >  1000000 THEN 'bullish'
        WHEN net_flow < -1000000 THEN 'bearish'
        ELSE                         'neutral'
    END                                             AS direction_hint,
    CONCAT(
        'Smart money HL bridge flow (24h): net ',
        CASE WHEN net_flow >= 0 THEN '+' ELSE '' END,
        CAST(CAST(ROUND(net_flow / 1e6, 2) AS DECIMAL(18,2)) AS VARCHAR), 'M USDC. ',
        'Deposits: $', CAST(CAST(ROUND(total_deposits / 1e6, 1) AS DECIMAL(18,1)) AS VARCHAR), 'M. ',
        'Withdrawals: $', CAST(CAST(ROUND(total_withdrawals / 1e6, 1) AS DECIMAL(18,1)) AS VARCHAR), 'M. ',
        'Wallets active: ', CAST(wallet_count AS VARCHAR), '. ',
        'Txs: ', CAST(tx_count AS VARCHAR), '.'
    )                                               AS summary
FROM aggregated
