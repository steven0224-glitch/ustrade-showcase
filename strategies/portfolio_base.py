"""포트폴리오 전략 베이스 — 횡단면(cross-sectional) 다종목.

단일종목 Strategy(generate_signals)와 별개 인터페이스.
generate_weights(prices) → 리밸런스일별 목표비중 DataFrame.
  index = 리밸런스 거래일 (전체 거래일의 부분집합)
  columns = 티커, 값 = 목표비중 (행 합 ≤ 1, 나머지는 현금)
"""
from abc import ABC, abstractmethod
import pandas as pd


class PortfolioStrategy(ABC):
    name = "portfolio_base"

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        ...

    @staticmethod
    def rebalance_dates(prices: pd.DataFrame, freq: str = "M") -> pd.DatetimeIndex:
        """각 기간의 마지막 거래일. freq='M' 월말, 'W' 주말, 'Q' 분기말."""
        period = prices.index.to_period(freq)
        is_last = ~period.duplicated(keep="last")
        return prices.index[is_last]

    def __repr__(self):
        p = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({p})"
