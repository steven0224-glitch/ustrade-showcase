"""팩터 라이브러리 — 가격 패널 → 횡단면 팩터 점수 (높을수록 기대수익 ↑).

ml-for-trading(WorldQuant 101 alphas 계열) 발췌. 가격 기반만 구현.
가치/퀄리티 팩터는 펀더멘털(FMP) 필요 → 추후 확장.

부호 규약: 모든 팩터는 "값이 클수록 매수" 방향으로 정규화.
  - low_volatility, reversal 은 음수화 (저변동성·최근 패자 반등이 양의 기대)
"""
import numpy as np
import pandas as pd


def momentum(prices: pd.DataFrame, lookback: int = 126, skip: int = 21) -> pd.DataFrame:
    """일반 모멘텀: skip 봉 전 제외, lookback 봉 전 대비 수익률 (값↑ = 매수).

    lookback/skip 거래일 단위 (21≈1개월, 126≈6개월, 252≈12개월).
    """
    return prices.shift(skip) / prices.shift(lookback) - 1.0


def momentum_12_1(prices: pd.DataFrame) -> pd.DataFrame:
    """12-1 모멘텀: 최근 1개월 제외 12개월 수익률 (고전 모멘텀)."""
    return momentum(prices, lookback=252, skip=21)


def momentum_6_1(prices: pd.DataFrame) -> pd.DataFrame:
    """6-1 모멘텀 (우리 rs_momentum 기본값과 동일 계열)."""
    return momentum(prices, lookback=126, skip=21)


def reversal_1m(prices: pd.DataFrame) -> pd.DataFrame:
    """단기 반전: 최근 1개월 수익률의 음수 (최근 패자 매수)."""
    return -(prices / prices.shift(21) - 1.0)


def low_volatility(prices: pd.DataFrame) -> pd.DataFrame:
    """저변동성: 63일 일간수익률 표준편차의 음수 (저변동 = 양의 기대)."""
    return -(prices.pct_change().rolling(63).std())


def high_52w(prices: pd.DataFrame) -> pd.DataFrame:
    """52주 고점 근접도: 현재가 / 252일 최고가 (고점 근처 = 모멘텀)."""
    return prices / prices.rolling(252).max()


def _rsi(prices: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """Wilder RSI 0~100 (열별 벡터화). 내부용 — 팩터/전략 공유."""
    delta = prices.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100.0 - 100.0 / (1.0 + rs)


def rsi_reversal(prices: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """RSI 평균회귀 팩터: 50 - RSI (과매도=양수=매수 가설). IC 가 실제 부호 검증."""
    return 50.0 - _rsi(prices, period)


def bollinger_reversal(prices: pd.DataFrame, window: int = 20, k: float = 2.0) -> pd.DataFrame:
    """볼린저 %b 평균회귀 팩터: 0.5 - %b (하단밴드 근처=양수=매수 가설). IC 가 부호 검증.
    %b = (price - lower) / (upper - lower)."""
    ma = prices.rolling(window).mean()
    sd = prices.rolling(window).std()
    width = (2 * k * sd).replace(0, np.nan)
    pctb = (prices - (ma - k * sd)) / width
    return 0.5 - pctb


def pullback_uptrend(prices: pd.DataFrame, window: int = 20, trend: int = 200) -> pd.DataFrame:
    """상승되돌림 팩터: 상승추세(>200MA)에서만 최근 고점 대비 되돌림 깊이 (깊을수록 매수 가설).
    비추세 종목은 NaN(횡단면 제외) — 하락추세 낙폭과대(falling knife) 회피."""
    recent_high = prices.rolling(window).max()
    depth = (recent_high - prices) / recent_high
    uptrend = prices > prices.rolling(trend).mean()
    return depth.where(uptrend)


FACTOR_REGISTRY = {
    "momentum_12_1": momentum_12_1,
    "momentum_6_1": momentum_6_1,
    "reversal_1m": reversal_1m,
    "low_volatility": low_volatility,
    "high_52w": high_52w,
    "rsi_reversal": rsi_reversal,
    "bollinger_reversal": bollinger_reversal,
    "pullback_uptrend": pullback_uptrend,
}


def get_factor(name: str, prices: pd.DataFrame) -> pd.DataFrame:
    if name not in FACTOR_REGISTRY:
        raise KeyError(f"알 수 없는 팩터: {name}. 사용가능: {list(FACTOR_REGISTRY)}")
    return FACTOR_REGISTRY[name](prices)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """횡단면(행 방향) z-score 정규화."""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1), axis=0)


def composite(prices: pd.DataFrame, names: list) -> pd.DataFrame:
    """선택 팩터들을 z-score 평균한 멀티팩터 합성점수."""
    zs = [zscore(get_factor(n, prices)) for n in names]
    stacked = pd.concat(zs).groupby(level=0).mean()
    return stacked.reindex(prices.index)
