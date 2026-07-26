"""
FX_Model_Data.xlsx  ->  data/clean/clean_csv/*.csv  +  FX_Model_Clean.xlsx

Data-cleaning pipeline v2 for the FX signal model.

What changed vs data_pipeline.py
--------------------------------
* START moved to 2013-01-01 (all output tables filtered to Fridays >= 2013).
* Currency coverage extended from ASIA9 to the 14 TRADED currencies
  (ASIA9 + EM5) plus the 3 FUNDING legs (EUR/JPY/CAD).  USD is the base.
* Carry is RECONSTRUCTED CIP-consistently as
        carry_XXX (vs USD, ann. %) = (XXX 1M implied yield) - (SOFR 1M)
  because the raw FX.CARRY.* columns have large 2016-2020 gaps for
  CNH/INR/TWD and only start 2020 for MYR.  The implied-yield source has
  no gaps, so every traded/funding currency now has carry back to 2013.
* Forward points use a dual source: Bloomberg (primary) with a Citi
  CVTSHIST fall-back where Bloomberg is missing.
* Vol / risk-reversal for MYR/THB/IDR/TWD prefer the Bloomberg supplement
  (the main Citi block is stale/frozen for these four).  Only 1M is built.
* New tables: L1_spot_usd_traded, L1_pi_leveraged, L1_isi, L1_country_factors,
  and (in the pretty workbook) the funding legs live in the "all" tables.
* New L2 features: carry_to_realvol (with a 1.0% realized-vol denominator
  floor), fwdpts_chg_4w, pi_lv_minus_rm.
* A completeness_report() prints, for the latest Friday, which traded
  currency x model-input cells are PRESENT vs MISSING, so a prediction is
  only produced when every input exists.

Reused verbatim from data_pipeline.py: to_friday, wide_to_friday, load_cvts,
load_block_pairs (imported), plus the L2 / coverage / pretty-Excel machinery
(write_pretty_excel copied and extended with the new tables).

Usage:  python3 data_pipeline_v2.py <path_to_FX_Model_Data.xlsx> [output_dir]
"""
import sys, os, datetime
import numpy as np
import pandas as pd
import openpyxl

# ---- reuse the existing helpers verbatim ----
from data_pipeline import (to_friday, wide_to_friday, load_cvts, load_block_pairs,
                           WEEKLY_FFILL, MONTHLY_FFILL)

START = pd.Timestamp('2013-01-01')
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
EM5    = ['BRL', 'MXN', 'CLP', 'PLN', 'HUF']
TRADED = ASIA9 + EM5                       # 14 traded currencies
FUNDING = ['EUR', 'JPY', 'CAD']            # G4 funding legs (USD is base)
ALLCCY = TRADED + FUNDING                  # 17


# ================================================================ low-level
def _grid(rows):
    return [r for r in rows]


def col_series(ws, date_idx, val_idx, data_row):
    """One (date_col, value_col) pair -> numeric Series, ascending, deduped."""
    rec = {}
    for row in ws.iter_rows(min_row=data_row, values_only=True):
        d = row[date_idx] if date_idx < len(row) else None
        if isinstance(d, datetime.datetime):
            rec[d] = row[val_idx] if val_idx < len(row) else None
    s = pd.Series(rec).sort_index()
    return pd.to_numeric(s, errors='coerce')


def detect_date_cols(ws, data_row, c0, c1, nscan=15):
    """Columns in [c0,c1) whose data cells are datetimes (block date columns)."""
    scan = []
    for i, row in enumerate(ws.iter_rows(min_row=data_row, values_only=True)):
        scan.append(row)
        if i + 1 >= nscan:
            break
    is_date = {}
    for c in range(c0, c1):
        vals = [r[c] for r in scan if c < len(r) and r[c] is not None]
        if not vals:
            is_date[c] = False
            continue
        is_date[c] = sum(isinstance(v, datetime.datetime) for v in vals) > len(vals) / 2
    return is_date


def auto_blocks(ws, header_row, data_row, c0, c1, name_fn):
    """Auto-detect (date_col -> [value_cols]) blocks and return dict name->Series.

    A value column is any non-date column in [c0,c1) that carries a header
    label; it is bound to the nearest date column at or to its left.
    name_fn(col, header_value) -> series name (or None to skip).
    """
    hdr = next(ws.iter_rows(min_row=header_row, max_row=header_row, values_only=True))
    is_date = detect_date_cols(ws, data_row, c0, c1)
    date_cols = sorted([c for c in range(c0, c1) if is_date.get(c)])
    groups = {}                                    # date_col -> [(vcol,name)]
    for c in range(c0, c1):
        if is_date.get(c):
            continue
        h = hdr[c] if c < len(hdr) else None
        if h is None or (isinstance(h, str) and not h.strip()):
            continue
        left = [d for d in date_cols if d <= c]
        if not left:
            continue
        name = name_fn(c, h)
        if name is None:
            continue
        groups.setdefault(left[-1], []).append((c, name))
    pairs, labels = [], []
    for dcol, items in groups.items():
        pairs.append((dcol, [v for v, _ in items]))
        labels.append([n for _, n in items])
    if not pairs:
        return {}
    return load_block_pairs(ws, pairs, labels, data_row)


def clip_start(df):
    """Drop all rows before START (applied after Friday alignment)."""
    return df[df.index >= START]


def L1put(L1, name, df):
    L1[name] = clip_start(df.dropna(how='all'))


