"""
IDEA 2 - portfolio blending instead of signal blending for sleeve A
(Ghayur, Heaney & Platt, "Constructing Long-Only Multifactor Strategies:
Portfolio Blending vs. Signal Blending", Financial Analysts Journal 2018).

Their result: at LOW-to-MODERATE factor exposure, blending separately-built
factor PORTFOLIOS beats blending factor SIGNALS into one composite. The reason
is interaction effects - names with offsetting exposures are held in the
portfolio blend and cut active risk, an effect a composite signal destroys by
averaging the exposures away before the portfolio is formed.

Why this is our case exactly. factor_orthogonal.py showed six factors keep real
IC after carry is projected out (ESI t2.70, real_carry t2.50, CA t2.27, CDS
t1.94, real_yield t1.93, REER t1.84), yet blending any of them into the 2:1
composite lowers composite IC. That is a blending failure, not a redundancy
failure - the exact situation the paper describes.

Here: build the SAME book machinery (|z|>0.5, top3-in/top5-stay hysteresis,
1/ATM-vol legs, 5% vol target) once per factor, then combine the return streams
at equal risk. Compared against the live signal-blended composite.

Note on breadth: CDS covers only 7 of 14 names, so its book is structurally
concentrated; it is reported separately as well as inside the blends.
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import TRADED, VOL_TARGET, load, zsec, build_book, perf

MIS_WIN = 260


def factor_set():
    reer = load('L1_reer').reindex(columns=TRADED)
    return {
        'carry/realvol': load('L2_carry_to_realvol'),
        'ESI 4w chg': load('L2_esi_chg_4w'),
        'real_carry': load('L2_real_carry_1m'),
        'real_yield_12m': load('L2_real_yield_12m'),
        'CA YoY': load('L2_ca_yoy_usdbn'),
        'REER (neg)': -reer,
        'CDS 4w chg (neg)': -load('L2_cds_chg_4w'),
    }


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    tot = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)
    F = {k: v.reindex(columns=TRADED) for k, v in factor_set().items()}

    def book(sig):
        """One factor -> one book -> vol-targeted return stream."""
        z = zsec(zsec(sig).rolling(3).mean())
        w = build_book(z, atm)
        raw = (w * tot.reindex(w.index)).sum(axis=1)
        lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
        return (raw * lev).dropna()

    print('=' * 96)
    print('STEP 1 - one book per factor (same machinery, 5% vol target each)')
    print('=' * 96)
    books = {}
    for k, v in F.items():
        r = book(v)
        books[k] = r
        print(perf(r, k))

    B = pd.DataFrame(books)
    print('\ncorrelation between the single-factor books:')
    print(B.corr().round(2).to_string())

    def sc(r, vol=0.05):
        r = r.dropna()
        return r * vol / (r.std() * np.sqrt(52))

    Bs = pd.DataFrame({k: sc(v) for k, v in books.items()})

    print()
    print('=' * 96)
    print('STEP 2 - portfolio blend vs the live signal blend')
    print('=' * 96)
    live = pd.read_csv('analysis/sleeve_a_curves.csv', index_col=0,
                       parse_dates=True)['BASE (top3/5)'].dropna()
    print(perf(live, 'LIVE signal blend 2:1 carry/ESI'))
    print(perf(sc(books['carry/realvol']), 'carry-only book (reference)'))

    blends = {
        'PB all 7 equal': list(F),
        'PB 6 (drop CDS, 7ccy only)': [k for k in F if not k.startswith('CDS')],
        'PB carry+ESI': ['carry/realvol', 'ESI 4w chg'],
        'PB carry+ESI+real_carry': ['carry/realvol', 'ESI 4w chg', 'real_carry'],
        'PB carry+ESI+CA': ['carry/realvol', 'ESI 4w chg', 'CA YoY'],
        'PB carry+ESI+real_carry+CA': ['carry/realvol', 'ESI 4w chg',
                                       'real_carry', 'CA YoY'],
        'PB carry 50% + rest 50%': None,
    }
    curves = {'LIVE signal blend': live}
    for nm, ks in blends.items():
        if ks is None:
            others = [k for k in F if k != 'carry/realvol']
            r = 0.5 * Bs['carry/realvol'].fillna(0) + \
                0.5 * Bs[others].fillna(0).mean(axis=1)
        else:
            r = Bs[ks].fillna(0).mean(axis=1)
        r = r[Bs['carry/realvol'].notna()].dropna()
        curves[nm] = r
        print(perf(r, nm))

    print()
    print('=' * 96)
    print('STEP 3 - subsample stability')
    print('=' * 96)
    print(f"{'variant':<30}" + ''.join(f'{p:>11}' for p in
                                       ['13-16', '17-19', '20-22', '23-26']))
    for nm, r in curves.items():
        row = f'{nm:<30}'
        for a, b in [('2013', '2016'), ('2017', '2019'),
                     ('2020', '2022'), ('2023', '2026')]:
            s = r.loc[a:b].dropna()
            row += f'{s.mean()/s.std()*np.sqrt(52):>+11.2f}' if len(s) > 30 else f'{"-":>11}'
        print(row)

    pd.DataFrame(curves).to_csv('analysis/portfolio_blend_curves.csv')
    print('\nwritten: analysis/portfolio_blend_curves.csv')


if __name__ == '__main__':
    main()
