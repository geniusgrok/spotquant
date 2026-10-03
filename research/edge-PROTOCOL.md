# Frozen BTC alpha/beta edge protocol — spot

Registered 2026-10-03 before new financial outcomes. The machine-readable contract is `edge_spec.json`; it is the finite registration, not a parameter search. Project `spot` baseline `atr-stop` remains scale1. No results or promotion are claimed here. Native qualification remains NOT_QUALIFIED and actual account-days zero.

## Economics and execution

BTCUSDT; initial CNY10000/no additions; 2020-01-01T00:00:00Z through 2026-09-20T00:00:00Z exclusive; original795 starts,300-second sessions/5-second polls, original FX/conversion/cost/latency/protection/ownership gates; stopped clients take no actions. Spot long/cash/no leverage; Coin one-way isolated/default no shorts, primary7.5/macro3.6. Goals Spot CAGR>=100%/continuous MDD<=30%, Coin>=150%/continuous MDD<50%. Native qualification remains NOT_QUALIFIED/actual account-days zero. Current Spot ATR scale1 and Coin incumbent are controls.

## Registered mechanisms, exact order

### 1. exit-confirm

scope: ordinary SMA exit only; rule: ordinary SMA exit waits until BOTH latest completed close <= its current SMA AND immediately preceding completed close <= its own contemporaneous SMA; equality counts below; missing: missing previous close/SMA history preserves the original ordinary exit, never a delay; preserve: ['repair', 'adverse', 'overextended', 'protection exits', 'fresh entry', 'ATR stop']; held_topup_rebalance: False.

### 2. stop-budget

scope: genuine NEW BUY only; account_risk_fraction: .12; risk: proposed initial stop loss + cash fee reserves + sum(max(0, current_price - confirmed allocated protection stop) * owned_qty) of all existing owned sleeves <= .12 * current whole-account marked USDT equity; proof: proven ownership and allocated stops required; missing ownership/protection blocks new risk; distance: actual proposal entry/stop with original fee/slip assumptions; spend: min(original proposal/free cash/90% pool/capital gates, remaining risk / distance); never increase original proposal; held_topup_rebalance: False; disclosure: risk budget, not protection against gaps.

### 3. crowding-interaction

scope: genuine NEW BUY only; funding_gt: .0003; basis_gt: .01; weak_momentum: current completed daily close <= close five completed days earlier; rule: halve original allowed spend only if all three conditions; otherwise preserve original proposal, including strong momentum; missing: missing/stale funding/basis or causal history blocks new BUY only; safety exits remain; held_topup_rebalance: False.

All three mechanisms are compatible at their distinct decision scopes. Combine every individually eligible mechanism in registered order, with all sizing caps preserved; do not search subsets. Simple funding OR basis entry filters were prior failed research, not new hypotheses. Task1's verified Spot flat-day attribution supports testing ordinary-exit confirmation. Coin first divergence includes different observed marks and consequent sizing/rounding; no precise unnecessary same-observation dependency was established. No automatic phase fix or selected winning schedule offset is registered. Macro raw IDs remain actual negative creation timestamps; diagnostic association is not operating equality.

## Verified point-in-time feature inputs

The registered raw crowding SHA-256 is `1d87be0b4c8cd8a7eacd4970a1a194e5f1ed0b2f3aa3710a43417aa9ab066ef2`, 7488 funding and 2485 basis records. Reuse the controller's `research.prepare_inputs.build` reconstruction from official caches, independently compare both raw byte hashes, then recheck official archive CHECKSUMs, sizes, warmup and aggregate spot identities. Never accept copied metadata in place of verified bytes. The immutable reconstruction is outside the repositories; absent identity mismatch do not repeat full market reconstruction.

`edge-features-v1` contains funding/basis `{observation_ms, available_ms, value}` records, `source` raw and market hashes plus availability assumptions, and real `coverage`. Decimal values are strings; explicit missing values use null plus a nonempty cause. Content SHA-256 covers canonical JSON excluding its own field; file SHA-256 may be separately pinned by `FeatureBook(path, expected_sha256=...)`. These hashes bind content/provenance; they are not signatures. Each standalone repository supplies the same validated reader without a sibling runtime import or adapter requests.

Funding observation is the actual historical settlement stamp including jitter; availability is observation+28800000ms. Select only the latest record whose availability <= now. It is stale at age >=28800000ms since availability, exactly, with no repair/tolerance; negative rates remain negative. Predicted rates and future cashflows cannot become earlier features.

Basis observation is the previous matched UTC-day completion boundary, exactly raw available_ms-60000; retain raw stamps. The raw basis was previous daily futures trade close/spot trade close-1 at day-open+DAY+60000. Publication 60000ms after completion is a modeled assumption, not observed exchange publication. Require max age1DAY and the current UTC availability date. This is a trade-close proxy, not instantaneous executable basis. Before availability, during gaps, on stale/date mismatch, or outside the frozen historical window, return None with a specific journal cause. The complete raw tail remains preserved for identity but cannot extend historical lookup beyond the exclusive endpoint. Forward observations require a separate collection/artifact, never historical backfill. Completed trend bars remain in each project's causal model/trigger history; this module produces only the public funding/basis inputs, not a replacement bar history.

