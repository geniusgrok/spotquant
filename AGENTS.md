# Spotquant engineering rules

Repository: geniusgrok/spotquant. Use the personal / geniusgrok GitHub connection.

## Current mandate

One manually triggered Binance BTCUSDT spot system. Long or USDT cash. No borrow, no short, no futures, no leverage. One model, one Binance spot adapter, one configuration. The model is SMA 40, two confirmed closes, a fresh cross, a close at least half the inclusive 252-day highest close, a 60% blow-off, a crash reversal that rises at least 6% after a day down at least 8% while still at least half under the inclusive 400-day highest close, a repair hold that ignores the SMA exit, the blow-off, and the 4% close until the handoff, and an amended 28% stop. One manual start runs repeated read-only cycles until its deadline or interruption. `run --execute` stays blocked because native qualification remains `NOT_QUALIFIED`. Economic qualification on P3 does not lift that block.

Economic window: 2020-01-01T00:00:00Z through 2026-09-20T00:00:00Z exclusive. Start CNY 10,000, no additions. Targets: cost-net CAGR >= 100% and continuous max drawdown <= 30%. P3 measures 101.67% CAGR and 29.15% MDD, so its economic qualification is `MET`. Do not move the window, lower the targets, add leverage, or present a lookahead path as the account result. A stress that misses a target does not change the base trial.

The meter is `python -m research.rebuild`. It is a daily-bar account, not a replay of `session.run` through historical order books. The live session only previews. Say that difference when quoting numbers. An entry preview stops 28% under the completed close. When a crash reversal and an ordinary entry are both armed, the preview is the repair entry. Path marks use 00:00 UTC; the daily curve and the final mark use the end of that UTC day.

## Execution safety

Default read-only. Engineering work does not authorize orders, transfers, credentials, or account-setting changes. Unknown balances and external BTC block new risk. Do not treat a new empty state directory as proof the account is flat. Do not invent a background daemon.

Credentials: `SPOTQUANT_BINANCE_KEY` / `SPOTQUANT_BINANCE_SECRET` for live, `SPOTQUANT_BINANCE_DEMO_KEY` / `SPOTQUANT_BINANCE_DEMO_SECRET` for demo. Client ids use `sq-`. Scope is `binance:BTCUSDT:spot:{live|demo}:{uid}`.

## Verification

One CI workflow, Python 3.13, `contents: read`, timeout 10 minutes, no secrets, no market download, no full historical research. Runtime dependencies stay in the standard library.