# ================================================================ main build
def build(xlsx_path, outdir):
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    L1, L2, notes = {}, {}, []

    # -------------------------------------------------- 1. SPOT (Top 30) ----
    ws = wb['Top 30 FX Spot']
    spot = load_cvts(ws, header_row=5, data_row=6)             # date col B
    # Positional dedupe: CLP appears twice with IDENTICAL headers (cols AC & AE).
    keep_idx, names, seen = [], [], set()
    for i, c in enumerate(spot.columns):                       # FX.SPOT.EUR.USD.CITI
        if not (isinstance(c, str) and c.startswith('FX.SPOT')):
            continue
        base, quote = c.split('.')[2], c.split('.')[3]
        ccy = base if base != 'USD' else quote
        if ccy in seen:                                        # drop the duplicate CLP
            continue
        seen.add(ccy)
        keep_idx.append(i)
        names.append((ccy, base != 'USD'))                     # (name, invert?)
    spot = spot.iloc[:, keep_idx].copy()
    spot.columns = [n for n, _ in names]
    for n, inv in names:
        if inv:                                                # EUR/GBP/AUD/NZD -> invert to USD/XXX
            spot[n] = 1.0 / spot[n]
    # MYR side block: date col AG(32), value AH(33)
    myr = col_series(ws, 32, 33, 6).dropna()
    if len(myr):
        spot = spot.join(pd.DataFrame({'MYR': myr}), how='outer')
    spot_fri = wide_to_friday(spot)
    L1put(L1, 'spot_usd_all30', spot_fri)
    L1put(L1, 'spot_usd_asia9', spot_fri[[c for c in ASIA9 if c in spot_fri.columns]])
    L1put(L1, 'spot_usd_traded', spot_fri[[c for c in TRADED if c in spot_fri.columns]])

    # -------------------------------------------------- 2. FORWARD POINTS ----
    ws = wb['FX Forward']
    # primary Bloomberg block (labels at r7), from col AL(37) onward
    def fwd_name(col, h):
        return None
    r7 = next(ws.iter_rows(min_row=7, max_row=7, values_only=True))
    def bbg_name(col, h):
        pair, tenor = str(h).split()                # "USDBRL 1W"
        if pair.startswith('USD'):
            ccy = pair[3:]
        elif pair == 'EURUSD':
            ccy = 'EUR'
        else:
            ccy = pair[3:]                          # EURHUF -> HUF, EURPLN -> PLN
        return f'{ccy}_{tenor}'
    prim = auto_blocks(ws, 7, 8, 37, 93, bbg_name)
    # backup Citi CVTSHIST block: header r4, data r5, date col B
    bkp = load_cvts(ws, header_row=4, data_row=5)
    bmap = {}
    for c in bkp.columns:
        if not (isinstance(c, str) and c.startswith('FX.FORWARD')):
            continue
        p = c.split('.')
        base, quote, tenor = p[3], p[4], p[5]
        ccy = quote if base == 'USD' else base      # EUR.USD -> EUR
        bmap[c] = f'{ccy}_{tenor}'
    bkp = bkp[list(bmap)].rename(columns=bmap)
    for tenor in ['1W', '1M']:
        pcols = {k.split('_')[0]: v for k, v in prim.items() if k.endswith('_' + tenor)}
        pdf = to_friday(pcols, WEEKLY_FFILL) if pcols else pd.DataFrame()
        bcols = [c for c in bkp.columns if c.endswith('_' + tenor)]
        bdf = wide_to_friday(bkp[bcols].rename(columns=lambda c: c.split('_')[0])) if bcols else pd.DataFrame()
        merged = pdf.combine_first(bdf) if len(pdf) else bdf     # primary wins
        merged = merged[[c for c in ALLCCY if c in merged.columns]]
        L1put(L1, f'fwd_pts_{tenor.lower()}', merged)
    notes.append('Forward points: Bloomberg (primary) with Citi CVTSHIST fall-back where Bloomberg '
                 'is missing. HUF/PLN primary points are EUR-cross (EURHUF/EURPLN) points; the Citi '
                 'fall-back is USD-cross. Convention noted per user instruction.')

    # -------------------------------------------------- 10/carry. IMPLIED YIELDS + SOFR ----
    ws = wb['Govt & FX Implied Yield']
    # 1M implied yields
    iy1m = {}
    a9 = {'KRW': 110, 'THB': 111, 'INR': 112, 'PHP': 113, 'CNH': 114,
          'IDR': 115, 'MYR': 116, 'SGD': 117, 'TWD': 118}
    em = {'PLN': 139, 'BRL': 140, 'HUF': 141, 'MXN': 142, 'EUR': 143, 'JPY': 144, 'CAD': 145}
    for k, c in a9.items():
        iy1m[k] = col_series(ws, 109, c, 7)
    for k, c in em.items():
        iy1m[k] = col_series(ws, 129, c, 7)
    iy1m['CLP'] = col_series(ws, 148, 149, 7)
    sofr1m = col_series(ws, 129, 146, 7)
    # 12M implied yields
    iy12m = {}
    a9_12 = {'KRW': 119, 'THB': 120, 'INR': 121, 'PHP': 122, 'CNH': 123,
             'IDR': 124, 'MYR': 125, 'SGD': 126, 'TWD': 127}
    em_12 = {'CLP': 130, 'PLN': 131, 'BRL': 132, 'HUF': 133, 'MXN': 134, 'EUR': 135, 'JPY': 136, 'CAD': 137}
    for k, c in a9_12.items():
        iy12m[k] = col_series(ws, 109, c, 7)
    for k, c in em_12.items():
        iy12m[k] = col_series(ws, 129, c, 7)
    sofr12m = col_series(ws, 129, 138, 7)

    L1put(L1, 'fx_implied_yield_1m', to_friday(iy1m, WEEKLY_FFILL))
    L1put(L1, 'fx_implied_yield_12m', to_friday(iy12m, WEEKLY_FFILL))

    # CIP-consistent carry = implied yield - SOFR, per currency
    sofr1m_f = to_friday({'SOFR': sofr1m}, WEEKLY_FFILL)['SOFR']
    iy1m_f = to_friday(iy1m, WEEKLY_FFILL)
    carry = iy1m_f.sub(iy1m_f.index.to_series().map(sofr1m_f), axis=0)
    carry = carry[[c for c in ALLCCY if c in carry.columns]]
    L1put(L1, 'carry_1m_ann', carry)

    # -------------------------------------------------- 4. REALIZED VOL ----
    cv = wb['Carry to Vol']
    cvd = load_cvts(cv, header_row=6, data_row=7)
    rvol = {}
    for c in cvd.columns:
        if isinstance(c, str) and 'REALIS' in c:                # FX.VOL.USD.BRL.ATM.1M.REALISED
            p = c.split('.')
            ccy = p[3] if p[2] == 'USD' else p[2]
            rvol[c] = ccy
    rv = cvd[list(rvol)].rename(columns=rvol)
    rv_f = wide_to_friday(rv)
    myr_rv = col_series(cv, 37, 42, 7).dropna()                 # Bloomberg MYR realized-vol supplement
    if len(myr_rv):
        myr_rv_f = to_friday({'MYR': myr_rv}, WEEKLY_FFILL)['MYR']
        if 'MYR' in rv_f.columns:
            rv_f['MYR'] = rv_f['MYR'].combine_first(myr_rv_f)
        else:
            rv_f = rv_f.join(pd.DataFrame({'MYR': myr_rv_f}))
    rv_f = rv_f[[c for c in ALLCCY if c in rv_f.columns]]
    L1put(L1, 'vol_realized_1m', rv_f)

    # -------------------------------------------------- 3. VOL & RR (1M) ----
    ws = wb['Vol & RR']
    vr = load_cvts(ws, header_row=7, data_row=8)
    rr, atm = {}, {}
    for c in vr.columns:
        if not isinstance(c, str) or not c.startswith('FX.VOL'):
            continue
        p = c.split('.')
        ccy = p[3] if p[2] == 'USD' else p[2]
        kind, tenor = p[4], p[5]
        if tenor != '1M':
            continue
        if kind in ('25RR', '10RR'):                            # CLP is 10-delta
            rr[c] = ccy
        elif kind == 'ATM':
            atm[c] = ccy
    rr_f = wide_to_friday(vr[list(rr)].rename(columns=rr))
    atm_f = wide_to_friday(vr[list(atm)].rename(columns=atm))
    # Bloomberg supplement (date col 37): 38 MYR25RR,39 MYRatm,40 THB25RR,41 THBatm,
    # 42 IDR25RR,43 IDRatm,44 TWD25RR,45 TWDatm  -> PREFER these for the 4 stale ccys.
    sup_rr = {'MYR': 38, 'THB': 40, 'IDR': 42, 'TWD': 44}
    sup_atm = {'MYR': 39, 'THB': 41, 'IDR': 43, 'TWD': 45}
    for ccy, ci in sup_rr.items():
        s = to_friday({ccy: col_series(ws, 37, ci, 8).dropna()}, WEEKLY_FFILL)[ccy]
        rr_f[ccy] = s.reindex(rr_f.index)                       # override stale main block
    for ccy, ci in sup_atm.items():
        s = to_friday({ccy: col_series(ws, 37, ci, 8).dropna()}, WEEKLY_FFILL)[ccy]
        atm_f[ccy] = s.reindex(atm_f.index)
    rr_f = rr_f[[c for c in ALLCCY if c in rr_f.columns]]
    atm_f = atm_f[[c for c in ALLCCY if c in atm_f.columns]]
    L1put(L1, 'rr25_1m', rr_f)
    L1put(L1, 'vol_implied_atm_1m', atm_f)
    notes.append('Vol/RR: MYR/THB/IDR/TWD use the Bloomberg supplement (main Citi block is '
                 'stale/frozen for these four). CLP risk reversal is 10-delta (only delta quoted). '
                 'Only the 1M tenor is produced.')

    # -------------------------------------------------- 5. REGIME ----
    ws = wb['Regime']
    reg_w = load_block_pairs(ws, [(1, [2, 3, 4, 5, 6, 7, 8, 9])],
                             [['DXY', 'MXWO', 'VIX', 'MOVE', 'EMFXVOL', 'G7FXVOL', 'US_HY_OAS', 'OVX']], 8)
    reg_c = load_block_pairs(ws, [(11, [12])], [['DB_G10_CARRY']], 8)
    reg_p = load_block_pairs(ws, [(14, [15])], [['US_ISM_PMI']], 8)
    regime = to_friday({**reg_w, **reg_c}, WEEKLY_FFILL).join(to_friday(reg_p, MONTHLY_FFILL))
    L1put(L1, 'regime', regime)

    # -------------------------------------------------- 6. COUNTRIES FACTOR ----
    ws = wb['Countries Factor']
    CF_MAP = {                                  # r6 ticker -> friendly column name
        'GSSGMID':  'SGD_NEER_MID',   'GSSGUP':  'SGD_NEER_UPPER', 'GSSGLOW': 'SGD_NEER_LOWER',
        'CNYMUSD':  'CNY_FIX',        'FCCNYFIX': 'BBG_CNY_FIX',   'CNYRINDX': 'CFETS_RMB_INDEX',
        'SETTOUR':  'THAI_TOURISM_EQ', 'THTATOTA': 'THAI_TOURIST_ARRIVALS',
        'KPCPNTFR': 'KOSPI_FOREIGN_NET', 'KPCPNTSE': 'KOSPI_FINANCIAL_NET', 'KSFINET': 'KOSPI_KOSDAQ_FOREIGN',
        'PEIMCRUV': 'INDIA_OIL_IMPORTS',
    }
    def cf_name(col, h):
        tk = str(h).split()[0]                  # ticker like 'GSSGMID'
        return CF_MAP.get(tk, tk)
    cf = auto_blocks(ws, 6, 8, 1, 26, cf_name)
    if cf:
        cff = to_friday(cf, WEEKLY_FFILL)
        L1put(L1, 'country_factors', cff)
        # DERIVED: PBOC counter-cyclical factor = actual CNY fix - BBG model fix
        # (negative = PBOC leans to support CNH / bullish; positive = tolerates depreciation).
        # Only defined from 2018-06 when the BBG model fix begins.
        if 'CNY_FIX' in cff.columns and 'BBG_CNY_FIX' in cff.columns:
            ccf = (cff['CNY_FIX'] - cff['BBG_CNY_FIX']).to_frame('CNH')
            L2['cny_ccf'] = clip_start(ccf.dropna(how='all'))
    notes.append('Country factors: SGD_NEER band (mid/upper/lower) = SGD valuation anchor; '
                 'CNY_FIX = PBOC USD/CNY central parity; BBG_CNY_FIX = Bloomberg CNY fixing survey (market expectation, from 2018-06); '
                 'CFETS_RMB_INDEX = CNY basket; THAI_TOURISM_EQ / THAI_TOURIST_ARRIVALS; '
                 'KOSPI_FOREIGN_NET / KOSPI_FINANCIAL_NET / KOSPI_KOSDAQ_FOREIGN = Korea equity flows; '
                 'INDIA_OIL_IMPORTS. L2_cny_ccf = CNY_FIX - BBG_CNY_FIX = PBOC fixing bias vs survey (regime-dependent, not a fixed sign) '
                 '(negative bullish CNH), available from 2018-06.')

    # -------------------------------------------------- 7. CDS 5Y ----
    cds = load_cvts(wb['CDS'], header_row=3, data_row=4)
    cmap = {'SBIIN': 'INR', 'KOREA': 'KRW', 'MALAYS': 'MYR', 'PTINDAJ': 'IDR',
            'PHILIP': 'PHP', 'BRAZIL': 'BRL', 'POLAND': 'PLN'}
    ren = {}
    for c in cds.columns:
        code = c.split('.')[2].split('-')[0]
        ren[c] = cmap.get(code, code)
    L1put(L1, 'cds_5y', wide_to_friday(cds.rename(columns=ren)))

    # -------------------------------------------------- 8. COMMODITIES ----
    ws = wb['Commodities']
    r5 = next(ws.iter_rows(min_row=5, max_row=5, values_only=True))
    def com_name(col, h):
        lab = r5[col] if col < len(r5) and r5[col] else h
        nm = str(lab).strip().replace(' ', '_').replace('-', '_').replace('/', '_')
        return {'S_1_Comdty': 'Soybean'}.get(nm, nm)           # soybean has no r5 label
    com = auto_blocks(ws, 6, 7, 1, 25, com_name)               # r6 tickers, r5 labels; cols to 25 (Korea Semi)
    # KOEISEU (Korea semi price) is monthly; separate it out
    monthly = {k: v for k, v in com.items() if 'KOEISEU' in k.upper() or 'Korea_Semi' in k}
    weekly = {k: v for k, v in com.items() if k not in monthly}
    comdf = to_friday(weekly, WEEKLY_FFILL)
    if monthly:
        comdf = comdf.join(to_friday(monthly, MONTHLY_FFILL))
    L1put(L1, 'commodities', comdf)

    # -------------------------------------------------- 9. CURRENT ACCOUNT ----
    ws = wb['Current Account']
    r5 = next(ws.iter_rows(min_row=5, max_row=5, values_only=True))
    cty = {'Korea': 'KRW', 'Thailand': 'THB', 'India': 'INR', 'Philippines': 'PHP',
           'China': 'CNH', 'Indonesia': 'IDR', 'Malaysia': 'MYR', 'Singapore': 'SGD',
           'Taiwan': 'TWD', 'Japan': 'JPY', 'Canada': 'CAD', 'Europe': 'EUR',
           'Brail': 'BRL', 'Brazil': 'BRL', 'Poland': 'PLN', 'Chile': 'CLP',
           'Hungary': 'HUF', 'Mexico': 'MXN'}
    ca_cols, ca_names = [], []
    for c in range(2, 19):
        lab = r5[c] if c < len(r5) else None
        if isinstance(lab, str) and lab.strip() in cty:
            ca_cols.append(c)
            ca_names.append(cty[lab.strip()])
    ca_raw = load_block_pairs(ws, [(1, ca_cols)], [ca_names], 7)
    ca_m = pd.DataFrame(ca_raw)
    L1put(L1, 'current_account_usdbn', to_friday(ca_raw, MONTHLY_FFILL))

    # -------------------------------------------------- 9b. CPI (monthly YoY) ----
    ws = wb['CPI']
    r6 = next(ws.iter_rows(min_row=6, max_row=6, values_only=True))
    cpi_lab = {'Brazil': 'BRL', 'Mexico': 'MXN', 'Chile': 'CLP', 'Hungary': 'HUF',
               'Japan': 'JPY', 'Europe': 'EUR', 'Canada': 'CAD', 'China': 'CNH',
               'Korea': 'KRW', 'India': 'INR', 'Indonesia': 'IDR', 'Philippines': 'PHP',
               'Thailand': 'THB', 'Taiwan': 'TWD', 'Singapore': 'SGD', 'Malaysia': 'MYR',
               'US': 'US'}
    main_cols, main_names, us_col, seen = [], [], None, set()
    for c in range(2, 22):                         # C..S share date col B(1); US = (date U=20, val V=21)
        lab = r6[c] if c < len(r6) else None
        if not (isinstance(lab, str) and lab.strip()):
            continue
        ccy = cpi_lab.get(lab.replace('CPI YoY', '').strip())
        if not ccy or ccy in seen:                 # skip duplicated Mexico column
            continue
        seen.add(ccy)
        if ccy == 'US':
            us_col = c
        else:
            main_cols.append(c)
            main_names.append(ccy)
    cpi_raw = load_block_pairs(ws, [(1, main_cols)], [main_names], 7)
    if us_col is not None:
        cpi_raw.update(load_block_pairs(ws, [(20, [us_col])], [['US']], 7))
    L1put(L1, 'cpi_yoy', to_friday(cpi_raw, MONTHLY_FFILL))

    # -------------------------------------------------- 10. GOVT YIELDS ----
    ws = wb['Govt & FX Implied Yield']
    # (date_col, [1Y,2Y,5Y,10Y value cols], currency)
    yblocks = [
        (1,  [2, 3, 5, 6],  'US'),          # US has an extra 3Y at col 4 (skipped)
        (8,  [9, 10, 11, 12],  'KRW'),
        (14, [15, 16, 17, 18], 'THB'),
        (20, [21, 22, 23, 24], 'IDR'),
        (26, [27, 28, 29, 30], 'PHP'),
        (32, [33, 34, 35, 36], 'CNH'),
        (44, [45, 46, 47, 48], 'MYR'),
        (50, [51, 52, 53, 54], 'SGD'),
        (56, [57, 58, 59, 60], 'TWD'),
        (62, [63, 64, 65, 66], 'EUR'),
        (62, [67, 68, 69, 70], 'CAD'),      # Canada shares Europe's date col
        (72, [73, 74, 75, 76], 'JPY'),
        (79, [80, 81, 82, 83], 'CLP'),
        (85, [86, 87, 88, 89], 'PLN'),
        (91, [92, 93, 94, 95], 'BRL'),
        (97, [98, 99, 100, 101], 'HUF'),
        (103, [104, 105, 106, 107], 'MXN'),
    ]
    ymap = {}
    for dcol, vcols, ccy in yblocks:
        got = load_block_pairs(ws, [(dcol, vcols)], [[f'{ccy}_{t}' for t in ['1Y', '2Y', '5Y', '10Y']]], 7)
        ymap.update(got)
    yields = to_friday(ymap, WEEKLY_FFILL)
    for t in ['1Y', '2Y', '5Y', '10Y']:
        cols = [c for c in yields.columns if c.endswith('_' + t)]
        L1put(L1, f'govt_yield_{t.lower()}', yields[cols].rename(columns=lambda c: c[:-len(t) - 1]))

    # -------------------------------------------------- 11. ECON SURPRISE ----
    esheet = 'Economics Surprise' if 'Economics Surprise' in wb.sheetnames else 'Economics Surpirse'
    es = load_cvts(wb[esheet], header_row=3, data_row=4)
    esi, isi = {}, {}
    for c in es.columns:
        if not isinstance(c, str):
            continue
        p = c.split('.')
        kind = p[2]                                   # ESI or ISI
        si = [x for x in p if x.startswith('SI_')]    # ISI has SI_CISI + SI_XX; take the last
        if not si:
            continue
        code = si[-1].replace('SI_', '')
        code = 'CNH' if code in ('CNY', 'CN') else code
        (esi if kind == 'ESI' else isi)[c] = code
    L1put(L1, 'esi', wide_to_friday(es[list(esi)].rename(columns=esi)))
    if isi:
        L1put(L1, 'isi', wide_to_friday(es[list(isi)].rename(columns=isi)))

    # -------------------------------------------------- 12. TERMS OF TRADE ----
    tot = load_cvts(wb['ToT'], header_row=4, data_row=5)
    ren = {}
    for c in tot.columns:
        if isinstance(c, str) and 'CTOT_' in c:
            code = c.split('CTOT_')[1].split(' ')[0].split('.')[0]
            ren[c] = 'CNH' if code == 'CNY' else code
    L1put(L1, 'ctot', wide_to_friday(tot[list(ren)].rename(columns=ren)))

    # -------------------------------------------------- 13. REER ----
    reer = load_cvts(wb['REER'], header_row=3, data_row=4)
    ren = {}
    for c in reer.columns:
        if not isinstance(c, str) or 'REER' not in c:
            continue
        ren[c] = 'USD_BROAD' if 'BROAD' in c else c.split('.')[-1].strip()
    L1put(L1, 'reer', wide_to_friday(reer[list(ren)].rename(columns=ren)))

    # -------------------------------------------------- 14. FX FLOWS / POSITION ----
    pi = load_cvts(wb['FX Flows & Position'], header_row=3, data_row=4)
    rm, lv, zrm, zlv = {}, {}, {}, {}
    for c in pi.columns:
        if not isinstance(c, str) or 'CITIPI' not in c:
            continue
        ccy = c.split('.')[2].replace('PI_', '')
        if c.endswith('PI_RM - CLOSE') or c.rstrip().endswith('PI_RM'):
            rm[c] = ccy
        elif c.endswith('PI_LV - CLOSE') or c.rstrip().endswith('PI_LV'):
            lv[c] = ccy
        elif 'PI_CFTCRM' in c:
            zrm[c] = ccy
        elif 'PI_CFTCLV' in c:
            zlv[c] = ccy
    L1put(L1, 'pi_realmoney', wide_to_friday(pi[list(rm)].rename(columns=rm)))
    L1put(L1, 'pi_leveraged', wide_to_friday(pi[list(lv)].rename(columns=lv)))
    L1put(L1, 'pi_rm_flow_z', wide_to_friday(pi[list(zrm)].rename(columns=zrm)))
    if zlv:
        L1put(L1, 'pi_lv_flow_z', wide_to_friday(pi[list(zlv)].rename(columns=zlv)))

    # -------------------------------------------------- 15. EQUITY ----
    ws = wb['Equity Performance']
    r5 = next(ws.iter_rows(min_row=5, max_row=5, values_only=True))
    tick2ccy = {'SHSZ300': 'CNH', 'NIFTY': 'INR', 'KOSPI': 'KRW', 'TWSE': 'TWD', 'STI': 'SGD',
                'SET': 'THB', 'FBMKLCI': 'MYR', 'JCI': 'IDR', 'PCOMP': 'PHP',
                'NKY': 'JPY', 'SPTSX': 'CAD', 'SX5E': 'EUR'}
    def eq_name(col, h):
        return tick2ccy.get(str(h).split()[0], None)
    eq = auto_blocks(ws, 5, 6, 1, 25, eq_name)
    L1put(L1, 'equity', to_friday(eq, WEEKLY_FFILL))

    # ============================================================ L2 features
    spot9 = L1['spot_usd_asia9']
    logspot = np.log(spot9)
    L2['log_spot'] = logspot
    L2['ret_1w'] = logspot.diff()
    L2['mom_12w_ex1w'] = logspot.shift(1) - logspot.shift(13)
    L2['skew_26w'] = L2['ret_1w'].rolling(26).skew()
    L2['vol_realized_13w_ann'] = L2['ret_1w'].rolling(13).std() * np.sqrt(52) * 100

    # carry-to-realvol with a 1.0% realized-vol denominator floor
    carry_t = L1['carry_1m_ann']
    realvol = L1['vol_realized_1m']
    common = [c for c in carry_t.columns if c in realvol.columns]
    denom = realvol[common].clip(lower=1.0)
    L2['carry_to_realvol'] = carry_t[common] / denom

    rr = L1['rr25_1m']
    L2['rr25_1m_z_52w'] = (rr - rr.rolling(52).mean()) / rr.rolling(52).std()
    L2['esi_chg_4w'] = L1['esi'].diff(4)
    L2['ctot_chg_13w'] = L1['ctot'].diff(13)
    L2['cds_chg_4w'] = L1['cds_5y'].diff(4)
    L2['equity_mom_12w'] = np.log(L1['equity']).shift(1) - np.log(L1['equity']).shift(13)
    ca_yoy = ca_m.sort_index().diff(12)
    L2['ca_yoy_usdbn'] = clip_start(to_friday({c: ca_yoy[c].dropna() for c in ca_yoy.columns}, MONTHLY_FFILL))

    # NEW: 4-week change in 1M forward points
    L2['fwdpts_chg_4w'] = L1['fwd_pts_1m'].diff(4)

    # NEW: leveraged flow z minus real-money flow z (divergence) where both exist
    if 'pi_lv_flow_z' in L1:
        zl, zr = L1['pi_lv_flow_z'], L1['pi_rm_flow_z']
        common_z = [c for c in zl.columns if c in zr.columns]
        L2['pi_lv_minus_rm'] = zl[common_z] - zr[common_z]

    # implied-yield slope 12M-1M
    L2['implied_yield_slope_12m_1m'] = L1['fx_implied_yield_12m'] - L1['fx_implied_yield_1m']

    # NEW: real yields (implied yield - CPI YoY) and real carry vs US
    if 'cpi_yoy' in L1:
        cpi = L1['cpi_yoy']
        for ten in ['1m', '12m']:
            iy = L1.get(f'fx_implied_yield_{ten}')
            if iy is not None:
                cc = [c for c in iy.columns if c in cpi.columns]
                L2[f'real_yield_{ten}'] = iy[cc] - cpi[cc]
        if 'US' in cpi.columns:
            cpi_diff = cpi.drop(columns=['US']).sub(cpi['US'], axis=0)
            L2['cpi_diff_vs_us'] = cpi_diff          # inflation differential (PPP drift)
            cc = [c for c in L1['carry_1m_ann'].columns if c in cpi_diff.columns]
            # real carry = nominal carry (implied yield - SOFR) minus inflation differential vs US
            L2['real_carry_1m'] = L1['carry_1m_ann'][cc] - cpi_diff[cc]

    # yield-curve features vs US
    y2, y10 = L1['govt_yield_2y'], L1['govt_yield_10y']
    slope = y10 - y2
    L2['curve_slope_10y2y'] = slope
    if 'US' in slope.columns:
        L2['slope_diff_vs_us'] = slope.drop(columns=['US'], errors='ignore').sub(slope['US'], axis=0)
    if 'US' in y2.columns:
        L2['ydiff_2y_vs_us'] = y2.drop(columns=['US'], errors='ignore').sub(y2['US'], axis=0)

    # align every L2 table to START as well
    for k in list(L2):
        L2[k] = clip_start(L2[k])

    # ============================================================ carry sanity
    citi_carry = {}
    cmap2 = {'BRL': 5, 'KRW': 18, 'MXN': 9, 'CNH': 10, 'INR': 12, 'TWD': 17}
    for ccy, ci in cmap2.items():
        citi_carry[ccy] = to_friday({ccy: col_series(cv, 1, ci, 7).dropna()}, WEEKLY_FFILL)[ccy]
    sanity = []
    for ccy in cmap2:
        a = L1['carry_1m_ann'][ccy] if ccy in L1['carry_1m_ann'].columns else pd.Series(dtype=float)
        b = citi_carry[ccy]
        j = pd.concat([a.rename('recon'), b.rename('citi')], axis=1).dropna()
        corr = j['recon'].corr(j['citi']) if len(j) > 20 else float('nan')
        sanity.append((ccy, len(j), round(corr, 3)))

    return wb, L1, L2, notes, ca_m, sanity


