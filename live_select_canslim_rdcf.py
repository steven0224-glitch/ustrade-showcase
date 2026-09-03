"""canslim + 역DCF 밸류 오버레이 — canslim 의 A/B 실험군 (12주 병행, 2026-08-14 개시).

canslim(§B 본선)과 **선정 로직만** 다르다: 동일 가격스크린·펀더점수(💎CANSLIM+📋애널) 위에
**역DCF 소프트 틸트**를 얹는다 — "지금 주가가 요구하는 내재성장률"이 "회사의 실제 과거성장률"을
크게 앞서는(=완벽하게 가격된) 종목에 점수 감점. 배제가 아니라 순위만 낮춘다(모멘텀 종목은 원래
비싸서 하드 게이트는 풀을 비운다). buffett↔buffett_v2 와 동일한 A/B 패턴.

⚠️ **canslim(§B)은 손대지 않는다** — 이 파일은 별도 엔진이다. leaf 헬퍼만 import 공유하고
select() 오케스트레이션은 재구현(공유 함수를 고치면 12주 A/B 한쪽이 바뀌어 실험이 깨짐 — 하우스 규약).

데이터:
  - 과거성장률: canslim_tag(t) 의 rev_g — canslim 이 이미 부르는 값(추가 FMP 0).
  - 내재성장 입력(market_cap·FCF): fmp.key_metrics_ttm(t) 1콜/종목(marketCap·freeCashFlowYieldTTM,
    FCF=fcf_yield×market_cap). 실패/결측 → 그 종목 틸트 0 = canslim 순위 그대로(fail-open).

틸트 정의(캘리브레이션 knob):
  gap = implied_growth − rev_g. penalty = clip((gap − GAP_FLOOR)/GAP_SCALE, 0, PENALTY_CAP).
  adjusted_score = score − penalty. gap≤GAP_FLOOR(적정/저평가)면 무감점.
"""
import pathlib
import sys

import fmp_factors as ff
from logsetup import get_logger

# canslim 의 leaf 헬퍼 재사용(오케스트레이션은 아래서 재구현) — 모듈 import 는 canslim.py 를
# 수정하지 않는다. import 부작용(A engine 경로 주입)은 canslim 과 동일.
from live_select_canslim import _mom_12_1, _is_value_trap
from strategies import reverse_dcf as rdcf

_A_DIR = pathlib.Path(__file__).resolve().parent.parent / "텔레그램_시그널_알리미"
if _A_DIR.is_dir() and str(_A_DIR) not in sys.path:
    sys.path.append(str(_A_DIR))
from engine.funda import canslim_tag, is_analyst_buy, piotroski  # noqa: E402

try:
    from fmp_client import FMP
except Exception:                        # FMP 클라이언트 부재 → 밸류 조회 불가 → 전 종목 틸트 0(canslim 로 폴백)
    FMP = None

_log = get_logger("live_select_canslim_rdcf")

# 틸트 캘리브레이션 — 물리상수 아님, 튜닝 대상. GAP_FLOOR 이하는 무감점(적정/저평가).
GAP_FLOOR = 0.10      # 내재성장이 실제성장을 +10%p 초과할 때부터 감점 시작
GAP_SCALE = 0.20      # +30%p 갭이면 만점 감점(=PENALTY_CAP)
PENALTY_CAP = 1.0     # 최대 감점(펀더점수 1점분 — score 는 0~2라 순위 재편은 하되 지배 안 함)
WACC = rdcf.DEFAULT_WACC


def _valuation_penalty(market_cap, fcf_yield, rev_g):
    """(penalty 0~CAP, gap|None). 데이터 부적격이면 (0.0, None) = 무감점 폴백."""
    if market_cap is None or fcf_yield is None or rev_g is None:
        return 0.0, None
    try:
        mc = float(market_cap)
        fcf0 = float(fcf_yield) * mc
        hist_g = float(rev_g)
    except (TypeError, ValueError):
        return 0.0, None
    implied, flag = rdcf.implied_growth(mc, fcf0, WACC)
    if implied is None:                  # 적자 FCF·시총≤0 등 — 이 축으로 평가 불가, 무감점
        return 0.0, None
    gap = rdcf.valuation_gap(implied, hist_g)
    if gap is None or gap <= GAP_FLOOR:
        return 0.0, gap
    penalty = min((gap - GAP_FLOOR) / GAP_SCALE, PENALTY_CAP)
    return penalty, gap


