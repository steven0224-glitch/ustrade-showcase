"""panic_exit (비상 전량청산) 검증 — 네트워크 0.

핵심 불변식:
  - 봇 관리분(managed)만 전량 청산, 보호종목(protected) 절대 매도 안 됨.
  - HALT 상태에서도 청산 SELL 은 뚫림(ManagedBroker 직접 제출 → GuardedBroker HALT 게이트 우회).
  - SELL 만, BUY 없음. 청산 후 KillSwitch 신규거래 정지(manual).
  - 미리보기(confirm=False)는 주문·정지 없음.
  - 정규장 외(force_open 없음)는 청산 보류하되 신규거래는 정지.

실행:  & $py tests_panic.py
"""
import sys
import tempfile
from pathlib import Path

import broker.guardrail as gr
from broker import ManagedBroker, KillSwitch, Side
from broker.base import Position
from tests_managed import FakeBroker, _sleeve
import panic_exit

PASS, FAIL = [], []
TS = "2026-06-22T23:00:00"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _ks(ns):
    return KillSwitch(today="2026-06-22", namespace=ns)


def _sold(fb):
    return [(r.side.value, r.symbol, r.qty) for r in fb.placed]


def test_flatten_all_managed():
    print("[FLATTEN] 봇 관리분 전량청산, 보호종목 불가침")
    fb = FakeBroker(positions=[Position("CONL", 800, 6.8), Position("GOOGL", 102, 300),
                               Position("NVDA", 3, 200)],
                    cash=100, prices={"CONL": 7, "GOOGL": 310, "NVDA": 200})
    mb = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 2, "NVDA": 3}))   # GOOGL co-mingle(실102, basis2)
    res = panic_exit._panic(mb, _ks("p1"), TS, confirm=True, market_closed=False, force_open=False)
    sold = _sold(fb)
    check("status ok", res["status"] == "ok", res)
    check("CONL(보호) 매도 0건", all(s != "CONL" for _, s, _ in sold), sold)
    check("GOOGL 매도 = basis 2 (실보유102 아님)", ("SELL", "GOOGL", 2) in sold, sold)
    check("NVDA 매도 = basis 3", ("SELL", "NVDA", 3) in sold, sold)
    check("매수 0건", all(side != "BUY" for side, _, _ in sold), sold)
    check("청산 후 managed basis 비움", mb.managed == {}, mb.managed)


def test_bypasses_halt():
    print("[HALT우회] 이미 정지 상태에서도 청산 SELL 뚫림")
    fb = FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2}))
    ks = _ks("p2")
    ks.trip("daily_loss 사전정지", "daily_loss")
    check("사전 정지 확인", ks.is_halted()[0] is True, ks.is_halted())
    res = panic_exit._panic(mb, ks, TS, confirm=True, market_closed=False, force_open=False)
    check("정지상태에도 status ok", res["status"] == "ok", res)
    check("GOOGL 청산 체결", ("SELL", "GOOGL", 2) in _sold(fb), _sold(fb))


def test_dry_run_no_orders_no_trip():
    print("[DRYRUN] 미리보기 — 주문 0건, 정지 안 함")
    fb = FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2}))
    ks = _ks("p3")
    res = panic_exit._panic(mb, ks, TS, confirm=False, market_closed=False, force_open=False)
    check("status dry_run", res["status"] == "dry_run", res)
    check("주문 0건", fb.placed == [], fb.placed)
    check("정지 안 함", ks.is_halted()[0] is False, ks.is_halted())
    check("plan 에 GOOGL 2 노출", ("GOOGL", 2) in res["plan"], res["plan"])


def test_market_closed_trips_only():
    print("[CLOSED] 정규장 외 — 청산 보류, 신규거래만 정지")
    fb = FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2}))
    ks = _ks("p4")
    res = panic_exit._panic(mb, ks, TS, confirm=True, market_closed=True, force_open=False)
    check("status closed", res["status"] == "closed", res)
    check("주문 0건(MARKET 체결 불가)", fb.placed == [], fb.placed)
    check("신규거래 정지함", ks.is_halted()[0] is True, ks.is_halted())


def test_force_open_sells_when_closed():
    print("[FORCE] 정규장 외라도 --force-open 이면 청산 시도")
    fb = FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2}))
    res = panic_exit._panic(mb, _ks("p5"), TS, confirm=True, market_closed=True, force_open=True)
    check("status ok", res["status"] == "ok", res)
    check("GOOGL 청산 시도", ("SELL", "GOOGL", 2) in _sold(fb), _sold(fb))


def test_no_positions_still_trips():
    print("[EMPTY] 청산 대상 없어도 신규거래 정지(폭주 차단)")
    fb = FakeBroker(positions=[], prices={})
    mb = ManagedBroker(fb, _sleeve([], {}))
    ks = _ks("p6")
    res = panic_exit._panic(mb, ks, TS, confirm=True, market_closed=False, force_open=False)
    check("status no_positions", res["status"] == "no_positions", res)
    check("주문 0건", fb.placed == [], fb.placed)
    check("그래도 신규거래 정지함", ks.is_halted()[0] is True, ks.is_halted())


def test_protected_only_never_sold():
    print("[보호전용] 보유가 보호종목뿐이면 아무것도 안 팖")
    fb = FakeBroker(positions=[Position("CONL", 800, 6.8)], prices={"CONL": 7})
    mb = ManagedBroker(fb, _sleeve(["CONL"], {}))      # CONL 보호, managed 없음
    res = panic_exit._panic(mb, _ks("p7"), TS, confirm=True, market_closed=False, force_open=False)
    check("status no_positions(보호분은 대상 아님)", res["status"] == "no_positions", res)
    check("CONL 매도 0건", fb.placed == [], fb.placed)


