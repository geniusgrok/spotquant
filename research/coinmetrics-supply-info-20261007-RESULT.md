# Coin Metrics USDT supply slope: conditional Spot information result

The [fixed protocol](coinmetrics-supply-info-20261007-SPEC.md) was committed as `0e9cb3b1cd2401dd2272f30a97a3f3ffa8e23894` and pushed before reading supply-slope signs or seven-day BTC outcomes. This is the **one** source-specific, non-commercial offline test of the original negative supply-slope hypothesis. The original CoinGecko source and its support failure remain separate and unchanged. No raw Coin Metrics series is in this branch.

## Support before information

The pinned original audited Spot manual ledger has 62 distinct accepted external BUY decisions after repeated polling and sleeves are collapsed. All 62 align to three consecutive Coin Metrics rows under the sole frozen `event + 48 hours + 10 minutes` **conditional historical availability assumption**, plus prior completed BTC momentum/RMS, original buyer fill and a mature seven-day BTC daily endpoint. Greedy seven-day non-overlap leaves **51** opportunities, split **25/26** chronologically, so the original `>=10` and `>=3` per half support gate passes. Original real historical Coin Metrics first receipts remain unavailable: `historical_first_receipt=null`, `pit=false`, strict-PIT eligible historical BUYs **0**. The lag is neither a measured publication delay nor a guarantee against historical revisions.

## One fixed information test

The expression was `ln(CapMrktEstUSD / PriceUSD)` three-day per-day slope, with negative slope predicting weaker seven-day BTC outcome. The response and source definition are attributed to [Coin Metrics](https://docs.coinmetrics.io/network-data/network-data-overview/market/market-capitalization#d); the Community data were used here only for the user's non-commercial research under the documented [CC BY-NC 4.0 terms](https://creativecommons.org/licenses/by-nc/4.0/) ([provider's free-tier page](https://github.com/coinmetrics/product-docs/blob/master/docs/access-our-data/api/README.md#free-tier-community-api)). The result below subtracts a fixed 0.37% round-trip fee/slippage proxy from a seven-day BTCUSDT **daily close price label**; it is not a filled-order or account return.

| Chronological sample | Events | Negative supply slope | Negative-group mean net label | Nonnegative-group mean net label | Controlled negative-slope coefficient |
| --- | ---: | ---: | ---: | ---: | ---: |
| All | 51 | 11 | +5.34% | +2.29% | **+3.61 percentage points** |
| First half | 25 | 4 | +10.95% | +3.64% | **+10.58 pp** |
| Second half | 26 | 7 | +2.13% | +0.79% | **+1.57 pp** |

The single OLS comparison controlled prior completed BTC 20-day momentum, BTC 20-day RMS and the same Coin Metrics three-day `PriceUSD` log slope. Every sample had both indicator classes and a full-rank fit. The nonnegative groups remained positive even after double declared costs, but the negative-slope group was also positive in both halves and **all three adjusted coefficients had the opposite sign** to the frozen weaker-BTC hypothesis. Thus the fixed information gate **fails**. The source-specific negative-slope half-budget expression is closed. Its opposite sign is not a registered candidate; no sign reversal, threshold/lag scan, second information test, account replay or new strategy is implied.

The 51 seven-day observations and two halves are historical development diagnostics drawn from previously studied manual opportunities, not prospective or strict point-in-time evidence. The daily close proxy does not establish the actual price achieved by a Binance MARKET order, ownership-specific later SELL cash flow, drawdown benefit, or the user's 100% CAGR / under-30% drawdown goal. No independent source information or clear net benefit survived the information gate, so no account variant, production/main change, deployment or trading was performed.

Reproduce the two bounded stages with `python -m research.coinmetrics_supply_info_20261007 support|information <original-account.json.gz> <search-artifacts.zip> <local-coinmetrics-raw.json> <local-coinmetrics-receipt.json>`. The script pins both original artifacts and the local Coin Metrics raw SHA via the existing source verifier. The local-only raw response was received 2026-10-07 15:28:41.330 UTC, SHA-256 `18d511062eb636677960843006325a7f448f176faca0fbcddb8a9459ae4f99da`; no new request was made for this information test.
