# P6: BTC simple baselines — 2026-10-01

Registered before measurement in `research/PROTOCOL.md`. All 12 complete
accounts are preserved, using CNY 10,000 with no additions, the frozen window,
P4 costs and ex-post FX. P4 final value, CAGR and continuous MDD reproduce.

| Account | Cost-net CAGR | Continuous MDD | Longest daily-close underwater run |
| --- | ---: | ---: | ---: |
| P4 | 78.49% | 31.40% | 485 days |
| USDT cash | -0.60% | 11.99% | 852 days |
| First-day buy and hold | 42.55% | 75.02% | 841 days |
| Initial cash allocated over 12 months | 36.41% | 75.02% | 841 days |

No simple baseline dominates P4 in all three matched cost scenarios. P4 has
historical incremental value over these BTC passive books, so further execution
work is justified. This is in-sample evidence, not proof of future superiority.
The original 100% CAGR / MDD <=30% goal is still NOT_MET. Cash means USDT,
not CNY: its CNY drawdown comes from exchange-rate valuation. Daily underwater
duration is not continuous intraday duration. No forced terminal BTC sale is
charged to either the passive books or P4.

Reproduce from a clean committed tree into a new output directory:

```sh
python -m research.restore_btc --through 2026-09-19
python -m research.baselines --out evidence/baselines-NEW
```

Source commit `94f423c`, market SHA-256 `3f6151860a87e8f82f315dc59581d6b73e4debf1ef5e3b86fb2e4d1ca987954e`.
The account engine remains a daily-bar meter, not an order-book/live-session replay.