# ================================================================ completeness
# Per-currency completeness (disciplined heterogeneity): a currency is only
# required to have the inputs for the factors that are ON for it. CORE inputs are
# required for every traded currency; OPTIONAL inputs are required only for the
# currencies where that factor is eligible (else reported 'n/a', not blocking).
CORE_INPUTS = [
    ('spot',    'L1', 'spot_usd_traded'),
    ('carry',   'L1', 'carry_1m_ann'),
    ('vol_atm', 'L1', 'vol_implied_atm_1m'),
    ('rr25',    'L1', 'rr25_1m'),
    ('fwd_pts', 'L1', 'fwd_pts_1m'),
    ('ca_yoy',  'L2', 'ca_yoy_usdbn'),
    ('esi',     'L1', 'esi'),
    ('ctot',    'L1', 'ctot'),
]
# (label, layer, table, {currencies where this factor is eligible / required})
OPTIONAL_INPUTS = [
    ('cds',    'L1', 'cds_5y',  {'INR', 'KRW', 'MYR', 'IDR', 'PHP', 'BRL', 'PLN'}),
    ('equity', 'L1', 'equity',  {'KRW', 'TWD', 'INR'}),          # equity-flow: N.Asia only
]


def completeness_report(L1, L2, asof=None, stale_days=70):
    """Per-currency PRESENT/MISS/n-a for the latest Friday.

    An input is PRESENT if its most recent observation on/before `asof` is
    non-missing and no older than `stale_days` (monthly series like ca_yoy lag a
    few weeks, so they are not falsely flagged). A currency is READY when all its
    CORE inputs plus every OPTIONAL input it is *eligible* for are present.
    """
    src = {'L1': L1, 'L2': L2}
    if asof is None:
        asof = L1['spot_usd_traded'].index.max()
    asof = pd.Timestamp(asof)

    def present(layer, tbl, ccy):
        df = src[layer].get(tbl)
        if df is None or ccy not in df.columns:
            return False
        s = df[ccy]
        s = s[s.index <= asof].dropna()
        return bool(len(s) and (asof - s.index[-1]).days <= stale_days)

    rows = []
    for ccy in TRADED:
        rec = {'currency': ccy}
        missing = []
        for label, layer, tbl in CORE_INPUTS:
            ok = present(layer, tbl, ccy)
            rec[label] = 'OK' if ok else 'MISS'
            if not ok:
                missing.append(label)
        for label, layer, tbl, eligible in OPTIONAL_INPUTS:
            if ccy not in eligible:
                rec[label] = 'n/a'                       # factor off for this ccy
                continue
            ok = present(layer, tbl, ccy)
            rec[label] = 'OK' if ok else 'MISS'
            if not ok:
                missing.append(label)
        rec['READY'] = 'YES' if not missing else 'NO (' + ','.join(missing) + ')'
        rows.append(rec)
    rep = pd.DataFrame(rows).set_index('currency')
    return asof, rep


