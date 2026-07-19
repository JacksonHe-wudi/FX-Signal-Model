"""
Layer 1 - Fair-value (cointegration) layer, Compass30-style, weekly.

For each Asian currency i:
    log(spot)_it = const + d1*f1_t + d2*f2_t + d3*f3_t + b'X_it + eps_it

- f1..f3: principal components of 28-currency log-spot panel (excl. CNH),
  re-estimated RECURSIVELY each week using only data available at that date.
- X_it: currency-specific observables (Citi terms-of-trade for all;
  log 5Y CDS where available; log equity index for KRW/CNH/MYR).
- eps_it: deviation from fair value ("misvaluation", + = USD too strong /
  local currency undervalued vs model).

Outputs
-------
analysis/eps_recursive.csv : out-of-sample misvaluation series per currency
analysis/layer1_diagnostics.csv : per-currency fit, ADF, error-correction test

Usage: python layer1_fair_value.py
"""
import os
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
OUT = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
PANEL_START = '2000-01-01'
MIN_WIN = 260                      # min 5 years of weekly data before first eps
STEP = 1                           # re-estimate every week

# data column mapping quirks: CTOT/ESI label China as CNY; India CDS = SBI proxy
CTOT_COL = {'CNH': 'CNY'}
CDS_COL = {'INR': 'INR_SBI'}
EQUITY_IN_X = ['KRW', 'CNH', 'MYR']          # per BofA Exhibit 16
CDS_IN_X = ['KRW', 'MYR', 'IDR', 'PHP', 'INR']


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def main():
    spot = load('L1_spot_usd_all30')
    ctot = load('L1_ctot')
    cds = load('L1_cds_5y')
    equity = load('L1_equity')

    logspot = np.log(spot)

    # ---------- PCA panel: 28 currencies excl CNH, balanced from PANEL_START
    pca_panel = logspot.drop(columns=['CNH']).loc[PANEL_START:].ffill(limit=2).dropna()
    dates = pca_panel.index

    # ---------- X variables per currency (levels, aligned to Friday grid)
    def x_block(ccy):
        xs = {}
        tc = CTOT_COL.get(ccy, ccy)
        if tc in ctot.columns:
            xs['ctot'] = ctot[tc]
        if ccy in CDS_IN_X:
            cc = CDS_COL.get(ccy, ccy)
            if cc in cds.columns:
                xs['log_cds'] = np.log(cds[cc])
        if ccy in EQUITY_IN_X and ccy in equity.columns:
            xs['log_equity'] = np.log(equity[ccy])
        return pd.DataFrame(xs)

    # ---------- recursive loop
    eps_rec = pd.DataFrame(index=dates, columns=ASIA9, dtype=float)
    for k in range(MIN_WIN, len(dates), STEP):
        t = dates[k]
        win = pca_panel.iloc[:k + 1]                       # data up to and incl t
        Z = (win - win.mean()) / win.std(ddof=1)
        U, S, _ = np.linalg.svd(Z.values, full_matrices=False)
        F = pd.DataFrame(U[:, :3] * S[:3], index=win.index, columns=['f1', 'f2', 'f3'])

        for ccy in ASIA9:
            y = logspot[ccy].reindex(win.index)
            X = x_block(ccy).reindex(win.index)
            df = pd.concat([y.rename('y'), F, X], axis=1).dropna()
            if len(df) < MIN_WIN:
                continue
            A = np.column_stack([np.ones(len(df)), df.drop(columns='y').values])
            beta, *_ = np.linalg.lstsq(A, df['y'].values, rcond=None)
            resid = df['y'].values - A @ beta
            if df.index[-1] == t:                          # eps at t, model est. thru t
                eps_rec.loc[t, ccy] = resid[-1]

    eps_rec = eps_rec.astype(float)

    # ---------- diagnostics
    from statsmodels.tsa.stattools import adfuller
    import statsmodels.api as sm

    ret_1w = logspot[ASIA9].diff()
    rows = []
    for ccy in ASIA9:
        e = eps_rec[ccy].dropna()
        if len(e) < 52:
            continue
        adf_p = adfuller(e, maxlag=8, autolag='AIC')[1]
        # error-correction: next-week return on current eps (Newey-West t)
        df = pd.concat([ret_1w[ccy].shift(-1).rename('dy'), e.rename('eps')], axis=1).dropna()
        m = sm.OLS(df['dy'], sm.add_constant(df['eps'])).fit(
            cov_type='HAC', cov_kwds={'maxlags': 4})
        theta, tstat = m.params['eps'], m.tvalues['eps']
        halflife = np.log(0.5) / np.log(1 + theta) if -1 < theta < 0 else np.nan
        rows.append([ccy, str(e.index[0].date()), len(e),
                     round(100 * e.iloc[-1], 2), round(adf_p, 3),
                     round(theta, 4), round(tstat, 2),
                     round(halflife, 1) if pd.notna(halflife) else np.nan,
                     round(m.rsquared, 4)])
    diag = pd.DataFrame(rows, columns=[
        'currency', 'eps_start', 'n_weeks', 'current_eps_pct',
        'adf_pvalue', 'theta_EC', 'theta_tstat_NW', 'halflife_weeks', 'r2'])

    # pooled panel error-correction
    pool = []
    for ccy in ASIA9:
        d = pd.concat([ret_1w[ccy].shift(-1).rename('dy'),
                       eps_rec[ccy].rename('eps')], axis=1).dropna()
        pool.append(d)
    pool = pd.concat(pool)
    mp = sm.OLS(pool['dy'], sm.add_constant(pool['eps'])).fit(
        cov_type='HAC', cov_kwds={'maxlags': 4})

    os.makedirs(OUT, exist_ok=True)
    eps_rec.index.name = 'date'
    eps_rec.to_csv(f'{OUT}/eps_recursive.csv')
    diag.to_csv(f'{OUT}/layer1_diagnostics.csv', index=False)

    print(diag.to_string(index=False))
    print(f"\npooled: theta={mp.params['eps']:.4f}  tNW={mp.tvalues['eps']:.2f}  "
          f"n={len(pool)}  R2={mp.rsquared:.4f}")
    print(f"\nsaved {OUT}/eps_recursive.csv, {OUT}/layer1_diagnostics.csv")


if __name__ == '__main__':
    main()
