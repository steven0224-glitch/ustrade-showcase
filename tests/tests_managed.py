"""ManagedBroker(관리 슬리브 v3) 검증 — 네트워크 0.

핵심 불변식: 기존/보호 보유분 절대 매도·매수 안 됨 + 사용자 추가분(co-mingle) basis cap +
심볼 정규화(BRK.B↔BRK-B) + 크래시/늦은체결 basis 자가복구(pending+reconcile).
적대적 리뷰 7건(1차 4 + 2차 3) 수정 회귀.

실행:  & $py tests_managed.py
"""
import sys
import tempfile

from broker import Executor, ManagedBroker, load_sleeve, save_sleeve
from broker.base import (AccountInfo, Position, Quote, Order, OrderRequest,
                         Side, OrderType, OrderStatus)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


class FakeBroker:
    name = "fake"

    def __init__(self, positions=None, cash=1000.0, prices=None):
        self._positions = positions or []
        self._cash = cash
        self._prices = prices or {}
        self.placed = []

    def connect(self): pass
    def disconnect(self): pass

    def get_account(self):
        eq = self._cash + sum(p.qty * self._prices.get(p.symbol, p.avg_price) for p in self._positions)
        return AccountInfo(cash=self._cash, equity=eq, buying_power=self._cash)

    def get_positions(self):
        return list(self._positions)

    def get_quote(self, s):
        px = self._prices.get(s, self._prices.get(s.replace("-", "."), 0.0))
        return Quote(symbol=s, last=px, bid=px, ask=px)

    def place_order(self, req):
        self.placed.append(req)
        return Order(order_id=f"F{len(self.placed)}", request=req, status=OrderStatus.FILLED,
                     filled_qty=req.qty, avg_fill_price=self._prices.get(req.symbol, 0.0))

    def get_order(self, oid):
        return Order(order_id=oid, request=None, status=OrderStatus.FILLED)


def _sleeve(protected=(), managed=None, pending=None):
    d = tempfile.mkdtemp()
    path = d + "/toss_sleeve.json"
    save_sleeve(path, set(protected), dict(managed or {}), dict(pending or {}))
    return path


def _ord(sym, side, qty, status=OrderStatus.FILLED, filled=None):
    filled = qty if filled is None else filled
    return Order(order_id="O", request=OrderRequest(sym, side, qty, OrderType.MARKET),
                 status=status, filled_qty=filled, avg_fill_price=0.0)


# ───── 포지션 cap + canonical 심볼 ─────
def test_positions_cap_to_basis():
    print("[CAP] get_positions = min(실수량, basis), canonical 심볼")
    fb = FakeBroker(positions=[Position("CONL", 800, 6.8), Position("GOOGL", 102, 300)])
    mb = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 2}))
    pos = {p.symbol: p.qty for p in mb.get_positions()}
    check("GOOGL 수량 = basis 2 (실보유 102 아님)", pos.get("GOOGL") == 2, pos)
    check("CONL(보호) 숨김", "CONL" not in pos, pos)


def test_positions_canonical_symbol():
    print("[NORM] get_positions 가 canonical 심볼 노출 (finding1: Executor diff churn 방지)")
    fb = FakeBroker(positions=[Position("BRK.B", 10, 400)])     # 토스 표기 BRK.B
    mb = ManagedBroker(fb, _sleeve([], {"BRK-B": 10}))           # 슬리브 canonical BRK-B
    pos = [p.symbol for p in mb.get_positions()]
    check("노출 심볼 = BRK-B (canonical, 유니버스와 일치)", pos == ["BRK-B"], pos)


