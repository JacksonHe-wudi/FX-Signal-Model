"""
Research precomputes for the dashboard's ML Lab / Drivers & Meta tabs and the
Backtester's STFV strategy. One entry point:

    python3 fx_one.py research          (embedded)  or
    python3 research_lib.py             (standalone in the repo)

Writes: analysis/stfv_misalignment.csv, analysis/ml_predictions.csv,
analysis/pca_*.csv, analysis/strategy_library.csv, analysis/meta_selector.csv.
Slowest part is the ML walk-forward (~2-4 minutes); everything is optional -
the dashboard degrades gracefully without these files.
"""
import os

import numpy as np
import pandas as pd

from fx_model import (TRADED, VOL_TARGET, zsec, build_book, vol_target,
                      legs, composite, bundle_from_clean)

CLEAN = 'data/clean/clean_csv'


def _L(n):
    return pd.read_csv(f'{CLEAN}/{n}.csv', index_col=0, parse_dates=True)


# ------------------------------------------------------------- STFV ---------
def research_stfv(T):
    """BofA-style short-term fair value misalignment (for the Backtester's
    fade strategy - IC -0.007 on this universe, kept so users can verify)."""
    ydiff = _L('L2_ydiff_2y_vs_us').reindex(columns=TRADED)
    ctot = _L('L2_ctot_chg_13w').reindex(columns=TRADED)
    reg = _L('L1_regime')
    dxy = np.log(reg['DXY']).diff()
    ret = -np.log(T['spot'][TRADED]).diff()
    dyd = ydiff.diff()
    mis = pd.DataFrame(index=ret.index, columns=TRADED, dtype=float)
    for c in TRADED:
        dat = pd.concat([ret[c].rename('y'), dyd[c].rename('dyd'),
                         ctot[c].diff().rename('ctot'), dxy.rename('dxy')],
                        axis=1, sort=True)
        resid = pd.Series(index=dat.index, dtype=float)
        vals = dat[['dyd', 'ctot', 'dxy']].fillna(0.0).values
        yv = dat['y'].values
        for i in range(52, len(dat)):
            if np.isnan(yv[i]):
                continue
            win = dat.iloc[i - 52:i].dropna(subset=['y'])
            if len(win) < 30:
                continue
            Xw = np.c_[np.ones(len(win)),
                       win[['dyd', 'ctot', 'dxy']].fillna(0).values]
            try:
                beta = np.linalg.lstsq(Xw, win['y'].values, rcond=None)[0]
            except np.linalg.LinAlgError:
                continue
            resid.iloc[i] = yv[i] - np.r_[1, vals[i]] @ beta
        mis[c] = resid.rolling(4).sum()
    mis.to_csv('analysis/stfv_misalignment.csv')
    print('  ok analysis/stfv_misalignment.csv')


