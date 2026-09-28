# Spotquant engineering rules

Repository: geniusgrok/spotquant. Use the personal / geniusgrok GitHub connection.

## Current mandate

One manually triggered Binance BTCUSDT spot system. Long or USDT cash. No borrow, no short, no futures, no leverage. One model (SMA 40, two confirmed closes, a fresh cross, a 252-day crash filter, and an amended 28% stop), one Binance spot adapter, one configuration. One manual start runs repeated read-only cycles until its deadline or interruption. `run --execute` stays blocked. Qualification remains `NOT_QUALIFIED`.

Economic window: 2020-01-01T00:00:00Z through 2026-09-20T00:00:00Z exclusive. Start CNY 10,000, no additions. Targets: cost-net CAGR >= 100% and continuous max drawdown <= 30%. P2 measures 75.19% CAGR and 33.10% MDD. Do not move the window, lower the targets, add leverage, or present a lookahead path as the account result.

The meter is `python -m research.rebuild`. It is a daily-bar account, not a replay of `session.run` through historical order books. The live session only previews. Say that difference when quoting numbers.

## Execution safety

Default read-only. Engineering work does not authorize orders, transfers, credentials, or account-setting changes. Unknown balances and external BTC block new risk. Do not treat a new empty state directory as proof the account is flat. Do not invent a background daemon.

Credentials: `SPOTQUANT_BINANCE_KEY` / `SPOTQUANT_BINANCE_SECRET` for live, `SPOTQUANT_BINANCE_DEMO_KEY` / `SPOTQUANT_BINANCE_DEMO_SECRET` for demo. Client ids use `sq-`. Scope is `binance:BTCUSDT:spot:{live|demo}:{uid}`.

## Verification

One CI workflow, Python 3.13, `contents: read`, timeout 10 minutes, no secrets, no market download, no full historical research. Runtime dependencies stay in the standard library.
