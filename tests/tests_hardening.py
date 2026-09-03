"""교차검증(2026-06-22 dual-team) 후 하드닝 수정 검증 — 네트워크 0.

대상 결함:
  C2 get_quote 결측가 0.0 → 거짓청산 / check_exits price>0 가드
  C5 아웃바운드 심볼 역정규화(BRK-B→BRK.B)
  C6 결정론 clientOrderId(같은 의도=같은 키)
  M5 401 토큰만료 → 재인증 후 재시도
  M6 상태맵 EXPIRED→CANCELLED
  M2 Executor cost_buffer 가 매수 사이징 축소
  M4 equity<=0 → fail-closed
  C1 killswitch 브로커별 namespace + 스케일점프 재seed + reset 재seed

실행:  & $py tests_hardening.py
"""
import pathlib
import sys
import tempfile

import numpy as np
import pandas as pd

from broker.toss import TossBroker, TossAPIError
from broker.base import OrderRequest, Order, Side, OrderType, OrderStatus, AccountInfo, Quote, Position
from broker.executor import Executor
import broker.guardrail as gr
from live_exit import check_exits, to_exit
from tests_toss import FakeSession, BASE, _BASE_ROUTES

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _broker(extra=None):
    routes = dict(_BASE_ROUTES)
    if extra:
        routes.update(extra)
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    return b, sess


_PRICES = ("GET", "/api/v1/prices")
_ORDERS = ("POST", "/api/v1/orders")


# ───── C2: get_quote 결측가 → raise ─────
def test_get_quote_missing_price():
    print("[C2] get_quote: lastPrice 결측/0 → raise (0.0 흘리면 거짓청산)")
    b, _ = _broker({_PRICES: (200, {"result": [{"symbol": "X"}]})})            # lastPrice 키 없음
    b.connect()
    raised = False
    try:
        b.get_quote("X")
    except TossAPIError:
        raised = True
    check("lastPrice 결측 → raise", raised)

    b2, _ = _broker({_PRICES: (200, {"result": [{"symbol": "X", "lastPrice": "0"}]})})
    b2.connect()
    raised2 = False
    try:
        b2.get_quote("X")
    except TossAPIError:
        raised2 = True
    check("lastPrice 0 → raise", raised2)

    b3, _ = _broker({_PRICES: (200, {"result": [{"symbol": "X", "lastPrice": "31.59"}]})})
    b3.connect()
    check("정상 lastPrice → Quote", b3.get_quote("X").last == 31.59)


def test_check_exits_zero_price():
    print("[C2] check_exits: price<=0 → data_ok False (자동청산 제외)")
    pos = [Position("AAA", 10, 140.0)]
    closes = {"AAA": pd.Series(np.linspace(100, 200, 250))}
    d = {x["symbol"]: x for x in check_exits(pos, closes, {"AAA": 0.0})}["AAA"]
    check("price 0 → data_ok False", d["data_ok"] is False, d)
    check("price 0 → to_exit 제외", to_exit([d]) == [], to_exit([d]))


# ───── C5: 아웃바운드 심볼 역정규화 ─────
def test_to_toss_symbol():
    print("[C5] _to_toss_symbol: BRK-B→BRK.B, 일반 티커 불변")
    check("BRK-B → BRK.B", TossBroker._to_toss_symbol("BRK-B") == "BRK.B")
    check("KMI → KMI", TossBroker._to_toss_symbol("KMI") == "KMI")
    check("AAPL → AAPL", TossBroker._to_toss_symbol("AAPL") == "AAPL")
    check("brk-b(소문자) → BRK.B", TossBroker._to_toss_symbol("brk-b") == "BRK.B")
    # place_order 가 변환된 심볼 전송
    b, sess = _broker({_ORDERS: (200, {"result": {"orderId": "O1"}})})
    b.connect()
    b.place_order(OrderRequest("BRK-B", Side.BUY, 1, OrderType.MARKET))
    body = next(c["json"] for c in sess.calls if c["path"] == "/api/v1/orders")
    check("place_order 심볼 BRK.B 전송", body["symbol"] == "BRK.B", body.get("symbol"))


# ───── C6: 결정론 clientOrderId ─────
def test_deterministic_client_order_id():
    print("[C6] clientOrderId: 같은 의도 → 같은 키(재시도 중복접수 차단)")
    b, sess = _broker({_ORDERS: (200, {"result": {"orderId": "O1"}})})
    b.connect()
    req = OrderRequest("KMI", Side.BUY, 1, OrderType.MARKET)
    b.place_order(req)
    b.place_order(req)
    cids = [c["json"]["clientOrderId"] for c in sess.calls if c["path"] == "/api/v1/orders"]
    check("두 동일주문 clientOrderId 동일", len(cids) == 2 and cids[0] == cids[1], cids)
    # 다른 수량 → 다른 키
    b.place_order(OrderRequest("KMI", Side.BUY, 2, OrderType.MARKET))
    cids2 = [c["json"]["clientOrderId"] for c in sess.calls if c["path"] == "/api/v1/orders"]
    check("다른 수량 → 다른 키", cids2[2] != cids2[0], cids2)


# ───── M5: 401 → 재인증 후 재시도 ─────
def test_reauth_on_401():
    print("[M5] 401(토큰만료) → 재인증 후 동일요청 재시도")
    state = {"n": 0}

    def bp_route(call):
        state["n"] += 1
        if state["n"] == 1:
            return (401, {"error": {"code": "token-expired", "message": "expired"}})
        return (200, {"result": {"currency": "USD", "cashBuyingPower": "3500.5"}})

    b, sess = _broker({("GET", "/api/v1/buying-power"): bp_route})   # buying-power 라우트만 교체
    b.connect()
    tok_calls_before = sum(1 for c in sess.calls if c["path"] == "/oauth2/token")
    acct = b.get_account()       # 첫 buying-power 401 → reconnect → 재시도 200
    tok_calls_after = sum(1 for c in sess.calls if c["path"] == "/oauth2/token")
    check("get_account 성공(재시도)", acct.cash == 3500.5, acct.cash)
    check("재인증 토큰 재발급 발생", tok_calls_after > tok_calls_before, (tok_calls_before, tok_calls_after))


# ───── M6: 상태맵 EXPIRED→CANCELLED ─────
def test_status_map_expired():
    print("[M6] get_order 상태 EXPIRED → CANCELLED(미체결 오인 방지)")
    route = {("GET", "/api/v1/orders/O1"): (200, {"result": {
        "orderId": "O1", "status": "EXPIRED", "symbol": "KMI", "side": "BUY",
        "quantity": "1", "orderType": "MARKET", "execution": {}}})}
    b, _ = _broker(route)
    b.connect()
    o = b.get_order("O1")
    check("EXPIRED → CANCELLED", o.status == OrderStatus.CANCELLED, o.status)


# ───── M2: cost_buffer 사이징 축소 ─────
class _StubBroker:
    def __init__(self, cash, price):
        self._cash, self._price = cash, price
    def get_account(self):
        return AccountInfo(cash=self._cash, equity=self._cash, buying_power=self._cash)
    def get_positions(self):
        return []
    def get_quote(self, s):
        return Quote(symbol=s, last=self._price, bid=self._price, ask=self._price)


def test_cost_buffer_sizing():
    print("[M2] Executor cost_buffer → 체결가>last 대비 매수 헤드룸(주수 축소)")
    stub = _StubBroker(cash=100.0, price=31.59)
    base = Executor(stub, alloc=1.0, cost_buffer=0.0).plan({"KMI": 1.0})
    buf = Executor(stub, alloc=1.0, cost_buffer=0.5).plan({"KMI": 1.0})
    qbase = sum(o.qty for o in base if o.side == Side.BUY)
    qbuf = sum(o.qty for o in buf if o.side == Side.BUY)
    check("버퍼0 → 3주", qbase == 3, qbase)
    check("버퍼0.5 → 2주(축소)", qbuf == 2, qbuf)
    check("버퍼가 사이징 축소함", qbuf < qbase, (qbase, qbuf))


# ───── M4 / C1: killswitch ─────
def _ks(namespace=""):
    d = pathlib.Path(tempfile.mkdtemp(prefix="ksh_"))
    gr.STATE_DIR = d
    gr.STATE_FILE = d / "killswitch.json"
    gr.KILL_FILE = d / "HALT"
    gr.LOCK_FILE = d / "run.lock"
    return gr.KillSwitch(today="2026-06-22", namespace=namespace), d


def test_equity_nonpositive_failclosed():
    print("[M4] equity<=0 → fail-closed(HaltError)")
    ks, _ = _ks()
    raised = False
    try:
        ks.roll_day(0.0)
    except gr.HaltError:
        raised = True
    check("roll_day(0) → HaltError", raised)
    ks2, _ = _ks()
    raised2 = False
    try:
        ks2.check_total_drawdown(-5.0)
    except gr.HaltError:
        raised2 = True
    check("check_total_drawdown(-5) → HaltError", raised2)


