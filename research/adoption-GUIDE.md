# Conditional canonical ATR-stop proposal

This branch implements the registered `atr-stop` semantics in the shared default session. It is a reviewable proposal. It has not passed the final financial selection, independent review or five-account adoption bridge. Existing P4 results remain historical; economic qualification remains NOT_MET and native qualification remains NOT_QUALIFIED. No real account or prospective account-days are created by a historical replay.

The stored Model uses version 5 checkpoints with fourteen timestamped completed true ranges. The first origin bar contributes no range. The shared decision copy uses `clip(4 * ATR14 / completed_close, .10, .30)` and the measured campaign-bounded allocated native stop floor. Persistent `trail=.28` and follow catch-up remain unchanged. The same decision helper protects terminal partial-sale remainders. Stops through the actual decision mark become ordinary reductions; new BUYs defer to SELLs and are clipped to free cash and the remaining whole-account capital ceiling. Consensus still requires genuine entries and never resizes owned positions.

Cycle entry rejects old rules, old or malformed checkpoints, research wrappers, wrong sleeve/rule parameters, missing checkpoints over durable state and incompatible pending allocations before Lifecycle recovery. It does not rewrite old state, remove orders or infer a flat account from a new directory. Migration or recovery with a frozen old executable is a separate controlled procedure.

After the original financial chain has finished and this committed source is reviewed and frozen, the coordinator may run these five accounts serially with the original inputs:

```sh
python3 -m research.adoption_spot --scenario base --out /tmp/adoption-base-NEW.json
python3 -m research.adoption_spot --scenario fee150 --out /tmp/adoption-fee150-NEW.json
python3 -m research.adoption_spot --scenario slip2 --out /tmp/adoption-slip2-NEW.json
python3 -m research.adoption_spot --scenario outage --out /tmp/adoption-outage-NEW.json
python3 -m research.adoption_spot --scenario base --risk-calibration /path/to/reviewed-project-calibration.json --out /tmp/adoption-risk-base-NEW.json
```

`research.complete_spot.measure(..., canonical=True)` is the explicit meter seam. It reuses HistoricalVenue, Config, session.run, canonical State/Model/decision/Lifecycle, monetary audit and archive checks. It neither constructs Policy nor enters configured; it rejects preinstalled research engine replacements. There is no copied exchange or account engine. Row schema, including filters, is retained. Canonical provenance and risk binding use the existing research additions and top-level adoption metadata; no assessor exclusion changes are needed.

Only this offline replay attaches a verified `atr-stop` calibration profile to the venue. Its exact file hash, full profile hash, candidate, rule, fixed 2022-01-01 cutoff and scale bind the canonical State atomically with the model checkpoint. A changed or missing profile fails before recovery. Scale is 1 before the cutoff and fixed afterward, applied only to new BUY allocation. There is no live configuration option and the proposed unscaled default remains 1. The calibration file is reverified after each replay.

Run `python3 -m unittest discover -s tests -v` and `python3 -m compileall -q spotquant research tests` for offline verification. Synthetic tests establish mechanism and local six-group compatibility, not historical equivalence. Full financial replay is not part of the implementation test suite.

The immutable analysis99 report and original frozen Spot8ca/Coinaced accounts remain authoritative. The coordinator must compare every one of the five adopted accounts to its corresponding frozen actual account with immutable analysis99 helpers, require all six groups equal and all archives complete/known/audited, and bind exact Git/Python/spec/protocol/input/calibration identities. Existing `alpha_assessment` source and forward gates are unchanged. Running old research CLIs on this new source must not be described as reproducing the frozen executable. An eligible diary can remain bound to the frozen selected research implementation; it is not an adopted-runtime or trading-account claim.

If selection or the exact bridge fails, keep the incumbent default and report the candidate as research-only. Live writes, private account access, background operation and native qualification remain unchanged.
