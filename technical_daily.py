"""
Technical analysis on DAILY spot (2013+, 14 currencies) - does it add anything?

Earlier work only had weekly spot, where every technical/momentum variant died
(4w momentum IC -0.010, 12w +0.006, Bollinger worth 12% of a pillar). Daily data
is the natural sampling for technicals, so this retests them properly.

Two roles are tested separately, because they are different claims:

  ROLE 1  STANDALONE cross-sectional signal
          Rank the 14 currencies by the technical each Friday; measure weekly
          cross-sectional IC against next-week total return, exactly as every
          other factor in the library was screened.

  ROLE 2  ENTRY GATE on sleeve A  (the user's proposed architecture)
          Sleeve A picks WHAT to trade; the technical only decides WHETHER to
          enter this week. Implemented as: take sleeve A's book, and for each
          name require the daily technical to agree with the intended side,
          otherwise stay flat in that name this week.

Technicals tested (all computed on daily spot, then sampled on Fridays; all use
data up to and including the Friday close, so no look-ahead):
  rsi_14      Wilder RSI, faded (overbought = short the currency)
  bb_z_20     distance from the 20d mean in daily-vol units, faded
  ma_cross    50d vs 200d trend of the currency (positive = currency strong)
  mom_20d     20-day return of the currency
  mom_60d     60-day return
  vol_adj_20  20d return divided by realized daily vol
Sign convention: every signal is expressed as "higher = expect the LOCAL
CURRENCY to appreciate", matching tot_next.
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
          'BRL', 'MXN', 'CLP', 'PLN', 'HUF']


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def zsec(df):
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def build_technicals(sp_d):
    """sp_d = daily USD/CCY. Returns dict of daily frames, higher = CCY strong."""
    # local-currency strength = -log(USD/CCY)
    px = -np.log(sp_d)
    out = {}

    d = px.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    out['rsi_14 (faded)'] = -(rsi - 50)

    m20 = px.rolling(20).mean()
    s20 = px.diff().rolling(20).std() * np.sqrt(20)
    out['bb_z_20 (faded)'] = -((px - m20) / s20.replace(0, np.nan))

    out['ma_cross_50_200'] = px.rolling(50).mean() - px.rolling(200).mean()
    out['mom_20d'] = px.diff(20)
    out['mom_60d'] = px.diff(60)
    out['vol_adj_mom_20d'] = px.diff(20) / (px.diff().rolling(20).std()
                                            * np.sqrt(20)).replace(0, np.nan)
    return out


def xs_ic(sig_w, tot_next, min_ccy=8):
    ics = []
    for t in sig_w.index:
        if t not in tot_next.index:
            continue
        a, b = sig_w.loc[t], tot_next.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            ics.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(ics).dropna()
    if len(s) < 50:
        return np.nan, np.nan, len(s)
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def main():
    sp_d = load('L1_spot_daily_traded')[TRADED]
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)
    fri = spot.index

    techs = build_technicals(sp_d)
    tech_w = {k: v.reindex(fri, method='ffill') for k, v in techs.items()}

    print('=' * 74)
    print('ROLE 1 - standalone cross-sectional signal (weekly IC vs total return)')
    print('=' * 74)
    print(f"{'technical':<24}{'IC':>9}{'t':>8}{'n_wks':>7}")
    for nm, s in tech_w.items():
        m, t, n = xs_ic(s.reindex(columns=TRADED), tot_next)
        print(f'{nm:<24}{m:>+9.4f}{t:>+8.2f}{n:>7}')
    # benchmark: the live composite
    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    num = 2 * zc.fillna(0) + ze.reindex(zc.index).fillna(0)
    den = 2 * zc.notna() + 1 * ze.reindex(zc.index).notna()
    comp = zsec((num / den.replace(0, np.nan)).rolling(3).mean())
    m, t, n = xs_ic(comp, tot_next)
    print(f'{"[live composite]":<24}{m:>+9.4f}{t:>+8.2f}{n:>7}   <- benchmark')

    print()
    print('=' * 74)
    print('ROLE 2 - entry gate on sleeve A (trade only when the technical agrees)')
    print('=' * 74)
    from sleeve_a_rv import build_book, VOL_TARGET
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    W = build_book(comp, atm)
    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
        disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)

    def run(w):
        raw = (w * tot_next.reindex(w.index)).sum(axis=1) * mult
        lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
        r = (raw * lev).dropna()
        cum = r.cumsum()
        return (r.mean() / r.std() * np.sqrt(52), 52 * r.mean() * 100,
                (cum - cum.cummax()).min() * 100,
                100 * (w.abs().sum(axis=1) / W.abs().sum(axis=1).replace(0, np.nan)).mean())

    base = run(W)
    print(f"{'gate':<28}{'Sharpe':>8}{'ann%':>8}{'maxDD%':>9}{'gross kept':>12}")
    print(f'{"none (sleeve A as-is)":<28}{base[0]:>+8.2f}{base[1]:>+8.2f}'
          f'{base[2]:>9.1f}{base[3]:>11.0f}%')
    for nm, s in tech_w.items():
        sw = s.reindex(columns=TRADED).reindex(W.index)
        agree = np.sign(sw).fillna(0) == np.sign(W)
        Wg = W.where(agree, 0.0)
        r = run(Wg)
        print(f'{nm:<28}{r[0]:>+8.2f}{r[1]:>+8.2f}{r[2]:>9.1f}{r[3]:>11.0f}%')


if __name__ == '__main__':
    main()