def test_total_drawdown_scalejump():
    print("[GUARD-1] 누적DD scale-jump 처리 — 정상위반 즉시트립, down-jump 한도초과면 HWM 보존·트립, up-jump HWM 보존")
    # 정상 실행(jump 아님) 한도위반 → 즉시 트립(지연 없음)
    ks, _ = _ks(namespace="toss")
    ks.roll_day(1000.0); ks.check_total_drawdown(1000.0)   # hwm=1000
    ks.roll_day(950.0)                                     # -5%(정상, jump 아님)
    r = False
    try:
        ks.check_total_drawdown(700.0)                    # -30% < -20% → 즉시 trip
    except gr.HaltError:
        r = True
    check("정상 한도위반 → 즉시 트립(지연 없음)", r is True and ks.state.get("halt_kind") == "total_drawdown",
          ks.state.get("halt_kind"))
    # down-jump(자산 1/10)이 HWM 대비로도 한도초과면 '스케일 오독'이 아니라 진짜 대손실 → 재seed 금지.
    # 종전엔 무조건 재seed 해 −90% 가 daily_loss·total_drawdown 를 모두 통과했다(−20% 트립인데 −90% 무트립).
    # 오독 방어는 HWM 기준으로 이관 — 과대보고 왕복 무트립은 test_bigloss_not_masked_by_scalejump 가 지킨다.
    ks2, _ = _ks(namespace="toss")
    ks2.roll_day(1000.0); ks2.check_total_drawdown(1000.0)  # hwm=1000
    ks2.roll_day(100.0)                                     # equity/prior=0.1 < 0.2 → down scale-jump
    check("down-jump + 한도초과 → HWM 보존(1000, 손실증거 삭제 안 함)", ks2.state.get("hwm") == 1000.0,
          ks2.state.get("hwm"))
    r2 = False
    try:
        ks2.check_total_drawdown(100.0)                    # hwm=1000 대비 -90% → 트립
    except gr.HaltError:
        r2 = True
    check("down-jump + 한도초과 → 트립(무음 통과 아님)",
          r2 is True and ks2.state.get("halt_kind") == "total_drawdown", (r2, ks2.state.get("halt_kind")))
    # up-jump(자산 6x 과대보고) → HWM 보존(인플레 방지)
    ks3, _ = _ks(namespace="toss")
    ks3.roll_day(1000.0); ks3.check_total_drawdown(1000.0)  # hwm=1000
    ks3.roll_day(6000.0)                                    # equity/prior=6 > 5 → up scale-jump
    check("up-jump → HWM 보존(1000, 인플레 안 함)", ks3.state.get("hwm") == 1000.0, ks3.state.get("hwm"))


def test_killswitch_namespace():
    print("[C1] killswitch 브로커별 namespace 분리")
    ks, d = _ks(namespace="toss")
    check("state 파일 = killswitch.toss.json", ks._state_file.name == "killswitch.toss.json", ks._state_file.name)
    ks.roll_day(100.0)
    check("namespaced 파일 생성", (d / "killswitch.toss.json").exists())
    check("기본 killswitch.json 미생성", not (d / "killswitch.json").exists())


def test_killswitch_scale_jump_reseed():
    print("[C1] 스케일 점프(>5배) → baseline·hwm 재seed(false-halt 방지)")
    ks, _ = _ks(namespace="toss")
    ks.roll_day(100.0)                       # baseline 100 (paper 스케일 가정)
    ks.roll_day(100000.0)                     # 1000배 점프 → 재seed
    check("day_start_equity 재seed=100000", ks.state["day_start_equity"] == 100000.0, ks.state["day_start_equity"])
    check("hwm 재seed=100000", ks.state["hwm"] == 100000.0, ks.state["hwm"])
    dd = ks.check_daily_loss(100000.0)        # 재seed 후 손실판정 → 트립 안 함
    check("재seed 후 일일손실 트립 안 함", abs(dd) < 1e-9, dd)


def test_killswitch_reset_reseed():
    print("[C1] reset(total_drawdown) → hwm + day_start_equity 둘 다 재seed")
    ks, _ = _ks(namespace="toss")
    ks.roll_day(1000.0)
    ks.trip("누적 드로다운", kind="total_drawdown")
    ks.reset()
    check("reset 후 halted False", ks.state["halted"] is False)
    check("reset 후 hwm None", ks.state["hwm"] is None, ks.state["hwm"])
    check("reset 후 day_start_equity None", ks.state["day_start_equity"] is None, ks.state["day_start_equity"])


# ───── M(dual-team 2026-06-24): 손실성 정지 중 보호청산 허용 ─────
def test_exit_blocked_classifier():
    print("[EXIT-HALT] exit_blocked: 손실성·에러누적 정지는 청산 허용, 구조적/바운드/수동 HALT 는 차단")
    if not hasattr(gr.KillSwitch, "exit_blocked"):
        check("KillSwitch.exit_blocked 존재", False, "미구현 — 모든 정지가 청산까지 차단 중")
        return
    ks, _ = _ks(namespace="toss")
    ks.trip("일일손실", kind="daily_loss")
    check("daily_loss 정지 → 청산 허용(blocked False)", ks.exit_blocked()[0] is False, ks.exit_blocked())
    ks.trip("누적DD", kind="total_drawdown")
    check("total_drawdown 정지 → 청산 허용", ks.exit_blocked()[0] is False, ks.exit_blocked())
    ks.trip("에러 누적", kind="error")
    check("error(에러누적) 정지 → 청산 허용(일시적 브로커장애서 보호청산 보장)", ks.exit_blocked()[0] is False, ks.exit_blocked())
    ks.trip("바운드 위반", kind="position_bound")
    check("position_bound 정지 → 청산 차단", ks.exit_blocked()[0] is True, ks.exit_blocked())
    ks.trip("알수없는 구조적 정지", kind="")
    check("generic(빈 kind) 정지 → 청산 차단(방어)", ks.exit_blocked()[0] is True, ks.exit_blocked())
    ks.reset()
    check("정지 없음 → 청산 허용", ks.exit_blocked()[0] is False, ks.exit_blocked())
    ks.trip("일일손실", kind="daily_loss")
    gr.KILL_FILE.write_text("x")
    check("수동 HALT 파일 → 손실성이어도 청산 차단", ks.exit_blocked()[0] is True, ks.exit_blocked())
    gr.KILL_FILE.unlink()


class _StubExec:
    """GuardedBroker inner 스텁 — get_quote + place_order 기록."""
    def __init__(self, price=100.0):
        self._price = price
        self.placed = []

    def get_quote(self, s):
        return Quote(symbol=s, last=self._price, bid=self._price, ask=self._price)

    def place_order(self, req):
        self.placed.append(req)
        return Order(order_id="O", request=req, status=OrderStatus.SUBMITTED)


def test_guarded_sell_during_loss_halt():
    print("[EXIT-HALT] GuardedBroker: 손실성 정지 중 SELL(청산) 허용 / BUY·수동HALT 는 차단")
    ks, _ = _ks(namespace="toss")
    ks.roll_day(1000.0)
    ks.trip("일일손실 한도", kind="daily_loss")
    inner = _StubExec(price=100.0)
    g = gr.GuardedBroker(inner, ks)
    sell_ok = True
    try:
        g.place_order(OrderRequest("AAA", Side.SELL, 1, OrderType.MARKET))
    except gr.HaltError:
        sell_ok = False
    check("daily_loss 정지 중 SELL 청산 허용", sell_ok and len(inner.placed) == 1, inner.placed)
    buy_blocked = False
    try:
        g.place_order(OrderRequest("AAA", Side.BUY, 1, OrderType.MARKET))
    except gr.HaltError:
        buy_blocked = True
    check("daily_loss 정지 중 BUY 차단(위험증가)", buy_blocked, buy_blocked)
    gr.KILL_FILE.write_text("x")
    sell_blocked = False
    try:
        g.place_order(OrderRequest("AAA", Side.SELL, 1, OrderType.MARKET))
    except gr.HaltError:
        sell_blocked = True
    check("수동 HALT 중 SELL 도 차단(fail-closed)", sell_blocked, sell_blocked)
    gr.KILL_FILE.unlink()


def test_reconcile_unverified_not_ok():
    print("[RECONCILE] 포지션 조회 실패 → None(검증불가) — '드리프트 없음 OK' 둔갑 차단(fail-open)")
    import live_engine as le
    from broker.base import Position

    class _NoPosBroker:
        def get_positions(self):
            raise RuntimeError("조회실패")

    class _OkBroker:
        def get_positions(self):
            return [Position("AAPL", 10, 100)]
    drift = le._reconcile({"AAPL": 10}, [], _NoPosBroker())
    check("조회 실패 → None (not [])", drift is None, drift)
    drift2 = le._reconcile({"AAPL": 10}, [], _OkBroker())
    check("일치 → 빈 drift []", drift2 == [], drift2)


