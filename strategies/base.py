"""전략 베이스 — 단일 신호로직(Single Source of Truth).

모든 전략은 generate_signals(df) 로 entry/exit 불리언 신호만 만든다.
이 신호를 simple / backtrader / vectorbt 세 엔진이 공유 → 전략 재작성 불필요.
"""
from abc import ABC, abstractmethod
import pandas as pd


class Strategy(ABC):
    name = "base"

    def __init__(self, **params):
        self.params = params

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """df(OHLCV) → DataFrame[index=df.index, columns=['entry','exit']] (bool)."""
        ...

    def __repr__(self):
        p = ", ".join(f"{k}={v}" for k, v in self.params.items())
        return f"{self.name}({p})"
