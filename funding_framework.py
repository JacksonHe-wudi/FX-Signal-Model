"""
FUNDING-LEG FRAMEWORK - "find the best EM long, then find the best funding
currency, where 'best' depends on the market regime".

What makes a good funding currency (the priors being tested):
  1. CHEAP TO SHORT   low carry - you pay little to be short it
  2. FALLS WHEN YOU NEED IT   high beta to EM risk - in a stress week your
     short rallies your P&L exactly when the long leg bleeds
  3. DIVERSIFIES      low correlation with the long leg otherwise
These conflict: the cheapest currencies to short (JPY, EUR) are the ones that
RALLY in stress (safe havens - being short them in risk-off hurts), while the
currencies that fall in stress (high-beta EM) are expensive to short. Hence
the regime hypothesis: calm -> fund with low-carry G-currencies and pocket the
spread; stress -> fund with high-beta currencies so the short side hedges.

Setup (all weights lagged one week, weekly, 2013+, mid):
  LONG leg   equal-vol basket of the 13 tradable EM (same as sleeve B)
  REGIME     mean 1M ATM vol of the 13, 104w z, lagged: calm z<0, stress z>=0
  FUNDING    rebuilt each week from the pool G4(EUR/JPY/CAD) + the 13 EM:
    F0  static 50% CAD + 50% G3          (live sleeve B)
    F1  3 lowest-carry in the pool       (pure prior 1)
    F2  3 highest-beta-to-EM in the pool (pure prior 2)
    F3  regime switch: calm->F1, stress->F2  (the user's hypothesis)
    F4  3 lowest 52w-corr to the long leg (pure prior 3)
    F5  bottom-3 of the sleeve-A composite (funding = weakest signals -> RV)
Every funding basket is equal-weighted 1/3 each.
"""
import numpy as np
import pandas as pd

from fx_model import (TRADED, FUNDING, VOL_TARGET, load_clean, zsec,
                      composite, vol_target, legs)

POOL = TRADED + FUNDING


def perf(r, name):
    r = r.dropna()
    cum = r.cumsum()
    return (f'{name:<44} Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}  '
            f'ann {52*r.mean()*100:+.2f}%  maxDD {(cum-cum.cummax()).min()*100:.1f}%')


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    tot = (sr + cr)[POOL]                       # realized-week convention
    carry = T['carry'][POOL]

    # ---- long leg: equal-vol EM basket, weights lagged ----
    iv = (1.0 / T['rvol'][TRADED].clip(lower=1.0)).shift(1)
    wL = iv.div(iv.sum(axis=1), axis=0)
    long_ret = (wL * tot[TRADED]).sum(axis=1, min_count=8)

    # ---- regime: mean ATM vol z, lagged ----
    av = T['atm'][TRADED].mean(axis=1)
    vz = ((av - av.rolling(104, min_periods=40).mean()) /
          av.rolling(104, min_periods=40).std()).shift(1)
    calm = vz < 0

    # ---- rolling beta of every pool ccy to the EM basket, lagged ----
    beta = pd.DataFrame({c: tot[c].rolling(52).cov(long_ret) /
                         long_ret.rolling(52).var() for c in POOL}).shift(1)
    corrL = pd.DataFrame({c: tot[c].rolling(52).corr(long_ret)
                          for c in POOL}).shift(1)

    comp = composite(T)                          # sleeve-A composite (traded only)

    def basket_ret(picks):
        """picks: DataFrame of 0/1 selections per week -> equal-weight return."""
        w = picks.div(picks.sum(axis=1), axis=0)
        return (w * tot[picks.columns]).sum(axis=1, min_count=1)

    def low3(score):
        r = score.rank(axis=1)
        return (r <= 3).astype(float)

    def high3(score):
        r = score.rank(axis=1, ascending=False)
        return (r <= 3).astype(float)

    carry_l = carry.shift(1)                     # carry known at selection time
    picks = {
        'F1 lowest-carry 3': low3(carry_l),
        'F2 highest-beta 3': high3(beta),
        'F4 lowest-corr 3': low3(corrL),
    }
    f3 = picks['F1 lowest-carry 3'].where(calm, picks['F2 highest-beta 3'])
    picks['F3 regime: calm F1 / stress F2'] = f3
    b3 = low3(comp.shift(1))                     # weakest composite = fund them
    picks['F5 bottom-3 composite (RV)'] = b3.reindex(columns=POOL).fillna(0)

    g3 = (tot['EUR'] + tot['JPY']) / 3.0
    f0 = 0.5 * tot['CAD'] + 0.5 * g3

    print('=' * 96)
    print('funding-leg horse race   long = equal-vol 13-EM basket   2013+, '
          'weekly, mid')
    print(f'regime split: calm {100*calm.mean():.0f}% of weeks / stress '
          f'{100*(~calm).mean():.0f}%')
    print('=' * 96)

    results = {}
    rows = [('F0 static 50 CAD + 50 G3  [LIVE]', long_ret - f0)]
    for nm, p in picks.items():
        rows.append((nm, long_ret - basket_ret(p)))
    rows.append(('(no funding: long EM vs USD)', long_ret))
    for nm, r in rows:
        r = (r * vol_target(r)).dropna()
        results[nm] = r
        print(perf(r, nm))

    print()
    print('per-regime Sharpe (same books, split by the lagged vol regime):')
    print(f"{'variant':<44}{'calm':>8}{'stress':>8}")
    for nm, r in results.items():
        rc, rs = r[calm.reindex(r.index).fillna(False)], \
            r[~calm.reindex(r.index).fillna(True)]
        print(f'{nm:<44}{rc.mean()/rc.std()*np.sqrt(52):>+8.2f}'
              f'{rs.mean()/rs.std()*np.sqrt(52):>+8.2f}')

    # what does each rule actually short?
    print()
    print('what each rule shorts (share of weeks each ccy is in the basket):')
    for nm in ['F1 lowest-carry 3', 'F2 highest-beta 3',
               'F3 regime: calm F1 / stress F2']:
        p = picks[nm]
        top = (p.mean().sort_values(ascending=False) * 100).head(6)
        print(f'  {nm:<36} ' + '  '.join(f'{c} {v:.0f}%' for c, v in top.items()))

    pd.DataFrame(results).to_csv('analysis/funding_framework_curves.csv')
    print('\nwritten: analysis/funding_framework_curves.csv')


if __name__ == '__main__':
    main()