# ───── 슬리브 equity ─────
def test_account_sleeve_equity():
    print("[EQUITY] equity = cash + managed(cap) 평가액")
    fb = FakeBroker(positions=[Position("CONL", 800, 6.8), Position("GOOGL", 102, 300)],
                    cash=500.0, prices={"CONL": 7.0, "GOOGL": 310.0})
    mb = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 2}))
    check("equity = 500 + 2*310 = 1120", mb.get_account().equity == 1120.0, mb.get_account().equity)
    mb2 = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 2}), cash_cap=300.0)
    check("cash_cap=300 → equity 920", mb2.get_account().equity == 920.0, mb2.get_account().equity)


# ───── 주문 가드 ─────
def test_guards():
    print("[GUARD] SELL=managed&¬protected, BUY=¬protected")
    fb = FakeBroker(prices={"GOOGL": 310.0})
    mb = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 5}))
    check("SELL CONL(보호) → REJECTED",
          mb.place_order(OrderRequest("CONL", Side.SELL, 1, OrderType.MARKET)).status == OrderStatus.REJECTED)
    check("SELL VOO(비관리) → REJECTED",
          mb.place_order(OrderRequest("VOO", Side.SELL, 1, OrderType.MARKET)).status == OrderStatus.REJECTED)
    check("BUY CONL(보호) → REJECTED",
          mb.place_order(OrderRequest("CONL", Side.BUY, 1, OrderType.MARKET)).status == OrderStatus.REJECTED)
    check("SELL GOOGL(관리) → 통과",
          mb.place_order(OrderRequest("GOOGL", Side.SELL, 2, OrderType.MARKET)).status == OrderStatus.FILLED)
    check("보호 주문 브로커 미전달", [r.symbol for r in fb.placed] == ["GOOGL"], fb.placed)


def test_symbol_normalization_guard():
    print("[NORM] BRK.B/대소문자/공백 변형으로 보호 못 뚫음")
    path = _sleeve(["BRK.B"], {})
    check("저장 정규화 protected=BRK-B", load_sleeve(path)["protected"] == {"BRK-B"})
    mb = ManagedBroker(FakeBroker(), path)
    for v in ("BRK-B", "BRK.B", "brk.b", " BRK-B "):
        check(f"BUY '{v}' → REJECTED", mb.place_order(
            OrderRequest(v, Side.BUY, 1, OrderType.MARKET)).status == OrderStatus.REJECTED)


def test_load_disjoint():
    print("[DISJOINT] protected 권위적 — 겹치면 managed/pending 에서 제거")
    import json
    d = tempfile.mkdtemp(); path = d + "/toss_sleeve.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"protected": ["AMAT"], "managed": {"AMAT": 3, "GOOGL": 5},
                   "pending": {"AMAT": 1}}, f)
    s = load_sleeve(path)
    check("AMAT managed 제거", "AMAT" not in s["managed"], s["managed"])
    check("AMAT pending 제거", "AMAT" not in s["pending"], s["pending"])
    check("SELL AMAT → REJECTED(보호)", ManagedBroker(FakeBroker(), path).place_order(
        OrderRequest("AMAT", Side.SELL, 3, OrderType.MARKET)).status == OrderStatus.REJECTED)


# ───── pending + reconcile (finding 2·3: basis 내구성) ─────
def test_buy_persists_pending():
    print("[PENDING] BUY 는 제출 前 pending 영속(크래시 안전), basis 는 아직 0")
    path = _sleeve([], {})
    mb = ManagedBroker(FakeBroker(prices={"NVDA": 200}), path)
    mb.place_order(OrderRequest("NVDA", Side.BUY, 3, OrderType.MARKET))
    check("pending 에 NVDA=3 영속", load_sleeve(path)["pending"].get("NVDA") == 3, load_sleeve(path))
    check("managed 아직 비어있음", load_sleeve(path)["managed"] == {}, load_sleeve(path))


def test_record_fills_buy_then_terminal():
    print("[FILLS] BUY 체결 → basis += , 종결 시 pending 차감")
    path = _sleeve([], {}, {"NVDA": 3})
    mb = ManagedBroker(FakeBroker(), path)
    mb.record_fills([_ord("NVDA", Side.BUY, 3, OrderStatus.FILLED, 3)])
    check("basis NVDA=3", mb.managed.get("NVDA") == 3, mb.managed)
    check("pending 해소(비움)", mb.pending == {}, mb.pending)


