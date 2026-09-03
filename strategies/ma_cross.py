"""이동평균 교차 — baseline. fast 가 slow 를 골든크로스하면 진입, 데드크로스면 청산."""
import pandas as pd
from .base import Strategy


class MovingAverageCross(Strategy):
    name = "ma_cross"

    def __init__(self, fast: int = 20, slow: int = 50, **kw):
        super().__init__(fast=fast, slow=slow)
        self.fast = fast
        self.slow = slow

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["Close"]
        f = c.rolling(self.fast).mean()
        s = c.rolling(self.slow).mean()
        cross_up = (f > s) & (f.shift() <= s.shift())
        cross_dn = (f < s) & (f.shift() >= s.shift())
        out = pd.DataFrame(index=df.index)
        out["entry"] = cross_up.fillna(False)
        out["exit"] = cross_dn.fillna(False)
        return out
