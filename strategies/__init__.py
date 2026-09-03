"""전략 레지스트리 — 이름으로 전략 객체 생성.

단일종목(REGISTRY) / 포트폴리오(PORTFOLIO_REGISTRY) 두 종류.
"""
from .base import Strategy
from .ma_cross import MovingAverageCross
from .momentum import TimeSeriesMomentum
from .vcp import VCPBreakout
from .rsi import RSI
from .bollinger import Bollinger
from .retracement import Retracement

from .portfolio_base import PortfolioStrategy
from .cross_momentum import RelativeStrengthMomentum

REGISTRY = {cls.name: cls for cls in [MovingAverageCross, TimeSeriesMomentum, VCPBreakout,
                                      RSI, Bollinger, Retracement]}
PORTFOLIO_REGISTRY = {cls.name: cls for cls in [RelativeStrengthMomentum]}


def get_strategy(name: str, **params) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"알 수 없는 전략: {name}. 사용 가능: {list(REGISTRY)}")
    return REGISTRY[name](**params)


def get_portfolio_strategy(name: str, **params) -> PortfolioStrategy:
    if name not in PORTFOLIO_REGISTRY:
        raise KeyError(f"알 수 없는 포트폴리오 전략: {name}. 사용 가능: {list(PORTFOLIO_REGISTRY)}")
    return PORTFOLIO_REGISTRY[name](**params)
