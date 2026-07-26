"""
Portfolio stack - combine the validated sleeves at equal risk (5% vol each).

  A  concentrated 14-ccy cross-sectional RV book   (sleeve_a_rv.py)
  B  EM beta funded 50% CAD + 50% G3               (funding_overlay.py)
  C  fade the 1M forward-points move (funding RV)  (sleeve_c_points.py)
  D  CNH PBOC fixing-bias signal                   (cnh_fixing_signal.py)

A bets on relative currency strength, B on EM beta versus a funding basket,
C on the funding market itself, D on one central bank's fixing. Pairwise
correlations are near zero, which is what makes the stack add Sharpe.

Sample notes: A/B run 2013+, C 2013+ (daily forwards now reach back), D only
from 2018-06 (the Bloomberg fixing survey starts then). Sleeves are equal-risk
scaled, then weighted; missing history is treated as flat (0), so early years
are effectively A+B+C.

Run AFTER: sleeve_a_rv.py, funding_overlay.py, sleeve_c_points.py,
cnh_fixing_signal.py.
"""
import numpy as np
import pandas as pd

AN = 'analysis'
WEIGHTS = {'A': 0.40, 'B': 0.20, 'C': 0.20, 'D': 0.20}


def perf(r, name):
    r = r.dropna()
    sh = r.mean() / r.std() * np.sqrt(52)
    cum = r.cumsum()
    dd = (cum - cum.cummax()).min()
    return (f'{name:<40} Sharpe {sh:+.2f}  ann {52*r.mean()*100:+.2f}%  '
            f'maxDD {dd*100:.1f}%  n={len(r)}')


def scale(r, vol=0.05):
    r = r.dropna()
    return r * vol / (r.std() * np.sqrt(52))


def sleeves():
    A = pd.read_csv(f'{AN}/sleeve_a_curves.csv', index_col=0,
                    parse_dates=True)['BASE (top3/5)']
    fo = pd.read_csv(f'{AN}/funding_overlay_curves.csv', index_col=0, parse_dates=True)
    B = 0.5 * fo['em_g3'] + 0.5 * fo['em_cad']
    # C: monthly-cycle P&L in bp -> weekly series
    c_raw = pd.read_csv(f'{AN}/sleeve_c_curve.csv', index_col=0, parse_dates=True)
    C = (c_raw['ret_bp'] / 1e4).resample('W-FRI').sum()
    cn = pd.read_csv(f'{AN}/cnh_fixing_signal.csv', index_col=0, parse_dates=True)
    D = (np.sign(cn['signal']) * cn['tot_next']).where(cn['signal'] != 0, 0.0)
    return {'A': A, 'B': B, 'C': C, 'D': D}


def main():
    raw = sleeves()
    df = pd.DataFrame({k: scale(v) for k, v in raw.items()})
    print('coverage:', {k: f'{v.dropna().index.min().date()}..'
                           f'{v.dropna().index.max().date()}' for k, v in raw.items()})
    print('\npairwise correlations:')
    print(df.corr().round(2).to_string())
    print()
    for k in df:
        print(perf(df[k], f'sleeve {k} (5% vol)'))

    combo = sum(WEIGHTS[k] * df[k].fillna(0) for k in WEIGHTS)[df['A'].notna()]
    lbl = '/'.join(f'{k}{int(100*w)}' for k, w in WEIGHTS.items())
    print()
    print(perf(combo, f'STACK {lbl}'))
    eq = df.fillna(0).mean(axis=1)[df['A'].notna()]
    print(perf(eq, 'STACK equal-weight (robustness)'))
    abd = (0.5 * df['A'].fillna(0) + 0.25 * df['B'].fillna(0)
           + 0.25 * df['D'].fillna(0))[df['A'].notna()]
    print(perf(abd, 'STACK A50/B25/D25 (previous, no C)'))

    print('\nsubsamples (main stack):')
    for nm, a, b in [('2013-2016', '2013', '2016'), ('2017-2019', '2017', '2019'),
                     ('2020-2022', '2020', '2022'), ('2023-2026', '2023', '2026')]:
        r = combo.loc[a:b].dropna()
        if len(r) > 30:
            print(f'  {nm}: Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}  '
                  f'ann {52*r.mean()*100:+.2f}%')
    combo.to_csv(f'{AN}/portfolio_stack_curve.csv')
    print(f'\nwritten: {AN}/portfolio_stack_curve.csv')


if __name__ == '__main__':
    main()
