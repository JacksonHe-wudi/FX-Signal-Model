"""
Layer 3 - three-pillar composite, regime-adaptive weights, trigger tests,
net-of-cost weekly backtest (positions held via 1W forward roll).

Return convention (long local currency, per week):
    total return = -dlog(USD/XXX) + implied 1M carry (annualized %) / 52
i.e. spot move plus the forward points received/paid on the weekly roll,
approximated by the 1M forward-implied carry curve point (validated against
the Citi carry series; the raw pip-scale of the 1W points table is left as
a diagnostic because vendor pip conventions are unreliable).

Pillars (user architecture):
    Fundamental = CA YoY + ESI 4w chg + (-CDS 4w chg) + carry/realized vol
    Momentum    = 12w spot momentum + real-money position + real-money flow z
    Technical   = walk-forward Bollinger signal
    (Risk-reversal z is tested both as a state variable and as an overlay.)

Adaptive weights: per pillar, expanding mean of its weekly IC computed in
weeks with the SAME regime state (USD trend x risk on/off), shrunk 50%
towards equal weight, floored at zero. Fully recursive.

Triggers compared:
    A. always-in rank portfolio (long top 3 / short bottom 3)
    B. conviction filter: only |composite z| > 0.5 traded
    C. cost hurdle: expected return (score x recursive slope) must exceed
       the round-trip cost estimate

Benchmarks: carry-only ranking; static equal pillar weights.
Costs: placeholder half-spreads (bp) pending desk numbers.
"""
import os
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
START = '2014-01-01'
HALF_SPREAD_BP = {'CNH': 2, 'SGD': 2, 'THB': 3, 'KRW': 3, 'TWD': 4,
                  'INR': 4, 'MYR': 5, 'PHP': 6, 'IDR': 8}
VOL_TARGET = 0.05


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def zsec(df):
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def xs_ic(score, fwd_ret):
    """weekly cross-sectional spearman IC series"""
    out = {}
    for t in score.index.intersection(fwd_ret.index):
        s = score.loc[t].dropna()
        r = fwd_ret.loc[t].reindex(s.index).dropna()
        s = s.loc[r.index]
        if len(s) >= 5 and s.nunique() >= 3:
            ic = spearmanr(s, r)[0]
            if pd.notna(ic):
                out[t] = ic
    return pd.Series(out)


def perf(rets, name):
    r = rets.dropna()
    ann = r.mean() * 52
    vol = r.std() * np.sqrt(52)
    sh = ann / vol if vol > 0 else np.nan
    dd = (r.cumsum() - r.cumsum().cummax()).min()
    return [name, f'{ann:.2%}', f'{vol:.2%}', round(sh, 2), f'{dd:.2%}',
            f'{(r > 0).mean():.0%}', str(r.index.min().date()), len(r)]


