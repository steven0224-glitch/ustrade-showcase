"""Stage 1 (CRITICAL 수정) 검증 — 네트워크 불필요 (data.load/select monkeypatch).

C1 레짐 frozen-window 제거 / C2 멱등성 / C3 체결게이트 / C4 GuardedBroker 경계.
실행:  & $py tests_stage1.py
"""
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from broker import guardrail  # 상태파일 경로 monkeypatch 대상
from broker.base import AccountInfo, Quote, Order, OrderRequest, OrderStatus, Side, OrderType

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _use_temp_state():
    """KillSwitch 상태파일을 임시 디렉토리로 (실제 state/ 오염 방지)."""
    d = Path(tempfile.mkdtemp(prefix="ks_"))
    guardrail.STATE_DIR = d
    guardrail.STATE_FILE = d / "killswitch.json"
    guardrail.KILL_FILE = d / "HALT"
    guardrail.LOCK_FILE = d / "run.lock"
    return d


# ───────────────────────── C1 — 레짐 frozen-window 제거 ─────────────────────────
def test_c1_regime_window():
    print("[C1] 레짐 SPY 윈도우 = 가격패널 구간 (동결 기본값 없음)")
    import live_risk

    idx = pd.bdate_range(end="2026-05-29", periods=300)
    prices = pd.DataFrame({"AAA": np.linspace(100, 200, 300),
                           "BBB": np.linspace(50, 90, 300)}, index=idx)
    calls = {}

    def fake_load(ticker, start, end, force=False):
        calls["start"], calls["end"] = start, end
        # 상승 추세 SPY → 마지막 > 200MA → 레짐 ON
        return pd.DataFrame({"Close": np.linspace(300, 600, 300)}, index=idx)

    orig = live_risk.data.load
    live_risk.data.load = fake_load
    try:
        w = {"AAA": 0.5, "BBB": 0.5}
        out_w, info = live_risk.apply_overlay(prices, w, vol_target=0.20, regime_ma=200)
    finally:
        live_risk.data.load = orig

    check("SPY start = 패널 시작일", calls["start"] == idx[0].strftime("%Y-%m-%d"),
          f"got {calls['start']}")
    # end 는 마지막봉+1일 (yfinance exclusive 보정, S4-5) — 동결 기본값 아님
    expected_end = (idx[-1] + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    check("SPY end = 패널 종료일+1 (exclusive 보정)", calls["end"] == expected_end,
          f"got {calls['end']}")
    check("동결 기본값 2025-01-01 안 씀", calls["end"] != "2025-01-01")
    check("상승추세 → 레짐 ON", info["regime"] == "ON", info)
    check("ON 이면 비중 반환(현금 아님)", out_w != {} and 0 < info["scale"] <= 1.0, info)

    # 하락 추세 → OFF → 전량 현금
    def fake_down(ticker, start, end, force=False):
        return pd.DataFrame({"Close": np.linspace(600, 300, 300)}, index=idx)
    live_risk.data.load = fake_down
    try:
        out_w2, info2 = live_risk.apply_overlay(prices, {"AAA": 1.0}, vol_target=0.20, regime_ma=200)
    finally:
        live_risk.data.load = orig
    check("하락추세 → 레짐 OFF → 현금", out_w2 == {} and info2["regime"] == "OFF", info2)


# ───────────────────────── 공용 FakeBroker ─────────────────────────
class FakeBroker:
    """결정론적 브로커 — fill 여부 지정. 네트워크 없음."""
    def __init__(self, fill=True, price=100.0, equity=1_000_000.0):
        self._fill, self._price, self._equity = fill, price, equity
        self._ids = 0

    def connect(self): pass
    def disconnect(self): pass
    def get_account(self): return AccountInfo(cash=self._equity, equity=self._equity, buying_power=self._equity)
    def get_positions(self): return []
    def get_quote(self, s): return Quote(symbol=s, last=self._price, bid=self._price, ask=self._price)

    def place_order(self, req: OrderRequest) -> Order:
        self._ids += 1
        o = Order(order_id=f"f-{self._ids}", request=req)
        if self._fill:
            o.status = OrderStatus.FILLED
            o.filled_qty = req.qty
            o.avg_fill_price = self._price
        else:
            o.status = OrderStatus.REJECTED
            o.message = "테스트 거부"
        return o

    def cancel_order(self, oid): return False
    def get_order(self, oid): raise KeyError(oid)


def _fake_select(weights):
    def sel(prices, **kw):
        return dict(weights), {"final": list(weights), "candidates": [], "fails": {},
                               "missing": [], "momentum_only": list(weights)}
    return sel


# ───────────────────────── C3 — 체결 게이트 ─────────────────────────
def test_c3_fill_gate():
    print("[C3] 거부 주문 → status=partial, 연속에러 누적, mark_traded 안 함")
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once

    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.33, "BBB": 0.33, "CCC": 0.33})  # 바운드 통과
    try:
        cfg = RunConfig(vol_target=0.0)        # apply_overlay 스킵 (SPY 불필요)
        broker = FakeBroker(fill=False)        # 모든 주문 거부
        res = run_once(None, broker, cfg, today="2026-06-01")
    finally:
        live_engine.select = orig

    check("status == partial", res["status"] == "partial", res["status"])
    check("거부 사유 포함", "거부" in res.get("reason", ""), res.get("reason"))
    ks = guardrail.KillSwitch(today="2026-06-01")
    check("연속에러 누적됨(>0)", ks.state.get("errors", 0) > 0, ks.state)
    check("당일 거래완료 기록 안 됨(재조정 가능)", not ks.already_traded())