def test_reconcile_recovers_crashed_buy():
    print("[RECON] 크래시/늦은체결: pending 을 실보유와 대조해 basis 흡수")
    # place_order 가 pending 영속했으나 record_fills 전에 크래시 → 다음 실행: 실보유 3 존재
    path = _sleeve([], {}, {"NVDA": 3})
    fb = FakeBroker(positions=[Position("NVDA", 3, 200)])
    mb = ManagedBroker(fb, path)
    mb.reconcile_basis()
    check("basis NVDA=3 복구", mb.managed.get("NVDA") == 3, mb.managed)
    check("pending 비움", mb.pending == {}, mb.pending)
    check("get_positions 가 NVDA 노출(중복매수 방지)",
          [p.symbol for p in mb.get_positions()] == ["NVDA"], mb.get_positions())


def test_reconcile_caps_at_real():
    print("[RECON] pending 과대(거부·부분) → 실보유로 cap (유령 포지션 없음)")
    # pending 5 인데 실제 3 만 체결 → basis 3
    mb = ManagedBroker(FakeBroker(positions=[Position("NVDA", 3, 200)]), _sleeve([], {}, {"NVDA": 5}))
    mb.reconcile_basis()
    check("basis = min(실보유3, 0+pending5) = 3", mb.managed.get("NVDA") == 3, mb.managed)
    # 완전 거부(실보유 0) → 유령 없음
    mb2 = ManagedBroker(FakeBroker(positions=[]), _sleeve([], {}, {"NVDA": 3}))
    mb2.reconcile_basis()
    check("거부 매수(실보유0) → basis 유령 없음", "NVDA" not in mb2.managed, mb2.managed)


def test_reconcile_preserves_comingle():
    print("[RECON] reconcile 가 co-mingle 보존 — basis 는 봇 의도 상한까지만")
    # 봇 basis 2 + pending 1 = 의도 3. 사용자가 100 추가 → 실보유 102. basis 는 3 이어야(102 아님).
    mb = ManagedBroker(FakeBroker(positions=[Position("GOOGL", 102, 300)]),
                       _sleeve([], {"GOOGL": 2}, {"GOOGL": 1}))
    mb.reconcile_basis()
    check("basis = min(102, 2+1) = 3 (사용자 100주 흡수 안 함)", mb.managed.get("GOOGL") == 3, mb.managed)


def test_amount_pending_drift_invariant():
    print("[RECON] 금액매수 pending 예약↔해소가 호가 드리프트에 불변 — 잔존 pending 이 co-mingle 흡수 안 함")
    fb = FakeBroker(positions=[Position("GOOGL", 100.0, 100.0)], prices={"GOOGL": 100.0})
    mb = ManagedBroker(fb, _sleeve())
    req = OrderRequest("GOOGL", Side.BUY, 0.0, OrderType.MARKET, amount=300.0)
    mb.place_order(req)                                   # 예약 @ $100 → pending=3.0, req._reserved_qty=3.0
    check("예약 pending=3.0(@$100)", abs(mb.pending.get("GOOGL", 0.0) - 3.0) < 1e-9, mb.pending)
    check("예약주수 stash", abs(getattr(req, "_reserved_qty", 0.0) - 3.0) < 1e-9, getattr(req, "_reserved_qty", None))
    fb._prices["GOOGL"] = 150.0                           # 체결~해소 사이 호가 상승(드리프트)
    fill = Order(order_id="F1", request=req, status=OrderStatus.FILLED, filled_qty=3.0, avg_fill_price=100.0)
    mb.record_fills([fill])                               # 해소는 _reserved(3.0) 로 — 구코드는 300/150=2.0 차감→pending 1.0 잔존
    check("종결 후 pending 완전 해소(드리프트에도 잔존 0)", mb.pending.get("GOOGL", 0.0) <= 1e-9, mb.pending)
    check("managed basis = 실매수 3.0", abs(mb.managed.get("GOOGL", 0.0) - 3.0) < 1e-9, mb.managed)
    fb._positions = [Position("GOOGL", 103.0, 100.0)]     # 사용자 co-mingle 100 + 봇 3 = 실보유 103
    mb.reconcile_basis()
    check("reconcile basis=3.0(co-mingle 100주 흡수 안 함)", abs(mb.managed.get("GOOGL", 0.0) - 3.0) < 1e-9, mb.managed)
    check("get_position qty=3.0(사용자분 노출 안 함)",
          mb.get_position("GOOGL") is not None and abs(mb.get_position("GOOGL").qty - 3.0) < 1e-9, mb.get_position("GOOGL"))


