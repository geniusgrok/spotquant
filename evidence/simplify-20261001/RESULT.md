# P5: rule deletion decision — 2026-10-01

Registered in `research/PROTOCOL.md` before measuring. Reproduce with
`python -m research.rebuild --simplify` after restoring official BTC daily
archives under `/tmp/spotquant-market/klines`. Code commit `d80d74d`; each JSON
records the full source and market identities. All 25 account ledgers are kept.
The restored P4 final CNY, continuous MDD and trade count reproduce exactly.

| Book | Cost-net CAGR | Continuous MDD | Closed trades |
| --- | ---: | ---: | ---: |
| P4 | 78.49% | 31.40% | 123 |
| No adverse close | 75.30% | 34.48% | 123 |
| No blow-off | 73.64% | 31.40% | 123 |
| No crash reversal/repair | 58.92% | 31.40% | 143 |
| All three deletions | 54.23% | 34.48% | 142 |

Decision: **retain P4**. None of the four deletions passes the registered
matched-scenario rule (at most one point CAGR loss and no higher MDD in each
of base, fee, slip, skip and outage). Even the base rows reject all four.
The sparse historical events do not justify deleting their rules under this
criterion. They also do not establish out-of-sample reliability.

The implementation reuses `spotquant.model.Model` and `simulate_sleeves`;
there is no second model or production selector. CLI flags cannot override
the registered scenario constants. P4 economic qualification remains
`NOT_MET` and native qualification `NOT_QUALIFIED`. This is a daily-bar
account measurement, not a historical replay of the live session or order book.

74 offline tests pass. Next model work should target risk or exposure rather
than repeating this deletion search. The long-term destination remains one
BTC spot project alongside one perpetual project.
