"""Alpha Zoo — HKUDS/Vibe-Trading `zoo/alpha101` 선별 이식 (16종).

Kakushadze(2015) "101 Formulaic Alphas" (arXiv:1601.00991) 중 내 OHLCV+vwap
패널로 계산 가능하고 US 일봉에 그럴듯한 횡단면 팩터만 골라 옮겼다. 공식은 Vibe
zoo 의 compute() 본문을 ops 연산자로 거의 그대로 이식 — 원 논문식 부호(높을수록 매수).

⚠️ 대부분 원래 CN/범용 유니버스에서 튠된 것 → **US 에서 재검증 필수**. 실제 채택은
eval_factor 의 random-control strict 게이트(confirmed_alive)를 통과한 것만.
selection 배선은 flag-gated(use_pead 선례) — 검증 전엔 결정론 매매 불변.

계약: compute(panel: dict) -> wide DataFrame. panel 키 = open/high/low/close/volume/vwap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from strategies.ops import (
    delay,
    delta,
    rank,
    safe_div,
    scale,
    signed_power,
    ts_argmax,
    ts_corr,
    ts_cov,
    ts_min,
    ts_rank,
    ts_std,
    ts_sum,
    make_one,
    where_ternary,
)

ALPHA_REGISTRY: dict = {}
ALPHA_META: dict = {}


def _alpha(name: str, theme: list, cols: list, formula: str):
    """등록 데코레이터 — 팩터명·테마·필요컬럼·공식 메타 부착."""
    def deco(fn):
        fn.meta = {"id": name, "theme": theme, "columns_required": cols, "formula_latex": formula}
        ALPHA_REGISTRY[name] = fn
        ALPHA_META[name] = fn.meta
        return fn
    return deco


@_alpha("alpha101_001", ["reversal", "volatility"], ["close"],
        "rank(ts_argmax(SignedPower((returns<0)?stddev(returns,20):close,2),5))-0.5")
def alpha101_001(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    returns = close.pct_change()
    cond = (returns < 0).astype(float)
    x = ts_std(returns, 20) * cond + close * (1.0 - cond)
    return rank(ts_argmax(signed_power(x, 2.0), 5)) - 0.5


@_alpha("alpha101_003", ["volume", "reversal"], ["open", "volume"],
        "-1 * correlation(rank(open), rank(volume), 10)")
def alpha101_003(panel: dict) -> pd.DataFrame:
    return -1.0 * ts_corr(rank(panel["open"]), rank(panel["volume"]), 10)


@_alpha("alpha101_004", ["reversal"], ["low"],
        "-1 * Ts_Rank(rank(low), 9)")
def alpha101_004(panel: dict) -> pd.DataFrame:
    return -1.0 * ts_rank(rank(panel["low"]), 9)


@_alpha("alpha101_006", ["volume", "reversal"], ["open", "volume"],
        "-1 * correlation(open, volume, 10)")
def alpha101_006(panel: dict) -> pd.DataFrame:
    return -1.0 * ts_corr(panel["open"], panel["volume"], 10)


@_alpha("alpha101_012", ["volume", "reversal"], ["close", "volume"],
        "sign(delta(volume,1)) * (-1 * delta(close,1))")
def alpha101_012(panel: dict) -> pd.DataFrame:
    return np.sign(delta(panel["volume"], 1)) * (-1.0 * delta(panel["close"], 1))


@_alpha("alpha101_013", ["volume"], ["close", "volume"],
        "-1 * rank(covariance(rank(close), rank(volume), 5))")
def alpha101_013(panel: dict) -> pd.DataFrame:
    return -1.0 * rank(ts_cov(rank(panel["close"]), rank(panel["volume"]), 5))


@_alpha("alpha101_019", ["momentum"], ["close"],
        "(-1*sign((close-delay(close,7))+delta(close,7)))*(1+rank(1+sum(returns,250)))")
def alpha101_019(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    returns = close.pct_change()
    return (-1.0 * np.sign((close - delay(close, 7)) + delta(close, 7))) \
        * (1.0 + rank(1.0 + ts_sum(returns, 250)))


@_alpha("alpha101_024", ["momentum"], ["close"],
        "delta(mean(close,100),100)/delay(close,100)<=0.05 ? -(close-ts_min(close,100)) : -delta(close,3)")
def alpha101_024(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    m100 = ts_sum(close, 100) / 100.0
    x = safe_div(delta(m100, 100), delay(close, 100))
    cond = x <= 0.05
    left = -1.0 * (close - ts_min(close, 100))
    right = -1.0 * delta(close, 3)
    return where_ternary(cond, left, right)


@_alpha("alpha101_032", ["momentum"], ["close", "vwap"],
        "scale(sum(close,7)/7 - close) + 20*scale(correlation(vwap, delay(close,5), 230))")
def alpha101_032(panel: dict) -> pd.DataFrame:
    close, vwap = panel["close"], panel["vwap"]
    return scale(ts_sum(close, 7) / 7.0 - close) + 20.0 * scale(ts_corr(vwap, delay(close, 5), 230))


@_alpha("alpha101_033", ["reversal"], ["open", "close"],
        "rank(-1 * (1 - open/close))")
def alpha101_033(panel: dict) -> pd.DataFrame:
    return rank(-1.0 * (1.0 - safe_div(panel["open"], panel["close"])))


@_alpha("alpha101_034", ["volatility"], ["close"],
        "rank((1-rank(stddev(returns,2)/stddev(returns,5)))+(1-rank(delta(close,1))))")
def alpha101_034(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    returns = close.pct_change()
    return rank((1.0 - rank(safe_div(ts_std(returns, 2), ts_std(returns, 5)))) + (1.0 - rank(delta(close, 1))))


@_alpha("alpha101_046", ["momentum"], ["close"],
        "piecewise on ((delay20-delay10)/10 - (delay10-close)/10)")
def alpha101_046(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    x = ((delay(close, 20) - delay(close, 10)) / 10.0) - ((delay(close, 10) - close) / 10.0)
    one = make_one(close)
    return where_ternary(0.25 < x, -1.0 * one, where_ternary(x < 0.0, one, -1.0 * (close - delay(close, 1))))


@_alpha("alpha101_051", ["momentum"], ["close"],
        "(same x as 046) < -0.05 ? 1 : -1*(close-delay(close,1))")
def alpha101_051(panel: dict) -> pd.DataFrame:
    close = panel["close"]
    x = ((delay(close, 20) - delay(close, 10)) / 10.0) - ((delay(close, 10) - close) / 10.0)
    one = make_one(close)
    return where_ternary(x < -0.05, one, -1.0 * (close - delay(close, 1)))


@_alpha("alpha101_053", ["reversal"], ["high", "low", "close"],
        "-1 * delta(((close-low)-(high-close))/(close-low), 9)")
def alpha101_053(panel: dict) -> pd.DataFrame:
    close, high, low = panel["close"], panel["high"], panel["low"]
    x = safe_div(((close - low) - (high - close)), (close - low))
    return -1.0 * delta(x, 9)


@_alpha("alpha101_054", ["reversal"], ["open", "high", "low", "close"],
        "-1 * ((low-close)*(open^5)) / ((low-high)*(close^5))")
def alpha101_054(panel: dict) -> pd.DataFrame:
    close, open_, high, low = panel["close"], panel["open"], panel["high"], panel["low"]
    num = (low - close) * open_.pow(5)
    denom = (low - high) * close.pow(5)
    return -1.0 * safe_div(num, denom)


@_alpha("alpha101_101", ["reversal"], ["open", "high", "low", "close"],
        "(close - open) / ((high - low) + 0.001)")
def alpha101_101(panel: dict) -> pd.DataFrame:
    close, open_, high, low = panel["close"], panel["open"], panel["high"], panel["low"]
    return safe_div((close - open_), (high - low + 0.001))


def compute(name: str, panel: dict) -> pd.DataFrame:
    if name not in ALPHA_REGISTRY:
        raise KeyError(f"알 수 없는 zoo 팩터: {name}. 사용가능: {list(ALPHA_REGISTRY)}")
    return ALPHA_REGISTRY[name](panel)
