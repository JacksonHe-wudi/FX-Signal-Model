"""
Is every other factor's information ALREADY INSIDE carry?

A factor can fail to enter the composite for three different reasons, and they
are not interchangeable:

  (1) REDUNDANT  - it is a rewrite of carry. Orthogonalize it against
                   z(carry/realvol) cross-sectionally each week; the residual
                   has no IC left.
  (2) INDEPENDENT BUT WEAK - the residual still carries IC, but too little to
                   survive the book's discretization (top3/bottom3 + hysteresis
                   + 1/vol weights). This is a breadth/noise failure, NOT a
                   "carry already knows it" failure.
  (3) DEAD       - no IC to begin with, orthogonalized or not.

Method, per week t:
    resid[t,c] = z_f[t,c] - beta[t] * z_carry[t,c],  beta[t] = cross-sectional
                 OLS slope of z_f on z_carry among the names present that week
Then rank-IC of resid against next week's total return, same estimator used for
every other factor in the library.

Output also reports the incremental IC of the pair (carry, factor) at 2:1 versus
carry alone, which is the number that actually matters for the book.
"""
import numpy as np
import pandas as pd

from sleeve_a_rv import TRADED, load, zsec

MIS_WIN = 260


def factors():
    """All fundamental / price-derived factors, signed so higher = CCY appreciates."""
    reer = load('L1_reer').reindex(columns=TRADED)
    lr = np.log(reer)
    F = {
        'carry/realvol  [IN USE]': load('L2_carry_to_realvol'),
        'ESI 4w chg     [IN USE]': load('L2_esi_chg_4w'),
        'real_carry (carry-CPI)': load('L2_real_carry_1m'),
        'real_yield_12m': load('L2_real_yield_12m'),
        'real_yield_1m': load('L2_real_yield_1m'),
        'CPI diff vs US': load('L2_cpi_diff_vs_us'),
        '2y yield diff vs US': load('L2_ydiff_2y_vs_us'),
        'CA YoY': load('L2_ca_yoy_usdbn'),
        'CTOT 13w chg': load('L2_ctot_chg_13w'),
        'REER level (neg)': -reer,
        'REER misvaluation (neg)': -(lr - lr.rolling(MIS_WIN, min_periods=104).mean())
        / lr.rolling(MIS_WIN, min_periods=104).std(),
        'CDS 4w chg (neg)': -load('L2_cds_chg_4w'),
        'fwd pts 4w chg': load('L2_fwdpts_chg_4w'),
        'fwd basis z 52w': load('L2_fwdpts_basis_z_52w'),
        'RR25 z 52w': load('L2_rr25_1m_z_52w'),
        'implied slope 12m-1m': load('L2_implied_yield_slope_12m_1m'),
        'curve slope 10y-2y': load('L2_curve_slope_10y2y'),
        'mom 12w ex-1w': load('L2_mom_12w_ex1w'),
        'equity mom 12w': load('L2_equity_mom_12w'),
        'skew 26w': load('L2_skew_26w'),
        'positioning LV-RM': load('L2_pi_lv_minus_rm'),
    }
    return {k: v.reindex(columns=TRADED) for k, v in F.items()}


def ric(sig, tot, min_ccy=6):
    out = []
    for t in sig.index:
        if t not in tot.index:
            continue
        a, b = sig.loc[t], tot.loc[t]
        m = a.notna() & b.notna()
        if m.sum() >= min_ccy:
            out.append(a[m].rank().corr(b[m].rank()))
    s = pd.Series(out).dropna()
    if len(s) < 50:
        return np.nan, np.nan, len(s)
    return s.mean(), s.mean() / s.std() * np.sqrt(len(s)), len(s)


