"""
Funding RV validation: 1W/1M forward-points curve trades.

Mechanics (docs/model_walkthrough.md section 5 step 4, user's spec):
  Trade = enter 1M swap + roll the near leg weekly with 1W swaps in the
  opposite direction. Example: 1M pts = -100, 1W pts = -20 -> pay 100,
  receive 20x4 = 80 -> lose 20 if nothing moves; breakeven when 1W widens
  to -25. So P&L(4w) = -sign(z) x [ sum_{i=0..3} pts_1w_{t+i} - pts_1m_t ]
  (per-currency pips; pip scale cancels inside a currency so we report
  scale-free hit rates and t-stats, no cross-currency pip aggregation).

Part A (PRIMARY, mean reversion): does an extreme basis z predict
  convergence, and does the enter-1M-roll-1W trade make money?
Part B (exploratory, directional): can anything in our dataset predict the
  DIRECTION of 1M points (outright swap positions, user's example 1)?
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
CCYS = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
        'BRL', 'MXN', 'CLP', 'PLN', 'HUF']
Z_ENTRY = 1.5


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def tstat(r):
    r = pd.Series(r).dropna()
    return r.mean() / r.std() * np.sqrt(len(r)) if len(r) > 5 and r.std() > 0 else np.nan


def main():
    p1w, p1m = load('L1_fwd_pts_1w'), load('L1_fwd_pts_1m')
    basis, z = load('L2_fwdpts_basis_1w_1m'), load('L2_fwdpts_basis_z_52w')

    # ---------------- Part A: mean reversion of the basis ----------------
    print('=' * 78)
    print('PART A - basis mean reversion + enter-1M-roll-1W trade (|z| > '
          f'{Z_ENTRY}, non-overlapping 4w trades)')
    print('=' * 78)
    print(f"{'ccy':<5}{'corr(z,d1w)':>12}{'corr(z,d4w)':>12}{'trades':>8}"
          f"{'hit':>6}{'mean_pips':>11}{'t':>7}")
    rows = []
    for c in CCYS:
        if c not in z.columns:
            continue
        zc = z[c].dropna()
        bc = basis[c].reindex(zc.index)
        # convergence: high z should be followed by basis falling
        c1 = zc.corr(bc.shift(-1) - bc)
        c4 = zc.corr(bc.shift(-4) - bc)
        # non-overlapping trade simulation (annualized units: comparing the
        # realized average 1W points to the locked 1M over the SAME horizon,
        # so the 4x7=28d vs ~30d calendar mismatch does not bias the P&L)
        pnl, dates = [], []
        i = 0
        idx = zc.index
        while i < len(idx) - 4:
            t = idx[i]
            if abs(zc[t]) >= Z_ENTRY:
                fut = 52.0 * p1w[c].reindex(idx[i:i + 4])
                m0 = 12.0 * p1m[c].reindex([t]).iloc[0]
                if fut.notna().all() and pd.notna(m0):
                    gap = (fut.mean() - m0) / 12.0   # monthly-pips equivalent
                    pnl.append(-np.sign(zc[t]) * gap)
                    dates.append(t)
                i += 4                               # non-overlapping
            else:
                i += 1
        pnl = pd.Series(pnl, index=dates)
        hit = 100 * (pnl > 0).mean() if len(pnl) else np.nan
        rows.append([c, c1, c4, len(pnl), hit, pnl.mean() if len(pnl) else np.nan,
                     tstat(pnl)])
        print(f'{c:<5}{c1:>12.2f}{c4:>12.2f}{len(pnl):>8}{hit:>5.0f}%'
              f'{(pnl.mean() if len(pnl) else np.nan):>11.1f}{tstat(pnl):>7.2f}')
    resA = pd.DataFrame(rows, columns=['ccy', 'corr_z_d1w', 'corr_z_d4w',
                                       'n_trades', 'hit_pct', 'mean_pips', 't'])
    # pooled hit rate (scale-free aggregate)
    ok = resA.dropna(subset=['t'])
    print(f"\npooled: {int(ok['n_trades'].sum())} trades, "
          f"median corr(z,d4w) = {resA['corr_z_d4w'].median():+.2f}, "
          f"currencies with t>1: {sorted(ok[ok['t'] > 1]['ccy'].tolist())}")

    # ---------------- Part B: directional points prediction --------------
    print('\n' + '=' * 78)
    print('PART B - can anything predict next-week 1M points direction? '
          '(per-ccy time-series rank IC, pooled mean)')
    print('=' * 78)
    ann1m = 12.0 * p1m
    target = ann1m.diff().shift(-1)                  # next-week change
    rr = load('L2_rr25_1m_z_52w')
    atm = load('L1_vol_implied_atm_1m')
    volz = (atm - atm.rolling(104, min_periods=40).mean()) / \
           atm.rolling(104, min_periods=40).std()
    carry = load('L1_carry_1m_ann')
    reg = load('L1_regime')
    hy = reg['US_HY_OAS'].diff(4) if 'US_HY_OAS' in reg.columns else None
    cands = {
        'basis_z (curve)': z,
        'pts_mom_4w': ann1m.diff(4),
        'rr25_z': rr,
        'vol_z': volz,
        'carry_level': carry,
    }
    print(f"{'candidate':<20}{'pooled IC':>10}{'|IC|>0.05 ccys':>40}")
    for nm, sig in cands.items():
        ics = {}
        for c in CCYS:
            if c in sig.columns and c in target.columns:
                d = pd.concat([sig[c], target[c]], axis=1).dropna()
                if len(d) > 100:
                    ics[c] = d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank())
        if ics:
            s = pd.Series(ics)
            big = {k: round(v, 2) for k, v in s.items() if abs(v) > 0.05}
            print(f'{nm:<20}{s.mean():>+10.3f}{str(big):>40}')
    if hy is not None:
        ics = {}
        for c in CCYS:
            if c in target.columns:
                d = pd.concat([hy, target[c]], axis=1).dropna()
                if len(d) > 100:
                    ics[c] = d.iloc[:, 0].rank().corr(d.iloc[:, 1].rank())
        s = pd.Series(ics)
        big = {k: round(v, 2) for k, v in s.items() if abs(v) > 0.05}
        print(f'{"dUS_HY_4w (common)":<20}{s.mean():>+10.3f}{str(big):>40}')

    resA.to_csv(f'{AN}/funding_rv_results.csv', index=False)
    print(f'\nwritten: {AN}/funding_rv_results.csv')


if __name__ == '__main__':
    main()
