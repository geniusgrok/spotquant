# BTC spot complete delivery — registered 2026-10-01

Implements all research and engineering directions explicitly authorized by user.
BTCUSDT long/cash only, no leverage/deposits/account requests. CNY10,000;
2020-01-01 to 2026-09-20 UTC exclusive, original 100% CAGR/MDD<=30% targets.
Actual session.run/cycle/Lifecycle, common frozen 795 Coin primary starts,
300s/5s sessions; only venue stops act during downtime. Common prior-date FX
and 0.001 conversion each way. Price inputs remain explicitly historical proxies.
Day stop uses prior completed high; a later high cannot alter an earlier low.

Fixed candidates before measuring:
1. default P4 SMA30/40/50;
2. consensus cash: when at least two real P4 sleeves enter/hold bullish, allocate
   90% rather than the existing share to current newly entering sleeves, bounded
   by free cash/capital and 100% total BTC. Never fabricate entries or change
   cold-start/fresh-cross requirements; durable sleeve weights preserve ownership;
3. downside: on completed daily close below SMA40 AND negative 5-day return,
   reduce owned BTC by half once; restore only after two consecutive closes
   above SMA40 through a new authorized strategy intent, without synthetic fills.
   Keep native protection of residual quantity and enforce ownership throughout;
4. funding entry filter: block new buys if latest settled rate lagged8h >0.0003;
5. previous-day futures/spot close basis filter: block buys if premium>0.01.
Missing/unpublished feature inputs block filtered candidates, never invent values.

Each runs base (fee0.001, entry/exit slip0.0005, stop slip0.001), fee x1.5,
slippage x2 and fixed 2020-03-01..2020-03-22 no-client-session outage. Keep full
cash/BTC/fill ledger, daily curves and between-session protection events.
Complete known-path baseline and independent money audit before selection.
Candidate eligible if every matched scenario has MDD no higher than incumbent
(1e-9), CAGR no lower by more than0.01; base CAGR improves>=0.01 OR MDD
improves>=0.01 with CAGR loss<=0.03. Choose highest worst-case CAGR then
smallest change. Economic targets and native qualification reported separately.

Data: official frozen spot daily archives; conservative intraday stop handling
and timestamped completed candles must be labelled. No actual LOB/queue proof.
Funding/basis use checksum-verified public inputs with timing lag and hashes;
they are signals only, no funding paid/received by the spot account.

Delivery also includes durable incremental fill reconciliation, immutable bounded
session archives and online backups, read-only backup validation/restore drill,
source-bound native evidence audit, alpha/beta attribution, fixed-capital joint
accounts, operator guide, independent review, CI and normal PR integration.
True Demo events/30 natural days cannot be replaced with synthetic/history data.
