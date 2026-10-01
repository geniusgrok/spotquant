# Grouped rounded-close residual: diagnosis and proposed-fix review

Read-only review while the original clean2d1e5fe20-account matrix is still running. No source/runtime changes or full financial reruns were made. **Diagnosis confirmed. Selective reruns are credible with the proof conditions below; the proposed patch itself is not yet implemented or accepted.**

## Confirmed cause

Preview sizes a grouped full close as the sum of each sleeve's floored quantity. The durable SELL allocation then distributes the actual fill using proportional unrounded sleeve weights. The group's total rounding residual is less than group_size times BASE_STEP, but a disproportionately large sleeve can receive more than one step of that residual. Keeping that sleeve as an ordinary live position causes an unplaceable protection or a crossed-stop remainder, although the full rounded close filled and all coins remain explained.

I independently reconstructed per-sleeve quantities from every available raw fill and its immutable durable allocation. The proposed additional classification (including final FILLED, executedQty/orderQty equality, observed whole-order fills and exact sum-of-floors sizing) identifies exactly one event in each failed consensus scenario, on its first unresolved session:

| Scenario | Fill timestamp_ms / order | Sleeve40 residual BTC | Fill-price value USDT | Unresolved sessions |
|---|---|---:|---:|---:|
| base |1755676804800 /336|0.0000164048646786482917626508|1.8793035653|135|
| outage |1755676804800 /336|0.0000164048646786482917626508|1.8793035653|135|
| fee150 |1713434408200 /248|0.0000133269681878778519237237|0.8430116012|295|
| slip2 |1713434408200 /248|0.0000115815471768764000159413|0.7322366469|295|

Every first error is `desired protection fails venue filters`. The four default scenarios, two completed downside scenarios and all three completed fixed-budget accounts have **zero additional-branch matches** in their complete recorded fill paths. Later scenarios must be checked after their original runs finish; zero errors alone is not a sufficient proof.

## Required details in the planned correction

1. **The dust flag alone is insufficient for a remainder >=BASE_STEP.** Current `_view(dust)` returns the true quantity; portfolio floors that quantity and still treats1.64e-5BTC as a one-step live position. This would continue building the failing protection. Separate strategic/tradable quantity from the full proven-owned quantity explicitly. A controlled dust view can make strategy sizing flat while external-balance reconciliation continues to include every real coin. Returning owned_qty=0 would be wrong because it reclassifies the residual as external BTC.

2. Limit the new classification to a durably owned SELL whose intended quantity equals the sum of floored recorded group weights, whose confirmed terminal native status is FILLED and executedQty equals that exact intended quantity, and whose true positive residual is below BASE_STEP*group_size and below MIN_NOTIONAL at the relevant fill. Retain the existing sub-step behavior. Do not classify an intentional half reduction or terminal partial/EXPIRED remainder this way. Do not change money tolerances, ownership checks, anchor or balance adoption rules.

3. **Cumulative application matters.** Final native FILLED/executedQty metadata or a sum over every fill already cached is not enough when fills span days. The whole intended SELL quantity must have been applied through the current chronological trade before the additional branch can strategically close a sleeve. Otherwise replay can close the position on an earlier partial-fill day simply because a later final fill is now known. The historical venue currently emits one fill per order, but native/read-only recovery must remain correct.

4. Read-only sessions currently reconstruct their owner mapping independently of Lifecycle.owners. Apply the same trustworthy native-state/quantity metadata construction there so backup recovery and later read-only observations can identify an arriving grouped close consistently. Metadata must be derived from the validated durable native result, not supplied by strategy policy.

5. Newly classified residuals have a valid quantity step but fail minimum notional. They cannot remain unconditionally labeled unplaceable if later market value becomes placeable. Future actual runtime should conservatively block for reconciliation or explicitly handle that transition. This does not affect the registered historical window: its maximum high is126199.63USDT, so even three full steps are worth only3.7859889USDT, below5. The new group's strict less-than-three-step residual cannot become placeable in these frozen accounts.