class _PartialBroker(FakeBroker):
    """일부 종목 SELL 이 거부(REJECTED)로 남는 브로커 — panic 잔존(residual) 테스트용."""
    def __init__(self, fail_syms=(), **kw):
        super().__init__(**kw)
        self._fail = set(fail_syms)

    def place_order(self, req):
        from broker.base import Order, OrderStatus
        self.placed.append(req)
        if req.symbol in self._fail:
            return Order(order_id=f"R{len(self.placed)}", request=req, status=OrderStatus.REJECTED, filled_qty=0.0)
        return Order(order_id=f"F{len(self.placed)}", request=req, status=OrderStatus.FILLED, filled_qty=req.qty)

    def get_order(self, oid):
        from broker.base import Order, OrderStatus
        return Order(order_id=oid, request=None, status=OrderStatus.REJECTED)


def test_panic_residual_surfaced():
    print("[RESIDUAL] 일부 미청산 → status=panic_incomplete + residual (무성 'ok' 차단)")
    probe = panic_exit._panic(
        ManagedBroker(FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310}),
                      _sleeve([], {"GOOGL": 2})),
        _ks("pr0"), TS, confirm=True, market_closed=False, force_open=False)
    if "residual" not in probe:
        check("panic residual 통보 구현", False, "미구현 — 일부 미청산도 ok 로 은폐")
        return
    fb = _PartialBroker(fail_syms=["NVDA"],
                        positions=[Position("GOOGL", 2, 300), Position("NVDA", 3, 200)],
                        prices={"GOOGL": 310, "NVDA": 200})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2, "NVDA": 3}))
    res = panic_exit._panic(mb, _ks("pr1"), TS, confirm=True, market_closed=False, force_open=False)
    check("일부 미청산 → status=panic_incomplete", res["status"] == "panic_incomplete", res["status"])
    check("잔존 NVDA residual 보고", ("NVDA", 3) in res.get("residual", []), res.get("residual"))
    check("청산된 GOOGL 은 residual 아님", all(s != "GOOGL" for s, _ in res.get("residual", [])), res.get("residual"))
    fb2 = FakeBroker(positions=[Position("GOOGL", 2, 300)], prices={"GOOGL": 310})
    mb2 = ManagedBroker(fb2, _sleeve([], {"GOOGL": 2}))
    res2 = panic_exit._panic(mb2, _ks("pr2"), TS, confirm=True, market_closed=False, force_open=False)
    check("전량 체결 → status ok", res2["status"] == "ok", res2["status"])


class _FakeTossForRun(_PartialBroker):
    """panic_exit.run() 이 만드는 TossBroker 대체(네트워크 0) — _PartialBroker 재사용에
    run() 진입 前 게이트가 요구하는 Toss 전용 속성(api_key/api_secret/market_open)만 얹는다."""
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.api_key, self.api_secret = "k", "s"

    def market_open(self, market):
        return True


def test_run_journal_failure_still_alerts_residual():
    print("[C3] run() 레벨: 저널 기록 실패해도 잔존노출 경보 발송 + status 정상판정(panic_incomplete) 유지")
    fb = _FakeTossForRun(fail_syms=["NVDA"],
                         positions=[Position("GOOGL", 2, 300), Position("NVDA", 3, 200)],
                         prices={"GOOGL": 310, "NVDA": 200})
    sleeve = _sleeve([], {"GOOGL": 2, "NVDA": 3})
    notified = []

    def _boom_journal(*a, **kw):
        raise OSError("저널 기록 실패(모사 — 디스크 가득참)")

    orig = (panic_exit.TossBroker, panic_exit.SLEEVE_PATH,
           panic_exit.append_jsonl_rotating, panic_exit.notify)
    panic_exit.TossBroker = lambda paper=False: fb
    panic_exit.SLEEVE_PATH = Path(sleeve)
    panic_exit.append_jsonl_rotating = _boom_journal
    panic_exit.notify = lambda msg, level, ts: notified.append((msg, level))
    try:
        res = panic_exit.run(confirm=True)
    finally:
        (panic_exit.TossBroker, panic_exit.SLEEVE_PATH,
         panic_exit.append_jsonl_rotating, panic_exit.notify) = orig

    check("저널 실패해도 status 정상판정(panic_incomplete)", res["status"] == "panic_incomplete", res)
    check("잔존 NVDA residual 보고 유지", ("NVDA", 3) in res.get("residual", []), res.get("residual"))
    check("저널 실패해도 잔존노출 경보 발송(무음화 안 됨)", len(notified) == 1, notified)
    msg = notified[0][0] if notified else ""
    check("경보 메시지에 잔존노출 문구 포함", "잔존 노출" in msg, msg)
    check("경보 메시지에 저널실패 표기 포함", "저널기록 실패" in msg, msg)


def main():
    print("=" * 70)
    print(" panic_exit(비상 전량청산) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    tmp = Path(tempfile.mkdtemp())
    orig = (gr.STATE_DIR, gr.STATE_FILE, gr.KILL_FILE, gr.LOCK_FILE)
    gr.STATE_DIR = tmp
    gr.STATE_FILE = tmp / "killswitch.json"
    gr.KILL_FILE = tmp / "HALT"
    gr.LOCK_FILE = tmp / "run.lock"
    try:
        tests = [test_flatten_all_managed, test_bypasses_halt, test_dry_run_no_orders_no_trip,
                 test_market_closed_trips_only, test_force_open_sells_when_closed,
                 test_no_positions_still_trips, test_protected_only_never_sold,
                 test_panic_residual_surfaced, test_run_journal_failure_still_alerts_residual]
        for t in tests:
            t(); print()
    finally:
        gr.STATE_DIR, gr.STATE_FILE, gr.KILL_FILE, gr.LOCK_FILE = orig
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
