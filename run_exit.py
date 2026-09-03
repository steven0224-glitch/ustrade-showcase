"""장중 청산 원샷 — cron 이 N분마다(미 정규장 중) 호출. 슬리브 보유분 청산룰 점검 → 트리거 시 매도.

진입(run_live, 데일리)과 별개의 리스크 오버레이. ManagedBroker 가 기존 보유분을 보호하므로
봇 매수분만 점검·청산한다. 실거래 전용(토스). 알림채널 필수. 미 정규장 닫혀있으면 skip.

기본(보수): 현재가<200MA 이탈 + 평균매입가 대비 -8% 손절. (--use-50ma / --ob-rsi 로 확장)

  python run_exit.py                    # 1회 점검(장중이면 청산)
  python run_exit.py --stop-pct 0.10    # 손절폭 변경
  python run_exit.py --force-open       # 장 게이트 무시(테스트용; 체결은 거부될 수 있음)

cron(DST 양 체제 커버 — EDT 22:30~05:00 / EST 23:30~06:00 KST 평일, 15분):  */15 22-23,0-6 * * 1-5
  (개장 전·마감 후 틱은 market_open 게이트가 자동 skip. 기존 23,0-4 는 겨울 EST 마지막 1h 미커버)
"""
import argparse
import os
import sys
from datetime import datetime, timedelta

import data
from calendar_util import last_completed_session, session_gap, is_regular_open
from paths import LOG_DIR, STATE_DIR, append_jsonl_rotating
from broker import (TossBroker, ManagedBroker, KillSwitch, GuardedBroker, HaltError,
                    OrderStatus, OrderRequest, Side, RunLock, LockBusy, load_sleeve)
from broker.base import fmt_qty
from live_engine import _await_fills, _dump_orders
from live_exit import check_exits, to_exit
from notify import notify, has_channel

SLEEVE_PATH = STATE_DIR / "toss_sleeve.json"


def _journal(rec: dict):
    append_jsonl_rotating(LOG_DIR / "exits.jsonl", rec)


def _settle_exits(orders, exits, syms) -> dict:
    """청산 주문 최종 상태 → 결과 dict. 트리거된 청산 중 미체결/거부/취소/부분이 하나라도 있으면
    status='exit_incomplete'(+unfilled) — 손절 미집행을 'ok' 로 은폐하지 않는다(C3 무성실패 차단).
    진입엔진(live_engine._run_once_locked)의 partial 처리와 대칭."""
    unfilled = [o for o in orders if o.status != OrderStatus.FILLED]
    res = {"exits": [d["symbol"] for d in exits],
           "orders": _dump_orders(orders), "checked": syms}
    if unfilled:
        res["status"] = "exit_incomplete"
        res["reason"] = "청산 일부/전부 미체결 — 손절 미집행"
        res["unfilled"] = [o.request.symbol for o in unfilled]
    else:
        res["status"] = "ok"
    return res


def _cleanup_orders(gbroker, orders, wait=30.0, poll=2.0):
    """제출된 청산주문 정리 — 체결대기 → 미체결 DAY주문 취소 → record_fills(basis 차감). 정상·halt·error 공통.
    미취소 DAY 매도가 늦게 체결되면 다음 cron 이 재매도(oversell) → 취소 필수. basis 미차감 시 다음 틱이
    실보유 초과분을 재청산(oversell) → basis 동기화 필수. GuardedBroker 는 cancel/record_fills 를 inner
    위임(place_order 만 가드) → halt 트립 후에도 정리가 동작한다."""
    if not orders:
        return orders
    orders = _await_fills(gbroker, orders, wait, poll)
    _term = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED)
    for o in orders:
        if o.status not in _term and o.order_id:
            try:
                if gbroker.cancel_order(o.order_id):
                    o.status = OrderStatus.CANCELLED
            except Exception:
                pass
    rec = getattr(gbroker, "record_fills", None)
    if rec is not None:
        try:
            rec(orders)
        except Exception:
            pass
    return orders