## Selective-rerun proof standard

A full20+3rerun is **not automatically necessary**. Reuse is defensible only after the original matrix finishes and an independent per-account proof establishes that the complete new behavior predicate is never reached for every reused account. The final source diff must contain only this guarded classification, required metadata plumbing, and strategic-flat handling that is demonstrably equivalent for existing sub-step dust. If unrelated execution behavior changes or an account reaches the new branch, that account must be rerun.

Build the proof from retained raw fills, immutable allocation weights/orders and native results, replaying quantities chronologically with the exact Decimal/rounding convention. Record original artifact hash/source, new patch source/hash, predicate version, inspected fill/order counts and every hit. Check both terminal/readback and cumulative-applied-fill conditions. Existing baseline/three-budget results currently have zero hits; recheck against the actual implemented predicate. For each affected account rerun, retain the old failed result as excluded evidence and store the replacement's own clean source and new artifact hash.

The final collection must explicitly identify sources per account and link the no-effect proof for reused accounts. Preserve the original single-source2d1e5fe matrix intact, including its failed consensus gates. Do not label the replacement collection as a single-source run, and do not select/promote from any original failed consensus result. Mechanical eligibility still requires all four matched scenarios for each compared candidate to be complete and audited; the full registered delivery needs every required account resolved.

Targeted validation should cover unequal three-sleeve full-close rounding, genuine partial/EXPIRED remainder, intentional half reduction, multi-fill cross-day completion, restart/read-only ownership, fresh-cross reentry using retained residual, and unchanged external BTC/cash rejection. The implementation and its evidence proof remain subject to a further read-only review before acceptance.

# Independent reusable proof and counter-design follow-up

Created `/tmp/verify_spot_grouped_residual.py`, an independent standard-library-only verifier that does not import or mutate production code. Example:

```sh
python3 /tmp/verify_spot_grouped_residual.py \
  evidence/complete-delivery-20261001/spot-accounts-final.progress.json \
  evidence/complete-delivery-20261001/portfolio-spot-accounts-final.json \
  --out /tmp/spot-grouped-residual-proof-NEW.json
```

The script reconstructs sleeve quantities and per-order applied gross quantities chronologically, initializes the first applied counter only when all original weights match current owned quantities within1e-24, carries unknown legacy history asNone, and evaluates only the **additional** >=STEP dust predicate. It checks unique fill IDs, durable ownership, and final sleeve/venue quantities. Output records verifier version/hash; each input's exact path/hash/source; per-account fill/SELL/order/session counts; counter initialization failures; every extended-branch hit; and a provisional zero_hit_reuse_candidate flag. The flag still requires review against the eventual implementation and any other source changes.

Snapshot proof `/tmp/spot-grouped-residual-proof-14.json` covers14completed original scenarios plus3budgets. It confirms0counter-initialization failures across all17accounts. Only consensus4have one hit each. Default4, downside4, funding-base/fee150 and all3budgets have0hits and pass their original complete/audit/execution gates. Remaining funding/basis cases require a later proof snapshot when completed.

The updated counter design addresses prior concerns: persisted cumulative gross, consistent surviving group counters, legacy-null history, preservation through dust reentry, and no premature use of future native terminal status. A cloned strategy view carrying_owned_dust while returning the full quantity addresses the >=STEP sizing problem; future-placeable residuals conservativelyUnknown and exclusion from downside strategic held groups are appropriate. Actual implementation review is still required.

One non-predicate change needs separate no-effect evidence: charging **all** owned residual to the capital ceiling can also touch older sub-step dust paths. For the currently completed17accounts, I independently reconstructed maximum cash and maximum BTC after every fill, then used max_cash + max_BTC *126199.63 as a conservative bound on account value throughout the fixed price path. The largest bound is88397.57664595USDT, far below the fixed5000000USDT ceiling. Thus the stricter residual-capital accounting cannot constrain any of these old account decisions. Repeat the same bound for remaining completed scenarios in the final equivalence record.

