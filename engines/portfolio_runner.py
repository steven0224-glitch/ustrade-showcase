"""포트폴리오 백테스터 — 다종목 비중 → 자산곡선.

모델:
  - 리밸런스일 사이엔 비중이 가격따라 표류(drift) — 일별 재계산
  - 리밸런스일 종가에 목표비중으로 조정, turnover×fee 차감 (편도)
  - 룩어헤드 방지: 당일 수익률엔 전일 종가 비중 적용
벤치마크: 유니버스 동일비중(월 리밸런스) + (옵션) SPY 매수후보유.
"""
import os
import numpy as np
import pandas as pd

from . import metrics
from ._plot import plt


def _simulate(prices, target_weights, cash, fee):
    """target_weights: DataFrame[index=리밸런스일, cols=티커]. → equity, port_ret Series."""
    rets = prices.pct_change().fillna(0.0)
    tickers = prices.columns
    rebal = {d: target_weights.loc[d].reindex(tickers).fillna(0.0).values
             for d in target_weights.index}

    w = np.zeros(len(tickers))   # 자산별 비중 (현금 = 1 - w.sum())
    eq = cash
    equity, port_rets = [], []

    for i, d in enumerate(prices.index):
        r = rets.loc[d].values
        if i == 0:
            port_r = 0.0
        else:
            port_r = float((w * r).sum())          # 현금은 0% 수익
            new_w = w * (1.0 + r)
            total = new_w.sum() + (1.0 - w.sum())  # = 1 + port_r
            w = new_w / total if total > 0 else new_w
        eq *= (1.0 + port_r)

        if d in rebal:                              # 종가 리밸런스
            tw = rebal[d]
            turnover = np.abs(tw - w).sum()
            eq *= (1.0 - turnover * fee)
            w = tw

        port_rets.append(port_r)
        equity.append(eq)

    idx = prices.index
    return pd.Series(equity, index=idx), pd.Series(port_rets, index=idx)


def run(prices, target_weights, cash=10000.0, fee=0.0005, label="portfolio",
        outdir="results", benchmark_prices=None, plot=True):
    equity, port_ret = _simulate(prices, target_weights, cash, fee)
    n_rebal = len(target_weights)
    m = metrics.compute(equity, port_ret, n_rebal)
    m["n_rebalances"] = n_rebal

    # 동일비중 벤치마크 (매 리밸런스일 1/N)
    ew_target = pd.DataFrame(
        1.0 / prices.shape[1],
        index=target_weights.index, columns=prices.columns,
    )
    ew_eq, _ = _simulate(prices, ew_target, cash, fee)

    if plot:
        os.makedirs(outdir, exist_ok=True)
        fig, ax = plt.subplots(figsize=(11, 5))
        ax.plot(equity.index, equity.values, label=f"{label} (전략)", lw=1.8)
        ax.plot(ew_eq.index, ew_eq.values, label="동일비중 벤치마크", lw=1.0, alpha=0.7)
        if benchmark_prices is not None:
            bh = cash * (benchmark_prices / benchmark_prices.iloc[0])
            ax.plot(bh.index, bh.values, label=f"{benchmark_prices.name} 매수후보유",
                    lw=1.0, alpha=0.7, ls="--")
        ax.set_title(f"{label} — portfolio engine")
        ax.set_ylabel("Equity")
        ax.legend()
        ax.grid(alpha=0.3)
        path = os.path.join(outdir, f"{label}_portfolio.png")
        fig.tight_layout()
        fig.savefig(path, dpi=110)
        plt.close(fig)
        m["plot"] = path

    m["benchmark_ew_final"] = float(ew_eq.iloc[-1])
    return m
