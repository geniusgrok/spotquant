# Single supported STOP re-entry: bounded paired diagnostic protocol

Frozen on 2026-10-07 before inspecting paired outcome. This is a development diagnostic, not a preregistered prospective test or native account result.

## Identity and qualification
- Fixed case commitment: SHA-256 `e3bf2304e1ea0590db53302ab71dbee0975d7629e65fe10b9a9367d6dded1b06` over the already selected native-STOP/sleeve/trigger-bar/candidate-bar tuple; do not search another case after this result.
- Original production source: `74bd6e035e36531c517029077c1a9e2e5ec44516`, clean; original complete account gzip SHA-256 `4c937ecf80f60d957486a752562c8ab8dfee4c06fa2e5b38b135cfb24ec38872`; existing daily composition SHA-256 `6a35dadcbe9228d95191a2d2716bc2c2d9f01aa8be08638718325fbefb376009`. No fresh market download.
- The original reducer, ordered fills, durable allocations and saved final positions have already been checked in `research/reconstruct-stop-state-20261007-RESULT.md`. This establishes one source-bound as-of candidate state, not a saved original SQLite checkpoint or new fill.
- Fixed treatment: only the earlier specified reason-aware re-entry after a terminal native STOP, seven completed days, two bullish closes and the prior-five-close high. No threshold, stop, crowding or fee changes. The incumbent is the original recorded decision.
- First run original `preview.decision` on both candidate states using the same pre-order cash, BTC, completed bar, venue price, ownership, fixed capital limit, and allocation scale. Verify the incumbent output equals the original accepted order and that the treatment changes the actual external MARKET BUY. A change solely in sleeve labels terminates the wallet test.

## One affected window
- If the executable order changes, compare from the original candidate order decision until the next UTC daily mark after the **first subsequent original full MARKET liquidation of the original BUY cohort**. This event-bound endpoint is fixed before paired outcome calculations. Do not extend to a favorable later interval.
- Carry forward all three sleeves, including residual dust; attribute the actual pooled candidate fill by the original equal-weight BUY rule. Preserve the original manual session clock, available completed bars, daily O-H-L-C venue proxy, original protective orders, SELL-first handling and original crowding input/scale. No invented extra sessions.
- Apply the original base fee 0.10%, MARKET slip 0.05%, stop slip 0.10%, BTC fee on BUY and USDT fee on SELL to both arms. Start from the identical reconstructed wallet and capital limit. Mark both arms at the same endpoint; report full account change, common-path daily trough/drawdown, turnover and total fees. Explicitly attribute extra BTC funded by formerly idle cash and capital diverted from the other BUY sleeves.
- If a changed candidate order/stop makes the archived schedule insufficient to reproduce a safe paired path, report the exact first divergence and stop. Do not substitute the original future fills or treat a marked position as a realized exit. No account command, credential, deployment, full-history replay or parameter rescue.

Decision rule: a bounded positive result only warrants further independent evidence; a loss, larger tail or unsafe lifecycle closes this candidate for adoption. No main change without clear cost-net account benefit and safety evidence. The historical source remains an OHLC proxy; actual account-days and prospective samples remain zero.