def test_pending_one_shot_no_comingle_absorb():
    print("[RECON] pending 은 one-shot — 실패매수 고아 pending 이 후일 사용자 co-mingle 을 basis 로 흡수 안 함")
    # 봇 매수 pending 6, 하지만 실제 미보유(거부·크래시 고아). reconcile 1회 후 pending 무조건 비워짐(누적 안 함).
    path = _sleeve([], {}, {"AAPL": 6})
    fb = FakeBroker(positions=[], prices={"AAPL": 100.0})   # 봇 미보유(매수 실패)
    mb = ManagedBroker(fb, path)
    mb.reconcile_basis()
    check("고아 pending → basis 0(유령 없음)", mb.managed.get("AAPL", 0.0) == 0.0, mb.managed)
    check("pending one-shot 비움(누적 안 함)", not mb.pending, mb.pending)
    # 이후 사용자가 AAPL 50 co-mingle 매수 → reconcile 이 사용자분 흡수 안 함(pending 이미 비워짐)
    fb._positions = [Position("AAPL", 50, 100.0)]
    mb2 = ManagedBroker(fb, path)                          # 디스크 재로드(pending 비워진 상태)
    mb2.reconcile_basis()
    check("co-mingle 50 → 봇 basis 0(사용자분 미흡수)", mb2.managed.get("AAPL", 0.0) == 0.0, mb2.managed)
    check("get_position None(사용자 50주 노출·매도 안 함)", mb2.get_position("AAPL") is None, mb2.get_position("AAPL"))


def test_amount_buy_quote_fail_lastpx_fallback():
    print("[RECON] 금액매수 place-time 시세 실패 → 마지막 성공시세 폴백 예약(0 예약→크래시 시 포지션 유실 방지)")
    class FlakyBroker(FakeBroker):
        fail_quote = False
        def get_quote(self, s):
            if self.fail_quote:
                raise RuntimeError("quote outage")
            return super().get_quote(s)
    fb = FlakyBroker(prices={"AAPL": 100.0})
    mb = ManagedBroker(fb, _sleeve())
    mb.get_quote("AAPL")                                 # 성공 → last_px 캐시(100)
    fb.fail_quote = True                                 # 이후 시세 장애
    mb.place_order(OrderRequest("AAPL", Side.BUY, 0.0, OrderType.MARKET, amount=500.0))
    check("시세실패에도 pending 예약 비영(500/100=5.0)", abs(mb.pending.get("AAPL", 0.0) - 5.0) < 1e-9, mb.pending)


