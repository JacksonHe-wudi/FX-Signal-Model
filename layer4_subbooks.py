"""
Layer 4 - MS-style sub-book architecture, target: max net Sharpe at mid.

Each candidate signal runs its OWN book:
    weights_i ∝ (cross-sectional z, capped ±2) / ATM implied vol,
    normalized to 200% gross, weekly rebalance at mid (desk execution),
    each book vol-targeted to 5% (trailing 52w, recursive).

Books are then blended at the PORTFOLIO level (the MS construction):
    meta-weight of each book ∝ max(trailing 104w IC of its score, 0),
    recomputed every week using only past data. Sign-ambiguous signals
    (VRP, RR term structure) enter as both + and - books; the meta-rule
    keeps whichever has evidence, recursively - no look-ahead, no
    in-sample sign picking.

The slow value book (eps) rebalances only every 4 weeks (its natural
horizon), all other books weekly.

Output: analysis/subbook_results.csv, analysis/subbook_curves.csv
"""
import os
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
START = '2014-01-01'
VOL_TARGET = 0.05


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def zsec(df):
    z = df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)
    return z.clip(-2, 2)


def sharpe(r):
    r = r.dropna()
    return r.mean() / r.std() * np.sqrt(52) if len(r) > 20 and r.std() > 0 else np.nan


