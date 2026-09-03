"""상대강도 모멘텀 (cross-sectional / relative-strength).

매 리밸런스일: 유니버스 내 모멘텀 상위 top_n 종목을 동일비중 보유.
모멘텀 = (skip일 전 가격) / (lookback일 전 가격) - 1   (최근 skip일은 제외 = 12-1 모멘텀)
"""
import pandas as pd
from .portfolio_base import PortfolioStrategy


class RelativeStrengthMomentum(PortfolioStrategy):
    name = "rs_momentum"

    def __init__(self, lookback: int = 252, skip: int = 21, top_n: int = 5,
                 freq: str = "M", **kw):
        super().__init__(lookback=lookback, skip=skip, top_n=top_n, freq=freq)
        self.lookback = lookback
        self.skip = skip
        self.top_n = top_n
        self.freq = freq

    def generate_weights(self, prices: pd.DataFrame) -> pd.DataFrame:
        mom = prices.shift(self.skip) / prices.shift(self.lookback) - 1.0
        rebal = self.rebalance_dates(prices, self.freq)

        rows = {}
        for d in rebal:
            row = mom.loc[d].dropna()
            if len(row) == 0:
                continue
            winners = row.nlargest(min(self.top_n, len(row))).index
            w = pd.Series(0.0, index=prices.columns)
            w[winners] = 1.0 / len(winners)
            rows[d] = w

        if not rows:
            raise ValueError("리밸런스 비중 생성 실패 — 기간/lookback 확인")
        return pd.DataFrame(rows).T
