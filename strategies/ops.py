"""Alpha 연산자 라이브러리 — HKUDS/Vibe-Trading `agent/src/factors/base.py` 이식.

모든 연산자는 **wide** DataFrame (index=거래일, columns=티커) 위에서 동작하고
같은 shape 를 반환한다. 원 계약과 동일한 두 불변식을 그대로 보존:

  - Look-ahead 금지: `delta(df, d)`·`delay(df, d)` 는 d>=1 강제. 음수 시프트(미래참조) 없음.
  - NaN 전파: 조용한 fillna(0) 없음. 워밍업·상수윈도우는 NaN. +/-inf 금지(호출부가 걸러냄).

alpha_zoo 의 compute() 들이 이 연산자만으로 짜여 있어, Vibe zoo 공식을 거의 그대로 옮겨온다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _as_float(df: pd.DataFrame) -> pd.DataFrame:
    if df.dtypes.eq(np.float64).all():
        return df
    return df.astype(np.float64)


# ── 횡단면(행 방향) ─────────────────────────────────────────────
def rank(df: pd.DataFrame) -> pd.DataFrame:
    """행(횡단면) 백분위 순위 [0,1]. NaN 유지, all-NaN 행은 all-NaN."""
    return df.rank(axis=1, method="average", pct=True, na_option="keep")


def scale(df: pd.DataFrame, a: float = 1.0) -> pd.DataFrame:
    """행 L1 정규화 — |합|=a. abs-sum 0/all-NaN 행은 NaN (조용한 0 없음)."""
    df = _as_float(df)
    abs_sum = df.abs().sum(axis=1, skipna=True)
    abs_sum = abs_sum.where(abs_sum > 0)
    return df.mul(a).div(abs_sum, axis=0)


# ── 시계열(열 방향, 후방참조) ────────────────────────────────────
def ts_rank(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """n-윈도우 내 마지막 값의 순위(백분위 [0,1]). 워밍업 NaN."""
    if n < 1:
        raise ValueError(f"ts_rank window must be >= 1, got {n}")

    def _last_rank(arr: np.ndarray) -> float:
        if np.isnan(arr).all():
            return np.nan
        last = arr[-1]
        if np.isnan(last):
            return np.nan
        valid = arr[~np.isnan(arr)]
        if valid.size == 0:
            return np.nan
        less = (valid < last).sum()
        eq = (valid == last).sum()
        return float((less + 0.5 * (eq + 1)) / valid.size)

    return df.rolling(window=n, min_periods=n).apply(_last_rank, raw=True)


def ts_corr(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    """열별 롤링 피어슨 상관. 상수윈도우 → NaN(조용한 0 없음), inf → NaN."""
    if n < 2:
        raise ValueError(f"ts_corr window must be >= 2, got {n}")
    x, y = _as_float(x), _as_float(y)
    cols = x.columns.union(y.columns)
    corr = x.reindex(columns=cols).rolling(window=n, min_periods=n).corr(y.reindex(columns=cols))
    return corr.replace([np.inf, -np.inf], np.nan)


def ts_cov(x: pd.DataFrame, y: pd.DataFrame, n: int) -> pd.DataFrame:
    """열별 롤링 표본 공분산."""
    if n < 2:
        raise ValueError(f"ts_cov window must be >= 2, got {n}")
    x, y = _as_float(x), _as_float(y)
    cols = x.columns.union(y.columns)
    cov = x.reindex(columns=cols).rolling(window=n, min_periods=n).cov(y.reindex(columns=cols))
    return cov.replace([np.inf, -np.inf], np.nan)


def ts_mean(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 1:
        raise ValueError(f"ts_mean window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).mean()


def ts_sum(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """롤링 합; 워밍업 NaN (Vibe zoo 의 _rolling_sum)."""
    if n < 1:
        raise ValueError(f"ts_sum window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).sum()


def ts_std(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 2:
        raise ValueError(f"ts_std window must be >= 2, got {n}")
    return df.rolling(window=n, min_periods=n).std(ddof=1)


def ts_max(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 1:
        raise ValueError(f"ts_max window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).max()


def ts_min(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 1:
        raise ValueError(f"ts_min window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).min()


def _argmax_last(arr: np.ndarray) -> float:
    if np.isnan(arr).all():
        return np.nan
    return float(np.argmax(np.where(np.isnan(arr), -np.inf, arr)))


def _argmin_last(arr: np.ndarray) -> float:
    if np.isnan(arr).all():
        return np.nan
    return float(np.argmin(np.where(np.isnan(arr), np.inf, arr)))


def ts_argmax(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 1:
        raise ValueError(f"ts_argmax window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).apply(_argmax_last, raw=True)


def ts_argmin(df: pd.DataFrame, n: int) -> pd.DataFrame:
    if n < 1:
        raise ValueError(f"ts_argmin window must be >= 1, got {n}")
    return df.rolling(window=n, min_periods=n).apply(_argmin_last, raw=True)


def delta(df: pd.DataFrame, d: int) -> pd.DataFrame:
    """d-lag 1차 차분 df - df.shift(d). Look-ahead 금지: d>=1 강제."""
    if d < 1:
        raise ValueError(f"delta lag must be >= 1 (lookahead ban), got {d}")
    return df - df.shift(d)


def delay(df: pd.DataFrame, d: int) -> pd.DataFrame:
    """후방 시프트 d봉. Look-ahead 금지: d>=1 강제 (Vibe zoo 의 _delay)."""
    if d < 1:
        raise ValueError(f"delay requires d >= 1 (lookahead ban), got {d}")
    return df.shift(d)


def decay_linear(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """선형감쇠 가중이동평균 (가중치 n, n-1, …, 1 정규화). 워밍업 NaN."""
    if n < 1:
        raise ValueError(f"decay_linear window must be >= 1, got {n}")
    weights = np.arange(n, 0, -1, dtype=np.float64)
    weights /= weights.sum()

    def _apply(arr: np.ndarray) -> float:
        if np.isnan(arr).any():
            return np.nan
        return float(np.dot(arr, weights))

    return df.rolling(window=n, min_periods=n).apply(_apply, raw=True)


def signed_power(df: pd.DataFrame, p: float) -> pd.DataFrame:
    """sign(df) * |df|**p — 부호 보존, 복소수 없음."""
    arr = df.to_numpy(dtype=np.float64, na_value=np.nan)
    out = np.sign(arr) * np.power(np.abs(arr), p)
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def safe_div(a: pd.DataFrame, b: pd.DataFrame, eps: float = 1e-12) -> pd.DataFrame:
    """안전 나눗셈 a / (b + eps*sign(b)). b==0/NaN → NaN (조용한 inf/0 없음)."""
    a, b = _as_float(a), _as_float(b)
    b_arr = b.to_numpy(dtype=np.float64, na_value=np.nan)
    denom = pd.DataFrame(b_arr + eps * np.sign(b_arr), index=b.index, columns=b.columns)
    return a.div(denom).replace([np.inf, -np.inf], np.nan)


def make_one(ref: pd.DataFrame) -> pd.DataFrame:
    """ref 와 같은 shape 의 1.0 프레임 (Vibe zoo 의 _make_one)."""
    return pd.DataFrame(1.0, index=ref.index, columns=ref.columns)


def where_ternary(cond, a, b) -> pd.DataFrame:
    """벡터화 삼항 (cond ? a : b). a/b 는 DataFrame 또는 스칼라. 비유한 → NaN."""
    cond_arr = cond.to_numpy(dtype=bool, na_value=False) if hasattr(cond, "to_numpy") else np.asarray(cond, dtype=bool)
    a_arr = np.full_like(cond_arr, float(a), dtype=np.float64) if isinstance(a, (int, float)) \
        else a.to_numpy(dtype=np.float64, na_value=np.nan)
    b_arr = np.full_like(cond_arr, float(b), dtype=np.float64) if isinstance(b, (int, float)) \
        else b.to_numpy(dtype=np.float64, na_value=np.nan)
    out = np.where(cond_arr, a_arr, b_arr)
    out = np.where(np.isfinite(out), out, np.nan)
    idx = cond.index if hasattr(cond, "index") else a.index
    cols = cond.columns if hasattr(cond, "columns") else a.columns
    return pd.DataFrame(out, index=idx, columns=cols)
