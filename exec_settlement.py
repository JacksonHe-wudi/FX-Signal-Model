"""
Settlement-accurate execution comparison (daily data, 2013+).

Fixes the approximation in exec_mix.py, which modelled "1M" as a 4-week (28d)
weight freeze. A real 1M forward settles POINT-TO-POINT (user's spec):
    trade 17th -> settle 17th next month; month-end -> month-end;
    non-existent day clipped to month-end; weekends modified-following.
So the roll cycle drifts off the Friday grid and the locked carry spans ~30-31
days, not 28.

P&L accounting (daily, per position):
    daily return = -dlog(spot)              (long CCY: USD/CCY down = gain)
                 + ann_rate_locked/252      (rate locked at ENTRY for the tenor)
Costs: each roll pays the one-way spread on the notional rolled, so 1W pays
~52x/yr and 1M ~12x/yr - the core economic trade-off.

Positions are held to settlement (no early unwind), so the 1M book carries
"stale" names the weekly signal has already dropped; that is the honest cost
of locking a tenor, and it also makes the 1M book slower/larger (8.3 vs 6.7
average positions).

Full daily history (2013+) after the source moved the Bloomberg forward block
to daily; the earlier run covered only 2019-07+.

Schemes: 1W weekly roll | 1M point-to-point | hybrid (core->1M, new->1W).
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import load, zsec, build_book, TRADED, VOL_TARGET

START = '2013-01-04'


def settle_1m(d):
    """Point-to-point 1M date, month-end preserved, modified-following."""
    d = pd.Timestamp(d)
    if d == d + pd.offsets.MonthEnd(0):                  # trade date is month-end
        t = (d + pd.DateOffset(months=1)) + pd.offsets.MonthEnd(0)
    else:
        t = d + pd.DateOffset(months=1)                  # pandas clips 31st -> 28th
    if t.weekday() >= 5:                                 # weekend -> modified following
        fwd = t + pd.Timedelta(days=7 - t.weekday())
        t = fwd if fwd.month == t.month else t - pd.Timedelta(days=t.weekday() - 4)
    return t


def annual_rates(sp, p1w, p1m, carry):
    """Clean annualized carry for each tenor.

    The raw daily FWD_POINT_PIP series have quoting-unit breaks (PHP switched
    by x100 and TWD by ~x850 from 2025-09-29; MYR's implied factor drifts and
    flips sign), so points are NOT used for the LEVEL. Instead:
        ann_1M = carry_1m_ann          (implied yield - SOFR, clean)
        ann_1W = ann_1M x (52*pts_1w)/(12*pts_1m)
    The tenor RATIO is scale-free (the pip factor cancels), and is winsorized
    to [0.2, 5] with a fallback of 1.0 when missing or implausible.
    """
    idx = sp.index
    ann1m = carry[[c for c in sp.columns if c in carry.columns]].reindex(idx).ffill()
    ratio = pd.DataFrame(1.0, index=idx, columns=ann1m.columns)
    for c in ann1m.columns:
        if c in p1w.columns and c in p1m.columns:
            r = (52.0 * p1w[c]) / (12.0 * p1m[c])
            ratio[c] = r.reindex(idx).where(r.between(0.2, 5.0)).fillna(1.0)
    return ann1m, ann1m * ratio


def simulate(wgt_wk, sp, rates, mode, tenure=None, verbose=False):
    """Daily P&L on an explicit roll chain (auditable).

    Each currency holds at most one position. A position is (weight,
    carry_per_day, expiry). It is refreshed ONLY on its own roll date:
      1W     -> every Friday
      1M     -> on its point-to-point expiry
      HYBRID -> core names on the 1M chain, others weekly
    A position that expires is always CLOSED, even if it cannot be reopened
    (missing data) - otherwise stale carry accrues forever.
    """
    days = sp.index[sp.index >= START]
    dlog = np.log(sp).diff()
    fridays = set(wgt_wk.index[wgt_wk.index >= START])
    wk_idx = wgt_wk.index[wgt_wk.index >= START]
    pos, pnl, nopen = {}, pd.Series(0.0, index=days), pd.Series(0.0, index=days)
    rolled = pd.Series(0.0, index=days)          # notional rolled (for costs)

    def make(c, w, d, tenor):
        tbl = rates[tenor]
        if c not in tbl.columns or d not in tbl.index or pd.isna(tbl.loc[d, c]):
            return None
        end = d + pd.Timedelta(days=7) if tenor == '1W' else settle_1m(d)
        # annualized rate locked at entry, accrued per business day
        return (w, tbl.loc[d, c] / 100.0 / 252.0, end)

    for d in days:
        # 1) ACCRUE FIRST with the book carried in from the previous day, so a
        #    position opened on date d only starts earning on d+1 (no look-ahead)
        tot = 0.0
        for c, (w, cpd, _) in pos.items():
            if pd.notna(dlog.loc[d, c]):
                tot += w * (-dlog.loc[d, c] + cpd)
        pnl.loc[d], nopen.loc[d] = tot, len(pos)

        # 2) THEN roll / refresh using signals known at the close of d
        prior = wk_idx[wk_idx <= d]
        if len(prior) == 0:
            continue
        w_now = wgt_wk.loc[prior[-1]]
        ten_now = tenure.loc[prior[-1]] if tenure is not None else None
        for c in list(set(wgt_wk.columns) | set(pos)):
            cur = pos.get(c)
            expired = cur is not None and d >= cur[2]
            if expired:
                pos.pop(c)                       # ALWAYS close at expiry
                cur = None
            tgt = w_now.get(c, 0.0)
            if mode == '1W':
                tenor, roll = '1W', (d in fridays)
            elif mode == '1M':
                tenor, roll = '1M', (cur is None)
            else:
                core = ten_now is not None and ten_now.get(c, 0) >= 4
                tenor = '1M' if core else '1W'
                roll = (cur is None) if tenor == '1M' else (d in fridays)
            if roll:
                if cur is not None:
                    pos.pop(c, None)             # replace on the roll date
                if tgt != 0:
                    p = make(c, tgt, d, tenor)
                    if p:
                        pos[c] = p
                        rolled.loc[d] += abs(tgt)
    if verbose:
        print(f'    avg open positions {nopen.mean():.1f}, '
              f'notional rolled {252*rolled.mean():.1f}x/yr')
    return pnl, rolled


def main():
    sp = load('L1_spot_daily_traded')
    p1w = load('L1_fwd_pts_1w_daily')
    p1m = load('L1_fwd_pts_1m_daily')
    carry = load('L1_carry_1m_ann')
    atm = load('L1_vol_implied_atm_1m')
    ann1m, ann1w = annual_rates(sp, p1w, p1m, carry)
    print('median annualized carry by tenor (%):')
    print('  1M:', {c: round(ann1m[c].median(), 2) for c in ann1m.columns})
    print('  1W:', {c: round(ann1w[c].median(), 2) for c in ann1w.columns}, '\n')

    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi)
    num = 2 * zc.fillna(0) + ze.reindex(zc.index).fillna(0)
    den = 2 * zc.notna() + 1 * ze.reindex(zc.index).notna()
    comp = zsec((num / den.replace(0, np.nan)).rolling(3).mean())
    W = build_book(comp, atm)
    ten = pd.DataFrame(0, index=W.index, columns=W.columns)
    for i in range(1, len(W)):
        ten.iloc[i] = np.where(W.iloc[i] != 0,
                               np.where(W.iloc[i - 1] != 0, ten.iloc[i - 1] + 1, 1), 0)

    rates = {'1W': ann1w, '1M': ann1m}
    print(f"{'scheme':<34}{'mid':>7}{'1bp':>7}{'2bp':>7}{'5bp':>7}{'maxDD':>8}")
    out = {}
    for mode, label in [('1W', '1W weekly roll'),
                        ('1M', '1M point-to-point'),
                        ('HYBRID', 'hybrid core->1M / new->1W')]:
        r0, rolled = simulate(W, sp, rates, mode, ten, verbose=True)
        r0 = r0.fillna(0.0)
        lev = VOL_TARGET / (r0.std() * np.sqrt(252))
        line = f'{label:<34}'
        for bp in [0, 1, 2, 5]:
            r = (r0 - rolled * bp / 1e4) * lev
            line += f'{r.mean()/r.std()*np.sqrt(252):>+7.2f}'
            if bp == 0:
                out[label] = r
        cum = out[label].cumsum()
        print(line + f'{(cum-cum.cummax()).min()*100:>7.1f}%')
    pd.DataFrame(out).to_csv('analysis/exec_settlement_curves.csv')
    print('\nwritten: analysis/exec_settlement_curves.csv')


if __name__ == '__main__':
    main()
