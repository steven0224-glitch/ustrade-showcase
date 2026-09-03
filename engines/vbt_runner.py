"""vectorbt 엔진 — 벡터화. 다종목·파라미터 스윕 초고속. 신호 연구·최적화용.

vectorbt 미설치/미지원 환경이면 import 시점에 ImportError → backtest.py 가 안내.
"""
import os
import vectorbt as vbt

from . import metrics


def run(df, signals, cash=10000.0, fee=0.0005, label="strategy", outdir="results", plot=True):
    close = df["Close"]
    entries = signals["entry"].astype(bool)
    exits = signals["exit"].astype(bool)

    pf = vbt.Portfolio.from_signals(
        close, entries, exits, init_cash=cash, fees=fee, freq="1D"
    )

    equity = pf.value()
    strat_ret = equity.pct_change().fillna(0.0)
    try:
        n_trades = int(pf.trades.count())
    except Exception:
        n_trades = int(entries.sum())

    m = metrics.compute(equity, strat_ret, n_trades)

    if plot:
        os.makedirs(outdir, exist_ok=True)
        from ._plot import plt
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(equity.index, equity.values, label=f"{label} (vectorbt)", lw=1.5)
        ax.set_title(f"{label} — vectorbt engine")
        ax.set_ylabel("Equity")
        ax.legend()
        ax.grid(alpha=0.3)
        path = os.path.join(outdir, f"{label}_vectorbt.png")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        m["plot"] = path

    return m