def test_reconcile_partial_then_full():
    print("[RECON] 부분체결(30s 초과) 잔량 복구: 체결분만 pending 차감(이중계상 방지) → 다음 reconcile 흡수")
    path = _sleeve([], {})
    fb = FakeBroker(prices={"NVDA": 200})
    mb = ManagedBroker(fb, path)
    mb.place_order(OrderRequest("NVDA", Side.BUY, 3, OrderType.MARKET))     # pending 3
    # 30s 시점 PARTIAL 1 체결(비종결) → basis 1, 체결분(1) 만 pending 차감 → pending 2 (잔여 미체결 의도)
    mb.record_fills([_ord("NVDA", Side.BUY, 3, OrderStatus.PARTIAL, 1)])
    check("부분체결 basis=1", mb.managed.get("NVDA") == 1, mb.managed)
    # basis+pending = 1+2 = 3 = 참의도 (구버그: pending 3 유지 → basis+pending=4 이중계상)
    check("비종결 pending=잔여의도 2(이중계상 방지)", mb.pending.get("NVDA") == 2, mb.pending)
    # 이후 전량 체결됨(실보유 3) → 다음 실행 reconcile: min(3, basis1+pending2)=3
    fb._positions = [Position("NVDA", 3, 200)]
    mb2 = ManagedBroker(fb, path)
    mb2.reconcile_basis()
    check("잔량 복구 basis=3", mb2.managed.get("NVDA") == 3, mb2.managed)


def test_reload_resyncs_from_disk():
    print("[RELOAD] reload() 가 디스크 슬리브로 인메모리 재동기화 (live_engine 락-안 재적재 훅 대응)")
    path = _sleeve([], {"NVDA": 3}, {})
    mb = ManagedBroker(FakeBroker(), path)
    check("초기 basis NVDA=3", mb.managed.get("NVDA") == 3, mb.managed)
    # 다른 프로세스(run_exit 등)가 락 밖에서 슬리브를 갱신했다고 가정 — mb 는 아직 모름(stale)
    save_sleeve(path, set(), {"NVDA": 3, "AAPL": 5}, {"MSFT": 1})
    check("reload 전엔 여전히 stale(AAPL 미반영)", "AAPL" not in mb.managed, mb.managed)
    mb.reload()
    check("reload 후 managed 재동기화(AAPL=5 반영)", mb.managed.get("AAPL") == 5, mb.managed)
    check("reload 후 pending 재동기화(MSFT=1 반영)", mb.pending.get("MSFT") == 1, mb.pending)


def test_record_fills_stale_cancel_recovers_real_fill():
    print("[FILLS] CANCELLED 로 강제된 주문이 실제론 체결됨(레이스) → get_order 재조회로 basis 복구")

    class _StaleCancelBroker(FakeBroker):
        def get_order(self, oid):
            # live_engine 이 cancel_order()==True 만으로 로컬 status 를 CANCELLED 로 강제하지만
            # 실제 거래소엔 전량 체결돼 있던 레이스 — 재조회가 진짜 체결량을 드러낸다.
            return Order(order_id=oid, request=None, status=OrderStatus.FILLED, filled_qty=3.0)

    mb = ManagedBroker(_StaleCancelBroker(), _sleeve([], {}, {"NVDA": 3}))
    stale = Order(order_id="O1", request=OrderRequest("NVDA", Side.BUY, 3, OrderType.MARKET),
                  status=OrderStatus.CANCELLED, filled_qty=0.0)   # 로컬 stale fq=0
    mb.record_fills([stale])
    check("stale fq=0 무시하고 재조회한 실체결 3 을 basis 반영", mb.managed.get("NVDA") == 3, mb.managed)
    check("pending 해소(종결)", mb.pending == {}, mb.pending)


def test_record_fills_protected_never_added():
    print("[FILLS] 보호종목 체결 들어와도 basis 미편입")
    mb = ManagedBroker(FakeBroker(), _sleeve(["CONL"], {}))
    mb.record_fills([_ord("CONL", Side.BUY, 1)])
    check("CONL basis 미편입", "CONL" not in mb.managed, mb.managed)


