"""
Layer 5 - high-conviction trigger sweep (user request: no universe
expansion; trade LESS, only when conviction is high).

Grid (reported in full - the whole curve matters, not the best cell):
  entry threshold T on the adaptive composite z: 0.5 / 0.75 / 1.0 / 1.25 / 1.5
  exit at 0.6*T (hysteresis)
  x  confirmation filter: none | >=2 of 3 pillars agree with composite sign
Plus a threshold-ensemble (average of all T books) as the anti-overfit pick.

Position sizing: active names weighted z/ATMvol, gross scaled by
min(1, n_active/4)*200% so a lone signal cannot carry the whole book;
5% vol target; mid execution; total returns (spot + 1M carry/52).
"""
import os
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
START = '2014-01-01'
VOL_TARGET = 0.05


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def sharpe(r):
    r = r.dropna()
    return r.mean() / r.std() * np.sqrt(52) if len(r) > 20 and r.std() > 0 else np.nan


def main():
    spot = load('L1_spot_usd_asia9')
    dlog = np.log(spot).diff()
    tot_next = (-dlog + (load('L1_carry_1m_ann') / 100 / 52).shift(1)).shift(-1)
    atm = load('L1_vol_implied_atm_1m')
    comp = load('composite_adaptive', AN)
    pillars = {p: load(f'pillar_score_{p}', AN)
               for p in ['fundamental', 'momentum', 'technical']}
    dates = comp.index.intersection(tot_next.index)
    dates = dates[dates >= START]

    def agree_ok(t, ccy, comp_sign):
        n = 0
        for s in pillars.values():
            if t in s.index and ccy in s.columns and pd.notna(s.loc[t, ccy]):
                if np.sign(s.loc[t, ccy]) == comp_sign:
                    n += 1
        return n >= 2

    def run(T, use_agree):
        active = {}                                     # ccy -> side
        wgt = pd.DataFrame(0.0, index=dates, columns=ASIA9)
        for t in dates:
            z = comp.loc[t].dropna()
            # exits (hysteresis at 0.6T)
            for c in list(active):
                if c not in z.index or abs(z[c]) < 0.6 * T or np.sign(z[c]) != active[c]:
                    del active[c]
            # entries
            for c in z.index:
                if c in active or abs(z[c]) < T:
                    continue
                sgn = np.sign(z[c])
                if use_agree and not agree_ok(t, c, sgn):
                    continue
                active[c] = sgn
            if active:
                v = pd.Series({c: active[c] * abs(z.get(c, 0)) / atm.loc[t, c]
                               for c in active if pd.notna(atm.loc[t, c])})
                if len(v):
                    g = min(1.0, len(v) / 4) * 2.0
                    wgt.loc[t, v.index] = v / v.abs().sum() * g
        raw = (wgt * tot_next.reindex(dates)).sum(axis=1)
        lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
        net = raw * lev.fillna(0)
        inmkt = (wgt.abs().sum(axis=1) > 0)
        act = net[inmkt]
        return net, {
            'sharpe': round(sharpe(net), 2),
            'ann_ret': f'{net.mean()*52:.2%}',
            'maxDD': f'{(net.cumsum()-net.cumsum().cummax()).min():.2%}',
            'pct_in_mkt': f'{inmkt.mean():.0%}',
            'avg_names': round(wgt.abs().gt(0).sum(axis=1).mean(), 1),
            'hit_active': f'{(act > 0).mean():.0%}' if len(act) else '-'}

    rows, curves = [], {}
    for use_agree in [False, True]:
        for T in [0.5, 0.75, 1.0, 1.25, 1.5]:
            net, m = run(T, use_agree)
            key = f"T={T}{'+agree' if use_agree else ''}"
            rows.append({'config': key, **m})
            curves[key] = net
    ens = pd.DataFrame({k: v for k, v in curves.items()}).mean(axis=1)
    lev = (VOL_TARGET / (ens.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
    ens = ens * lev.fillna(0)
    rows.append({'config': 'ENSEMBLE(all 10)', 'sharpe': round(sharpe(ens), 2),
                 'ann_ret': f'{ens.mean()*52:.2%}',
                 'maxDD': f'{(ens.cumsum()-ens.cumsum().cummax()).min():.2%}',
                 'pct_in_mkt': '-', 'avg_names': '-', 'hit_active': '-'})
    curves['ENSEMBLE'] = ens

    res = pd.DataFrame(rows)
    print(res.to_string(index=False))

    segs = [('2014-2015', '2014', '2015'), ('2016-2019', '2016', '2019'),
            ('2020-2021', '2020', '2021'), ('2022-2023', '2022', '2023'),
            ('2024-2026', '2024', '2026')]
    best = res.iloc[res['sharpe'].apply(
        lambda x: x if pd.notna(x) else -9).idxmax()]['config']
    print(f'\nsubsamples - {best} vs ENSEMBLE:')
    for nm, a, b in segs:
        print(f"  {nm}: {sharpe(curves[best].loc[a:b])!s:>5} | "
              f"{sharpe(curves['ENSEMBLE'].loc[a:b]):.2f}")

    os.makedirs(AN, exist_ok=True)
    res.to_csv(f'{AN}/conviction_sweep.csv', index=False)
    pd.DataFrame(curves).to_csv(f'{AN}/conviction_curves.csv')


if __name__ == '__main__':
    main()
