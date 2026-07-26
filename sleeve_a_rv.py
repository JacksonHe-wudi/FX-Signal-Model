"""
Sleeve A - concentrated cross-sectional RV book on the 14-currency universe.

Signal (prespecified from the IC screen, no fitted weights -> minimal
overfitting surface): composite = 2 x z(carry/realvol) + 1 x z(ESI chg 4w),
renormalized per currency by the members actually available, smoothed 3 weeks,
then cross-sectionally z-scored.

Construction (carried over from the validated Asia-9 design, not re-tuned):
  |z| > 0.5 conviction gate -> long top3 / short bottom3, stay while in
  top5/bottom5 (hysteresis); 1/ATM-vol weights within each leg (legs +-1 =
  self-funded, net 0); dispersion-timing multiplier (0.5-1.5x); 5% vol target
  (52w, lagged, cap 3x). Mid execution (user instruction).

Variants reported:
  BASE            all 14 currencies, all members on
  +stress gate    gross x0.5 when max cross-asset vol z > 2 (Willer Ch4.3;
                  vols: VIX, MOVE, OVX, EM FX vol, G7 FX vol)
  +eligibility    CNH & SGD carry OFF (managed; NEER regime) - matrix prior
  top4/6          wider legs for the wider universe
"""
import numpy as np
import pandas as pd

DATA = 'data/clean/clean_csv'
AN = 'analysis'
TRADED = ['CNH', 'IDR', 'INR', 'KRW', 'MYR', 'PHP', 'SGD', 'THB', 'TWD',
          'BRL', 'MXN', 'CLP', 'PLN', 'HUF']
VOL_TARGET = 0.05


def load(name):
    return pd.read_csv(f'{DATA}/{name}.csv', index_col=0, parse_dates=True)


def zsec(df):
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def perf(r, name):
    r = r.dropna()
    sh = r.mean() / r.std() * np.sqrt(52)
    cum = r.cumsum()
    dd = (cum - cum.cummax()).min()
    hit = 100 * (r[r != 0] > 0).mean()
    return (f'{name:<28} Sharpe {sh:+.2f}  ann {52*r.mean()*100:+.2f}%  '
            f'vol {np.sqrt(52)*r.std()*100:.2f}%  maxDD {dd*100:.1f}%  hit {hit:.0f}%')


def build_book(comp, atm, top_in=3, top_stay=5):
    """Conviction + hysteresis + 1/vol weights. comp: weekly z-scores."""
    dates = comp.index
    wgt = pd.DataFrame(0.0, index=dates, columns=comp.columns)
    prevL, prevS = set(), set()
    for t in dates:
        s = comp.loc[t].dropna()
        s = s[s.abs() > 0.5]
        if len(s) < 2:
            prevL, prevS = set(), set()
            continue
        rd, ra = s.rank(ascending=False), s.rank(ascending=True)
        L = set(s.index[rd <= top_in]) | {c for c in prevL if c in s.index and rd[c] <= top_stay}
        S = set(s.index[ra <= top_in]) | {c for c in prevS if c in s.index and ra[c] <= top_stay}
        L, S = L - S, S - L
        prevL, prevS = L, S
        iv = 1.0 / atm.loc[t].reindex(list(L | S)).replace(0, np.nan)
        iv = iv.fillna(iv.mean() if pd.notna(iv.mean()) else 1.0)
        for side, idx in [(1, list(L)), (-1, list(S))]:
            if idx:
                v = iv.reindex(idx)
                wgt.loc[t, idx] = side * v / v.sum()
    return wgt


def run(comp, tot_next, atm, disp_mult, stress=None, top_in=3, top_stay=5):
    wgt = build_book(comp, atm, top_in, top_stay)
    raw = (wgt * tot_next.reindex(wgt.index)).sum(axis=1) * disp_mult
    if stress is not None:
        raw = raw * stress.reindex(raw.index).fillna(1.0)
    lev = (VOL_TARGET / (raw.rolling(52).std() * np.sqrt(52))).clip(upper=3).shift(1)
    return raw * lev


def main():
    spot = load('L1_spot_usd_traded')
    carry = load('L1_carry_1m_ann')[spot.columns]
    atm = load('L1_vol_implied_atm_1m')[spot.columns]
    tot_next = (-np.log(spot).diff() + (carry / 100 / 52).shift(1)).shift(-1)

    cv = load('L2_carry_to_realvol').reindex(columns=TRADED)
    esi = load('L2_esi_chg_4w').reindex(columns=TRADED)

    def composite(cv_in):
        zc, ze = zsec(cv_in), zsec(esi)
        num = 2.0 * zc.fillna(0) + 1.0 * ze.reindex(zc.index).fillna(0)
        den = 2.0 * zc.notna() + 1.0 * ze.reindex(zc.index).notna()
        comp = (num / den.replace(0, np.nan)).rolling(3).mean()
        return zsec(comp)

    comp = composite(cv)

    # dispersion-timing multiplier
    disp = comp.std(axis=1)
    dz = (disp - disp.rolling(104, min_periods=40).mean()) / \
         disp.rolling(104, min_periods=40).std()
    mult = (dz.clip(-1, 1) * 0.5 + 1.0).fillna(1.0)

    # stress gate: max cross-asset vol z > 2 -> gross x0.5 (lagged state)
    reg = load('L1_regime')
    vols = [c for c in ['VIX', 'MOVE', 'OVX', 'EMFXVOL', 'G7FXVOL'] if c in reg.columns]
    vz = pd.concat({c: (reg[c] - reg[c].rolling(104, min_periods=40).mean())
                    / reg[c].rolling(104, min_periods=40).std() for c in vols}, axis=1)
    stress = (vz.max(axis=1) > 2.0).shift(1).fillna(False).map({True: 0.5, False: 1.0})

    # eligibility variant: CNH & SGD carry off (managed / NEER)
    cv_elig = cv.copy()
    cv_elig[['CNH', 'SGD']] = np.nan
    comp_elig = composite(cv_elig)

    print('=' * 96)
    print('SLEEVE A - concentrated RV book, 14 currencies, 2013+, mid execution')
    print('=' * 96)
    curves = {}
    for name, c_, s_, ti, ts in [
            ('BASE (top3/5)', comp, None, 3, 5),
            ('+stress gate', comp, stress, 3, 5),
            ('+eligibility (CNH,SGD)', comp_elig, None, 3, 5),
            ('+elig +stress', comp_elig, stress, 3, 5),
            ('top4/6 +elig +stress', comp_elig, stress, 4, 6)]:
        net = run(c_, tot_next, atm, mult, s_, ti, ts)
        curves[name] = net
        print(perf(net, name))

    best = '+elig +stress'
    print(f'\nsubsamples - {best}:')
    for nm, a, b in [('2013-2016', '2013', '2016'), ('2017-2019', '2017', '2019'),
                     ('2020-2022', '2020', '2022'), ('2023-2026', '2023', '2026')]:
        r = curves[best].loc[a:b].dropna()
        if len(r) > 30:
            print(f'  {nm}: Sharpe {r.mean()/r.std()*np.sqrt(52):+.2f}  '
                  f'ann {52*r.mean()*100:+.2f}%')

    pd.DataFrame(curves).to_csv(f'{AN}/sleeve_a_curves.csv')
    print(f'\nwritten: {AN}/sleeve_a_curves.csv')


if __name__ == '__main__':
    main()
