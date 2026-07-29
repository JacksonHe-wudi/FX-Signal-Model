"""
ML multi-factor model - walk-forward, honest, on the 13 tradable currencies.

Method follows Taylor-Filippou-Rapach-Zhou (CEPR DP15305, "Exchange Rate
Prediction with Machine Learning and a Smart Carry Trade Portfolio"): country
characteristics alone don't forecast FX, but characteristics INTERACTED with
global financial conditions do, and regularized ML handles the resulting
high-dimensional feature set.

Setup
  panel        week x currency, 2013+ (signals lagged - features at t predict
               the return realized over week t+1)
  target       next-week total return, cross-sectionally demeaned (we trade RV,
               so the dollar component is removed from the label)
  characteristics (per ccy)   carry/realvol, carry, ESI 4w chg, real carry,
               REER 5y deviation, CDS 4w chg, mom 4w, mom 12w, RR z, VRP,
               CA YoY, fwd-basis z
  global state (per week)     EM vol z, VIX z, DXY 26w trend, avg EM carry z
  features     characteristics + characteristic x global interactions
               (the TFRZ mechanism), all cross-sectionally z-scored
  models       Ridge (linear benchmark) and HistGradientBoosting (nonlinear)
  validation   expanding walk-forward, refit every 26 weeks, first 156 weeks
               are training-only. NOTHING in the feature/label pipeline sees
               the future.
  portfolio    each week long top3 / short bottom3 of the model's prediction,
               1/ATM-vol legs - the SAME machinery as the live book, so the
               comparison is apples to apples.

Output: analysis/ml_predictions.csv (for the dashboard) + console report.
"""
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor

from fx_model import TRADED, VOL_TARGET, load_clean, zsec, build_book, \
    vol_target, legs, composite

D = 'data/clean/clean_csv'
TRAIN_MIN = 156          # 3y before the first prediction
REFIT = 26               # refit cadence in weeks


def L(n):
    return pd.read_csv(f'{D}/{n}.csv', index_col=0, parse_dates=True)


def build_panel(T):
    reer = L('L1_reer').reindex(columns=TRADED)
    lr = np.log(reer)
    chars = {
        'carry_vol': (T['carry'][TRADED] / T['rvol'][TRADED].clip(lower=1.0)),
        'carry': T['carry'][TRADED],
        'esi_chg': (T['esi'] - T['esi'].shift(4)).reindex(columns=TRADED),
        'real_carry': L('L2_real_carry_1m').reindex(columns=TRADED),
        'reer_dev': -(lr - lr.rolling(260, min_periods=104).mean())
        / lr.rolling(260, min_periods=104).std(),
        'cds_chg': -L('L2_cds_chg_4w').reindex(columns=TRADED),
        'mom4': -np.log(T['spot'][TRADED]).diff(4),
        'mom12': -np.log(T['spot'][TRADED]).diff(12),
        'rr_z': -L('L2_rr25_1m_z_52w').reindex(columns=TRADED),
        'vrp': -(T['atm'][TRADED] - T['rvol'][TRADED]),
        'ca': L('L2_ca_yoy_usdbn').reindex(columns=TRADED),
        'basis_z': L('L2_fwdpts_basis_z_52w').reindex(columns=TRADED),
    }
    chars = {k: zsec(v) for k, v in chars.items()}
    idx = chars['carry_vol'].index

    reg = L('L1_regime').reindex(idx)
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
    tot = (sr[TRADED] + cr[TRADED])
    y = tot.shift(-1)                        # next week's realized return
    y = y.sub(y.mean(axis=1), axis=0)        # cross-sectionally demeaned

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
                inter += [xi[0] * gv, xi[2] * gv, xi[6] * gv]   # carry_vol/esi/mom4 x global
            yy = y.loc[t, c] if t in y.index else np.nan
            rows.append(xi + inter + [g['dxy_tr']] + [yy])
            meta.append((t, c))
    cols = (list(chars) +
            [f'{a}_x_{b}' for b in gl_keys for a in ['carry_vol', 'esi', 'mom4']] +
            ['dxy_tr', 'y'])
    P = pd.DataFrame(rows, columns=cols,
                     index=pd.MultiIndex.from_tuples(meta, names=['date', 'ccy']))
    return P


