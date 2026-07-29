"""
IC screen of the NEW external factors (data/external/, fetched 2026-07-29)
on the 13 tradable currencies, weekly Friday grid, with honest lags:

  bis_reer_lvl    XS z of the BIS real broad REER LEVEL - legitimate now,
                  unlike the vendor version: one methodology, 2020=100 for
                  all 13, so cross-sectional comparison is meaningful.
                  Negated (cheap = buy). Lagged 8 Fridays (publication).
  bis_reer_dev    deviation from own 5y mean in own-vol units, negated,
                  same 8-Friday lag. (The vendor version scored -0.024.)
  reserves_3m     IMF official reserves, 3m % change, lagged 6 Fridays -
                  rising reserves = CB accumulating = appreciation pressure
                  being absorbed... sign left to the data (prior unclear).
  exports_yoy     IMF exports YoY % change, lagged 8 Fridays.
  equity_mom_12w  12-week local equity momentum - NOW with 12/13 coverage
                  (Yahoo fills ID PH TH SG BR MX CN CL PL HU; the repo's
                  own data covers KRW TWD INR). Previously only 9 names.
  cftc BRL/MXN    per-currency TIME-SERIES test (only 2 names, so no XS):
                  does the leveraged-fund net position (%OI, 156w z) or its
                  4w change predict next-week total return? Release lag
                  respected: report is as-of Tuesday, public Friday, so the
                  signal is stamped on release Friday and predicts the week
                  AFTER (no lookahead).
"""
import numpy as np
import pandas as pd

from fx_model import TRADED, load_clean, zsec, legs

EXT = 'data/external'


def LX(n):
    return pd.read_csv(f'{EXT}/{n}.csv', index_col=0, parse_dates=True)


def L(n):
    return pd.read_csv(f'data/clean/clean_csv/{n}.csv', index_col=0,
                       parse_dates=True)


def to_friday(df, lag_fridays=0, limit=12):
    df = df[~df.index.duplicated(keep='last')].sort_index()
    fri = pd.date_range('2013-01-04', '2026-07-24', freq='W-FRI')
    out = df.reindex(df.index.union(fri)).ffill(limit=limit * 21 if
                                                len(df) < 2000 else limit * 5)
    out = out.reindex(fri)
    return out.shift(lag_fridays)