No new design issue currently forces a full20+3rerun. That conclusion remains conditional on the narrow final patch, final23-account proof coverage, appropriate regression cases and honest per-account source labeling. Production/runtime source was not changed by this review.

# Isolated actual-patch review and one remaining P2

Reviewed the five implementation-file deltas under `/tmp/spot-grouped-fix` against the untouched runtime tree. The counter, legacy-null handling, shared read-only/executor metadata construction, cloned strategic-flat view, retained real ownership and future-placeable guard match the planned design.35affected tests pass. Independent old/new process comparison of16existing sub-step-dust fresh/bear/repair/wait, entry-enabled/disabled and capital-limit cases produced identical portfolio outputs.

The independent verifier is now versionisolated-grouped-dust-cumulative-v3. It uses the implementedabs(applied-intended)<=1e-24criterion, records canonical hashes for each account result and hashes the five reviewed implementation files. Latest proof is`/tmp/spot-grouped-residual-proof-actual-latest.json`, covering15completed scenarios plus3budgets; only consensus4hit the extended predicate. I also folded the same recorded fills through the isolated actual `_owned_fills` and independently confirmed identical predicate hit counts and unchanged final owned BTC. The newly completed funding-slip2is zero-hit. This is recorded-fill arithmetic validation, not a new financial/session replay.

**P2 — final native readback arriving after fill application leaves the residual permanently unclassified.** If a full rounded SELL fill is applied while the owner's durable result is stillNEW/PARTIALLY_FILLED (e.g. read-only observation, or a fill after the cycle's recover query), the new branch declines classification and commits the trade as accounted. When a later native query confirmsFILLED, there is no new fill to callgrouped_close_dustagain. The >=STEP residual remains an ordinary position and repeats the same unplaceable-protection failure.

Independent reproduction: use the grouped-close fixture with native statusNEW/executed0, then apply the complete0.00004BTCsell. Sleeve30has0.00001168656716417910447761194030BTC, sell_applied['17']='0.00004', accounted=[1], dust=False. Update the durable metadata toFILLED/executed0.00004and process the already-accounted fill again: closed=[]and dust remainsFalse.

Minimal safe approaches: either fail the fold before its commit withUnknownwhen the fully applied rounded-close residual shape is established but terminal confirmation is still pending (so the next recovered cycle replays it), or explicitly revisit eligible retained residuals when validated terminal readback arrives, using the persisted applied counter and trustworthy fill-price evidence. Preserve the prohibition on guessing legacy history or relaxing balance checks. Add a regression for this observation ordering. The interim implementation acceptance is therefore **REQUEST CHANGES for this one boundary**, not final acceptance; other checks and zero-hit evidence remain valid.

# Late-readback guard fix: isolated implementation accepted

**The remaining P2 is resolved; ACCEPT the current isolated implementation for subsequent clean-source integration and affected-account reruns.** The function first recognizes the fully applied rounded-close residual shape, then raisesUnknownif terminal status/executed quantity is missing or inconsistent. This prevents accounting the decisive fill before its required readback. Incomplete legacy cumulative history remains unclassified; genuine partial/EXPIRED and intentional half reductions do not satisfy the full-close shape.

Beyond the updated regression, I ran an independent actual read-only `cycle` against a private SQLite State. The largest leftover sleeve was deliberately the second sleeve so the first sleeve could undergo temporary processing before the guard fired. Persisted positions, model checkpoints, accounted IDs and cursor all remained identical afterUnknown. After changing only the durable native result toFILLED/executed full quantity, the next cycle replayed the same fill, returnedread_only, marked all three actual residuals as dust and preserved total0.000027BTC. This verifies the session's staged rollback, not just a helper return value.36affected tests pass.