def walk_forward(P, model_fn):
    dates = P.index.get_level_values('date').unique().sort_values()
    Xcols = [c for c in P.columns if c != 'y']
    preds = {}
    i = TRAIN_MIN
    while i < len(dates):
        tr_dates = dates[:i]
        te_dates = dates[i:i + REFIT]
        tr = P.loc[P.index.get_level_values('date').isin(tr_dates)].dropna()
        te = P.loc[P.index.get_level_values('date').isin(te_dates)]
        if len(tr) > 500 and len(te):
            m = model_fn()
            m.fit(tr[Xcols].fillna(0), tr['y'])
            p = m.predict(te[Xcols].fillna(0))
            for (t, c), v in zip(te.index, p):
                preds[(t, c)] = v
        i += REFIT
    s = pd.Series(preds)
    return s.unstack() if len(s) else pd.DataFrame()


def book_from_score(score, T, sr, cr):
    comp = zsec(score.reindex(columns=TRADED))
    atm = T['atm'].reindex(columns=TRADED)
    W = build_book(comp, atm)
    Wl = W.shift(1).reindex(sr.index)[TRADED]
    r = (Wl * (sr[TRADED] + cr[TRADED])).sum(axis=1)
    r = r[Wl.abs().sum(axis=1) > 0]
    return (r * vol_target(r)).dropna()


def perf(r, name):
    r = r.dropna()
    cum = r.cumsum()
    return (f'{name:<38} Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}  '
            f'ann {52*r.mean()*100:+.2f}%  maxDD {(cum-cum.cummax()).min()*100:.1f}%'
            f'  n={len(r)}')


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    print('building panel...')
    P = build_panel(T)
    print(f'panel: {len(P)} obs, {P.shape[1]-1} features, '
          f'{P.index.get_level_values("date").nunique()} weeks')

    models = {
        'Ridge (a=10)': lambda: Ridge(alpha=10.0),
        'GBM (depth3)': lambda: HistGradientBoostingRegressor(
            max_depth=3, max_iter=150, learning_rate=0.05,
            l2_regularization=1.0, random_state=0),
    }
    print('\n' + '=' * 88)
    print('walk-forward ML vs the live 2-member composite '
          '(same book machinery, same weeks)')
    print('=' * 88)

    # benchmark on the SAME weeks the ML can trade
    comp_live = composite(T)
    out = {}
    for nm, fn in models.items():
        print(f'\n{nm}: walk-forward...', flush=True)
        S = walk_forward(P, fn)
        out[nm] = S
        r_ml = book_from_score(S, T, sr, cr)
        r_lv = book_from_score(comp_live.loc[S.index], T, sr, cr)
        ic = []
        y = (sr[TRADED] + cr[TRADED]).shift(-1)
        for t in S.index:
            if t in y.index:
                a, b = S.loc[t], y.loc[t]
                m = a.notna() & b.notna()
                if m.sum() >= 8:
                    ic.append(a[m].rank().corr(b[m].rank()))
        ic = pd.Series(ic)
        print(f'  IC {ic.mean():+.4f} (t {ic.mean()/ic.std()*np.sqrt(len(ic)):+.2f})')
        print(' ', perf(r_ml, f'{nm} book'))
        print(' ', perf(r_lv, 'live composite, same weeks'))
        blend = 0.5 * (r_ml * 0.05 / (r_ml.std() * np.sqrt(52))) + \
            0.5 * (r_lv * 0.05 / (r_lv.std() * np.sqrt(52))).reindex(r_ml.index)
        print(' ', perf(blend.dropna(), '50/50 blend'))

    pd.concat(out, axis=1).to_csv('analysis/ml_predictions.csv')
    print('\nwritten: analysis/ml_predictions.csv')


if __name__ == '__main__':
    main()