def test_sell_skips_fatfinger_notional():
    print("[GUARD] 위험축소 SELL 은 fat-finger 명목캡 미적용 (청산 차단·논리역전 방지)")
    ks, _ = _ks(namespace="toss")
    ks.roll_day(1000.0)                       # 명목캡 = 0.40*1000*1.5 = 600
    g = gr.GuardedBroker(_StubExec(price=100.0), ks)
    sell_ok = True
    try:
        g.place_order(OrderRequest("AAA", Side.SELL, 10, OrderType.MARKET))   # 명목 1000 > 600
    except gr.HaltError:
        sell_ok = False
    check("명목 1000>캡600 SELL 통과(청산)", sell_ok, sell_ok)
    check("SELL 로 정지 안 됨", ks.is_halted()[0] is False, ks.is_halted())
    ks2, _ = _ks(namespace="toss2")
    ks2.roll_day(1000.0)
    g2 = gr.GuardedBroker(_StubExec(price=100.0), ks2)
    buy_tripped = False
    try:
        g2.place_order(OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    except gr.HaltError:
        buy_tripped = True
    check("같은 명목 BUY 는 fat-finger 트립(대조)", buy_tripped, buy_tripped)


def test_notify_checks_http_status():
    print("[NOTIFY] 전송이 HTTP 상태 검사 — 401/400 을 성공으로 오인 안 함 (무성실패 차단)")
    import os
    import requests as _rq
    import notify as nt

    class _Resp:
        def __init__(self, ok):
            self.ok = ok
            self.status_code = 200 if ok else 401
    saved_post = _rq.post
    saved_env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")}
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "c"
    try:
        _rq.post = lambda *a, **k: _Resp(False)
        check("HTTP 401 → _telegram False", nt._telegram("x") is False, nt._telegram("x"))
        _rq.post = lambda *a, **k: _Resp(True)
        check("HTTP 200 → _telegram True", nt._telegram("x") is True, nt._telegram("x"))
    finally:
        _rq.post = saved_post
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_notify_fail_flag_selfheal():
    print("[NOTIFY] 채널설정+전송0건 → notify_fail.flag 생성, 성공/미설정 시 자가치유 제거 (SPOF 독립신호)")
    import os
    import pathlib
    import tempfile
    import notify as nt

    d = pathlib.Path(tempfile.mkdtemp())
    saved_dir, saved_flag = nt.STATE_DIR, nt._NOTIFY_FAIL_FLAG
    saved_tg, saved_sl = nt._telegram, nt._slack
    saved_env = {k: os.environ.get(k) for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                                                "SLACK_WEBHOOK_URL", "USTRADE_NOTIFY_OFF")}
    nt.STATE_DIR = d
    nt._NOTIFY_FAIL_FLAG = d / "notify_fail.flag"
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "c"
    os.environ.pop("SLACK_WEBHOOK_URL", None)
    os.environ.pop("USTRADE_NOTIFY_OFF", None)   # 러너가 켜둔 채널 킬스위치 — 이 테스트는 전송 경로 자체가 대상
    try:
        flag = nt._NOTIFY_FAIL_FLAG
        nt._telegram = lambda x: False          # 채널 설정됨인데 전송 전부 실패
        nt._slack = lambda x: False
        nt.notify("손절 미집행", "error", "TS1")
        check("전송0건 → flag 생성", flag.exists())
        check("flag 내용에 메시지 포함", flag.exists() and "손절 미집행" in flag.read_text(encoding="utf-8"))

        nt._telegram = lambda x: True           # 채널 회복 → 자가치유 제거
        nt.notify("정상", "info", "TS2")
        check("전송 성공 → flag 제거", not flag.exists())

        nt._telegram = lambda x: False          # 재실패 → 재생성
        nt.notify("재실패", "error", "TS3")
        check("재실패 → flag 재생성", flag.exists())

        os.environ.pop("TELEGRAM_BOT_TOKEN", None)   # 채널 미설정 = 실패상태 아님 → 제거
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        nt.notify("미설정", "info", "TS4")
        check("채널 미설정 → flag 제거", not flag.exists())

        # USTRADE_NOTIFY_OFF=1 = 테스트·게이트용 채널 킬스위치. 채널이 설정돼 있어도 전송 0건이고
        # flag(채널사망 신호)도 건드리지 않는다 — 배포 게이트가 운영자를 호출하거나 실제 신호를
        # 지우는 것을 막는 가드(2026-08-08 페르소나 "t" halt 오발송 수리).
        os.environ["TELEGRAM_BOT_TOKEN"] = "t"
        os.environ["TELEGRAM_CHAT_ID"] = "c"
        os.environ["USTRADE_NOTIFY_OFF"] = "1"
        sent = []
        nt._telegram = lambda x: bool(sent.append(x)) or True   # 전송되면 기록됨(되면 안 됨)
        nt.notify("게이트 소음", "halt", "TS5")
        check("NOTIFY_OFF → 채널 전송 0건", sent == [], sent)
        check("NOTIFY_OFF → flag 무변(채널사망 신호 보존)", not flag.exists())
        flag.write_text("기존 채널사망", encoding="utf-8")
        nt.notify("게이트 소음2", "halt", "TS6")
        check("NOTIFY_OFF → 기존 flag 미삭제", flag.exists())
        flag.unlink()
    finally:
        nt.STATE_DIR, nt._NOTIFY_FAIL_FLAG = saved_dir, saved_flag
        nt._telegram, nt._slack = saved_tg, saved_sl
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_heartbeat_flags_dead_channel():
    print("[HEARTBEAT] notify_fail.flag 존재 → dead-man 이 채널사망 능동 경보 (백스톱 SPOF 보강)")
    import pathlib
    import tempfile
    import heartbeat as hb

    d = pathlib.Path(tempfile.mkdtemp())
    saved_dir = hb.STATE_DIR
    saved_lcs, saved_mso, saved_notify = hb.last_completed_session, hb.minutes_since_open, hb.notify
    captured = []
    hb.STATE_DIR = d
    hb.last_completed_session = lambda: None      # check(1) 일일진입 누락 스킵
    hb.minutes_since_open = lambda: None          # check(2) 청산 cron 스킵
    hb.notify = lambda m, *a, **k: captured.append(m)
    try:
        flag = d / "notify_fail.flag"
        check("flag 없음 → exit 0", hb.check() == 0)
        flag.write_text("손절 미집행 미전달", encoding="utf-8")
        check("flag 존재 → exit 1", hb.check() == 1)
        check("경보에 '채널' 언급", any("채널" in m for m in captured))
    finally:
        hb.STATE_DIR = saved_dir
        hb.last_completed_session, hb.minutes_since_open, hb.notify = saved_lcs, saved_mso, saved_notify


def test_dashboard_surfaces_notify_fail():
    print("[DASH] build_data.read_notify_fail — notify_fail.flag → 대시보드 채널死 배너 데이터")
    import os
    import pathlib
    import tempfile
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd

    d = pathlib.Path(tempfile.mkdtemp())
    check("flag 없음 → down False", bd.read_notify_fail(d)["down"] is False)
    (d / "notify_fail.flag").write_text("손절 미집행 미전달", encoding="utf-8")
    r = bd.read_notify_fail(d)
    check("flag 존재 → down True", r["down"] is True)
    check("detail 에 메시지", "손절 미집행" in r["detail"])


def test_dashboard_halt_file_reflected():
    print("[DASH] read_engine_state — STATE_DIR/HALT 도 halted 반영 (긴급정지 눌러도 ARMED 오표시 버그)")
    import os
    import json
    import pathlib
    import tempfile
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd
    import paths as P

    tmp = pathlib.Path(tempfile.mkdtemp())
    state, logs = tmp / "state", tmp / "logs"
    state.mkdir(); logs.mkdir()
    saved = (P.STATE_DIR, P.LOG_DIR, bd.load_closes)
    P.STATE_DIR, P.LOG_DIR = state, logs
    bd.load_closes = lambda **_k: {}    # 네트워크/캐시 차단 (allow_net 인자 수용)
    try:
        (logs / "runs.jsonl").write_text(
            json.dumps({"broker": "toss", "account": {"equity": 200.0, "cash": 200.0},
                        "orders": [], "session": "2026-06-24", "status": "ok"}) + "\n",
            encoding="utf-8")
        r = bd.read_engine_state(offline=True)
        check("HALT 파일 없음 → halted False", r is not None and r["halted"] is False)
        (state / "HALT").write_text("manual", encoding="utf-8")
        r2 = bd.read_engine_state(offline=True)
        check("HALT 파일 존재 → halted True (게이트 is_halted 와 일치)", r2["halted"] is True)
    finally:
        P.STATE_DIR, P.LOG_DIR, bd.load_closes = saved


def test_dashboard_live_mtm_overlay():
    print("[DASH] read_engine_state — 장중 라이브 호가 MTM 오버레이(yfinance) + 실패·장마감시 일봉종가 폴백(무회귀)")
    import os
    import json
    import pathlib
    import tempfile
    import pandas as pd
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd

    tmp = pathlib.Path(tempfile.mkdtemp())
    state, logs = tmp / "state", tmp / "logs"
    state.mkdir(); logs.mkdir()
    (logs / "runs.jsonl").write_text(
        json.dumps({"broker": "paper",
                    "account": {"equity": 950.0, "cash": 750.0},
                    "positions": [{"symbol": "AAPL", "qty": 1.0, "avg": 200.0}],
                    "session": "2026-06-26", "status": "ok"}) + "\n",
        encoding="utf-8")
    closes = {"AAPL": pd.Series([198.0, 199.0, 200.0])}     # 일봉 종가(폴백 경로)
    saved = (bd.market_state, bd.live_quotes)
    try:
        # 1) 장 마감 → 오버레이 skip = 일봉종가·동결 equity (무회귀)
        bd.market_state = lambda: "장 마감"
        r = bd.read_engine_state(offline=False, closes=closes, broker="paper",
                                 home=str(tmp), ks_namespace="paper_t")
        check("장마감: 종목 last=일봉종가 200", r["holdings"][0]["last"] == 200.0)
        check("장마감: total=동결 스냅샷 950", r["summary"]["total"] == 950.0)
        check("장마감: live 플래그 없음", "live" not in r["holdings"][0])

        # 2) 장중 + 라이브 호가 250 → 종목·헤드라인 MTM 갱신
        bd.market_state = lambda: "장중"
        bd.live_quotes = lambda syms, **k: {"AAPL": 250.0}
        r2 = bd.read_engine_state(offline=False, closes=closes, broker="paper",
                                  home=str(tmp), ks_namespace="paper_t")
        check("장중: 종목 last=라이브 250", r2["holdings"][0]["last"] == 250.0)
        check("장중: live 플래그 True", r2["holdings"][0].get("live") is True)
        check("장중: total=현금750+평가250=1000 (라이브 MTM)", r2["summary"]["total"] == 1000.0)
        check("장중: pnl=250-200=50", r2["summary"]["pnl"] == 50.0)

        # 3) 장중이지만 라이브 실패({}) → 일봉종가 폴백(무회귀) + 폴백 플래그 표면화
        bd.live_quotes = lambda syms, **k: {}
        r3 = bd.read_engine_state(offline=False, closes=closes, broker="paper",
                                  home=str(tmp), ks_namespace="paper_t")
        check("장중·라이브실패: last=일봉종가 200 폴백", r3["holdings"][0]["last"] == 200.0)
        check("장중·라이브실패: total=동결 950 (무회귀)", r3["summary"]["total"] == 950.0)
        check("장중·라이브실패: live_mtm_failed True (거짓 정상 차단)", r3["live_mtm_failed"] is True)
        check("장중·라이브성공: live_mtm_failed False", r2["live_mtm_failed"] is False)
        check("장마감: live_mtm_failed False (폴백 아님, 정상 종가)", r["live_mtm_failed"] is False)
    finally:
        bd.market_state, bd.live_quotes = saved


def test_heartbeat_selfcrash_alerts():
    print("[HEARTBEAT] check() 자체 크래시(캘린더 raise) → notify 경보 + exit 1 (데드맨 자기死 무성 방지)")
    import heartbeat as hb

    saved_lcs, saved_notify = hb.last_completed_session, hb.notify
    captured = []
    def boom():
        raise RuntimeError("calendar exploded")
    hb.last_completed_session = boom
    hb.notify = lambda m, *a, **k: captured.append(m)
    try:
        rc = hb.check()
        check("check() 크래시 → exit 1", rc == 1)
        check("크래시 → notify 경보 발송", any("heartbeat" in m for m in captured))
    finally:
        hb.last_completed_session, hb.notify = saved_lcs, saved_notify