def run(cash_cap=None, force_open=False, stop_pct=0.08, use_50ma=False, ob_rsi=None) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    if not has_channel():
        notify("청산 거부 — 알림채널 미설정(무인 실거래 필수)", "error", ts)
        return {"status": "error", "reason": "알림채널 미설정"}
    toss = TossBroker(paper=False)
    if not (toss.api_key and toss.api_secret):
        notify("청산 거부 — TOSS_API_KEY/SECRET 미설정", "error", ts)
        return {"status": "error", "reason": "TOSS_API_KEY/SECRET 미설정"}
    if not SLEEVE_PATH.exists():
        notify("청산 거부 — 토스 슬리브 미설정(toss_setup.py 먼저)", "error", ts)
        return {"status": "error", "reason": "슬리브 미설정"}
    session = last_completed_session()
    today = (session or datetime.now().date()).isoformat()
    try:
        with RunLock():
            res = _run_locked(toss, today, ts, cash_cap, force_open, stop_pct, use_50ma, ob_rsi)
    except LockBusy as e:
        return {"status": "locked", "reason": str(e)}
    except Exception as e:
        notify(f"청산 크래시: {e}", "error", ts)
        _journal({"ts": ts, "status": "crash", "reason": str(e)})
        return {"status": "crash", "reason": str(e)}
    _journal({"ts": ts, **{k: res[k] for k in ("status", "reason", "exits", "unfilled", "orders", "checked")
                           if k in res}})
    return res


def _run_locked(toss, today, ts, cash_cap, force_open, stop_pct, use_50ma, ob_rsi) -> dict:
    toss.connect()
    try:
        is_open = force_open or toss.market_open("US")
    except Exception:
        # 토스 시장시간 API 일시 실패가 보호청산을 통째로 막지(crash) 않게 — 로컬 NYSE 캘린더 폴백.
        is_open = force_open or is_regular_open()
    if not is_open:
        return {"status": "closed", "reason": "미 정규장 시간 아님 — 청산 보류(MARKET 체결 불가)"}
    ks = KillSwitch(today=today, namespace="toss")   # 진입(run_live toss)과 같은 toss 스케일 state 공유, paper 와 분리
    ks.resume_if_new_day()   # 묵은 일일손실 정지는 새 날 자동해제(진입 경로와 대칭) — 안 하면 청산이 영구 차단됨
    # 손실성 정지(daily_loss/total_drawdown)는 보호 청산을 막지 않는다 — 손절이 손실한도의 목적.
    # 수동 HALT·데이터무결성·에러·바운드 정지만 청산도 fail-closed 차단. (GuardedBroker 가 SELL 허용 대칭)
    blocked, reason = ks.exit_blocked()
    if blocked:
        notify(f"청산 보류 — 거래정지(청산 차단형): {reason}", "halt", ts)
        return {"status": "halted", "reason": reason}

    managed = ManagedBroker(toss, str(SLEEVE_PATH), cash_cap=cash_cap)
    gbroker = GuardedBroker(managed, ks)
    managed.reconcile_basis()                      # 미확정 매수 흡수(중복 방지)
    positions = gbroker.get_positions()            # 봇 보유분만(보호분 제외)
    if not positions:
        return {"status": "no_positions", "reason": "관리 보유분 없음"}
    syms = [p.symbol for p in positions]

    # end_excl 을 today(ET 직전완료세션) 기준 +1일로 — 호스트로컬(KST) 날짜는 장중 cron 시 ET 와 한 날
    # 어긋나 end_excl 산출이 의도(ET 세션 정합)와 불일치. run_live.py 의 ET 세션 기준과 통일.
    end_excl = (datetime.fromisoformat(today).date() + timedelta(days=1)).isoformat()
    closes, live = {}, {}
    for s in syms:
        try:
            cl = data.load(s, "2022-01-01", end_excl)["Close"].dropna()
            # 진입과 동일한 신선도 정책 — stale 일봉(>3세션)으로 자동매도 방지. 초과 시 None→data_ok=False(수동확인).
            if len(cl) and session_gap(cl.index[-1], today) > 3:
                cl = None
            closes[s] = cl
        except Exception:
            closes[s] = None
        try:
            live[s] = gbroker.get_quote(s).last
        except Exception:
            live[s] = None

    decisions = check_exits(positions, closes, live, stop_pct=stop_pct,
                            use_50ma=use_50ma, ob_rsi=ob_rsi)
    manual = [d for d in decisions if not d["data_ok"]]
    if manual:
        notify("⚠️ 청산 데이터 부족 — 수동확인: " + ", ".join(d["symbol"] for d in manual), "warn", ts)
    exits = to_exit(decisions)
    if not exits:
        return {"status": "ok", "reason": "청산 트리거 없음", "checked": syms}

    # 청산 매도 (가드레일 경유). 슬리브가 SELL=managed 만 허용 → 보호분 절대 안 나감.
    orders = []
    try:
        for d in exits:
            orders.append(gbroker.place_order(OrderRequest(d["symbol"], Side.SELL, d["qty"])))
    except HaltError as e:
        # 루프 중간 트립 — 이미 제출된 주문을 정리(취소·basis 차감)하지 않으면 살아있는 DAY 매도 +
        # 다음 틱 재청산이 겹쳐 과매도. 정상경로와 동일 정리 후 반환.
        orders = _cleanup_orders(gbroker, orders)
        notify(f"🚨 청산 중단(가드 트립) — 손절 미집행 가능, 수동확인: {e}", "halt", ts)
        return {"status": "tripped", "reason": str(e), "orders": _dump_orders(orders)}
    except Exception as e:
        # run_exit(보호청산)은 공유 toss 에러윈도우에 기록하지 않는다 — 청산의 transient 오류가 누적돼
        # 스스로(또는 진입)를 fail-closed 정지시켜 '손절 불능'을 만드는 것 차단. 오류는 notify 로만 경보.
        # 체계적 토스 장애는 run_live(진입) 경로가 자기 윈도우로 누적·정지. (청산은 계속 재시도.)
        orders = _cleanup_orders(gbroker, orders)   # 제출분 취소·basis 차감(재청산 oversell 창 닫기)
        notify(f"🚨 청산 오류 — 손절 미집행 가능, 수동확인: {e}", "error", ts)
        return {"status": "error", "reason": str(e), "orders": _dump_orders(orders)}

    orders = _cleanup_orders(gbroker, orders)   # 체결대기 → 미체결 DAY 취소 → basis 차감(정상경로)

    filled = [o for o in orders if o.status == OrderStatus.FILLED]
    if filled:
        reasons = " / ".join(f"{d['symbol']}{''.join(d['flags'])}" for d in exits)
        txt = ", ".join(f"{o.request.symbol} {fmt_qty(o.filled_qty)}" for o in filled)
        notify(f"[청산] {txt} | {reasons}", "warn", ts)
    res = _settle_exits(orders, exits, syms)
    # 트리거된 청산이 미체결/거부/취소/부분이면 손실 차단 실패 — ok 로 은폐 말고 경보 + 비-ok status.
    if res["status"] == "exit_incomplete":
        miss = ", ".join(f"{o.request.symbol}({o.status.value})"
                         for o in orders if o.status != OrderStatus.FILLED)
        notify(f"🚨 청산 미완료 — 손절 미집행, 수동확인 필수: {miss}", "error", ts)
    return res


