"""오프라인 오버레이 백테스트 공용 헬퍼 — vol_estimator / threshold_rebalance 두 스크립트 공유.

*연구 전용.* 실거래/모의 경로 어디서도 import 안 함 (research/ 격리 관례,
divergence_backtest 전례). 라이브 지오메트리(live_risk.apply_overlay)를 벡터로 재현:
  · 레짐: SPY 종가 > 200MA (t-1 정보 → t 적용, 무-lookahead)
  · 스케일: clip(vol_target / σ, 0, 1) — 디레버리지만
  · 현행 σ: 20봉 rolling std(ddof=0)×√252  (live_risk.py:93 과 동일 정의)

비용 상수: 왕복 0.35% = 수수료 0.1%×2 + 슬리피지 0.05% + 반스프레드 0.025% 양방향
(strategy 데스크 memory 2026-07-07 산식 — 코드 상수에서 직접 산출된 값).

⚠️ 생존편향: diversified 바스켓 = '오늘 살아남은' 대형주 28 (universe.py:3-5 경고).
절대 성과는 상향 편향 — 추정기/리밸 방식 '상대 비교' 용도로만 해석할 것.
"""
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트
import data          # noqa: E402
import universe      # noqa: E402

TRADING_DAYS = 252.0
VOL_TARGET = 0.20          # live_engine.py:58 기본값
REGIME_MA = 200
ROUNDTRIP_COST = 0.0035    # 왕복 0.35%
PER_SIDE_COST = ROUNDTRIP_COST / 2.0

START = "2011-01-01"       # 실효 시작은 완전행 정책상 META 상장(2012-05) 이후 —
                           # 2015-16·2018Q4·2020 크래시·2022 약세 포함 (하락장 요건 충족)
WARMUP = 260               # 200MA + vol 추정 워밍업 (거래일)


def load_panel_and_spy(start=START, end=None, basket="diversified"):
    """(종가 패널 T×N, SPY 종가) — 공통 세션 정렬. 전 종목 완전행만(META 2012 상장 이후)."""
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")
    tickers = universe.get_universe(basket)
    panel = data.load_panel(tickers, start, end).dropna(how="any")
    spy = data.load("SPY", start, end)["Close"].reindex(panel.index).ffill()
    return panel, spy


def risk_on_series(spy, ma=REGIME_MA):
    """레짐 시리즈 — spy>MA. t 시점 값은 t 종가 기준(사용측이 t-1 값을 t 에 적용해 무-lookahead)."""
    return (spy > spy.rolling(ma).mean()).to_numpy()


def rolling_vol(rets, n):
    """현행 추정기 — 직전 n 봉 std(ddof=0)×√252. rets[t] 까지 포함한 값이 vols[t]."""
    return pd.Series(rets).rolling(n).std(ddof=0).to_numpy() * np.sqrt(TRADING_DAYS)


def ewma_vol(rets, lam, init_n=60):
    """EWMA(RiskMetrics) — σ²_t = λσ²_{t-1} + (1-λ)r²_t. vols[t]=r_t 까지 반영된 σ.
    초기화: 첫 init_n 봉 분산(편향 완화)."""
    r2 = np.asarray(rets, dtype=float) ** 2
    out = np.full(len(r2), np.nan)
    if len(r2) < init_n + 1:
        return out
    v = float(np.mean(r2[:init_n]))
    for i in range(init_n, len(r2)):
        v = lam * v + (1.0 - lam) * r2[i]
        out[i] = v
    return np.sqrt(out * TRADING_DAYS)


def ann_metrics(daily_ret, freq=TRADING_DAYS):
    """일수익률 배열 → {cagr, vol, sharpe, mdd}. 빈 배열 graceful."""
    r = np.asarray(daily_ret, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 2:
        return {"cagr": 0.0, "vol": 0.0, "sharpe": 0.0, "mdd": 0.0}
    eq = np.cumprod(1.0 + r)
    years = len(r) / freq
    cagr = eq[-1] ** (1.0 / years) - 1.0 if years > 0 and eq[-1] > 0 else -1.0
    vol = r.std(ddof=0) * np.sqrt(freq)
    sharpe = (r.mean() * freq) / vol if vol > 1e-12 else 0.0
    peak = np.maximum.accumulate(eq)
    mdd = float((eq / peak - 1.0).min())
    return {"cagr": cagr, "vol": vol, "sharpe": sharpe, "mdd": mdd}


def out_path(name):
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)
