"""RSI 평균회귀 (과매도 진입 · 과매수 청산).

조건:
  - 진입: RSI 가 buy 임계 하향 돌파 (과매도 진입)
  - 청산: RSI 가 sell 임계 상향 돌파 (과매수 회복)

주의: 규칙 기반 근사 — 그대로 실거래 금지, 백테스트·IC 검증 전제(VCPBreakout 와 동일 정책).
라이브 선택(live_select)은 포트폴리오 횡단면 방식이라 이 단일종목 타이밍 전략과 별개.
"""
import pandas as pd
from .base import Strategy
from .factors import _rsi


class RSI(Strategy):
    name = "rsi"

    def __init__(self, period: int = 14, buy: float = 30.0, sell: float = 70.0, **kw):
        super().__init__(period=period, buy=buy, sell=sell)
        self.period = period
        self.buy = buy
        self.sell = sell

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        rsi = _rsi(df[["Close"]], self.period)["Close"]
        over_sold = rsi < self.buy
        over_bought = rsi > self.sell
        out = pd.DataFrame(index=df.index)
        out["entry"] = (over_sold & ~over_sold.shift(1, fill_value=False)).fillna(False)
        out["exit"] = (over_bought & ~over_bought.shift(1, fill_value=False)).fillna(False)
        return out
