"""
PCA driver analysis + strategy meta-selector backtest. Feeds the dashboard.

PART 1  What drives each currency?
  PCA on standardized weekly total returns of the 13 tradable currencies
  (full sample and trailing 104w). The PCs are interpreted by correlating
  their scores with OBSERVABLES - EM basket return (dollar/EM beta), DXY,
  EM vol changes, a carry-ranked long-short, and region baskets - so the
  dashboard can say "CNH this year: 38% dollar factor, 11% carry factor,
  51% idiosyncratic" instead of "PC1 0.38".

PART 2  Strategy library + "pick the best trailing-12m Sharpe" meta-selector
  The user asked for automatic selection of whatever strategy has the best
  Sharpe over the last 12 months. That is performance chasing, so it is
  BACKTESTED like everything else: each month-end, rank the library by
  trailing 52w Sharpe, hold the winner (and, as variants, the top-2 blend and
  the ALL-equal-weight blend) for the next month. Compared against just
  holding the live composite. No selection result is asserted - the numbers
  decide.

Outputs (for dashboard):
  analysis/pca_loadings_full.csv     per-ccy loadings on PC1-3, full sample
  analysis/pca_loadings_roll.csv     same, trailing 104w
  analysis/pca_interp.csv            PC x observable correlation matrix
  analysis/pca_var_decomp.csv        per-ccy variance share of PC1-3 + idio
  analysis/strategy_library.csv      weekly returns of all library strategies
  analysis/meta_selector.csv         meta-selector picks and returns
"""
import numpy as np
import pandas as pd

from fx_model import (TRADED, VOL_TARGET, load_clean, zsec, build_book,
                      vol_target, legs, composite)
from test_external_factors import LX, to_friday

D = 'data/clean/clean_csv'


def L(n):
    return pd.read_csv(f'{D}/{n}.csv', index_col=0, parse_dates=True)