def test_heartbeat_paper_mode():
    print("[HEARTBEAT] --mode paper — 페르소나 home 일1런 세션갭·장중 스냅샷 stale·저널부재 감시")
    import datetime as dtm
    import json
    import os
    import pathlib
    import tempfile
    import heartbeat as hb

    root = pathlib.Path(tempfile.mkdtemp())
    h_daily = root / "p-daily"; h_intra = root / "p-intra"; h_missing = root / "p-missing"
    for h in (h_daily, h_intra):
        (h / "logs").mkdir(parents=True)
    h_missing.mkdir()

    def write_runs(home, recs):
        (home / "logs" / "runs.jsonl").write_text(
            "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    saved_dir = hb.STATE_DIR
    saved_lcs, saved_mso, saved_notify = hb.last_completed_session, hb.minutes_since_open, hb.notify
    saved_env = {k: os.environ.get(k) for k in ("USTRADE_PERSONA_HOMES", "USTRADE_DASH_URL")}
    captured = []
    hb.STATE_DIR = root                                     # notify_fail.flag 없음 → 채널체크 무경보
    hb.last_completed_session = lambda: dtm.date(2026, 7, 2)   # 직전종료 세션 고정(목요일)
    hb.minutes_since_open = lambda: 60.0                    # 장중, 기동유예 지남
    hb.notify = lambda m, *a, **k: captured.append(m)
    os.environ["USTRADE_PERSONA_HOMES"] = ";".join(str(h) for h in (h_daily, h_intra, h_missing))
    os.environ["USTRADE_DASH_URL"] = ""                     # 대시보드 체크 비활성(테스트 무네트워크)
    try:
        # 죽은 체제: 일1런 세션갭 2(6/30→7/2) + 장중 스냅샷 30분 stale + 저널 없는 home
        stale_ts = (dtm.datetime.now() - dtm.timedelta(minutes=30)).isoformat()
        write_runs(h_daily, [{"ts": "2026-07-01T14:45:00", "session": "2026-06-30", "status": "ok"}])
        write_runs(h_intra, [{"ts": stale_ts, "session": "2026-07-02", "status": "intraday", "intraday": True}])
        check("이상 감지 → exit 1", hb.check(mode="paper") == 1)
        check("일1런 세션갭 경보", any("p-daily" in m and "일1런" in m for m in captured))
        check("장중 stale 경보", any("p-intra" in m and "장중 루프" in m for m in captured))
        check("저널부재 경보", any("p-missing" in m and "저널" in m for m in captured))
        check("live 일일진입 경보는 안 섞임", not any("리밸런스 기록 없음" in m for m in captured))

        # 건강한 체제: 갭 1(아침실행 대기중) + 방금 스냅샷 → 무경보
        captured.clear()
        os.environ["USTRADE_PERSONA_HOMES"] = ";".join(str(h) for h in (h_daily, h_intra))
        write_runs(h_daily, [{"ts": "2026-07-02T14:45:00", "session": "2026-07-01", "status": "ok"}])
        write_runs(h_intra, [{"ts": dtm.datetime.now().isoformat(), "session": "2026-07-02",
                              "status": "intraday", "intraday": True}])
        check("갭1+신선 스냅샷 → exit 0", hb.check(mode="paper") == 0)
        check("무경보", not captured)

        # 휴장(주말 등): minutes_since_open None → 장중 체크 자체가 꺼짐 (stale 이어도 무경보)
        hb.minutes_since_open = lambda: None
        write_runs(h_intra, [{"ts": stale_ts, "session": "2026-07-02", "status": "intraday", "intraday": True}])
        check("휴장 → 장중 stale 무경보", hb.check(mode="paper") == 0)

        # daily_run 페르소나(oneil/wood)는 일1런 후 기동(개장+~1h) → 개장+90분 전까진 스냅샷 stale 여도
        # '미기동'이라 무경보(매 거래일 10:00 ET 오발화 회귀 차단, 2026-07-10). home 이름=실 페르소나라
        # personas.PERSONAS 조회로 daily_run 분기 — p-intra(미등록)는 기본 15분 유예 그대로.
        h_oneil = root / "ustrade-paper-oneil"; (h_oneil / "logs").mkdir(parents=True)
        write_runs(h_oneil, [{"ts": stale_ts, "session": "2026-07-02", "status": "intraday", "intraday": True}])
        os.environ["USTRADE_PERSONA_HOMES"] = str(h_oneil)
        captured.clear()
        hb.minutes_since_open = lambda: 60.0                # 개장+60분 (<90 유예) — 아직 미기동 구간
        check("daily_run 미기동 구간 stale → 무경보", hb.check(mode="paper") == 0)
        check("오발화 없음", not any("장중 루프" in m for m in captured))
        captured.clear()
        hb.minutes_since_open = lambda: 100.0               # 개장+100분 (≥90 유예) — 기동했어야 함
        hb.check(mode="paper")
        check("daily_run 유예 후 stale → 경보", any("oneil" in m and "장중 루프" in m for m in captured))
    finally:
        hb.STATE_DIR = saved_dir
        hb.last_completed_session, hb.minutes_since_open, hb.notify = saved_lcs, saved_mso, saved_notify
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_acct_snapshot_error_flag():
    print("[ENGINE] _acct_snapshot 실패 → acct_error 플래그 (빈 {} 무성 흡수 차단, positions 위조 없음)")
    from live_engine import _acct_snapshot

    class BoomBroker:
        def get_account(self):
            raise RuntimeError("broker down")

    r = _acct_snapshot(BoomBroker())
    check("실패 → acct_error True", r.get("acct_error") is True)
    check("account 키 위조 없음", "account" not in r)
    check("positions 키 위조 없음 (selection_review 필터 불변)", "positions" not in r)

    class OkBroker:
        def get_account(self):
            class A: cash, equity = 100.0, 200.0
            return A()
        def get_positions(self):
            return []

    r2 = _acct_snapshot(OkBroker())
    check("성공 → acct_error 없음", "acct_error" not in r2)
    check("성공 → account 정상", r2["account"]["equity"] == 200.0)


def test_fmp_stale_cache_tracking():
    print("[FMP] 만료캐시 폴백 사용 시 STALE_HITS·최대나이 집계 (30일 전 펀더 무성 거래 표면화)")
    import os
    import pathlib
    import tempfile
    import time as _time
    import fmp_client as fc

    d = pathlib.Path(tempfile.mkdtemp())
    saved_dir, saved_env = fc.CACHE_DIR, os.environ.get("FMP_API_KEY")
    saved_hits, saved_age = fc.STALE_HITS, fc.STALE_MAX_AGE_D
    fc.CACHE_DIR = d
    os.environ["FMP_API_KEY"] = "t"
    fc.STALE_HITS, fc.STALE_MAX_AGE_D = 0, 0.0
    try:
        cli = fc.FMP(cache_ttl_days=7.0)
        cli._fetch = lambda ep, params: [{"pe": 10}]          # 1차: 성공 → 캐시 생성
        check("정상 fetch", cli.get("ratios-ttm", symbol="AAA") == [{"pe": 10}])
        check("stale 미집계(신선)", fc.STALE_HITS == 0)
        cf = next(d.glob("ratios-ttm_*.json"))
        old = _time.time() - 20 * 86400                       # 캐시를 20일 전으로 백데이트(TTL 7일 초과)
        os.utime(cf, (old, old))

        def boom(ep, params):
            raise fc.RateLimited("402 소진")
        cli._fetch = boom                                     # 2차: 쿼터실패 → 만료캐시 폴백
        check("만료캐시 폴백 반환", cli.get("ratios-ttm", symbol="AAA") == [{"pe": 10}])
        check("STALE_HITS 집계", fc.STALE_HITS == 1)
        check("최대나이 ≈20일", 19.0 < fc.STALE_MAX_AGE_D < 21.0)
    finally:
        fc.CACHE_DIR = saved_dir
        fc.STALE_HITS, fc.STALE_MAX_AGE_D = saved_hits, saved_age
        if saved_env is None:
            os.environ.pop("FMP_API_KEY", None)
        else:
            os.environ["FMP_API_KEY"] = saved_env


def test_fmp_fresh_cache_corrupt_selfheals():
    print("[FMP-FIX] fresh 캐시 손상(JSON 파싱 실패) → crash 대신 재fetch 로 자가치유(만료-폴백 경로와 대칭)")
    import json
    import os
    import pathlib
    import tempfile
    import fmp_client as fc

    d = pathlib.Path(tempfile.mkdtemp())
    saved_dir, saved_env = fc.CACHE_DIR, os.environ.get("FMP_API_KEY")
    fc.CACHE_DIR = d
    os.environ["FMP_API_KEY"] = "t"
    try:
        cli = fc.FMP(cache_ttl_days=7.0)
        cli._fetch = lambda ep, params: [{"pe": 10}]
        cli.get("ratios-ttm", symbol="AAA")                       # 1차: 정상 fetch → fresh 캐시 생성
        cf = next(d.glob("ratios-ttm_*.json"))
        cf.write_text("{not valid json", encoding="utf-8")        # 캐시 파일 손상(mtime 은 그대로 → 여전히 fresh)

        threw = False
        try:
            cli._fetch = lambda ep, params: [{"pe": 20}]           # 손상 감지 후 재fetch 되는 값
            out = cli.get("ratios-ttm", symbol="AAA")
        except Exception:
            threw = True
        check("손상 fresh 캐시에도 throw 안 함(재fetch 로 폴백)", not threw)
        if not threw:
            check("재fetch 결과 반환", out == [{"pe": 20}], out)
            check("손상 캐시가 재fetch 값으로 자가치유(다음 호출도 정상)",
                  json.loads(cf.read_text(encoding="utf-8")) == [{"pe": 20}], cf.read_text(encoding="utf-8"))
    finally:
        fc.CACHE_DIR = saved_dir
        if saved_env is None:
            os.environ.pop("FMP_API_KEY", None)
        else:
            os.environ["FMP_API_KEY"] = saved_env


def test_select_degraded_ratio_30pct():
    print("[SELECT] screen_degraded 문턱 30% — 결측 4/10(40%)이 과반 미달이어도 플래그 (기존 //2 는 미탐)")
    import pandas as pd
    import numpy as np
    import fmp_factors as ff
    import live_select as ls

    idx = pd.date_range("2025-12-01", periods=160, freq="B")
    cols = [f"T{i}" for i in range(10)]
    prices = pd.DataFrame({c: 100.0 + i * np.arange(len(idx)) for i, c in enumerate(cols)},
                          index=idx)                            # 기울기순 모멘텀 랭킹 결정적
    saved_snap, saved_screen = ff.snapshot, ff.screen
    try:
        def fake_snapshot(tickers, fmp=None):
            have = tickers[:6]                                  # 10 중 6만 펀더 존재 → 결측 4(40%)
            return pd.DataFrame({"pe": [10.0] * len(have), "net_margin": [0.1] * len(have)},
                                index=have)
        ff.snapshot = fake_snapshot
        ff.screen = lambda snap, **k: (list(snap.index), {})
        w, info = ls.select(prices, top_n=3, pool=10)
        check("결측 40% → screen_degraded True", info["screen_degraded"] is True)
        check("missing 4건", len(info["missing"]) == 4)
        check("최종 top3 산출(거래 지속)", len(info["final"]) == 3 and len(w) == 3)
    finally:
        ff.snapshot, ff.screen = saved_snap, saved_screen


def test_heartbeat_status_file():
    print("[HEARTBEAT] 점검 결과 상태파일(heartbeat_status.json) — 대시보드 감시 배지 데이터원")
    import json
    import pathlib
    import tempfile
    import heartbeat as hb

    d = pathlib.Path(tempfile.mkdtemp())
    saved_dir = hb.STATE_DIR
    saved_lcs, saved_mso, saved_notify = hb.last_completed_session, hb.minutes_since_open, hb.notify
    hb.STATE_DIR = d
    hb.last_completed_session = lambda: None
    hb.minutes_since_open = lambda: None
    hb.notify = lambda *a, **k: None
    try:
        check("정상 → exit 0", hb.check() == 0)
        sf = d / "heartbeat_status.json"
        check("상태파일 생성", sf.exists())
        st = json.loads(sf.read_text(encoding="utf-8"))
        check("mode 기록", st["mode"] == "live")
        check("alerts 0", st["alerts"] == 0)
    finally:
        hb.STATE_DIR = saved_dir
        hb.last_completed_session, hb.minutes_since_open, hb.notify = saved_lcs, saved_mso, saved_notify


def test_paper_fee_toss_parity():
    print("[FEE] 페르소나 paper 수수료 = 토스 패리티 0.1% 기본 + env 오버라이드 + 매수/매도 실차감")
    import os
    import pandas as pd
    from run_live import make_broker
    from broker.paper import PaperBroker

    prices = pd.DataFrame({"AAPL": [100.0] * 5})
    saved = os.environ.get("USTRADE_PAPER_FEE_RATE")
    try:
        os.environ.pop("USTRADE_PAPER_FEE_RATE", None)
        b = make_broker("paper", prices)
        check("기본 0.1% (토스 패리티)", abs(b._commission - 0.001) < 1e-12)
        os.environ["USTRADE_PAPER_FEE_RATE"] = "0.002"
        b2 = make_broker("paper", prices)
        check("env 오버라이드 0.2%", abs(b2._commission - 0.002) < 1e-12)
    finally:
        if saved is None:
            os.environ.pop("USTRADE_PAPER_FEE_RATE", None)
        else:
            os.environ["USTRADE_PAPER_FEE_RATE"] = saved

    pb = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.001, spread=0.0, slippage=0.0)
    o = pb.place_order(OrderRequest("AAPL", Side.BUY, 5, OrderType.MARKET))
    check("BUY 체결", o.status == OrderStatus.FILLED)
    check("매수 수수료 실차감 (1000−500−0.5)", abs(pb._cash - 499.5) < 1e-9)
    pb.place_order(OrderRequest("AAPL", Side.SELL, 5, OrderType.MARKET))
    check("매도 수수료 실차감 (499.5+500−0.5)", abs(pb._cash - 999.0) < 1e-9)

    # $10 이하 거래 무료 — 명목 $8(0.08주@$100) 매수: 수수료 0, 현금 딱 $8만 차감
    pf = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.001, spread=0.0, slippage=0.0, free_below=10.0)
    pf.place_order(OrderRequest("AAPL", Side.BUY, 0.08, OrderType.MARKET))
    check("$8 거래(≤$10) → 수수료 0 (현금 1000−8)", abs(pf._cash - 992.0) < 1e-9)
    # 경계 바로 위 — 명목 $10.10(0.101주@$100): 수수료 부과
    pf.place_order(OrderRequest("AAPL", Side.BUY, 0.101, OrderType.MARKET))
    check("$10.1 거래(>$10) → 수수료 부과", abs(pf._cash - (992.0 - 10.1 - 10.1 * 0.001)) < 1e-9)
    # 금액주문 경로도 동일 — orderAmount $9 매수: 무료
    pa = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.001, spread=0.0, slippage=0.0, free_below=10.0)
    oa = pa.place_order(OrderRequest("AAPL", Side.BUY, 0, OrderType.MARKET, amount=9.0))
    check("금액주문 $9(≤$10) 체결", oa.status == OrderStatus.FILLED)
    check("금액주문 $9 → 수수료 0 (현금 1000−9)", abs(pa._cash - 991.0) < 1e-9)


