"""경량 벡터 백테스터 (numba 불필요, 의존성 최소).

규칙:
  - entry 신호 & 무포지션 → 다음 봉부터 보유 (shift(1) 로 룩어헤드 방지)
  - exit 신호 & 보유 → 청산
  - 포지션 변경 시 fee(편도) 차감
buy&hold 와 함께 자산곡선 PNG 저장.
"""
import os
import numpy as np
import pandas as pd

from . import metrics
from ._plot import plt


def run(df, signals, cash=10000.0, fee=0.0005, label="strategy", outdir="results", plot=True):
    close = df["Close"]
    entry = signals["entry"].astype(bool).values
    exit_ = signals["exit"].astype(bool).values

    pos = np.zeros(len(df))
    in_pos = False
    for i in range(len(df)):
        if not in_pos and entry[i]:
            in_pos = True
        elif in_pos and exit_[i]:
            in_pos = False
        pos[i] = 1.0 if in_pos else 0.0
    pos = pd.Series(pos, index=df.index)

    ret = close.pct_change().fillna(0.0)
    strat_ret = pos.shift(1).fillna(0.0) * ret
    turns = pos.diff().abs().fillna(pos.iloc[0])
    strat_ret = strat_ret - turns * fee

    equity = cash * (1.0 + strat_ret).cumprod()
    n_trades = int((pos.diff() > 0).sum())

    m = metrics.compute(equity, strat_ret, n_trades)

    if plot:
        os.makedirs(outdir, exist_ok=True)
        bh = cash * (1.0 + ret).cumprod()
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(equity.index, equity.values, label=f"{label} (전략)", lw=1.5)
        ax.plot(bh.index, bh.values, label="Buy & Hold", lw=1.0, alpha=0.6)
        ax.set_title(f"{label} — simple engine")
        ax.set_ylabel("Equity")
        ax.legend()
        ax.grid(alpha=0.3)
        path = os.path.join(outdir, f"{label}_simple.png")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        m["plot"] = path

    return m