# ================================================================ pretty excel
# copied from data_pipeline.write_pretty_excel and EXTENDED with the new tables.
EXCEL_SPEC = [
    ('Spot Rates 30 Currencies', 'L1', 'spot_usd_all30',
     'Weekly spot, foreign currency per 1 USD (rise = USD stronger), incl. funding legs. Global PCA input.', '0.0000'),
    ('Spot Rates Asia 9', 'L1', 'spot_usd_asia9',
     'Weekly spot for the nine Asian target currencies, foreign currency per 1 USD.', '0.0000'),
    ('Spot Rates Traded 14', 'L1', 'spot_usd_traded',
     'Weekly spot for the 14 traded currencies (ASIA9 + EM5), foreign currency per 1 USD.', '0.0000'),
    ('Forward Points 1 Week', 'L1', 'fwd_pts_1w',
     'One-week forward points in pips. Bloomberg primary, Citi fall-back.', '0.0000'),
    ('Forward Points 1 Month', 'L1', 'fwd_pts_1m',
     'One-month forward points in pips. Bloomberg primary, Citi fall-back.', '0.0000'),
    ('Implied Carry 1 Month', 'L1', 'carry_1m_ann',
     'CIP-consistent carry vs USD = 1M implied yield minus SOFR 1M, annualized percent.', '0.00'),
    ('Realized Volatility 1 Month', 'L1', 'vol_realized_1m',
     'One-month realized volatility, annualized percent.', '0.00'),
    ('Implied Volatility 1 Month', 'L1', 'vol_implied_atm_1m',
     'One-month ATM implied volatility, annualized percent (Bloomberg supplement for MYR/THB/IDR/TWD).', '0.00'),
    ('Risk Reversal 25 Delta 1 Month', 'L1', 'rr25_1m',
     'One-month 25-delta risk reversal (CLP is 10-delta). Bloomberg supplement for MYR/THB/IDR/TWD.', '0.000'),
    ('Positioning Real Money', 'L1', 'pi_realmoney', 'Citi positioning indicator, real money.', '0.00'),
    ('Positioning Leveraged', 'L1', 'pi_leveraged', 'Citi positioning indicator, leveraged accounts.', '0.00'),
    ('Positioning Flow Z Score', 'L1', 'pi_rm_flow_z', 'Citi real-money flow z-score (CFTC).', '0.00'),
    ('Economic Surprise Index', 'L1', 'esi', 'Citi Economic Surprise Index by economy.', '0.0'),
    ('Inflation Surprise Index', 'L1', 'isi', 'Citi Inflation Surprise Index by economy.', '0.0'),
    ('Sovereign CDS 5 Year', 'L1', 'cds_5y',
     'Five-year sovereign CDS par spread, bp. India proxied by State Bank of India.', '0.0'),
    ('Citi Terms of Trade', 'L1', 'ctot', 'Citi commodity terms-of-trade index by currency.', '0.00'),
    ('Equity Indices', 'L1', 'equity', 'Local benchmark equity index level, mapped to its currency.', '#,##0.00'),
    ('Current Account USD Billion', 'L1', 'current_account_usdbn',
     'Current account balance, USD bn, monthly, forward-filled onto Fridays.', '0.0'),
    ('Government Yield 2 Year', 'L1', 'govt_yield_2y', 'Two-year government bond yield, percent.', '0.000'),
    ('Government Yield 5 Year', 'L1', 'govt_yield_5y', 'Five-year government bond yield, percent.', '0.000'),
    ('Government Yield 10 Year', 'L1', 'govt_yield_10y', 'Ten-year government bond yield, percent.', '0.000'),
    ('Commodities', 'L1', 'commodities', 'Weekly commodity prices and semiconductor equity indices.', '#,##0.00'),
    ('Country Factors', 'L1', 'country_factors', 'Auxiliary country factors (SGD NEER, CNY fix/basket, tourism, KOSPI flows).', '0.00'),
    ('Regime Indicators', 'L1', 'regime',
     'DXY, MSCI World, VIX, MOVE, EM/G7 FX vol, US HY OAS, OVX, DB G10 carry, US ISM PMI.', '0.00'),
    ('Real Effective Exchange Rate', 'L1', 'reer', 'Real effective exchange rate index by currency.', '0.00'),
    ('Implied Yield 1 Month', 'L1', 'fx_implied_yield_1m', 'NDF/forward-implied yield, 1M, annualized percent.', '0.00'),
    ('Implied Yield 12 Month', 'L1', 'fx_implied_yield_12m', 'NDF/forward-implied yield, 12M, annualized percent.', '0.00'),
    # ---- L2 features ----
    ('Log Spot Asia 9', 'L2', 'log_spot', 'Natural log of spot.', '0.0000'),
    ('Weekly Return', 'L2', 'ret_1w', 'One-week log return of spot.', '0.0000'),
    ('Momentum 12 Weeks', 'L2', 'mom_12w_ex1w', 'Twelve-week log return excluding the most recent week.', '0.0000'),
    ('Realized Skewness 26 Weeks', 'L2', 'skew_26w', 'Skewness of weekly returns, rolling 26 weeks.', '0.000'),
    ('Carry to Volatility Ratio', 'L2', 'carry_to_realvol',
     'Carry / realized vol, with realized vol floored at 1.0% to avoid blow-ups.', '0.000'),
    ('Risk Reversal Z Score', 'L2', 'rr25_1m_z_52w', 'Risk reversal standardized over rolling 52 weeks.', '0.00'),
    ('Surprise Index Change 4 Weeks', 'L2', 'esi_chg_4w', 'Four-week change in the economic surprise index.', '0.0'),
    ('Terms of Trade Change 13 Wks', 'L2', 'ctot_chg_13w', 'Thirteen-week change in the terms-of-trade index.', '0.00'),
    ('CDS Change 4 Weeks', 'L2', 'cds_chg_4w', 'Four-week change in the 5Y sovereign CDS spread, bp.', '0.0'),
    ('Equity Momentum 12 Weeks', 'L2', 'equity_mom_12w', 'Twelve-week log return of the local equity index.', '0.0000'),
    ('Current Account Yearly Change', 'L2', 'ca_yoy_usdbn', 'Year-over-year change in the current account, USD bn.', '0.0'),
    ('Forward Points Change 4 Weeks', 'L2', 'fwdpts_chg_4w', 'Four-week change in 1M forward points (funding dynamics).', '0.000'),
    ('Positioning Lev minus RM', 'L2', 'pi_lv_minus_rm', 'Leveraged flow z minus real-money flow z (divergence).', '0.00'),
    ('CNH PBOC CCF', 'L2', 'cny_ccf', 'CNY fix minus BBG fixing survey = PBOC fixing bias vs market expectation; regime-dependent (from 2018-06).', '0.0000'),
    ('Yield Curve Slope', 'L2', 'curve_slope_10y2y', 'Ten-year minus two-year government yield.', '0.000'),
    ('Slope Differential vs US', 'L2', 'slope_diff_vs_us', 'Local 10Y-2Y slope minus US slope.', '0.000'),
    ('Yield Differential 2Y vs US', 'L2', 'ydiff_2y_vs_us', 'Local 2Y yield minus US 2Y yield.', '0.000'),
    ('Implied Yield Slope 12M-1M', 'L2', 'implied_yield_slope_12m_1m', '12M minus 1M forward-implied yield.', '0.00'),
]