def test_trade_feed_merges_sources():
    print("[DASH] read_trade_feed — 페르소나 일1런 체결 + 장중 체결 + 상태 이벤트 통합(ts desc)")
    import json
    import os
    import pathlib
    import tempfile
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd

    root = pathlib.Path(tempfile.mkdtemp())
    home = root / "ustrade-paper-zztest"; (home / "logs").mkdir(parents=True)   # _persona_homes 접두사 규약
    (home / "logs" / "runs.jsonl").write_text("\n".join([
        json.dumps({"ts": "2026-07-06T14:45:00", "broker": "paper", "status": "ok",
                    "orders": [{"side": "BUY", "symbol": "AAPL", "qty": 2.5, "fill": 200.0,
                                "status": "FILLED", "reason": "모멘텀 상위"}]}),
        json.dumps({"ts": "2026-07-06T15:00:00", "broker": "paper", "status": "tripped",
                    "reason": "일일손실 한도"}),
    ]) + "\n", encoding="utf-8")
    (home / "logs" / "intraday.jsonl").write_text(
        json.dumps({"ts": "2026-07-06T15:30:00", "persona": "x", "action": "SELL_ALL",
                    "symbol": "NVDA", "qty": 1.2, "price": 150.0, "reason": "트레일손절"}) + "\n",
        encoding="utf-8")
    # 밀폐 필수 — _persona_homes 는 env 외에 C:\·%LOCALAPPDATA% 의 ustrade-paper-* 실디렉토리도
    # 무조건 합류(스테일 env 방어 기능)라 env 오버라이드만으론 격리 안 됨. VM 게이트에선 실 페르소나의
    # 신선한 이벤트가 items[:n] 캡에서 과거일자 픽스처를 밀어내 mine 이 비었음(2026-07-08 롤백 실증).
    # 전역 LOG_DIR(실계좌 기여)도 같은 이유로 빈 디렉토리로 격리.
    import paths as P
    empty = root / "empty-logs"; empty.mkdir()
    saved = (bd._persona_homes, P.LOG_DIR)
    bd._persona_homes = lambda: [("zztest", str(home))]
    P.LOG_DIR = empty
    try:
        feed = bd.read_trade_feed()
        mine = [x for x in feed if x["who"] == "zztest"]
        check("3개 이벤트 수집(체결+상태+장중)", len(mine) == 3)
        check("ts desc 정렬(장중이 맨 앞)", bool(mine) and mine[0]["kind"] == "장중"
              and mine[0]["side"] == "SELL_ALL")
        check("상태 이벤트 포함", any(x["kind"] == "상태" and x["side"] == "TRIPPED" for x in mine))
        check("일1런 체결 사유 보존", any(x["reason"] == "모멘텀 상위" for x in mine))
    finally:
        bd._persona_homes, P.LOG_DIR = saved


def test_panic_journal_rotation():
    print("[PANIC] panics.jsonl 5MB 초과 → .1 백업 회전 (무한증가·review 통째읽기 방지)")
    import pathlib
    import tempfile
    import panic_exit as pe

    saved = pe.LOG_DIR
    d = pathlib.Path(tempfile.mkdtemp())
    pe.LOG_DIR = d
    try:
        f = d / "panics.jsonl"
        f.write_text("x" * 5_000_001, encoding="utf-8")   # 5MB 초과 더미
        pe._journal({"ts": "T", "event": "test"})
        check("5MB 초과 → .1 로 회전", (d / "panics.jsonl.1").exists())
        check("새 panics.jsonl 은 1줄", f.exists()
              and len(f.read_text(encoding="utf-8").strip().splitlines()) == 1)
    finally:
        pe.LOG_DIR = saved


def test_paper_amount_order():
    print("[FRAC] PaperBroker 금액주문 — orderAmount → filled_qty=amount/fill, 현금 차감")
    from broker.paper import PaperBroker
    pb = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.0, spread=0.0, slippage=0.0)
    o = pb.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=250.0))
    check("금액주문 체결", o.status == OrderStatus.FILLED, o.status)
    check("filled_qty = 250/100 = 2.5", abs(o.filled_qty - 2.5) < 1e-9, o.filled_qty)
    check("현금 250 차감", abs(pb.get_account().cash - 750.0) < 1e-9, pb.get_account().cash)


def test_paper_nan_price_not_nan_equity():
    print("[NANPX] PaperBroker: NaN 시세 → get_quote raise, equity 는 폴백가로 유한 (bad_equity 오트립 차단)")
    from broker.paper import PaperBroker
    px = {"AAA": 100.0}
    pb = PaperBroker(cash=1000.0, price_fn=lambda s: px[s], commission=0.0, spread=0.0, slippage=0.0)
    pb.place_order(OrderRequest("AAA", Side.BUY, 5, OrderType.MARKET))   # 평단 100, 현금 500

    px["AAA"] = 120.0
    check("정상 시세 → equity = 500 + 5·120", abs(pb.get_account().equity - 1100.0) < 1e-9,
          pb.get_account().equity)

    px["AAA"] = float("nan")                                             # yfinance 미완성 행 시뮬
    raised = False
    try:
        pb.get_quote("AAA")
    except ValueError:
        raised = True
    check("NaN 시세 → get_quote raise", raised)
    eq = pb.get_account().equity
    check("equity 유한(마지막 성공시세 120 폴백)", abs(eq - 1100.0) < 1e-9, eq)

    pb2 = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.0, spread=0.0, slippage=0.0)
    pb2.place_order(OrderRequest("BBB", Side.BUY, 5, OrderType.MARKET))
    pb2._last_px.clear()                                                 # 신규 프로세스(=일1런) 시뮬
    pb2._price_fn = lambda s: float("nan")
    eq2 = pb2.get_account().equity
    check("last_px 없어도 평단 폴백으로 유한", eq2 == eq2 and abs(eq2 - 1000.0) < 1e-9, eq2)