def main():
    ap = argparse.ArgumentParser(description="장중 청산 원샷")
    ap.add_argument("--cash-cap", dest="cash_cap", type=float, default=None)
    ap.add_argument("--stop-pct", dest="stop_pct", type=float, default=0.08, help="손절폭(기본 0.08=8%%)")
    ap.add_argument("--use-50ma", dest="use_50ma", action="store_true", help="50MA 이탈도 청산")
    ap.add_argument("--ob-rsi", dest="ob_rsi", type=float, default=None, help="RSI 과열 청산 임계(예 80; 전량 청산)")
    ap.add_argument("--force-open", dest="force_open", action="store_true")
    a = ap.parse_args()
    cap = a.cash_cap
    if cap is None and os.environ.get("TOSS_MANAGED_CASH"):
        cap = float(os.environ["TOSS_MANAGED_CASH"])
    res = run(cash_cap=cap, force_open=a.force_open, stop_pct=a.stop_pct,
              use_50ma=a.use_50ma, ob_rsi=a.ob_rsi)
    print(f"status: {res['status']}" + (f" | {res.get('reason','')}" if res.get("reason") else ""))
    benign = {"ok", "no_positions", "closed", "locked"}
    return 0 if res["status"] in benign else (
        2 if res["status"] in {"halted", "tripped", "error", "crash", "exit_incomplete"} else 1)


if __name__ == "__main__":
    sys.exit(main())
