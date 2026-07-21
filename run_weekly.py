"""
One-command weekly run: workbook in -> trade signal report out.

    python run_weekly.py data/raw/FX_Model.xlsx            # full run
    python run_weekly.py data/raw/FX_Model.xlsx --fast     # reuse eps/technical

Steps
-----
 1. data_pipeline.py       xlsx -> data/clean/clean_csv (44 tables)
 2. layer1_fair_value.py   recursive fair value -> eps_recursive.csv   (skipped with --fast)
 3. technical_bollinger.py walk-forward band signal                    (skipped with --fast)
 4. layer3_portfolio.py    adaptive composite (mid execution)
 5. final sizing = conviction book (|z|>0.5, top3 in / top5 stay)
                   x 1/ATM-vol weights
                   x dispersion timing multiplier (0.5-1.5)
                   x 5% vol-target leverage
    -> analysis/weekly_signal.csv + analysis/weekly_report.md
"""
import os
import subprocess
import sys

import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
VOL_TARGET = 0.05


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def sh(cmd, env_extra=None):
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    print(f'>> {" ".join(cmd)}')
    r = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        raise SystemExit(f'step failed: {cmd}')


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else 'data/raw/FX_Model.xlsx'
    fast = '--fast' in sys.argv

    sh(['python3', 'data_pipeline.py', xlsx, 'data/clean'])
    if not fast:
        sh(['python3', 'layer1_fair_value.py'])
        sh(['python3', 'technical_bollinger.py'])
    sh(['python3', 'layer3_portfolio.py'], {'COST_SCALE': '0', 'ADJ_SPEED': '1'})

    # ---------------- final sizing ----------------
    comp = load('composite_adaptive', AN)
    spot = load('L1_spot_usd_asia9')
    carry = load('L1_carry_1m_ann')
    atm = load('L1_vol_implied_atm_1m')
    eps = load('eps_recursive', AN)
    pw = load('pillar_weights', AN)
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)

    dates = comp.index
    prevL, prevS = set(), set()
    wgt = pd.DataFrame(0.0, index=dates, columns=ASIA9)
    for t in dates:
        s = comp.loc[t].dropna()
        s = s[s.abs() > 0.5]
        if len(s) < 2:
            prevL, prevS = set(), set()
            continue
        rd, ra = s.rank(ascending=False), s.rank(ascending=True)
        L = set(s.index[rd <= 3]) | {c for c in prevL if c in s.index and rd[c] <= 5}
        S = set(s.index[ra <= 3]) | {c for c in prevS if c in s.index and ra[c] <= 5}
        L, S = L - S, S - L
        prevL, prevS = L, S
        iv = 1 / atm.loc[t].reindex(list(L | S)).replace(0, np.nan)
        iv = iv.fillna(iv.mean() if pd.notna(iv.mean()) else 1.0)
        for side, idx in [(1, list(L)), (-1, list(S))]:
            if idx:
                v = iv.reindex(idx)
                wgt.loc[t, idx] = side * v / v.sum()

    # dispersion timing multiplier (recursive)
    disp = comp.std(axis=1)
    disp_z = (disp - disp.rolling(104, min_periods=40).mean()) / \
             disp.rolling(104, min_periods=40).std()
    mult = disp_z.clip(-1, 1) * 0.5 + 1.0

    # vol-target leverage from the strategy's own realized vol
    strat = (wgt * tot_next.reindex(dates)).sum(axis=1) * mult
    lev = (VOL_TARGET / (strat.rolling(52).std() * np.sqrt(52))).clip(upper=3)

    t = dates[-1]
    final_w = wgt.loc[t] * mult.loc[t] * (lev.loc[t] if pd.notna(lev.loc[t]) else 1.0)

    rows = []
    for c in ASIA9:
        z = comp.loc[t, c] if c in comp.columns else np.nan
        act = '多 LONG' if final_w[c] > 0 else ('空 SHORT' if final_w[c] < 0 else '—')
        rows.append([c, round(z, 2) if pd.notna(z) else None, act,
                     f'{final_w[c]*100:+.1f}%' if final_w[c] != 0 else '',
                     round(carry.loc[:t, c].dropna().iloc[-1], 2) if c in carry else None,
                     round(atm.loc[:t, c].dropna().iloc[-1], 2) if c in atm else None,
                     f'{eps.loc[:t, c].dropna().iloc[-1]*100:+.1f}%'
                     if c in eps and len(eps.loc[:t, c].dropna()) else None])
    tab = pd.DataFrame(rows, columns=['currency', 'composite_z', 'action',
                                      'weight_pct_of_book', 'carry_ann_pct',
                                      'atm_vol_1m', 'misvaluation_eps'])
    tab.to_csv(f'{AN}/weekly_signal.csv', index=False)

    with open(f'{AN}/weekly_report.md', 'w') as f:
        f.write(f'# Weekly FX Signal - {t.date()}\n\n')
        f.write(f'- pillar weights: Fundamental {pw.loc[t, "Fundamental"]:.0%} / '
                f'Momentum {pw.loc[t, "Momentum"]:.0%} / '
                f'Technical {pw.loc[t, "Technical"]:.0%}\n')
        f.write(f'- dispersion multiplier: {mult.loc[t]:.2f}x   '
                f'vol-target leverage: {lev.loc[t]:.2f}x\n')
        f.write(f'- execution: 1W forward/NDF roll at mid, weekly Friday rebalance\n\n')
        f.write(tab.to_markdown(index=False))
        f.write('\n\nPositive weight = long local currency (short USD/XXX 1W forward).\n')

    print(f'\n===== WEEKLY SIGNAL {t.date()} =====')
    print(tab.to_string(index=False))
    print(f'\ndispersion x{mult.loc[t]:.2f}, leverage x{lev.loc[t]:.2f}, '
          f'pillars F{pw.loc[t, "Fundamental"]:.0%}/M{pw.loc[t, "Momentum"]:.0%}/'
          f'T{pw.loc[t, "Technical"]:.0%}')
    print(f'written: {AN}/weekly_signal.csv, {AN}/weekly_report.md')


if __name__ == '__main__':
    main()
