"""볼린저밴드 평균회귀 (하단밴드 이탈 진입 · 중심선 회복 청산).

조건:
  - 진입: 종가가 하단밴드(MA - k·σ) 하향 이탈 (과매도)
  - 청산: 종가가 중심선(MA) 상향 회복

주의: 규칙 기반 근사 — 그대로 실거래 금지, 백테스트·IC 검증 전제(VCPBreakout 와 동일 정책).
"""
import pandas as pd
from .base import Strategy


class Bollinger(Strategy):
    name = "bollinger"

    def __init__(self, window: int = 20, k: float = 2.0, **kw):
        super().__init__(window=window, k=k)
        self.window = window
        self.k = k

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["Close"]
        ma = c.rolling(self.window).mean()
        sd = c.rolling(self.window).std()
        lower = ma - self.k * sd
        below = c < lower
        above_mid = c > ma
        out = pd.DataFrame(index=df.index)
        out["entry"] = (below & ~below.shift(1, fill_value=False)).fillna(False)
        out["exit"] = (above_mid & ~above_mid.shift(1, fill_value=False)).fillna(False)
        return out