The independent verifier is now `isolated-grouped-dust-cumulative-v4-readback-guard`; it records additional_shape_count, extended dust hits and pending_readback_guard_count separately. `/tmp/spot-grouped-residual-proof-readback-guard.json` binds the updated five-file implementation hashes and currently covers16completed original scenarios plus3budgets. Consensus4each have one additional shape with confirmed terminal metadata; every other covered scenario/budget has zero additional shapes. All guard counts and unknown-counter-initialization counts are zero. For reused zero-shape accounts, native readback arrival timing cannot activate either added branch. All funding4are now included; basis4still await completed original evidence.

This acceptance does not authorize mutation of the still-running original source and does not complete the required23-account no-effect proof. Preserve the untouched original20results, then bind the final clean fix revision and selective replacements explicitly. Affected consensus results still require new valid full-window executions; all original failed versions remain excluded from selection.

# Retained integrated rollback regression

At the parent's explicit request, added only a reviewer test to the isolated staging tree: `/tmp/spot-grouped-fix/tests/test_grouped_close.py::GroupedCloseTests.test_readonly_cycle_rolls_back_earlier_sleeve_before_late_terminal_guard`. No production implementation or live original-run file changed.

The test retains the actual private-SQLite/read-only-cycle scenario described above, using existing fixtures and Lifecycle.save rather than duplicating execution logic. A standalone runner is `/tmp/probe_grouped_readback_rollback.py [isolated_root]`; default root is/tmp/spot-grouped-fix. The standalone probe and all8grouped-close regressions pass. Final proof still awaits the four basis accounts; the latest16-scenario-plus3-budget proof remains valid for completed rows.


# Selective assembly and configurable-budget helper review (initial)

The independent proof `/tmp/spot-grouped-residual-proof-18.json` now covers18 completed original scenarios plus3 budgets. Newly completed basis-base and basis-fee150 both have zero additional shapes, pending-readback guards and counter-initialization failures. Only the four consensus scenarios require replacement so far. The last basis-slip2/outage still await completed raw evidence.

The isolated assembly helper preserves exact result rows, records each account’s measured source/bundle SHA/canonical row SHA, explicitly labels mixed sources and keeps native/production promotion false. It requires replacement keys to exactly match failed/nonzero-proof accounts and checks identical inputs, identities, capital and scheduled starts. Its two tests and all8 grouped-close regressions pass. The default budget runner retains the same independent2500/5000/7500 initial capitals and measure(default, base) call; optional ProcessPool uses separate processes and preserves ordered output. No monetary semantics change was found in the existing default path.

Two P2 evidence/consumer issues remain in this initial helper revision:

1. `assemble_spot.main` binds the proof’s five implementation hashes only to the assembly worktree. The replacement’s measured source is merely required to say dirty=False, so another clean measured source with a different runtime could supply the same candidate/input/gated rows and be accepted. Bind the replacement’s declared git tree and source digest to the reviewed implementation hashes as well; verifying only current assembly HEAD is insufficient.
2. `portfolio_spot --candidate` can emit nondefault budgets, but `complete_assessment.joint` hardcodes the10000 Spot endpoint to default-base and does not check budget candidates. Passing consensus budgets would silently mix different strategies across the purported fixed-allocation frontier. Require the frozen default candidate in this consumer, or explicitly select and consistently use one candidate for all budget sizes/endpoints.

These are helper acceptance blockers only; they do not invalidate the accepted isolated monetary patch or the zero-hit proof. No production source or evidence artifact was modified by this review.


# Corrected helper acceptance and selected allocation path

**ACCEPT the corrected isolated helpers; both reported P2 issues are resolved.** `verify_measured_source` now reads the replacement’s immutable full Git tree, reconstructs the same ordered tracked-Python source digest and compares both the recorded source digest and five reviewed implementation hashes. An independent actual-Git probe verified that clean2d passes its own authentic recorded digest/reference, but is rejected against the patched implementation reference. Tampered source digest, dirty flag and abbreviated revision are rejected. The result is explicitly retained in the assembly report.

