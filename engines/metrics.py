"""백테스트 성과지표 — 엔진 무관 공통 계산."""
import numpy as np
import pandas as pd

TRADING_DAYS = 252


def compute(equity: pd.Series, strat_ret: pd.Series, n_trades: int) -> dict:
    eq = equity.dropna()
    if len(eq) < 2:
        return {"error": "데이터 부족"}

    total_return = eq.iloc[-1] / eq.iloc[0] - 1.0
    years = len(eq) / TRADING_DAYS
    cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1.0 if years > 0 else 0.0

    roll_max = eq.cummax()
    drawdown = eq / roll_max - 1.0
    max_dd = drawdown.min()

    r = strat_ret.dropna()
    std = r.std()
    sharpe = (r.mean() / std * np.sqrt(TRADING_DAYS)) if std > 0 else 0.0
    downside = r[r < 0].std()
    sortino = (r.mean() / downside * np.sqrt(TRADING_DAYS)) if downside and downside > 0 else 0.0

    return {
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "sortino": sortino,
        "n_trades": n_trades,
        "final_equity": eq.iloc[-1],
    }


def format_report(m: dict, label: str = "") -> str:
    if "error" in m:
        return f"[{label}] {m['error']}"
    return (
        f"[{label}]\n"
        f"  총수익률   : {m['total_return']:+.2%}\n"
        f"  CAGR       : {m['cagr']:+.2%}\n"
        f"  MDD        : {m['max_drawdown']:.2%}\n"
        f"  Sharpe     : {m['sharpe']:.2f}\n"
        f"  Sortino    : {m['sortino']:.2f}\n"
        f"  매매횟수   : {m['n_trades']}\n"
        f"  최종자산   : {m['final_equity']:,.0f}"
    )
