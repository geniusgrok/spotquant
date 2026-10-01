# BTC offline lifecycle and personal workflow — 2026-10-01

`execution.json` is the current offline evidence. It runs nine money
and recovery cases, then uses the actual P4 Model and portfolio preview,
existing `follow.apply_day` sleeve attribution, durable SQLite order IDs and
an in-memory venue. Lost ACK/restart does not duplicate entry; terminal partial
fills protect actual coins; exits release confirmed protection, settle proceeds
and re-protect remaining coins after a terminal partial sale. Unexplained
balances and unsupported stop replacement block rather than claim success.
Earlier execution files and their source identities remain in Git history
at `6411a9b`; the working tree retains the current result.

This is a minimum offline executor, not a Binance writer. Only the concrete
in-memory adapter and separate offline state are accepted. Stop locking,
amendments, partial-fill timing and cancellation/sale protection gaps remain
unverified native semantics. Public `run --execute` is still blocked.

`observation-current.json` records the actual 2026-10-01 attempt and official
archives through 2026-09-30. It is market/archive observation, not an account
observation or real order ledger. There is one distinct observed UTC day;
30-day operational completion is false. Earlier historical bars are labeled
backfill. `input-restoration.json` checks a cold restoration of 111 frozen
archives against the original market identity, without replacing originals.

Commands:

```sh
python -m research.execution_replay --out evidence/execution-NEW.json
python -m research.operations observe --refresh
python -m research.operations combine spot-snapshot.json perpetual-snapshot.json
```

Observation is one manual invocation, not a daemon. It restores/verifies frozen
inputs, refreshes only completed forward days, pins P4 rules and saves attempts
under `~/.local/state/spotquant/observations`. Missing/failed days remain visible;
repeated attempts do not manufacture additional observation days.

Combined-report inputs each require `known: true`, `symbol: BTCUSDT`, `market`
(`spot` or `perpetual`), `environment` (`demo` or `live`), string `account_uid`,
integer `observed_at_ms`, and decimal strings `equity_usdt`, `btc_position`,
`btc_price_usdt`. Equity already includes unrealized PnL; do not add collateral
again. Inputs must be no older than 120 seconds, within 5 seconds of one another,
and use one valuation price/environment. Unknown and incomplete accounts are
refused. Report gross and net BTC exposure separately; a hedge does not erase
gross exposure. The 10% price shock is linear and omits liquidation/funding/costs.
No actual account snapshots are available here; no real combined result is claimed.

Spotquant remains the spot destination. A sole perpetual executor will be chosen
after comparable economic and native execution evidence; no third project is
introduced for reporting.
