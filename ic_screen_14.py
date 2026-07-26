"""
Quick cross-sectional IC screen on the NEW 14-currency universe (2013+).

Purpose: evidence for (a) is sentiment worth keeping, (b) which factors carry
the Sharpe budget - previewing the full layer-2 rewiring. Weekly cross-
sectional Spearman IC vs next-week TOTAL return (spot + carry/52), min 8
currencies per week. Sign convention: signal as stated (flip visible in sign).
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
          'BRL', 'MXN', 'CLP', 'PLN', 'HUF']


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def xs_ic(sig, tot_next, min_ccy=8):
    sig = sig.reindex(columns=[c for c in TRADED if c in sig.columns])
    ics = []
    for t in sig.index:
        if t not in tot_next.index:
            continue
        a, b = sig.loc[t], tot_next.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            ics.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(ics).dropna()
    if len(s) < 50:
        return np.nan, np.nan, len(s)
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')
    tot_next = (-np.log(spot).diff()
                + (carry[spot.columns] / 100 / 52).shift(1)).shift(-1)

    iv, rv = load('L1_vol_implied_atm_1m'), load('L1_vol_realized_1m')
    logspot = np.log(spot)

    sigs = {
        # ---- fundamental ----
        'carry/realvol (core)':  load('L2_carry_to_realvol'),
        'real_carry_1m (new)':   load('L2_real_carry_1m'),
        'real_yield_12m (new)':  load('L2_real_yield_12m'),
        'ca_yoy':                load('L2_ca_yoy_usdbn'),
        'esi_chg_4w':            load('L2_esi_chg_4w'),
        'cds_chg_4w (neg)':      -load('L2_cds_chg_4w'),
        'ctot_chg_13w':          load('L2_ctot_chg_13w'),
        # ---- momentum ----
        'mom_4w (new)':          -(logspot.shift(1) - logspot.shift(5)),
        'mom_12w (old)':         -(logspot.shift(1) - logspot.shift(13)),
        'equity_mom_12w':        load('L2_equity_mom_12w'),
        'fwdpts_chg_4w':         load('L2_fwdpts_chg_4w'),
        # ---- sentiment ----
        'rr25_z (sent, neg)':    -load('L2_rr25_1m_z_52w'),
        'vrp = impl - real':     iv - rv,
        'pi_realmoney':          load('L1_pi_realmoney'),
        'pi_lv_minus_rm':        load('L2_pi_lv_minus_rm'),
    }
    print(f"14-ccy cross-sectional IC vs next-week TOTAL return, 2013+\n"
          f"{'signal':<26}{'IC':>9}{'t':>8}{'n_wks':>7}")
    rows = []
    for nm, s in sigs.items():
        try:
            m, t, n = xs_ic(s, tot_next)
            rows.append([nm, m, t, n])
            print(f'{nm:<26}{m:>+9.4f}{t:>+8.2f}{n:>7}')
        except Exception as e:
            print(f'{nm:<26}  ERR {e}')
    pd.DataFrame(rows, columns=['signal', 'ic', 't', 'n']) \
        .to_csv('analysis/ic_screen_14.csv', index=False)
    print('\nwritten: analysis/ic_screen_14.csv')


if __name__ == '__main__':
    main()
