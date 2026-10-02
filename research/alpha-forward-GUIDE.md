# Registered assessment and public forward diary

These tools read public research artifacts. They do not contact an exchange, request credentials, simulate native fills, change defaults, or place orders. Native cases and actual account-days remain zero. The historical study is contaminated by prior research; neither selection nor a regression intercept proves prospective alpha.

Run from a clean committed Spotquant checkout with the mirrored Coinquant and Starquant siblings. The exact registered specification and protocol are checked. Keep the original raw artifacts immutable. JSON and lossless `.json.gz` are accepted; SHA256 binds the actual compressed bytes when compressed.

## Assessment and calibration

```sh
python -m research.alpha_assessment \
  --spot /path/spot48part.json.gz --perp /path/perp48part.json.gz \
  --out /path/provisional.json --calibration-out /path/calibration.json \
  --csv /path/provisional.csv --markdown /path/provisional.md
```

Spot must contain exactly seven candidates × four stresses; Coin must contain exactly five × four. Missing/extra accounts fail. Explicit `complete:false` cases remain rejected accounts, with no valid-performance or calibration claim. Spot outage uses the producer's exact 789 starts; other original accounts use 795. Every Spot archive proof is checked. Original timing is preserved: reads dispatched before the deadline may finish within their fixed 200ms latency; writes require actual trace evidence of dispatch before deadline and receipt exactly 1000ms later. Elapsed time must exactly match the recorded millisecond clock; no epsilon is used. Source digests are independently reconstructed from recorded Git trees. Spot full-schedule file hashes and Coin primary-list hashes are checked in their respective native formats. Source history must remain available locally.

The default public daily archive is `/tmp/spotquant-market/klines`; `--market`, `--fx`, and `--schedule` can override paths, never their bound input identities. Existing canonical, attribution, regression, daily metrics and passive-control helpers are reused. The Spot archive and FX bytes are rehashed locally. Coin retains its original market identity and consumed minute/print file hashes; matching accounts require equal market identity and equal hashes for every overlapping consumed file. The assessor does not download or recreate Coin archives.

The generated calibration includes all ten new candidates and both scale-one baseline controls. Each profile binds candidate/baseline, the exact original bundle bytes, and the 2022-01-01 cutoff. Only actual 2020–2021 USDT returns enter calibration. If a base account failed, its profile is absent; its registered risk obligation remains explicitly `not_applicable_due_to_invalid_unscaled_base` with rejection reasons. The report always inventories all ten directions. An invalid contemporaneous baseline blocks calibration of otherwise valid candidates; those obligations remain pending until that baseline is corrected. The invalid baseline itself has no invented unity-control profile. These statuses do not claim valid performance and keep `all_measured_accounts_valid:false` and `rules_freeze_ready:false`. Missing legal profiles produce a controlled error; do not invent one. Rerun the real producers with the fixed calibration:

```sh
python -m research.alpha_spot --scenario base --risk-calibration /path/calibration.json --out /path/risk-spot.json
# From the Coinquant sibling:
python -m research.alpha_perp --scenario base --risk-calibration /path/calibration.json --out /path/risk-perp.json.gz
```

These commands document later authorized measurement, not a daemon. Both valid baseline replicas are required unity controls and must exactly match the unscaled baselines' full financial, fill, daily, ownership, session/action/client/clock and bounded-path evidence. Every valid unscaled candidate requires an actual new risk account; derived/scaled curves are never accepted as substitutes. Validation volatility and beta are recomputed from actual rerun daily accounts. A failed/unidentifiable risk match cannot receive a matched-alpha label.

Repeat assessment with `--calibration`, `--risk-spot`, and `--risk-perp`. Supply approved original baselines via `--baseline-spot` and `--baseline-perp`. The approved Spot original is `evidence/complete-delivery-20261001/spot-consensus-corrected.json`, not the older failed consensus stage. The Coin original is its sibling's `evidence/complete-delivery-20261001/perp-exclusive-accounts.json`. The gate pins the exact approved raw SHA256 values: Spot `cf075b86ba5df590e078a7c626c53cb20937be955cfb6ad8d41eae19b78645e8`; Coin `15dd7bfc242d52bf692663179cd1e3867418f8b55a4cad0894d253a8b296f00a`. Supply those original bytes, not recompressed/substituted copies. It retains measured/derivation identities, verifies committed source and ancestry, checks the exact Coin original input SHA and incumbent row bindings, and compares six separate evidence groups for every stress: financial (including Spot audit fees), fills, daily, ownership, operating and remaining original fields. Synthetic client-ID spellings normalize relationally; numeric order/trade IDs, sizes/prices/cash, status/actions/cleanup/bounded-path evidence and every timestamp stay exact. Nondeterministic Spot archive hashes normalize only after each archive proof and hash shape passes. Explicit producer research identity/checkpoint/diagnostic additions are separately validated and excluded from account-operation equality. An original mismatch blocks adoption and rule freeze.

