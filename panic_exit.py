"""비상 전량청산 원샷 (panic flatten) — HALT 상태에서도 봇 관리분만 시장가 청산.

P1 (베스트프랙티스 갭분석 2026-06-22): 현재 구조는 state/HALT 가 켜지면 신규주문과
청산을 *동시에* 막아(run_exit.py 가 is_halted 에서 조기반환, GuardedBroker 가 SELL 도 거부),
폭주 버그 시 손실 포지션을 자동으로 비울 단일 명령이 없다. 이 스크립트가 그 데드락을 깬다.

안전 불변식:
  1. 청산 대상 = ManagedBroker.get_positions() 의 봇 관리분만 → 보호 11종목(기존 보유) 절대 불가침.
  2. SELL 만, BUY 절대 없음. (위험 축소 방향에 한정한 HALT 우회)
  3. HALT 우회는 GuardedBroker(가드 트립 시 SELL 거부)를 거치지 않고 ManagedBroker 에 *직접*
     SELL 제출해 달성. GuardedBroker(진입 가드)는 손대지 않는다.
  4. RunLock 으로 run_live/run_exit 와 동일 임계구역 → 동시매도 레이스 차단.
  5. 정규장 외에는 MARKET 미체결 → '신규거래 정지(trip) + 청산은 개장 후 재실행' 안내만.
  6. 청산 후 KillSwitch.trip(kind='manual') 로 신규진입 영구정지(자동해제 안 됨 → run_live --reset-halt 로만 재개).

기본은 **미리보기(dry-run)** — 실수 방지. 실제 청산은 --confirm 필요.

  python panic_exit.py                 # 미리보기: 무엇을 팔지만 출력(주문·정지 없음)
  python panic_exit.py --confirm       # 실제: 봇 관리분 전량 시장가 청산 + 신규거래 정지
  python panic_exit.py --confirm --force-open   # 정규장 외에도 청산 시도(체결은 거부될 수 있음)

청산 후 재개:  python run_live.py --reset-halt ...
"""
import argparse
import os
import sys
from datetime import datetime

from broker import (TossBroker, ManagedBroker, KillSwitch,
                    OrderStatus, OrderRequest, Side, RunLock, LockBusy)
from broker.base import fmt_qty
from live_engine import _await_fills, _dump_orders
from notify import notify
from calendar_util import last_completed_session, is_regular_open
from paths import LOG_DIR, STATE_DIR, append_jsonl_rotating

SLEEVE_PATH = STATE_DIR / "toss_sleeve.json"
_TERM = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED)


def _journal(rec: dict):
    append_jsonl_rotating(LOG_DIR / "panics.jsonl", rec)   # 5MB 초과 시 1개 백업으로 회전 (runs/exits 와 동일)


def _panic(managed, ks, ts, *, confirm: bool, market_closed: bool, force_open: bool) -> dict:
    """비상 청산 코어 — broker/ks 주입형(네트워크 없이 테스트 가능).

    HALT 우회: managed(ManagedBroker)에 직접 제출 → GuardedBroker 의 HALT 게이트를 거치지 않는다.
    슬리브 보호는 managed 가 그대로 강제(SELL=managed&¬protected) → 보호분 불가침 유지.
    """
    managed.reconcile_basis()                       # 미확정 매수 흡수(정확한 보유 기준 확보)
    plan = [(p.symbol, p.qty) for p in managed.get_positions() if p.qty > 1e-9]

    if not plan:                                    # 비울 게 없어도 신규거래는 정지(폭주 차단)
        if confirm:
            ks.trip("panic: 청산 대상 없음 — 신규거래 정지", "manual")
        return {"status": "no_positions", "plan": [], "tripped": confirm}

    if market_closed and not force_open:            # MARKET 체결 불가 → 정지만, 청산은 개장 후
        if confirm:
            ks.trip("panic: 정규장 외 — 신규거래 정지(청산은 개장 후 재실행)", "manual")
        return {"status": "closed", "plan": plan, "tripped": confirm,
                "reason": "미 정규장 아님 — MARKET 체결 불가. 신규거래 정지함; 개장 후 --confirm 재실행 또는 토스 앱 수동매도."}

    if not confirm:                                 # 미리보기 — 주문·정지 없음
        return {"status": "dry_run", "plan": plan,
                "note": ("미 정규장 외(체결은 개장 후)" if market_closed else "정규장 — 즉시 체결 가능")}

    # ── 실제 청산: 봇 관리분 전량 시장가 SELL (HALT 우회: managed 직접 제출) ──
    orders = []
    for sym, qty in plan:
        try:
            orders.append(managed.place_order(OrderRequest(sym, Side.SELL, qty)))
        except Exception as e:                      # 한 종목 실패가 나머지 청산을 막지 않게
            _journal({"ts": ts, "event": "place_error", "symbol": sym, "qty": qty, "err": str(e)})

    orders = _await_fills(managed, orders, 30.0, 2.0)
    # 잔존 미체결 청산주문 취소 — 늦은 DAY 체결이 재매도(oversell) 되는 것 차단.
    for o in orders:
        if o.status not in _TERM and o.order_id:
            try:
                if managed.cancel_order(o.order_id):
                    o.status = OrderStatus.CANCELLED
            except Exception:
                pass
    try:
        managed.record_fills(orders)                # basis 차감(청산분 반영)
    except Exception:
        pass

    # 신규진입 영구정지 (자동해제 안 되는 manual). 청산 후 호출 → 부분실패해도 정지는 건다.
    ks.trip("panic flatten — 신규거래 정지", "manual")

    filled = [o for o in orders if o.status == OrderStatus.FILLED]
    # 잔존 노출 — 계획 대비 체결 부족분(거부·미체결·부분·place_error). 비상청산이 일부만 됐는데
    # 'ok' 로 은폐하면 손실 포지션이 무방비로 남는다 → panic_incomplete + residual 로 표면화.
    filled_qty = {}
    for o in filled:
        filled_qty[o.request.symbol] = filled_qty.get(o.request.symbol, 0.0) + o.filled_qty
    residual = [(s, q) for s, q in plan if filled_qty.get(s, 0.0) + 1e-9 < q]
    return {"status": "panic_incomplete" if residual else "ok", "plan": plan, "tripped": True,
            "filled": [(o.request.symbol, o.filled_qty) for o in filled],
            "residual": residual, "orders": _dump_orders(orders)}


