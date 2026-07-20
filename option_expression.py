"""
Option-expression test: same weekly signals, three ways to hold them.

  forward : short/long USD/XXX 1W forward (current model)  -> ret = s*(-dlog X + carry)
  atm     : buy 1W ATM option in the signal direction, hold to expiry
  d25     : buy 1W 25-delta option in the signal direction

Pricing: Black-76 on the 1W forward F = S*exp(weekly carry), T=1/52,
vol = 1W ATM adjusted by the 25d risk reversal for the traded wing
(sigma_USDput = ATM - RR/2, sigma_USDcall = ATM + RR/2; butterfly not
available, disclosed). Mid execution, payoff settled at next Friday spot.
Weeks where option inputs are missing are excluded from ALL three legs so
the comparison is apples-to-apples.

Signals: the layer-3 conviction book rebuilt from composite_adaptive.csv
(|z|>0.5, top3 in / top5 stay). Sign only; unit notional per active name.
"""
import numpy as np
import pandas as pd
from scipy.stats import norm

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
START = '2014-01-01'
T = 1 / 52


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def sharpe(r):
    r = pd.Series(r).dropna()
    return r.mean() / r.std() * np.sqrt(52) if len(r) > 20 and r.std() > 0 else np.nan


def b76(F, K, sigma, kind):
    if sigma <= 0 or F <= 0 or K <= 0:
        return np.nan
    v = sigma * np.sqrt(T)
    d1 = (np.log(F / K) + 0.5 * v * v) / v
    d2 = d1 - v
    if kind == 'call':
        return F * norm.cdf(d1) - K * norm.cdf(d2)
    return K * norm.cdf(-d2) - F * norm.cdf(-d1)


def strike_from_delta(F, sigma, delta, kind):
    v = sigma * np.sqrt(T)
    if kind == 'call':                       # N(d1) = delta
        d1 = norm.ppf(delta)
        return F * np.exp(0.5 * v * v - v * d1)
    d1 = norm.ppf(1 - delta)                 # put: N(-d1)=delta
    return F * np.exp(0.5 * v * v - v * d1)


def main():
    spot = load('L1_spot_usd_asia9')
    dlog = np.log(spot).diff()
    carry_w = (load('L1_carry_1m_ann') / 100 / 52)
    atm1w = load('L1_vol_implied_atm_1w') / 100
    rr1w = load('L1_rr25_1m') * 0 + load('L1_rr25_1w')   # keep 1W RR
    rr1w = rr1w / 100
    comp = load('composite_adaptive', AN)
    dates = comp.index.intersection(spot.index)
    dates = dates[(dates >= START)]

    # rebuild signal sides (layer-3 conviction book)
    prevL, prevS = set(), set()
    side = pd.DataFrame(0.0, index=dates, columns=ASIA9)
    for t in dates:
        s = comp.loc[t].dropna()
        s = s[s.abs() > 0.5]
        if len(s) < 2:
            prevL, prevS = set(), set()
            continue
        rd, ra = s.rank(ascending=False), s.rank(ascending=True)
        L = set(s.index[rd <= 3]) | {c for c in prevL if c in s.index and rd[c] <= 5}
        S = set(s.index[ra <= 3]) | {c for c in prevS if c in s.index and ra[c] <= 5}
        L, S = L - S, S - L
        prevL, prevS = L, S
        for c in L:
            side.loc[t, c] = 1
        for c in S:
            side.loc[t, c] = -1

    rows = []
    sleeves = {}
    for ccy in ASIA9:
        recs = []
        for i, t in enumerate(dates[:-1]):
            s = side.loc[t, ccy]
            if s == 0:
                continue
            S0 = spot.loc[t, ccy] if t in spot.index else np.nan
            S1 = spot[ccy].reindex([dates[i + 1]]).iloc[0]
            cw = carry_w.loc[t, ccy] if (t in carry_w.index and ccy in carry_w) else np.nan
            iv = atm1w.loc[t, ccy] if (t in atm1w.index and ccy in atm1w) else np.nan
            rr = rr1w.loc[t, ccy] if (t in rr1w.index and ccy in rr1w) else np.nan
            if any(pd.isna(x) for x in [S0, S1, cw, iv, rr]) or iv <= 0:
                continue
            F = S0 * np.exp(cw)              # 1W forward on USD/XXX
            fwd_ret = s * (-(np.log(S1 / S0)) + cw)
            # option in signal direction: long local -> USD put; short -> USD call
            kind = 'put' if s > 0 else 'call'
            sig_wing = iv - rr / 2 if kind == 'put' else iv + rr / 2
            sig_wing = max(sig_wing, 0.005)
            # ATM (K=F, use wing vol at ATM ~ iv)
            prem_atm = b76(F, F, iv, kind) / S0
            pay_atm = (max(F - S1, 0) if kind == 'put' else max(S1 - F, 0)) / S0
            K25 = strike_from_delta(F, sig_wing, 0.25, kind)
            prem_25 = b76(F, K25, sig_wing, kind) / S0
            pay_25 = (max(K25 - S1, 0) if kind == 'put' else max(S1 - K25, 0)) / S0
            recs.append([t, fwd_ret, pay_atm - prem_atm, pay_25 - prem_25, prem_25])
        if len(recs) < 30:
            continue
        df = pd.DataFrame(recs, columns=['t', 'fwd', 'atm', 'd25', 'prem25']).set_index('t')
        sleeves[ccy] = df
        rows.append([
            ccy, len(df),
            f"{df['fwd'].mean()*100:+.3f}%", f"{(df['fwd']>0).mean():.0%}", round(sharpe(df['fwd']), 2),
            f"{df['atm'].mean()*100:+.3f}%", f"{(df['atm']>0).mean():.0%}", round(sharpe(df['atm']), 2),
            f"{df['d25'].mean()*100:+.3f}%", f"{(df['d25']>0).mean():.0%}", round(sharpe(df['d25']), 2),
            f"{df['prem25'].mean()*100:.3f}%"])

    out = pd.DataFrame(rows, columns=[
        'ccy', 'n', 'fwd_exp', 'fwd_hit', 'fwd_sh',
        'atm_exp', 'atm_hit', 'atm_sh',
        'd25_exp', 'd25_hit', 'd25_sh', 'avg_25d_prem'])
    print(out.to_string(index=False))

    # portfolio level: equal-notional across active names each week
    port = {}
    for k in ['fwd', 'atm', 'd25']:
        wk = {}
        for ccy, df in sleeves.items():
            for t, v in df[k].items():
                wk.setdefault(t, []).append(v)
        port[k] = pd.Series({t: np.mean(v) for t, v in wk.items()}).sort_index()
    print('\nportfolio (equal notional across active names):')
    for k, nm in [('fwd', 'forward'), ('atm', 'ATM option'), ('d25', '25-delta option')]:
        r = port[k]
        print(f"  {nm:16s} Sharpe {sharpe(r):.2f}  exp/week {r.mean()*100:+.3f}%  "
              f"hit {(r>0).mean():.0%}  worst wk {r.min()*100:.2f}%")

    pd.concat({k: v for k, v in port.items()}, axis=1).to_csv(f'{AN}/option_expression_curves.csv')
    out.to_csv(f'{AN}/option_expression_results.csv', index=False)


if __name__ == '__main__':
    main()
