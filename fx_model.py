#!/usr/bin/env python3
"""
FX WEEKLY SIGNAL MODEL - single self-contained file.

Everything from the raw workbook to the weekly order sheet lives here: data
cleaning, the three sleeves, the backtest, the P&L attribution and the charts.
Nothing else in the repo is imported, so this file can be copied to another
machine on its own.

    pip install pandas numpy openpyxl matplotlib
    python3 fx_model.py                                 # auto-finds the .xlsx here
    python3 fx_model.py FX_Model_Data.xlsx              # backtest + charts + sheet
    python3 fx_model.py FX_Model_Data.xlsx --outdir out
    python3 fx_model.py --clean out/clean.csv.gz        # re-run without the xlsx

WHAT THE MODEL IS
-----------------
Three books that bet on different things, each scaled to 5% annualized vol,
then combined 50/25/25. Pairwise correlations are near zero, which is the only
reason the stack beats its best single sleeve.

  A  cross-sectional RV, 14 EM currencies, self-funded long/short   50%
       signal  = z( 2*z(carry/realized vol) + 1*z(ESI 4w change) ), 3w smoothed
       book    = |z|>0.5 gate, long top3 / short bottom3, held while top5/bottom5
       sizing  = 1/ATM-vol within each leg, legs sum to +-1 (net 0, USD-neutral)
  B  EM beta versus a funding basket                                25%
       long an equal-vol basket of the same 14, short 50% CAD + 50% G3.
       Funding leg choice is the whole trade: the same EM basket funded 100% USD
       is Sharpe -0.11 since 2013; funded by the basket it is +0.66.
  D  CNH PBOC fixing bias                                           25%
       CCF = (actual CNY fix - Bloomberg fixing survey)/fix in bp. Trade WITH
       the pressure the fix reveals (+sign), not with the central bank, and only
       when the fix leans against the 4-week spot trend and 1M vol z < 1.5.

WHAT IS DELIBERATELY NOT IN IT
------------------------------
Every factor below was built and measured on this data and did not survive:
momentum (4w/12w/equity), risk reversals, implied-vol slope, govt curve slope
and curvature, CTOT, commodity baskets, realized skewness, rate momentum, North
Asia foreign equity flows, daily technicals (RSI/Bollinger/MA cross) both
standalone and as an entry gate, CDS, REER, terms of trade, and a forward-points
mean-reversion sleeve (killed by execution: an FX swap held to maturity has no
mark-to-market). Two published strategies were also tested and failed on this
universe: Lustig-Roussanov-Verdelhan average-forward-discount timing (t 0.83 at
the 52-week horizon, every implementation loses) and Ghayur-Heaney-Platt
portfolio blending (+0.15 Sharpe, difference t 0.76, worse in 2023-26).

The composite has two members because that is what the evidence supports, not
because the other data is unread.

CONVENTIONS
-----------
* spot is USD/XXX everywhere: a HIGHER number means a STRONGER dollar, so the
  return of being long the local currency is -dlog(spot).
* carry is CIP-consistent: 1M implied yield minus 1M SOFR. The raw FX.CARRY
  columns have 2016-2020 holes for CNH/INR/TWD and start only in 2020 for MYR.
* returns are EXCESS returns already - these are unfunded forwards, so the
  risk-free rate must NOT be subtracted a second time when computing Sharpe.
* ret[t] is the return REALIZED over the week ending t, earned on weights
  decided at t-1. No signal ever sees its own week's return.
* forward fill limits: weekly sources 2 weeks, monthly sources 8 weeks; past
  that the cell stays blank and the currency drops out of that week's book.
"""
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
    print('ALL legs cash-settled (NDF/CSF) incl. EUR/JPY/CAD funding - roll BEFORE')
    print('each leg\'s fixing date; G10 legs fix WM/Refinitiv 4pm London.')
    print('Sizes are % of total book notional at a 5% vol target per sleeve.')


# ============================================================== 7. CLI ======

def bundle_from_clean(dirpath='data/clean/clean_csv'):
    """The 8-table bundle the backtest consumes, read from the full-pipeline
    output so the workbook is only parsed once."""
    def L(n):
        return pd.read_csv(os.path.join(dirpath, n + '.csv'),
                           index_col=0, parse_dates=True)
    return {'spot': L('L1_spot_usd_all30'), 'carry': L('L1_carry_1m_ann'),
            'rvol': L('L1_vol_realized_1m'), 'atm': L('L1_vol_implied_atm_1m'),
            'esi': L('L1_esi'), 'fwd_1w': L('L1_fwd_pts_1w'),
            'fwd_1m': L('L1_fwd_pts_1m'),
            'fix': L('L1_country_factors')[['CNY_FIX', 'BBG_CNY_FIX']]}


def _find_workbook():
    """No path given: look for the workbook next to where you run the script
    (and next to the script itself). Prefers FX_Model_Data*.xlsx, ignores
    Excel lock files (~$...), picks the most recently modified match."""
    import glob as _g
    here = os.path.dirname(os.path.abspath(__file__))
    dirs = ['.', here] if os.path.abspath('.') != here else ['.']
    for pat in ('FX_Model_Data*.xlsx', 'FX_Model*.xlsx', '*.xlsx'):
        cands = []
        for d in dirs:
            cands += [p for p in _g.glob(os.path.join(d, pat))
                      if not os.path.basename(p).startswith('~$')]
        if cands:
            return max(cands, key=os.path.getmtime)
    return None


def main():
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
        wb = _find_workbook()
        if wb:
            a.xlsx = wb
            print(f'auto-detected workbook: {wb}')
        elif os.path.exists(os.path.join(a.outdir, 'clean.csv.gz')):
            a.clean = os.path.join(a.outdir, 'clean.csv.gz')
            print(f'no workbook found - using cached {a.clean}')
        else:
            ap.error('no .xlsx found in this folder - put FX_Model_Data.xlsx '
                     'next to this script, or pass a path / --clean <file>')
    os.makedirs(a.outdir, exist_ok=True)

    if a.clean and os.path.exists(a.clean):
        T = load_clean(a.clean)
        print(f'loaded clean data from {a.clean}')
    elif '_PIPE' in globals():
        # full pipeline: parses the workbook once, writes ALL tables the
        # dashboard needs (data/clean/clean_csv, 58 tables + pretty workbook),
        # then feeds the backtest from the same output
        print(f'running full pipeline on {a.xlsx} ...')
        _wb, L1, L2, notes, _ca, _sanity = _PIPE['build'](a.xlsx, 'data/clean')
        _PIPE['write_outputs']('data/clean', L1, L2, notes)
        T = bundle_from_clean()
        p = a.clean or os.path.join(a.outdir, 'clean.csv.gz')
        save_clean(T, p)
        print('pipeline complete - dashboard data ready under data/clean/')
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


if __name__ == '__main__':
    main()