Baseline budget consumption now requires explicit default/incumbent candidate identity, both at metadata verification and at the actual joint curve construction. The separately named selected allocation report consistently takes the selected candidate’s10000 account and its three actual budget accounts. Missing selected budgets, mismatched identities, capital, schedule or Spot input metadata are rejected; final complete/audit checks occur before constructing curves. The original baseline output stays separately labeled, while selected rows identify both candidates. The report preserves per-account Spot source attribution and hashes the optional selected-budget input.

Nine focused assembly/assessment tests pass. An additional independent two-day synthetic wallet probe exercised the real canonical/joint functions with deliberately different returns at each budget size: all five selected allocation curves equaled direct sums of their respective actual wallets, including the proper same-candidate10000 endpoints; no scaling from the10000 curve occurred. Replacing one selected2500 row with a baseline candidate was rejected. No additional material issue was found in this targeted helper delta.

The two newly covered basis accounts also have conservative capital upper bounds75605.9727273861USDT(base) and74135.9600727322USDT(fee150), well below the5000000USDT ceiling, so the separate residual-capital guard remains inactive. Final23-account proof, clean-source integration binding, valid selective replacement executions and final selection/evidence review are still required. This acceptance does not promote any strategy or certify the pending raw accounts.


# Final committed integration and full23-account no-effect proof

**ACCEPT source integration `1fca802ec89445e1b89dcc3d600dbfed721b8e34` and reuse of the19 zero-effect accounts; only the four original consensus accounts require replacement.** This verdict covers the reviewed patch and evidence equivalence, not the still-running corrected consensus outcomes or strategy promotion.

Independently compared all11 integrated source/test files against the accepted isolated tree: Git blobs, current worktree and reviewed staging bytes are identical. Reconstructed the full tracked Python source digest directly from the immutable commit: `d863ef763b84192e23aea53b5ca01fcdbf05ffaaf3d30233856c842c154540ee`. The assembly helper’s immutable source verifier accepts that commit against the five implementation hashes retained in the proof.17 focused grouped-close/assembly/assessment tests pass in the real repository.

The original full20 raw Git blob and worktree are byte-identical, SHA256 `65cc08a7341eb2ef7d7b30641b783341edd7dc7781e8dfa10ae801b5b8c86022`. The original3-budget blob remains unchanged, SHA256 `8e15ca5fd83c8c89c3c65545f81f079186964efce3abd6566554fa5cce5c79d6`. Both retain clean measured source2d1e5fe. The committed independent verifier is exactly the reviewer-authored script (SHA256 `078c09d10041c7e0b9facc6997d11601a41a1d45418a7aafb982eefac8fe93ca`). Re-executing it against final raw bytes and committed implementation produced `/tmp/spot-grouped-independent-final23-proof.json`, which is structurally identical to the committed proof `grouped-residual-zero-effect-proof.json` (committed proof SHA256 `8ec61b66e008be4283903bf1c4207c1466d6ef7bee33bc371fd79bfae084f8a0`).

Coverage:23accounts,3085fills,1616SELL fills,8696durable native owners and18255actual sessions. Each normal scenario/budget has795sessions; each outage scenario has789 because the registered outage skips six starts. Every retained actual session has an archive verification. Consensus4 each has exactly one added residual shape; every other account has zero. Pending-readback guards and counter-initialization failures are zero across all23. The19 reusable accounts have passing complete/audit/execution gates. Original consensus unresolved counts remain135(base/outage) and295(fee150/slip2), with complete=False; those failed rows are preserved and excluded.

Rechecked the separate capital-ceiling non-effect bound on all19 reusable accounts: maximum of max_cash + max_BTC*126199.63 is75607.1137318874USDT, below the5000000USDT ceiling. Newly completed basis-slip2 bound74114.8610385752USDT and basis-outage75605.9727273861USDT also pass. The already-reviewed strategic sub-step clone behavior and metadata-only counter changes plus zero-shape proof therefore support reuse without a full20+3financial rerun.

