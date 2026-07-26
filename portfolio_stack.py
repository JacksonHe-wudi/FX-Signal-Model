"""
Portfolio stack - combine the validated sleeves at equal risk (5% vol each).

  A  concentrated 14-ccy RV book        (sleeve_a_rv.py,   Sharpe 1.24)
  B  EM beta vs 50% CAD + 50% G3        (funding_overlay,  Sharpe 0.66)
  D  CNH fixing-bias signal              (cnh_fixing_signal, Sharpe 1.00, 2018+)

Pairwise correlations ~0 (A-B 0.02, A-D 0.06, B-D -0.05) -> the stack adds
Sharpe almost like independent bets. D contributes only from 2018 (survey
start); before that the combo is effectively A+B.

Run AFTER sleeve_a_rv.py, funding_overlay.py, cnh_fixing_signal.py.
"""
import numpy as np
import pandas as pd

AN = 'analysis'
WEIGHTS = {'A': 0.50, 'B': 0.25, 'D': 0.25}


def perf(r, name):
    r = r.dropna()
    sh = r.mean() / r.std() * np.sqrt(52)
    cum = r.cumsum()
    dd = (cum - cum.cummax()).min()
    return (f'{name:<34} Sharpe {sh:+.2f}  ann {52*r.mean()*100:+.2f}%  '
            f'maxDD {dd*100:.1f}%')


def scale(r, vol=0.05):
    r = r.dropna()
    return r * vol / (r.std() * np.sqrt(52))


def main():
    A = pd.read_csv(f'{AN}/sleeve_a_curves.csv', index_col=0,
                    parse_dates=True)['BASE (top3/5)']
    fo = pd.read_csv(f'{AN}/funding_overlay_curves.csv', index_col=0, parse_dates=True)
    B = 0.5 * fo['em_g3'] + 0.5 * fo['em_cad']
    cn = pd.read_csv(f'{AN}/cnh_fixing_signal.csv', index_col=0, parse_dates=True)
    D = (np.sign(cn['signal']) * cn['tot_next']).where(cn['signal'] != 0, 0.0)

    df = pd.DataFrame({'A': scale(A), 'B': scale(B), 'D': scale(D)})
    print('pairwise corr:', {p: round(df[p[0]].corr(df[p[2]]), 2)
                             for p in ['A-B', 'A-D', 'B-D']})
    for k in df:
        print(perf(df[k], f'sleeve {k} (5% vol)'))
    combo = sum(WEIGHTS[k] * df[k].fillna(0) for k in WEIGHTS)[df['A'].notna()]
    print(perf(combo, f'STACK A{int(100*WEIGHTS["A"])}/'
                      f'B{int(100*WEIGHTS["B"])}/D{int(100*WEIGHTS["D"])}'))
    print('\nsubsamples:')
    for nm, a, b in [('2013-2016', '2013', '2016'), ('2017-2019', '2017', '2019'),
                     ('2020-2022', '2020', '2022'), ('2023-2026', '2023', '2026')]:
        r = combo.loc[a:b].dropna()
        print(f'  {nm}: Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}')
    combo.to_csv(f'{AN}/portfolio_stack_curve.csv')
    print(f'\nwritten: {AN}/portfolio_stack_curve.csv')


if __name__ == '__main__':
    main()
