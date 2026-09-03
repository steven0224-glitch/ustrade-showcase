"""canslim 전략(live_select_canslim) 검증 — 네트워크 불필요.

A 텔레그램 시그널 코어신호 이식분의 단위테스트:
  가격 게이트(close>200MA·prox>=min_proximity·12-1 mom>0·>=252봉) | 스코어(💎+📋) 랭킹 |
  동일비중 | 공집합 | min_score 게이트 | value_trap 게이트 | screen_degraded.

펀더 함수(canslim_tag/is_analyst_buy/piotroski)는 monkeypatch 스텁 → yfinance 호출 0.
가격 스크린은 합성 패널로 결정적 검증.

실행:  & $py tests_canslim.py
"""
import sys

import numpy as np
import pandas as pd

import live_select_canslim as lsc

PASS, FAIL = [], []

# 패치 복원용 원본 보관
_ORIG = (lsc.canslim_tag, lsc.is_analyst_buy, lsc.piotroski)

N = 300
IDX = pd.bdate_range(end="2026-05-29", periods=N)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _lin(a, b, n=N):
    return np.linspace(a, b, n)


# ── 결정적 합성 시계열(각 게이트 결과를 알고 구성) ──────────────────────────────
PASS_A = _lin(100, 200)                                   # 상승 close=200 prox=1.0 mom≈+0.67
PASS_B = _lin(100, 160)                                   # 상승 mom≈+0.42 (A보다 낮음)
PASS_C = _lin(100, 140)                                   # 상승 mom≈+0.29 (가장 낮음)
BELOW_MA = _lin(300, 120)                                 # 하락 close<200MA → MA 게이트 탈락
NEG_MOM = np.concatenate([_lin(200, 100, 280), _lin(100, 210, 20)])   # 연중하락→막판랠리: prox1.0·MA통과·mom<0
MILD_PB = np.concatenate([_lin(100, 200, 290), _lin(200, 192, 10)])   # 상승후 약한 눌림: prox≈0.96
SHORT = np.concatenate([np.full(100, np.nan), _lin(120, 180, 200)])   # 유효봉 200<252 → 길이 게이트 탈락


def _panel(cols):
    return pd.DataFrame(cols, index=IDX)


def _set_funda(canslim=None, analyst=None, pio=None, default_pio=None):
    """펀더 스텁 주입. canslim: {t:(leader,meta)} | analyst: set(t) | pio: {t:dict}."""
    canslim = canslim or {}
    analyst = analyst or set()
    pio = pio or {}
    dp = default_pio if default_pio is not None else {
        "score": 7, "tested": 8, "reliable": True, "neg_equity": False}
    lsc.canslim_tag = lambda t: canslim.get(t, (False, {"eps_g": 0.0, "rev_g": 0.0}))
    lsc.is_analyst_buy = lambda t: t in analyst
    lsc.piotroski = lambda t: pio.get(t, dp)


# ───── 가격 게이트 ─────
def test_price_gate():
    print("[GATE] 가격 스크린 — MA·proximity·12-1 mom·길이 게이트")
    _set_funda()
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C,
                     "BELOW_MA": BELOW_MA, "NEG_MOM": NEG_MOM, "SHORT": SHORT})
    _, info = lsc.select(prices, top_n=3, pool=12)
    cands = info["candidates"]
    check("통과집합 = 상승 3종목(모멘텀 내림차순)", cands == ["PASS_A", "PASS_B", "PASS_C"], cands)
    check("close<200MA → 탈락", "BELOW_MA" not in cands, cands)
    check("12-1 mom<=0 → 탈락(연중하락 후 막판랠리)", "NEG_MOM" not in cands, cands)
    check("유효봉<252 → 탈락", "SHORT" not in cands, cands)


# ───── proximity 파라미터 게이트 ─────
def test_proximity_param():
    print("[GATE] min_proximity 상향 → 52주 고가서 먼 종목 탈락")
    _set_funda()
    prices = _panel({"MILD_PB": MILD_PB, "PASS_A": PASS_A})
    _, base = lsc.select(prices, top_n=3, pool=12)                       # 기본 0.85
    check("기본 min_proximity=0.85 → MILD_PB(prox≈0.96) 통과", "MILD_PB" in base["candidates"],
          base["candidates"])
    _, tight = lsc.select(prices, top_n=3, pool=12, min_proximity=0.99)  # 상향
    check("min_proximity=0.99 → MILD_PB 탈락", "MILD_PB" not in tight["candidates"],
          tight["candidates"])
    check("min_proximity=0.99 → PASS_A(prox=1.0) 유지", "PASS_A" in tight["candidates"],
          tight["candidates"])