Next acceptance boundary: verify all four corrected consensus rows, their clean1fca source and frozen inputs, matching795/789scheduled sessions, financial/ownership/archive/execution gates, then assemble an explicitly mixed-source collection and independently review the registered mechanical selection. Keep the original failed matrix byte-for-byte intact. No financial replay or source mutation was performed by this review.


# Corrected consensus evidence and registered selection acceptance

**ACCEPT the four corrected complete consensus accounts and the explicitly mixed-source20-account collection.** Replacement SHA256 is `cf075b86ba5df590e078a7c626c53cb20937be955cfb6ad8d41eae19b78645e8`; assembled SHA256 is `439edad38e7a2af382597d204f35cd0a6372ca9da98cdcf4c964852329568e92`. Immutable measured source1fca802 and its complete Python digest/five reviewed implementation hashes validate. Independently checked actual market/FX/schedule/crowding bytes against the recorded hashes. Rebuilding the assembly from original, replacement and proof exactly reproduces the assembled object and each source ledger entry. The unchanged original failed matrix retains SHA65cc08a7341eb2ef7d7b30641b783341edd7dc7781e8dfa10ae801b5b8c86022.

Independent retained verifier `/tmp/review_spot_corrected_accounts.py` and result `/tmp/spot-corrected-independent-review.json` reconstruct initial cash, signed cash/BTC changes, fees, native proxy quantities, final ownership, CNY conversion and CAGR for every replacement. All four have141fills; base/fee150/slip2have795sessions, outage789. All3174actual sessions have measured archive verification/hash evidence and no pending/unresolved execution. All2632POST/DELETE dispatches lie before their session deadlines and their measured returns are1000ms later. The historical runner actually checked each temporary session backup at measurement time; this report does not claim all historical SQLite files remain retained for a new restore check. The separately retained synthetic recovery specimen remains the independently reopenable restore artifact.

Each account reconstructs2454canonical daily marks and its terminal equity. A second independent marker calculation (`/tmp/review_spot_proxy_drawdown.py`, output `/tmp/spot-corrected-proxy-mdd-review.json`) reconstructs drawdown using7502daily-proxy knot/fill markers per account, including stop execution marks and prior-date FX. Every result agrees with reported continuous proxy MDD to less than1e-20. This validates the explicit OHLC proxy, not observed intraday/native matching behavior. The independent chronological sleeve verifier also passes each final ownership: additional dust shapes1(base),2(fee150),3(slip2),1(outage), no pending-readback guards or counter initialization failures. Fills through the original first defect remain exactly identical:115base/outage and82fee150/slip2, establishing unchanged preceding financial behavior.

Independently evaluated the frozen all-four-scenario dominance/improvement rule: **consensus is the sole eligible candidate**. Base CAGR51.8502779716%, continuous proxy MDD41.2073348338%, final CNY165529.103382. Worst consensus CAGR50.8761072440%; fee150MDD41.4221252528% and slip2MDD41.4513489723%. The registered comparison chooses consensus, but the original100%CAGR/30%MDD economic targets remain **NOT_MET**, and native qualification remains **NOT_QUALIFIED**. No promotion, native authorization or future-return inference follows from this retrospective choice. The selected2500/5000/7500accounts remain a separate pending evidence boundary and must not replace or scale the retained original baseline budgets.

# Proposed measured-consensus default integration

**ACCEPT the reviewed isolated integration for application after the active budget processes finish.** The selected allocation helper is extracted from the measured Policy without monetary changes. Production portfolio calls it by default; the historical Policy explicitly requests consensus=False for its original baseline and separately invokes the same helper only for the consensus candidate. Existing fresh-cross/entry/read-only/open-order/ownership gates run first. The helper changes only an already-present BUY for its existing entering sleeves when at least two real bullish enter/hold sleeves vote; it does not create entries, trade existing holders, or rebalance.