EXCEL_GROUPS = [
    ('MARKET DATA', '203864', '8EAADB', [
        'Spot Rates 30 Currencies', 'Spot Rates Asia 9', 'Spot Rates Traded 14',
        'Forward Points 1 Week', 'Forward Points 1 Month',
        'Implied Volatility 1 Month', 'Realized Volatility 1 Month', 'Regime Indicators']),
    ('TECHNICAL', 'C55A11', 'F4B183', [
        'Log Spot Asia 9', 'Weekly Return', 'Momentum 12 Weeks',
        'Realized Skewness 26 Weeks', 'Equity Momentum 12 Weeks']),
    ('FUNDAMENTAL', '2E75B6', '9DC3E6', [
        'Implied Carry 1 Month', 'Carry to Volatility Ratio',
        'Implied Yield 1 Month', 'Implied Yield 12 Month', 'Implied Yield Slope 12M-1M',
        'Forward Points Change 4 Weeks',
        'Government Yield 2 Year', 'Government Yield 5 Year', 'Government Yield 10 Year',
        'Yield Curve Slope', 'Slope Differential vs US', 'Yield Differential 2Y vs US',
        'Sovereign CDS 5 Year', 'CDS Change 4 Weeks',
        'Citi Terms of Trade', 'Terms of Trade Change 13 Wks',
        'Commodities', 'Country Factors',
        'Current Account USD Billion', 'Current Account Yearly Change',
        'Real Effective Exchange Rate', 'Equity Indices',
        'Economic Surprise Index', 'Inflation Surprise Index', 'Surprise Index Change 4 Weeks']),
    ('SENTIMENT', '538135', 'A9D18E', [
        'Risk Reversal 25 Delta 1 Month', 'Risk Reversal Z Score',
        'Positioning Real Money', 'Positioning Leveraged',
        'Positioning Flow Z Score', 'Positioning Lev minus RM', 'CNH PBOC CCF']),
]