# ───── Executor 통합 (핵심) ─────
def test_integration_protected_never_liquidated():
    print("[★통합] 리밸런서가 보호종목(CONL) 절대 매도 안 함")
    fb = FakeBroker(positions=[Position("CONL", 800, 6.8), Position("GOOGL", 2, 300)],
                    cash=500.0, prices={"CONL": 7.0, "GOOGL": 310.0, "NVDA": 200.0})
    mb = ManagedBroker(fb, _sleeve(["CONL"], {"GOOGL": 2}))
    for r in Executor(mb, alloc=0.95).plan({}):
        mb.place_order(r)
    placed = [(r.side.value, r.symbol, r.qty) for r in fb.placed]
    check("CONL 주문 0건", all(s != "CONL" for _, s, _ in placed), placed)
    check("GOOGL 매도 발생", any(side == "SELL" and s == "GOOGL" for side, s, _ in placed), placed)


def test_integration_comingle_caps_sell():
    print("[★통합] co-mingle: basis 수량만 매도 (사용자분 불가침)")
    fb = FakeBroker(positions=[Position("GOOGL", 102, 300)], cash=10.0, prices={"GOOGL": 310.0})
    mb = ManagedBroker(fb, _sleeve([], {"GOOGL": 2}))
    for r in Executor(mb, alloc=0.95).plan({}):
        mb.place_order(r)
    sells = [(r.symbol, r.qty) for r in fb.placed if r.side == Side.SELL]
    check("GOOGL 매도 = basis 2 (사용자 100주 불가침)", sells == [("GOOGL", 2)], sells)


def test_integration_dot_symbol_no_churn():
    print("[★통합] BRK.B/BRK-B 표기차 — 유령 매도+재매수 churn 없음(finding1)")
    fb = FakeBroker(positions=[Position("BRK.B", 10, 400)], cash=50.0,
                    prices={"BRK-B": 400.0, "BRK.B": 400.0})
    mb = ManagedBroker(fb, _sleeve([], {"BRK-B": 10}))
    # 전략이 여전히 BRK-B 보유 원함(목표비중)
    for r in Executor(mb, alloc=0.95).plan({"BRK-B": 1.0}):
        mb.place_order(r)
    brk = [(r.side.value, r.qty) for r in fb.placed if r.symbol == "BRK-B"]
    sells = [x for x in brk if x[0] == "SELL"]
    buys = [x for x in brk if x[0] == "BUY"]
    check("BRK-B 전량매도 후 재매수 churn 없음", not (sells and buys), brk)


# ───── dual-team 2026-06-24: fail-closed 평가 + pending 롤백 ─────
def test_account_quote_fail_failclosed():
    print("[EQUITY] quote 실패 → fail-closed(raise), avg_price 폴백 안 함 (손실가드 보호)")

    class _NoQuoteBroker(FakeBroker):
        def get_quote(self, s):
            raise RuntimeError("no price")
    mb = ManagedBroker(_NoQuoteBroker(positions=[Position("GOOGL", 2, 300)], cash=500),
                       _sleeve([], {"GOOGL": 2}))
    raised = False
    try:
        mb.get_account()
    except Exception:
        raised = True
    check("quote 실패 시 get_account raise(fail-closed)", raised)


def test_buy_reject_rolls_back_pending():
    print("[PENDING] BUY 비즈니스 거부 → pending 롤백 (과대계상 즉시 해소)")

    class _RejectBroker(FakeBroker):
        def place_order(self, req):
            self.placed.append(req)
            return Order(order_id="", request=req, status=OrderStatus.REJECTED, message="거부")
    mb = ManagedBroker(_RejectBroker(positions=[], cash=1000), _sleeve([], {}))
    o = mb.place_order(OrderRequest("AAPL", Side.BUY, 5, OrderType.MARKET))
    check("거부 반환", o.status == OrderStatus.REJECTED, o.status)
    check("pending 롤백됨 (AAPL 없음)", "AAPL" not in mb.pending, mb.pending)


