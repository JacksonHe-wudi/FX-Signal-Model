"""
Backtest of the user's ORIGINAL hourly double-Bollinger breakout system
(fx_signal_model.py) on the uploaded 1y of hourly Citi closes, 28 pairs.

System, faithfully reproduced from the original code:
  bands   SMA(p) +- k * 1.20 * rolling_std(p)   (1.20 = the illiquid-hours adj)
  entry   close crosses UP through the INNER upper band -> LONG
          close crosses DOWN through the INNER lower band -> SHORT
  exits   (priority order, checked each bar, all at close except the breaker)
          1. circuit breaker: 0.5% adverse move from the position's peak,
             filled AT the stop price
          2. symmetric stop: give back as much as the intended upside,
             using the PREVIOUS bar's band levels (as the original does)
          3. take profit: close beyond the OUTER band
  costs   slippage per exit in the original (10 CNH pips ~ 1.5bp); here
          charged per SIDE in bp of price, swept 0 / 1.5 / 3.

Validation protocol (the original grid-searched p,q,r on the full window):
  IS  2025-07-30 .. 2026-01-31  - grid search p x inner x outer, pick by Sharpe
  OOS 2026-02-01 .. 2026-07-29  - frozen parameters
  plus a cross-sectional test: CNH's IS-best parameters applied unchanged to
  the other 27 pairs OOS. A real edge travels; an overfit one doesn't.

Sharpe is computed on DAILY aggregated P&L (hourly Sharpe would overstate
independence), annualized by sqrt(261).
"""
import numpy as np
import pandas as pd

PKL = ('/tmp/claude-0/-home-user-FX-Signal-Model/'
       '60b36b76-c782-5fc7-86d9-902ff89ac8d3/scratchpad/hourly.pkl')
SD_ADJ = 1.20
DD_LIMIT = 0.005
GRID_P = [20, 30, 40, 50, 60]
GRID_R = [0.5, 0.65, 0.8]          # inner (entry) band, in SDs
GRID_Q = [1.5, 1.75, 2.0]          # outer (take-profit) band
IS_END = pd.Timestamp('2026-01-31 23:00')


def run_system(close, p, r_in, q_out, slip_bp):
    """Event loop over hourly closes. Returns hourly P&L series (in return
    units) and the trade count."""
    c = close.values
    n = len(c)
    sma = close.rolling(p).mean().values
    sd = close.rolling(p).std().values * SD_ADJ
    entL, entS = sma + r_in * sd, sma - r_in * sd
    tpL, tpS = sma + q_out * sd, sma - q_out * sd
    slip = slip_bp * 1e-4

    pnl = np.zeros(n)
    pos, entry, peak, trades = 0, 0.0, 0.0, 0
    for i in range(1, n):
        if np.isnan(c[i]) or np.isnan(sma[i]) or np.isnan(c[i - 1]):
            continue
        if pos == 1:
            peak = max(peak, c[i - 1])
            stop = peak * (1 - DD_LIMIT)
            sym = entL[i - 1] - (tpL[i - 1] - entL[i - 1])
            if c[i] < stop:                              # circuit breaker
                pnl[i] = stop / c[i - 1] - 1 - slip
                pos = 0
            elif not np.isnan(sym) and c[i] < sym:       # symmetric stop
                pnl[i] = c[i] / c[i - 1] - 1 - slip
                pos = 0
            elif c[i] > tpL[i]:                          # take profit
                pnl[i] = c[i] / c[i - 1] - 1 - slip
                pos = 0
            else:
                pnl[i] = c[i] / c[i - 1] - 1
        elif pos == -1:
            peak = min(peak, c[i - 1])
            stop = peak * (1 + DD_LIMIT)
            sym = entS[i - 1] - (tpS[i - 1] - entS[i - 1])
            if c[i] > stop:
                pnl[i] = -(stop / c[i - 1] - 1) - slip
                pos = 0
            elif not np.isnan(sym) and c[i] > sym:
                pnl[i] = -(c[i] / c[i - 1] - 1) - slip
                pos = 0
            elif c[i] < tpS[i]:
                pnl[i] = -(c[i] / c[i - 1] - 1) - slip
                pos = 0
            else:
                pnl[i] = -(c[i] / c[i - 1] - 1)
        if pos == 0:
            if c[i - 1] < entL[i - 1] and c[i] > entL[i]:
                pos, entry, peak = 1, c[i], c[i]
                pnl[i] -= slip
                trades += 1
            elif c[i - 1] > entS[i - 1] and c[i] < entS[i]:
                pos, entry, peak = -1, c[i], c[i]
                pnl[i] -= slip
                trades += 1
    return pd.Series(pnl, index=close.index), trades


def daily_sharpe(r):
    d = r.resample('D').sum()
    d = d[d != 0]
    if len(d) < 20 or d.std() == 0:
        return np.nan
    return d.mean() / d.std() * np.sqrt(261)


