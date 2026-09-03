"""diversification.py — 분산비율(DR) 관찰 지표. 계산 전용, 매매 경로 어디서도 import 안 함.

DR = (Σ wᵢσᵢ) / σ_p   (Choueifaty & Coignard 2008, "Toward Maximum Diversification")
  · 1.0  = 보유가 완전 동행 — 티커만 다른 사실상 한 베팅
  · 클수록 상관구조가 실제 분산 효과를 내는 중 (DR² ≈ 유효 독립 베팅 수)

하우스엔 섹터·테마 노출 한도가 코드에 없다(HOUSE.md §3) — 같은 테마 중복 베팅 점검은
risk 데스크 사람 눈이 유일 방어선. 이 지표는 그 점검을 숫자 하나로 표면화한다.
관찰 전용: 이 값으로 어떤 자동 액션도 하지 않는다 (selection_review 리포트·대시보드 병기).
출처: docs/queue-post-freeze.md 2026-07-22 항목 (원칙 1 단서 — 관찰 전용 pre-판정 구현, 사용자 승인).
"""
import math

TRADING_DAYS = 252.0
LOOKBACK = 60      # 상관 추정 창(세션) — 대략 1분기
MIN_ROWS = 21      # 최소 수익률 행수 — 이 미만이면 추정 포기(None). 20봉 vol 관례와 정합


def div_ratio(weights, closes, lookback=LOOKBACK, min_rows=MIN_ROWS):
    """분산비율 계산. throw 안 함 — 추정 불가면 None (관찰 지표 계약).

    weights : {ticker: 비중} — 양수만 사용, 합은 임의(내부 정규화. 평가액을 그대로 넣어도 됨)
    closes  : {ticker: [종가, ...]} 시간순 리스트(꼬리 정렬 가정 — 같은 캐시 패널 출신이면 성립)
              pandas DataFrame(index=날짜, columns=티커)도 허용
    반환    : {"dr", "port_vol", "wavg_vol", "n_used", "n_total"} 또는 None
              n_used < n_total 이면 일부 종목이 데이터 부족으로 제외된 것(비중 재정규화됨).
    """
    try:
        import numpy as np

        if hasattr(closes, "columns"):                      # DataFrame → dict of lists
            closes = {str(c): closes[c].dropna().tolist() for c in closes.columns}

        pos = {t: float(w) for t, w in (weights or {}).items()
               if isinstance(w, (int, float)) and math.isfinite(float(w)) and float(w) > 0}
        n_total = len(pos)
        if n_total == 0:
            return None

        # 데이터 충분한 종목만 — 꼬리(min 길이) 정렬로 수익률 행렬 구성
        usable = {t: [float(x) for x in closes.get(t) or []] for t in pos}
        usable = {t: c for t, c in usable.items()
                  if len(c) >= min_rows + 1 and all(math.isfinite(x) and x > 0 for x in c[-(lookback + 1):])}
        if not usable:
            return None
        L = min(min(len(c) for c in usable.values()), lookback + 1)
        if L < min_rows + 1:
            return None

        tickers = sorted(usable)
        px = np.array([usable[t][-L:] for t in tickers], dtype=float).T   # (L × n)
        rets = px[1:] / px[:-1] - 1.0                                     # (L-1 × n)
        w = np.array([pos[t] for t in tickers], dtype=float)
        w = w / w.sum()                                                   # 재정규화(제외분 흡수)

        vols = rets.std(axis=0, ddof=1) * math.sqrt(TRADING_DAYS)         # 종목별 연환산 σ
        port_vol = float((rets @ w).std(ddof=1) * math.sqrt(TRADING_DAYS))
        wavg_vol = float((w * vols).sum())
        if not (math.isfinite(port_vol) and math.isfinite(wavg_vol)) or port_vol <= 1e-12:
            return None
        return {"dr": round(wavg_vol / port_vol, 3),
                "port_vol": round(port_vol, 4), "wavg_vol": round(wavg_vol, 4),
                "n_used": len(tickers), "n_total": n_total}
    except Exception:
        return None
