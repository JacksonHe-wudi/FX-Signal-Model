"""
FX ALL-IN-ONE DASHBOARD
=======================
    streamlit run dashboard.py

One screen for the whole process: per-currency Fundamental / Sentiment /
Technical read with an entry verdict, a strategy backtester anyone can drive
(including the live A+B+D model, a BofA-style short-term fair value fade, an
ML walk-forward model, technicals with adjustable parameters), driver
attribution for the last month, sentiment percentiles, and auto-pulled news
per country.

Needs: streamlit, plotly, pandas, numpy, scikit-learn (ML tab), feedparser
(news tab). Data: the repo's data/clean/clean_csv/*.csv (run
data_pipeline_v2.py first) plus optional analysis/*.csv extras.

Honesty notes are printed inside each tab: every pre-tested strategy shows the
evidence we found for it, including the negative results. The dashboard shows
you the numbers - it does not pretend a dead strategy is alive.
"""
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
