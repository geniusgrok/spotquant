# USDT market-cap slope: first source receipt and qualification

2026-10-07 UTC. The [frozen protocol](usdt-cap-slope-20261007-PROTOCOL.md), [on-demand collector/parser](usdt_cap_slope_20261007.py), [raw public response](usdt-cap-receipts/1791384086918-ed1b3448797f.raw.json), and [receive-time manifest](usdt-cap-receipts/1791384086918-ed1b3448797f.receipt.json) are on this archive branch. No main/runtime or account change.

## Read-only source result

One saved keyless CoinGecko GET returned HTTP 200. Request started **2026-10-07 14:41:26.600 UTC**; all response bytes were received **14:41:26.918 UTC** (22:41:26.918 UTC+8). The 879 raw bytes have SHA-256 `ed1b3448797fd23d30060a57544abe6bd1e90ba5e06d060a10c67a17af21bc11`; the manifest has SHA-256 `81215bd290d2c87913e599fd736d54b6eca4e5155b5f7ece80177ae9cabe079b`. Both archive files were read back byte-for-byte after upload. An earlier availability probe made one other public GET without retaining an observation; it is not a sample.

The response has seven paired UTC-midnight market-cap/USD-price points and a current non-midnight point in each array, which the parser ignores. Its latest completed point is **2026-10-07 00:00 UTC**. From the as-received 5/6/7 October points, the frozen per-day log slopes are market cap `+0.0000250390324241`, USDT/USD price `+0.0000195215884065`, and price-adjusted implied supply `+0.00000551744401764`. This is a **source-qualified single receipt**, not a BUY event, predictive success, minting event, account gain, or proof the older points were known on their dates.

The first receipt preserves original values; later on-demand receipts must use new files and link predecessor hashes and revisions. The source's D+1/D+2 revised history is never substituted for an earlier as-of snapshot. Official [daily availability](https://docs.coingecko.com/demo/reference/coins-id-market-chart) and [market-cap revision](https://support.coingecko.com/hc/en-us/articles/61976309053337-Why-do-historical-market-cap-values-change-shortly-after-a-date-then-settle) semantics motivated those checks.

## Information gate

All existing accepted historical Spotquant BUY decisions predate this first proof of availability. Eligible actual new BUY decisions **0**, independent seven-day outcomes **0**, account comparisons **0**. Therefore there is no qualified action or economics to test and no basis to edit the running crowding/BTC/SELL/STOP path. The old development accounts remain bound to their original source SHA; this receipt does not retrofit them with a new signal.

The frozen source point can only be considered at a real manual new-BUY decision after its receive time and before the next daily point could be published, **2026-10-08 00:10 UTC** (08:10 UTC+8); none was observed here. Each later independent opportunity requires a newly received source version and a genuinely future seven-day result. At least ten non-overlapping mature windows and two supported chronological halves are required before a single half-budget NEW BUY candidate can enter a same-wallet account comparison. No daemon, repeated historical download, paid plan, key, Binance request, trading or deployment was used.

Minimal local reproduction of this saved first receipt: place the two receipt files together, run `python research/usdt_cap_slope_20261007.py selfcheck`, then `python research/usdt_cap_slope_20261007.py signal <receipt-directory> --decision-ms 1791384086918`. The signal command reports source qualification only and explicitly leaves manual BUY and future outcome unverified.
