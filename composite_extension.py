"""
Does adding REER (valuation) or CDS (credit) to sleeve A's composite help?

Context: the live composite is only two members,
    comp = z( 2*z(carry/realvol) + 1*z(ESI 4w chg) )
which prompted the fair question "so all that fundamental data is unused?".

A full IC screen on the 14-currency universe found only two factors that are
BOTH statistically alive AND not already inside carry:
    REER            IC +0.029  t +2.37  14 ccys   (valuation)
    CDS 4w chg      IC +0.028  t +1.59   7 ccys   (credit; never tested before -
                                                   earlier screens used min_ccy=8
                                                   and only 7 names have CDS)
Everything else either duplicates carry (real_carry 0.056, real_yield 0.046,
CPI diff 0.044 - all 0.5-0.87 correlated with carry/vol) or is dead (CTOT 0.002).

REER is tested in two forms because the raw index levels have different base
years, so a cross-sectional z of levels is partly a base-year artefact:
    reer_lvl    cross-sectional z of the level, negated (cheap currency = buy)
    reer_mis    deviation from the currency's OWN 5y mean in own-vol units,
                negated - a real misvaluation measure, base-year invariant

The composite's denominator already renormalizes by members actually present,
so a 7-of-14 member like CDS is handled without penalising the other 7.
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import (DATA, TRADED, VOL_TARGET, load, zsec, perf, run)

MIS_WIN = 260   # 5y of Fridays for the REER own-history window


def members():
    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)

    reer = load('L1_reer').reindex(columns=TRADED)
    reer_lvl = -reer
    lr = np.log(reer)
    mis = (lr - lr.rolling(MIS_WIN, min_periods=104).mean()) / \
        lr.rolling(MIS_WIN, min_periods=104).std()
    reer_mis = -mis

    cds = -load('L2_cds_chg_4w').reindex(columns=TRADED)   # spreads tighten = buy
    return dict(carry=cv, esi=esi, reer_lvl=reer_lvl, reer_mis=reer_mis, cds=cds)


def composite(parts, weights):
    """parts/weights keyed alike; renormalize by members actually present."""
    idx = parts['carry'].index
    num = pd.DataFrame(0.0, index=idx, columns=TRADED)
    den = pd.DataFrame(0.0, index=idx, columns=TRADED)
    for k, w in weights.items():
        z = zsec(parts[k]).reindex(idx)
        num = num + w * z.fillna(0)
        den = den + w * z.notna()
    raw = (num / den.replace(0, np.nan)).rolling(3).mean()
    return zsec(raw)


def xs_ic(sig, tot_next, min_ccy=8):
    ics = []
    for t in sig.index:
        if t not in tot_next.index:
            continue
        a, b = sig.loc[t], tot_next.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            ics.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(ics).dropna()
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)
    P = members()

    # --- how correlated is each candidate with what we already trade? ---
    print('=' * 92)
    print('STEP 1 - is the candidate actually NEW information?')
    print('=' * 92)
    zc = zsec(P['carry'])
    print(f"{'candidate':<12}{'corr vs z(carry/vol)':>22}{'corr vs z(ESI)':>17}"
          f"{'own IC':>9}{'t':>7}{'ccys':>6}")
    for k in ['esi', 'reer_lvl', 'reer_mis', 'cds']:
        z = zsec(P[k])
        c1 = zc.corrwith(z, axis=1).mean()
        c2 = zsec(P['esi']).corrwith(z, axis=1).mean()
        n = int(P[k].notna().sum(axis=1).max())
        ic, t, _ = xs_ic(z, tot_next, min_ccy=min(8, n))
        print(f'{k:<12}{c1:>+22.2f}{c2:>+17.2f}{ic:>+9.4f}{t:>+7.2f}{n:>6}')

    # --- run each candidate composite through the actual book ---
    disp_base = composite(P, {'carry': 2.0, 'esi': 1.0}).std(axis=1)
    dzb = (disp_base - disp_base.rolling(104, min_periods=40).mean()) / \
        disp_base.rolling(104, min_periods=40).std()
    mult = (dzb.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)

    specs = [
        ('LIVE  2*carry + 1*esi', {'carry': 2.0, 'esi': 1.0}),
        ('+ 1*reer_lvl', {'carry': 2.0, 'esi': 1.0, 'reer_lvl': 1.0}),
        ('+ 0.5*reer_lvl', {'carry': 2.0, 'esi': 1.0, 'reer_lvl': 0.5}),
        ('+ 1*reer_mis', {'carry': 2.0, 'esi': 1.0, 'reer_mis': 1.0}),
        ('+ 0.5*reer_mis', {'carry': 2.0, 'esi': 1.0, 'reer_mis': 0.5}),
        ('+ 1*cds', {'carry': 2.0, 'esi': 1.0, 'cds': 1.0}),
        ('+ 0.5*cds', {'carry': 2.0, 'esi': 1.0, 'cds': 0.5}),
        ('+ 1*reer_lvl + 1*cds', {'carry': 2.0, 'esi': 1.0,
                                  'reer_lvl': 1.0, 'cds': 1.0}),
        ('+ 0.5 reer_lvl + 0.5 cds', {'carry': 2.0, 'esi': 1.0,
                                      'reer_lvl': 0.5, 'cds': 0.5}),
    ]

    print()
    print('=' * 92)
    print('STEP 2 - does it improve the BOOK? (sleeve A, 14 ccys, 2013+, mid)')
    print('=' * 92)
    curves = {}
    for name, w in specs:
        comp = composite(P, w)
        ic, t, _ = xs_ic(comp, tot_next)
        net = run(comp, tot_next, atm, mult)
        curves[name] = net
        r = net.dropna()
        cum = r.cumsum()
        print(f'{name:<26} IC {ic:+.4f} (t {t:+.2f})   Sharpe '
              f'{r.mean()/r.std()*np.sqrt(52):+.2f}   ann '
              f'{52*r.mean()*100:+.2f}%   maxDD {(cum-cum.cummax()).min()*100:.1f}%')

    print()
    print('=' * 92)
    print('STEP 3 - subsample stability of the survivors')
    print('=' * 92)
    keep = [n for n, _ in specs]
    print(f"{'variant':<26}" + ''.join(f'{p:>12}' for p in
                                       ['13-16', '17-19', '20-22', '23-26']))
    for n in keep:
        row = f'{n:<26}'
        for a, b in [('2013', '2016'), ('2017', '2019'),
                     ('2020', '2022'), ('2023', '2026')]:
            r = curves[n].loc[a:b].dropna()
            row += f'{r.mean()/r.std()*np.sqrt(52):>+12.2f}' if len(r) > 30 else f'{"-":>12}'
        print(row)

    pd.DataFrame(curves).to_csv('analysis/composite_extension_curves.csv')
    print('\nwritten: analysis/composite_extension_curves.csv')


if __name__ == '__main__':
    main()