Initial review identified a contradictory per-sleeve quote: the aggregate requested90USDT while one sleeve still displayed50USDT. The revised isolated helper resolves this with explicitly labeled advisory shares of the actual pooled BUY, distributing the final arithmetic remainder to the last sleeve. Actual combined orders, equal durable ownership weights and protections are unchanged.

Independent old-measured-Policy versus new-default comparison covered2592combinations of entry/bear/repair models, owned quantities, entry enablement, open-order gates and capital limits. Executable orders, decisions and protections are identical; only the intended576per-sleeve advisory values differ. All384affected allocation cases sum exactly to the actual combined quote. Fifty focused preview/session/account/execution/grouped-close tests pass. Neither native qualification nor CLI authorization code is changed. This is a technical equivalence acceptance; operator documentation must still distinguish the selected default from the frozen research P4 baseline and retain both economic/native qualification limits.

Reviewed final isolated file SHA256 values: preview.py `a1e0226b331c6d0fcf77af50ce7c097a010439e1db1949e831e3e2b65cc97996`; complete_spot.py `a4864b43388c3e86426a9b455bb65aabdf008ffd16aebaab9184d93639c0eeb6`; test_preview.py `a1b733a3c370cf879d5bd0ac932b86567d22b8a5ce993b98e27ccfff590466a0`. Comparison probe: `/tmp/probe_consensus_default_equivalence.py`. No financial rerun or production modification was performed by the reviewer.


# Selected consensus actual-budget evidence and final default commit

**ACCEPT all three selected consensus budget accounts.** Artifact `portfolio-spot-consensus.json` SHA256 is `b7fe8020df2c58e2910c57ef856a61b5905702a69bca30874cddc9c578755b0d`. Its clean immutable measured revision7b4b44e2ea7ba764ccf84e4bf6601b75fa6e646d reconstructs Python SHA d863ef763b84192e23aea53b5ca01fcdbf05ffaaf3d30233856c842c154540ee and contains the reviewed execution implementation. The market, FX, schedule and crowding hashes match both actual input files and the selected consensus10000endpoint.

Each2500/5000/7500account contains795actual sessions,141fills,658bounded client writes and2454daily marks. All complete/audit/execution/archive gates pass;2385backup hashes are unique across these sessions/accounts. Independent signed fill reconstruction verifies cash, BTC, fees, final per-sleeve ownership, FX conversions, final CNY and CAGR. Chronological ownership verification observes respectively1/2/0additional residual shapes and zero pending-readback/counter-initialization failures. Independently recomputed continuous proxy MDD from7502price/fill markers per account agrees within1e-20.

Final CNY values are41370.8936341836,82759.6310176576 and124139.7657994600. These differ from scaled consensus10000results by-11.3822113195,-4.9206733485 and-7.0617370491CNY, and the first actual rounded quote orders also differ from linear scaling. Retained fills/allocations/session archives and the measured independent-budget source support the actual-account claim. These selected rows are suitable for the separately labeled selected joint curve with the same consensus10000endpoint; the original default budgets remain separate.

Reviewer artifacts: `/tmp/review_spot_selected_budgets.py`, `/tmp/spot-selected-budgets-independent-review.json`, `/tmp/spot-selected-budgets-ownership.json`, and `/tmp/spot-selected-budgets-mdd-review.json`.

Commit13deeb46350d0ac0311b6820c606645cff0974ff integrates the three previously accepted default files byte-for-byte. The only additional assessment behavior counts days where absolute closing BTC quantity times closing USDT price is at least5, while retaining the old nonzero-balance count and explicitly disclosing dust. This is descriptive and does not alter monetary calculations or selection. An initially incomplete synthetic test fixture was corrected with its missing price field;24focused assessment/preview tests pass. Documentation clearly separates the selected finite-session proxy from the old equal-allocation daily meter, and preserves NOT_MET/NOT_QUALIFIED. One minor command typo was reported: AGENTS uses unsupported plural--candidates; the actual CLI argument is--candidate with required fresh--out. Final cross-product joint assessment still awaits the Coin budget evidence.