# ------------------------------------------------------ ML walk-forward -----
def research_ml(T):
    """TFRZ-style walk-forward (CEPR DP15305): characteristics x global
    conditions, Ridge + gradient boosting, expanding refit every 26 weeks.
    Honest verdict from the study: Ridge IC +0.032 (t 2.25) but book +0.11 vs
    live +1.30 on the same weeks - real information, no book value."""
    try:
        from sklearn.linear_model import Ridge
        from sklearn.ensemble import HistGradientBoostingRegressor
    except ImportError:
        print('  skip ML (pip install scikit-learn)')
        return
    reer = _L('L1_reer').reindex(columns=TRADED)
    lr = np.log(reer)
    chars = {
        'carry_vol': (T['carry'][TRADED] / T['rvol'][TRADED].clip(lower=1.0)),
        'carry': T['carry'][TRADED],
        'esi_chg': (T['esi'] - T['esi'].shift(4)).reindex(columns=TRADED),
        'real_carry': _L('L2_real_carry_1m').reindex(columns=TRADED),
        'reer_dev': -(lr - lr.rolling(260, min_periods=104).mean())
        / lr.rolling(260, min_periods=104).std(),
        'cds_chg': -_L('L2_cds_chg_4w').reindex(columns=TRADED),
        'mom4': -np.log(T['spot'][TRADED]).diff(4),
        'mom12': -np.log(T['spot'][TRADED]).diff(12),
        'rr_z': -_L('L2_rr25_1m_z_52w').reindex(columns=TRADED),
        'vrp': -(T['atm'][TRADED] - T['rvol'][TRADED]),
        'ca': _L('L2_ca_yoy_usdbn').reindex(columns=TRADED),
        'basis_z': _L('L2_fwdpts_basis_z_52w').reindex(columns=TRADED),
    }
    chars = {k: zsec(v) for k, v in chars.items()}
    idx = chars['carry_vol'].index
    reg = _L('L1_regime').reindex(idx)

    def z104(s):
        return ((s - s.rolling(104, min_periods=40).mean())
                / s.rolling(104, min_periods=40).std())
    glob = pd.DataFrame({
        'emvol_z': z104(reg['EMFXVOL']) if 'EMFXVOL' in reg else np.nan,
        'vix_z': z104(reg['VIX']) if 'VIX' in reg else np.nan,
        'dxy_tr': np.sign(reg['DXY'] - reg['DXY'].rolling(26).mean())
        if 'DXY' in reg else np.nan,
        'em_carry': z104(T['carry'][TRADED].mean(axis=1)),
    }, index=idx).ffill(limit=4)

    sr, cr = legs(T)
    y = (sr[TRADED] + cr[TRADED]).shift(-1)
    y = y.sub(y.mean(axis=1), axis=0)

    rows, meta = [], []
    gl_keys = ['emvol_z', 'vix_z', 'em_carry']
    for t in idx:
        g = glob.loc[t]
        for c in TRADED:
            xi = [chars[k].loc[t, c] if t in chars[k].index else np.nan
                  for k in chars]
            if np.isnan(xi[0]):
                continue
            inter = []
            for gk in gl_keys:
                gv = g[gk]
                inter += [xi[0] * gv, xi[2] * gv, xi[6] * gv]
            rows.append(xi + inter + [g['dxy_tr']] +
                        [y.loc[t, c] if t in y.index else np.nan])
            meta.append((t, c))
    cols = (list(chars) +
            [f'{a}_x_{b}' for b in gl_keys for a in ['carry_vol', 'esi', 'mom4']]
            + ['dxy_tr', 'y'])
    P = pd.DataFrame(rows, columns=cols,
                     index=pd.MultiIndex.from_tuples(meta, names=['date', 'ccy']))
    print(f'  ML panel: {len(P)} obs x {P.shape[1]-1} features')

    def walk_forward(model_fn):
        dates = P.index.get_level_values('date').unique().sort_values()
        Xcols = [c for c in P.columns if c != 'y']
        preds = {}
        i = 156
        while i < len(dates):
            tr = P.loc[P.index.get_level_values('date').isin(dates[:i])].dropna()
            te = P.loc[P.index.get_level_values('date').isin(dates[i:i + 26])]
            if len(tr) > 500 and len(te):
                m = model_fn()
                m.fit(tr[Xcols].fillna(0), tr['y'])
                for (t, c), v in zip(te.index, m.predict(te[Xcols].fillna(0))):
                    preds[(t, c)] = v
            i += 26
        s = pd.Series(preds)
        return s.unstack() if len(s) else pd.DataFrame()

    out = {}
    for nm, fn in [('Ridge (a=10)', lambda: Ridge(alpha=10.0)),
                   ('GBM (depth3)', lambda: HistGradientBoostingRegressor(
                       max_depth=3, max_iter=150, learning_rate=0.05,
                       l2_regularization=1.0, random_state=0))]:
        print(f'  walk-forward {nm} ...')
        out[nm] = walk_forward(fn)
    pd.concat(out, axis=1, sort=True).to_csv('analysis/ml_predictions.csv')
    print('  ok analysis/ml_predictions.csv')


