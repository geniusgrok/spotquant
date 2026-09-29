# Spotquant engineering rules

Repository: geniusgrok/spotquant. Use the personal / geniusgrok GitHub connection.

## Current mandate

One manually triggered Binance BTCUSDT spot system. Long or USDT cash. No borrow, no short, no futures, no leverage. One model class, one Binance spot adapter, one configuration. The default book is P4: three SMA sleeves (30, 40, 50) on one USDT pool. Every sleeve uses two confirmed closes, a fresh cross, a close at least half the inclusive 252-day highest close, a 61% blow-off, a crash reversal that rises at least 7% after a day down at least 11% while still at least half under the inclusive 400-day highest close, a repair hold that ignores the SMA exit, the blow-off, and the 4% close until the close is back above the SMA and no more than 11% under that high, the 4% close, and an amended 28% stop. One manual start runs repeated read-only cycles until its deadline or interruption. `run --execute` stays blocked because native qualification remains `NOT_QUALIFIED`.

Economic window: 2020-01-01T00:00:00Z through 2026-09-20T00:00:00Z exclusive. Start CNY 10,000, no additions. Targets: cost-net CAGR >= 100% and continuous max drawdown <= 30%. P4 measures 78.49% CAGR and 31.40% MDD, so its economic qualification is `NOT_MET`. The single-sleeve P3 book measures 101.67% and 29.15% and stays in the evidence as the in-sample upper bound; its neighbors SMA 30, 35, 45, and 50 print 58.93%, 59.08%, 94.83%, and 70.37%. Do not move the window, lower the targets, add leverage, present a lookahead path as the account result, or describe P3 as the expected return. State `NOT_MET` when quoting P4.

The meter is `python3 -m research.rebuild`. It is a daily-bar account, not a replay of `session.run` through historical order books. The live session only previews. Say that difference when quoting numbers. An entry preview stops 28% under the completed close. A hold with no recorded fill uses that close. A followed fill starts the preview peak at the fill. Sleeves on one order share that fill. A second order id, or two sleeve groups of the same size, is unknown. A balance drop is a sleeve exit only when one sell order leaves exactly those sleeves' coins. The stop in force is the prior peak; the bar's high tightens it for the next day. End-of-day CNY enters the drawdown. Cold start waits for a fresh cross. `run --execute` stays blocked; an empty intents table is not an order lifecycle. The seeded skip stress prints 32.63% drawdown. The 2020-03-01 outage stress prints 65.08% and 31.40%. Any change to the rules goes through the protocol in `research/PROTOCOL.md`: declare, then measure, then decide mechanically.

## Execution safety

Default read-only. Engineering work does not authorize orders, transfers, credentials, or account-setting changes. Unknown balances and external BTC block new risk. Do not treat a new empty state directory as proof the account is flat. Do not invent a background daemon.

Credentials: `SPOTQUANT_BINANCE_KEY` / `SPOTQUANT_BINANCE_SECRET` for live, `SPOTQUANT_BINANCE_DEMO_KEY` / `SPOTQUANT_BINANCE_DEMO_SECRET` for demo. Client ids use `sq-`. Scope is `binance:BTCUSDT:spot:{live|demo}:{uid}`.

## Verification

One CI workflow, Python 3.13, `contents: read`, timeout 10 minutes, no secrets, no market download, no full historical research. Runtime dependencies stay in the standard library.
