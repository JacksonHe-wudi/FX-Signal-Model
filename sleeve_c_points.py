"""
Sleeve C - trade the forward POINTS themselves (funding market), not spot.

The user's swap example: buy/sell USDCNH 1M swap at -100 points; if the market
moves to -120 you made 20 pips. That P&L is independent of where spot goes.
funding_rv.py showed 4-week momentum in 1M points reverts (pooled IC -0.18,
13 of 14 currencies same sign) - the strongest raw signal in the library. This
sleeve turns that into a tradable book.

Units: points are converted to an ANNUALIZED RATE so currencies are comparable:
    ann_rate = 12 x pts_1M / (spot x pip_factor) x 100      [% p.a.]
pip_factor is calibrated per currency against the clean carry_1m_ann series
(median of pts_1M / (spot x carry/1200)); 12 of 14 come out as clean powers of
10. Days whose implied |ann_rate| exceeds MAX_ANN are dropped as scale breaks
(TWD/PHP/MYR carry bad ticks - up to 20565%/yr).

Signal: z-score of the 4-week change in ann_rate, traded CONTRARIAN
(fade the move). Cross-sectional, |z| gate, equal-risk legs.

P&L: a 1M swap position held H days earns the change in the 1M rate over that
window, scaled by the tenor: pnl = -side x (ann_rate_t+H - ann_rate_t)/12 x
(H/21) in monthly-rate units -> expressed in bp of notional. Costs are charged
per entry on the points bid/ask (the relevant spread for a swap is the POINTS
spread, typically well under 1bp of notional for liquid 1M NDF).
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
          'BRL', 'MXN', 'CLP', 'PLN', 'HUF']
MAX_ANN = 40.0          # implied annualized rate beyond this = unusable tick
CAL_WIN = 250           # rolling window (bd) for recalibrating the pip factor
EXCLUDE = ('MYR',)      # points/carry disagree even in SIGN - unusable
LOOKBACK = 20           # business days (~4 weeks) for the momentum being faded
HOLD = 20               # holding period in business days
Z_GATE = 1.0


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def build_rates():
    """Annualized 1M rate per currency, with a ROLLING pip-factor calibration.

    A single full-sample pip factor is wrong when the source changes quoting
    units: PHP switched by x100 and TWD by ~x850 from 2025-09-29, and a fixed
    factor would discard that whole (most recent) year. A trailing-window
    recalibration tracks the break instead. MYR is excluded outright - its
    implied factor drifts 1554..8551 and turns NEGATIVE from 2025, i.e. points
    and carry disagree on sign, so the series is unusable rather than rescaled.
    """
    sp = load('L1_spot_daily_traded')
    p1m = load('L1_fwd_pts_1m_daily')
    carry = load('L1_carry_1m_ann')
    fac, ann = {}, {}
    for c in TRADED:
        if c in EXCLUDE or c not in p1m.columns or c not in sp.columns:
            continue
        cc = carry[c].reindex(sp.index).ffill()
        implied = (p1m[c] / (sp[c] * cc / 1200)).where(cc.abs() > 0.5)
        # trailing median, shifted so today's factor uses only past data
        roll = implied.rolling(CAL_WIN, min_periods=60).median().shift(1)
        roll = roll.where(roll.abs() > 1e-9).ffill()
        if roll.notna().sum() < 200:
            continue
        fac[c] = roll
        r = 12.0 * p1m[c] / (sp[c] * roll) * 100.0        # % p.a.
        ann[c] = r.where(r.abs() <= MAX_ANN)              # residual bad ticks
    return pd.DataFrame(ann).dropna(how='all'), fac


def main():
    ann, fac = build_rates()
    print('pip factor (rolling, latest):', {k: round(v.dropna().iloc[-1])
                                             for k, v in fac.items()})
    dropped = {c: int((ann[c].isna() & load('L1_fwd_pts_1m_daily')[c]
                       .reindex(ann.index).notna()).sum()) for c in ann.columns}
    print('days dropped as scale breaks:', {k: v for k, v in dropped.items() if v})

    # signal: fade the 4-week move in the rate
    chg = ann.diff(LOOKBACK)
    z = (chg - chg.rolling(252, min_periods=120).mean()) / \
        chg.rolling(252, min_periods=120).std()
    # fwd (below) is POSITIVE when the rate falls, so betting on a fall = +1.
    # Contrarian: rate rose (z>0) -> bet it falls -> position +1. So sig = z.
    sig = z

    # forward P&L of a 1M swap over HOLD days, in bp of notional:
    # holding the points position earns the change in the monthly rate
    fwd = -(ann.shift(-HOLD) - ann) / 12.0 * 100.0        # bp, per unit short-rate
    fwd = fwd * (HOLD / 21.0)

    print(f'\nper-currency: FADE the 4w move in the 1M rate, hold {HOLD}bd, |z|>{Z_GATE}')
    print(f"{'ccy':<5}{'n':>6}{'hit':>7}{'mean_bp':>9}{'t':>7}")
    rows = []
    for c in ann.columns:
        s = sig[c].where(sig[c].abs() > Z_GATE)
        r = (np.sign(s) * fwd[c]).dropna()
        r = r.iloc[::HOLD]                                # non-overlapping
        if len(r) < 15:
            continue
        t = r.mean() / r.std() * np.sqrt(len(r))
        rows.append([c, len(r), 100 * (r > 0).mean(), r.mean(), t])
        print(f'{c:<5}{len(r):>6}{100*(r>0).mean():>6.0f}%{r.mean():>9.1f}{t:>7.2f}')
    res = pd.DataFrame(rows, columns=['ccy', 'n', 'hit', 'mean_bp', 't'])

    # ---- portfolio: NON-OVERLAPPING cycles only ----
    # Overlapping 20d windows share 19 of 20 days, so annualizing them inflates
    # the Sharpe badly. Build one non-overlapping series per starting offset and
    # report the distribution across the 20 offsets.
    def cycles(names, offset):
        out = []
        for d in sig.index[offset::HOLD]:
            if d not in fwd.index:
                continue
            s = sig.loc[d, names].where(sig.loc[d, names].abs() > Z_GATE).dropna()
            f = fwd.loc[d, names].dropna()
            common = [c for c in s.index if c in f.index]
            if len(common) >= 3:
                out.append([d, (np.sign(s[common]) * f[common]).sum() / len(common),
                            len(common)])
        return pd.DataFrame(out, columns=['d', 'ret_bp', 'n']).set_index('d')

    CLEAN = [c for c in ann.columns if c not in ('PHP', 'TWD')]
    for label, names in [(f'ALL {len(ann.columns)} (ex MYR)', list(ann.columns)),
                         (f'ex PHP/TWD ({len(CLEAN)})', CLEAN)]:
        print(f'\nPORTFOLIO - {label}, non-overlapping {HOLD}bd cycles')
        for cbp in [0.0, 0.5, 1.0, 2.0]:
            shs, ns = [], []
            for off in range(HOLD):
                p = cycles(names, off)
                if len(p) < 12:
                    continue
                r = (p['ret_bp'] - cbp) / 1e4
                shs.append(r.mean() / r.std() * np.sqrt(252 / HOLD))
                ns.append(len(p))
            if shs:
                lab = 'gross' if cbp == 0 else f'{cbp}bp'
                print(f'  {lab:<6} Sharpe median {np.median(shs):+.2f}  '
                      f'[p25 {np.percentile(shs,25):+.2f}, p75 '
                      f'{np.percentile(shs,75):+.2f}]  n/cycle-series {int(np.median(ns))}')
    p = cycles(CLEAN, 0)
    res.to_csv('analysis/sleeve_c_results.csv', index=False)
    p.to_csv('analysis/sleeve_c_curve.csv')
    print('\nwritten: analysis/sleeve_c_results.csv, sleeve_c_curve.csv')


if __name__ == '__main__':
    main()
