#!/usr/bin/env python3
"""
FX ONE - the whole system in a single file.

  MODEL / BACKTEST (CLI)
    python3 fx_one.py FX_Model_Data.xlsx --outdir out     # clean + backtest
    python3 fx_one.py --clean out/clean.csv.gz            # re-run w/o xlsx
    python3 fx_one.py fetch [cftc bis fred imf equity]    # refresh free
                                                          # external data
  DASHBOARD
    streamlit run fx_one.py

Contains: the data cleaning from the raw workbook, the three sleeves
(A cross-sectional RV, B EM-beta vs funding basket, D CNH fixing bias), the
backtest with carry/spot attribution and charts, the weekly order sheet, the
free-external-data fetchers (CFTC/BIS/FRED/IMF/Yahoo), and the full
Velocity-styled dashboard (entry verdicts, per-currency deep dive, strategy
backtester, ML lab, PCA drivers, strategy leaderboard + auto-selection
verdict, news, and the all-currency fair-value/technical-bounds grid).

Optional research artifacts read from analysis/*.csv when present (produced
by ml_factor.py, pca_drivers.py, the STFV precompute); the corresponding
dashboard tabs degrade gracefully with instructions when absent.

Dependencies: pandas numpy openpyxl matplotlib; dashboard adds streamlit
plotly; news tab adds feedparser; ML tab adds scikit-learn.
"""

# %% PART 1 - MODEL, BACKTEST, ORDER SHEET %%

import argparse
import datetime
import os
import sys

import numpy as np
import pandas as pd

# ============================================================ 0. CONFIG =====
START = pd.Timestamp('2013-01-01')
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
EM5 = ['BRL', 'MXN', 'CLP', 'PLN', 'HUF']
# MYR is EXCLUDED from trading: Citi cannot trade MYR NDF for us. Its data is
# still cleaned (ALLCCY) so it can be re-admitted by editing this one line.
# Side note: MYR also has the worst data quality of the 14 (pip-scale factor
# drifting 1554..8551), so dropping it was already the marginal call.
TRADED = [c for c in ASIA9 + EM5 if c != 'MYR']     # 13 tradable
FUNDING = ['EUR', 'JPY', 'CAD']
ALLCCY = ASIA9 + EM5 + FUNDING

WEEKLY_FFILL, MONTHLY_FFILL = 2, 8
VOL_TARGET = 0.05           # per-sleeve annualized vol target
MAX_LEV = 3.0
CONVICTION = 0.5            # |z| gate for sleeve A
TOP_IN, TOP_STAY = 3, 5     # hysteresis
W_CARRY, W_ESI = 2.0, 1.0   # composite prior weights (NOT fitted)
SMOOTH = 3                  # weeks
RISK = {'A': 0.50, 'B': 0.25, 'D': 0.25}
FUND_BASKET = {'CAD': 0.50, 'USD': 0.5 / 3, 'EUR': 0.5 / 3, 'JPY': 0.5 / 3}
D_VOLZ_MAX = 1.5            # sleeve D calm gate


# ==================================================== 1. EXCEL -> CLEAN =====
def _load_cvts(ws, header_row=3, data_row=4, date_idx=1):
    """Citi Velocity CVTSHIST layout: labels on header_row, dates in column B."""
    hdr = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    cols = [(i, h.replace(' - CLOSE', '').strip()) for i, h in enumerate(hdr)
            if i != date_idx and isinstance(h, str) and h.strip() and 'Date' not in h]
    recs = []
    for row in ws.iter_rows(min_row=data_row, values_only=True):
        d = row[date_idx] if date_idx < len(row) else None
        if isinstance(d, datetime.datetime):
            recs.append([d] + [row[i] if i < len(row) else None for i, _ in cols])
    df = pd.DataFrame(recs, columns=['date'] + [c for _, c in cols])
    return df.set_index('date').sort_index().apply(pd.to_numeric, errors='coerce')


def _col_series(ws, date_idx, val_idx, data_row):
    """One (date column, value column) pair anywhere on a sheet."""
    rec = {}
    for row in ws.iter_rows(min_row=data_row, values_only=True):
        d = row[date_idx] if date_idx < len(row) else None
        if isinstance(d, datetime.datetime):
            rec[d] = row[val_idx] if val_idx < len(row) else None
    return pd.to_numeric(pd.Series(rec).sort_index(), errors='coerce')


def _auto_blocks(ws, header_row, data_row, c0, c1, name_fn):
    """Side-by-side (date col, value cols) blocks; each value col binds to the
    nearest date column at or to its left."""
    hdr = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    scan = []
    for i, row in enumerate(ws.iter_rows(min_row=data_row, values_only=True)):
        scan.append(row)
        if i + 1 >= 15:
            break
    is_date = {}
    for c in range(c0, c1):
        vals = [r[c] for r in scan if c < len(r) and r[c] is not None]
        is_date[c] = bool(vals) and \
            sum(isinstance(v, datetime.datetime) for v in vals) > len(vals) / 2
    date_cols = sorted([c for c in range(c0, c1) if is_date.get(c)])
    out = {}
    for c in range(c0, c1):
        if is_date.get(c):
            continue
        h = hdr[c] if c < len(hdr) else None
        if h is None or (isinstance(h, str) and not h.strip()):
            continue
        left = [d for d in date_cols if d <= c]
        if not left:
            continue
        try:
            name = name_fn(c, h)
        except Exception:
            name = None
        if name:
            out[name] = _col_series(ws, left[-1], c, data_row).dropna()
    return out


def _to_friday(series_map, ffill_limit):
    """As-of align onto the Friday grid: each observation is stamped on the
    FIRST Friday >= its own date, so nothing is ever known early."""
    series_map = {k: v for k, v in series_map.items() if len(v)}
    lo = min(s.index.min() for s in series_map.values())
    hi = max(s.index.max() for s in series_map.values())
    grid = pd.date_range(lo, hi, freq='W-FRI')
    out = {}
    for name, s in series_map.items():
        s = s[~s.index.duplicated(keep='last')].sort_index()
        fri = s.index + pd.to_timedelta((4 - s.index.dayofweek) % 7, unit='D')
        out[name] = pd.Series(s.values, index=fri).groupby(level=0).last() \
            .reindex(grid).ffill(limit=ffill_limit)
    return pd.DataFrame(out, index=grid)


def _wide(df, lim=WEEKLY_FFILL):
    return _to_friday({c: df[c].dropna() for c in df.columns}, lim)


def clean(xlsx_path):
    """Raw workbook -> the eight tables the live model actually consumes."""
    import openpyxl
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    T = {}

    # ---- spot, harmonized to USD/XXX --------------------------------------
    ws = wb['Top 30 FX Spot']
    sp = _load_cvts(ws, header_row=5, data_row=6)
    keep, names, seen = [], [], set()
    for i, c in enumerate(sp.columns):                 # FX.SPOT.EUR.USD.CITI
        if not (isinstance(c, str) and c.startswith('FX.SPOT')):
            continue
        base, quote = c.split('.')[2], c.split('.')[3]
        ccy = base if base != 'USD' else quote
        if ccy in seen:                                # CLP is listed twice
            continue
        seen.add(ccy)
        keep.append(i)
        names.append((ccy, base != 'USD'))
    sp = sp.iloc[:, keep].copy()
    sp.columns = [n for n, _ in names]
    for n, inv in names:
        if inv:                                        # EUR/GBP/AUD -> USD/XXX
            sp[n] = 1.0 / sp[n]
    myr = _col_series(ws, 32, 33, 6).dropna()          # MYR sits in its own block
    if len(myr):
        sp = sp.join(pd.DataFrame({'MYR': myr}), how='outer')
    T['spot'] = _wide(sp)

    # ---- carry = 1M implied yield - 1M SOFR (CIP-consistent) --------------
    ws = wb['Govt & FX Implied Yield']
    iy = {}
    for k, c in {'KRW': 110, 'THB': 111, 'INR': 112, 'PHP': 113, 'CNH': 114,
                 'IDR': 115, 'MYR': 116, 'SGD': 117, 'TWD': 118}.items():
        iy[k] = _col_series(ws, 109, c, 7)
    for k, c in {'PLN': 139, 'BRL': 140, 'HUF': 141, 'MXN': 142,
                 'EUR': 143, 'JPY': 144, 'CAD': 145}.items():
        iy[k] = _col_series(ws, 129, c, 7)
    iy['CLP'] = _col_series(ws, 148, 149, 7)
    sofr = _to_friday({'SOFR': _col_series(ws, 129, 146, 7)}, WEEKLY_FFILL)['SOFR']
    iyf = _to_friday(iy, WEEKLY_FFILL)
    carry = iyf.sub(iyf.index.to_series().map(sofr), axis=0)
    T['carry'] = carry[[c for c in ALLCCY if c in carry.columns]]

    # ---- realized vol ------------------------------------------------------
    cv = wb['Carry to Vol']
    cvd = _load_cvts(cv, header_row=6, data_row=7)
    ren = {}
    for c in cvd.columns:                    # FX.VOL.USD.BRL.ATM.1M.REALISED
        if isinstance(c, str) and 'REALIS' in c:
            p = c.split('.')
            ren[c] = p[3] if p[2] == 'USD' else p[2]
    rv = _wide(cvd[list(ren)].rename(columns=ren))
    sup = _col_series(cv, 37, 42, 7).dropna()          # Bloomberg MYR supplement
    if len(sup):
        s = _to_friday({'MYR': sup}, WEEKLY_FFILL)['MYR']
        rv['MYR'] = rv['MYR'].combine_first(s) if 'MYR' in rv else s
    T['rvol'] = rv[[c for c in ALLCCY if c in rv.columns]]

    # ---- 1M ATM implied vol ------------------------------------------------
    ws = wb['Vol & RR']
    vr = _load_cvts(ws, header_row=7, data_row=8)
    ren = {}
    for c in vr.columns:
        if not (isinstance(c, str) and c.startswith('FX.VOL')):
            continue
        p = c.split('.')
        if p[4] == 'ATM' and p[5] == '1M':
            ren[c] = p[3] if p[2] == 'USD' else p[2]
    atm = _wide(vr[list(ren)].rename(columns=ren))
    for ccy, ci in {'MYR': 39, 'THB': 41, 'IDR': 43, 'TWD': 45}.items():
        # the main Citi block is frozen for these four - Bloomberg wins
        s = _col_series(ws, 37, ci, 8).dropna()
        if len(s):
            atm[ccy] = _to_friday({ccy: s}, WEEKLY_FFILL)[ccy].reindex(atm.index)
    T['atm'] = atm[[c for c in ALLCCY if c in atm.columns]]

    # ---- forward points (only used to pick 1W vs 1M at execution) ----------
    ws = wb['FX Forward']

    def bbg(col, h):
        pair, tenor = str(h).split()                   # "USDBRL 1W"
        return f'{pair[3:]}_{tenor}' if pair != 'EURUSD' else f'EUR_{tenor}'

    prim = _auto_blocks(ws, 7, 8, 37, 120, bbg)
    bkp = _load_cvts(ws, header_row=4, data_row=5)
    bmap = {}
    for c in bkp.columns:                    # FX.FORWARD.x.USD.BRL.1W
        if isinstance(c, str) and c.startswith('FX.FORWARD'):
            p = c.split('.')
            bmap[c] = f'{p[4] if p[3] == "USD" else p[3]}_{p[5]}'
    bkp = bkp[list(bmap)].rename(columns=bmap)
    for tenor in ['1W', '1M']:
        pc = {k.split('_')[0]: v for k, v in prim.items() if k.endswith('_' + tenor)}
        pdf = _to_friday(pc, WEEKLY_FFILL) if pc else pd.DataFrame()
        bc = [c for c in bkp.columns if c.endswith('_' + tenor)]
        bdf = _wide(bkp[bc].rename(columns=lambda c: c.split('_')[0])) if bc else pd.DataFrame()
        m = pdf.combine_first(bdf) if len(pdf) else bdf
        T[f'fwd_{tenor.lower()}'] = m[[c for c in ALLCCY if c in m.columns]]

    # ---- economic surprise index ------------------------------------------
    sh = 'Economics Surprise' if 'Economics Surprise' in wb.sheetnames \
        else 'Economics Surpirse'
    es = _load_cvts(wb[sh], header_row=3, data_row=4)
    ren = {}
    for c in es.columns:
        if not isinstance(c, str):
            continue
        p = c.split('.')
        si = [x for x in p if x.startswith('SI_')]
        if p[2] == 'ESI' and si:
            code = si[-1].replace('SI_', '')
            ren[c] = 'CNH' if code in ('CNY', 'CN') else code
    T['esi'] = _wide(es[list(ren)].rename(columns=ren))

    # ---- CNY fix and the Bloomberg fixing survey (sleeve D) ----------------
    CF = {'CNYMUSD': 'CNY_FIX', 'FCCNYFIX': 'BBG_CNY_FIX'}
    cf = _auto_blocks(wb['Countries Factor'], 6, 8, 1, 34,
                      lambda c, h: CF.get(str(h).split()[0]))
    T['fix'] = _to_friday(cf, WEEKLY_FFILL) if cf else pd.DataFrame()

    for k in T:
        T[k] = T[k][T[k].index >= START].dropna(how='all')
    return T