Selection uses unscaled paired same-stress accounts. All eligible compatible components are proposed. Core modes are exclusive, with higher worst-stress CAGR winning and ties choosing slow core. Zero components means not applicable; one uses its existing account. Two or more require exactly the proposed combination's actual four stresses via `--combo-spot` / `--combo-perp`. A rejected combination preserves the best eligible singleton. No subsets are searched. Risk evidence remains a separate descriptive classification.

Supply each predeclared Coin sensitivity replay via repeated `--sensitivity-perp FILE`: incumbent and final selected account at 9900/10100 CNY and original 10000 CNY with −/+60000 ms starts. If incumbent remains selected, identical identity tuples are counted once. No budget or schedule is optimized. `--final` rejects pending registered work; explicitly rejected/inapplicable obligations may close work while retaining invalid performance and blocking rule freeze. Completion never implies validation. All output files use exclusive creation.

For independently measured case files, supply an explicit one-level manifest in place of a bundle:

```json
{"format":"alpha-account-manifest-v1","kind":"spot","files":[{"path":"case.json.gz","sha256":"EXACT_64_HEX_RAW_FILE_SHA256"}]}
```

Paths resolve relative to the manifest. Every file's original raw hash/source is retained in `account_source_manifests`; overlapping accounts, nested manifests, source changes and input mismatches fail. The manifest's raw SHA binds the calibration input; each contributing original account SHA remains in the report. Never assemble a file by relabeling its measured source.

The JSON report contains all-candidate financial metrics, separate full/2022+ returns, daily versus continuous MDD, costs/funding, descriptive HAC7 regression, ES, calendar log-return concentration, passive cash controls, causal event/reason/exit counts, actual desired/accepted/fill size summaries (counts/min/max/sums with semantic opportunity/reason links) and Coin trigger/decision-price gaps, post-event five/twenty-day close diagnostics, rejection explanations, source manifests, risk controls and sensitivity inventory. Year 2026 is partial through September19. Warm-up opportunities are counted separately and excluded from post-event in-window summaries. Opportunity creation does not establish actionability; actual recorded gate reasons remain explicit. Future endpoints unavailable within the window are counted, not invented. CSV and Markdown are standalone concise exports; full diagnosis remains in JSON. No plotting dependency is required.

Memory is bounded by one parsed input bundle plus compact summaries/base curves. Rows release bulky journals, fills and allocations immediately after summarization; bundles are consumed sequentially. For especially large outputs, use one-account files with a manifest. No generic streaming framework, new dependency or strategy grid is added.

## Initialize and append observed public bars

Only a completed, valid report with original baseline comparisons, risk reruns, required combinations and sensitivities can freeze a diary:

```sh
python -m research.alpha_assessment --forward-init /path/spot-forward.json \
  --analysis /path/final.json --kind spot
```

Initialization starts at actual current UTC with CNY10000 cash, BTC0, no observations, no simulated account-days. It binds the analysis raw SHA, immutable measured source, exact spec/protocol, selected rule and original baseline raw SHA. Current execution head is recorded separately. Documentation commits or ordinary merges are accepted only when current tracked Python bytes, spec and protocol prove source equivalence. Frozen measured identities never change.

Manually obtain a completed public bar, preserve the downloaded payload's SHA256, and create a receipt record:

```json
{"symbol":"BTCUSDT","interval_ms":86400000,"open_ms":0,"close_ms":86400000,"open":"100","high":"110","low":"90","close":"105","observed_ms":86401000,"public_source":"https://data.binance.vision/REAL_PUBLIC_RESOURCE","public_payload_sha256":"EXACT_64_HEX_RAW_PAYLOAD_SHA256"}
```

The numbers above illustrate the schema, not an appendable historical bar. Use actual UTC millisecond times. `close_ms` is the exclusive UTC boundary: normalize Binance's inclusive `closeTime` by adding one millisecond. Spot uses daily bars; Coin uses completed four-hour bars (`interval_ms:14400000`). The bar must be the latest fully completed interval at both observation and append receipt, close after initialization, and strictly newer than the last observation. Future times, historical gaps/backfill, repeated bars and identity changes fail. Missed intervals remain recorded gaps.

```sh
python -m research.alpha_assessment --forward-append /path/spot-forward.json \
  --analysis /path/final.json --kind spot --bar /path/observed-bar.json
```

Append validates the existing integrity digest, uses an advisory file lock and atomically replaces the diary after fsync. Preserve its `.lock` sidecar. It records caller-provided public provenance; it does not independently attest a remote payload or turn observed bars into fills. Cash and native/account-day counters remain unchanged. There is no auto-import of study history or background process.