Spot builder: `python3.13 -m research.edge_features --build --crowding PATH --verified-crowding VERIFIED_PATH --perp-repo REPO --perp-market PATH --spot-market PATH --out NEW_PATH`. Defaults use sibling Coin only for verification of official warmup bytes and the official `/tmp` caches; the reader itself is standalone. Output creation is exclusive and existing outputs reject. Readers validate JSON uniqueness, file/content/raw/market identities, format, finite Decimal values, strict unique sorted observation/availability stamps, exact lag, UTC basis boundaries and recomputed frozen-window coverage. `last_lookup` optionally journals selected observation, availability, age, missing cause and hashes. No native requests or financial replay are involved.

## Controls, stresses and baseline equality

Cash plus protected-participation-25/50/75/100 use actual funded accounts, same finite session/Lifecycle, installed28% protective stop, no held topup/rebalance, genuine cold-start gates and next completed day after actual stop before re-entry. Their different signal/protection rules must be disclosed; they are not pure buy-and-hold or constant weights. No fractional daily-open curve is claimed executable.

Spot base/fee150/slip2/outage; Coin base/fees-x1.5/read-400ms/trigger-slip. All registered single mechanisms run all four stresses even if base loses. Spot baseline+3 mechanisms+5 controls produces36 unscaled accounts; Coin baseline+3 mechanisms produces16. Actual calibrated base reruns include baseline+3 candidates per project. Coin incumbent ±60s base accounts are diagnostics only. Applicable combinations run all four stresses and actual calibrated base. Accepted baseline replay must match all six original evidence groups (financial/fills/daily/ownership/remaining_original_fields/operating) under existing allowed normalization; unexplained mismatch blocks promotion. New envelopes bind exact sources without changing old raw fields to manufacture equality. Original Spot canonical meter and Coin incumbent no-op hooks may establish equality.

## Risk and adoption

Risk calibration uses only2020–2021 USDT731days. Scale=min(1,baseline train vol/candidate train vol when candidate vol>0,max(.01,baseline train beta)/max(.01,candidate train beta) when candidate beta exceeds baseline and candidate beta>0). The .01 floors apply only to beta ratio operands; there is no outer .01 scale floor and scale0 is valid. Use scale1 before2022. Actual new order sizing only after cutoff1640995200000; no retroactive held shrink or curve scaling. Each project writes its OWN calibration document with own candidate-keyed profiles, binding project kind, baseline, spec/source/input identities. Rerun baseline scale1 and each candidate actual base. Achieved validation upper bands require vol<=baseline1.05 AND beta<=baseline+.02; this is not exact risk equality. All historical data are contaminated; regression intercepts/CIs remain descriptive.

Promotion requires complete/known/audited original matched stresses; every scenario CAGR>=baseline-.01; Spot every continuous proxy MDD<=corresponding baseline, Coin every MDD<.50. Base CAGR>=baseline+.01 OR base MDD<=baseline-.01 with CAGR>=baseline-.01. Base worst CNYday>=baseline-.005, historical ES99 loss<=baseline1.05 and longest underwater no longer than baseline. Both actual risk upper bands must pass. Return-increasing promotion additionally requires validation cumulativeUSDTreturn gain>0; risk-only alternative requires actual calibrated candidate continuousMDD reduction>=.01 versus actual unity baseline with validation gain>=-.01, while retaining every unscaled all-stress gate. Rank highest worst-stressCAGR then registered candidate order. Original targets separately stay NOT_MET unless exact goals pass. Retain each original producer year convention. Combine all individually eligible compatible mechanisms with no subset search, remeasure all four stresses and actual calibrated base, adopt only the combination's own gates pass.

## Fixed two-account capital

Actual fixed Spot/Coin CNY2500/7500,5000/5000,7500/2500 accounts for current defaults and final selected if different; no added cash, transfers or rebalance. Sum actual synchronized daily equity curves and closing BTC exposure. Daily joint MDD is not continuous joint MDD. 5000/5000 is the predeclared neutral reference; other splits are sensitivity evidence, not an optimized default.

## Verification and limits

Meaningful offline tests cover before/at availability, exact eight-hour expiry, settlement jitter, missing/current-date/stale basis, invalid/duplicate/nonfinite records, future-tail perturbation invariance, negative funding and identical standalone readers in both repos. Source identities and real coverage are emitted to external task-artifacts without modifying old evidence. All Coin tests/producers sharing UIDs remain strictly serial; HOME/locks are unchanged. Clean committed source/spec/input identities freeze before new financial measurements. Unit tests and public historical proxies do not prove financial success, prospective alpha, native execution or qualification.
