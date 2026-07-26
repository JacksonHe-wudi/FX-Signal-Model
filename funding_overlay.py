"""
G3/CAD funding overlay - design validation on OUR data (2013+, 14 traded ccys).

Key structural fact: the main cross-sectional book is self-funded (long leg +1,
short leg -1, net ~0), so there is no residual USD leg to hedge. The overlay is
therefore a SEPARATE DIRECTIONAL SLEEVE: when the model wants aggregate EM
exposure, fund it with a G3 basket (USD/EUR/JPY) or CAD instead of pure USD
(MS Beyond Carry Ex26: USD funding Sharpe 0.26 maxDD -28.8% vs G3 0.54/-13.6%,
CAD 0.58/-12.3%, 2010-2026, robust across sub-horizons).

Part 1  Funding denominator test (static long EM basket):
        same EM leg, three funding choices -> Sharpe / maxDD on OUR data.
Part 2  Timed sleeve: EM-vs-G3 position switched by the USD-trend regime
        (long EM vs G3 only when DXY < 52w MA), the simplest de-monopolized
        dollar-leg timing per MS's 0.36 dollar-leg evidence.
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
          'BRL', 'MXN', 'CLP', 'PLN', 'HUF']


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def perf(r, name):
    r = r.dropna()
    if len(r) < 50:
        return f'{name:<34} (too short)'
    sh = r.mean() / r.std() * np.sqrt(52)
    cum = r.cumsum()
    dd = (cum - cum.cummax()).min()
    return (f'{name:<34} Sharpe {sh:+.2f}  ann {52*r.mean()*100:+.1f}%  '
            f'vol {np.sqrt(52)*r.std()*100:.1f}%  maxDD {dd*100:.1f}%')


def main():
    spot = load('L1_spot_usd_all30')
    carry = load('L1_carry_1m_ann')
    rvol = load('L1_vol_realized_1m')
    reg = load('L1_regime')

    # weekly total return of being LONG each currency vs USD
    tot = (-np.log(spot).diff()).add((carry / 100 / 52).shift(1), fill_value=np.nan)

    # ---- EM leg: equal-vol basket of the 14 traded (weights lagged) ----
    iv = (1.0 / rvol[TRADED].clip(lower=1.0)).shift(1)
    w = iv.div(iv.sum(axis=1), axis=0)
    em = (w * tot[TRADED]).sum(axis=1, min_count=8)

    # ---- funding legs ----
    g3 = (tot['EUR'] + tot['JPY']) / 3.0          # short 1/3 EUR + 1/3 JPY
    cad = tot['CAD']                              # (1/3 USD = no adjustment)

    print('=' * 86)
    print('PART 1 - same long-EM basket, three funding denominators (2013+, weekly)')
    print('=' * 86)
    print(perf(em, 'vs 100% USD (current default)'))
    print(perf(em - g3, 'vs G3 basket (1/3 USD,EUR,JPY)'))
    print(perf(em - cad, 'vs 100% CAD'))
    print(perf(em - 0.5 * cad - 0.5 * g3, 'vs 50% CAD + 50% G3'))

    # ---- Part 2: timed sleeve on the USD regime state ----
    dxy = reg['DXY']
    usd_down = (dxy < dxy.rolling(52).mean()).shift(1)      # lagged state
    print('\n' + '=' * 86)
    print('PART 2 - timed sleeve: hold only in USD-downtrend weeks '
          f'({100*usd_down.mean():.0f}% of sample)')
    print('=' * 86)
    for nm, leg in [('EM vs USD', em), ('EM vs G3', em - g3), ('EM vs CAD', em - cad)]:
        timed = leg.where(usd_down.reindex(leg.index).fillna(False), 0.0)
        print(perf(timed, f'{nm}, USD-downtrend only'))
    # symmetric long/short version: long EM in USD downtrend, short in uptrend
    for nm, leg in [('EM vs G3', em - g3)]:
        sym = leg.where(usd_down.reindex(leg.index).fillna(False), -leg)
        print(perf(sym, f'{nm}, symmetric long/short'))

    out = pd.DataFrame({'em_usd': em, 'em_g3': em - g3, 'em_cad': em - cad,
                        'usd_down': usd_down.reindex(em.index)})
    out.to_csv(f'{AN}/funding_overlay_curves.csv')
    print(f'\nwritten: {AN}/funding_overlay_curves.csv')


if __name__ == '__main__':
    main()
