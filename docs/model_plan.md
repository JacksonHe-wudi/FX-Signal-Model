# Weekly FX Prediction Model — Build Plan

Target: predict **next-week (Friday-to-Friday) returns** of 9 Asian currencies vs USD,
traded via weekly 1W-forward roll. All signals from `data/clean/clean_csv`.

## 0. What Layer 1 already tells us

Recursive fair-value layer (`layer1_fair_value.py`, output `analysis/eps_recursive.csv`):

- Cointegration mostly holds: ADF rejects unit root in the misvaluation series
  for 7 of 9 currencies (MYR, SGD marginal).
- **But 1-week error-correction is statistically zero** (pooled theta ~ 0,
  half-lives 1–3 years). This matches Engel–Mark–West: factor-deviation value
  has no weekly alpha on its own.
- Conclusion: **value (eps) enters the weekly model as a slow tilt / stretch
  filter, not as the engine.** The weekly engine must come from carry, trend
  and sentiment.

## 1. Candidate signals (grouped in buckets)

| Bucket | Signal | Source table | Prior sign (for local ccy) |
|---|---|---|---|
| Value | − eps (recursive misvaluation) | `eps_recursive` | cheap → appreciate (slow) |
| Value | REER deviation from 5y mean | `L1_reer` | low REER → appreciate (slow) |
| Carry | carry / realized vol | `L2_carry_to_realvol` | high → appreciate |
| Carry | implied yield slope 12M−1M (winsorized) | `L2_implied_yield_slope_12m_1m` | steep → tightening → appreciate |
| Trend | 12w spot momentum ex last week | `L2_mom_12w_ex1w` | continuation |
| Trend | 12w equity momentum | `L2_equity_mom_12w` | equity up → ccy up |
| Technical | 26w realized skewness | `L2_skew_26w` | extreme negative → rebound |
| Sentiment | real-money flow z-score | `L1_pi_rm_flow_z` | sign decided by IC test |
| Sentiment | risk-reversal z (level & 4w change) | `L2_rr25_1m_z_52w` | RR up (USD calls bid) → depreciate |
| Sentiment | ESI 4w change | `L2_esi_chg_4w` | up → appreciate (user prior: positive) |
| Risk | CDS 4w change | `L2_cds_chg_4w` | widening → depreciate |

## 2. Step 1 — IC audit (do this before any combination)

For every signal: weekly **cross-sectional Spearman IC** vs next-week return,
2015–2026, plus time-series IC per currency.
Report mean IC, Newey–West t-stat, IC in three regime states (USD trend,
risk on/off, PMI). Keep a signal only if |t| > ~1.5 or a strong economic prior
says keep it (then cap its weight). This empirically fixes ambiguous signs
(notably positioning: contrarian vs momentum).

## 3. Step 2 — Composite score

1. Winsorize each signal at ±3 cross-sectional z.
2. Average within bucket → bucket score (value / carry / trend / sentiment).
3. Equal-weight bucket scores → composite. (Regime-dependent weights are v2,
   only after the equal-weight version is profitable.)

## 4. Step 3 — Portfolio construction

- Rank composite each Friday; long top 3 / short bottom 3 (equal count),
  weights proportional to 1 / ATM implied vol, scaled to 5% annualized target.
- Execute via 1W forwards; weekly P&L = spot return + 1W forward carry − cost.
- Costs: static per-pair spread table on 1W points (to be provided by desk).
- Value overlay: eps acts as (a) a tilt added with small weight and
  (b) a stretch filter — do not add shorts in a currency already > 2 sigma cheap.

## 5. Step 4 — Alternative engine (point forecast, run in parallel)

Panel ridge regression: next-week return on all signals (pooled across
currencies, expanding window, weekly re-fit). Produces an explicit E[return]
per currency; trade only when |E[return]| exceeds the cost hurdle.
Compare against the rank engine; keep the better one, or blend.

## 6. Validation protocol (fixed before running)

- Walk-forward only; every input recursive (PCA, eps, z-scores).
- Subsamples that must each be examined: 2015–16 CNY deval, 2018 EM selloff,
  2020 COVID, 2022 Fed hikes, 2024–26.
- Metrics: net Sharpe, max drawdown, hit rate, turnover, per-bucket
  attribution, regime table (MS Exhibit 9 style).
- Benchmark: carry-only strategy. Acceptance = beat carry-only net of costs
  and positive in a majority of subsamples.

## 7. Build order

1. `layer1_fair_value.py` — done (eps + diagnostics).
2. `layer2_ic_audit.py` — IC table for all signals.
3. `layer3_portfolio.py` — composite, portfolio, cost model, backtest.
4. Regime analysis + report.