# ───── 스코어 랭킹 (모멘텀이 게이트, 💎+📋 가 랭킹) ─────
def test_score_ranking():
    print("[RANK] score(💎CANSLIM+📋애널) 우선, 동점은 모멘텀 — A와 동일")
    # PASS_C(최저 mom)에 score 2 부여 → 모멘텀 1위 PASS_A 보다 앞서야
    _set_funda(canslim={"PASS_C": (True, {"eps_g": 0.3, "rev_g": 0.2})},
               analyst={"PASS_C"})
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C})
    weights, info = lsc.select(prices, top_n=2, pool=12)
    check("score 2 종목이 모멘텀 1위보다 앞 선정", info["final"] == ["PASS_C", "PASS_A"], info["final"])
    check("PASS_C score=2", info["scores"].get("PASS_C") == 2, info["scores"])
    check("PASS_A score=0", info["scores"].get("PASS_A") == 0, info["scores"])


# ───── 동일비중 ─────
def test_equal_weight():
    print("[SIZE] 선정분 동일비중 1/N")
    _set_funda()
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C})
    weights, info = lsc.select(prices, top_n=3, pool=12)
    check("3종목 선정", len(weights) == 3, weights)
    check("각 비중 = 1/3", all(abs(w - 1 / 3) < 1e-9 for w in weights.values()), weights)
    check("비중합 ≈ 1.0", abs(sum(weights.values()) - 1.0) < 1e-9, sum(weights.values()))


# ───── 공집합 ─────
def test_empty():
    print("[EMPTY] 통과 종목 없음 → 빈 비중(보류, live_engine 이 skip 처리)")
    _set_funda()
    prices = _panel({"BELOW_MA": BELOW_MA})
    weights, info = lsc.select(prices, top_n=3, pool=12)
    check("weights 공집합", weights == {}, weights)
    check("info final 비어있음", info["final"] == [], info["final"])


# ───── min_score 게이트(opt-in) ─────
def test_min_score():
    print("[DIAL] min_score=1 → 확인(💎/📋) 없는 순수 모멘텀 제외")
    _set_funda(canslim={"PASS_C": (True, {"eps_g": 0.3, "rev_g": 0.2})},   # PASS_C score1
               analyst={"PASS_B"})                                         # PASS_B score1
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C})
    weights, info = lsc.select(prices, top_n=3, pool=12, min_score=1)
    check("score 0(PASS_A) 제외", "PASS_A" not in info["final"], info["final"])
    check("PASS_A → below_min_score 기록", "PASS_A" in info["below_min_score"],
          info["below_min_score"])
    # PASS_B·PASS_C 동점(score 1) → 모멘텀 내림차순: PASS_B(mom≈0.42) > PASS_C(mom≈0.29)
    check("선정 = score>=1 둘(동점은 모멘텀 순)", info["final"] == ["PASS_B", "PASS_C"],
          info["final"])


# ───── value_trap 게이트(opt-in) ─────
def test_value_trap_gate():
    print("[DIAL] value_trap_gate → Piotroski reliable & F<5 제외 (기본 off=충실)")
    _set_funda(pio={"TRAP": {"score": 3, "tested": 8, "reliable": True, "neg_equity": False},
                    "CLEAN": {"score": 7, "tested": 8, "reliable": True, "neg_equity": False}})
    prices = _panel({"CLEAN": PASS_A, "TRAP": PASS_B})
    _, off = lsc.select(prices, top_n=2, pool=12, value_trap_gate=False)
    check("게이트 off → TRAP 선정(충실)", "TRAP" in off["final"], off["final"])
    _, on = lsc.select(prices, top_n=2, pool=12, value_trap_gate=True)
    check("게이트 on → TRAP 제외", "TRAP" not in on["final"], on["final"])
    check("TRAP → excluded_value_trap 기록", "TRAP" in on["excluded_value_trap"],
          on["excluded_value_trap"])
    check("CLEAN 유지", "CLEAN" in on["final"], on["final"])