def main():
    df = pd.read_pickle(PKL)
    df = df.loc[:, ~df.columns.duplicated()]
    ren = {}
    for c in df.columns:
        p = c.split('.')
        ren[c] = p[3] if p[2] == 'USD' else p[2]
    df = df.rename(columns=ren)
    cnh = df['CNH'].dropna()

    # ---------------- grid search, in-sample, CNH ----------------
    print('=' * 86)
    print('STEP 1 - grid search on USDCNH, IN-SAMPLE (2025-07-30 .. 2026-01-31)')
    print('cost = 1.5bp per side (the original 10 CNH pips)')
    print('=' * 86)
    is_px = cnh[cnh.index <= IS_END]
    oos_px = cnh[cnh.index > IS_END - pd.Timedelta(days=5)]   # warmup overlap
    rows = []
    for p in GRID_P:
        for r_in in GRID_R:
            for q in GRID_Q:
                r, tr = run_system(is_px, p, r_in, q, 1.5)
                rows.append((p, r_in, q, daily_sharpe(r), 1e4 * r.sum(), tr))
    G = pd.DataFrame(rows, columns=['p', 'inner', 'outer', 'sharpe', 'bp', 'trades'])
    G = G.sort_values('sharpe', ascending=False)
    print(G.head(8).to_string(index=False,
                              float_format=lambda x: f'{x:+.2f}'))
    print(f'\ngrid stats: {len(G)} combos, {int((G.sharpe > 0).sum())} positive '
          f'({100 * (G.sharpe > 0).mean():.0f}%), median Sharpe '
          f'{G.sharpe.median():+.2f}')
    best = G.iloc[0]
    p_b, r_b, q_b = int(best.p), best.inner, best.outer
    print(f'IS-best: p={p_b}, inner={r_b}, outer={q_b}  '
          f'(IS Sharpe {best.sharpe:+.2f}, {int(best.trades)} trades in 6m)')

    # ---------------- OOS on CNH ----------------
    print()
    print('=' * 86)
    print('STEP 2 - frozen parameters, OUT-OF-SAMPLE (2026-02 .. 2026-07), CNH')
    print('=' * 86)
    for slip in [0.0, 1.5, 3.0]:
        r, tr = run_system(oos_px, p_b, r_b, q_b, slip)
        r = r[r.index > IS_END]
        print(f'  cost {slip:.1f}bp/side:  Sharpe {daily_sharpe(r):+.2f}   '
              f'total {1e4 * r.sum():+.0f}bp in 6m   trades {tr}')

    # ---------------- cross-sectional OOS ----------------
    print()
    print('=' * 86)
    print('STEP 3 - SAME frozen parameters on the other 27 pairs, OOS, 1.5bp')
    print('=' * 86)
    res = {}
    for ccy in df.columns:
        px = df[ccy].dropna()
        px_oos = px[px.index > IS_END - pd.Timedelta(days=5)]
        r, tr = run_system(px_oos, p_b, r_b, q_b, 1.5)
        r = r[r.index > IS_END]
        res[ccy] = (daily_sharpe(r), 1e4 * r.sum(), tr)
    R = pd.DataFrame(res, index=['sharpe', 'bp_6m', 'trades']).T \
        .sort_values('sharpe', ascending=False)
    print(R.to_string(float_format=lambda x: f'{x:+.1f}'))
    print(f'\ncross-section: median Sharpe {R.sharpe.median():+.2f}, '
          f'{int((R.sharpe > 0).sum())}/{len(R)} positive, '
          f'mean {R.sharpe.mean():+.2f}')

    # ---------------- per-pair IS-best -> OOS (gives the method every chance) --
    print()
    print('=' * 86)
    print('STEP 4 - optimize EACH pair on its own IS, trade its own OOS, 1.5bp')
    print('=' * 86)
    rows = []
    for ccy in df.columns:
        px = df[ccy].dropna()
        is_p = px[px.index <= IS_END]
        oo_p = px[px.index > IS_END - pd.Timedelta(days=5)]
        bs, bconf = -9e9, None
        for p in GRID_P:
            for r_in in GRID_R:
                for q in GRID_Q:
                    r, _ = run_system(is_p, p, r_in, q, 1.5)
                    s = daily_sharpe(r)
                    if pd.notna(s) and s > bs:
                        bs, bconf = s, (p, r_in, q)
        r, _ = run_system(oo_p, *bconf, 1.5)
        r = r[r.index > IS_END]
        rows.append((ccy, bs, daily_sharpe(r)))
    PB = pd.DataFrame(rows, columns=['ccy', 'IS_best', 'OOS']).set_index('ccy')
    print(PB.sort_values('OOS', ascending=False).to_string(
        float_format=lambda x: f'{x:+.2f}'))
    print(f'\nIS median {PB.IS_best.median():+.2f}  ->  '
          f'OOS median {PB.OOS.median():+.2f}   '
          f'({int((PB.OOS > 0).sum())}/{len(PB)} positive OOS)')


if __name__ == '__main__':
    main()
