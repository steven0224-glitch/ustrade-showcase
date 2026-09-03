"""라이브 종목선택 v2 — 텔레그램 시그널(A)의 코어 매수신호 이식.

A `engine/screen.py:screen_buys` 가격 스크린(12-1 모멘텀 게이트) + `engine/funda.py`
교차검증(💎CANSLIM·📋애널 strong_buy·Piotroski)을 B의 `select()` 계약에 맞춰 래핑.
live_select.select(모멘텀+FMP) 과 동일 시그니처 → live_engine 무수정 디스패치.

신호 파이프라인 (입력 prices 종가패널 → (weights, info)):
  1. 가격 스크린(하드게이트) : close>200MA AND prox(52주근접)>=min_proximity AND 12-1 모멘텀>0
                               → (-mom,ticker) 정렬 상위 pool 을 펀더 검증 풀로
  2. 교차검증(랭킹 틸트)      : score = 💎CANSLIM(EPS YoY>=25%&매출>=10%) + 📋애널 strong_buy
                               (A와 동일 — 모멘텀이 게이트, 펀더는 점수. 필수 아님)
  3. 선정                     : (-score,-mom,ticker) 정렬 상위 top_n → 동일비중
  4. 안전 다이얼(opt-in)      : min_score(>=N 강제), value_trap_gate(Piotroski reliable&F<5 제외)

레짐(SPY 200MA)·vol타겟은 select 이후 live_risk.apply_overlay 가 처리 → 여기서 중복 안 함.
모멘텀은 12-1(A 검증 우월: OOS 1.27) — B 현행 6-1(live_select)과 의도적으로 다름.
임계값(prox 0.85, EPS 25%/매출 10%, strong_buy)은 A 원본 그대로. 펀더는 A 함수 직접 호출.
"""
import pathlib
import sys

import fmp_factors as ff
from logsetup import get_logger

# A(텔레그램_시그널_알리미)의 engine 패키지 경로 주입 — 정교한 펀더 함수 단일소스 재사용(복제 X).
# A는 B의 형제 디렉토리(.../Projects/텔레그램_시그널_알리미). 절대경로 하드코딩 대신 형제경로.
# ⚠ append(끝)로 넣음 — A에도 data/·config.py 등 B와 동명 항목이 있어 sys.path 앞에 넣으면
#   B의 data.py 를 가린다. engine 패키지는 A에만 있어 끝에 둬도 해석되고 B 모듈은 우선 보존.
_A_DIR = pathlib.Path(__file__).resolve().parent.parent / "텔레그램_시그널_알리미"
if _A_DIR.is_dir() and str(_A_DIR) not in sys.path:
    sys.path.append(str(_A_DIR))

# engine.funda 는 yfinance 만 의존(data/universe import 안 함) → 부작용·순환 없음. 전부 graceful.
from engine.funda import canslim_tag, is_analyst_buy, piotroski  # noqa: E402

_log = get_logger("live_select_canslim")


def _mom_12_1(s) -> float:
    """12-1 모멘텀 = 252일전→21일전 수익(최근 1개월 스킵). A engine/indicators.mom_12_1 미러."""
    denom = float(s.iloc[-252])
    if denom <= 0:                       # 0/음수 가격 — inf 모멘텀이 풀 1위로 편입되는 것 차단(prox 가드와 대칭)
        return 0.0
    return float(s.iloc[-21] / denom - 1.0)


def _is_value_trap(f: dict) -> bool:
    """Piotroski 신뢰가능(tested>=7) & F<5 → value trap. A screen_garp 게이트와 동일 기준."""
    return bool(f.get("reliable")) and (f.get("score") or 0) < 5


