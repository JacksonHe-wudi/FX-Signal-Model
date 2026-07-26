"""
Execution mix test: how to trade the book - 1W, 1M, or a combination.

Variants (same sleeve-A signal, mid + cost regimes):
  V0 all-1W weekly     : current default
  V1 all-1M monthly    : weights frozen 4 weeks, one spread per month
  V2 all-1W biweekly   : weekly signal, trade every 2nd week
  V3 CORE-SATELLITE    : names in the book >= 4 consecutive weeks (core, ~66%
                         of notional - the carry book is persistent) held via
                         1M forwards refreshed on a monthly cycle; new/marginal
                         names via 1W rolls, adjusted weekly.
Cost model: each week 1W notional pays the one-way spread once (roll), 1M
notional pays 1/4 of it (amortized monthly roll).

Result (2013-2026):
              mid    2bp    5bp   turnover
  V0          1.24   0.79   0.11   0.80x
  V1          1.01   0.90   0.73   0.45x
  V2          1.29   1.06   0.72   0.63x
  V3 hybrid   1.29   1.06   0.73   0.67x   <- best or tied-best everywhere
"""
import numpy as np
import pandas as pd
from sleeve_a_rv import load, zsec, build_book, TRADED, VOL_TARGET


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)
    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    num = 2 * zc.fillna(0) + ze.reindex(zc.index).fillna(0)
    den = 2 * zc.notna() + 1 * ze.reindex(zc.index).notna()
    comp = zsec((num / den.replace(0, np.nan)).rolling(3).mean())
    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
         disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)
    W = build_book(comp, atm)
    idx, cols = W.index, W.columns

    tenure = pd.DataFrame(0, index=idx, columns=cols)
    for i in range(1, len(idx)):
        prev = tenure.iloc[i - 1]
        tenure.iloc[i] = np.where(W.iloc[i] != 0,
                                  np.where(W.iloc[i - 1] != 0, prev + 1, 1), 0)
    core = tenure >= 4

    Wh = W.copy()
    is1m = pd.DataFrame(False, index=idx, columns=cols)
    for i in range(1, len(idx)):
        for c in cols:
            if core.iloc[i][c] and W.iloc[i][c] != 0:
                is1m.iloc[i, is1m.columns.get_loc(c)] = True
                if i % 4 != 0 and Wh.iloc[i - 1][c] != 0:
                    Wh.iloc[i, Wh.columns.get_loc(c)] = Wh.iloc[i - 1][c]

    def run(wgt, label, roll_frac):
        raw = (wgt * tot_next.reindex(idx)).sum(axis=1) * mult
        lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
        net = raw * lev
        line = f'{label:<36}'
        for bp in [0, 2, 5]:
            cost = (roll_frac * lev * bp / 1e4 if isinstance(roll_frac, pd.Series)
                    else wgt.abs().sum(axis=1) * roll_frac * lev * bp / 1e4)
            r = (net - cost).dropna()
            line += f'  {bp}bp {r.mean()/r.std()*np.sqrt(52):+.2f}'
        print(line + f'   turn {wgt.diff().abs().sum(axis=1).mean():.2f}x')

    run(W, 'V0 all-1W weekly', 1.0)
    Wm = W.copy()
    for i in range(len(idx)):
        if i % 4 != 0:
            Wm.iloc[i] = Wm.iloc[i - 1]
    run(Wm, 'V1 all-1M monthly', 0.25)
    W2 = W.copy()
    for i in range(len(idx)):
        if i % 2 != 0:
            W2.iloc[i] = W2.iloc[i - 1]
    run(W2, 'V2 all-1W biweekly', 0.5)
    g1w = (Wh.abs() * (~is1m)).sum(axis=1)
    g1m = (Wh.abs() * is1m).sum(axis=1)
    run(Wh, 'V3 hybrid core->1M / new->1W', g1w * 1.0 + g1m * 0.25)
    print(f'V3 core(1M) share of notional: {(g1m/(g1w+g1m)).mean():.0%}')


if __name__ == '__main__':
    main()