def save_clean(T, path):
    long = pd.concat({k: v.stack(future_stack=True) for k, v in T.items()}) \
        .rename('value').reset_index()
    long.columns = ['table', 'date', 'col', 'value']
    long.to_csv(path, index=False, compression='infer')


def load_clean(path):
    long = pd.read_csv(path, parse_dates=['date'])
    return {k: g.pivot(index='date', columns='col', values='value')
            for k, g in long.groupby('table')}


# ======================================================== 2. SIGNALS ========
def zsec(df):
    """Cross-sectional z-score, one row at a time."""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def composite(T):
    """2:1 carry/ESI blend, renormalized by the members actually present."""
    cv = (T['carry'][TRADED] /
          T['rvol'][TRADED].clip(lower=1.0)).reindex(columns=TRADED)
    esi = (T['esi'] - T['esi'].shift(4)).reindex(columns=TRADED)
    zc, ze = zsec(cv), zsec(esi.reindex(cv.index))
    num = W_CARRY * zc.fillna(0) + W_ESI * ze.fillna(0)
    den = W_CARRY * zc.notna() + W_ESI * ze.notna()
    return zsec((num / den.replace(0, np.nan)).rolling(SMOOTH).mean())


def build_book(comp, atm):
    """Conviction gate + hysteresis + 1/ATM-vol legs. Returns weights at t,
    to be applied to the return realized at t+1."""
    W = pd.DataFrame(0.0, index=comp.index, columns=comp.columns)
    prevL, prevS = set(), set()
    for t in comp.index:
        s = comp.loc[t].dropna()
        s = s[s.abs() > CONVICTION]
        if len(s) < 2:
            prevL, prevS = set(), set()
            continue
        rd, ra = s.rank(ascending=False), s.rank(ascending=True)
        L = set(s.index[rd <= TOP_IN]) | {c for c in prevL
                                          if c in s.index and rd[c] <= TOP_STAY}
        S = set(s.index[ra <= TOP_IN]) | {c for c in prevS
                                          if c in s.index and ra[c] <= TOP_STAY}
        L, S = L - S, S - L
        prevL, prevS = L, S
        iv = 1.0 / atm.loc[t].reindex(list(L | S)).replace(0, np.nan)
        iv = iv.fillna(iv.mean() if pd.notna(iv.mean()) else 1.0)
        for side, idx in [(1, sorted(L)), (-1, sorted(S))]:
            if idx:
                v = iv.reindex(idx)
                W.loc[t, idx] = side * v / v.sum()
    return W


def vol_target(r):
    """Trailing 52w vol -> leverage, lagged so the scale is knowable ex-ante."""
    lev = (VOL_TARGET / (r.rolling(52).std() * np.sqrt(52))).clip(upper=MAX_LEV)
    return lev.shift(1)


# ========================================== 3. SLEEVES WITH ATTRIBUTION =====
def legs(T):
    """Per-currency weekly spot and carry return components, indexed at the
    week they are REALIZED. carry accrues on the rate known one week earlier.

    Both components are masked to the SAME validity set, so spot + carry is
    exactly the total return of a position held that week. Without the shared
    mask a currency with a spot print but no carry quote would contribute its
    spot move to the attribution while never being tradable - the split would
    then not add up to the book's actual P&L.
    """
    sp = T['spot']
    spot_ret = -np.log(sp).diff()                    # long local ccy vs USD
    carry_ret = (T['carry'].reindex(columns=sp.columns) / 100 / 52).shift(1)
    ok = spot_ret.notna() & carry_ret.notna()
    return spot_ret.where(ok), carry_ret.where(ok)


def sleeve_A(T, jitter=0.0, seed=0):
    """jitter adds tiny noise to the composite before the book is formed.

    That is a robustness knob, not a modelling choice. The |z| > 0.5 gate is a
    hard threshold and the long/short sets carry forward through hysteresis, so
    a name sitting exactly on the threshold in one week flips membership for
    every week after it. Perturbing the composite at machine epsilon - an
    economically meaningless change - moves the full-sample Sharpe across
    roughly [1.05, 1.28]. Any single run is one draw from that band, so the
    headline number is reported as an ensemble mean, never as one path.
    """
    comp = composite(T)
    if jitter:
        rng = np.random.default_rng(seed)
        comp = comp + rng.normal(0.0, jitter, comp.shape)
    atm = T['atm'].reindex(columns=TRADED)
    W = build_book(comp, atm)

    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
        disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)

    spot_ret, carry_ret = legs(T)
    # weights decided at t-1 earn the return realized at t
    Wl = W.mul(mult, axis=0).shift(1).reindex(spot_ret.index)[TRADED]
    s = (Wl * spot_ret[TRADED]).sum(axis=1)
    c = (Wl * carry_ret[TRADED]).sum(axis=1)
    lev = vol_target(s + c)
    return {'spot': s * lev, 'carry': c * lev, 'weights': Wl.mul(lev, axis=0),
            'comp': comp, 'raw_w': W, 'mult': mult}


def sleeve_B(T):
    spot_ret, carry_ret = legs(T)
    iv = (1.0 / T['rvol'][TRADED].clip(lower=1.0)).shift(1)
    w = iv.div(iv.sum(axis=1), axis=0).reindex(spot_ret.index)
    s = (w * spot_ret[TRADED]).sum(axis=1, min_count=8)
    c = (w * carry_ret[TRADED]).sum(axis=1, min_count=8)
    for k, wt in FUND_BASKET.items():
        if k == 'USD':                    # the numeraire leg returns nothing
            continue
        s = s - wt * spot_ret[k]
        c = c - wt * carry_ret[k]
    lev = vol_target(s + c)
    fund = pd.DataFrame({k: -wt * lev for k, wt in FUND_BASKET.items()
                         if k != 'USD'})
    return {'spot': s * lev, 'carry': c * lev,
            'weights': w.mul(lev, axis=0), 'fund_w': fund}


def sleeve_D(T):
    spot_ret, carry_ret = legs(T)
    fix = T.get('fix')
    idx = spot_ret.index
    if fix is None or 'BBG_CNY_FIX' not in fix.columns:
        z = pd.Series(0.0, index=idx)
        return {'spot': z, 'carry': z, 'signal': z}
    ccf = ((fix['CNY_FIX'] - fix['BBG_CNY_FIX']) / fix['CNY_FIX'] * 1e4) \
        .reindex(idx)
    trend = np.log(T['spot']['CNH']).diff(4).reindex(idx)
    a = T['atm']['CNH'].reindex(idx)
    vz = (a - a.rolling(104, min_periods=40).mean()) / \
        a.rolling(104, min_periods=40).std()
    sig = np.sign(ccf).where((ccf * trend < 0) & (vz < D_VOLZ_MAX), 0.0).fillna(0.0)
    sig = sig.shift(1)                    # act on next week's return
    s = sig * spot_ret['CNH']
    c = sig * carry_ret['CNH']
    lev = vol_target((s + c).where(ccf.shift(1).notna()))
    return {'spot': s * lev, 'carry': c * lev, 'signal': sig, 'lev': lev}


# ======================================================== 4. BACKTEST =======
def scale5(r, vol=VOL_TARGET):
    r = r.dropna()
    return r * vol / (r.std() * np.sqrt(52))


def stats(r):
    r = r.dropna()
    cum = r.cumsum()
    return dict(sharpe=r.mean() / r.std() * np.sqrt(52),
                ann=52 * r.mean() * 100,
                vol=np.sqrt(52) * r.std() * 100,
                maxdd=(cum - cum.cummax()).min() * 100,
                hit=100 * (r[r != 0] > 0).mean(), n=len(r))


def backtest(T):
    S = {'A': sleeve_A(T), 'B': sleeve_B(T), 'D': sleeve_D(T)}
    tot = {k: (v['spot'] + v['carry']).dropna() for k, v in S.items()}
    live = tot['A'].index

    # equal-risk scaling, then the 50/25/25 risk budget
    k = {n: (VOL_TARGET / (tot[n].std() * np.sqrt(52))) for n in tot}
    parts = {n: (tot[n] * k[n]).reindex(live).fillna(0) for n in tot}
    stack = sum(RISK[n] * parts[n] for n in RISK)
    att = {n: {'spot': (S[n]['spot'] * k[n]).reindex(live).fillna(0) * RISK[n],
               'carry': (S[n]['carry'] * k[n]).reindex(live).fillna(0) * RISK[n]}
           for n in RISK}
    return S, tot, parts, stack, att


def ensemble(T, n=12, jitter=1e-12):
    """Re-run the whole stack n times with a machine-epsilon jitter on sleeve
    A's composite and report the spread. See sleeve_A() for why this matters:
    the reported Sharpe of any single run carries roughly +-0.07 of pure
    implementation noise, which is larger than most of the design decisions
    that were argued over."""
    outA, outS = [], []
    B, D = sleeve_B(T), sleeve_D(T)
    tb, td = (B['spot'] + B['carry']).dropna(), (D['spot'] + D['carry']).dropna()
    for i in range(n):
        A = sleeve_A(T, jitter=jitter, seed=i)
        ta = (A['spot'] + A['carry']).dropna()
        outA.append(ta.mean() / ta.std() * np.sqrt(52))
        k = {'A': VOL_TARGET / (ta.std() * np.sqrt(52)),
             'B': VOL_TARGET / (tb.std() * np.sqrt(52)),
             'D': VOL_TARGET / (td.std() * np.sqrt(52))}
        st = sum(RISK[n_] * (x * k[n_]).reindex(ta.index).fillna(0)
                 for n_, x in [('A', ta), ('B', tb), ('D', td)])
        outS.append(st.mean() / st.std() * np.sqrt(52))
    return np.array(outA), np.array(outS)


def ccy_contrib(T, S, tot):
    """Weekly P&L contribution of every currency to the WHOLE portfolio,
    at the live risk budget - the sum across currencies equals the stack."""
    sr, cr = legs(T)
    ret = sr.add(cr, fill_value=None)
    k = {n: VOL_TARGET / ((S[n]['spot'] + S[n]['carry']).dropna().std() * np.sqrt(52))
         for n in RISK}
    idx = (S['A']['spot'] + S['A']['carry']).dropna().index
    out = pd.DataFrame(0.0, index=idx, columns=[c for c in ALLCCY if c != 'MYR'])
    # sleeve A: weights already include mult, lev, and the t-1 shift
    WA = S['A']['weights'].reindex(idx) * k['A'] * RISK['A']
    for c in TRADED:
        out[c] += (WA[c] * ret[c]).fillna(0)
    # sleeve B: EM leg + funding legs
    WB = S['B']['weights'].reindex(idx) * k['B'] * RISK['B']
    for c in TRADED:
        out[c] += (WB[c] * ret[c]).fillna(0)
    FB = S['B']['fund_w'].reindex(idx) * k['B'] * RISK['B']
    for c in FB.columns:
        out[c] += (FB[c] * ret[c]).fillna(0)
    # sleeve D: CNH only
    wD = (S['D']['signal'] * S['D']['lev']).reindex(idx) * k['D'] * RISK['D']
    out['CNH'] += (wD * ret['CNH']).fillna(0)
    return out