def orthogonalize(zf, zc):
    """Weekly cross-sectional residual of zf on zc."""
    res = pd.DataFrame(np.nan, index=zf.index, columns=zf.columns)
    for t in zf.index:
        a, b = zf.loc[t], zc.reindex(zf.columns).loc[t] if False else zc.loc[t]
        m = a.notna() & b.notna()
        if m.sum() < 4:
            continue
        x, y = b[m].values, a[m].values
        beta = np.dot(x - x.mean(), y - y.mean()) / max(np.dot(x - x.mean(), x - x.mean()), 1e-12)
        res.loc[t, m[m].index] = y - y.mean() - beta * (x - x.mean())
    return res


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    tot = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)

    F = factors()
    zc = zsec(F['carry/realvol  [IN USE]'])
    # baseline must be smoothed the SAME way the pair is, else the comparison
    # credits the pair for the 3-week smoothing rather than for the new factor
    base_ic, base_t, _ = ric(zsec(zc.rolling(3).mean()), tot)

    print('=' * 104)
    print('IS THE INFORMATION ALREADY IN CARRY?  (weekly cross-sectional '
          'orthogonalization vs z(carry/realvol))')
    print('=' * 104)
    print(f'baseline: z(carry/realvol) alone   IC {base_ic:+.4f}  t {base_t:+.2f}\n')
    print(f"{'factor':<26}{'corr w/':>9}{'raw IC':>9}{'raw t':>7}"
          f"{'resid IC':>10}{'resid t':>9}{'pair IC':>9}{'ccys':>6}  verdict")
    print(f"{'':<26}{'carry':>9}")

    rows = []
    for k, v in F.items():
        if k.startswith('carry/realvol'):
            continue
        zf = zsec(v)
        idx = zf.index.intersection(zc.index)
        zf, zc_ = zf.loc[idx], zc.loc[idx]
        corr = zc_.corrwith(zf, axis=1).mean()
        r_ic, r_t, _ = ric(zf, tot)
        res = orthogonalize(zf, zc_)
        o_ic, o_t, n = ric(res, tot)
        # pair at 2:1, renormalized by members present (same as the live composite)
        num = 2 * zc_.fillna(0) + 1 * zf.fillna(0)
        den = 2 * zc_.notna() + 1 * zf.notna()
        p_ic, _, _ = ric(zsec((num / den.replace(0, np.nan)).rolling(3).mean()), tot)
        ncc = int(v.notna().sum(axis=1).max())

        if pd.isna(r_t) or abs(r_t) < 1.5:
            verdict = 'DEAD (no IC to start with)'
        elif pd.notna(o_t) and abs(o_t) < 1.5:
            verdict = 'REDUNDANT - carry already has it'
        else:
            verdict = 'INDEPENDENT - real extra info'
        rows.append((k, verdict, o_ic, o_t, p_ic))
        print(f'{k:<26}{corr:>+9.2f}{r_ic:>+9.4f}{r_t:>+7.2f}'
              f'{o_ic:>+10.4f}{o_t:>+9.2f}{p_ic:>+9.4f}{ncc:>6}  {verdict}')

    print()
    print('=' * 104)
    print('SUMMARY')
    print('=' * 104)
    for tag in ['REDUNDANT - carry already has it', 'INDEPENDENT - real extra info',
                'DEAD (no IC to start with)']:
        hit = [r[0] for r in rows if r[1] == tag]
        print(f'{tag:<36} {len(hit):>2}: {", ".join(hit) if hit else "-"}')

    print()
    ind = [r for r in rows if r[1].startswith('INDEPENDENT')]
    if ind:
        print('For the INDEPENDENT ones, does pairing with carry beat carry alone '
              f'(IC {base_ic:+.4f})?')
        for k, _, o_ic, o_t, p_ic in sorted(ind, key=lambda r: -r[4]):
            d = p_ic - base_ic
            print(f'  {k:<26} pair IC {p_ic:+.4f}  ({d:+.4f} vs carry alone)  '
                  f'{"HELPS" if d > 0.002 else "no gain"}')


if __name__ == '__main__':
    main()
