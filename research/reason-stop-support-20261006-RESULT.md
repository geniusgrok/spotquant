# Frozen STOP reentry: whole-history support census

The earlier reason-stop-reentry wallet comparison covered only the registered 2020Q2 and 2023Q4 windows. Its journals contain no confirmed native STOP_LOSS close, so its zero effect cannot answer full-history treatment support. This check uses the unchanged 7 completed-day wait from the last completed bar at the actual fill, two bullish closes, intact trend, 252-day high filter, no extension/crash-reversal arm, and close strictly above the preceding five completed closes. No threshold or date is selected from returns.

Run from this archive branch's root:

```sh
python -m research.reason_stop_support_20261006 \
  evidence/btc-flow-risk-20261004/spot-baseline-projection.json.gz \
  evidence/btc-progress-20261005/research-artifacts.zip
```

The script verifies both existing source checksums and the day packet checksum, then compares every original logged decision close and bullish vote with the recovered completed daily bars. It inspects **all** original STOP_LOSS fills without selecting winners: terminal native receipt, intended and executed quantity, durable sleeve allocation, actual fill quantity, reconstructed group-close ownership, final durable `sell_applied` counters, and final retained position consistency. It applies the frozen price rule per allocated sleeve before that sleeve's next actual BUY and checks whether any resulting arm can reach an already scheduled original BUY session. The computation sends no request, runs no wallet, places no order, and does not restore or replay an account.

The original lossless projection retains final positions but no per-decision durable `dust` or `sell_applied` position snapshot. Reconstructing an intermediate closed residual from fills and checking final counters is useful forensic evidence; it is not the missing as-of durable state. The script reports this evidence tier separately and returns zero **strictly persisted-state-qualified** opportunities under the requested rule. Any price-pattern match remains provisional; it cannot be promoted to an executable treatment or finite account comparison solely from this record. The prior two-window zero effect and the original target failures remain unchanged. Main is unchanged.