# ----------------------------------------------- PCA + strategy library -----
def research_pca_meta(T):
    """PCA driver decomposition + the 7-strategy library + the monthly
    chase-the-best-trailing-Sharpe meta-selector backtest (verdict: chasing
    loses - 1.03 vs 1.33 for holding the live composite, 2015+)."""
    sr, cr = legs(T)
    tot = (sr[TRADED] + cr[TRADED])
    std = tot.div(tot.rolling(52, min_periods=20).std().shift(1)).dropna()

    def pca(X):
        X = X.dropna()
        Xc = X - X.mean()
        U, S, Vt = np.linalg.svd(Xc.values, full_matrices=False)
        var = S ** 2 / (S ** 2).sum()
        load = pd.DataFrame(Vt.T[:, :3], index=X.columns,
                            columns=['PC1', 'PC2', 'PC3'])
        scores = pd.DataFrame(U[:, :3] * S[:3], index=X.index,
                              columns=['PC1', 'PC2', 'PC3'])
        return load, scores, var[:3]

    em = tot.mean(axis=1)

    def orient(load, scores):
        for pc in ['PC1', 'PC2', 'PC3']:
            if scores[pc].corr(em.reindex(scores.index)) < 0:
                scores[pc] *= -1
                load[pc] *= -1
        return load, scores

    def var_decomp(X, scores):
        out = {}
        for c in X.columns:
            yv = X[c].reindex(scores.index)
            tv = yv.var()
            expl = [(yv.cov(scores[pc]) / scores[pc].var()) ** 2
                    * scores[pc].var() / tv for pc in ['PC1', 'PC2', 'PC3']]
            out[c] = expl + [1 - sum(expl)]
        return pd.DataFrame(out, index=['PC1', 'PC2', 'PC3', 'idio']).T

    load_f, scores_f, _ = pca(std)
    load_f, scores_f = orient(load_f, scores_f)
    vd_f = var_decomp(std, scores_f)
    load_r, scores_r, _ = pca(std.iloc[-104:])
    load_r, scores_r = orient(load_r, scores_r)
    vd_r = var_decomp(std.iloc[-104:], scores_r)
    load_f.to_csv('analysis/pca_loadings_full.csv')
    load_r.to_csv('analysis/pca_loadings_roll.csv')
    pd.concat({'full': vd_f, 'roll104': vd_r}, axis=1, sort=False) \
        .to_csv('analysis/pca_var_decomp.csv')
    print('  ok analysis/pca_*.csv')

    # strategy library on identical book machinery
    atm = T['atm'].reindex(columns=TRADED)
    carry = T['carry'][TRADED]

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
    px = -np.log(T['spot'][TRADED])
    lib = {'LIVE composite': book((2 * zc.fillna(0) + ze.fillna(0)) /
                                  (2 * zc.notna() + ze.notna())
                                  .replace(0, np.nan)),
           'carry/vol only': book(cv),
           'ESI only': book(esi),
           'TSMOM 12w': book(tot.rolling(12).sum()),
           'MA cross': book(px.rolling(10).mean() - px.rolling(40).mean())}
    if os.path.exists('analysis/stfv_misalignment.csv'):
        mis = pd.read_csv('analysis/stfv_misalignment.csv', index_col=0,
                          parse_dates=True)
        lib['STFV fade'] = book(-mis.reindex(columns=TRADED).rolling(2).mean())
    if os.path.exists('analysis/ml_predictions.csv'):
        raw = pd.read_csv('analysis/ml_predictions.csv', header=[0, 1],
                          index_col=0, parse_dates=True)
        lib['ML ridge'] = book(raw['Ridge (a=10)'].reindex(columns=TRADED))
    R = pd.DataFrame(lib)
    R.to_csv('analysis/strategy_library.csv')
    print('  ok analysis/strategy_library.csv')

    Rn = R.div(R.std() * np.sqrt(52)) * 0.05
    months = Rn.resample('ME').last().index
    rets, picks = [], {}
    for i in range(len(months) - 1):
        t0, t1 = months[i], months[i + 1]
        hist = Rn.loc[:t0].iloc[-52:]
        if len(hist) < 52:
            continue
        srank = (hist.mean() / hist.std() * np.sqrt(52)).dropna()
        if srank.empty:
            continue
        seg = Rn.loc[t0:t1].iloc[1:]
        rets.append(pd.DataFrame({
            'chase best': seg[srank.idxmax()],
            'chase top2': seg[list(srank.nlargest(2).index)].mean(axis=1),
            'equal all': seg.mean(axis=1),
            'static LIVE': seg['LIVE composite']}))
        picks[t0] = srank.idxmax()
    pd.concat(rets).to_csv('analysis/meta_selector.csv')
    pd.Series(picks).to_csv('analysis/meta_picks.csv')
    print('  ok analysis/meta_selector.csv')


def research_main():
    os.makedirs('analysis', exist_ok=True)
    T = bundle_from_clean()
    print('[research] STFV fair-value misalignment')
    research_stfv(T)
    print('[research] ML walk-forward (the slow part, ~2-4 min)')
    research_ml(T)
    print('[research] PCA drivers + strategy library + meta-selector')
    research_pca_meta(T)
    print('[research] done - restart the dashboard to see the new tabs filled')


if __name__ == '__main__':
    research_main()
