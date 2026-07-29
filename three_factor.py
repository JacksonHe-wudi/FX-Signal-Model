"""
Three-pillar model: FUNDAMENTAL + SENTIMENT + TECHNICAL, on the 13 tradable
currencies (MYR out).

The user wants the classic three-pillar structure. Earlier attempts died, but
the universe has changed twice since (9 -> 14 -> 13 names) and sentiment was
only ever tested as RR z alone, so this is a clean rebuild:

PILLAR F (fundamental)  = the live composite: 2 z(carry/realvol) + 1 z(ESI 4w)

PILLAR S (sentiment)    - options and price-vs-fundamental measures, since we
  have no usable flow data (positioning tables are empty for the 13):
    rr_z       52w z of the 25d risk reversal, FADED (stretched fear = buy)
    vrp        implied ATM - realized vol, cross-sectional, FADED
               (rich vol = fear priced in = buy the currency)
    divergence 12w price momentum z MINUS fundamental z: price ran ahead of
               fundamentals = sentiment-driven = fade it
    basis_z    52w z of forward-points basis (funding squeeze), faded

PILLAR T (technical)    - from DAILY spot, sampled Fridays:
    ma_cross   50d vs 200d
    vamom      20d return / 20d daily vol
    rsi        Wilder 14d, faded

Each candidate is IC-screened alone; each pillar is the equal-weight blend of
its members; the 3-pillar composite is tested at several weight schemes through
the full sleeve-A machinery against the live 2-member composite.
"""
import numpy as np
import pandas as pd

from fx_model import (TRADED, VOL_TARGET, load_clean, zsec, build_book,
                      composite, vol_target, legs, sleeve_A)

D = 'data/clean/clean_csv'


def L(n):
    return pd.read_csv(f'{D}/{n}.csv', index_col=0, parse_dates=True)


def ric(sig, tot, min_ccy=8):
    out = []
    for t in sig.index:
        if t not in tot.index:
            continue
        a, b = sig.loc[t], tot.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            out.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(out).dropna()
    if len(s) < 50:
        return np.nan, np.nan, len(s)
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    tot_next = (sr[TRADED] + cr[TRADED]).shift(-1)   # signal-date convention
    atm = T['atm'].reindex(columns=TRADED)

    # ---------------- pillar F ----------------
    zF = composite(T)

    # ---------------- pillar S candidates ----------------
    rr_z = -L('L2_rr25_1m_z_52w').reindex(columns=TRADED)
    vrp = -(T['atm'].reindex(columns=TRADED) - T['rvol'].reindex(columns=TRADED))
    mom12 = L('L2_mom_12w_ex1w').reindex(columns=TRADED)
    diver = -(zsec(mom12) - zF)          # price ahead of fundamentals -> fade
    basis = -L('L2_fwdpts_basis_z_52w').reindex(columns=TRADED)
    S_CAND = {'rr_z (faded)': rr_z, 'vrp (faded)': vrp,
              'divergence (faded)': diver, 'basis_z (faded)': basis}

    # ---------------- pillar T candidates ----------------
    spd = L('L1_spot_daily_traded').reindex(columns=TRADED)
    px = -np.log(spd)
    d = px.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = -(100 - 100 / (1 + up / dn.replace(0, np.nan)) - 50)
    T_CAND = {
        'ma_cross_50_200': (px.rolling(50).mean() - px.rolling(200).mean()),
        'vamom_20d': px.diff(20) / (d.rolling(20).std() * np.sqrt(20)).replace(0, np.nan),
        'rsi_14 (faded)': rsi,
    }
    fri = zF.index
    T_CAND = {k: v.reindex(fri, method='ffill') for k, v in T_CAND.items()}

    print('=' * 84)
    print('STEP 1 - member ICs on the 13 tradable currencies (weekly, 2013+)')
    print('=' * 84)
    print(f"{'candidate':<26}{'IC':>9}{'t':>7}{'n':>6}")
    m, t, n = ric(zF, tot_next)
    print(f'{"PILLAR F (live comp)":<26}{m:>+9.4f}{t:>+7.2f}{n:>6}')
    groups = {'S': S_CAND, 'T': T_CAND}
    pillars = {'F': zF}
    for g, cands in groups.items():
        zs = []
        for k, v in cands.items():
            z = zsec(v.reindex(fri))
            m, t, n = ric(z, tot_next)
            print(f'{g+": "+k:<26}{m:>+9.4f}{t:>+7.2f}{n:>6}')
            zs.append(z)
        pil = zsec(sum(z.fillna(0) for z in zs) /
                   sum(z.notna() for z in zs).replace(0, np.nan))
        m, t, n = ric(pil, tot_next)
        print(f'{"PILLAR "+g+" (blend)":<26}{m:>+9.4f}{t:>+7.2f}{n:>6}')
        pillars[g] = pil

    print()
    print('pillar cross-correlations (weekly, cross-sectional):')
    for a in 'FST':
        row = ''
        for b in 'FST':
            c = pillars[a].corrwith(pillars[b], axis=1).mean()
            row += f'{c:>+8.2f}'
        print(f'  {a}: {row}')

    # ---------------- STEP 2: books ----------------
    print()
    print('=' * 84)
    print('STEP 2 - full sleeve-A machinery, 13 ccys, weight schemes F:S:T')
    print('=' * 84)

    def run_book(comp):
        W = build_book(comp, atm)
        disp = comp.std(axis=1)
        dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
            disp.rolling(104, min_periods=40).std()
        mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)
        Wl = W.mul(mult, axis=0).shift(1).reindex(sr.index)[TRADED]
        r = (Wl * (sr[TRADED] + cr[TRADED])).sum(axis=1)
        lev = vol_target(r)
        return (r * lev).dropna()

    schemes = [('LIVE  F only (2-member)', {'F': 1}),
               ('F2 S1', {'F': 2, 'S': 1}),
               ('F2 T1', {'F': 2, 'T': 1}),
               ('F2 S1 T1', {'F': 2, 'S': 1, 'T': 1}),
               ('F1 S1 T1 (equal)', {'F': 1, 'S': 1, 'T': 1}),
               ('F4 S1 T1', {'F': 4, 'S': 1, 'T': 1})]
    print(f"{'scheme':<26}{'IC':>9}{'t':>7}{'Sharpe':>8}{'ann%':>8}{'maxDD%':>8}"
          + ''.join(f'{p:>8}' for p in ['13-16', '17-19', '20-22', '23-26']))
    for nm, w in schemes:
        num = sum(v * pillars[k].fillna(0) for k, v in w.items())
        den = sum(v * pillars[k].notna() for k, v in w.items())
        comp = zsec((num / den.replace(0, np.nan)).rolling(1).mean())
        m, t, _ = ric(comp, tot_next)
        r = run_book(comp)
        cum = r.cumsum()
        row = (f'{nm:<26}{m:>+9.4f}{t:>+7.2f}'
               f'{r.mean()/r.std()*np.sqrt(52):>+8.2f}{52*r.mean()*100:>+8.2f}'
               f'{(cum-cum.cummax()).min()*100:>8.1f}')
        for a, b in [('2013', '2016'), ('2017', '2019'),
                     ('2020', '2022'), ('2023', '2026')]:
            x = r.loc[a:b]
            row += f'{x.mean()/x.std()*np.sqrt(52):>+8.2f}' if len(x) > 30 else f'{"-":>8}'
        print(row)


if __name__ == '__main__':
    main()
