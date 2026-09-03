"""Stage 4 (적대 재검증 후속 수정) 검증 — 네트워크 불필요.

S4-1 부분체결 감사추적 / S4-2 reset 윈도우 비움 / S4-3 cross-day 에러윈도우 /
S4-4 RunLock 동시실행 차단 / S4-6 NaN 가드 / S4-7 청산 시세불량 허용 /
S4-8 __getattr__ 재귀가드 + halt_kind 자동해제.
(S4-5 레짐 end+1 = tests_stage1 C1 에서 검증.)
실행:  & $py tests_stage4.py
"""
import copy
import sys

import numpy as np
import pandas as pd

from broker import (GuardConfig, KillSwitch, HaltError, GuardedBroker, RunLock,
                    Executor)
from broker.base import (AccountInfo, Quote, Order, OrderRequest, OrderStatus,
                         Side, OrderType, Position)
from tests_stage1 import _use_temp_state, _fake_select

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


class _Broker:
    """결정론 브로커. fill/price/positions 지정. trip_on=N 이면 N번째 주문서 HaltError."""
    def __init__(self, fill=True, price=100.0, equity=1_000_000.0, positions=None, trip_on=None):
        self._fill, self._price, self._eq = fill, price, equity
        self._pos = positions or []
        self._trip_on, self._n = trip_on, 0

    def connect(self): pass
    def disconnect(self): pass
    def get_account(self): return AccountInfo(self._eq, self._eq, self._eq)
    def get_positions(self): return list(self._pos)
    def get_quote(self, s): return Quote(s, self._price, self._price, self._price)

    def place_order(self, req):
        self._n += 1
        if self._trip_on and self._n == self._trip_on:
            raise HaltError("mid-loop trip (테스트)")
        o = Order(order_id=f"o-{self._n}", request=req)
        if self._fill:
            o.status = OrderStatus.FILLED; o.filled_qty = req.qty; o.avg_fill_price = self._price
        else:
            o.status = OrderStatus.REJECTED; o.message = "테스트 거부"
        return o

    def cancel_order(self, i): return False
    def get_order(self, i): raise KeyError(i)


# ───── S4-1 — 루프 중 트립해도 이미 체결된 주문이 보고에 남음 ─────
def test_s4_1_partial_audit():
    print("[S4-1] 루프 중 HaltError → tripped 에도 체결주문·계좌 보고 (유령포지션 방지)")
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.34, "BBB": 0.33, "CCC": 0.33})
    try:
        cfg = RunConfig(vol_target=0.0, max_staleness_sessions=0)
        broker = _Broker(fill=True, trip_on=2)   # 2번째 주문서 트립
        res = run_once(None, broker, cfg, today="2026-06-01")
    finally:
        live_engine.select = orig
    check("status == tripped", res["status"] == "tripped", res["status"])
    check("체결된 주문 1건 보고됨", len(res.get("orders", [])) == 1, res.get("orders"))
    check("계좌 스냅샷 포함(감사추적)", "account" in res, list(res))


# ───── S4-2 — reset 후 즉시 재트립 안 함 ─────
def test_s4_2_reset_clears_window():
    print("[S4-2] reset 이 에러 윈도우 비움 → 리셋 직후 첫 에러로 재트립 안 함")
    _use_temp_state()
    ks = KillSwitch(config=GuardConfig(max_consecutive_errors=3, error_window=6), today="2026-06-01")
    for _ in range(3):
        try: ks.record_error("x")
        except HaltError: pass
    check("3회 에러 → 트립", ks.state["halted"])
    ks.reset()
    check("reset 후 recent 비움", ks.state["recent"] == [], ks.state["recent"])
    tripped = False
    try: ks.record_error("after-reset")
    except HaltError: tripped = True
    check("리셋 직후 단일 에러 → 재트립 안 함", not tripped and not ks.state["halted"])


# ───── S4-3 — 하루 1회 실패가 날짜 넘어 누적 ─────
def test_s4_3_cross_day_window():
    print("[S4-3] 매일 1회 실패도 윈도우 누적 → 트립 (roll_day 가 recent 안 비움)")
    _use_temp_state()
    cfg = GuardConfig(max_consecutive_errors=3, error_window=6)
    tripped_day = None
    for i, day in enumerate(["2026-06-01", "2026-06-02", "2026-06-03"], 1):
        ks = KillSwitch(config=cfg, today=day)   # 매일 새 프로세스 모사 (state 영속)
        ks.roll_day(100000.0)
        try:
            ks.record_error(f"feed fail {day}")
        except HaltError:
            tripped_day = i
    check("3일째(누적 3회) 트립", tripped_day == 3, f"tripped_day={tripped_day}")