def write_pretty_excel(path, L1, L2, cov, notes):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    TITLE_F = Font(name='Arial', size=11, bold=True, color='1F3864')
    HDR_F = Font(name='Arial', size=10, bold=True, color='FFFFFF')
    HDR_FILL = PatternFill('solid', fgColor='1F3864')
    DATA_F = Font(name='Arial', size=10)
    THIN = Border(bottom=Side(style='thin', color='D9D9D9'))
    META_TAB = '808080'

    wb = Workbook()
    ws = wb.active
    ws.title = 'Contents'
    ws.sheet_properties.tabColor = META_TAB
    ws['A1'] = 'FX Asia Weekly Model - Cleaned Database (v2)'
    ws['A1'].font = Font(name='Arial', size=14, bold=True, color='1F3864')
    conventions = [
        'All tables: rows are Friday dates (from 2013-01-01), columns are currencies or series.',
        'Spot convention: foreign currency per 1 USD (rise = stronger USD); EUR/GBP/AUD/NZD inverted.',
        'Alignment: each observation placed on the first Friday on/after its date stamp - no look-ahead.',
        f'Forward-fill limits: weekly sources up to {WEEKLY_FFILL} weeks, monthly up to {MONTHLY_FFILL} weeks.',
        'Blank cells mean the data does not exist for that date - nothing is interpolated.',
        'Generated by data_pipeline_v2.py - do not edit by hand.'] + notes
    r = 3
    for c_ in conventions:
        ws.cell(row=r, column=1, value=c_).font = DATA_F
        r += 1
    r += 1
    ws.cell(row=r, column=1, value='Sheet').font = HDR_F
    ws.cell(row=r, column=1).fill = HDR_FILL
    ws.cell(row=r, column=2, value='Description').font = HDR_F
    ws.cell(row=r, column=2).fill = HDR_FILL
    r += 1
    spec_by_title = {t: (layer, key, desc, fmt) for t, layer, key, desc, fmt in EXCEL_SPEC}
    for gname, divider_color, _tc, titles in EXCEL_GROUPS:
        cell = ws.cell(row=r, column=1, value=gname)
        cell.font = Font(name='Arial', size=11, bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor=divider_color)
        r += 1
        for title in titles:
            if title not in spec_by_title:
                continue
            layer, key, desc, _ = spec_by_title[title]
            if key in (L1 if layer == 'L1' else L2):
                ws.cell(row=r, column=1, value='    ' + title).font = Font(name='Arial', size=10, bold=True)
                ws.cell(row=r, column=2, value=desc).font = DATA_F
                r += 1
    ws.column_dimensions['A'].width = 34
    ws.column_dimensions['B'].width = 115

    for gname, divider_color, tab_color, titles in EXCEL_GROUPS:
        gtitles = [t for t in titles if t in spec_by_title and
                   spec_by_title[t][1] in (L1 if spec_by_title[t][0] == 'L1' else L2)]
        if not gtitles:
            continue
        dv = wb.create_sheet(gname)
        dv.sheet_properties.tabColor = divider_color
        dv['A1'] = gname
        dv['A1'].font = Font(name='Arial', size=22, bold=True, color=divider_color)
        dv['A2'] = 'Sheets in this section: ' + ', '.join(gtitles)
        dv['A2'].font = Font(name='Arial', size=10, italic=True, color='808080')
        dv.column_dimensions['A'].width = 120
        for title in gtitles:
            layer, key, desc, numfmt = spec_by_title[title]
            src = L1 if layer == 'L1' else L2
            df = src[key].dropna(how='all')
            ws = wb.create_sheet(title[:31])
            ws.sheet_properties.tabColor = tab_color
            ws['A1'] = f'{title} - {desc}'
            ws['A1'].font = TITLE_F
            ws.cell(row=2, column=1, value='Date').font = HDR_F
            ws.cell(row=2, column=1).fill = HDR_FILL
            for j, c in enumerate(df.columns, start=2):
                cell = ws.cell(row=2, column=j, value=str(c))
                cell.font, cell.fill = HDR_F, HDR_FILL
                cell.alignment = Alignment(horizontal='center')
                ws.column_dimensions[get_column_letter(j)].width = 12
            ws.column_dimensions['A'].width = 12
            for i, (dt, row) in enumerate(df.iterrows(), start=3):
                dcell = ws.cell(row=i, column=1, value=dt)
                dcell.number_format, dcell.font, dcell.border = 'yyyy-mm-dd', DATA_F, THIN
                for j, v in enumerate(row.values, start=2):
                    if pd.notna(v):
                        cell = ws.cell(row=i, column=j, value=float(v))
                        cell.number_format, cell.font, cell.border = numfmt, DATA_F, THIN
            ws.freeze_panes = 'B3'

    ws = wb.create_sheet('Data Coverage')
    ws.sheet_properties.tabColor = META_TAB
    ws['A1'] = 'Data Coverage - first/last observation and share of missing weeks per series'
    ws['A1'].font = TITLE_F
    for j, c in enumerate(cov.columns, start=1):
        cell = ws.cell(row=2, column=j, value=c)
        cell.font, cell.fill = HDR_F, HDR_FILL
        ws.column_dimensions[get_column_letter(j)].width = 22
    for i, (_, row) in enumerate(cov.iterrows(), start=3):
        for j, v in enumerate(row.values, start=1):
            cell = ws.cell(row=i, column=j, value=v)
            cell.font, cell.border = DATA_F, THIN
            if isinstance(v, (pd.Timestamp, datetime.datetime)):
                cell.number_format = 'yyyy-mm-dd'
    ws.freeze_panes = 'A3'
    wb.save(path)


