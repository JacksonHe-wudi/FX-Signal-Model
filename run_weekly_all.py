"""
Weekly execution sheet for the full A+B+D book - one command, all three sleeves.

    python3 run_weekly_all.py

Risk budget: each sleeve is scaled to 5% annualized vol, then weighted
A 50% / B 25% / D 25%. Weights below are % of TOTAL book notional, already
including each sleeve's own vol-target leverage and risk weight.

Sleeve A - cross-sectional RV, 14 currencies
    Instrument : 1W or 1M outright forward / NDF per currency (tenor per the
                 basis rule; see 'tenor' column)
    Direction  : LONG = sell USD/CCY forward; SHORT = buy USD/CCY forward
    Entry/exit : |composite z| > 0.5, long top3 / short bottom3, held while in
                 top5/bottom5 (hysteresis)
    Sizing     : 1/ATM-vol within each leg; legs sum to +-1 (self-funded)
    Roll       : weekly on the Friday grid (1W) or at point-to-point maturity (1M)

Sleeve B - EM beta versus a funding basket
    Instrument : long an equal-vol basket of the 14 EM currencies (1M forwards)
                 funded 50% CAD + 50% G3 (1/3 USD, 1/3 EUR, 1/3 JPY)
    Direction  : structural long EM / short the funding basket - passive, always
                 on (DXY-trend timing was tested and does NOT add)
    Roll       : monthly, all legs together

Sleeve D - CNH fixing bias
    Instrument : USD/CNH 1W forward, single leg
    Direction  : +sign(CCF), i.e. trade WITH the pressure the fix reveals
    Gates      : only when the fix leans against the 4-week spot trend AND 1M
                 ATM vol z < 1.5; otherwise flat
    Roll       : weekly
"""
import glob
import os

import numpy as np
import pandas as pd

from sleeve_a_rv import load, zsec, build_book, TRADED, VOL_TARGET
import data_pipeline_v2 as dp