# ───────────────────────── C2 — 멱등성(당일 1회 락) ─────────────────────────
def test_c2_idempotency():
    print("[C2] 성공 후 같은 날 재실행 → already_ran (중복매매 방지)")
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once

    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.33, "BBB": 0.33, "CCC": 0.33})  # 바운드 통과
    try:
        cfg = RunConfig(vol_target=0.0)
        r1 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-01")
        r2 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-01")          # 같은 날
        r3 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-01", force=True)  # 강제
        r4 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-02")          # 다음 날
    finally:
        live_engine.select = orig

    check("1회차 ok", r1["status"] == "ok", r1["status"])
    check("2회차 already_ran (락)", r2["status"] == "already_ran", r2["status"])
    check("force=True 면 우회 실행", r3["status"] == "ok", r3["status"])
    check("다음 날은 다시 실행", r4["status"] == "ok", r4["status"])


# ───────────────────────── C4 — GuardedBroker 경계 강제 ─────────────────────────
def test_c4_guarded_broker():
    print("[C4] place_order 가 경계에서 HALT/명목 강제 (caller 무관)")
    d = _use_temp_state()
    from broker import GuardedBroker, KillSwitch, HaltError, Executor

    # (a) HALT 파일 존재 → 어떤 주문도 거부
    (d / "HALT").write_text("stop", encoding="utf-8")
    ks = KillSwitch(today="2026-06-01")
    gb = GuardedBroker(FakeBroker(fill=True), ks)
    req = OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET)
    halted = False
    try:
        gb.place_order(req)
    except HaltError:
        halted = True
    check("HALT 파일 → place_order 차단", halted)

    # Executor 직접 호출도 GuardedBroker 통하면 차단 (우회 불가) — rebalance() 삭제(가드 우회
    # 어포던스, EXEC-audit #5)로 plan()+place_order 로 동일 경로 재현.
    exe = Executor(gb)
    blocked = False
    try:
        for r in exe.plan({"AAA": 1.0}):
            gb.place_order(r)
    except HaltError:
        blocked = True
    check("Executor.plan+place_order 도 가드 통과", blocked)
    (d / "HALT").unlink()

    # (b) 명목 초과 (fat-finger) → 차단
    ks2 = KillSwitch(today="2026-06-01")
    gb2 = GuardedBroker(FakeBroker(fill=True, price=100.0), ks2)
    big = OrderRequest("AAA", Side.BUY, 20000, OrderType.MARKET)  # 2,000,000 > 1,000,000 한도
    fat = False
    try:
        gb2.place_order(big)
    except HaltError:
        fat = True
    check("명목 200만 > 한도 100만 → 차단", fat)

    # (c) 정상 범위 주문은 통과 + 위임 메서드 동작
    ks3 = KillSwitch(today="2026-06-02")  # 새 날(이전 trip 영속하므로 깨끗한 상태 위해 reset)
    ks3.reset()
    gb3 = GuardedBroker(FakeBroker(fill=True, price=100.0), ks3)
    o = gb3.place_order(OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    check("정상 주문 통과(FILLED)", o.status == OrderStatus.FILLED, o.status)
    check("__getattr__ 위임 (get_account)", gb3.get_account().equity == 1_000_000.0)


def main():
    print("=" * 70)
    print(" Stage 1 (CRITICAL) 검증 — PaperBroker/FakeBroker, 네트워크 없음")
    print("=" * 70)
    for t in (test_c1_regime_window, test_c2_idempotency, test_c3_fill_gate, test_c4_guarded_broker):
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