# ================================================================ outputs
def write_outputs(outdir, L1, L2, notes):
    os.makedirs(outdir, exist_ok=True)
    csvdir = os.path.join(outdir, 'clean_csv')
    os.makedirs(csvdir, exist_ok=True)
    coverage = []
    for layer, tables in [('L1', L1), ('L2', L2)]:
        for name, df in tables.items():
            df.index.name = 'date'
            df.to_csv(os.path.join(csvdir, f'{layer}_{name}.csv'))
            for c in df.columns:
                s = df[c].dropna()
                if len(df):
                    coverage.append([f'{layer}_{name}', str(c),
                                     s.index.min() if len(s) else None,
                                     s.index.max() if len(s) else None,
                                     len(s), round(100 * (1 - len(s) / len(df)), 1)])
    cov = pd.DataFrame(coverage, columns=['Table', 'Series', 'First Observation',
                                          'Last Observation', 'Observations', 'Percent Missing'])
    xlsx_out = os.path.join(outdir, 'FX_Model_Clean.xlsx')
    write_pretty_excel(xlsx_out, L1, L2, cov, notes)
    return xlsx_out, csvdir, cov


def main():
    src = sys.argv[1]
    outdir = sys.argv[2] if len(sys.argv) > 2 else 'data/clean'
    wb, L1, L2, notes, ca_m, sanity = build(src, outdir)
    xlsx_out, csvdir, cov = write_outputs(outdir, L1, L2, notes)

    print(f'\nwritten workbook : {xlsx_out}')
    print(f'written csv dir  : {csvdir}\n')

    # (a) all output tables with shape + date range
    print('=' * 78)
    print('(a) OUTPUT TABLES')
    print('=' * 78)
    for layer, tables in [('L1', L1), ('L2', L2)]:
        for name, df in tables.items():
            lo = df.index.min().date() if len(df) else None
            hi = df.index.max().date() if len(df) else None
            print(f'  {layer}_{name:<24} shape={str(df.shape):>10}  {lo} .. {hi}  ({df.shape[1]} cols)')

    # (b) previously-problematic series coverage
    print('\n' + '=' * 78)
    print('(b) PREVIOUSLY-PROBLEMATIC SERIES  (gap% over 2013+ Friday grid)')
    print('=' * 78)
    def gappct(df, ccy):
        if ccy not in df.columns:
            return 'no col'
        s = df[ccy]
        return f'{round(100 * s.isna().mean(), 1)}%'
    def firstd(df, ccy):
        if ccy not in df.columns:
            return '-'
        s = df[ccy].dropna()
        return s.index.min().date() if len(s) else '-'
    print('  Vol/RR (MYR/THB/IDR/TWD) - now Bloomberg-sourced:')
    for ccy in ['MYR', 'THB', 'IDR', 'TWD']:
        print(f'    {ccy}: rr25 gap={gappct(L1["rr25_1m"], ccy):>6} from {firstd(L1["rr25_1m"], ccy)} | '
              f'atmvol gap={gappct(L1["vol_implied_atm_1m"], ccy):>6} from {firstd(L1["vol_implied_atm_1m"], ccy)}')
    print('  Carry (reconstructed implied-yield minus SOFR) - 2016-2020 gap check:')
    for ccy in ['CNH', 'INR', 'TWD', 'MYR']:
        c = L1['carry_1m_ann']
        sub = c[ccy][(c.index >= '2016-01-01') & (c.index <= '2020-12-31')] if ccy in c.columns else pd.Series(dtype=float)
        print(f'    {ccy}: full gap={gappct(c, ccy):>6} from {firstd(c, ccy)} | 2016-2020 gap={round(100*sub.isna().mean(),1)}%')
    print('  Carry reconstruction sanity (corr vs Citi FX.CARRY where overlapping):')
    for ccy, n, corr in sanity:
        print(f'    {ccy}: overlap={n:<4} corr={corr}')

    # (c) completeness report for latest Friday
    print('\n' + '=' * 78)
    asof, rep = completeness_report(L1, L2)
    print(f'(c) COMPLETENESS REPORT for latest Friday {asof.date()}')
    print('=' * 78)
    print(rep.to_string())
    ready = [c for c in rep.index if rep.loc[c, 'READY'] == 'YES']
    print(f'\n  Ready to predict ({len(ready)}/{len(rep)}): {", ".join(ready) if ready else "NONE"}')
    notready = [c for c in rep.index if rep.loc[c, 'READY'] != 'YES']
    if notready:
        print('  NOT ready:')
        for c in notready:
            print(f'    {c}: {rep.loc[c, "READY"]}')

    print('\n' + '=' * 78)
    print('COVERAGE TABLE (per series)')
    print('=' * 78)
    print(cov.to_string(index=False, max_rows=400))


if __name__ == '__main__':
    main()