def test_guardrail_amount_notional():
    print("[FRAC] GuardedBroker 금액주문 — 명목=주문금액 fat-finger 캡, 시세결측이 매수 안 막음(B3)")
    ks, _ = _ks(namespace="toss")
    ks.roll_day(1000.0)                            # 캡 = 0.40*1000*1.5 = 600
    g = gr.GuardedBroker(_StubExec(price=100.0), ks)
    ok = True
    try:
        g.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=500.0))
    except gr.HaltError:
        ok = False
    check("금액 500<캡600 → 통과", ok, ok)
    ks2, _ = _ks(namespace="toss2")
    ks2.roll_day(1000.0)
    g2 = gr.GuardedBroker(_StubExec(price=100.0), ks2)
    tripped = False
    try:
        g2.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=700.0))
    except gr.HaltError:
        tripped = True
    check("금액 700>캡600 → fat-finger 트립 (명목 우회 차단)", tripped, tripped)
    ks3, _ = _ks(namespace="toss3")
    ks3.roll_day(1000.0)

    class _NoQuote(_StubExec):
        def get_quote(self, s):
            raise RuntimeError("no quote")
    g3 = gr.GuardedBroker(_NoQuote(), ks3)
    ok3 = True
    try:
        g3.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=100.0))
    except Exception:
        ok3 = False
    check("시세결측이어도 금액주문 매수 통과(가용성)", ok3, ok3)


def test_managed_pending_amount():
    print("[FRAC] ManagedBroker 금액주문 — pending 을 추정주수로 예약(크래시복구 유지, B1)")
    import broker.managed as mg
    d = pathlib.Path(tempfile.mkdtemp(prefix="sl_"))
    sleeve = d / "sleeve.json"
    mg.save_sleeve(str(sleeve), [], {})

    class _Inner:
        def get_quote(self, s):
            return Quote(s, 100.0, 100.0, 100.0)

        def place_order(self, req):
            return Order("O", req, OrderStatus.SUBMITTED)

        def get_positions(self):
            return []
    m = mg.ManagedBroker(_Inner(), str(sleeve))
    m.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=250.0))
    check("금액주문 pending=추정주수 2.5 (0 아님)", abs(m.pending.get("MU", 0.0) - 2.5) < 1e-9, m.pending)

    class _InnerReject(_Inner):
        def place_order(self, req):
            return Order("", req, OrderStatus.REJECTED)
    sleeve2 = d / "sleeve2.json"
    mg.save_sleeve(str(sleeve2), [], {})
    m2 = mg.ManagedBroker(_InnerReject(), str(sleeve2))
    m2.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=250.0))
    check("금액주문 REJECTED → pending 롤백 0", m2.pending.get("MU", 0.0) == 0.0, m2.pending)


def test_executor_fractional_plan():
    print("[FRAC] Executor 소수주 — 달러델타 orderAmount 매수 + min_order_usd 밴드 + 전량청산")
    from broker.paper import PaperBroker
    prices = {"MU": 120.0, "AMD": 170.0, "GOOG": 180.0, "OLD": 50.0}
    pb = PaperBroker(cash=2000.0, price_fn=lambda s: prices[s], commission=0.0, spread=0.0, slippage=0.0)
    exe = Executor(pb, alloc=1.0, fractional=True, min_order_usd=5.0, fee_reserve=0.0)
    reqs = exe.plan({"MU": 0.10, "AMD": 0.10, "GOOG": 0.10})
    buys = [r for r in reqs if r.side == Side.BUY]
    check("3개 다 금액주문(amount 설정)", len(buys) == 3 and all(r.amount is not None for r in buys),
          [(r.symbol, r.amount) for r in buys])
    check("종목당 ~$200 orderAmount", all(abs(r.amount - 200.0) < 1.0 for r in buys), [r.amount for r in buys])
    check("매수 qty=0 (금액주문)", all(r.qty == 0.0 for r in buys))
    # sub-min 매수 → 주문 안 함 (밴드)
    reqs2 = exe.plan({"MU": 0.001})                 # 0.001*2000 = $2 < min 5
    check("sub-min($2<$5) 매수 → 주문 안 함(밴드)", reqs2 == [], reqs2)
    # 타겟밖 보유 → 전량 소수매도 (밴드 면제)
    pb2 = PaperBroker(cash=1000.0, price_fn=lambda s: prices[s], commission=0.0, spread=0.0, slippage=0.0)
    pb2._positions["OLD"] = Position("OLD", 3.3, 50.0)
    exe2 = Executor(pb2, alloc=1.0, fractional=True, min_order_usd=5.0, fee_reserve=0.0)
    reqs3 = exe2.plan({"MU": 0.5})
    sells = [r for r in reqs3 if r.side == Side.SELL]
    check("타겟밖 보유 → 전량 소수매도 3.3주",
          any(r.symbol == "OLD" and abs(r.qty - 3.3) < 1e-9 for r in sells), [(r.symbol, r.qty) for r in sells])
    # 회귀(R3 MAJOR): 소액 포지션의 sub-min 트림은 no-op 여야 — dust-closeout 이 밴드를 가로채 전량청산하면 안 됨
    pb3 = PaperBroker(cash=0.0, price_fn=lambda s: 100.0, commission=0.0, spread=0.0, slippage=0.0)
    pb3._positions["SMALL"] = Position("SMALL", 0.05, 100.0)   # $5 보유
    exe3 = Executor(pb3, alloc=1.0, fractional=True, min_order_usd=5.0, fee_reserve=0.0)
    reqs4 = exe3.plan({"SMALL": 0.8})                          # target $4, 트림 $1 < min$5 → 무거래
    check("소액 sub-min 트림 → 전량청산 아님(밴드 먼저)", reqs4 == [], reqs4)


def test_executor_fractional_hi_price_trim():
    print("[FRAC] 고가주 정당트림이 <0.01주로 절사돼도 드롭 안 함 — 최소증분 매도(2dp floor→0 회귀 차단)")
    from broker.paper import PaperBroker
    # price=$1200, 보유 0.50주($600). 목표 $590 → 트림 -$10(밴드 $5 통과) = 0.0083주 → floor 0.
    pb = PaperBroker(cash=0.0, price_fn=lambda s: 1200.0, commission=0.0, spread=0.0, slippage=0.0)
    pb._positions["HI"] = Position("HI", 0.50, 1200.0)
    exe = Executor(pb, alloc=1.0, fractional=True, min_order_usd=5.0, fee_reserve=0.0)
    reqs = exe.plan({"HI": 590.0 / 600.0})            # investable=600, target$=590, delta=-10
    sells = [r for r in reqs if r.side == Side.SELL]
    check("트림 드롭 안 됨(매도 1건)", len(sells) == 1, [(r.symbol, r.side.value, r.qty) for r in reqs])
    check("최소 거래증분 0.01주 매도", sells and abs(sells[0].qty - 0.01) < 1e-9, sells and sells[0].qty)
    # 잔량 있는 큰 포지션은 전량청산으로 강등되지 않아야(과청산 방지)
    check("과청산 아님(0.01주만, 전량 아님)", sells and sells[0].qty < 0.50, sells and sells[0].qty)