def main():
    spot = load('L1_spot_usd_asia9')
    dlog = np.log(spot).diff()
    carry = load('L1_carry_1m_ann')                       # annualized %
    carry_w = carry / 100 / 52
    # total local-ccy return over week t -> t+1, using carry known at t
    tot_next = (-dlog + carry_w.shift(1)).shift(-1)
    spot_next = (-dlog).shift(-1)

    # ---------------- pillar member signals (local-ccy scores) ----------------
    esi = load('L1_esi').rename(columns={'CNY': 'CNH'})
    members = {
        'Fundamental': {
            'ca_yoy': load('L2_ca_yoy_usdbn'),
            'esi_chg': esi.diff(4),
            'cds_chg_neg': -load('L1_cds_5y').diff(4).rename(columns={'INR_SBI': 'INR'}),
            'carry_vol': load('L2_carry_to_realvol')},
        'Momentum': {
            'spot_mom': -load('L2_mom_12w_ex1w'),
            'pi_position': load('L1_pi_realmoney'),
            'pi_flow': load('L1_pi_rm_flow_z')},
        'Technical': {
            'bollinger': -load('tech_bollinger_signal', AN)},
    }
    rr_overlay = -load('L2_rr25_1m_z_52w')

    # within-pillar ADAPTIVE member weights: expanding trailing total-return IC
    # (floor 0) so members that stop working self-deweight, fully recursively
    spot_all = load('L1_spot_usd_asia9')
    tot_for_ic = (-np.log(spot_all).diff() +
                  (load('L1_carry_1m_ann') / 100 / 52).shift(1)).shift(-1)
    pillar_scores = {}
    for p, mem in members.items():
        zs = {k: zsec(m.reindex(columns=ASIA9)) for k, m in mem.items()}
        trail = {k: xs_ic(z, tot_for_ic).rolling(104, min_periods=30).mean().shift(1)
                 for k, z in zs.items()}
        idx = sorted(set().union(*[z.index for z in zs.values()]))
        num, den = None, None
        for k, z in zs.items():
            w = trail[k].reindex(idx).clip(lower=0).fillna(0)
            if len(zs) == 1:
                w = pd.Series(1.0, index=idx)
            term = z.reindex(idx).mul(w, axis=0)
            num = term if num is None else num.add(term, fill_value=0)
            den = w if den is None else den.add(w, fill_value=0)
        eqw = pd.concat(list(zs.values())).groupby(level=0).mean().reindex(idx)
        sc = num.div(den.replace(0, np.nan), axis=0)
        pillar_scores[p] = sc.where(den.gt(0), eqw)     # fallback equal weight

    # ---------------- IC retest on TOTAL returns (carry-type verdict) ---------
    retest = {'carry_to_realvol': load('L2_carry_to_realvol'),
              'implied_slope': load('L2_implied_yield_slope_12m_1m'),
              'ydiff_2y': None, 'ydiff_10y': None}
    y2, y10 = load('L2_ydiff_2y_vs_us'), load('L1_govt_yield_10y')
    for df in (y2, y10):
        if 'IDR_GT' in df.columns:
            df['IDR'] = df['IDR_GT'].combine_first(df['IDR_BV'])
    retest['ydiff_2y'] = y2
    retest['ydiff_10y'] = y10.drop(columns=['US']).sub(y10['US'], axis=0)
    print('=== carry-type signals: spot-only vs total-return IC (2014+) ===')
    for nm, sig in retest.items():
        sig = sig.reindex(columns=[c for c in ASIA9 if c in sig.columns]).loc[START:]
        a = xs_ic(sig, spot_next.loc[START:])
        b = xs_ic(sig, tot_next.loc[START:])
        print(f'  {nm:18s} spot IC {a.mean():+.4f}  ->  total IC {b.mean():+.4f}  (n={len(b)})')

    # ---------------- regime-adaptive pillar weights --------------------------
    reg = load('L1_regime')
    state = (reg['DXY'] > reg['DXY'].rolling(52).mean()).astype(int) * 2 + \
            (reg['MXWO'] < reg['MXWO'].rolling(52).mean()).astype(int)
    pillar_ic = {p: xs_ic(s, tot_next) for p, s in pillar_scores.items()}

    dates = tot_next.loc[START:].index
    W = pd.DataFrame(index=dates, columns=list(members), dtype=float)
    for t in dates:
        st = state.reindex([t]).iloc[0] if t in state.index else np.nan
        w = {}
        for p, ics in pillar_ic.items():
            past = ics[ics.index < t]
            same = past[state.reindex(past.index) == st] if pd.notna(st) else past
            est = same.mean() if len(same) >= 30 else past.tail(104).mean()
            w[p] = max(est if pd.notna(est) else 0.0, 0.0)
        tot = sum(w.values())
        n = len(w)
        for p in w:                                     # 25% shrink to equal
            adaptive = w[p] / tot if tot > 0 else 1 / n
            W.loc[t, p] = 0.25 * (1 / n) + 0.75 * adaptive

    # ---------------- composites ---------------------------------------------
    def composite(weights):
        c = None
        for p, s in pillar_scores.items():
            term = s.reindex(dates).mul(weights[p] if isinstance(weights, dict)
                                        else weights[p], axis=0)
            c = term if c is None else c.add(term, fill_value=0)
        return zsec(c)

    comp_adapt = composite({p: W[p] for p in members}).rolling(3, min_periods=1).mean()
    for nm, cc in [('comp_adapt', None)]:
        pass
    comp_equal = composite({p: pd.Series(1 / 3, index=dates) for p in members}).rolling(3, min_periods=1).mean()
    comp_carry = zsec(load('L2_carry_to_realvol').reindex(dates)[ASIA9])
    comp_rr = zsec(comp_adapt * 0.75 + zsec(rr_overlay.reindex(dates)) * 0.25).rolling(3, min_periods=1).mean()

    # ---------------- portfolio engine ----------------------------------------
    atm = load('L1_vol_implied_atm_1m').reindex(dates)
    cost = pd.Series(HALF_SPREAD_BP) / 1e4

    def run(comp, trigger='A'):
        wgt = pd.DataFrame(0.0, index=dates, columns=ASIA9)
        beta = None
        prevL, prevS = set(), set()
        for i, t in enumerate(dates):
            s = comp.loc[t].dropna()
            if len(s) < 6:
                continue
            if trigger == 'B':
                s = s[s.abs() > 0.5]
            if trigger == 'C':
                hist_s = comp.loc[:t].iloc[:-1].stack()
                hist_r = tot_next.loc[:t].iloc[:-1].stack()
                j = hist_s.index.intersection(hist_r.index)
                if len(j) > 300:
                    beta = np.polyfit(hist_s.loc[j], hist_r.loc[j], 1)[0]
                if beta is None:
                    continue
                exp_ret = s * beta
                s = s[exp_ret.abs() > 1 * cost.reindex(s.index)]
            if len(s) < 2:
                prevL, prevS = set(), set()
                continue
            # hysteresis: enter on top/bottom 3, stay while still in top/bottom 5
            r_desc = s.rank(ascending=False)
            r_asc = s.rank(ascending=True)
            longs = set(s.index[r_desc <= 3]) | {c for c in prevL
                                                if c in s.index and r_desc[c] <= 5}
            shorts = set(s.index[r_asc <= 3]) | {c for c in prevS
                                                 if c in s.index and r_asc[c] <= 5}
            longs, shorts = longs - shorts, shorts - longs
            prevL, prevS = longs, shorts
            iv = 1 / atm.loc[t].reindex(list(longs | shorts)).replace(0, np.nan)
            iv = iv.fillna(iv.mean() if pd.notna(iv.mean()) else 1.0)
            for side, idx in [(1, list(longs)), (-1, list(shorts))]:
                if idx:
                    v = iv.reindex(idx)
                    wgt.loc[t, idx] = side * v / v.sum()
        # partial adjustment: move 1/3 of the way to target each week
        exec_w = wgt.copy()
        prev = np.zeros(len(ASIA9))
        for t in dates:
            prev = prev + (wgt.loc[t].values - prev) / 3.0
            exec_w.loc[t] = prev
        wgt = exec_w
        gross = (wgt * tot_next.reindex(dates)).sum(axis=1)
        tc = (wgt.diff().abs() * cost).sum(axis=1)
        net = gross - tc
        lev = (VOL_TARGET / (net.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
        return net * lev.fillna(1.0), gross

    print('\ncomposite total-return ICs:')
    for nm, cc in [('adaptive', comp_adapt), ('equal', comp_equal),
                   ('carry-only', comp_carry), ('adaptive+RR', comp_rr)]:
        s = xs_ic(cc, tot_next)
        print(f'  {nm:12s} IC {s.mean():+.4f}  t {s.mean()/s.std()*np.sqrt(len(s)):.2f}')

    results = []
    curves = {}
    for nm, comp, trig in [('Adaptive 3-pillar (A always-in)', comp_adapt, 'A'),
                           ('Adaptive + RR overlay (A)', comp_rr, 'A'),
                           ('Adaptive (B conviction>0.5)', comp_adapt, 'B'),
                           ('Adaptive (C cost hurdle)', comp_adapt, 'C'),
                           ('Static equal pillars (A)', comp_equal, 'A'),
                           ('Carry-only benchmark (A)', comp_carry, 'A')]:
        scaled, gross = run(comp, trig)
        row = perf(scaled.loc[START:], nm)
        g = gross.loc[START:].dropna()
        row.append(round(g.mean() * 52 / (g.std() * np.sqrt(52)), 2) if g.std() > 0 else np.nan)
        results.append(row)
        curves[nm] = scaled.loc[START:]

    res = pd.DataFrame(results, columns=['strategy', 'ann_ret', 'ann_vol', 'sharpe',
                                         'max_dd', 'hit', 'start', 'n_weeks',
                                         'gross_sharpe'])
    os.makedirs(AN, exist_ok=True)
    res.to_csv(f'{AN}/backtest_results.csv', index=False)
    pd.DataFrame(curves).to_csv(f'{AN}/backtest_curves.csv')
    W.to_csv(f'{AN}/pillar_weights.csv')
    print('\n=== net-of-cost backtests (5% vol target, weekly 1W-forward roll) ===')
    print(res.to_string(index=False))
    print('\ncurrent pillar weights:', W.iloc[-1].round(2).to_dict())
    print('current composite (adaptive):')
    print(comp_adapt.iloc[-1].dropna().sort_values(ascending=False).round(2).to_string())


if __name__ == '__main__':
    main()