W_A, W_B, W_D = 0.50, 0.25, 0.25
FUND = {'CAD': 0.50, 'USD': 0.5 / 3, 'EUR': 0.5 / 3, 'JPY': 0.5 / 3}


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')
    atm = load('L1_vol_implied_atm_1m')
    rvol = load('L1_vol_realized_1m')
    basis = load('L2_fwdpts_basis_1w_1m')

    # ---------- completeness gate ----------
    L1, L2 = {}, {}
    for f in glob.glob('data/clean/clean_csv/*.csv'):
        n = os.path.basename(f)[:-4]
        (L1 if n.startswith('L1_') else L2)[n.split('_', 1)[1]] = \
            pd.read_csv(f, index_col=0, parse_dates=True)
    asof, gate = dp.completeness_report(L1, L2)
    ready = gate['READY'] == 'YES'
    blocked = [c for c in TRADED if not ready.get(c, False)]

    # ---------- sleeve A ----------
    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    num = 2 * zc.fillna(0) + ze.reindex(zc.index).fillna(0)
    den = 2 * zc.notna() + 1 * ze.reindex(zc.index).notna()
    comp = zsec((num / den.replace(0, np.nan)).rolling(3).mean())
    if blocked:
        comp[blocked] = np.nan
    tot_next = (-np.log(spot).diff() + (carry[spot.columns] / 100 / 52).shift(1)).shift(-1)
    wgt = build_book(comp, atm[spot.columns])
    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
         disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)
    raw = (wgt * tot_next.reindex(wgt.index)).sum(axis=1) * mult
    lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
    t = wgt.index[wgt.abs().sum(axis=1) > 0][-1]
    scale_a = mult.loc[t] * (lev.loc[t] if pd.notna(lev.loc[t]) else 1.0)

    active = wgt != 0
    tenure = {}
    for c in wgt.columns:
        s = active[c].loc[:t][::-1]
        tenure[c] = int(s.cummin().sum()) if len(s) and s.iloc[0] else 0

    print(f'{"="*78}\nWEEKLY EXECUTION SHEET   as of {t.date()}\n{"="*78}')
    print(f'gate: {int(ready.sum())}/14 ready' + (f'  BLOCKED {blocked}' if blocked else ''))
    print(f'\n--- SLEEVE A  (RV book, {W_A:.0%} of risk)  '
          f'dispersion x{mult.loc[t]:.2f}, vol-target x{lev.loc[t]:.2f} ---')
    print(f"{'ccy':<5}{'z':>7}{'side':>7}{'wt%':>8}{'wks':>5}{'basis':>8}{'tenor':>7}")
    rows = []
    for c in TRADED:
        w = wgt.loc[t, c] * scale_a * W_A
        if w == 0:
            continue
        b = basis.loc[:t, c].dropna().iloc[-1] if c in basis.columns else np.nan
        fav1w = (b > 0) if w > 0 else (b < 0)
        ten = '1M' if tenure[c] >= 4 else ('1W' if (pd.isna(b) or fav1w) else '1M')
        side = 'LONG' if w > 0 else 'SHORT'
        rows.append([c, comp.loc[t, c], side, 100 * w, tenure[c], b, ten])
        print(f'{c:<5}{comp.loc[t,c]:>+7.2f}{side:>7}{100*w:>+8.1f}{tenure[c]:>5}'
              f'{b:>8.0f}{ten:>7}')
    gross_a = sum(abs(r[3]) for r in rows)
    print(f'      gross {gross_a:.1f}% of book   (LONG = sell USD/CCY fwd)')

    # ---------- sleeve B ----------
    iv = (1.0 / rvol[TRADED].clip(lower=1.0)).loc[t]
    wb = (iv / iv.sum()).dropna()
    volB = ((wb * (-np.log(spot[TRADED]).diff())).sum(axis=1)).rolling(52).std() * np.sqrt(52)
    levB = min(VOL_TARGET / volB.loc[t], 3.0) if volB.loc[t] > 0 else 1.0
    print(f'\n--- SLEEVE B  (EM beta vs funding basket, {W_B:.0%} of risk)  '
          f'vol-target x{levB:.2f} ---')
    print('  LONG  EM basket (equal-vol, 1M forwards):')
    print('   ', '  '.join(f'{c} {100*wb[c]*levB*W_B:+.1f}%' for c in TRADED if c in wb))
    print('  SHORT funding basket:')
    print('   ', '  '.join(f'{k} {-100*v*levB*W_B:+.1f}%' for k, v in FUND.items()))

    # ---------- sleeve D ----------
    cf = load('L1_country_factors')
    ccf = ((cf['CNY_FIX'] - cf['BBG_CNY_FIX']) / cf['CNY_FIX'] * 1e4).dropna()
    trend = np.log(spot['CNH']).diff(4)
    az = load('L1_vol_implied_atm_1m')['CNH']
    vz = (az - az.rolling(104, min_periods=40).mean()) / az.rolling(104, min_periods=40).std()
    td = ccf.index[ccf.index <= t][-1]
    c_bp, tr, v = ccf.loc[td], trend.reindex([td]).iloc[0], vz.reindex([td]).iloc[0]
    ok_ctr, ok_calm = (c_bp * tr) < 0, (v < 1.5)
    sig_d = np.sign(c_bp) if (ok_ctr and ok_calm) else 0.0
    volD = (spot['CNH'].pct_change().rolling(52).std() * np.sqrt(52)).loc[:t].iloc[-1]
    levD = min(VOL_TARGET / volD, 3.0) if volD > 0 else 1.0
    print(f'\n--- SLEEVE D  (CNH fixing bias, {W_D:.0%} of risk) ---')
    print(f'  CCF {c_bp:+.1f}bp   counter-trend {"OK" if ok_ctr else "NO"}   '
          f'calm(volz {v:+.2f}) {"OK" if ok_calm else "NO"}')
    if sig_d == 0:
        print('  -> FLAT (a gate is closed)')
    else:
        print(f'  -> {"LONG" if sig_d > 0 else "SHORT"} CNH  '
              f'{100*sig_d*levD*W_D:+.1f}% of book  (1W forward)')

    print(f'\n{"="*78}\nRoll calendar: A per-name tenor above; B monthly all legs; '
          f'D weekly.\nAll sizes are % of total book notional at 5% vol target '
          f'per sleeve.\n{"="*78}')


if __name__ == '__main__':
    main()
