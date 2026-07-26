"""
IDEA 1 - time sleeve B with the average forward discount (Lustig-Roussanov-
Verdelhan, "Countercyclical currency risk premia", JFE 2014).

LRV's result: the AVERAGE forward discount of the dollar against a basket of
foreign currencies is the best single predictor of the average excess return
that basket earns against the dollar. The premium is countercyclical - wide
average discount = high price of risk = high expected basket return; narrow or
negative = the dollar itself is the carry asset.

Why this is the right paper for us: sleeve B is a permanently-on long-EM /
short-funding-basket position, and strategy_logic.md 3.3 flags the exact gap -
the cross-sectional z-score inside sleeve A kills any aggregate view, so the
book has no "when should I be big in EM" switch. AFD is a level signal, so it
survives where a z-scored composite cannot.

Our AFD = cross-sectional mean of carry_1m_ann over the 14-currency basket,
which is already an implied-yield-minus-SOFR spread, i.e. exactly a forward
discount versus the dollar. Signal is the trailing z of that level (lagged one
week - no look-ahead).

Three uses tested:
    scale   gross multiplier 1 + k*clip(z,-1,1)     (never flips, always long EM)
    tilt    multiplier 1 + k*z, floored at 0        (can go flat, never short)
    flip    sign(z) - the full LRV dollar-carry, short EM when AFD is low
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import TRADED, VOL_TARGET, load, perf

WIN = 260   # 5y of Fridays for the AFD z-score


def main():
    # convention copied verbatim from funding_overlay.py so the baseline
    # reproduces the live sleeve B (Sharpe 0.66): tot[t] is the return realized
    # over the week ending t, basket weights lagged one week.
    spot = load('L1_spot_usd_all30')
    carry = load('L1_carry_1m_ann')
    rvol = load('L1_vol_realized_1m')
    tot = (-np.log(spot).diff()).add((carry / 100 / 52).shift(1), fill_value=np.nan)

    iv = (1.0 / rvol[TRADED].clip(lower=1.0)).shift(1)
    wB = iv.div(iv.sum(axis=1), axis=0)
    em = (wB * tot[TRADED]).sum(axis=1, min_count=8)
    g3 = (tot['EUR'] + tot['JPY']) / 3.0     # 1/3 USD leg = zero by construction
    base = (em - 0.5 * tot['CAD'] - 0.5 * g3).dropna()

    # ---- LRV average forward discount ----
    afd = carry[TRADED].mean(axis=1)
    z = ((afd - afd.rolling(WIN, min_periods=104).mean()) /
         afd.rolling(WIN, min_periods=104).std()).shift(1)

    print('=' * 92)
    print('IDEA 1 - average forward discount (LRV 2014) as sleeve B timing')
    print('=' * 92)
    print(f'AFD level today {afd.dropna().iloc[-1]:+.2f}%  '
          f'z {z.dropna().iloc[-1]:+.2f}   (5y window)')
    print(f'AFD z range {z.min():+.2f} .. {z.max():+.2f}, '
          f'weeks with z<0: {100*(z<0).mean():.0f}%\n')

    def vt(r):
        r = r.dropna()
        lev = (VOL_TARGET / (r.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
        return (r * lev).dropna()

    print(perf(vt(base), 'B static (live)'))
    for k in [0.5, 1.0]:
        m = (1 + k * z.clip(-1, 1)).reindex(base.index)
        print(perf(vt(base * m), f'B scale  k={k}'))
    for k in [0.5, 1.0]:
        m = (1 + k * z).clip(lower=0).reindex(base.index)
        print(perf(vt(base * m), f'B tilt   k={k} (can go flat)'))
    s = np.sign(z).reindex(base.index).fillna(0)
    print(perf(vt(base * s), 'B flip   sign(z)  [full LRV]'))
    print(perf(vt(base * (z > 0).reindex(base.index).astype(float)),
               'B on/off  long only when z>0'))

    # ---- does AFD predict the basket at all? the LRV claim itself ----
    print('\npredictive regression of next-4w basket return on AFD z:')
    y = base.rolling(4).sum().shift(-4)
    d = pd.concat([z, y], axis=1).dropna()
    d.columns = ['z', 'y']
    b = np.polyfit(d['z'], d['y'], 1)
    resid = d['y'] - np.polyval(b, d['z'])
    se = resid.std() / (d['z'].std() * np.sqrt(len(d) / 4))   # 4w overlap adj
    print(f'  beta {b[0]*10000:+.1f}bp per 1 z   t {b[0]/se:+.2f}   '
          f'R2 {1-resid.var()/d["y"].var():.3f}   n {len(d)} (overlapping)')

    # ---- does it also help sleeve A? (A is XS, so this is a gross-timing test) ----
    A = pd.read_csv('analysis/sleeve_a_curves.csv', index_col=0,
                    parse_dates=True)['BASE (top3/5)'].dropna()
    m = (1 + 0.5 * z.clip(-1, 1)).reindex(A.index).fillna(1.0)
    print()
    print(perf(A, 'A static (live)'))
    print(perf(A * m, 'A x AFD scale k=0.5'))


if __name__ == '__main__':
    main()
