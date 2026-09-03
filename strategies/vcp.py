"""VCP / 브레이크아웃 (CANSLIM 계열, 단순화 버전).

조건:
  - 고점 근처에서 횡보 (near_high): 종가가 base 기간 최고가의 85% 이상
  - 변동성 수축 (contracting): 최근 20일 변동성이 base 평균 변동성보다 작음
  - 피벗 돌파 (breakout): 종가가 직전 pivot 기간 고가를 상향 돌파
  - 거래량 확인: 거래량이 50일 평균의 vol_mult 배 이상
청산: 종가가 20일 이동평균 하회.

주의: 실제 VCP 는 시각적 패턴 판단이 들어감. 이건 규칙 기반 근사치 — 그대로 실거래 금지, 백테스트로 검증·튜닝 전제.
"""
import pandas as pd
from .base import Strategy


class VCPBreakout(Strategy):
    name = "vcp"

    def __init__(self, base: int = 50, pivot: int = 20, vol_mult: float = 1.5, **kw):
        super().__init__(base=base, pivot=pivot, vol_mult=vol_mult)
        self.base = base
        self.pivot = pivot
        self.vol_mult = vol_mult

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        c = df["Close"]
        h = df["High"]
        v = df["Volume"]

        recent_high = c.rolling(self.base).max()
        near_high = c >= recent_high * 0.85

        vol20 = c.pct_change().rolling(20).std()
        contracting = vol20 < vol20.rolling(self.base).mean()

        pivot_level = h.rolling(self.pivot).max().shift(1)
        vol_avg = v.rolling(50).mean()
        breakout = (c > pivot_level) & (v > vol_avg * self.vol_mult) & near_high & contracting

        ma20 = c.rolling(20).mean()
        exit_sig = c < ma20

        out = pd.DataFrame(index=df.index)
        out["entry"] = breakout.fillna(False)
        out["exit"] = exit_sig.fillna(False)
        return out