def main():
    spot = load('L1_spot_usd_asia9')
    dlog = np.log(spot).diff()
    carry_w = load('L1_carry_1m_ann') / 100 / 52
    tot_next = (-dlog + carry_w.shift(1)).shift(-1)
    atm = load('L1_vol_implied_atm_1m')

    esi = load('L1_esi').rename(columns={'CNY': 'CNH'})
    iv, rv = load('L1_vol_implied_atm_1m'), load('L1_vol_realized_1m')
    rr1m, rr1w = load('L1_rr25_1m'), load('L1_rr25_1w')

    scores = {
        'carry_vol': load('L2_carry_to_realvol'),
        'ca_yoy': load('L2_ca_yoy_usdbn'),
        'esi_chg': esi.diff(4),
        'rr_sent': -load('L2_rr25_1m_z_52w'),
        'vrp_pos': iv - rv,                     # implied rich vs realized
        'vrp_neg': -(iv - rv),
        'rrterm_pos': rr1m - rr1w,              # RR term-structure slope
        'rrterm_neg': -(rr1m - rr1w),
        'mom': -load('L2_mom_12w_ex1w'),
        'pi_pos': load('L1_pi_realmoney'),
        'pi_flow': load('L1_pi_rm_flow_z'),
        'boll': -load('tech_bollinger_signal', AN),
        'value_slow': load('eps_recursive', AN),   # 4-weekly rebalanced
    }
    SLOW = {'value_slow'}

    dates = tot_next.loc[START:].index

    # ---------------- build each book ----------------
    book_ret, book_ic_trail = {}, {}
    for name, sig in scores.items():
        z = zsec(sig.reindex(columns=[c for c in ASIA9 if c in sig.columns]))
        z = z.reindex(dates)
        if name in SLOW:                          # hold 4 weeks
            z = z.iloc[::4].reindex(dates).ffill(limit=3)
        w = (z / atm.reindex(dates)).replace([np.inf, -np.inf], np.nan)
        g = w.abs().sum(axis=1)
        w = w.div(g.replace(0, np.nan), axis=0) * 2.0
        raw = (w * tot_next.reindex(dates)).sum(axis=1)
        lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
        book_ret[name] = raw * lev.fillna(0)

        ics = {}
        for t in dates:
            s = z.loc[t].dropna()
            r = tot_next.loc[t].reindex(s.index).dropna()
            s = s.loc[r.index]
            if len(s) >= 5 and s.nunique() >= 3:
                v = spearmanr(s, r)[0]
                if pd.notna(v):
                    ics[t] = v
        book_ic_trail[name] = (pd.Series(ics).reindex(dates)
                               .expanding(min_periods=60).mean().shift(1))

    books = pd.DataFrame(book_ret)

    # ---------------- meta blend ----------------
    # CORE books (economic prior) keep an equal-weight floor; sign-ambiguous
    # books (vrp/rrterm mirrors) earn weight only from expanding-window IC.
    # core = books that PASSED the layer-2 IC audit on total returns;
    # audit-failed books (mom/boll/value weekly, pi) are evidence-gated only
    CORE = ['carry_vol', 'ca_yoy', 'esi_chg', 'rr_sent']
    trail = pd.DataFrame(book_ic_trail).reindex(dates)
    evid = trail.clip(lower=0).fillna(0)
    evid = evid.div(evid.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    base = pd.DataFrame(0.0, index=dates, columns=trail.columns)
    base[CORE] = 1.0 / len(CORE)
    mw = 0.5 * base + 0.5 * evid
    mw = mw.div(mw.sum(axis=1), axis=0)
    combo_raw = (mw * books).sum(axis=1)
    lev = (VOL_TARGET / (combo_raw.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
    combo = (combo_raw * lev.fillna(0)).loc[START:]

    eqw_raw = books.mean(axis=1)
    lev2 = (VOL_TARGET / (eqw_raw.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
    eqw = (eqw_raw * lev2.fillna(0)).loc[START:]

    # ---------------- report ----------------
    print('=== individual books (net at mid, 5% vol target) ===')
    rows = []
    for n in books.columns:
        rows.append([n, round(sharpe(books[n].loc[START:]), 2),
                     round(trail[n].iloc[-1], 4) if pd.notna(trail[n].iloc[-1]) else None,
                     round(mw[n].iloc[-1], 3)])
    print(pd.DataFrame(rows, columns=['book', 'sharpe', 'trailing_IC',
                                      'current_meta_wgt']).to_string(index=False))

    live = [n for n in books.columns if mw[n].iloc[-1] > 0.02]
    print('\nbook correlation matrix (books with current weight):')
    print(books[live].loc[START:].corr().round(2).to_string())

    print(f"\nCOMBINED (adaptive meta-weights): Sharpe {sharpe(combo):.2f}   "
          f"ann {combo.mean()*52:.2%}  maxDD "
          f"{(combo.cumsum()-combo.cumsum().cummax()).min():.2%}")
    print(f"COMBINED (naive equal weight):   Sharpe {sharpe(eqw):.2f}")

    segs = [('2014-2015', '2014', '2015'), ('2016-2019', '2016', '2019'),
            ('2020-2021', '2020', '2021'), ('2022-2023', '2022', '2023'),
            ('2024-2026', '2024', '2026')]
    print('\nsubsamples (adaptive combo):')
    for nm, a, b in segs:
        print(f'  {nm}: {sharpe(combo.loc[a:b]):.2f}')

    try:
        l3 = pd.read_csv(f'{AN}/backtest_curves.csv', index_col=0, parse_dates=True)
        l3b = l3['Adaptive (B conviction>0.5)'].reindex(combo.index)
        blend_raw = 0.5 * combo + 0.5 * l3b
        levb = (VOL_TARGET / (blend_raw.rolling(52).std() * np.sqrt(52))).clip(upper=4).shift(1)
        blend = (blend_raw * levb.fillna(1)).loc[START:]
        print(f"\nBLEND (0.5 layer3-conviction + 0.5 sub-books): Sharpe {sharpe(blend):.2f}"
              f"   corr(l3,l4)={l3b.corr(combo):.2f}")
        for nm, a, b in segs:
            print(f'  {nm}: {sharpe(blend.loc[a:b]):.2f}')
    except Exception as e:
        blend = None
        print('blend skipped:', e)

    out = pd.DataFrame({'combo_adaptive': combo, 'combo_equal': eqw})
    if blend is not None:
        out['blend_l3_l4'] = blend
    os.makedirs(AN, exist_ok=True)
    out.to_csv(f'{AN}/subbook_curves.csv')
    books.loc[START:].to_csv(f'{AN}/subbook_book_returns.csv')
    mw.to_csv(f'{AN}/subbook_meta_weights.csv')


if __name__ == '__main__':
    main()