def ric(sig, tot, min_ccy=8):
    out = []
    for t in sig.index:
        if t not in tot.index:
            continue
        a, b = sig.loc[t], tot.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            out.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(out).dropna()
    if len(s) < 50:
        return np.nan, np.nan, len(s)
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def main():
    T = load_clean('out_clean.csv.gz')
    sr, cr = legs(T)
    tot_next = (sr[TRADED] + cr[TRADED]).shift(-1)

    print('=' * 86)
    print('NEW EXTERNAL FACTORS - weekly IC on the 13 tradable, honest lags')
    print('=' * 86)
    print(f"{'factor':<34}{'IC':>9}{'t':>7}{'wks':>6}{'ccys':>6}")

    reer = LX('fred_bis_reer_m')          # 13/13 incl TW, monthly
    reer_f = to_friday(reer, lag_fridays=8)
    lvl = -zsec(reer_f)
    m, t, n = ric(lvl, tot_next)
    print(f'{"BIS REER level (cheap=+), lag8w":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(reer.shape[1]):>6}')

    lr = np.log(reer_f)
    dev = -(lr - lr.rolling(260, min_periods=104).mean()) / \
        lr.rolling(260, min_periods=104).std()
    m, t, n = ric(zsec(dev), tot_next)
    print(f'{"BIS REER 5y deviation, lag8w":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(reer.shape[1]):>6}')

    res = LX('imf_reserves_usd_m')
    res3 = res.pct_change(3) * 100
    res_f = to_friday(res3, lag_fridays=6).reindex(columns=TRADED)
    m, t, n = ric(zsec(res_f), tot_next)
    print(f'{"IMF reserves 3m chg, lag6w":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(res.shape[1]):>6}')

    exp = LX('imf_exports_usd_m')
    expy = exp.pct_change(12) * 100
    exp_f = to_friday(expy, lag_fridays=8).reindex(columns=TRADED)
    m, t, n = ric(zsec(exp_f), tot_next)
    print(f'{"IMF exports YoY, lag8w":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(exp.shape[1]):>6}')

    eq_y = LX('yahoo_equity_close_d')
    eq_own = L('L1_equity') if True else None
    try:
        eq_own = L('L1_equity')
    except Exception:
        eq_own = pd.DataFrame()
    eq = pd.DataFrame(index=eq_y.index.union(eq_own.index))
    for c in TRADED:
        if c in eq_y.columns:
            eq[c] = eq_y[c].reindex(eq.index)
        elif c in eq_own.columns:
            eq[c] = eq_own[c].reindex(eq.index)
    eq_f = to_friday(np.log(eq), lag_fridays=0, limit=2)
    mom = eq_f.diff(12)
    m, t, n = ric(zsec(mom), tot_next)
    print(f'{"equity mom 12w (12/13 cover)":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(mom.notna().sum(axis=1).max()):>6}')
    mom4 = eq_f.diff(4)
    m, t, n = ric(zsec(mom4), tot_next)
    print(f'{"equity mom 4w (12/13 cover)":<34}{m:>+9.4f}{t:>+7.2f}{n:>6}'
          f'{int(mom4.notna().sum(axis=1).max()):>6}')

    # ---------------- CFTC: per-currency time-series tests ----------------
    print()
    print('CFTC leveraged-fund net positioning (%OI) - time-series tests')
    print('(signal stamped on release Friday = report Tuesday + 3 days; '
          'predicts NEXT week)')
    pos = LX('cftc_tff_net_pct_oi')
    pos.index = pos.index + pd.Timedelta(days=3)          # Tue -> release Fri
    fri = pd.date_range('2013-01-04', '2026-07-24', freq='W-FRI')
    pos_f = pos.reindex(pos.index.union(fri)).ffill(limit=6).reindex(fri)
    tot_all = (sr + cr)
    print(f"{'test':<40}{'corr':>8}{'t':>7}{'n':>6}")
    for c in ['BRL', 'MXN']:
        y = tot_all[c].shift(-1)
        z = ((pos_f[c] - pos_f[c].rolling(156, min_periods=52).mean()) /
             pos_f[c].rolling(156, min_periods=52).std())
        for nm, s in [('level z (crowding, 3y)', z),
                      ('4w change', pos_f[c].diff(4)),
                      ('level z EXTREME |z|>1.5 fade',
                       (-np.sign(z)).where(z.abs() > 1.5, 0.0))]:
            d = pd.concat([s.rename('x'), y.rename('y')], axis=1).dropna()
            if nm.endswith('fade'):
                r = (d['x'] * d['y'])
                r = r[d['x'] != 0]
                tval = r.mean() / r.std() * np.sqrt(len(r)) if len(r) > 30 else np.nan
                print(f'{c+" "+nm:<40}{r.mean()*1e4:>+7.1f}bp{tval:>+7.2f}'
                      f'{len(r):>6}')
            else:
                cor = d['x'].corr(d['y'])
                tval = cor * np.sqrt(len(d)) if len(d) > 30 else np.nan
                print(f'{c+" "+nm:<40}{cor:>+8.3f}{tval:>+7.2f}{len(d):>6}')

    # EM aggregate: mean of BRL+MXN net z as a global EM-positioning state
    z_em = ((pos_f[['BRL', 'MXN']] -
             pos_f[['BRL', 'MXN']].rolling(156, min_periods=52).mean()) /
            pos_f[['BRL', 'MXN']].rolling(156, min_periods=52).std()).mean(axis=1)
    em_ret = tot_all[TRADED].mean(axis=1).shift(-1)
    d = pd.concat([z_em.rename('x'), em_ret.rename('y')], axis=1).dropna()
    cor = d['x'].corr(d['y'])
    print(f'{"EM-agg positioning z -> EM basket":<40}{cor:>+8.3f}'
          f'{cor*np.sqrt(len(d)):>+7.2f}{len(d):>6}')


if __name__ == '__main__':
    main()