# ───── S4-4 — 동시 실행 차단 ─────
def test_s4_4_runlock():
    print("[S4-4] 락 보유 중 run_once → status=locked (더블트레이드 방지)")
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once
    orig = live_engine.select
    w3 = {"AAA": 0.34, "BBB": 0.33, "CCC": 0.33}   # 바운드(40%) 통과
    live_engine.select = _fake_select(w3)
    try:
        cfg = RunConfig(vol_target=0.0, max_staleness_sessions=0)
        with RunLock():   # 다른 프로세스가 락 보유 모사
            res = run_once(None, _Broker(), cfg, today="2026-06-01", force=True)
    finally:
        live_engine.select = orig
    check("락 점유 시 locked 반환", res["status"] == "locked", res["status"])
    # 락 해제 후엔 정상
    res2 = None
    live_engine.select = _fake_select(w3)
    try:
        res2 = run_once(None, _Broker(), RunConfig(vol_target=0.0, max_staleness_sessions=0),
                        today="2026-06-01", force=True)
    finally:
        live_engine.select = orig
    check("락 해제 후 정상 실행", res2 and res2["status"] == "ok", res2 and res2["status"])


# ───── S4-6 — NaN 가드 ─────
def test_s4_6_nan_guards():
    print("[S4-6] NaN 비중 가드 우회 차단 + SPY/변동성 부족 시 명시적 에러")
    _use_temp_state()
    ks = KillSwitch(today="2026-06-01")
    tripped = False
    try:
        ks.check_targets({"AAA": float("nan")})
    except HaltError:
        tripped = True
    check("NaN 비중 → check_targets 트립(우회 차단)", tripped)

    import live_risk
    # 패널 50행 < 200MA → rolling(200) 전부 NaN → mav NaN → raise (조용한 OFF 아님)
    idx50 = pd.bdate_range(end="2026-05-29", periods=50)
    short_prices = pd.DataFrame({"AAA": np.linspace(100, 200, 50)}, index=idx50)
    orig = live_risk.data.load
    live_risk.data.load = lambda t, s, e, force=False: pd.DataFrame(
        {"Close": np.linspace(300, 600, 50)}, index=idx50)
    raised = False
    try:
        live_risk.apply_overlay(short_prices, {"AAA": 1.0}, regime_ma=200)
    except ValueError:
        raised = True
    finally:
        live_risk.data.load = orig
    check("SPY<200MA → 레짐 계산불가 ValueError (조용한 OFF 아님)", raised)

    # 변동성 추정 불가(가중종목 tail 전부 NaN) → raise (regime 은 정상 통과)
    idx = pd.bdate_range(end="2026-05-29", periods=300)   # 300행 → SPY mav 유한 → regime ON
    p2 = pd.DataFrame({"AAA": [np.nan] * 300, "SPYref": np.linspace(1, 2, 300)}, index=idx)
    live_risk.data.load = lambda t, s, e, force=False: pd.DataFrame(
        {"Close": np.linspace(300, 600, 300)}, index=idx)
    raised2 = False
    try:
        live_risk.apply_overlay(p2, {"AAA": 1.0}, regime_ma=200)
    except ValueError:
        raised2 = True
    finally:
        live_risk.data.load = orig
    check("변동성 추정불가 → ValueError (NaN 비중 생성 안 함)", raised2)


