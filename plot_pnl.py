"""
P&L time-series chart for the A+B+D stack.

    python3 plot_pnl.py          ->  analysis/pnl_timeseries.png

Panels
  1  cumulative P&L, stack vs each sleeve (all at 5% target vol, so the lines
     are comparable; the stack line is the actual 50/25/25 risk-weighted book)
  2  drawdown of the stack
  3  rolling 52-week Sharpe of the stack
  4  calendar-year returns of the stack and each sleeve

Run AFTER portfolio_stack.py (reads analysis/*.csv).
"""
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from portfolio_stack import sleeves, scale, WEIGHTS

COL = {'A': '#2e6fb7', 'B': '#d4890b', 'D': '#4c9a52', 'STACK': '#111111'}


def main():
    raw = sleeves()
    df = pd.DataFrame({k: scale(v) for k, v in raw.items()})
    stack = sum(WEIGHTS[k] * df[k].fillna(0) for k in WEIGHTS)[df['A'].notna()]
    df = df.loc[stack.index]

    fig = plt.figure(figsize=(13, 13))
    gs = fig.add_gridspec(4, 1, height_ratios=[3, 1.1, 1.1, 1.3], hspace=0.32)

    # ---------------- 1. cumulative P&L ----------------
    ax = fig.add_subplot(gs[0])
    for k in ['A', 'B', 'D']:
        s = df[k].dropna()
        ax.plot(s.index, 100 * s.cumsum(), lw=1.2, color=COL[k], alpha=0.75,
                label=f'sleeve {k}  (5% vol, Sharpe '
                      f'{s.mean()/s.std()*np.sqrt(52):+.2f})')
    sh = stack.mean() / stack.std() * np.sqrt(52)
    ax.plot(stack.index, 100 * stack.cumsum(), lw=2.4, color=COL['STACK'],
            label=f'STACK 50/25/25  (Sharpe {sh:+.2f}, ann '
                  f'{52*stack.mean()*100:+.2f}%)')
    ax.axhline(0, color='#999', lw=0.7)
    ax.set_ylabel('cumulative P&L  (% of book)')
    ax.set_title('FX weekly model - cumulative P&L, 2013-2026\n'
                 'each sleeve scaled to 5% annualized vol; stack is the '
                 'risk-weighted 50/25/25 combination', fontsize=12, loc='left')
    ax.legend(loc='upper left', fontsize=9, framealpha=0.9)
    ax.grid(alpha=0.25)
    # sleeve D only starts 2018-06 (Bloomberg fixing survey history)
    d0 = raw['D'].dropna().index.min()
    ax.axvline(d0, color=COL['D'], ls=':', lw=1)
    ax.text(d0, ax.get_ylim()[0], '  D starts (fix survey)', fontsize=8,
            color=COL['D'], va='bottom')

    # ---------------- 2. drawdown ----------------
    ax2 = fig.add_subplot(gs[1], sharex=ax)
    cum = stack.cumsum()
    dd = 100 * (cum - cum.cummax())
    ax2.fill_between(dd.index, dd, 0, color='#b7333a', alpha=0.55, lw=0)
    ax2.set_ylabel('drawdown %')
    ax2.grid(alpha=0.25)
    ax2.text(0.005, 0.08, f'max {dd.min():.1f}%', transform=ax2.transAxes,
             fontsize=9, color='#b7333a')

    # ---------------- 3. rolling 52w Sharpe ----------------
    ax3 = fig.add_subplot(gs[2], sharex=ax)
    rs = stack.rolling(52).mean() / stack.rolling(52).std() * np.sqrt(52)
    ax3.plot(rs.index, rs, lw=1.3, color=COL['STACK'])
    ax3.axhline(0, color='#999', lw=0.7)
    ax3.axhline(sh, color='#2e6fb7', ls='--', lw=1,
                label=f'full-sample {sh:+.2f}')
    ax3.fill_between(rs.index, rs, 0, where=rs < 0, color='#b7333a', alpha=0.3, lw=0)
    ax3.set_ylabel('rolling 52w Sharpe')
    ax3.legend(loc='upper right', fontsize=8)
    ax3.grid(alpha=0.25)

    # ---------------- 4. calendar-year returns ----------------
    ax4 = fig.add_subplot(gs[3])
    yr = pd.DataFrame({**{k: df[k].fillna(0) for k in ['A', 'B', 'D']},
                       'STACK': stack}).resample('YE').sum() * 100
    yr.index = yr.index.year
    x = np.arange(len(yr))
    w = 0.2
    for i, k in enumerate(['A', 'B', 'D', 'STACK']):
        ax4.bar(x + (i - 1.5) * w, yr[k], w, color=COL[k], label=k,
                alpha=0.95 if k == 'STACK' else 0.75)
    ax4.axhline(0, color='#333', lw=0.8)
    ax4.set_xticks(x)
    ax4.set_xticklabels(yr.index)
    ax4.set_ylabel('calendar-year %')
    ax4.legend(fontsize=8, ncol=4)
    ax4.grid(alpha=0.25, axis='y')
    ax4.text(0.005, -0.22, '2026 is partial (to '
             f'{stack.index.max().date()}); D flat before 2018-06',
             transform=ax4.transAxes, fontsize=8, color='#666')

    out = 'analysis/pnl_timeseries.png'
    fig.savefig(out, dpi=140, bbox_inches='tight', facecolor='white')
    print(f'written: {out}')
    print(yr.round(2).to_string())


if __name__ == '__main__':
    main()