def pca(X):
    """X: weeks x ccys, standardized. Returns loadings (ccy x pc), scores,
    variance shares."""
    X = X.dropna()
    Xc = X - X.mean()
    U, S, Vt = np.linalg.svd(Xc.values, full_matrices=False)
    var = S ** 2 / (S ** 2).sum()
    load = pd.DataFrame(Vt.T[:, :3], index=X.columns,
                        columns=['PC1', 'PC2', 'PC3'])
    scores = pd.DataFrame(U[:, :3] * S[:3], index=X.index,
                          columns=['PC1', 'PC2', 'PC3'])
    return load, scores, var[:3]


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    tot = (sr[TRADED] + cr[TRADED])
    std = tot.div(tot.rolling(52, min_periods=20).std().shift(1)).dropna()

    # ---------------- PART 1: PCA ----------------
    load_f, scores_f, var_f = pca(std)
    # sign convention: PC1 positive = EM up
    em = tot.mean(axis=1)
    for pc in ['PC1', 'PC2', 'PC3']:
        if scores_f[pc].corr(em.reindex(scores_f.index)) < 0:
            scores_f[pc] *= -1
            load_f[pc] *= -1

    reg = L('L1_regime')
    carry = T['carry'][TRADED]
    crk = carry.rank(axis=1)
    hi = crk.ge(10)                       # top-4 carry
    lo = crk.le(4)
    carry_ls = (tot.where(hi.shift(1)).mean(axis=1)
                - tot.where(lo.shift(1)).mean(axis=1))
    APAC = ['CNH', 'IDR', 'INR', 'KRW', 'PHP', 'SGD', 'THB', 'TWD']
    LAT = ['BRL', 'MXN', 'CLP']
    obs = pd.DataFrame({
        'EM basket': em,
        'DXY': -np.log(reg['DXY']).diff() if 'DXY' in reg else np.nan,
        'EM vol chg': -reg['EMFXVOL'].diff() if 'EMFXVOL' in reg else np.nan,
        'carry L/S': carry_ls,
        'LATAM-APAC': tot[LAT].mean(axis=1) - tot[APAC].mean(axis=1),
    })
    interp = pd.DataFrame({pc: obs.reindex(scores_f.index).corrwith(scores_f[pc])
                           for pc in ['PC1', 'PC2', 'PC3']})
    print('=' * 78)
    print(f'PCA on weekly standardized returns, {len(scores_f)} weeks, 13 ccys')
    print(f'variance shares: PC1 {var_f[0]:.0%}  PC2 {var_f[1]:.0%}  '
          f'PC3 {var_f[2]:.0%}   (top-3 = {var_f.sum():.0%})')
    print('=' * 78)
    print('\nPC interpretation (corr of PC scores with observables):')
    print(interp.round(2).to_string())

    # per-ccy variance decomposition, full sample and trailing 104w
    def var_decomp(X, load, scores):
        out = {}
        for c in X.columns:
            y = X[c].reindex(scores.index)
            tot_v = y.var()
            expl = []
            for pc in ['PC1', 'PC2', 'PC3']:
                b = y.cov(scores[pc]) / scores[pc].var()
                expl.append((b ** 2) * scores[pc].var() / tot_v)
            out[c] = expl + [1 - sum(expl)]
        return pd.DataFrame(out, index=['PC1', 'PC2', 'PC3', 'idio']).T

    vd_f = var_decomp(std, load_f, scores_f)
    std_r = std.iloc[-104:]
    load_r, scores_r, var_r = pca(std_r)
    for pc in ['PC1', 'PC2', 'PC3']:
        if scores_r[pc].corr(em.reindex(scores_r.index)) < 0:
            scores_r[pc] *= -1
            load_r[pc] *= -1
    vd_r = var_decomp(std_r, load_r, scores_r)

    print('\nper-currency variance decomposition (trailing 104w):')
    print((100 * vd_r).round(0).astype(int).to_string())

    load_f.to_csv('analysis/pca_loadings_full.csv')
    load_r.to_csv('analysis/pca_loadings_roll.csv')
    interp.to_csv('analysis/pca_interp.csv')
    pd.concat({'full': vd_f, 'roll104': vd_r}, axis=1) \
        .to_csv('analysis/pca_var_decomp.csv')

    # ---------------- PART 2: strategy library + meta-selector -------------
    atm = T['atm'].reindex(columns=TRADED)

    def book(score):
        comp = zsec(zsec(score).rolling(3).mean())
        W = build_book(comp, atm)
        Wl = W.shift(1).reindex(sr.index)[TRADED]
        r = (Wl * tot).sum(axis=1)
        r = r[Wl.abs().sum(axis=1) > 0]
        return (r * vol_target(r)).dropna()

    cv = carry / T['rvol'][TRADED].clip(lower=1.0)
    esi = (T['esi'] - T['esi'].shift(4)).reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    lib = {}
    lib['LIVE composite'] = book((2 * zc.fillna(0) + ze.fillna(0)) /
                                 (2 * zc.notna() + ze.notna())
                                 .replace(0, np.nan))
    lib['carry/vol only'] = book(cv)
    lib['ESI only'] = book(esi)
    lib['TSMOM 12w'] = book(tot.rolling(12).sum())
    px = -np.log(T['spot'][TRADED])
    lib['MA cross'] = book(px.rolling(10).mean() - px.rolling(40).mean())
    try:
        mis = pd.read_csv('analysis/stfv_misalignment.csv', index_col=0,
                          parse_dates=True)
        lib['STFV fade'] = book(-mis.reindex(columns=TRADED).rolling(2).mean())
    except FileNotFoundError:
        pass
    try:
        raw = pd.read_csv('analysis/ml_predictions.csv', header=[0, 1],
                          index_col=0, parse_dates=True)
        lib['ML ridge'] = book(raw['Ridge (a=10)'].reindex(columns=TRADED))
    except FileNotFoundError:
        pass

    R = pd.DataFrame(lib)
    R.to_csv('analysis/strategy_library.csv')

    def sh(r):
        r = r.dropna()
        return r.mean() / r.std() * np.sqrt(52) if len(r) > 20 else np.nan

    print('\n' + '=' * 78)
    print('strategy library (same book machinery, 5% vol, mid)')
    print('=' * 78)
    t12 = R.iloc[-52:]
    print(f"{'strategy':<20}{'full Sharpe':>12}{'last 12m':>10}")
    for c in R.columns:
        print(f'{c:<20}{sh(R[c]):>+12.2f}{sh(t12[c]):>+10.2f}')

    # meta-selector: month-end rebalance, pick by trailing 52w Sharpe
    Rn = R.div(R.std() * np.sqrt(52)) * 0.05          # equal-vol the library
    months = Rn.resample('ME').last().index
    picks, rets = {}, []
    hold = None
    for i in range(len(months) - 1):
        t0, t1 = months[i], months[i + 1]
        hist = Rn.loc[:t0].iloc[-52:]
        if len(hist) < 52:
            continue
        s = hist.mean() / hist.std() * np.sqrt(52)
        s = s.dropna()
        if s.empty:
            continue
        best = s.idxmax()
        top2 = s.nlargest(2).index
        seg = Rn.loc[t0:t1].iloc[1:]
        rets.append(pd.DataFrame({
            'chase best': seg[best],
            'chase top2': seg[list(top2)].mean(axis=1),
            'equal all': seg.mean(axis=1),
            'static LIVE': seg['LIVE composite'],
        }))
        picks[t0] = best
    M = pd.concat(rets)
    M.to_csv('analysis/meta_selector.csv')
    pd.Series(picks).to_csv('analysis/meta_picks.csv')

    print('\n' + '=' * 78)
    print('META-SELECTOR - monthly, rank by trailing 52w Sharpe (2015+)')
    print('=' * 78)
    for c in M.columns:
        r = M[c].dropna()
        cum = r.cumsum()
        print(f'{c:<14} Sharpe {sh(r):+.2f}  ann {52*r.mean()*100:+.2f}%  '
              f'maxDD {(cum-cum.cummax()).min()*100:.1f}%')
    ps = pd.Series(picks)
    print('\npick frequency:', dict(ps.value_counts()))
    sw = (ps != ps.shift()).sum()
    print(f'switches: {sw} in {len(ps)} months '
          f'(each switch = full book turnover in BOTH strategies)')


if __name__ == '__main__':
    main()
