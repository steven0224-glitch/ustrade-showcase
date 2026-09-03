"""Stage 5 (무인 안전망) 검증 — 네트워크 불필요.

체결확인 폴링 / 사후 reconciliation / heartbeat dead-man.
실행:  & $py tests_stage5.py
"""
import json
import sys
import tempfile
from pathlib import Path

import pandas as pd

from broker.base import Order, OrderRequest, OrderStatus, Side, OrderType, Position
from tests_stage1 import _use_temp_state, _fake_select
from tests_stage4 import _Broker

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ───── 체결 확인 폴링 ─────
class _PollBroker:
    """place_order 는 PENDING 반환, get_order 폴링 N회 후 FILLED (비동기 체결 모사)."""
    def __init__(self, fills_after=2):
        self.fills_after, self.calls, self._o = fills_after, 0, None

    def place_order(self, req):
        self._o = Order(order_id="p1", request=req)
        self._o.status = OrderStatus.PENDING
        return self._o

    def get_order(self, oid):
        self.calls += 1
        if self.calls >= self.fills_after:
            self._o.status = OrderStatus.FILLED
            self._o.filled_qty = self._o.request.qty
            self._o.avg_fill_price = 100.0
        return self._o


def test_fill_polling():
    print("[Stage5] 체결확인 폴링 — PENDING → 폴링 → FILLED, 안 차면 미체결 유지")
    from live_engine import _await_fills
    req = OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET)

    b = _PollBroker(fills_after=2)
    o = b.place_order(req)
    _await_fills(b, [o], timeout=1.0, interval=0.01)
    check("PENDING → 폴링 후 FILLED", o.status == OrderStatus.FILLED, o.status)

    stuck = _PollBroker(fills_after=10_000)   # 절대 안 참
    o2 = stuck.place_order(req)
    _await_fills(stuck, [o2], timeout=0.05, interval=0.01)
    check("타임아웃까지 미체결이면 PENDING 유지(→상위서 partial)", o2.status == OrderStatus.PENDING, o2.status)


# ───── 사후 reconciliation ─────
def test_reconcile():
    print("[Stage5] 사후 정합성 — 기대 포지션 vs 브로커 실제 대조")
    from live_engine import _reconcile

    filled = Order(order_id="x", request=OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    filled.status = OrderStatus.FILLED
    filled.filled_qty = 10

    # 드리프트: 체결됐는데 브로커 포지션 비어있음 (desync)
    drift = _reconcile({}, [filled], _Broker(positions=[]))
    check("desync → 드리프트 감지", len(drift) == 1 and drift[0]["symbol"] == "AAA", drift)

    # 정합: 브로커 포지션이 기대치와 일치
    ok = _reconcile({}, [filled], _Broker(positions=[Position("AAA", 10, 100.0)]))
    check("일치 → 드리프트 없음", ok == [], ok)

    # run_once 통합: PaperBroker(포지션 실제 갱신) → reconcile.ok True
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once
    from broker import PaperBroker
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.34, "BBB": 0.33, "CCC": 0.33})
    try:
        cfg = RunConfig(vol_target=0.0, max_staleness_sessions=0)
        broker = PaperBroker(cash=1_000_000.0, price_fn=lambda s: 100.0, commission=0.0)
        res = run_once(None, broker, cfg, today="2026-06-01")
    finally:
        live_engine.select = orig
    check("run_once(ok)에 reconcile 포함", "reconcile" in res, list(res))
    check("PaperBroker 체결 → 정합 ok", res.get("reconcile", {}).get("ok") is True,
          res.get("reconcile"))


# ───── heartbeat dead-man ─────
def test_heartbeat():
    print("[Stage5] heartbeat — 직전 세션 미실행 감지 (cron 정지)")
    import heartbeat
    from datetime import date

    d = Path(tempfile.mkdtemp(prefix="hb_"))
    sess = date(2026, 5, 29)   # 금요일 정규장
    # 마감 시각 (실 NYSE 캘린더)
    close = heartbeat._NYSE.schedule(sess.isoformat(), sess.isoformat())["market_close"].iloc[-1]

    orig_last, orig_now, orig_log, orig_mso, orig_state = (
        heartbeat.last_completed_session, heartbeat.now_et, heartbeat.LOG_DIR,
        heartbeat.minutes_since_open, heartbeat.STATE_DIR)
    heartbeat.last_completed_session = lambda: sess
    heartbeat.LOG_DIR = d
    # 이 테스트는 check(1)=일일진입 누락만 검증. check(2)=장중청산 deadman 은 실시계
    # (minutes_since_open)에 의존해 장중에 돌리면 오발(저널 비어→경보) → None 으로 중립화.
    # check(3)=notify_fail.flag 도 실 STATE_DIR 의 잔존 flag 에 오염되지 않게 temp 로 격리.
    heartbeat.minutes_since_open = lambda: None
    heartbeat.STATE_DIR = d
    try:
        # 1) 유예 내 (마감 +1h) → 알림 안 함 (저널 비어도)
        heartbeat.now_et = lambda: (close + pd.Timedelta(hours=1)).to_pydatetime()
        check("마감 직후 유예 내 → 정상(0)", heartbeat.check(grace_hours=6) == 0)

        # 2) 유예 경과 + 저널에 해당 세션 실행 기록 있음 → 정상
        heartbeat.now_et = lambda: (close + pd.Timedelta(hours=8)).to_pydatetime()
        (d / "runs.jsonl").write_text(json.dumps({"session": "2026-05-29", "status": "ok"}) + "\n",
                                      encoding="utf-8")
        check("유예 경과 + 실행기록 있음 → 정상(0)", heartbeat.check(grace_hours=6) == 0)

        # 3) 유예 경과 + 기록 없음 → 미실행 알림(1)
        (d / "runs.jsonl").write_text(json.dumps({"session": "2026-05-22", "status": "ok"}) + "\n",
                                      encoding="utf-8")
        check("유예 경과 + 기록 없음 → 알림(1)", heartbeat.check(grace_hours=6) == 1)
    finally:
        (heartbeat.last_completed_session, heartbeat.now_et, heartbeat.LOG_DIR,
         heartbeat.minutes_since_open, heartbeat.STATE_DIR) = (
            orig_last, orig_now, orig_log, orig_mso, orig_state)


def main():
    print("=" * 70)
    print(" Stage 5 (무인 안전망) 검증 — 네트워크 없음")
    print("=" * 70)
    for t in (test_fill_polling, test_reconcile, test_heartbeat):
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
