"""상승되돌림 (업트렌드 눌림목 반등 진입 · 추세 이탈 청산).

조건:
  - 업트렌드: 종가 > 추세 MA(기본 200)
  - 진입: 업트렌드 중 종가가 단기 MA(기본 20) 를 하향 눌림 후 상향 회복(반등)
  - 청산: 종가가 추세 MA 하회 (추세 이탈)

주의: 규칙 기반 근사 — 그대로 실거래 금지, 백테스트·IC 검증 전제(VCPBreakout 와 동일 정책).
하락추세 낙폭과대(falling knife) 회피 위해 추세 MA 위에서만 진입.
"""
import pandas as pd
from .base import Strategy


class Retracement(Strategy):
    name = "retracement"

    def __init__(self, fast: int = 20, trend: int = 200, **kw):
        super().__init__(fast=fast, trend=trend)
        self.fast = fast
        self.trend = trend

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["Close"]
        ma_fast = c.rolling(self.fast).mean()
        ma_trend = c.rolling(self.trend).mean()
        uptrend = c > ma_trend
        above_fast = c > ma_fast
        # 업트렌드에서 단기 MA 상향 회복 = 눌림목 반등 진입
        bounce = uptrend & above_fast & ~above_fast.shift(1, fill_value=False)
        out = pd.DataFrame(index=df.index)
        out["entry"] = bounce.fillna(False)
        out["exit"] = (c < ma_trend).fillna(False)
        return out
