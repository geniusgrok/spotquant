# Spotquant: ordinary MARKET exits and manual-entry signal age

2026-10-06. Read-only development-history screens. This records two closed questions; it does not modify the strategy or create a new economic account.

## Source and frozen decisions

Current main when measured: `7f2e25df69e024e669ffc2426b3be1c82af55879`. Original accepted projection: `evidence/btc-flow-risk-20261004/spot-baseline-projection.json.gz` on this archive branch, raw SHA-256 `ef32a84a7e89cfd7ea3d76a8c6fa56a1da95546500269a8a0b688047f94a2118`. The original projection binds its own older economic producer; this study does not relabel it as the current main. The earlier `research/search-RESULT.md`, `edge-RESULT.md`, `progress-RESULT.md`, `lifecycle-RESULT.md`, and `search-next-RESULT.md` were checked to avoid reopening failed routes.

The two accompanying JSON specifications were frozen before their respective outcome screens. The scripts use existing source bytes only and do not fetch market data or run strategy sessions. Reproduction requires the existing archive projection and `evidence/btc-search-20261005/search-artifacts.zip` extracted to the `/tmp` paths named in the scripts. No copy of those data files is added here.

## MARKET exit attribution: closed

Of 129 fills, there are 129 distinct filled order IDs: 62 MARKET buys, 53 MARKET sells, and 14 filled native stops. No filled order has multiple partial fills in this projection. Stop replacements are distinct instructions, not extra fills; the remaining BTC is documented dust.

All 53 MARKET sells match exactly one accepted decision. Every owned sleeve in those sells has action `exit`; the shared orders span sleeves 30, 40 and 50. There are 156 FIFO attribution fragments across the 53 MARKET orders. Counting negative fragments as independent opportunities would exaggerate the number of losses; some orders contain offsetting fragments. At the *order* level, 24 MARKET sells have negative realized net cash attribution, while the set of all MARKET sells has positive net attribution. The full 129-fill attribution reconciles to the previously accepted diagnosis.

The original bar-open-to-decision interval is about 24–47 hours, corresponding to about 0–23 hours after the daily bar completed. Decision-to-modeled-fill delay is 2.6–4.0 seconds. Recorded adverse decision-to-fill price difference is tightly grouped near 5bp, matching the historical model's fixed market impact. These synthetic fills do not identify a repeatable abnormal venue delay or live avoidable slippage. Stops have no recorded decision price/time in the fill event; their delay remains unknown. The fixed execution-loss hypothesis fails its pre-account gate. Neither faster trading nor a new exit rule is supported by this result.

## BUY signal age: closed

The single frozen action was to decline an original *new BUY* when its accepted decision was more than 12 hours after the source daily bar completed. It leaves owned positions, sells and protective stops intact. Among 62 real original BUY batches, it would affect 24: 10 in 2020–2021 and 14 in 2022–2026. Both affected groups have **positive** original fee-inclusive FIFO realized cash attribution. They are independent orders, not repeated polls or sleeve copies. No event lies near the 12-hour boundary, so the modeled 60-second bar-availability convention does not change classification.

The fixed screen requires affected original cohorts to be detrimental in both periods before an account is warranted; it fails. Their recorded outcome is descriptive, not the compounded counterfactual of omitting them, but it is enough to reject the proposed gate. The rule would reduce BTC participation and remove historically profitable exposure. The threshold, sign, and periods will not be rescanned.

## Adoption and limitations

No changed-account CNY CAGR, continuous or proxy drawdown, tail loss, turnover, participation, or start-time robustness was measured because neither question passed the pre-account gate. The older complete historical backtest remains development evidence, not a new prospective result; the 100% CAGR / 30% maximum-drawdown target remains unmet and native account days remain zero. No runtime code, `main`, CI, private account, deployment, credentials, or always-on collector changed. This archive record preserves negative evidence without enlarging the main branch.
