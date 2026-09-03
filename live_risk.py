"""라이브 리스크 오버레이 — 레짐필터 + 변동성타겟 (backtest risk_runner 의 라이브 버전).

backtest 는 일별 적용(engines/risk_runner), 여기선 리밸런스 단일시점 적용:
  레짐 OFF (SPY < 200MA)  → 목표비중 전부 0 (전량 현금)
  레짐 ON                 → vol_target/실현변동성 으로 총노출 스케일 (max_leverage=1.0, 디레버리지만)
확정구성과 동일 파라미터: regime_ma=200, vol_target=0.20.
"""
import numpy as np
import pandas as pd

import data


def regime_on(benchmark="SPY", regime_ma=200, lookback_days=420):
    """SPY 200MA 레짐이 현재 ON(상승, 종가>200MA)인지 단일 bool — 장중 진입게이트용.
    일1런 apply_overlay 와 *동일 정의·동일 기준세션*(last_completed_session) 사용 → 진짜 '재사용'.
    레짐은 일봉 신호라 장중 변화가 사실상 없어 1회 산출로 충분.

    stale 가드(apply_overlay 와 대칭): SPY 마지막봉이 기준세션 대비 1세션 초과 뒤쳐지면(캐시 미갱신
    등) 옛 종가로 약세장을 ON 오판할 수 있으므로 None. 데이터 부족/로드실패/stale → None →
    호출측이 '판정불가 → 진입허용'(fail-open). 일1런이 이미 자기 레짐을 반영했으므로, 장중 게이트가
    데이터 hiccup 으로 전 진입을 봉쇄하는 것보다 fail-open 이 안전·일관."""
    from datetime import timedelta
    try:
        from calendar_util import last_completed_session, session_gap
        ses = last_completed_session()                            # 일1런(run_live)과 동일 기준세션
        if ses is None:
            return None
        start = (ses - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
        end = (ses + timedelta(days=1)).strftime("%Y-%m-%d")      # exclusive → 기준세션봉 포함
        spy = data.load(benchmark, start, end)["Close"].dropna()
    except Exception:
        return None
    if len(spy) < regime_ma:                                       # 200MA 산출 불가
        return None
    try:                                                          # stale 거부 — 옛 종가 레짐 오판 차단
        if session_gap(spy.index[-1], ses.isoformat()) > 1:
            return None
    except Exception:
        pass                                                     # 갭 계산 실패는 치명적 아님(아래로 진행)
    px = float(spy.iloc[-1])
    mav = float(spy.rolling(regime_ma).mean().iloc[-1])
    if not (np.isfinite(px) and np.isfinite(mav)):
        return None
    return bool(px > mav)


def apply_overlay(prices, weights, vol_target=0.20, regime_ma=200, vol_lookback=20,
                  max_leverage=1.0, benchmark="SPY"):
    info = {}

    # 1) 레짐 — SPY 200MA
    # SPY 윈도우는 가격 패널과 동일 구간에서 도출 (동결 기본값 금지 — 레짐이 stale 데이터로
    # 계산되면 무인 시스템이 영구 현금/영구 풀투자로 잘못 고착됨).
    # end 는 마지막봉+1일 — yfinance end 는 exclusive 라 +1 해야 당일 세션봉이 포함됨
    # (안 하면 레짐이 1세션 stale 한 SPY 종가로 계산됨).
    start = prices.index[0].strftime("%Y-%m-%d")
    end = (prices.index[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    spy_raw = data.load(benchmark, start, end)["Close"]
    # SPY 마지막 유효봉이 패널 대비 stale 이면 ffill 이 옛 종가로 레짐 오판(영구현금/영구풀투자 고착) →
    # 명시적 에러(호출측 거래 보류). SPY 는 매 세션 거래하므로 패널 마지막 세션 대비 1세션 초과 stale 이면 결함.
    if len(spy_raw):
        from calendar_util import session_gap
        if session_gap(spy_raw.index[-1], prices.index[-1].strftime("%Y-%m-%d")) > 1:
            raise ValueError(f"SPY stale — 마지막봉 {spy_raw.index[-1].date()} vs 패널 {prices.index[-1].date()} "
                             f"(ffill 시 레짐 오판)")
    spy = spy_raw.reindex(prices.index).ffill()
    ma = spy.rolling(regime_ma).mean()
    px, mav = float(spy.iloc[-1]), float(ma.iloc[-1])
    if not (np.isfinite(px) and np.isfinite(mav)):
        # SPY 데이터가 regime_ma 보다 짧음 → 레짐 판정 불가. 조용히 OFF(영구현금)로 빠지지
        # 말고 명시적 에러 (호출측이 거래 보류로 처리).
        raise ValueError(f"레짐 계산 불가 — SPY 데이터 부족 (px={px}, {regime_ma}MA={mav})")
    risk_on = px > mav
    info.update(regime=("ON" if risk_on else "OFF"), spy=px, spy_ma=mav)
    if not risk_on:
        info["scale"] = 0.0
        return {}, info   # 전량 현금

    # 2) 변동성 타겟 — 선택종목 가중포트 실현변동성
    if weights and vol_target:
        w = np.array([weights[t] for t in weights])
        ret = prices[list(weights)].pct_change(fill_method=None).tail(vol_lookback)
        # 완전행(모든 선정종목 유효)만 사용 — 단일 종목의 중간 데이터갭(NaN)이 port_ret 전체를 NaN 으로
        # 오염시켜 realized=NaN→ValueError→그날 리밸런스 통째 스킵하던 것 방지(갭 행만 제외해 견고화).
        ret = ret.dropna(how="any")
        if len(ret) < 2:
            # 완전행 부족(데이터 갭 과다) → 벡터 변동성 추정 불가. scale=1.0(풀사이즈 진행)으로 fail-open 하면
            # 고변동 레짐서 과노출 위험 → 명시적 에러로 fail-closed(호출측 거래 보류). 단일 종목 소수 갭은
            # 위 dropna 로 흡수되므로 여기 도달=대량 결측(진짜 이상).
            raise ValueError(f"변동성 추정 불가 — 완전행 부족(데이터 갭 과다, rows={len(ret)})")
        port_ret = (ret.values * w).sum(axis=1)
        realized = float(np.std(port_ret) * np.sqrt(252))
        if not np.isfinite(realized):
            raise ValueError(f"변동성 추정 불가 — 데이터 부족 (realized={realized})")
        scale = 1.0 if realized == 0 else float(np.clip(vol_target / realized, 0.0, max_leverage))
    else:
        realized, scale = 0.0, 1.0
    info.update(realized_vol=realized, scale=scale)
    return {t: v * scale for t, v in weights.items()}, info