# ───── EXEC-2: 매도 미체결 시 매수는 실현현금으로 재사이징(추정 proceeds 과대배포 차단) ─────
def test_exec2_unfilled_sell_shrinks_buy():
    print("[★EXEC-2] run_once: 매도 거부 → 그 proceeds 로 사이징한 매수가 실현현금 이내로 축소")
    import live_engine
    from live_engine import RunConfig, run_once
    from broker.paper import PaperBroker
    from tests_stage1 import _use_temp_state, _fake_select

    class _SellRejectPaper(PaperBroker):
        """토스 비즈니스 거부 시뮬 — 매도는 항상 REJECTED(현금 미실현), 매수/현금회계는 실제."""
        def place_order(self, req):
            if req.side == Side.SELL:
                oid = f"paper-sr"
                o = Order(order_id=oid, request=req, status=OrderStatus.REJECTED,
                          message="테스트 매도 거부(proceeds 미실현)")
                self._orders[oid] = o
                return o
            return super().place_order(req)

    prices = {"OLD": 100.0, "NEW": 50.0}
    b = _SellRejectPaper(cash=100.0, price_fn=lambda s: prices[s],
                         commission=0.0, spread=0.0, slippage=0.0)
    b._positions["OLD"] = Position("OLD", 10.0, 100.0)   # 평가 $1000 (매도 예정이나 거부됨)
    # equity=1100 → investable≈1045 → NEW 목표 8주($400, w=40% 단일비중 한도). plan 예산=cash100+
    # proceeds~1000=1100 → 8주 전량 배정. 매도 거부 시 실현현금 100 뿐 → 매수는 8주 아니라 2주로 축소돼야 함.
    _use_temp_state()
    orig = live_engine.select
    live_engine.select = _fake_select({"NEW": 0.4})   # OLD 미포함 → 전량청산(SELL) 대상. 40%=단일비중 한도
    try:
        res = run_once(None, b, RunConfig(top_n=3, vol_target=0.0, max_staleness_sessions=0),
                       today="2026-07-03", force=True)
    finally:
        live_engine.select = orig

    orders = res.get("orders", [])
    new_buys = [o for o in orders if o["symbol"] == "NEW" and o["side"] == "BUY"]
    deployed = sum(o["qty"] * prices["NEW"] for o in new_buys)   # 실제 제출된 매수대금
    check("NEW 매수 실현현금($100) 이내로 축소 (8주 과대배포 아님)", deployed <= 100.0 + 1e-6,
          f"deployed={deployed} (qty={[o['qty'] for o in new_buys]})")
    check("매수 전량 드롭 아님 — $100 로 살 수 있는 만큼은 배포", deployed > 0, new_buys)
    # 불변식: 매도가 거부됐으니 현금은 최대 초기 $100 만 쓸 수 있음(마이너스 현금 없음)
    check("체결 후 현금 음수 아님(오버알로케이션 없음)", b.get_account().cash >= -1e-6,
          b.get_account().cash)


def main():
    print("=" * 70)
    print(" ManagedBroker(관리 슬리브 v3) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    tests = [test_positions_cap_to_basis, test_positions_canonical_symbol, test_account_sleeve_equity,
             test_guards, test_symbol_normalization_guard, test_load_disjoint,
             test_buy_persists_pending, test_record_fills_buy_then_terminal,
             test_reconcile_recovers_crashed_buy, test_reconcile_caps_at_real,
             test_reconcile_preserves_comingle, test_amount_pending_drift_invariant,
             test_pending_one_shot_no_comingle_absorb, test_amount_buy_quote_fail_lastpx_fallback,
             test_reconcile_partial_then_full, test_reload_resyncs_from_disk,
             test_record_fills_stale_cancel_recovers_real_fill,
             test_record_fills_protected_never_added, test_integration_protected_never_liquidated,
             test_integration_comingle_caps_sell, test_integration_dot_symbol_no_churn,
             test_account_quote_fail_failclosed, test_buy_reject_rolls_back_pending,
             test_exec2_unfilled_sell_shrinks_buy]
    for t in tests:
        t(); print()
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