# ───── S4-7 — 청산은 시세불량이어도 허용, 매수는 거부 ─────
def test_s4_7_exit_on_bad_quote():
    print("[S4-7] 비정상 시세 — 청산(SELL) 허용, 매수(BUY) 거부")
    _use_temp_state()
    # executor.plan: 보유 AAA, 목표 {} (전량청산), 시세 0 → SELL 생성 (raise 안 함)
    broker = _Broker(fill=True, price=0.0, positions=[Position("AAA", 10, 100.0)])
    exe = Executor(broker)
    reqs = exe.plan({})   # 전량 청산
    sells = [r for r in reqs if r.side == Side.SELL and r.symbol == "AAA"]
    check("시세 0 이어도 청산 SELL 생성", len(sells) == 1 and sells[0].qty == 10, reqs)

    # GuardedBroker: SELL 시세0 → 허용, BUY 시세0 → ValueError
    ks = KillSwitch(today="2026-06-01")
    gb = GuardedBroker(_Broker(fill=True, price=0.0), ks)
    o = gb.place_order(OrderRequest("AAA", Side.SELL, 10, OrderType.MARKET))
    check("GuardedBroker: SELL 시세0 허용", o.status == OrderStatus.FILLED, o.status)
    raised = False
    try:
        gb.place_order(OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    except ValueError:
        raised = True
    check("GuardedBroker: BUY 시세0 거부", raised)


# ───── S4-8 — __getattr__ 재귀가드 + halt_kind 자동해제 ─────
def test_s4_8_getattr_and_haltkind():
    print("[S4-8] GuardedBroker deepcopy 안전 + 일일손실만 새 날 자동해제")
    _use_temp_state()
    ks = KillSwitch(today="2026-06-01")
    gb = GuardedBroker(_Broker(), ks)
    ok = True
    try:
        copy.deepcopy(gb)   # _inner 접근 전 __getattr__ 무한재귀였던 케이스
    except RecursionError:
        ok = False
    except Exception:
        ok = True   # deepcopy 가 다른 이유로 실패해도 무한재귀만 아니면 OK
    check("deepcopy 무한재귀 없음", ok)

    # 일일손실 트립 → 새 날 roll_day 자동 해제
    _use_temp_state()
    ks1 = KillSwitch(today="2026-06-01")
    ks1.state["day_start_equity"] = 100000.0
    try: ks1.check_daily_loss(90000.0)   # -10% → trip(kind=daily_loss)
    except HaltError: pass
    check("일일손실 트립됨", ks1.state["halted"] and ks1.state["halt_kind"] == "daily_loss")
    ks2 = KillSwitch(today="2026-06-02")
    ks2.roll_day(90000.0)
    check("새 날 → 일일손실 정지 자동 해제", not ks2.state["halted"])

    # 비-일일손실 트립(포지션바운드)은 새 날에도 유지
    _use_temp_state()
    ks3 = KillSwitch(today="2026-06-01")
    try: ks3.check_targets({"AAA": 0.99})   # 바운드 위반 trip(kind="")
    except HaltError: pass
    ks4 = KillSwitch(today="2026-06-02")
    ks4.roll_day(100000.0)
    check("바운드 정지는 새 날에도 유지(자동해제 안 함)", ks4.state["halted"])


def test_s4_10_daily_loss_resume_integration():
    print("[S4-10] 일일손실 정지가 새 날 run_once 에서 자동해제 (is_halted 조기반환 회귀)")
    import live_engine
    from live_engine import RunConfig, run_once
    orig = live_engine.select
    w3 = {"AAA": 0.34, "BBB": 0.33, "CCC": 0.33}

    # day1: 일일손실 트립 영속
    _use_temp_state()
    ks1 = KillSwitch(today="2026-06-01")
    ks1.state["day"] = "2026-06-01"
    ks1.state["day_start_equity"] = 100000.0
    try: ks1.check_daily_loss(90000.0)   # -10% → trip(kind=daily_loss), 상태 저장
    except HaltError: pass
    check("day1 일일손실 트립 영속", ks1.is_halted()[0] and ks1.state["halt_kind"] == "daily_loss")

    # day2: run_once(운영 흐름) → halted 로 막히면 안 됨 (자동해제 후 진행)
    live_engine.select = _fake_select(w3)
    try:
        res = run_once(None, _Broker(fill=True), RunConfig(vol_target=0.0, max_staleness_sessions=0),
                       today="2026-06-02")
    finally:
        live_engine.select = orig
    check("새 날 run_once 가 halted 로 막히지 않음(자동해제)", res["status"] != "halted", res["status"])
    check("새 날 정상 진행(ok)", res["status"] == "ok", res["status"])

    # 대조: 같은 날 재실행은 정지 유지 (자동해제 안 함)
    _use_temp_state()
    ksA = KillSwitch(today="2026-06-01")
    ksA.state["day"] = "2026-06-01"
    ksA.state["day_start_equity"] = 100000.0
    try: ksA.check_daily_loss(90000.0)
    except HaltError: pass
    live_engine.select = _fake_select(w3)
    try:
        resB = run_once(None, _Broker(fill=True), RunConfig(vol_target=0.0, max_staleness_sessions=0),
                        today="2026-06-01", force=True)
    finally:
        live_engine.select = orig
    check("같은 날 재실행은 정지 유지", resB["status"] == "halted", resB["status"])


def test_s4_9_secret_scrub():
    print("[S4-9] FMP apikey 가 에러/로그에 노출 안 됨 (PII/비밀 유출 차단)")
    import fmp_factors as ff
    import requests
    leak = ("429 for url: https://financialmodelingprep.com/stable/ratios-ttm"
            "?symbol=CAT&apikey=SECRETKEY123")
    scrubbed = ff._safe_err(requests.HTTPError(leak))
    check("factors: 키 마스킹", "SECRETKEY123" not in scrubbed and "apikey=***" in scrubbed, scrubbed)


def main():
    print("=" * 70)
    print(" Stage 4 (적대 재검증 후속) 검증 — 네트워크 없음")
    print("=" * 70)
    for t in (test_s4_1_partial_audit, test_s4_2_reset_clears_window, test_s4_3_cross_day_window,
              test_s4_4_runlock, test_s4_6_nan_guards, test_s4_7_exit_on_bad_quote,
              test_s4_8_getattr_and_haltkind, test_s4_10_daily_loss_resume_integration,
              test_s4_9_secret_scrub):
        print()
        t()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
