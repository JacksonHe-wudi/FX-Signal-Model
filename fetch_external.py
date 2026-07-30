"""
Download the free external EM datasets that the 2026-07-29 source scout
verified as live (see docs/external_data_sources.md), into data/external/.

    python3 fetch_external.py            # fetch everything
    python3 fetch_external.py cftc bis   # fetch selected groups

Groups
  cftc    weekly CFTC positioning: TFF leveraged funds + legacy noncommercial
          net positions for BRL, MXN, JPY, EUR, CAD (BRL from 2011, MXN 2006)
  bis     monthly REAL broad REER for all 13 (single methodology, 2020=100,
          fixes the vendor REER's inconsistent base years), daily nominal
          NEER (all 13), daily policy rates (11/13 - no SG/TW)
  fred    weekly/daily global-state series: NFCI, VIX, broad USD, EM USD
          index, 10y-2y; plus the FRED mirror of BIS REER (covers Taiwan)
  imf     monthly official FX reserves (12/13, no TW) and monthly USD
          exports (12/13, no PH) via the IMF SDMX 2.1 API
  equity  daily local equity indices via Yahoo v8: true indices for
          ID/PH/TH/SG/BR/MX/CN + USD-ETF proxies for CL (ECH), PL (EPOL)
          and the OTP single-stock proxy for HU

Every fetch is independent; failures are reported and skipped. Timestamps in
filenames are avoided so downstream code has stable paths.

Timing rules for model use (enforce in the consumer, not here):
  * CFTC positions are as-of Tuesday, released Friday ~3:30pm ET - a Friday
    signal must use the PREVIOUS week's report.
  * BIS monthly REER arrives with a 1-2 month lag - ffill limit 10 weeks.
  * Yahoo SET (Thailand) history can lag 1-2 weeks - the consumer should
    treat trailing NaNs as missing, not zero.
"""
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
    cmd = ['curl', '-sS', '--max-time', '90', '-G' if '$' in url else '-L']
    if 'yahoo' in url:                 # Yahoo rejects bare curl; FRED
        cmd += ['-H', 'User-Agent: Mozilla/5.0']   # rejects the browser UA
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


def fetch_fxspot():
    """BACKUP daily spot (Yahoo FX was audited and rejected: weekly return
    corr vs two agreeing professional sources only 0.5-0.9, TWD 0.5, no CNH
    history). FRED H.10 agrees with the Citi workbook at 0.94-0.98 weekly -
    use it to cross-check the primary data, 8 of 13 currencies, 2-3d lag."""
    ids = {'CNH': 'DEXCHUS', 'INR': 'DEXINUS', 'KRW': 'DEXKOUS',
           'SGD': 'DEXSIUS', 'THB': 'DEXTHUS', 'TWD': 'DEXTAUS',
           'BRL': 'DEXBZUS', 'MXN': 'DEXMXUS'}
    out = {}
    for c, sid in ids.items():
        try:
            txt = curl(f'https://fred.stlouisfed.org/graph/fredgraph.csv'
                       f'?id={sid}').decode()
            df = pd.read_csv(io.StringIO(txt))
            df.columns = ['date', c]
            df['date'] = pd.to_datetime(df['date'])
            out[c] = pd.to_numeric(df.set_index('date')[c], errors='coerce')
        except Exception as e:
            print(f'  skip {c}: {str(e)[:50]}')
    if out:
        save(pd.DataFrame(out).dropna(how='all'), 'fred_h10_spot_backup_d')


GROUPS = {'cftc': fetch_cftc, 'bis': fetch_bis, 'fred': fetch_fred,
          'imf': fetch_imf, 'equity': fetch_equity, 'fxspot': fetch_fxspot}


def main():
    picks = sys.argv[1:] or list(GROUPS)
    for g in picks:
        print(f'[{g}]')
        try:
            GROUPS[g]()
        except Exception as e:
            print(f'  FAILED: {str(e)[:180]}')


if __name__ == '__main__':
    main()