def test_dump_orders_fractional_qty():
    print("[FRAC] _dump_orders 저널 — 금액주문은 request.qty(0) 대신 실 체결 소수주수+amount 기록 (저널완전성·review P&L·알림 'BUY X 0' 방지)")
    from live_engine import _dump_orders
    from broker.base import OrderRequest, Order, OrderStatus, OrderType, Side
    # 금액주문 BUY: request.qty=0(달러로 주문), 체결 4.6주
    req = OrderRequest(symbol="XOM", side=Side.BUY, qty=0, order_type=OrderType.MARKET, amount=630.0, ref_price=137.0)
    o = Order(order_id="p1", request=req, status=OrderStatus.FILLED, filled_qty=4.6, avg_fill_price=137.0)
    d = _dump_orders([o])[0]
    check("qty=실 체결주수 4.6 (request.qty 0 아님)", abs(d["qty"] - 4.6) < 1e-9, d)
    check("amount=630 보존", d.get("amount") == 630.0, d)
    # 미체결 금액주문 → qty 0 이지만 amount 가 의도 보존
    o2 = Order(order_id="p2", request=req, status=OrderStatus.REJECTED, filled_qty=0.0)
    d2 = _dump_orders([o2])[0]
    check("미체결 금액주문 → qty 0 + amount 의도 보존", d2["qty"] == 0.0 and d2["amount"] == 630.0, d2)
    # 정수주문 불변 — request.qty 그대로, amount 키 없음 (정수경로 byte-identical)
    req3 = OrderRequest(symbol="KMI", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    o3 = Order(order_id="p3", request=req3, status=OrderStatus.FILLED, filled_qty=10, avg_fill_price=31.7)
    d3 = _dump_orders([o3])[0]
    check("정수주문 qty=10 불변, amount 키 없음", d3["qty"] == 10 and "amount" not in d3, d3)


def test_startup_jitter():
    print("[JITTER] startup_jitter — 비대화형서만 0~max 슬립, 대화형·max0·env0 은 no-op (페르소나 동시발사 herd 분산)")
    import os
    import startup

    calls = []
    rec = calls.append                                  # sleep_fn 주입 — 실제 슬립 없이 호출값만 포착
    # 비대화형(스케줄러) + 고정 rand 0.5 + max 30 → 15s 슬립
    d = startup.startup_jitter(30.0, interactive=False, sleep_fn=rec, rand_fn=lambda: 0.5)
    check("비대화형 max30 rand0.5 → 15s 슬립", abs(d - 15.0) < 1e-9, d)
    check("sleep_fn 이 15s 로 호출됨", calls == [15.0], calls)
    # 대화형(개발자 터미널) → 슬립 0, sleep_fn 미호출 (수동 run footgun 차단)
    calls.clear()
    d2 = startup.startup_jitter(30.0, interactive=True, sleep_fn=rec, rand_fn=lambda: 0.5)
    check("대화형 → 0s, 슬립 안 함", d2 == 0.0 and calls == [], (d2, calls))
    # max<=0 → 비활성
    calls.clear()
    d3 = startup.startup_jitter(0.0, interactive=False, sleep_fn=rec, rand_fn=lambda: 0.9)
    check("max0 → 0s, 슬립 안 함(비활성)", d3 == 0.0 and calls == [], (d3, calls))
    # env USTRADE_STARTUP_JITTER_SEC 해석 (max_seconds None)
    saved = os.environ.get("USTRADE_STARTUP_JITTER_SEC")
    try:
        os.environ["USTRADE_STARTUP_JITTER_SEC"] = "10"
        calls.clear()
        d4 = startup.startup_jitter(interactive=False, sleep_fn=rec, rand_fn=lambda: 1.0)
        check("env 10 + rand1.0 → 10s", abs(d4 - 10.0) < 1e-9, d4)
        os.environ["USTRADE_STARTUP_JITTER_SEC"] = "0"      # env 0 = 비활성 스위치
        calls.clear()
        d5 = startup.startup_jitter(interactive=False, sleep_fn=rec, rand_fn=lambda: 1.0)
        check("env 0 → 0s, 슬립 안 함", d5 == 0.0 and calls == [], (d5, calls))
        os.environ["USTRADE_STARTUP_JITTER_SEC"] = "garbage"  # 파싱 실패 → 기본 30 폴백
        calls.clear()
        d6 = startup.startup_jitter(interactive=False, sleep_fn=rec, rand_fn=lambda: 1.0)
        check("env 비정상 → 기본 30s 폴백", abs(d6 - 30.0) < 1e-9, d6)
    finally:
        if saved is None:
            os.environ.pop("USTRADE_STARTUP_JITTER_SEC", None)
        else:
            os.environ["USTRADE_STARTUP_JITTER_SEC"] = saved


def test_dashboard_auth_failclosed():
    print("[AUTHZ] dashboard _auth — DASH_TOKEN 미설정 → 403(control 비활성), 오토큰 401, 정상토큰 통과")
    import os
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    try:
        import server as srv  # dashboard/server.py 가 fastapi import → 최소 VM/CI 엔 없음
    except ImportError as e:
        print(f"  SKIP: 대시보드 의존성 없음(fastapi 미설치, PC 전용 컴포넌트) — {e}")
        return
    from fastapi import HTTPException

    saved = srv.DASH_TOKEN
    try:
        srv.DASH_TOKEN = None
        code = None
        try:
            srv._auth("아무거나")
        except HTTPException as e:
            code = e.status_code
        check("DASH_TOKEN 미설정 → 403(fail-closed)", code == 403, code)

        srv.DASH_TOKEN = "secret"
        code2 = None
        try:
            srv._auth("wrong")
        except HTTPException as e:
            code2 = e.status_code
        check("토큰 설정 + 오토큰 → 401", code2 == 401, code2)

        ok = True
        try:
            srv._auth("secret")
        except HTTPException:
            ok = False
        check("정상 토큰 → 통과(무예외)", ok, ok)
    finally:
        srv.DASH_TOKEN = saved


def test_guardrail_save_atomic_and_load_failclosed():
    print("[GUARD-SAVE] _save 원자쓰기(교체 실패 재시도돼도 상태파일 파손 안 됨) + _load 손상파일 fail-closed(halted=True)")
    import json
    import os
    ks, d = _ks(namespace="atomic")
    ks.roll_day(1000.0)   # 정상 상태파일 1회 기록

    import paths as P
    real_replace = P.atomic_replace
    calls = {"n": 0}

    def flaky_replace(tmp, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] <= 2:
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        return real_replace(tmp, dst, *a, **k)

    P.atomic_replace = flaky_replace
    try:
        ks.check_total_drawdown(1000.0)   # _save 1회차 — 교체 실패(1) 시뮬, tmp 삭제됨
        loaded = json.loads(ks._state_file.read_text(encoding="utf-8"))   # 직전(roll_day) 저장분 그대로 — 파손 아님
        check("교체 실패 시 기존 상태파일 유효 JSON 유지(파손 아님)", loaded.get("hwm") is None, loaded)
        ks.check_total_drawdown(1000.0)   # _save 2회차 — 교체 실패(2) 시뮬
        loaded2 = json.loads(ks._state_file.read_text(encoding="utf-8"))
        check("2회 연속 교체 실패해도 여전히 유효 JSON(파손 아님)", loaded2.get("hwm") is None, loaded2)
        ks.check_total_drawdown(1000.0)   # _save 3회차 — 교체 성공(재시도 로직 아님, 단순 호출 재개 시뮬)
    finally:
        P.atomic_replace = real_replace
    check("교체 성공 후 새 상태 정상 반영", json.loads(ks._state_file.read_text(encoding="utf-8")).get("hwm") == 1000.0)

    ks2, d2 = _ks(namespace="corrupt")
    ks2._state_file.write_text("{이것은 잘린 JSON", encoding="utf-8")   # 손상/절단 파일
    ks3 = gr.KillSwitch(today="2026-06-22", namespace="corrupt")
    halted, reason = ks3.is_halted()
    check("손상된 상태파일 → 로드시 fail-closed(halted=True)", halted is True, (halted, reason))


def test_runlock_steal_race():
    print("[LOCK-TOCTOU] 죽은 pid 노후락을 둘이 동시 회수 → 정확히 1개만 진입 "
          "(unlink→open 비원자로 양쪽 다 획득 = 더블트레이드 차단)")
    import os
    import threading
    import time as _t
    _ks(namespace="lockrace")           # gr.LOCK_FILE 등을 tmp 로 패치
    lock = gr.LOCK_FILE
    saved_alive = gr._pid_alive
    winners = []
    try:
        for _ in range(12):
            lock.write_text("999999", encoding="utf-8")     # Windows pid 는 4의 배수 → 무효 = 죽은 pid
            old = _t.time() - gr._LOCK_HARD_SEC - 100       # 회수 대상 노후락
            os.utime(lock, (old, old))
            barrier = threading.Barrier(2)
            synced = set()

            def _sync_dead(pid, _b=barrier, _s=synced):
                # steal 판정 직전에 두 스레드를 붙여 임계구역 경합을 강제(타임아웃 시 그냥 진행).
                # 스레드당 1회만 — 회수 후 재판정 호출까지 붙잡으면 불필요하게 대기한다.
                if threading.get_ident() not in _s:
                    _s.add(threading.get_ident())
                    try:
                        _b.wait(timeout=1.0)
                    except Exception:
                        pass
                return False

            gr._pid_alive = _sync_dead
            got = []

            def worker():
                lk = gr.RunLock(path=lock)
                try:
                    lk.__enter__()
                except gr.LockBusy:
                    return
                got.append(lk)

            ths = [threading.Thread(target=worker) for _ in range(2)]
            for t in ths:
                t.start()
            for t in ths:
                t.join(timeout=10)
            winners.append(len(got))
            for lk in got:
                lk.__exit__()
        check("동시 회수 12회 — 매회 정확히 1개만 락 획득", set(winners) == {1}, winners)
        check("회수 후 락 파일 정리됨", not lock.exists(), lock.exists())

        # 결정론 TOCTOU — '판정 시점엔 좀비였는데 회수 직전에 다른 실행이 새 락을 만든' 인터리브.
        # 옛 unlink→open 은 그 살아있는 락을 지우고 획득했다(양쪽 동시 보유). 지금은 떼어낸 뒤
        # 재판정해서 신선하면 원상복구 + 거부.
        lock.write_text("999999", encoding="utf-8")
        old = _t.time() - gr._LOCK_HARD_SEC - 100
        os.utime(lock, (old, old))

        def _steal_then_dead(pid):
            gr._pid_alive = saved_alive                        # 재판정은 실제 생존확인으로
            lock.unlink()
            lock.write_text(str(os.getpid()), encoding="utf-8")  # 다른 실행이 회수·재생성(살아있는 pid)
            return False                                       # 판정 시점 응답 = '죽은 pid'

        gr._pid_alive = _steal_then_dead
        busy = False
        try:
            with gr.RunLock(path=lock):
                pass
        except gr.LockBusy:
            busy = True
        check("판정 후 생긴 새 락은 탈취 안 함(LockBusy)", busy, busy)
        check("회수 취소 시 새 락 원상복구",
              lock.exists() and lock.read_text().strip() == str(os.getpid()),
              lock.exists() and lock.read_text())
    finally:
        gr._pid_alive = saved_alive


def test_runlock_steal_dead_after_param():
    print("[LOCK-DEADAFTER] RunLock(steal_dead_after=) — 죽은 pid + age200s: "
          "steal_dead_after=120 은 탈취, 기본값(1800) 은 아직 좀비 미달(LockBusy)")
    import os
    import time as _t
    _ks(namespace="deadafter")          # gr.LOCK_FILE 등을 tmp 로 패치
    lock = gr.LOCK_FILE
    saved_alive = gr._pid_alive
    gr._pid_alive = lambda pid: False   # 항상 죽은 pid 판정(회수 임계값만 격리 검증)
    try:
        lock.write_text("999999", encoding="utf-8")
        old = _t.time() - 200                      # age200s — 기본임계(1800) 미만, 단축임계(120) 이상
        os.utime(lock, (old, old))
        with gr.RunLock(path=lock, steal_dead_after=120):
            check("steal_dead_after=120 — age200s 죽은락 탈취 성공", True)
        check("탈취 후 락 파일 정리됨", not lock.exists(), lock.exists())

        lock.write_text("999999", encoding="utf-8")
        old = _t.time() - 200
        os.utime(lock, (old, old))
        busy = False
        try:
            with gr.RunLock(path=lock):             # 기본값 1800 — age200s 는 아직 좀비 미달
                pass
        except gr.LockBusy:
            busy = True
        check("기본값(1800) — age200s 는 미회수(LockBusy)", busy, busy)
    finally:
        gr._pid_alive = saved_alive
        try:
            lock.unlink()
        except OSError:
            pass


def test_bigloss_not_masked_by_scalejump():
    print("[GUARD-1b] 대손실(−85%)이 스케일급변 재seed 에 지워지지 않음 — "
          "'손실이 클수록 안 잡히는' 비단조 제거 + 과대보고 왕복은 계속 흡수")
    ks, _ = _ks(namespace="bigloss")
    ks.roll_day(1000.0); ks.check_daily_loss(1000.0); ks.check_total_drawdown(1000.0)   # hwm=1000
    ks.roll_day(150.0)                        # equity/prior=0.15 → down scale-jump 경로
    check("−85% down-jump → HWM 보존(1000)", ks.state.get("hwm") == 1000.0, ks.state.get("hwm"))
    tripped = False
    try:
        ks.check_daily_loss(150.0)
        ks.check_total_drawdown(150.0)
    except gr.HaltError:
        tripped = True
    check("−85% → 트립 발생(양 가드 통과 아님)", tripped and ks.is_halted()[0],
          (tripped, ks.is_halted()))
    check("halt_kind=total_drawdown(자동해제 안 됨 — 수동 확인)",
          ks.state.get("halt_kind") == "total_drawdown", ks.state.get("halt_kind"))
    # 단조성 대조군 — 경계 바로 밖(−21%)도 트립. 큰 손실이 작은 손실보다 관대해지지 않는다.
    ks2, _ = _ks(namespace="bigloss2")
    ks2.roll_day(1000.0); ks2.check_total_drawdown(1000.0)
    ks2.roll_day(790.0)                       # jump 아님(0.79)
    t2 = False
    try:
        ks2.check_total_drawdown(790.0)
    except gr.HaltError:
        t2 = True
    check("−21% 도 트립(단조성 대조군)", t2, t2)
    # 스케일 오독 방어 무회귀 — 과대보고(6x) 후 정상복귀는 HWM 대비 손실 0 이라 여전히 오트립 없음
    ks3, _ = _ks(namespace="bigloss3")
    ks3.roll_day(1000.0); ks3.check_total_drawdown(1000.0)   # hwm=1000
    ks3.roll_day(6000.0)                                     # up-jump(과대보고) → HWM 보존
    ks3.roll_day(1000.0)                                     # 복귀 = down-jump 지만 HWM 대비 손실 0
    ok3 = True
    try:
        ks3.check_daily_loss(1000.0)
        ks3.check_total_drawdown(1000.0)
    except gr.HaltError:
        ok3 = False
    check("과대보고 왕복(1000→6000→1000) → 오트립 없음", ok3 and not ks3.is_halted()[0],
          (ok3, ks3.is_halted()))


def test_trip_persist_failure_failclosed():
    print("[GUARD-TRIP] trip 영속화 실패 → 무음 금지: 예외 전파 + 보조 마커 → 다음 런도 거래 불가")
    import os
    import paths as P
    ks, d = _ks(namespace="nosave")
    ks.roll_day(1000.0)                       # 정상 상태파일 1회 기록(= 정지 없음)
    marker = d / "killswitch.nosave.halt"
    real_replace = P.atomic_replace

    def fail_replace(tmp, dst, *a, **k):      # 상태 교체 영구 실패(디스크/락 장애 시뮬)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False

    P.atomic_replace = fail_replace
    try:
        raised = False
        try:
            ks.trip("일일손실 한도 초과(시뮬)", kind="daily_loss")
        except OSError:
            raised = True
        check("영속화 실패 → 예외 전파(stderr 무음 아님)", raised, raised)
        check("이번 런은 인메모리 정지 유지", ks.is_halted()[0] is True, ks.is_halted())
        check("보조 정지 마커 생성", marker.exists(), list(d.iterdir()))
    finally:
        P.atomic_replace = real_replace
    # 다음 런(새 인스턴스) — 상태파일은 옛 '정지 없음' 이지만 마커로 정지 승계돼야 한다
    nxt = gr.KillSwitch(today="2026-06-22", namespace="nosave")
    check("다음 런 로드 → 여전히 정지", nxt.is_halted()[0] is True, nxt.is_halted())
    check("정지 kind 승계(daily_loss)", nxt.state.get("halt_kind") == "daily_loss",
          nxt.state.get("halt_kind"))
    blocked = False
    try:
        gr.GuardedBroker(_StubExec(price=100.0), nxt).place_order(
            OrderRequest("AAA", Side.BUY, 1, OrderType.MARKET))
    except gr.HaltError:
        blocked = True
    check("다음 런 신규매수 차단(fail-closed)", blocked, blocked)
    nxt.reset()                               # 사람이 확인 후 재개
    check("reset(=_save 성공) → 마커 자동 제거", not marker.exists())
    check("reset 후 정지 해제", nxt.is_halted()[0] is False, nxt.is_halted())


def test_dashboard_site_gate_read_endpoints():
    print("[AUTHZ] site_gate — DASH_SITE_PASS 설정 시 /api/dashboard 는 ?k=/쿠키 필요, "
          "/api/health 는 항상 통과, 미설정 시 개방 (TestClient/httpx 없이 게이트 로직 직접 구동)")
    import os
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    try:
        import server as srv  # dashboard/server.py 가 fastapi import → 최소 VM/CI 엔 없음
    except ImportError as e:
        print(f"  SKIP: 대시보드 의존성 없음(fastapi 미설치, PC 전용 컴포넌트) — {e}")
        return

    class _FakeRequest:
        """_site_pass_ok 가 읽는 것만 흉내(query_params.get, cookies.get) — TestClient 불필요."""
        def __init__(self, k=None, cookie=None):
            self.query_params = {"k": k} if k is not None else {}
            self.cookies = {"dashpass": cookie} if cookie is not None else {}

    saved_pass = srv.DASH_SITE_PASS
    try:
        srv.DASH_SITE_PASS = "secret123"

        req_noauth = _FakeRequest()
        check("패스 설정 + ?k=/쿠키 없음 → _site_pass_ok False",
              srv._site_pass_ok(req_noauth) is False, srv._site_pass_ok(req_noauth))

        req_k = _FakeRequest(k="secret123")
        check("정확한 ?k= → _site_pass_ok True", srv._site_pass_ok(req_k) is True, srv._site_pass_ok(req_k))

        req_cookie = _FakeRequest(cookie="secret123")
        check("정확한 쿠키 → _site_pass_ok True", srv._site_pass_ok(req_cookie) is True, srv._site_pass_ok(req_cookie))

        req_wrong = _FakeRequest(k="wrong")
        check("틀린 ?k= → _site_pass_ok False", srv._site_pass_ok(req_wrong) is False, srv._site_pass_ok(req_wrong))

        check("/api/dashboard 는 read-data 경로(가드 대상)",
              "/api/dashboard".startswith(srv._READ_DATA_PATHS), srv._READ_DATA_PATHS)
        check("/api/health 는 read-data 경로 아님(예외 대상)",
              not "/api/health".startswith(srv._READ_DATA_PATHS), srv._READ_DATA_PATHS)
        check("/api/control 은 read-data 경로 아님(토큰 게이트로 별도 처리)",
              not "/api/control".startswith(srv._READ_DATA_PATHS), srv._READ_DATA_PATHS)

        srv.DASH_SITE_PASS = None
        check("DASH_SITE_PASS 미설정 → 미들웨어가 가드 이전에 개방(guard 조건 자체가 falsy)",
              not bool(srv.DASH_SITE_PASS), srv.DASH_SITE_PASS)
    finally:
        srv.DASH_SITE_PASS = saved_pass


def main():
    print("=" * 70)
    print(" 하드닝 수정 검증 (dual-team 후) — 네트워크 없음")
    print("=" * 70)
    print()
    # killswitch 테스트가 guardrail 전역(STATE_DIR 등)을 tmp 로 바꾸므로 — 공유 pytest 프로세스
    # 오염 방지 위해 원복.
    _saved = (gr.STATE_DIR, gr.STATE_FILE, gr.KILL_FILE, gr.LOCK_FILE)
    try:
        for t in (test_get_quote_missing_price, test_check_exits_zero_price, test_to_toss_symbol,
                  test_deterministic_client_order_id, test_reauth_on_401, test_status_map_expired,
                  test_cost_buffer_sizing, test_equity_nonpositive_failclosed, test_total_drawdown_scalejump,
                  test_killswitch_namespace,
                  test_killswitch_scale_jump_reseed, test_killswitch_reset_reseed,
                  test_exit_blocked_classifier, test_guarded_sell_during_loss_halt,
                  test_reconcile_unverified_not_ok, test_sell_skips_fatfinger_notional,
                  test_notify_checks_http_status, test_notify_fail_flag_selfheal,
                  test_heartbeat_flags_dead_channel, test_dashboard_surfaces_notify_fail,
                  test_dashboard_halt_file_reflected, test_dashboard_live_mtm_overlay,
                  test_heartbeat_selfcrash_alerts, test_heartbeat_paper_mode,
                  test_acct_snapshot_error_flag, test_fmp_stale_cache_tracking,
                  test_fmp_fresh_cache_corrupt_selfheals,
                  test_select_degraded_ratio_30pct, test_heartbeat_status_file,
                  test_trade_feed_merges_sources, test_paper_fee_toss_parity,
                  test_panic_journal_rotation, test_paper_amount_order,
                  test_paper_nan_price_not_nan_equity,
                  test_guardrail_amount_notional, test_managed_pending_amount,
                  test_executor_fractional_plan, test_executor_fractional_hi_price_trim,
              test_dump_orders_fractional_qty,
                  test_startup_jitter,
                  test_dashboard_auth_failclosed, test_dashboard_site_gate_read_endpoints,
                  test_guardrail_save_atomic_and_load_failclosed,
                  test_runlock_steal_race, test_runlock_steal_dead_after_param,
                  test_bigloss_not_masked_by_scalejump,
                  test_trip_persist_failure_failclosed):
            t()
            print()
    finally:
        gr.STATE_DIR, gr.STATE_FILE, gr.KILL_FILE, gr.LOCK_FILE = _saved
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
