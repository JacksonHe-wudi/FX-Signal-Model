"""
Technical signal - weekly Bollinger breakout with best-Sharpe parameter
selection, adapted from the original hourly CNH model (fx_signal_model.py).

Logic per currency (weekly, on USD/XXX spot):
- Bands: SMA(p) +/- r*sd(p) = entry bands, SMA(p) +/- q*sd(p) = exit bands.
- Go long USD/XXX when close crosses UP through the upper entry band;
  go short when it crosses DOWN through the lower entry band.
- Exit on: outer band hit (take profit), trailing stop
  (entry band -/+ the entry-to-exit band width), or peak drawdown breaker.
- Parameter selection (the "run_loop" idea, made walk-forward): every 13
  weeks, grid-search (p, q, r) on the trailing 156 weeks and keep the
  combination with the best annualized Sharpe (min 3 trades). Parameters are
  then FIXED until the next re-selection - no look-ahead.
- Weekly signal = current position state {-1, 0, +1} on USD/XXX from
  simulating the chosen parameters over the trailing window.

Outputs:
  analysis/tech_bollinger_signal.csv  (position on USD/XXX; flip sign for
                                       local-currency score)
  analysis/tech_bollinger_diagnostics.csv
"""
import os
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
OUT = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']

GRID_P = [8, 13, 26, 52]              # band window, weeks
GRID_Q = [1.5, 2.0, 2.5]              # outer (exit) band, std devs
GRID_R = [0.5, 0.75, 1.0]             # inner (entry) band, std devs
DD_LIMIT = 0.04                       # peak drawdown circuit breaker
FIT_WIN = 156                         # 3y selection window
RESELECT = 13                         # re-select params every quarter
MIN_TRADES = 3


def simulate(close, p, q, r, dd=DD_LIMIT):
    """Run the band strategy on a price path. Returns (weekly strategy
    returns on USD/XXX, number of entries, final position)."""
    n = len(close)
    sma = pd.Series(close).rolling(p).mean().values
    sd = pd.Series(close).rolling(p).std().values
    up_in, up_out = sma + r * sd, sma + q * sd
    dn_in, dn_out = sma - r * sd, sma - q * sd
    pos = np.zeros(n)
    peak = np.nan
    trades = 0
    for i in range(1, n):
        prev = pos[i - 1]
        c, cp = close[i], close[i - 1]
        state = prev
        if prev == 1:
            peak = max(peak, c)
            trail = up_in[i - 1] - (up_out[i - 1] - up_in[i - 1])
            if c > up_out[i] or c < peak * (1 - dd) or c < trail:
                state = 0
        elif prev == -1:
            peak = min(peak, c)
            trail = dn_in[i - 1] + (dn_in[i - 1] - dn_out[i - 1])
            if c < dn_out[i] or c > peak * (1 + dd) or c > trail:
                state = 0
        if state == 0 and not np.isnan(up_in[i]) and not np.isnan(up_in[i - 1]):
            if cp < up_in[i - 1] and c > up_in[i]:
                state, peak, trades = 1, c, trades + 1
            elif cp > dn_in[i - 1] and c < dn_in[i]:
                state, peak, trades = -1, c, trades + 1
        pos[i] = state
    ret = np.diff(np.log(close), prepend=np.nan)
    strat = np.roll(pos, 1) * ret          # position held from t-1 earns t's return
    strat[0] = 0.0
    return np.nan_to_num(strat), trades, pos[-1]


def sharpe(x):
    s = np.nanstd(x)
    return np.nan if s == 0 else np.nanmean(x) / s * np.sqrt(52)


def main():
    spot = pd.read_csv(f'{DATA}/L1_spot_usd_asia9.csv', index_col=0, parse_dates=True)
    signal = pd.DataFrame(index=spot.index, columns=ASIA9, dtype=float)
    chosen = {}

    for ccy in ASIA9:
        s = spot[ccy].dropna()
        v = s.values
        idx = s.index
        params = None
        next_fit = FIT_WIN
        for k in range(FIT_WIN, len(v)):
            if k >= next_fit:                       # (re-)select on trailing window
                win = v[k - FIT_WIN:k + 1]
                best, best_sh = None, -np.inf
                for p in GRID_P:
                    for q in GRID_Q:
                        for r in GRID_R:
                            if r >= q:
                                continue
                            st, tr, _ = simulate(win, p, q, r)
                            if tr < MIN_TRADES:
                                continue
                            sh = sharpe(st)
                            if pd.notna(sh) and sh > best_sh:
                                best_sh, best = sh, (p, q, r)
                if best is not None:
                    params = best
                next_fit = k + RESELECT
            if params is None:
                continue
            _, _, endpos = simulate(v[k - FIT_WIN:k + 1], *params)
            signal.loc[idx[k], ccy] = endpos
        chosen[ccy] = params

    signal = signal.astype(float)

    # walk-forward performance of the technical signal alone (spot only)
    ret = np.log(spot[ASIA9]).diff()
    strat = signal.shift(1) * ret
    rows = []
    for ccy in ASIA9:
        x = strat[ccy].dropna()
        sig = signal[ccy].dropna()
        share_active = float((sig != 0).mean())
        rows.append([ccy, str(x.index.min().date()) if len(x) else None,
                     round(sharpe(x.values), 2) if len(x) else np.nan,
                     round(share_active, 2),
                     int(sig.iloc[-1]) if len(sig) else None,
                     str(chosen[ccy])])
    port = strat.mean(axis=1).dropna()             # naive equal-weight across ccys
    diag = pd.DataFrame(rows, columns=['currency', 'signal_start', 'walkfwd_sharpe',
                                       'share_weeks_active', 'current_signal_usdxxx',
                                       'current_params_pqr'])

    os.makedirs(OUT, exist_ok=True)
    signal.index.name = 'date'
    signal.to_csv(f'{OUT}/tech_bollinger_signal.csv')
    diag.to_csv(f'{OUT}/tech_bollinger_diagnostics.csv', index=False)
    print(diag.to_string(index=False))
    print(f"\nequal-weight basket of the 9 walk-forward signals: "
          f"Sharpe {sharpe(port.values):.2f} over {port.index.min().date()} -> {port.index.max().date()}")
    print("note: spot-only returns, before carry and costs; sign is on USD/XXX")


if __name__ == '__main__':
    main()