# ───── screen_degraded ─────
def test_screen_degraded():
    print("[DEGRADED] 펀더 대량 결측(분기손익 미수신) → screen_degraded 플래그")
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C})
    # 전부 canslim 빈 dict(결측) → funda_absent 3 > 풀/2 → degraded
    _set_funda(default_pio={"score": None, "tested": 0, "reliable": False, "neg_equity": None})
    lsc.canslim_tag = lambda t: (False, {})        # 빈 meta = 결측
    _, deg = lsc.select(prices, top_n=3, pool=12)
    check("대량 결측 → screen_degraded True", deg["screen_degraded"] is True, deg["screen_degraded"])
    # 대조 — 펀더 정상 수신 시 degraded False
    _set_funda()
    _, ok = lsc.select(prices, top_n=3, pool=12)
    check("펀더 정상 → screen_degraded False", ok["screen_degraded"] is False, ok["screen_degraded"])


def test_a_engine_isolation():
    print("[FIX] A엔진 종목별 hiccup 격리 — 한 종목 예외가 전체 선정을 안 죽임(해당 종목만 결측 취급)")

    def _canslim_tag(t):
        if t == "PASS_B":
            raise RuntimeError("일시적 A엔진 장애")
        return (False, {"eps_g": 0.0, "rev_g": 0.0})

    def _pio(t):
        if t == "PASS_A":
            raise RuntimeError("피오트로스키 조회 실패")
        return {"score": 7, "tested": 8, "reliable": True, "neg_equity": False}

    lsc.canslim_tag = _canslim_tag
    lsc.is_analyst_buy = lambda t: False
    lsc.piotroski = _pio
    prices = _panel({"PASS_A": PASS_A, "PASS_B": PASS_B, "PASS_C": PASS_C})
    threw = False
    try:
        _, info = lsc.select(prices, top_n=3, pool=12)   # value_trap_gate=False(기본) → final 루프서 piotroski 재조회
    except Exception:
        threw = True
    check("종목 예외에도 throw 안 함(격리)", not threw)
    if not threw:
        check("3종목 모두 정상 선정", info["final"] == ["PASS_A", "PASS_B", "PASS_C"], info["final"])
        check("PASS_B(canslim_tag 예외) score=0 — 결측 취급", info["scores"].get("PASS_B") == 0, info["scores"])
        check("PASS_C(정상) piotroski=7 유지(예외 무관 종목은 영향 없음)",
              info["piotroski"].get("PASS_C") == 7, info["piotroski"])
        check("PASS_A(piotroski 예외) 결측(크래시 없이 스킵)", info["piotroski"].get("PASS_A") is None, info["piotroski"])


def test_canslim_pool_default():
    print("[POOL] run_live 가 canslim 후보풀을 12 로 보정 (RunConfig 기본 8 축소 방지)")
    import run_live
    from live_engine import RunConfig
    if not hasattr(run_live, "_normalize_cfg"):
        check("run_live._normalize_cfg 존재", False, "미구현")
        return
    check("canslim pool 8 → 12", run_live._normalize_cfg(RunConfig(strategy="canslim")).pool == 12,
          run_live._normalize_cfg(RunConfig(strategy="canslim")).pool)
    check("canslim pool 20 유지(≥12)", run_live._normalize_cfg(RunConfig(strategy="canslim", pool=20)).pool == 20)
    check("momentum pool 8 유지(미변경)", run_live._normalize_cfg(RunConfig(strategy="momentum")).pool == 8)


def main():
    print("=" * 70)
    print(" canslim 전략(텔레그램 시그널 코어 이식) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    try:
        test_price_gate(); print()
        test_proximity_param(); print()
        test_score_ranking(); print()
        test_equal_weight(); print()
        test_empty(); print()
        test_min_score(); print()
        test_value_trap_gate(); print()
        test_screen_degraded(); print()
        test_a_engine_isolation(); print()
        test_canslim_pool_default()
    finally:
        lsc.canslim_tag, lsc.is_analyst_buy, lsc.piotroski = _ORIG   # 패치 복원
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
