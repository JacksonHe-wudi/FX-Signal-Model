"""
CNH-specific PBOC fixing-bias (CCF) signal - build + validation.

Framework: docs/ccf_fixing_bias_framework.md   (see its Empirical Verdict)

KEY EMPIRICAL FINDING (2018-06..2026, 425 wks): the fixing bias is a
REVEALED-PRESSURE indicator, not a follow-the-central-bank indicator.
PBOC leans against the wind; at the weekly horizon the wind mostly wins:

  CCF > 0 (fix weaker than survey = PBOC capping appreciation)
        -> underlying appreciation pressure -> CNH still rises  -> LONG
  CCF < 0 (fix stronger than survey = PBOC fighting depreciation)
        -> underlying depreciation pressure -> CNH still falls -> SHORT

The follow-the-PBOC sign (-sign(ccf)) loses money at t=-2.35; the
with-the-pressure sign (+sign(ccf)) with counter-trend + calm gates earns
+10.6bp/wk on trigger weeks, hit 56%, t=+2.86, positive in 8 of 9 years.
Willer's "market obliges" applies to day-scale turning points; on weekly
totals the revealed-pressure reading dominates - exactly the user's own
description ("CCF positive = strong appreciation expectation exists; the
PBOC only slows the pace").

Final spec:
  signal = +sign(ccf_bp)                     (WITH the revealed pressure)
           x 1{ccf_bp x trend_4w < 0}        (counter-trend: fix genuinely
                                              leaning against spot trend)
           x 1{vol_z < 1.5}                  (calm regime; stress weeks show
                                              no edge -> stand aside)
  (10bp magnitude gate tested and DROPPED: weekly sampling is already coarse
   and the gate only removed profitable weeks.)
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
TH_VOL = 1.5          # calm-regime gate on 1M ATM vol z-score
TREND_W = 4           # weeks of spot trend for the counter-trend check


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def main():
    cf = load('L1_country_factors')
    spot = load('L1_spot_usd_traded')['CNH']
    carry = load('L1_carry_1m_ann')['CNH']
    atm = load('L1_vol_implied_atm_1m')['CNH']

    fix, survey = cf['CNY_FIX'], cf['BBG_CNY_FIX']
    ccf_bp = ((fix - survey) / fix * 1e4).dropna()

    # next-week total return of long CNH (short USD/CNH): -dlog(spot) + carry/52
    dlog = np.log(spot).diff()
    tot_next = (-dlog + (carry / 100 / 52).shift(1)).shift(-1)

    idx = ccf_bp.index.intersection(tot_next.dropna().index)
    ccf_bp = ccf_bp.reindex(idx)
    trend = np.log(spot).diff(TREND_W).reindex(idx)          # + = CNH weakening
    vol_z = ((atm - atm.rolling(104, min_periods=40).mean())
             / atm.rolling(104, min_periods=40).std()).reindex(idx)
    y = tot_next.reindex(idx)

    g_ctr = (ccf_bp * trend) < 0            # fix leaning against the spot trend
    g_calm = vol_z < TH_VOL
    g_mag = ccf_bp.abs() >= 10.0            # ablation only

    sig = np.sign(ccf_bp) * g_ctr * g_calm  # FINAL: with the revealed pressure

    def stats(s, name):
        r = (np.sign(s) * y)[s != 0].dropna()
        if len(r) < 8:
            return f'{name:<34} n={len(r):>3}  (too few)'
        return (f'{name:<34} n={len(r):>3}  hit={100*(r>0).mean():.0f}%  '
                f'mean/wk={1e4*r.mean():+.1f}bp  t={r.mean()/r.std()*np.sqrt(len(r)):+.2f}')

    print(f'CCF fixing-bias signal, CNH, {idx.min().date()} -> {idx.max().date()} '
          f'({len(idx)} wks, trigger rate {100*(sig!=0).mean():.0f}%)\n')
    print(stats(sig, 'FINAL  +sign(ccf) x ctr x calm'))
    print(stats(np.sign(ccf_bp), 'raw +sign(ccf), all weeks'))
    print(stats(np.sign(ccf_bp) * g_calm, 'ablation: calm gate only'))
    print(stats(np.sign(ccf_bp) * g_mag * g_ctr * g_calm, 'ablation: + 10bp magnitude gate'))
    print(stats(-np.sign(ccf_bp), 'follow-the-PBOC (WRONG sign)'))
    print(stats(np.sign(ccf_bp) * g_ctr * (~g_calm), 'stress weeks only (gated out)'))

    r = (np.sign(sig) * y)[sig != 0].dropna()
    print('\nfinal spec by year:')
    for yr, g in r.groupby(r.index.year):
        print(f'  {yr}: n={len(g):>2}  hit={100*(g>0).mean():.0f}%  mean={1e4*g.mean():+.1f}bp')

    out = pd.DataFrame({'ccf_bp': ccf_bp, 'trend_4w': trend, 'vol_z': vol_z,
                        'signal': sig, 'tot_next': y})
    out.to_csv(f'{AN}/cnh_fixing_signal.csv')
    print(f'\nwritten: {AN}/cnh_fixing_signal.csv')


if __name__ == '__main__':
    main()
