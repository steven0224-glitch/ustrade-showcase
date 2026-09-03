"""시계열 모멘텀 — 최근 lookback 영업일 수익률이 threshold 초과면 보유.

상대강도(종목간 랭킹) 버전은 다종목 동시 백테스트가 필요 → 추후 확장.
여기선 단일종목 시계열 모멘텀(time-series momentum)으로 구현.
"""
import pandas as pd
from .base import Strategy


class TimeSeriesMomentum(Strategy):
    name = "momentum"

    def __init__(self, lookback: int = 126, threshold: float = 0.0, **kw):
        super().__init__(lookback=lookback, threshold=threshold)
        self.lookback = lookback
        self.threshold = threshold

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["Close"]
        mom = c / c.shift(self.lookback) - 1.0
        long = (mom > self.threshold).fillna(False)
        prev = long.shift(1, fill_value=False)
        out = pd.DataFrame(index=df.index)
        out["entry"] = long & (~prev)   # 상태 진입 전환
        out["exit"] = (~long) & prev    # 상태 이탈 전환
        return out
