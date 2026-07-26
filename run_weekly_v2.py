"""
ONE-COMMAND weekly run, v2 - the concentrated 14-currency RV engine.

    python3 run_weekly_v2.py                    # uses data/clean (run pipeline first)

Chain:
  1. completeness gate (per-currency, disciplined heterogeneity)
  2. sleeve A composite (2 x carry/realvol + 1 x ESI chg, 3w smooth) ->
     conviction book (|z|>0.5, top3/5 hysteresis, 1/ATM-vol legs)
  3. dispersion-timing multiplier + 5% vol-target leverage
  4. ROLL-TENOR recommendation per active position (validated persistence:
     the currently-cheaper tenor keeps its edge over the roll horizon):
       long  CCY (selling USD/CCY fwd, collecting points):
             roll 1W if 52*pts_1w > 12*pts_1m (basis>0) else lock 1M
       short CCY (buying USD/CCY fwd, paying points):
             roll 1W if basis < 0 else lock 1M
  5. turnover + cost sensitivity (mid is the base case per user instruction)
Outputs: analysis/weekly_signal_v2.csv + printed report.
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import (load, zsec, build_book, TRADED, VOL_TARGET)
import data_pipeline_v2 as dp


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)

    # ---- completeness gate ----
    import glob, os
    L1, L2 = {}, {}
    for f in glob.glob('data/clean/clean_csv/*.csv'):
        n = os.path.basename(f)[:-4]
        df = pd.read_csv(f, index_col=0, parse_dates=True)
        (L1 if n.startswith('L1_') else L2)[n.split('_', 1)[1]] = df
    asof, gate = dp.completeness_report(L1, L2)
    ready = gate['READY'] == 'YES'
    blocked = [c for c in TRADED if not ready.get(c, False)]

    # ---- sleeve A composite ----
    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    num = 2.0 * zc.fillna(0) + ze.reindex(zc.index).fillna(0)
    den = 2.0 * zc.notna() + 1.0 * ze.reindex(zc.index).notna()
    comp = zsec((num / den.replace(0, np.nan)).rolling(3).mean())
    if blocked:                                   # gate: no signal without inputs
        comp[blocked] = np.nan

    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
         disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)

    wgt = build_book(comp, atm)
    raw = (wgt * tot_next.reindex(wgt.index)).sum(axis=1) * mult
    lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
    net = raw * lev

    # ---- turnover + cost sensitivity ----
    turn = wgt.diff().abs().sum(axis=1)
    print('cost sensitivity (one-way bp on turnover):')
    for bp in [0, 2, 5]:
        r = (net - (turn * lev * bp / 1e4)).dropna()
        print(f'  {bp}bp: Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}  '
              f'(avg weekly turnover {turn.mean():.2f}x)')

    # ---- latest signal + roll tenor ----
    t = wgt.index[wgt.abs().sum(axis=1) > 0][-1]
    basis = load('L2_fwdpts_basis_1w_1m')
    rows = []
    for c in TRADED:
        w = wgt.loc[t, c] * mult.loc[t] * (lev.loc[t] if pd.notna(lev.loc[t]) else 1.0)
        z = comp.loc[t, c] if c in comp.columns else np.nan
        b = basis.loc[:t, c].dropna().iloc[-1] if c in basis.columns else np.nan
        if w > 0:
            act, tenor = '多 LONG', ('1W roll' if b > 0 else '锁 1M')
        elif w < 0:
            act, tenor = '空 SHORT', ('1W roll' if b < 0 else '锁 1M')
        else:
            act, tenor = '—', ''
        rows.append([c, round(z, 2) if pd.notna(z) else None, act,
                     f'{w*100:+.1f}%' if w != 0 else '',
                     round(b, 0) if pd.notna(b) else None, tenor,
                     'READY' if ready.get(c, False) else 'BLOCKED'])
    tab = pd.DataFrame(rows, columns=['ccy', 'z', 'action', 'weight',
                                      'basis_ann_pips', 'roll_tenor', 'gate'])
    tab.to_csv('analysis/weekly_signal_v2.csv', index=False)

    print(f'\n===== WEEKLY SIGNAL v2  {t.date()}  '
          f'(dispersion x{mult.loc[t]:.2f}, leverage x{lev.loc[t]:.2f}) =====')
    print(tab.to_string(index=False))
    if blocked:
        print(f'\nBLOCKED currencies (missing inputs): {blocked}')
    print('\nwritten: analysis/weekly_signal_v2.csv')


if __name__ == '__main__':
    main()