def select(prices, lookback=252, top_n=3, pool=12, min_margin=0.0, max_pe=80.0,
           min_score=0, value_trap_gate=False, min_proximity=0.85, **_):
    """A 코어신호 → (target_weights dict, info dict). live_select.select 과 동일 계약.

    lookback/min_margin/max_pe 는 호출부 호환용으로 받되 미사용(모멘텀은 고정 12-1,
    펀더 게이트는 A 방식). min_score/value_trap_gate/min_proximity 로 보수성 조절.
    """
    # ── 1) 가격 스크린 (A screen_buys:56-71 미러, B의 prices 패널 위에서) ──────────
    rows = []
    for t in prices.columns:
        s = prices[t].dropna()
        if len(s) < 252:                       # 12-1·52주(252봉) 필요
            continue
        close = float(s.iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1])
        high52 = float(s.rolling(252).max().iloc[-1])
        mom = _mom_12_1(s)
        prox = close / high52 if high52 > 0 else 0.0
        if not (close > sma200 and prox >= min_proximity and mom > 0):
            continue
        rows.append({"ticker": t, "close": close, "mom": mom, "prox": prox})
    # 모멘텀 내림차순, 동률은 ticker 오름차순 tie-break(결정론). A와 동일.
    rows.sort(key=lambda r: (-r["mom"], r["ticker"]))
    pool_rows = rows[:pool]

    # ── 2) 교차검증 스코어 (A engine/funda 직접 호출, 종목당 순차 — 429 완화) ────────
    # value_trap_gate 켜질 때만 풀 전체 Piotroski 조회(게이트가 선정 前 필요). 평소엔 선정분만.
    funda_absent = 0
    for r in pool_rows:
        t = r["ticker"]
        try:                                    # 종목별 격리 — A엔진 hiccup 하나가 그날 선정 전체를 안 죽임(FMP snapshot 경로와 대칭)
            leader, canslim = canslim_tag(t)       # (💎, {eps_g,rev_g}) — 실패 시 (False,{})
            analyst = bool(is_analyst_buy(t))      # 📋 strong_buy
        except Exception as e:
            _log.warning("A엔진 스킵 %s: %s", t, e)
            leader, canslim, analyst = False, {}, False
        r["score"] = int(bool(leader)) + int(analyst)
        r["canslim"] = bool(leader)
        r["analyst"] = analyst
        r["f"] = None
        if value_trap_gate:
            try:
                r["f"] = piotroski(t)
            except Exception as e:
                _log.warning("piotroski 스킵 %s: %s", t, e)
                r["f"] = {}
        # 펀더 데이터 부재 추정(canslim 빈 dict = 분기손익 미수신, 또는 A엔진 예외). 대량 부재 시 degraded.
        if not canslim:
            funda_absent += 1
    screen_degraded = ff.screen_degraded_flag(len(pool_rows), funda_absent)

    # ── 3) 안전 다이얼 + 랭킹 + 선정 ─────────────────────────────────────────────
    eligible = list(pool_rows)
    below_min = []
    value_traps = []
    if min_score > 0:
        below_min = [r["ticker"] for r in eligible if r["score"] < min_score]
        eligible = [r for r in eligible if r["score"] >= min_score]
    if value_trap_gate:
        value_traps = [r["ticker"] for r in eligible if _is_value_trap(r["f"])]
        eligible = [r for r in eligible if not _is_value_trap(r["f"])]

    eligible.sort(key=lambda r: (-r["score"], -r["mom"], r["ticker"]))
    final = eligible[:top_n]
    weights = ff.equal_weight([r["ticker"] for r in final])

    # value_trap_gate 가 off 였으면 선정분만 Piotroski 조회(투명성·저비용)
    if not value_trap_gate:
        for r in final:
            try:                                # 종목별 격리(FMP snapshot 경로와 대칭)
                r["f"] = piotroski(r["ticker"])
            except Exception as e:
                _log.warning("piotroski 스킵 %s: %s", r["ticker"], e)
                r["f"] = {}

    info = {
        "strategy": "canslim",
        "candidates": [r["ticker"] for r in pool_rows],          # 모멘텀 통과 풀
        "final": [r["ticker"] for r in final],
        "scores": {r["ticker"]: r["score"] for r in pool_rows},
        "canslim": [r["ticker"] for r in pool_rows if r["canslim"]],
        "analyst": [r["ticker"] for r in pool_rows if r["analyst"]],
        "piotroski": {r["ticker"]: (r["f"] or {}).get("score")
                      for r in pool_rows if r.get("f")},
        "momentum_only": [r["ticker"] for r in pool_rows[:top_n]],
        "screen_degraded": screen_degraded,
        "below_min_score": below_min,
        "excluded_value_trap": value_traps,
    }
    return weights, info