def run(confirm=False, force_open=False, cash_cap=None) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    toss = TossBroker(paper=False)
    if not (toss.api_key and toss.api_secret):
        return {"status": "error", "reason": "TOSS_API_KEY/SECRET 미설정"}
    if not SLEEVE_PATH.exists():
        return {"status": "error", "reason": "토스 슬리브 미설정(toss_setup.py 먼저)"}
    session = last_completed_session()
    today = (session or datetime.now().date()).isoformat()
    try:
        with RunLock():
            toss.connect()
            try:
                market_closed = not toss.market_open("US")
            except Exception:
                # 토스 시장시간 API 일시 실패가 비상청산을 통째로 막지 않게 — 로컬 NYSE 캘린더 폴백.
                market_closed = not is_regular_open()
            ks = KillSwitch(today=today, namespace="toss")
            managed = ManagedBroker(toss, str(SLEEVE_PATH), cash_cap=cash_cap)
            res = _panic(managed, ks, ts, confirm=confirm,
                         market_closed=market_closed, force_open=force_open)
    except LockBusy as e:
        return {"status": "locked", "reason": str(e)}
    except Exception as e:
        try:
            notify(f"🚨 panic 크래시: {e}", "error", ts)
        except Exception:
            pass
        _journal({"ts": ts, "status": "crash", "reason": str(e)})
        return {"status": "crash", "reason": str(e)}

    # 저널을 try 로 격리 — 실패해도 아래 결과 notify(특히 panic_incomplete 의 '잔존노출 수동매도
    # 필수' CRIT 경보)는 반드시 발송돼야 한다. 저널 실패 자체도 notify 문구에 병기.
    journal_err = None
    try:
        _journal({"ts": ts, **{k: res[k] for k in ("status", "reason", "plan", "filled", "residual", "tripped", "orders")
                               if k in res}})
    except Exception as e:
        journal_err = e
    # 알림(best-effort — panic 은 채널 없어도 동작)
    try:
        suffix = f" [저널기록 실패: {journal_err!r}]" if journal_err else ""
        if res["status"] == "ok":
            txt = ", ".join(f"{s} {fmt_qty(q)}" for s, q in res.get("filled", [])) or "(체결 없음)"
            notify(f"🚨[PANIC] 전량청산: {txt} | 신규거래 정지(재개=run_live --reset-halt){suffix}", "halt", ts)
        elif res["status"] == "panic_incomplete":
            resid = ", ".join(f"{s} {fmt_qty(q)}" for s, q in res.get("residual", []))
            txt = ", ".join(f"{s} {fmt_qty(q)}" for s, q in res.get("filled", [])) or "(체결 없음)"
            notify(f"🚨[PANIC] ⚠️ 일부만 청산({txt}) — 잔존 노출 수동매도 필수: {resid} | 신규거래 정지{suffix}", "error", ts)
        elif res["status"] == "closed":
            notify(f"🚨[PANIC] 정규장 외 — 신규거래 정지함. 청산은 개장 후 재실행/수동매도.{suffix}", "halt", ts)
    except Exception:
        pass
    return res


def main():
    ap = argparse.ArgumentParser(description="비상 전량청산 원샷 (기본=미리보기, --confirm 으로 실행)")
    ap.add_argument("--confirm", action="store_true", help="실제 청산 실행(없으면 미리보기만)")
    ap.add_argument("--force-open", dest="force_open", action="store_true",
                    help="정규장 외에도 청산 시도(체결 거부될 수 있음)")
    ap.add_argument("--cash-cap", dest="cash_cap", type=float, default=None)
    a = ap.parse_args()
    cap = a.cash_cap
    if cap is None and os.environ.get("TOSS_MANAGED_CASH"):
        cap = float(os.environ["TOSS_MANAGED_CASH"])
    res = run(confirm=a.confirm, force_open=a.force_open, cash_cap=cap)

    print(f"status: {res['status']}" + (f" | {res.get('reason','')}" if res.get("reason") else ""))
    if res.get("plan"):
        print("  대상(봇 관리분):", ", ".join(f"{s} {fmt_qty(q)}주" for s, q in res["plan"]))
    if res["status"] == "dry_run":
        print(f"  [미리보기] {res.get('note','')} — 실제 청산하려면 --confirm")
    if res.get("filled"):
        print("  청산 체결:", ", ".join(f"{s} {fmt_qty(q)}" for s, q in res["filled"]))
    if res.get("residual"):
        print("  ⚠️ 잔존 노출(미청산):", ", ".join(f"{s} {fmt_qty(q)}주" for s, q in res["residual"]), "— 수동매도 필요")
    if res.get("tripped"):
        print("  신규거래 정지됨(manual) — 재개: python run_live.py --reset-halt ...")
    benign = {"ok", "dry_run", "no_positions", "closed", "locked"}
    return 0 if res["status"] in benign else 1


if __name__ == "__main__":
    sys.exit(main())
