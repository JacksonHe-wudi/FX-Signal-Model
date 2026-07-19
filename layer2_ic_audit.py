"""
Layer 2 - IC audit: does each candidate signal predict NEXT-WEEK returns?

Convention: every signal is expressed as a LOCAL-CURRENCY score
(higher = local currency expected to appreciate vs USD next week).
Next-week local-currency return = -dlog(USD/XXX) from Friday to Friday.

For each signal we report the weekly cross-sectional Spearman IC
(signal rank vs next-week return rank across the available currencies),
its Newey-West style t-stat, hit rate, and the IC split by three regimes
(USD trend, risk on/off, US PMI) - the raw material for the
regime-adaptive pillar weights.

Output: analysis/ic_audit.csv, printed summary.
"""
import os
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

DATA = 'data/clean/clean_csv'
AN = 'analysis'
ASIA9 = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD']
START = '2013-01-01'
MIN_CCY = 5


def load(name, folder=DATA):
    return pd.read_csv(f'{folder}/{name}.csv', index_col=0, parse_dates=True)


def zsec(df):
    """cross-sectional z-score by row"""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def main():
    spot = load('L1_spot_usd_asia9')
    ret_local_next = -(np.log(spot).diff().shift(-1))       # next week, local ccy

    ctot_map = {'CNH': 'CNY'}
    esi = load('L1_esi').rename(columns={'CNY': 'CNH'})
    ca = load('L2_ca_yoy_usdbn')
    reer = load('L1_reer')
    eps = load('eps_recursive', AN)
    tech = load('tech_bollinger_signal', AN)

    # ---------- signals as local-currency scores ----------
    signals = {}
    # Fundamental pillar
    signals[('Fundamental', 'carry_to_realvol')] = load('L2_carry_to_realvol')
    signals[('Fundamental', 'implied_slope_12m_1m')] = load('L2_implied_yield_slope_12m_1m')
    signals[('Fundamental', 'esi_chg_4w')] = esi.diff(4)
    signals[('Fundamental', 'cds_chg_4w_neg')] = -load('L1_cds_5y').diff(4).rename(
        columns={'INR_SBI': 'INR'})
    signals[('Fundamental', 'ca_yoy')] = ca
    signals[('Fundamental', 'value_eps')] = eps            # eps>0: local cheap -> appreciate
    signals[('Fundamental', 'reer_gap_neg')] = -(np.log(reer) -
                                                 np.log(reer).rolling(260).mean())

    def fix_idr(df):
        """government-bond tables carry IDR twice (GT and BVAL) - coalesce"""
        df = df.copy()
        if 'IDR_GT' in df.columns:
            df['IDR'] = df['IDR_GT'].combine_first(df.get('IDR_BV'))
            df = df.drop(columns=[c for c in ['IDR_GT', 'IDR_BV'] if c in df.columns])
        return df

    # longer-maturity government-bond versions (user request)
    signals[('Fundamental', 'govt_slope_diff_10y2y')] = fix_idr(load('L2_slope_diff_vs_us'))
    signals[('Fundamental', 'ydiff_2y_vs_us')] = fix_idr(load('L2_ydiff_2y_vs_us'))
    y10 = fix_idr(load('L1_govt_yield_10y'))
    signals[('Fundamental', 'ydiff_10y_vs_us')] = y10.drop(columns=['US']).sub(y10['US'], axis=0)
    signals[('Fundamental', 'ctot_chg_13w')] = load('L2_ctot_chg_13w').rename(
        columns={'CNY': 'CNH'})

    # signature-commodity basket: signed 13-week log change of each currency's
    # key export/import commodities (exporter +, importer -)
    com = np.log(load('L1_commodities')).diff(13)
    krsemi = np.log(load('L1_kr_semi_export_px')).diff(13)
    basket = {
        'IDR': [('Coal', 1), ('Palm_Oil', 1)],
        'MYR': [('Palm_Oil', 1), ('Brent_Oil', 1)],
        'INR': [('Brent_Oil', -1), ('Gold', -1)],
        'THB': [('Gold', 1), ('Brent_Oil', -1), ('Rice', 1)],
        'KRW': [('PHLX_Semiconductor_Sector_Index', 1), ('Brent_Oil', -1)],
        'TWD': [('PHLX_Semiconductor_Sector_Index', 1), ('Brent_Oil', -1)],
        'CNH': [('Copper', 1)],
        'PHP': [('Rice', -1), ('Brent_Oil', -1)],
    }
    cs = {}
    for ccy, legs in basket.items():
        parts = [sign * com[col] for col, sign in legs if col in com.columns]
        if ccy == 'KRW' and 'KR_SEMI_EXPORT_PX' in krsemi.columns:
            parts.append(krsemi['KR_SEMI_EXPORT_PX'])
        cs[ccy] = pd.concat(parts, axis=1).mean(axis=1)
    signals[('Fundamental', 'commodity_signature')] = pd.DataFrame(cs)

    # curve curvature (2s5s10s butterfly) versus US
    y2f, y5f, y10f = (fix_idr(load(f'L1_govt_yield_{t}')) for t in ['2y', '5y', '10y'])
    fly = 2 * y5f - y2f - y10f
    signals[('Fundamental', 'curvature_diff_vs_us')] = fly.drop(columns=['US']).sub(fly['US'], axis=0)

    # Momentum pillar
    signals[('Momentum', 'spot_mom_12w')] = -load('L2_mom_12w_ex1w')   # USDXXX down -> local up
    signals[('Momentum', 'equity_mom_12w')] = load('L2_equity_mom_12w')
    # Technical pillar
    signals[('Technical', 'bollinger_walkfwd')] = -tech                # -pos(USDXXX)
    signals[('Technical', 'skew_26w')] = load('L2_skew_26w')           # +skew(USDXXX): local just crashed -> rebound
    # Momentum pillar also carries positioning/flows (user spec): real-money
    # flows chase and extend trends - sign to be confirmed by the IC itself
    signals[('Momentum', 'pi_position_rm')] = load('L1_pi_realmoney')
    signals[('Momentum', 'pi_flow_z')] = load('L1_pi_rm_flow_z')
    # Sentiment (audited as info check; used later as regime/state variables)
    signals[('Sentiment', 'rr_z_neg')] = -load('L2_rr25_1m_z_52w')     # high USD-call skew -> local weak

    # ---------- regime states ----------
    reg = load('L1_regime')
    dxy_up = reg['DXY'] > reg['DXY'].rolling(52).mean()
    risk_off = reg['MXWO'] < reg['MXWO'].rolling(52).mean()
    pmi_dn = reg['US_ISM_PMI'] < 50

    # ---------- IC computation ----------
    rows = []
    for (pillar, name), sig in signals.items():
        sig = sig.reindex(columns=[c for c in ASIA9 if c in sig.columns])
        common = sig.index.intersection(ret_local_next.index)
        common = common[common >= START]
        ics, dates = [], []
        for t in common:
            s = sig.loc[t].dropna()
            r = ret_local_next.loc[t, s.index].dropna()
            s = s.loc[r.index]
            if len(s) < MIN_CCY or s.nunique() < 3:
                continue
            ic = spearmanr(s, r)[0]
            if pd.notna(ic):
                ics.append(ic)
                dates.append(t)
        ics = pd.Series(ics, index=pd.DatetimeIndex(dates))
        if len(ics) < 100:
            continue
        # autocorrelation-robust t (Newey-West with 4 lags on the IC series)
        m, n = ics.mean(), len(ics)
        acf = [ics.autocorr(l) for l in range(1, 5)]
        var_adj = ics.var() * (1 + 2 * sum((1 - l / 5) * a for l, a in
                                           enumerate(acf, 1) if pd.notna(a)))
        t_nw = m / np.sqrt(var_adj / n)
        seg = lambda mask: round(ics.reindex(ics.index[
            mask.reindex(ics.index).fillna(False)]).mean(), 4)
        rows.append([pillar, name, n, round(m, 4), round(t_nw, 2),
                     round((ics > 0).mean(), 2),
                     seg(dxy_up), seg(~dxy_up), seg(risk_off), seg(~risk_off),
                     seg(pmi_dn), seg(~pmi_dn)])

    out = pd.DataFrame(rows, columns=[
        'pillar', 'signal', 'n_weeks', 'mean_IC', 't_NW', 'hit_rate',
        'IC_usd_up', 'IC_usd_dn', 'IC_riskoff', 'IC_riskon',
        'IC_pmi_dn', 'IC_pmi_up'])
    os.makedirs(AN, exist_ok=True)
    out.to_csv(f'{AN}/ic_audit.csv', index=False)
    print(out.to_string(index=False))


if __name__ == '__main__':
    main()
