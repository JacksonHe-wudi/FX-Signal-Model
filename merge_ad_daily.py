"""
Two of the user's proposals, tested:

TEST 1 - merge sleeve D into sleeve A
  Instead of running the CNH fixing signal as its own single-leg book, inject
  it into the cross-sectional composite: comp[CNH] += lam * gated_sign(CCF).
  The merged book is compared against the live A(2/3)+D(1/3) stack at the same
  total risk. lam swept.

TEST 2 - DAILY technical gating of sleeve A
  We now have daily spot, so run the technical check every DAY, not once a
  week: hold the weekly position only on days when the daily technical agrees
  with its side, flat otherwise. Daily P&L = -dlog(spot) + carry/5 per business
  day. Technicals: 20d vol-adjusted momentum and 50/200 MA cross (the two least
  bad from the weekly screen). Ungated daily book is the benchmark - identical
  data, so the comparison isolates the gate.
"""
import numpy as np
import pandas as pd

from fx_model import (TRADED, VOL_TARGET, RISK, load_clean, zsec, build_book,
                      composite, vol_target, legs, sleeve_A, sleeve_D)

D = 'data/clean/clean_csv'


def L(n):
    return pd.read_csv(f'{D}/{n}.csv', index_col=0, parse_dates=True)


def perf(r, name):
    r = r.dropna()
    ann = np.sqrt(252 if r.index.freqstr is None and
                  (r.index[1:] - r.index[:-1]).median().days <= 3 else 52)
    # infer frequency robustly: weekly if median gap >= 5 days
    ann = np.sqrt(52) if (r.index[1:] - r.index[:-1]).median().days >= 5 else np.sqrt(252)
    cum = r.cumsum()
    return (f'{name:<42} Sharpe {r.mean()/r.std()*ann:+.2f}  '
            f'ann {r.mean()*ann**2*100:+.2f}%  maxDD {(cum-cum.cummax()).min()*100:.1f}%')


def sc(r, vol=VOL_TARGET):
    r = r.dropna()
    ann = np.sqrt(52) if (r.index[1:] - r.index[:-1]).median().days >= 5 else np.sqrt(252)
    return r * vol / (r.std() * ann)


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    atm = T['atm'].reindex(columns=TRADED)

    # ================= TEST 1: merge D into A =================
    print('=' * 88)
    print('TEST 1 - CCF injected into the composite  vs  separate sleeves')
    print('=' * 88)
    A = sleeve_A(T)
    Dd = sleeve_D(T)
    rA = (A['spot'] + A['carry']).dropna()
    rD = (Dd['spot'] + Dd['carry']).dropna()
    sep = (2 / 3 * sc(rA).reindex(rA.index).fillna(0)
           + 1 / 3 * sc(rD).reindex(rA.index).fillna(0))
    print(perf(sep, 'SEPARATE  A(2/3) + D(1/3), 5%-vol scaled'))
    print(perf(sc(rA), '  sleeve A alone'))
    print(perf(sc(rD), '  sleeve D alone'))

    # the gated CCF sign, exactly as sleeve D computes it (pre-shift)
    fix = T['fix']
    idx = A['comp'].index
    ccf = ((fix['CNY_FIX'] - fix['BBG_CNY_FIX']) / fix['CNY_FIX'] * 1e4).reindex(idx)
    trend = np.log(T['spot']['CNH']).diff(4).reindex(idx)
    a = T['atm']['CNH'].reindex(idx)
    vz = (a - a.rolling(104, min_periods=40).mean()) / \
        a.rolling(104, min_periods=40).std()
    sig = np.sign(ccf).where((ccf * trend < 0) & (vz < 1.5), 0.0).fillna(0.0)

    for lam in [0.5, 1.0, 2.0]:
        comp = A['comp'].copy()
        comp['CNH'] = comp['CNH'].fillna(0) + lam * sig
        comp = zsec(comp)
        W = build_book(comp, atm)
        disp = comp.std(axis=1)
        dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
            disp.rolling(104, min_periods=40).std()
        mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)
        Wl = W.mul(mult, axis=0).shift(1).reindex(sr.index)[TRADED]
        r = (Wl * (sr[TRADED] + cr[TRADED])).sum(axis=1)
        r = (r * vol_target(r)).dropna()
        print(perf(sc(r), f'MERGED    comp[CNH] += {lam} * gated_CCF'))

    # ================= TEST 2: daily technical gate =================
    print()
    print('=' * 88)
    print('TEST 2 - daily technical gate on sleeve A (daily P&L, mid)')
    print('=' * 88)
    spd = L('L1_spot_daily_traded').reindex(columns=TRADED)
    spd = spd[spd.index >= '2013'].dropna(how='all')
    ret_d = -np.log(spd).diff()
    carry_d = (T['carry'][TRADED] / 100 / 252 * 5 / 5).reindex(spd.index, method='ffill')
    tot_d = ret_d + carry_d / 5 * 5 / 5      # carry per business day
    tot_d = ret_d + (T['carry'][TRADED] / 100 / 260).reindex(spd.index, method='ffill')

    # weekly book expanded to daily, held Fri close -> next Fri close
    Wl = A['weights'].reindex(columns=TRADED)
    Wd = Wl.reindex(spd.index, method='ffill')
    base = (Wd * tot_d).sum(axis=1).dropna()
    print(perf(sc(base), 'no gate (weekly book, daily P&L)'))

    px = -np.log(spd)
    d1 = px.diff()
    techs = {
        'vamom_20d': px.diff(20) / (d1.rolling(20).std() * np.sqrt(20)).replace(0, np.nan),
        'ma_cross_50_200': px.rolling(50).mean() - px.rolling(200).mean(),
    }
    for nm, s in techs.items():
        agree = (np.sign(s.shift(1)) == np.sign(Wd)) & (Wd != 0)
        Wg = Wd.where(agree, 0.0)
        r = (Wg * tot_d).sum(axis=1).dropna()
        kept = (Wg.abs().sum(axis=1) / Wd.abs().sum(axis=1).replace(0, np.nan)).mean()
        print(perf(sc(r), f'daily gate: {nm} (keeps {100*kept:.0f}% gross)'))
    # also: technical as a daily OVERLAY (add-on tilt) rather than a veto
    for nm, s in techs.items():
        tilt = np.sign(s.shift(1)).where(Wd != 0, 0.0) * Wd.abs() * 0.5
        r = ((Wd + tilt) * tot_d).sum(axis=1).dropna()
        print(perf(sc(r), f'daily 0.5x tilt: {nm}'))


if __name__ == '__main__':
    main()