def chart_contrib(contrib, stack, outdir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    cum = 100 * contrib.cumsum()
    order = cum.iloc[-1].sort_values(ascending=False).index
    cmap = plt.get_cmap('tab20')

    fig = plt.figure(figsize=(13, 12))
    gs = fig.add_gridspec(3, 1, height_ratios=[2.6, 2.2, 1.3], hspace=0.34)

    ax = fig.add_subplot(gs[0])
    for i, c in enumerate(order):
        lw = 2.0 if abs(cum[c].iloc[-1]) >= 3 else 1.0
        ax.plot(cum.index, cum[c], lw=lw, color=cmap(i % 20),
                label=f'{c} {cum[c].iloc[-1]:+.1f}%')
    ax.plot(stack.index, 100 * stack.cumsum(), lw=2.6, color='#111',
            label=f'TOTAL {100*stack.sum():+.1f}%')
    ax.axhline(0, color='#999', lw=0.7)
    ax.set_ylabel('cumulative contribution (% of book)')
    ax.set_title('Per-currency contribution to the WHOLE portfolio (A+B+D, '
                 'live risk budget)\nlines sum to the black total', loc='left',
                 fontsize=12)
    ax.legend(loc='upper left', fontsize=7, ncol=3)
    ax.grid(alpha=0.25)

    # rolling 52w annualized contribution, as a heatmap
    ax2 = fig.add_subplot(gs[1])
    roll = (contrib.rolling(52).sum() * 100).iloc[51:]
    im = ax2.imshow(roll[order].T.values, aspect='auto', cmap='RdBu_r',
                    vmin=-2.5, vmax=2.5,
                    extent=[0, len(roll), len(order), 0])
    ax2.set_yticks(np.arange(len(order)) + 0.5)
    ax2.set_yticklabels(order, fontsize=8)
    step = max(1, len(roll) // 8)
    ax2.set_xticks(np.arange(0, len(roll), step))
    ax2.set_xticklabels([d.strftime('%Y-%m') for d in roll.index[::step]],
                        fontsize=8)
    ax2.set_title('rolling 52-week contribution (% per year)  red = making '
                  'money, blue = losing', loc='left', fontsize=10)
    fig.colorbar(im, ax=ax2, fraction=0.025, pad=0.01)

    ax3 = fig.add_subplot(gs[2])
    tot = cum.iloc[-1][order]
    ax3.bar(np.arange(len(tot)), tot.values,
            color=['#2e8f5b' if v >= 0 else '#b7333a' for v in tot.values])
    ax3.set_xticks(np.arange(len(tot)))
    ax3.set_xticklabels(tot.index, fontsize=8)
    ax3.axhline(0, color='#333', lw=0.8)
    ax3.set_ylabel('total contribution %')
    ax3.set_title('whole-sample contribution by currency', loc='left', fontsize=10)
    ax3.grid(alpha=0.25, axis='y')

    p = os.path.join(outdir, 'pnl_by_currency.png')
    fig.savefig(p, dpi=140, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return p


def report(T, S, tot, parts, stack, att):
    W = 78
    print('=' * W)
    print('FX WEEKLY MODEL - BACKTEST')
    print(f'sample {stack.index.min().date()} .. {stack.index.max().date()}'
          f'   {len(stack)} weeks   {len(TRADED)} tradable currencies '
          f'(MYR excluded: Citi cannot trade MYR NDF)')
    print('=' * W)

    def line(name, r):
        d = stats(r)
        print(f'{name:<34}Sharpe {d["sharpe"]:+.2f}  ann {d["ann"]:+.2f}%  '
              f'vol {d["vol"]:.2f}%  maxDD {d["maxdd"]:.1f}%  hit {d["hit"]:.0f}%')

    for n in ['A', 'B', 'D']:
        line(f'sleeve {n} (5% vol)', scale5(tot[n]))
    print('-' * W)
    line('STACK  A50 / B25 / D25', stack)
    line('  equal weight (robustness)', pd.DataFrame(parts).mean(axis=1))
    print()
    print('pairwise correlation (this is why the stack works):')
    print(pd.DataFrame({n: scale5(tot[n]) for n in tot}).corr().round(2).to_string())

    print()
    print('P&L attribution - carry versus spot  (% of book, whole sample)')
    print(f'{"":<12}{"carry":>10}{"spot":>10}{"total":>10}{"carry share":>14}')
    rows = {}
    for n in ['A', 'B', 'D']:
        c, s = att[n]['carry'].sum() * 100, att[n]['spot'].sum() * 100
        rows[n] = (c, s)
        sh = f'{100*c/(c+s):.0f}%' if abs(c + s) > 1e-9 else '-'
        print(f'{"sleeve "+n:<12}{c:>+10.1f}{s:>+10.1f}{c+s:>+10.1f}{sh:>14}')
    c = sum(v[0] for v in rows.values())
    s = sum(v[1] for v in rows.values())
    print(f'{"STACK":<12}{c:>+10.1f}{s:>+10.1f}{c+s:>+10.1f}'
          f'{100*c/(c+s):>13.0f}%')

    print()
    print('subsamples (stack):')
    for a, b in [('2013', '2016'), ('2017', '2019'), ('2020', '2022'),
                 ('2023', '2026')]:
        r = stack.loc[a:b].dropna()
        if len(r) > 30:
            print(f'  {a}-{b}: Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}   '
                  f'ann {52*r.mean()*100:+.2f}%')
    print()
    yr = pd.DataFrame({**{n: parts[n] for n in parts}, 'STACK': stack}) \
        .resample('YE').sum() * 100
    yr.index = yr.index.year
    print('calendar years (%):')
    print(yr.round(2).to_string())
    return yr


# ========================================================== 5. CHARTS =======
COL = {'A': '#2e6fb7', 'B': '#d4890b', 'D': '#4c9a52', 'STACK': '#111111',
       'carry': '#2f8f5b', 'spot': '#b7333a'}


def charts(parts, stack, att, outdir):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    made = []

    # ---------- chart 1: cumulative P&L, drawdown, rolling Sharpe ----------
    fig = plt.figure(figsize=(13, 11))
    gs = fig.add_gridspec(3, 1, height_ratios=[3, 1.1, 1.2], hspace=0.3)
    ax = fig.add_subplot(gs[0])
    for n in ['A', 'B', 'D']:
        r = parts[n][parts[n] != 0]
        ax.plot(r.index, 100 * r.cumsum(), lw=1.2, color=COL[n], alpha=0.75,
                label=f'sleeve {n} (5% vol, Sharpe '
                      f'{r.mean()/r.std()*np.sqrt(52):+.2f})')
    sh = stack.mean() / stack.std() * np.sqrt(52)
    ax.plot(stack.index, 100 * stack.cumsum(), lw=2.4, color=COL['STACK'],
            label=f'STACK 50/25/25 (Sharpe {sh:+.2f}, ann '
                  f'{52*stack.mean()*100:+.2f}%)')
    ax.axhline(0, color='#999', lw=0.7)
    ax.set_ylabel('cumulative P&L (% of book)')
    ax.set_title('FX weekly model - cumulative P&L', loc='left', fontsize=12)
    ax.legend(loc='upper left', fontsize=9)
    ax.grid(alpha=0.25)

    ax2 = fig.add_subplot(gs[1], sharex=ax)
    cum = stack.cumsum()
    dd = 100 * (cum - cum.cummax())
    ax2.fill_between(dd.index, dd, 0, color='#b7333a', alpha=0.55, lw=0)
    ax2.set_ylabel('drawdown %')
    ax2.grid(alpha=0.25)
    ax2.text(0.005, 0.08, f'max {dd.min():.1f}%', transform=ax2.transAxes,
             fontsize=9, color='#b7333a')

    ax3 = fig.add_subplot(gs[2], sharex=ax)
    rs = stack.rolling(52).mean() / stack.rolling(52).std() * np.sqrt(52)
    ax3.plot(rs.index, rs, lw=1.3, color='#111')
    ax3.axhline(0, color='#999', lw=0.7)
    ax3.axhline(sh, color='#2e6fb7', ls='--', lw=1, label=f'full sample {sh:+.2f}')
    ax3.fill_between(rs.index, rs, 0, where=rs < 0, color='#b7333a', alpha=0.3, lw=0)
    ax3.set_ylabel('rolling 52w Sharpe')
    ax3.legend(loc='upper right', fontsize=8)
    ax3.grid(alpha=0.25)
    p1 = os.path.join(outdir, 'pnl_timeseries.png')
    fig.savefig(p1, dpi=140, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    made.append(p1)

    # ---------- chart 2: carry versus spot attribution ----------
    C = sum(att[n]['carry'] for n in att)
    Sp = sum(att[n]['spot'] for n in att)
    fig = plt.figure(figsize=(13, 12))
    gs = fig.add_gridspec(3, 2, height_ratios=[2.6, 1.6, 1.5], hspace=0.36,
                          wspace=0.22)

    axa = fig.add_subplot(gs[0, :])
    axa.plot(C.index, 100 * C.cumsum(), lw=2.0, color=COL['carry'],
             label=f'CARRY component  {100*C.sum():+.1f}%')
    axa.plot(Sp.index, 100 * Sp.cumsum(), lw=2.0, color=COL['spot'],
             label=f'SPOT component   {100*Sp.sum():+.1f}%')
    axa.plot(stack.index, 100 * stack.cumsum(), lw=2.6, color='#111',
             label=f'TOTAL            {100*stack.sum():+.1f}%')
    axa.axhline(0, color='#999', lw=0.7)
    axa.set_ylabel('cumulative P&L (% of book)')
    axa.set_title('Where the P&L comes from: carry accrual versus spot movement\n'
                  'stack at the live 50/25/25 risk budget; carry is the rate '
                  'differential earned on the position, spot is the currency move',
                  loc='left', fontsize=12)
    axa.legend(loc='upper left', fontsize=10)
    axa.grid(alpha=0.25)

    # per-sleeve split
    axb = fig.add_subplot(gs[1, 0])
    for n in ['A', 'B', 'D']:
        axb.plot(att[n]['carry'].index, 100 * att[n]['carry'].cumsum(),
                 lw=1.4, color=COL[n], label=f'{n} carry')
        axb.plot(att[n]['spot'].index, 100 * att[n]['spot'].cumsum(),
                 lw=1.4, ls='--', color=COL[n], alpha=0.7, label=f'{n} spot')
    axb.axhline(0, color='#999', lw=0.7)
    axb.set_ylabel('% of book')
    axb.set_title('by sleeve (solid = carry, dashed = spot)', fontsize=10, loc='left')
    axb.legend(fontsize=7, ncol=3)
    axb.grid(alpha=0.25)

    axc = fig.add_subplot(gs[1, 1])
    ratio = (C.rolling(52).sum() /
             (C.rolling(52).sum().abs() + Sp.rolling(52).sum().abs())) * 100
    axc.plot(ratio.index, ratio, lw=1.4, color='#555')
    axc.axhline(50, color='#999', ls='--', lw=0.8)
    axc.set_ylabel('carry share of |P&L| %')
    axc.set_title('rolling 52w: how much of the P&L is carry', fontsize=10, loc='left')
    axc.grid(alpha=0.25)

    axd = fig.add_subplot(gs[2, :])
    yc = (C.resample('YE').sum() * 100)
    ys = (Sp.resample('YE').sum() * 100)
    x = np.arange(len(yc))
    axd.bar(x - 0.2, yc.values, 0.4, color=COL['carry'], label='carry')
    axd.bar(x + 0.2, ys.values, 0.4, color=COL['spot'], label='spot')
    axd.plot(x, (yc + ys).values, 'o-', color='#111', lw=1.4, ms=4, label='total')
    axd.axhline(0, color='#333', lw=0.8)
    axd.set_xticks(x)
    axd.set_xticklabels([d.year for d in yc.index])
    axd.set_ylabel('calendar-year %')
    axd.legend(fontsize=9)
    axd.grid(alpha=0.25, axis='y')
    p2 = os.path.join(outdir, 'pnl_carry_vs_spot.png')
    fig.savefig(p2, dpi=140, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    made.append(p2)
    return made


# ================================================ 6. WEEKLY ORDER SHEET =====
def order_sheet(T, S):
    A = S['A']
    comp, W, mult = A['comp'], A['raw_w'], A['mult']
    t = W.index[W.abs().sum(axis=1) > 0][-1]
    lev = vol_target((A['spot'] + A['carry']).dropna())
    lv = lev.reindex([t]).iloc[0]
    lv = lv if pd.notna(lv) else 1.0
    scale = mult.loc[t] * lv * RISK['A']

    active = W != 0
    tenure = {}
    for c in W.columns:
        s = active[c].loc[:t][::-1]
        tenure[c] = int(s.cummin().sum()) if len(s) and s.iloc[0] else 0

    W78 = 78
    print('\n' + '=' * W78)
    print(f'WEEKLY ORDER SHEET   as of {t.date()}')
    print('=' * W78)
    print(f'--- SLEEVE A  (RV book, {RISK["A"]:.0%} of risk)   '
          f'dispersion x{mult.loc[t]:.2f}   vol-target x{lv:.2f}')
    print(f'{"ccy":<6}{"z":>7}{"side":>7}{"wt%":>8}{"wks":>5}{"basis":>9}{"tenor":>7}')
    b1w, b1m = T.get('fwd_1w'), T.get('fwd_1m')
    gross = 0.0
    for c in TRADED:
        w = W.loc[t, c] * scale
        if w == 0:
            continue
        basis = np.nan
        if b1w is not None and c in b1w.columns and c in b1m.columns:
            x, y = b1w[c].loc[:t].dropna(), b1m[c].loc[:t].dropna()
            if len(x) and len(y):
                basis = 52 * x.iloc[-1] - 12 * y.iloc[-1]
        fav1w = (basis > 0) if w > 0 else (basis < 0)
        ten = '1M' if tenure[c] >= 4 else ('1W' if (pd.isna(basis) or fav1w) else '1M')
        gross += abs(w)
        bs = f'{basis:>9.0f}' if pd.notna(basis) else f'{"n/a":>9}'
        print(f'{c:<6}{comp.loc[t,c]:>+7.2f}{"LONG" if w>0 else "SHORT":>7}'
              f'{100*w:>+8.1f}{tenure[c]:>5}{bs}{ten:>7}')
    print(f'      gross {100*gross:.1f}% of book    LONG = SELL USD/CCY forward')

    # sleeve B
    iv = (1.0 / T['rvol'][TRADED].clip(lower=1.0)).loc[:t].iloc[-1]
    wb = (iv / iv.sum()).dropna()
    tb = (S['B']['spot'] + S['B']['carry']).dropna()
    lb = vol_target(tb).reindex([t]).iloc[0]
    lb = lb if pd.notna(lb) else 1.0
    print(f'\n--- SLEEVE B  (EM beta vs funding basket, {RISK["B"]:.0%} of risk)   '
          f'vol-target x{lb:.2f}')
    print('  LONG  EM basket (equal-vol, 1M forwards):')
    print('   ', '  '.join(f'{c} {100*wb[c]*lb*RISK["B"]:+.1f}%'
                           for c in TRADED if c in wb))
    print('  SHORT funding basket:')
    print('   ', '  '.join(f'{k} {-100*v*lb*RISK["B"]:+.1f}%'
                           for k, v in FUND_BASKET.items()))

    # sleeve D
    sig = S['D'].get('signal')
    print(f'\n--- SLEEVE D  (CNH fixing bias, {RISK["D"]:.0%} of risk)')
    if sig is None or sig.reindex([t]).isna().all():
        print('  -> no fixing data for this week')
    else:
        v = sig.reindex([t]).iloc[0]
        td = (S['D']['spot'] + S['D']['carry']).dropna()
        ld = vol_target(td).reindex([t]).iloc[0]
        ld = ld if pd.notna(ld) else 1.0
        if v == 0:
            print('  -> FLAT (counter-trend or calm gate is closed)')
        else:
            print(f'  -> {"LONG" if v > 0 else "SHORT"} CNH  '
                  f'{100*v*ld*RISK["D"]:+.1f}% of book   (1W forward)')
    print('=' * W78)
    print('Roll: A per-name tenor above; B monthly, all legs together; D weekly.')
    print('Sizes are % of total book notional at a 5% vol target per sleeve.')


# ============================================================== 7. CLI ======
def cli_main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('xlsx', nargs='?', help='path to FX_Model_Data.xlsx')
    ap.add_argument('--clean', help='use a previously saved clean file instead')
    ap.add_argument('--outdir', default='out')
    ap.add_argument('--no-charts', action='store_true')
    ap.add_argument('--ensemble', type=int, default=12, metavar='N',
                    help='robustness runs with a machine-epsilon jitter '
                         '(0 to skip)')
    a = ap.parse_args()
    if not a.xlsx and not a.clean:
        ap.error('give either the workbook path or --clean <file>')
    os.makedirs(a.outdir, exist_ok=True)

    if a.clean and os.path.exists(a.clean):
        T = load_clean(a.clean)
        print(f'loaded clean data from {a.clean}')
    else:
        print(f'reading {a.xlsx} ...')
        T = clean(a.xlsx)
        p = a.clean or os.path.join(a.outdir, 'clean.csv.gz')
        save_clean(T, p)
        print(f'clean data written to {p}')
    for k, v in T.items():
        print(f'  {k:<8} {v.shape[0]:>4} weeks x {v.shape[1]:>2} cols  '
              f'{v.index.min().date()}..{v.index.max().date()}')

    S, tot, parts, stack, att = backtest(T)
    print()
    report(T, S, tot, parts, stack, att)

    if a.ensemble:
        ea, es = ensemble(T, n=a.ensemble)
        print()
        print(f'robustness - {a.ensemble} runs with a 1e-12 jitter on the '
              f'composite (see sleeve_A docstring):')
        print(f'  sleeve A Sharpe  {ea.mean():+.2f} +- {ea.std():.2f}   '
              f'range [{ea.min():+.2f}, {ea.max():+.2f}]')
        print(f'  STACK    Sharpe  {es.mean():+.2f} +- {es.std():.2f}   '
              f'range [{es.min():+.2f}, {es.max():+.2f}]')
        print('  the single-path numbers above are one draw from these bands.')

    out = pd.DataFrame({'stack': stack,
                        **{f'{n}_total': parts[n] for n in parts},
                        **{f'{n}_{p}': att[n][p] for n in att for p in att[n]}})
    out.to_csv(os.path.join(a.outdir, 'returns.csv'))
    print(f'\nreturns written to {os.path.join(a.outdir, "returns.csv")}')

    if not a.no_charts:
        try:
            for p in charts(parts, stack, att, a.outdir):
                print(f'chart written to {p}')
            contrib = ccy_contrib(T, S, tot)
            print(f'chart written to {chart_contrib(contrib, stack, a.outdir)}')
            contrib.to_csv(os.path.join(a.outdir, 'contrib_by_ccy.csv'))
        except ImportError:
            print('matplotlib not installed - charts skipped')

    order_sheet(T, S)




# %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
# %% PART 2 - FREE EXTERNAL DATA FETCHERS (CFTC / BIS / FRED / IMF / Yahoo) %%
# %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

import io
import json
import os
import subprocess
import sys

import pandas as pd

OUT = 'data/external'
CA = '/root/.ccr/ca-bundle.crt'
ISO2 = {'CNH': 'CN', 'IDR': 'ID', 'INR': 'IN', 'KRW': 'KR', 'PHP': 'PH',
        'SGD': 'SG', 'THB': 'TH', 'TWD': 'TW', 'BRL': 'BR', 'MXN': 'MX',
        'CLP': 'CL', 'PLN': 'PL', 'HUF': 'HU'}
ISO3 = {'CNH': 'CHN', 'IDR': 'IDN', 'INR': 'IND', 'KRW': 'KOR', 'PHP': 'PHL',
        'SGD': 'SGP', 'THB': 'THA', 'TWD': 'TWN', 'BRL': 'BRA', 'MXN': 'MEX',
        'CLP': 'CHL', 'PLN': 'POL', 'HUF': 'HUN'}


def curl(url, headers=None):
    cmd = ['curl', '-sS', '--max-time', '90', '-G' if '$' in url else '-L',
           '-H', 'User-Agent: Mozilla/5.0']       # Yahoo rejects bare curl
    if os.path.exists(CA):
        cmd += ['--cacert', CA]
    for h in headers or []:
        cmd += ['-H', h]
    cmd += [url]
    r = subprocess.run(cmd, capture_output=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode()[:200])
    return r.stdout


def save(df, name):
    os.makedirs(OUT, exist_ok=True)
    p = f'{OUT}/{name}.csv'
    df.to_csv(p)
    print(f'  ok  {name:<28} {df.shape[0]:>6} rows x {df.shape[1]:>2} cols  '
          f'{df.index.min()} .. {df.index.max()}')


# ---------------------------------------------------------------- CFTC ------
def fetch_cftc():
    name_map = {'BRAZILIAN REAL': 'BRL', 'MEXICAN PESO': 'MXN',
                'JAPANESE YEN': 'JPY', 'EURO FX': 'EUR',
                'CANADIAN DOLLAR': 'CAD'}
    base = 'https://publicreporting.cftc.gov/resource/gpe5-46if.json'
    where = ("contract_market_name in ('MEXICAN PESO','BRAZILIAN REAL',"
             "'JAPANESE YEN','EURO FX','CANADIAN DOLLAR') "
             "AND report_date_as_yyyy_mm_dd>'2005-01-01'")
    url = (base + '?$select=report_date_as_yyyy_mm_dd,contract_market_name,'
           'lev_money_positions_long,lev_money_positions_short,'
           'open_interest_all&$where=' +
           where.replace(' ', '%20').replace("'", '%27') +
           '&$order=report_date_as_yyyy_mm_dd&$limit=50000')
    rows = json.loads(curl(url))
    df = pd.DataFrame(rows)
    df['date'] = pd.to_datetime(df['report_date_as_yyyy_mm_dd'])
    df['ccy'] = df['contract_market_name'].map(name_map)
    for c in ['lev_money_positions_long', 'lev_money_positions_short',
              'open_interest_all']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df['net_pct_oi'] = 100 * (df['lev_money_positions_long'] -
                              df['lev_money_positions_short']) / \
        df['open_interest_all'].replace(0, pd.NA)
    net = df.pivot_table(index='date', columns='ccy', values='net_pct_oi',
                         aggfunc='last').sort_index()
    save(net, 'cftc_tff_net_pct_oi')

    base2 = 'https://publicreporting.cftc.gov/resource/6dca-aqww.json'
    names2 = {f'{k} - CHICAGO MERCANTILE EXCHANGE': v
              for k, v in name_map.items()}
    where2 = ('market_and_exchange_names in (' +
              ','.join(f"'{k}'" for k in names2) +
              ") AND report_date_as_yyyy_mm_dd>'2005-01-01'")
    url2 = (base2 + '?$select=report_date_as_yyyy_mm_dd,'
            'market_and_exchange_names,noncomm_positions_long_all,'
            'noncomm_positions_short_all,open_interest_all&$where=' +
            where2.replace(' ', '%20').replace("'", '%27') +
            '&$order=report_date_as_yyyy_mm_dd&$limit=50000')
    rows2 = json.loads(curl(url2))
    d2 = pd.DataFrame(rows2)
    d2['date'] = pd.to_datetime(d2['report_date_as_yyyy_mm_dd'])
    d2['ccy'] = d2['market_and_exchange_names'].map(names2)
    for c in ['noncomm_positions_long_all', 'noncomm_positions_short_all',
              'open_interest_all']:
        d2[c] = pd.to_numeric(d2[c], errors='coerce')
    d2['net_pct_oi'] = 100 * (d2['noncomm_positions_long_all'] -
                              d2['noncomm_positions_short_all']) / \
        d2['open_interest_all'].replace(0, pd.NA)
    net2 = d2.pivot_table(index='date', columns='ccy', values='net_pct_oi',
                          aggfunc='last').sort_index()
    save(net2, 'cftc_legacy_noncomm_net_pct_oi')


# ----------------------------------------------------------------- BIS ------
def _bis_csv(url, value_name):
    txt = curl(url).decode()
    df = pd.read_csv(io.StringIO(txt), header=None)
    # BIS csv: dims..., title, period, value, status...; find period col by
    # scanning for the YYYY-MM pattern
    per_col = None
    for c in df.columns:
        s = df[c].astype(str)
        if s.str.match(r'^\d{4}-\d{2}').mean() > 0.5:
            per_col = c
            break
    if per_col is None:
        raise RuntimeError('period column not found')
    area_col = 3          # D/M, R/N, B, AREA is the 4th field in WS_EER keys
    return df, per_col, area_col


def fetch_bis():
    inv = {v: k for k, v in ISO2.items()}
    areas = '+'.join(ISO2[c] for c in ISO2)

    url = (f'https://stats.bis.org/api/v1/data/WS_EER/M.R.B.{areas}/all'
           '?format=csv')
    df, per, ar = _bis_csv(url, 'reer')
    df['ccy'] = df[ar].map(inv)
    out = df.pivot_table(index=per, columns='ccy', values=df.columns[-4],
                         aggfunc='last')
    out.index = pd.PeriodIndex(out.index, freq='M').to_timestamp('M')
    save(out.sort_index(), 'bis_reer_real_broad_m')

    url = (f'https://stats.bis.org/api/v1/data/WS_EER/D.N.B.{areas}/all'
           '?format=csv')
    df, per, ar = _bis_csv(url, 'neer')
    df['ccy'] = df[ar].map(inv)
    out = df.pivot_table(index=per, columns='ccy', values=df.columns[-4],
                         aggfunc='last')
    out.index = pd.to_datetime(out.index)
    save(out.sort_index(), 'bis_neer_nominal_broad_d')

    pol_areas = '+'.join(ISO2[c] for c in ISO2 if c not in ('SGD', 'TWD'))
    url = (f'https://stats.bis.org/api/v1/data/WS_CBPOL/D.{pol_areas}/all'
           '?format=csv')
    txt = curl(url).decode()
    df = pd.read_csv(io.StringIO(txt), header=None)
    per_col = None
    for c in df.columns:
        if df[c].astype(str).str.match(r'^\d{4}-\d{2}-\d{2}$').mean() > 0.5:
            per_col = c
            break
    df['ccy'] = df[1].map(inv)
    out = df.pivot_table(index=per_col, columns='ccy',
                         values=df.columns[-4], aggfunc='last')
    out.index = pd.to_datetime(out.index)
    save(out.sort_index(), 'bis_policy_rate_d')


# ---------------------------------------------------------------- FRED ------
def fetch_fred():
    def grab(sid):
        txt = curl(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}')
        df = pd.read_csv(io.StringIO(txt.decode()))
        df.columns = ['date', sid]
        df['date'] = pd.to_datetime(df['date'])
        return df.set_index('date')[sid].apply(pd.to_numeric, errors='coerce')

    glob = pd.DataFrame({s: grab(s) for s in
                         ['NFCI', 'VIXCLS', 'DTWEXBGS', 'DTWEXEMEGS',
                          'T10Y2Y']})
    save(glob, 'fred_global_state_d')

    reer = pd.DataFrame({c: grab(f'RB{ISO2[c]}BIS') for c in ISO2})
    save(reer, 'fred_bis_reer_m')

    res_ids = {'CNH': 'TRESEGCNM052N', 'INR': 'TRESEGINM052N',
               'IDR': 'TRESEGIDM052N', 'KRW': 'TRESEGKRM052N',
               'BRL': 'TRESEGBRM052N', 'MXN': 'TRESEGMXM052N',
               'PLN': 'TRESEGPLM052N'}
    res = {}
    for c, sid in res_ids.items():
        try:
            res[c] = grab(sid)
        except Exception as e:
            print(f'  skip reserves {c}: {str(e)[:60]}')
    if res:
        save(pd.DataFrame(res), 'fred_fx_reserves_usd_m')


# ----------------------------------------------------------------- IMF ------
def _imf_chunks(url_fn, hdr):
    """The IMF endpoint caps response rows, so a 20-year panel pull silently
    truncates (we saw exports end at 2026-01 while narrow queries reach
    2026-05). Fetch in date chunks and concatenate."""
    frames = []
    for a, b in [('2000-01', '2005-12'), ('2006-01', '2011-12'),
                 ('2012-01', '2017-12'), ('2018-01', '2022-12'),
                 ('2023-01', '2030-12')]:
        try:
            txt = curl(url_fn(a, b), headers=hdr).decode()
            df = pd.read_csv(io.StringIO(txt))
            # chunks can carry different metadata columns - keep only the
            # three we use so concat cannot misalign
            ref = [c for c in df.columns
                   if c.upper() in ('REF_AREA', 'COUNTRY')][0]
            per = [c for c in df.columns if 'TIME_PERIOD' in c.upper()][0]
            val = [c for c in df.columns if c.upper() == 'OBS_VALUE'][0]
            frames.append(df[[ref, per, val]].set_axis(
                ['COUNTRY', 'TIME_PERIOD', 'OBS_VALUE'], axis=1))
        except Exception as e:
            print(f'  chunk {a}..{b} failed: {str(e)[:60]}')
    return pd.concat(frames, ignore_index=True).dropna(subset=['TIME_PERIOD'])


def fetch_imf():
    hdr = ['Accept: application/vnd.sdmx.data+csv;version=1.0.0']
    ccys = [c for c in ISO3 if c != 'TWD']
    key = '+'.join(ISO3[c] for c in ccys)
    df = _imf_chunks(
        lambda a, b: (f'https://api.imf.org/external/sdmx/2.1/data/IMF.STA,'
                      f'IRFCL/{key}.IRFCLDT1_IRFCL65_USD..M'
                      f'?startPeriod={a}&endPeriod={b}'), hdr)
    inv = {v: k for k, v in ISO3.items()}
    df['ccy'] = df['COUNTRY'].map(inv)
    out = df.pivot_table(index='TIME_PERIOD', columns='ccy',
                         values='OBS_VALUE', aggfunc='last')
    out.index = pd.PeriodIndex([p.replace('-M', '-') for p in out.index],
                               freq='M').to_timestamp('M')
    save(out.sort_index(), 'imf_reserves_usd_m')

    ccys = [c for c in ISO3 if c != 'PHP']
    key = '+'.join(ISO3[c] for c in ccys)
    df = _imf_chunks(
        lambda a, b: (f'https://api.imf.org/external/sdmx/2.1/data/IMF.STA,'
                      f'ITG/{key}.XG.FOB_USD.M'
                      f'?startPeriod={a}&endPeriod={b}'), hdr)
    df['ccy'] = df['COUNTRY'].map(inv)
    out = df.pivot_table(index='TIME_PERIOD', columns='ccy',
                         values='OBS_VALUE', aggfunc='last')
    out.index = pd.PeriodIndex([p.replace('-M', '-') for p in out.index],
                               freq='M').to_timestamp('M')
    save(out.sort_index(), 'imf_exports_usd_m')


# -------------------------------------------------------------- equity ------
def fetch_equity():
    symbols = {'IDR': '%5EJKSE', 'PHP': 'PSEI.PS', 'THB': '%5ESET.BK',
               'SGD': '%5ESTI', 'BRL': '%5EBVSP', 'MXN': '%5EMXX',
               'CNH': '000001.SS', 'CLP': 'ECH', 'PLN': 'EPOL',
               'HUF': 'OTP.BD'}
    out = {}
    for ccy, sym in symbols.items():
        try:
            url = (f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}'
                   '?period1=0&period2=9999999999&interval=1d')
            j = json.loads(curl(url))
            r = j['chart']['result'][0]
            ts = pd.to_datetime(r['timestamp'], unit='s').normalize()
            close = pd.Series(r['indicators']['quote'][0]['close'], index=ts)
            out[ccy] = close.dropna()
        except Exception as e:
            print(f'  skip equity {ccy} ({sym}): {str(e)[:60]}')
    if out:
        save(pd.DataFrame(out).sort_index(), 'yahoo_equity_close_d')


GROUPS = {'cftc': fetch_cftc, 'bis': fetch_bis, 'fred': fetch_fred,
          'imf': fetch_imf, 'equity': fetch_equity}


def fetch_main(picks=None):
    picks = picks or list(GROUPS)
    for g in picks:
        print(f'[{g}]')
        try:
            GROUPS[g]()
        except Exception as e:
            print(f'  FAILED: {str(e)[:180]}')




# %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%
# %% PART 3 - DASHBOARD (streamlit run fx_one.py)                          %%
# %%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%%

def run_dashboard():
 import glob
 import os

 import numpy as np
 import pandas as pd
 import streamlit as st

 try:
     import plotly.graph_objects as go
     from plotly.subplots import make_subplots
 except ImportError:
     st.error('pip install plotly')
     st.stop()

 DATA = 'data/clean/clean_csv'
 TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'PHP', 'SGD', 'THB', 'TWD',
           'BRL', 'MXN', 'CLP', 'PLN', 'HUF']          # MYR excluded (no NDF at Citi)
 FUNDING = ['EUR', 'JPY', 'CAD']
 CCY_COUNTRY = {'CNH': 'China', 'IDR': 'Indonesia', 'INR': 'India',
                'KRW': 'South Korea', 'PHP': 'Philippines', 'SGD': 'Singapore',
                'THB': 'Thailand', 'TWD': 'Taiwan', 'BRL': 'Brazil',
                'MXN': 'Mexico', 'CLP': 'Chile', 'PLN': 'Poland',
                'HUF': 'Hungary', 'MYR': 'Malaysia'}

 st.set_page_config(page_title='FX Model Dashboard', page_icon='🌏',
                    layout='wide')

 # ---- Citi-Velocity-inspired look: dark navy, cyan accent, spaced uppercase
 # section headers, region-coded categorical colors ----
 st.markdown('''<style>
 h1 { font-size: 1.55rem !important; letter-spacing: .02em; }
 h2, h3 { text-transform: uppercase; letter-spacing: .18em;
          font-size: .95rem !important; font-weight: 600 !important;
          color: #e6edf3 !important; }
 h2::after, h3::after { content: " \\00BB"; color: #00bdf2; }
 [data-testid="stCaptionContainer"] { color: #8b98a9 !important; }
 .stTabs [data-baseweb="tab-list"] { border-bottom: 1px solid #1f2937; }
 .stTabs [data-baseweb="tab"] { text-transform: uppercase;
   letter-spacing: .1em; font-size: .8rem; }
 [data-testid="stMetricValue"] { color: #00bdf2; }
 a { color: #00bdf2 !important; }
 thead th { background: #131a26 !important; }
 </style>''', unsafe_allow_html=True)

 REGION = {**{c: 'APAC' for c in ['CNH', 'IDR', 'INR', 'KRW', 'PHP', 'SGD',
                                  'THB', 'TWD', 'MYR']},
           **{c: 'CEEMEA' for c in ['PLN', 'HUF']},
           **{c: 'LATAM' for c in ['BRL', 'MXN', 'CLP']},
           **{c: 'G10' for c in ['EUR', 'JPY', 'CAD', 'USD']}}
 RCOL = {'APAC': '#f2c500', 'CEEMEA': '#d6336c', 'LATAM': '#7048a8',
         'G10': '#f76707', 'Thematic': '#74b816'}
 PLOT_BG = dict(template='plotly_dark', paper_bgcolor='rgba(0,0,0,0)',
                plot_bgcolor='rgba(0,0,0,0)')


 def ccy_colors(seq):
     return [RCOL.get(REGION.get(c, 'Thematic'), '#74b816') for c in seq]


 @st.cache_data(ttl=3600)
 def fair_value(_L):
     """Short-term model-implied fair value per currency (weekly).

     Rolling 52w regression of the weekly currency return on observable
     drivers (2y yield-diff change, CTOT change, DXY return); the misalignment
     is the 13-week cumulated residual - how far the currency has over/under-
     shot what its drivers explain this quarter. fair spot = spot * exp(mis):
     mis > 0 means the currency ran AHEAD of fundamentals (rich), so fair
     USD/CCY sits ABOVE spot. Descriptive tool - fading it scored IC -0.007
     on this universe (see strategy_logic.md section 7), so these lines are
     context for entries, not a signal.
     """
     spot_ = _L['L1_spot_usd_traded']
     ret = -np.log(spot_).diff()
     ydiff = _L.get('L2_ydiff_2y_vs_us', pd.DataFrame()).reindex(ret.index)
     ctot_ = _L.get('L1_ctot', pd.DataFrame()).reindex(ret.index)
     reg_ = _L.get('L1_regime', pd.DataFrame()).reindex(ret.index)
     dxy = np.log(reg_['DXY']).diff() if 'DXY' in reg_ else \
         pd.Series(np.nan, index=ret.index)
     mis = pd.DataFrame(index=ret.index, columns=ret.columns, dtype=float)
     for c in ret.columns:
         X = pd.DataFrame({
             'dyd': ydiff[c].diff() if c in ydiff else np.nan,
             'ctot': np.log(ctot_[c]).diff() if c in ctot_ else np.nan,
             'dxy': dxy})
         dat = pd.concat([ret[c].rename('y'), X], axis=1)
         resid = pd.Series(index=dat.index, dtype=float)
         vals = dat[['dyd', 'ctot', 'dxy']].fillna(0.0).values
         yv = dat['y'].values
         for i in range(52, len(dat)):
             if np.isnan(yv[i]):
                 continue
             sl = slice(i - 52, i)
             yw = yv[sl]
             ok = ~np.isnan(yw)
             if ok.sum() < 30:
                 continue
             Xw = np.c_[np.ones(ok.sum()), vals[sl][ok]]
             try:
                 beta = np.linalg.lstsq(Xw, yw[ok], rcond=None)[0]
             except np.linalg.LinAlgError:
                 continue
             resid.iloc[i] = yv[i] - np.r_[1, vals[i]] @ beta
         mis[c] = resid.rolling(13, min_periods=5).sum()
     return spot_ * np.exp(mis)


 # ================================================================= data =====
 @st.cache_data(ttl=3600)
 def load_all():
     out = {}
     for f in glob.glob(f'{DATA}/*.csv'):
         n = os.path.basename(f)[:-4]
         out[n] = pd.read_csv(f, index_col=0, parse_dates=True)
     return out


 def zsec(df):
     return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


 def pctile(s, x=None, win=260):
     """Percentile of the latest (or given) value within trailing win obs."""
     s = s.dropna()
     if len(s) < 30:
         return np.nan
     ref = s.iloc[-win:]
     v = s.iloc[-1] if x is None else x
     return 100.0 * (ref <= v).mean()


 L = load_all()
 if not L:
     st.error(f'no data found under {DATA} - run data_pipeline_v2.py first')
     st.stop()

 spot = L['L1_spot_usd_traded']
 spot_all = L['L1_spot_usd_all30']
 carry = L['L1_carry_1m_ann']
 rvol = L['L1_vol_realized_1m']
 atm = L['L1_vol_implied_atm_1m']
 esi = L['L1_esi']
 spd = L.get('L1_spot_daily_traded')
 ASOF = spot.index.max()

 ret_w = -np.log(spot_all).diff()
 carry_w = (carry.reindex(columns=spot_all.columns) / 100 / 52).shift(1)
 tot_w = ret_w.add(carry_w, fill_value=np.nan)

 # ---- the live composite (2 x carry/vol + 1 x ESI chg, 3w smooth) ----
 cv = (carry[TRADED] / rvol[TRADED].clip(lower=1.0)).reindex(columns=TRADED)
 esi_chg = (esi - esi.shift(4)).reindex(columns=TRADED)
 zc, ze = zsec(cv), zsec(esi_chg.reindex(cv.index))
 num = 2 * zc.fillna(0) + ze.fillna(0)
 den = 2 * zc.notna() + 1 * ze.notna()
 COMP = zsec((num / den.replace(0, np.nan)).rolling(3).mean())

 # ---- factor library: name -> (panel, historical IC, IC t, verdict) ----
 # ICs are from this repo's studies on the tradable universe (see
 # docs/strategy_logic.md section 6/7). They drive the "supportive?" flags.
 def _reer_dev():
     r = L['L1_reer'].reindex(columns=TRADED)
     lr = np.log(r)
     return -(lr - lr.rolling(260, min_periods=104).mean()) / \
         lr.rolling(260, min_periods=104).std()


 FACTORS = {
     'carry/realvol':   (cv, +0.073, 6.4, 'IN THE MODEL'),
     'ESI 4w change':   (esi_chg, +0.026, 2.5, 'IN THE MODEL'),
     'real carry (CPI-adj)': (L.get('L2_real_carry_1m', pd.DataFrame()).reindex(columns=TRADED),
                              +0.056, 5.0, 'duplicates carry'),
     'CA YoY':          (L.get('L2_ca_yoy_usdbn', pd.DataFrame()).reindex(columns=TRADED),
                         +0.019, 1.8, 'marginal'),
     'REER 5y deviation (cheap=+)': (_reer_dev(), +0.029, 2.4, 'no book value'),
     'CDS 4w chg (tighter=+)': (-L.get('L2_cds_5y', pd.DataFrame()).diff(4).reindex(columns=TRADED),
                                +0.000, 0.0, 'coverage artefact'),
     'momentum 12w':    (-np.log(spot[TRADED]).diff(12), +0.006, 0.4, 'dead'),
     'CTOT 13w chg':    (L.get('L2_ctot_chg_13w', pd.DataFrame()).reindex(columns=TRADED),
                         +0.002, 0.2, 'dead'),
 }
 SENTIMENT = {
     'risk reversal z (fear priced)': -L.get('L2_rr25_1m_z_52w', pd.DataFrame()).reindex(columns=TRADED),
     'vol risk premium (implied-realized)': -(atm[TRADED] - rvol[TRADED]),
     'ATM vol level': atm[TRADED],
 }


 def vol_target(r, tgt=0.05, cap=3.0):
     lev = (tgt / (r.rolling(52).std() * np.sqrt(52))).clip(upper=cap)
     return lev.shift(1)


 def build_book(comp, atm_, top_in=3, top_stay=5, gate=0.5):
     W = pd.DataFrame(0.0, index=comp.index, columns=comp.columns)
     pl, ps = set(), set()
     for t in comp.index:
         s = comp.loc[t].dropna()
         s = s[s.abs() > gate]
         if len(s) < 2:
             pl, ps = set(), set()
             continue
         rd, ra = s.rank(ascending=False), s.rank(ascending=True)
         Lg = set(s.index[rd <= top_in]) | {c for c in pl if c in s.index and rd[c] <= top_stay}
         Sh = set(s.index[ra <= top_in]) | {c for c in ps if c in s.index and ra[c] <= top_stay}
         Lg, Sh = Lg - Sh, Sh - Lg
         pl, ps = Lg, Sh
         iv = 1.0 / atm_.loc[t].reindex(list(Lg | Sh)).replace(0, np.nan)
         iv = iv.fillna(iv.mean() if pd.notna(iv.mean()) else 1.0)
         for sd, ix in [(1, sorted(Lg)), (-1, sorted(Sh))]:
             if ix:
                 v = iv.reindex(ix)
                 W.loc[t, ix] = sd * v / v.sum()
     return W


 def run_xs_book(score, cost_bp=0.0, universe=None):
     """Cross-sectional book with the live machinery + linear cost model."""
     uni = universe or TRADED
     comp = zsec(score.reindex(columns=uni))
     W = build_book(comp, atm.reindex(columns=uni))
     Wl = W.shift(1).reindex(tot_w.index)[uni]
     gross = (Wl * tot_w[uni]).sum(axis=1)
     to = Wl.diff().abs().sum(axis=1)
     net = gross - to * cost_bp * 1e-4
     net = net[Wl.abs().sum(axis=1) > 0]
     return (net * vol_target(net)).dropna(), Wl


 def stats_row(r):
     r = r.dropna()
     if len(r) < 30:
         return {}
     cum = r.cumsum()
     return {'Sharpe': r.mean() / r.std() * np.sqrt(52),
             'ann %': 52 * r.mean() * 100,
             'vol %': np.sqrt(52) * r.std() * 100,
             'maxDD %': (cum - cum.cummax()).min() * 100,
             'hit %': 100 * (r[r != 0] > 0).mean(),
             'weeks': len(r)}


 # ============================================================ layout ========
 st.title('🌏 FX Model — all-in-one dashboard')
 st.caption(f'data as of **{ASOF.date()}** · 13 tradable EM currencies '
            f'(MYR excluded) · weekly Friday grid · '
            f'live book = A(RV) 50% + B(EM beta) 25% + D(CNH fix) 25%, '
            f'Sharpe 1.51 ± 0.08')

 tabs = st.tabs(['🎯 Overview', '💱 Currency deep-dive', '🧪 Backtester',
                 '🤖 ML Lab', '🧭 Drivers & Meta', '📰 News & data',
                 '📈 All currencies'])


 # ------------------------------------------------------------ overview -----
 with tabs[0]:
     c1, c2 = st.columns([3, 2])
     with c1:
         st.subheader('Entry verdict per currency')
         st.caption('F = composite z (evidence-weighted fundamental). '
                    'S = sentiment percentile (RR fear + vol premium, 5y). '
                    'T = 50/200d MA cross of daily spot. Verdict = F direction, '
                    'confirmed/flagged by S and T. F carries the weight because '
                    'F is the only pillar with a proven IC on this universe.')
         rows = []
         for c in TRADED:
             f = COMP[c].dropna()
             fz = f.iloc[-1] if len(f) else np.nan
             sp_ = pd.concat([x[c].dropna().rank(pct=True).iloc[-1:] * 100
                              for x in [SENTIMENT['risk reversal z (fear priced)'],
                                        SENTIMENT['vol risk premium (implied-realized)']]
                              if c in x.columns and len(x[c].dropna()) > 30])
             sq = sp_.mean() if len(sp_) else np.nan
             tq = np.nan
             if spd is not None and c in spd.columns:
                 px = -np.log(spd[c].dropna())
                 if len(px) > 200:
                     tq = float(np.sign(px.rolling(50).mean().iloc[-1]
                                        - px.rolling(200).mean().iloc[-1]))
             if pd.isna(fz) or abs(fz) <= 0.5:
                 verdict = '— no edge'
             else:
                 side = 'LONG' if fz > 0 else 'SHORT'
                 agree = (tq == np.sign(fz)) if not pd.isna(tq) else None
                 verdict = f'{"🟢" if agree else "🟡"} {side}' + \
                     ('' if agree in (True, None) else ' (T disagrees)')
             rows.append({'ccy': c, 'F composite z': fz,
                          'S percentile': sq,
                          'T trend': {1.0: 'up', -1.0: 'down'}.get(tq, '—'),
                          'verdict': verdict})
         df_v = pd.DataFrame(rows).set_index('ccy')
         st.dataframe(df_v.style.format({'F composite z': '{:+.2f}',
                                         'S percentile': '{:.0f}'})
                      .background_gradient(subset=['F composite z'],
                                           cmap='RdYlGn', vmin=-2, vmax=2),
                      height=500)
     with c2:
         st.subheader('Live book this week')
         Wb = build_book(COMP, atm.reindex(columns=TRADED))
         t = Wb.index[Wb.abs().sum(axis=1) > 0][-1]
         pos = Wb.loc[t]
         pos = pos[pos != 0].sort_values(ascending=False)
         st.write(f'sleeve A positions, {t.date()} '
                  '(LONG = sell USD/CCY forward):')
         st.dataframe(pos.rename('weight').to_frame()
                      .style.format('{:+.2f}').bar(align='mid',
                                                   color=['#b7333a', '#2e8f5b']))
         st.markdown('**B**: long equal-vol EM basket vs 50% CAD + 50% G3 '
                     '(passive) · **D**: CNH per the fixing-bias gates '
                     '(see run_weekly_all.py for exact sizes)')

     st.subheader('Economic surprise index')
     esi_cols = [c for c in esi.columns if esi[c].dropna().size > 30]
     latest = esi[esi_cols].dropna(how='all').iloc[-1]
     month_ago = esi[esi_cols].dropna(how='all').iloc[-5] \
         if len(esi.dropna(how='all')) > 5 else latest
     order_e = [c for c in esi_cols if REGION.get(c, 'Thematic') == 'G10'] + \
         [c for c in esi_cols if REGION.get(c, 'Thematic') == 'APAC'] + \
         [c for c in esi_cols if REGION.get(c, 'Thematic') == 'CEEMEA'] + \
         [c for c in esi_cols if REGION.get(c, 'Thematic') == 'LATAM'] + \
         [c for c in esi_cols if REGION.get(c) is None]
     fig_e = go.Figure()
     fig_e.add_trace(go.Bar(
         x=order_e, y=[latest.get(c, np.nan) for c in order_e],
         marker_color=ccy_colors(order_e), name=f'latest ({ASOF.date()})'))
     fig_e.add_trace(go.Scatter(
         x=order_e, y=[month_ago.get(c, np.nan) for c in order_e],
         mode='markers', marker=dict(color='#4dabf7', size=8),
         name='1 month ago'))
     fig_e.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                         legend=dict(orientation='h'), **PLOT_BG)
     st.plotly_chart(fig_e, use_container_width=True)
     st.caption('bars colored by region — 🟧 G10 · 🟨 APAC · 🟥 CEEMEA · '
                '🟪 LATAM · 🟩 thematic — bar = latest weekly ESI, '
                'dot = one month ago')


 # ------------------------------------------------ currency deep-dive --------
 with tabs[1]:
     ccy = st.selectbox('currency', TRADED, index=0)
     cc1, cc2 = st.columns([3, 2])

     with cc1:
         st.subheader(f'USD/{ccy} — price & technicals (adjustable)')
         tc1, tc2, tc3, tc4 = st.columns(4)
         ma_f = tc1.number_input('fast MA (d)', 10, 100, 50, 5)
         ma_s = tc2.number_input('slow MA (d)', 50, 300, 200, 10)
         bb_p = tc3.number_input('BB window (d)', 10, 60, 20, 5)
         bb_k = tc4.number_input('BB width (sd)', 1.0, 3.0, 2.0, 0.25)
         if spd is not None and ccy in spd.columns:
             px = spd[ccy].dropna().iloc[-750:]
             fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                                 row_heights=[0.72, 0.28], vertical_spacing=0.04)
             fig.add_trace(go.Scatter(x=px.index, y=px, name=f'USD/{ccy}',
                                      line=dict(color='#e6edf3', width=1.4)), 1, 1)
             fig.add_trace(go.Scatter(x=px.index, y=px.rolling(ma_f).mean(),
                                      name=f'MA{ma_f}', line=dict(width=1)), 1, 1)
             fig.add_trace(go.Scatter(x=px.index, y=px.rolling(ma_s).mean(),
                                      name=f'MA{ma_s}', line=dict(width=1)), 1, 1)
             m, s = px.rolling(bb_p).mean(), px.rolling(bb_p).std()
             for sgn in (+1, -1):
                 fig.add_trace(go.Scatter(x=px.index, y=m + sgn * bb_k * s,
                                          name='BB', showlegend=sgn > 0,
                                          line=dict(color='#999', width=0.8,
                                                    dash='dot')), 1, 1)
             d = px.diff()
             up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
             dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
             rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
             fig.add_trace(go.Scatter(x=px.index, y=rsi, name='RSI14',
                                      line=dict(color='#7048a8', width=1)), 2, 1)
             fig.add_hline(y=70, row=2, col=1, line_dash='dot', line_color='#bbb')
             fig.add_hline(y=30, row=2, col=1, line_dash='dot', line_color='#bbb')
             fig.update_layout(height=430, margin=dict(l=10, r=10, t=10, b=10),
                               legend=dict(orientation='h'), **PLOT_BG)
             st.plotly_chart(fig, use_container_width=True)
         st.caption('⚠️ evidence note: every technical here was backtested on '
                    'this universe in 4 roles (standalone, pillar, entry gate, '
                    'TSMOM sleeve) and none added to the book — treat these as '
                    'descriptive, not predictive. See docs/strategy_logic.md §7.')

         st.subheader('What drove the last month')
         drv = pd.DataFrame({
             'carry/vol z': zc[ccy] if ccy in zc else np.nan,
             'ESI z': ze[ccy] if ccy in ze else np.nan,
             'DXY': np.log(L['L1_regime']['DXY']).diff()
             if 'L1_regime' in L else np.nan,
             'EM vol chg': L['L1_regime']['EMFXVOL'].diff()
             if 'L1_regime' in L else np.nan,
         }).reindex(tot_w.index)
         y = tot_w[ccy]
         win = pd.concat([y.rename('y'), drv], axis=1).dropna().iloc[-104:]
         if len(win) > 40:
             X = np.c_[np.ones(len(win)), win.iloc[:, 1:].values]
             beta = np.linalg.lstsq(X, win['y'].values, rcond=None)[0]
             last4 = win.iloc[-4:]
             contrib = {}
             for j, nmm in enumerate(win.columns[1:]):
                 contrib[nmm] = beta[j + 1] * last4[nmm].sum() * 1e4
             resid = (last4['y'].sum() * 1e4 - sum(contrib.values())
                      - beta[0] * 4 * 1e4)
             contrib['unexplained'] = resid
             cs = pd.Series(contrib)
             figd = go.Figure(go.Bar(x=cs.index, y=cs.values,
                                     marker_color=['#2e8f5b' if v >= 0 else '#b7333a'
                                                   for v in cs.values]))
             figd.update_layout(height=260, yaxis_title='bp (4w)', **PLOT_BG,
                                margin=dict(l=10, r=10, t=20, b=10),
                                title=f'{ccy} last-4w total return '
                                      f'{win["y"].iloc[-4:].sum()*1e4:+.0f}bp, '
                                      'decomposed (104w rolling betas)')
             st.plotly_chart(figd, use_container_width=True)

     with cc2:
         st.subheader('Fundamental — supportive or not?')
         st.caption('percentile = current value within its own 5y history. '
                    '"supportive" = sign(current cross-sectional z) x sign of '
                    'the factor\'s historically measured IC on this universe.')
         rows = []
         for nm, (pan, ic, t_, note) in FACTORS.items():
             if pan is None or ccy not in getattr(pan, 'columns', []):
                 continue
             s = pan[ccy].dropna()
             if len(s) < 30:
                 continue
             z_now = zsec(pan).loc[s.index[-1], ccy] if len(pan) else np.nan
             sup = '—'
             if abs(ic) >= 0.015 and not pd.isna(z_now):
                 sup = '✅ yes' if np.sign(z_now) * np.sign(ic) > 0 else '❌ against'
             rows.append({'factor': nm, 'value': s.iloc[-1],
                          '5y %ile': pctile(s), 'XS z': z_now,
                          'hist IC': ic, 'supportive': sup, 'status': note})
         st.dataframe(pd.DataFrame(rows).set_index('factor')
                      .style.format({'value': '{:+.2f}', '5y %ile': '{:.0f}',
                                     'XS z': '{:+.2f}', 'hist IC': '{:+.3f}'}),
                      height=330)

         st.subheader('Sentiment — percentile now')
         for nm, pan in SENTIMENT.items():
             if ccy not in pan.columns:
                 continue
             s = pan[ccy].dropna()
             if len(s) < 30:
                 continue
             p = pctile(s)
             st.progress(min(max(p / 100, 0.0), 1.0),
                         text=f'{nm}: {s.iloc[-1]:+.2f}  ({p:.0f}th pct of 5y)')
         st.caption('⚠️ evidence note: options-based sentiment was tested '
                    'cross-sectionally (RR z IC −0.014, VRP −0.022) — '
                    'informative for CONTEXT and risk, not a ranking signal. '
                    'The only sentiment signal that survived testing is the '
                    'CNH fixing bias (sleeve D).')


 # ------------------------------------------------------- backtester ---------
 with tabs[2]:
     st.subheader('Strategy backtester')
     b1, b2, b3 = st.columns([2, 2, 1])
     strat = b1.selectbox('strategy', [
         'LIVE composite (2xCarry/vol + 1xESI)',
         'custom factor blend',
         'technical cross-sectional',
         'TSMOM (time-series trend)',
         'BofA-style short-term fair value (fade)',
         'ML Ridge (walk-forward, precomputed)'])
     uni = b2.multiselect('universe', TRADED, default=TRADED)
     cost = b3.number_input('cost bp/side', 0.0, 10.0, 1.0, 0.5)

     score = None
     note = ''
     if strat.startswith('LIVE'):
         w_c = st.slider('carry/vol weight', 0.0, 4.0, 2.0, 0.5)
         w_e = st.slider('ESI weight', 0.0, 4.0, 1.0, 0.5)
         n2 = w_c * zc.fillna(0) + w_e * ze.fillna(0)
         d2 = w_c * zc.notna() + w_e * ze.notna()
         score = (n2 / d2.replace(0, np.nan)).rolling(3).mean()
         note = 'the production signal - evidence: IC 0.080 (t 7.1) at 2:1'
     elif strat.startswith('custom'):
         cols = st.columns(4)
         wts, i = {}, 0
         for nm, (pan, ic, t_, _) in FACTORS.items():
             if pan is None or pan.empty:
                 continue
             wts[nm] = cols[i % 4].slider(nm, -2.0, 2.0,
                                          2.0 if 'carry/realvol' in nm else
                                          (1.0 if 'ESI' in nm else 0.0), 0.5)
             i += 1
         nn = sum(w * zsec(FACTORS[nm][0]).fillna(0) for nm, w in wts.items() if w)
         dd = sum(abs(w) * zsec(FACTORS[nm][0]).notna()
                  for nm, w in wts.items() if w)
         if isinstance(nn, pd.DataFrame):
             score = (nn / dd.replace(0, np.nan)).rolling(3).mean()
         note = 'build your own blend - the IC column in tab 2 tells you which ' \
                'members historically carried signal'
     elif strat.startswith('technical'):
         ind = st.selectbox('indicator', ['mom 4w', 'mom 12w', 'RSI14 (faded)',
                                          'BB z (faded)', 'MA cross 50/200'])
         fade = st.checkbox('fade (reverse sign)', value=False)
         px = -np.log(spot[TRADED])
         if ind == 'mom 4w':
             score = px.diff(4)
         elif ind == 'mom 12w':
             score = px.diff(12)
         elif ind.startswith('RSI'):
             d = px.diff()
             up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
             dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
             score = -(100 - 100 / (1 + up / dn.replace(0, np.nan)) - 50)
         elif ind.startswith('BB'):
             m = px.rolling(20).mean()
             s_ = px.rolling(20).std()
             score = -((px - m) / s_.replace(0, np.nan))
         else:
             score = px.rolling(10).mean() - px.rolling(40).mean()
         if fade:
             score = -score
         note = 'evidence: every variant tested dead on this universe ' \
                '(best IC +0.020, t 1.6) - see for yourself'
     elif strat.startswith('TSMOM'):
         k = st.slider('lookback (weeks)', 4, 52, 12, 4)
         score = tot_w[TRADED].rolling(k).sum()
         note = 'evidence: Sharpe 0.04-0.30 across lookbacks, maxDD -14..-20%'
     elif strat.startswith('BofA'):
         mis = pd.read_csv('analysis/stfv_misalignment.csv', index_col=0,
                           parse_dates=True) if \
             os.path.exists('analysis/stfv_misalignment.csv') else None
         if mis is None:
             st.warning('run the STFV precompute first (see repo)')
         else:
             score = -mis.reindex(columns=TRADED).rolling(2).mean()
         note = 'rolling 52w return regression on rate-diff/ToT/DXY drivers; ' \
                'fade the cumulated 4w residual. evidence: IC -0.007, book ' \
                '0.06 on this EM universe (it is a G10 tool - misalignments ' \
                'in managed EM currencies persist by design)'
     else:
         mlp = 'analysis/ml_predictions.csv'
         if os.path.exists(mlp):
             raw = pd.read_csv(mlp, header=[0, 1], index_col=0, parse_dates=True)
             score = raw['Ridge (a=10)'].reindex(columns=TRADED)
             note = 'TFRZ-style walk-forward ridge (chars x global interactions). ' \
                    'evidence: IC +0.032 (t 2.3) but book +0.11 vs live 1.30 ' \
                    'on the same weeks - significant IC, no book value'
         else:
             st.warning('run ml_factor.py first')

     if score is not None and len(uni) >= 4:
         r, Wl = run_xs_book(score, cost_bp=cost, universe=uni)
         bench, _ = run_xs_book(COMP, cost_bp=cost, universe=uni)
         stt = pd.DataFrame({'selected': stats_row(r),
                             'LIVE benchmark': stats_row(bench)}).T
         st.info(f'📖 {note}')
         st.dataframe(stt.style.format('{:+.2f}'))
         figb = go.Figure()
         figb.add_trace(go.Scatter(x=r.index, y=100 * r.cumsum(),
                                   name='selected', line=dict(width=2)))
         figb.add_trace(go.Scatter(x=bench.index, y=100 * bench.cumsum(),
                                   name='LIVE benchmark',
                                   line=dict(width=1.5, color='#888')))
         figb.update_layout(height=330, yaxis_title='cumulative %', **PLOT_BG,
                            margin=dict(l=10, r=10, t=20, b=10))
         st.plotly_chart(figb, use_container_width=True)
         contrib = (Wl * tot_w[Wl.columns]).sum() * 100
         figc = go.Figure(go.Bar(x=contrib.index, y=contrib.values,
                                 marker_color=['#2e8f5b' if v >= 0 else '#b7333a'
                                               for v in contrib.values]))
         figc.update_layout(height=240, yaxis_title='total contribution %', **PLOT_BG,
                            margin=dict(l=10, r=10, t=20, b=10))
         st.plotly_chart(figc, use_container_width=True)


 # ------------------------------------------------------------- ML lab -------
 with tabs[3]:
     st.subheader('ML Lab — walk-forward multi-factor (honest results)')
     st.markdown('''
 Method: **Taylor–Filippou–Rapach–Zhou (CEPR DP15305)** — country
 characteristics *interacted with global financial conditions*, regularized
 models, strict expanding walk-forward (first prediction after 3y of training,
 refit every 26 weeks, nothing sees the future).

 | model | walk-forward IC | book Sharpe | live composite (same weeks) |
 |---|---|---|---|
 | Ridge (α=10), 22 features | **+0.032 (t 2.25)** | +0.11 | **+1.30** |
 | HistGradientBoosting d3 | −0.005 (t −0.35) | −0.21 | +1.30 |

 **Read this honestly:** the ridge IC is statistically real — the interactions
 do carry some information — but it does not survive the book's discretization,
 and the tree model overfits outright. 13 currencies × ~550 training weeks is
 two orders of magnitude less data than the settings where ML famously works.
 The 2-member composite remains the best signal we can defend.
 ''')
     mlp = 'analysis/ml_predictions.csv'
     if os.path.exists(mlp):
         raw = pd.read_csv(mlp, header=[0, 1], index_col=0, parse_dates=True)
         S = raw['Ridge (a=10)'].reindex(columns=TRADED)
         last = S.dropna(how='all').iloc[-1].sort_values(ascending=False)
         figm = go.Figure(go.Bar(x=last.index, y=last.values,
                                 marker_color=['#2e8f5b' if v >= 0 else '#b7333a'
                                               for v in last.values]))
         figm.update_layout(**PLOT_BG, title=f'Ridge predictions, latest week '
                                  f'({S.dropna(how="all").index[-1].date()})',
                            height=300, margin=dict(l=10, r=10, t=40, b=10))
         st.plotly_chart(figm, use_container_width=True)
         # yearly IC of the ridge
         y = tot_w[TRADED].shift(-1)
         ics = {}
         for t in S.index:
             if t in y.index:
                 a, b = S.loc[t], y.loc[t]
                 m = a.notna() & b.notna()
                 if m.sum() >= 8:
                     ics[t] = a[m].rank().corr(b[m].rank())
         ics = pd.Series(ics)
         yr = ics.groupby(ics.index.year).mean()
         figy = go.Figure(go.Bar(x=yr.index.astype(str), y=yr.values))
         figy.update_layout(**PLOT_BG, title='Ridge IC by calendar year', height=260,
                            margin=dict(l=10, r=10, t=40, b=10))
         st.plotly_chart(figy, use_container_width=True)
     else:
         st.warning('run `python3 ml_factor.py` to populate this tab')


 # ------------------------------------------------- drivers & meta -----------
 with tabs[4]:
     d1, d2 = st.columns([3, 2])
     with d1:
         st.subheader('What drives each currency (PCA, trailing 2y)')
         st.caption('PCA on standardized weekly returns. PC1 = the dollar/EM '
                    'factor (corr 0.97 with the EM basket, 0.70 with DXY); '
                    'PC2 = carry / LATAM-vs-APAC factor; PC3 = residual LATAM '
                    'beta. Run `python3 pca_drivers.py` to refresh.')
         try:
             vd = pd.read_csv('analysis/pca_var_decomp.csv', header=[0, 1],
                              index_col=0)['roll104'] * 100
             comp_names = {'PC1': 'dollar / EM factor',
                           'PC2': 'carry / region factor',
                           'PC3': 'LATAM residual', 'idio': 'idiosyncratic'}
             figp = go.Figure()
             cols = {'PC1': '#00bdf2', 'PC2': '#f2c500', 'PC3': '#d6336c',
                     'idio': '#4a5568'}
             for pc in ['PC1', 'PC2', 'PC3', 'idio']:
                 figp.add_trace(go.Bar(x=vd.index, y=vd[pc],
                                       name=comp_names[pc],
                                       marker_color=cols[pc]))
             figp.update_layout(barmode='stack', height=340,
                                yaxis_title='% of variance',
                                legend=dict(orientation='h'),
                                margin=dict(l=10, r=10, t=10, b=10), **PLOT_BG)
             st.plotly_chart(figp, use_container_width=True)
             dom = vd[['PC1', 'PC2', 'PC3', 'idio']].idxmax(axis=1)
             st.caption('dominant driver now: ' + ' · '.join(
                 f'**{c}** {comp_names[dom[c]].split(" /")[0]}'
                 f' ({vd.loc[c, dom[c]]:.0f}%)'
                 for c in vd.index))
         except FileNotFoundError:
             st.warning('run `python3 pca_drivers.py` first')
     with d2:
         st.subheader('Strategy leaderboard — trailing 12m')
         st.caption('Sharpe of each library strategy, last 52 weeks vs full '
                    'sample. The tempting move is to switch into whatever is '
                    'hot - see the verdict below before doing that.')
         try:
             SL = pd.read_csv('analysis/strategy_library.csv', index_col=0,
                              parse_dates=True)

             def _sh(r):
                 r = r.dropna()
                 return r.mean() / r.std() * np.sqrt(52) if len(r) > 20 else np.nan
             lb = pd.DataFrame({
                 'last 12m': SL.iloc[-52:].apply(_sh),
                 'full sample': SL.apply(_sh)}).sort_values('last 12m',
                                                            ascending=False)
             st.dataframe(lb.style.format('{:+.2f}')
                          .background_gradient(cmap='RdYlGn', vmin=-1, vmax=2),
                          height=290)
         except FileNotFoundError:
             st.warning('run `python3 pca_drivers.py` first')

     st.subheader('Auto-selection backtest — does chasing the best 12m Sharpe work?')
     try:
         M = pd.read_csv('analysis/meta_selector.csv', index_col=0,
                         parse_dates=True)

         def _row(r):
             r = r.dropna()
             cum = r.cumsum()
             return {'Sharpe': r.mean() / r.std() * np.sqrt(52),
                     'ann %': 52 * r.mean() * 100,
                     'maxDD %': 100 * (cum - cum.cummax()).min()}
         res = pd.DataFrame({c: _row(M[c]) for c in M.columns}).T \
             .sort_values('Sharpe', ascending=False)
         c1_, c2_ = st.columns([1, 2])
         with c1_:
             st.dataframe(res.style.format('{:+.2f}'))
             st.error('**Verdict: chasing loses.** Monthly re-selection by '
                      'trailing 52w Sharpe scores 1.03 vs 1.33 for simply '
                      'holding the live composite (2015+, before switching '
                      'costs - 39 full-book switches in 138 months would make '
                      'it worse). Recent winners mean-revert; the composite\'s '
                      'edge is structural. The leaderboard above is for '
                      'MONITORING, not for switching.')
         with c2_:
             figm2 = go.Figure()
             for c in M.columns:
                 r = M[c].dropna()
                 figm2.add_trace(go.Scatter(
                     x=r.index, y=100 * r.cumsum(), name=c,
                     line=dict(width=2.2 if c == 'static LIVE' else 1.2)))
             figm2.update_layout(height=330, yaxis_title='cumulative %',
                                 legend=dict(orientation='h'),
                                 margin=dict(l=10, r=10, t=10, b=10),
                                 **PLOT_BG)
             st.plotly_chart(figm2, use_container_width=True)
     except FileNotFoundError:
         st.warning('run `python3 pca_drivers.py` first')


 # ------------------------------------------------------------- news ---------
 with tabs[5]:
     st.subheader('News & official data (auto-pulled)')
     try:
         import feedparser
         # sandboxed/proxied environments: trust the local proxy CA if present
         # (no effect on a normal machine - the file simply doesn't exist)
         _ca = '/root/.ccr/ca-bundle.crt'
         if os.path.exists(_ca):
             os.environ.setdefault('SSL_CERT_FILE', _ca)
             os.environ.setdefault('REQUESTS_CA_BUNDLE', _ca)
         sel = st.multiselect('countries', list(CCY_COUNTRY.values()),
                              default=['China', 'Brazil', 'Mexico'])
         n_items = st.slider('headlines per country', 3, 15, 6)
         for country in sel:
             st.markdown(f'#### {country}')
             q = country.replace(' ', '+') + '+currency+central+bank'
             url = (f'https://news.google.com/rss/search?q={q}'
                    '&hl=en-US&gl=US&ceid=US:en')
             try:
                 feed = feedparser.parse(url)
                 for e in feed.entries[:n_items]:
                     ts = getattr(e, 'published', '')[:16]
                     st.markdown(f'- [{e.title}]({e.link})  \n'
                                 f'  <span style="color:#888;font-size:0.8em">'
                                 f'{ts}</span>', unsafe_allow_html=True)
             except Exception as ex:
                 st.warning(f'feed failed: {ex}')
     except ImportError:
         st.warning('pip install feedparser for the news tab')
     st.markdown('''
 ---
 **Official data quick links** (weekly ritual):
 [PBOC fix](http://www.pbc.gov.cn/en/) ·
 [BSP](https://www.bsp.gov.ph/) · [BOT](https://www.bot.or.th/en/) ·
 [BI](https://www.bi.go.id/en/) · [RBI](https://www.rbi.org.in/) ·
 [BOK](https://www.bok.or.kr/eng/) · [CBC Taiwan](https://www.cbc.gov.tw/en/) ·
 [MAS](https://www.mas.gov.sg/) · [BCB](https://www.bcb.gov.br/en) ·
 [Banxico](https://www.banxico.org.mx/indexen.html) ·
 [BCCh](https://www.bcentral.cl/en/) · [NBP](https://nbp.pl/en/) ·
 [MNB](https://www.mnb.hu/en) ·
 [TE calendar](https://tradingeconomics.com/calendar)
 ''')


 # ------------------------------------------------- all currencies -----------
 with tabs[6]:
     st.subheader('All currencies - spot, model fair value, technical bounds')
     st.caption('black = daily USD/CCY spot (2y) · cyan = model-implied '
                'short-term fair value (rolling 52w driver regression, 13w '
                'cumulated residual; FV above spot = currency rich vs '
                'fundamentals) · shaded = Bollinger 20d +-2sd as the technical '
                'upper/lower bound. FV is context, not a signal: fading it '
                'scored IC -0.007 on this universe.')
     lookback = st.slider('lookback (trading days)', 120, 750, 500, 10)
     FV = fair_value(L)
     ncol = 3
     rows_n = (len(TRADED) + ncol - 1) // ncol
     figg = make_subplots(rows=rows_n, cols=ncol, subplot_titles=TRADED,
                          vertical_spacing=0.06, horizontal_spacing=0.05)
     for i, c in enumerate(TRADED):
         rr, cc_ = i // ncol + 1, i % ncol + 1
         if spd is None or c not in spd.columns:
             continue
         px = spd[c].dropna().iloc[-lookback:]
         m = px.rolling(20).mean()
         sdev = px.rolling(20).std()
         up, dn = m + 2 * sdev, m - 2 * sdev
         figg.add_trace(go.Scatter(x=px.index, y=up, line=dict(width=0),
                                   showlegend=False, hoverinfo='skip'), rr, cc_)
         figg.add_trace(go.Scatter(x=px.index, y=dn, fill='tonexty',
                                   fillcolor='rgba(120,140,170,0.18)',
                                   line=dict(width=0), showlegend=False,
                                   hoverinfo='skip'), rr, cc_)
         figg.add_trace(go.Scatter(x=px.index, y=px, name=c,
                                   line=dict(color='#e6edf3', width=1.2),
                                   showlegend=False), rr, cc_)
         if c in FV.columns:
             fv = FV[c].dropna()
             fv = fv[fv.index >= px.index.min()]
             figg.add_trace(go.Scatter(x=fv.index, y=fv, name='FV',
                                       line=dict(color='#00bdf2', width=1.6,
                                                 dash='dot'),
                                       showlegend=False), rr, cc_)
     figg.update_layout(height=290 * rows_n, margin=dict(l=10, r=10, t=30, b=10),
                        **PLOT_BG)
     figg.update_annotations(font_size=12)
     st.plotly_chart(figg, use_container_width=True)
     # rich/cheap summary table
     rowsr = []
     for c in TRADED:
         if c not in FV.columns or spd is None or c not in spd.columns:
             continue
         fv = FV[c].dropna()
         px = spd[c].dropna()
         if fv.empty or px.empty:
             continue
         gap = 100 * np.log(px.iloc[-1] / fv.iloc[-1])
         rowsr.append({'ccy': c, 'spot': px.iloc[-1], 'fair value': fv.iloc[-1],
                       'gap %': gap,
                       'read': ('CCY RICH vs fundamentals' if gap < -0.5 else
                                'CCY CHEAP vs fundamentals' if gap > 0.5 else
                                'near fair')})
     if rowsr:
         st.dataframe(pd.DataFrame(rowsr).set_index('ccy')
                      .style.format({'spot': '{:.4g}', 'fair value': '{:.4g}',
                                     'gap %': '{:+.2f}'}), height=350)
         st.caption('gap = log(spot/FV): spot BELOW fair value (gap<0) means '
                    'the currency has appreciated past its drivers = rich.')


# ==================================================================== entry ==
def _streamlit_ctx():
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        return get_script_run_ctx() is not None
    except Exception:
        return False


if _streamlit_ctx():
    run_dashboard()
elif __name__ == '__main__':
    import sys as _sys
    if len(_sys.argv) > 1 and _sys.argv[1] == 'fetch':
        fetch_main(_sys.argv[2:] or None)
    else:
        cli_main()