def select(prices, lookback=252, top_n=3, pool=12, min_margin=0.0, max_pe=80.0,
           min_score=0, value_trap_gate=False, min_proximity=0.85, **_):
    """canslim + 역DCF 틸트 → (weights, info). live_select.select 과 동일 계약.

    canslim.select 과 1~3단계(가격스크린·펀더점수·안전다이얼)는 동일. 차이는 랭킹 직전
    adjusted_score = score − valuation_penalty 로 순위를 매기는 것뿐.
    """
    # ── 1) 가격 스크린 (canslim 과 동일) ──────────────────────────────────────────
    rows = []
    for t in prices.columns:
        s = prices[t].dropna()
        if len(s) < 252:
            continue
        close = float(s.iloc[-1])
        sma200 = float(s.rolling(200).mean().iloc[-1])
        high52 = float(s.rolling(252).max().iloc[-1])
        mom = _mom_12_1(s)
        prox = close / high52 if high52 > 0 else 0.0
        if not (close > sma200 and prox >= min_proximity and mom > 0):
            continue
        rows.append({"ticker": t, "close": close, "mom": mom, "prox": prox})
    rows.sort(key=lambda r: (-r["mom"], r["ticker"]))
    pool_rows = rows[:pool]

    # ── 2) 펀더 점수 + rev_g 포획(canslim 은 rev_g 를 버림) ────────────────────────
    fmp = FMP() if FMP is not None else None
    funda_absent = 0
    val_degraded = 0
    for r in pool_rows:
        t = r["ticker"]
        rev_g = None
        try:
            leader, canslim = canslim_tag(t)      # (💎, {eps_g, rev_g})
            analyst = bool(is_analyst_buy(t))
            rev_g = canslim.get("rev_g") if isinstance(canslim, dict) else None
        except Exception as e:
            _log.warning("A엔진 스킵 %s: %s", t, e)
            leader, canslim, analyst = False, {}, False
        r["score"] = int(bool(leader)) + int(analyst)
        r["canslim"] = bool(leader)
        r["analyst"] = analyst
        r["f"] = None
        if not canslim:
            funda_absent += 1
        # 역DCF 밸류 조회(1콜/종목) — 실패/결측은 무감점 폴백(canslim 순위 보존)
        mc = fyield = None
        if fmp is not None:
            try:
                km = fmp.key_metrics_ttm(t) or {}
                mc, fyield = km.get("marketCap"), km.get("freeCashFlowYieldTTM")
            except Exception as e:
                _log.warning("key_metrics 스킵 %s: %s", t, ff._safe_err(e))
        pen, gap = _valuation_penalty(mc, fyield, rev_g)
        r["val_penalty"] = pen
        r["val_gap"] = gap
        if pen == 0.0 and gap is None:
            val_degraded += 1
        if value_trap_gate:
            try:
                r["f"] = piotroski(t)
            except Exception as e:
                _log.warning("piotroski 스킵 %s: %s", t, e)
                r["f"] = {}
    screen_degraded = ff.screen_degraded_flag(len(pool_rows), funda_absent)

    # ── 3) 안전 다이얼 + 밸류틸트 랭킹 + 선정 ─────────────────────────────────────
    eligible = list(pool_rows)
    below_min = []
    value_traps = []
    if min_score > 0:
        below_min = [r["ticker"] for r in eligible if r["score"] < min_score]
        eligible = [r for r in eligible if r["score"] >= min_score]
    if value_trap_gate:
        value_traps = [r["ticker"] for r in eligible if _is_value_trap(r["f"])]
        eligible = [r for r in eligible if not _is_value_trap(r["f"])]

    # 핵심 차이: adjusted = score − val_penalty. 동점 tie-break 은 canslim 과 동일(-mom, ticker).
    for r in eligible:
        r["adj_score"] = r["score"] - r["val_penalty"]
    eligible.sort(key=lambda r: (-r["adj_score"], -r["mom"], r["ticker"]))
    final = eligible[:top_n]
    weights = ff.equal_weight([r["ticker"] for r in final])

    if not value_trap_gate:
        for r in final:
            try:
                r["f"] = piotroski(r["ticker"])
            except Exception as e:
                _log.warning("piotroski 스킵 %s: %s", r["ticker"], e)
                r["f"] = {}

    info = {
        "strategy": "canslim_rdcf",
        "candidates": [r["ticker"] for r in pool_rows],
        "final": [r["ticker"] for r in final],
        "scores": {r["ticker"]: r["score"] for r in pool_rows},
        "adj_scores": {r["ticker"]: round(r.get("adj_score", r["score"]), 3) for r in pool_rows},
        "val_penalty": {r["ticker"]: round(r["val_penalty"], 3) for r in pool_rows if r["val_penalty"]},
        "val_gap": {r["ticker"]: (round(r["val_gap"], 3) if r["val_gap"] is not None else None)
                    for r in pool_rows},
        "canslim": [r["ticker"] for r in pool_rows if r["canslim"]],
        "analyst": [r["ticker"] for r in pool_rows if r["analyst"]],
        "piotroski": {r["ticker"]: (r["f"] or {}).get("score")
                      for r in pool_rows if r.get("f")},
        "momentum_only": [r["ticker"] for r in pool_rows[:top_n]],
        "screen_degraded": screen_degraded,
        "val_degraded": val_degraded,      # 밸류 조회 실패 종목수(무감점 폴백된 수) — 관측용
        "below_min_score": below_min,
        "excluded_value_trap": value_traps,
    }
    return weights, info
