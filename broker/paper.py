"""PaperBroker — 모의체결. 라이브 경로 전체를 토스 없이 dry-run 검증.

시장가 체결 시 스프레드+슬리피지 반영(낙관적 mid 체결 방지), 지정가는 교차 시 체결.
현금/포지션 인메모리 추적. price_fn(symbol)->float 주입 (결정론적 테스트). 미주입 시
yfinance 최근 종가. ※현금은 float — 장기 다수 리밸런스 시 미세 드리프트 가능(실거래
전환 시 Decimal 권장).
"""
import itertools
import math
from typing import Callable, Optional

from .base import (BaseBroker, AccountInfo, Position, Quote, Order, OrderRequest,
                   OrderStatus, OrderType, Side, floor_qty)


class PaperBroker(BaseBroker):
    name = "paper"

    def __init__(self, cash: float = 100000.0, price_fn: Optional[Callable[[str], float]] = None,
                 commission: float = 0.0005, spread: float = 0.0005, slippage: float = 0.0005,
                 state_file=None, free_below: float = 10.0):
        self._state_file = state_file   # 설정 시 현금·포지션 디스크 영속 → 다일 진화(스케줄 재실행 간 책 유지)
        self._cash = cash
        self._positions: dict = {}   # symbol -> Position
        self._orders: dict = {}      # order_id -> Order
        self._last_px: dict = {}     # symbol -> 마지막 성공 시세 (호가공백 시 equity 폴백 — 원가 avg 보다 시장 현실 반영)
        self._price_fn = price_fn or self._yf_price
        self._commission = commission
        self._free_below = free_below  # 명목 이 값 이하 거래는 수수료 무료(소액 면제, 토스 정책)
        self._spread = spread        # 호가 스프레드 (bid/ask = mid ∓ spread/2)
        self._slippage = slippage    # 시장가 추가 슬리피지 (체결 불리 방향)
        self._ids = itertools.count(1)
        if state_file:
            self._load()             # 직전 저장 책 복원 (없으면 시드 cash 로 시작)

    # --- 연결 (모의: no-op) ---
    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    # --- 시세 ---
    @staticmethod
    def _yf_price(symbol: str) -> float:
        import data
        from datetime import date
        df = data.load(symbol, "2024-01-01", str(date.today()))
        return float(df["Close"].iloc[-1])

    def _fee(self, notional: float) -> float:
        """거래 수수료 — 명목 $free_below 이하는 무료(소액 면제), 초과는 명목×commission."""
        return 0.0 if notional <= self._free_below else notional * self._commission

    def get_quote(self, symbol: str) -> Quote:
        p = float(self._price_fn(symbol))
        if not math.isfinite(p) or p <= 0:
            # NaN/inf/≤0 시세 = "조회 실패"와 동치로 승격. yfinance 가 미완성 세션 행을 NaN Close 로
            # 돌려주면 종전엔 그대로 통과 → get_account 의 mkt 가 NaN → equity NaN → 킬스위치가
            # bad_equity 로 fail-closed 트립(자동해제 없음, 사람이 --reset-halt 해야 복구).
            # 예외로 올리면 각 호출부의 기존 시세공백 폴백이 그대로 처리한다:
            # get_account=마지막 성공시세/평단, place_order=보유 매도만 평단 진행, executor=사이징 거부.
            raise ValueError(f"{symbol} 비정상 시세 (last={p})")
        self._last_px[symbol] = p              # 마지막 성공 시세 — 호가공백 폴백(원가 과대평가 회피)
        half = self._spread / 2.0
        return Quote(symbol=symbol, last=p, bid=p * (1 - half), ask=p * (1 + half))

    # --- 계좌/포지션 ---
    def get_positions(self) -> list:
        return list(self._positions.values())

    def get_account(self) -> AccountInfo:
        mkt = 0.0
        for sym, pos in self._positions.items():
            try:
                px = self.get_quote(sym).last
            except Exception:
                # 시세 조회 불가(영속 보유가 당일 패널서 빠짐·장중 호가공백 등) → *마지막 성공 시세* 폴백.
                # 원가(avg) 직행은 폭락+호가공백(같은 부하로 상관발생) 시 equity 를 원가로 과대평가해
                # 일중손실 halt(eq<=day_start*(1-loss))를 정확히 필요한 순간 억제 → 마지막 실거래가로
                # 시장 현실 반영(halt 정상 발화). 한 번도 못 받은 종목만 최후로 평단(crash·책동결 방지).
                lp = self._last_px.get(sym)
                px = lp if (lp and lp > 0) else pos.avg_price
            mkt += pos.qty * px
        equity = self._cash + mkt
        return AccountInfo(cash=self._cash, equity=equity, buying_power=self._cash)

    # --- 주문 ---
    def place_order(self, req: OrderRequest) -> Order:
        oid = f"paper-{next(self._ids)}"
        order = Order(order_id=oid, request=req)
        try:
            quote = self.get_quote(req.symbol)
        except Exception:
            # 시세 조회 불가(영속 보유가 당일 패널서 빠짐) — 보유분 매도(청산)는 평단 폴백으로 진행해
            # 책이 동결되지 않게(다음 런이 orphan 청산해 진화). 매수/미보유는 가격 필수라 재raise.
            pos = self._positions.get(req.symbol)
            if req.side == Side.SELL and pos is not None:
                quote = Quote(symbol=req.symbol, last=pos.avg_price,
                              bid=pos.avg_price, ask=pos.avg_price)
            else:
                raise
        # 체결가 결정 — 시장가는 ask 매수/bid 매도 + 슬리피지(불리 방향)
        if req.order_type == OrderType.MARKET:
            if req.side == Side.BUY:
                fill = quote.ask * (1 + self._slippage)
            else:
                fill = quote.bid * (1 - self._slippage)
        else:  # LIMIT — 교차 시에만 체결
            crosses = (req.side == Side.BUY and quote.last <= req.limit_price) or \
                      (req.side == Side.SELL and quote.last >= req.limit_price)
            if not crosses:
                order.status = OrderStatus.SUBMITTED
                order.message = "지정가 미교차 — 대기"
                self._orders[oid] = order
                return order
            fill = req.limit_price

        # 금액주문(orderAmount) — 달러로 매수, 체결가로 소수주 환산(BUY 전용). reconcile 위해 filled_qty 채움.
        amt = getattr(req, "amount", None)
        if amt is not None and req.side == Side.BUY:
            cost = float(amt)
            if not (fill > 0) or fill in (float("inf"), float("-inf")):   # 0/NaN/inf 체결가 — 현금차감·FILLED 금지(현금 증발 차단)
                order.status = OrderStatus.REJECTED
                order.message = f"비정상 체결가 ({fill}) — 금액주문 거부"
                self._orders[oid] = order
                return order
            fee = self._fee(cost)
            if self._cash < cost + fee:
                order.status = OrderStatus.REJECTED
                order.message = f"현금부족 (필요 {cost+fee:,.0f} > 보유 {self._cash:,.0f})"
                self._orders[oid] = order
                return order
            qty = floor_qty(cost / fill)              # 소수주 2자리 절사(정책) — 요청금액 이하로만 체결
            if qty <= 0:                              # 금액이 0.01주도 못 사는 초소액 → 거부(현금 미차감)
                order.status = OrderStatus.REJECTED
                order.message = f"금액 {cost:,.2f} < 0.01주 (체결가 {fill:,.2f}) — 소수주 최소단위 미달"
                self._orders[oid] = order
                return order
            cost = qty * fill                         # 절사 후 실매수대금(요청금액 이하) — 잔액은 현금 유지
            fee = self._fee(cost)
            self._cash -= (cost + fee)
            self._apply_fill(req.symbol, qty, fill)
            order.status = OrderStatus.FILLED
            order.filled_qty = qty
            order.avg_fill_price = fill
            self._orders[oid] = order
            return order

        cost = fill * req.qty
        fee = self._fee(cost)
        if req.side == Side.BUY:
            if self._cash < cost + fee:
                order.status = OrderStatus.REJECTED
                order.message = f"현금부족 (필요 {cost+fee:,.0f} > 보유 {self._cash:,.0f})"
                self._orders[oid] = order
                return order
            self._cash -= (cost + fee)
            self._apply_fill(req.symbol, req.qty, fill)
        else:  # SELL
            pos = self._positions.get(req.symbol)
            if pos is None or pos.qty < req.qty:
                order.status = OrderStatus.REJECTED
                order.message = "보유수량 부족"
                self._orders[oid] = order
                return order
            self._cash += (cost - fee)
            self._apply_fill(req.symbol, -req.qty, fill)

        order.status = OrderStatus.FILLED
        order.filled_qty = req.qty
        order.avg_fill_price = fill
        self._orders[oid] = order
        return order

    def _apply_fill(self, symbol: str, signed_qty: float, price: float):
        pos = self._positions.get(symbol)
        if pos is None:
            if signed_qty > 0:
                self._positions[symbol] = Position(symbol, floor_qty(signed_qty), price)   # 신규 보유수량 2자리 절사(정책)
        else:
            new_qty = pos.qty + signed_qty
            if new_qty <= 1e-9:
                del self._positions[symbol]
            elif signed_qty > 0:  # 추가매수 → 평단 갱신 (평단은 절사前 실수량 가중, 보유수량만 절사)
                pos.avg_price = (pos.avg_price * pos.qty + price * signed_qty) / new_qty
                pos.qty = floor_qty(new_qty)   # 책 보유수량 2자리 절사 — float 누적꼬리(예 83.20+41.32=124.51999999) 제거 → 매도·표시가 2dp 정책 준수
            else:  # 일부매도 → 평단 유지
                pos.qty = floor_qty(new_qty)   # 부분매도 잔량도 2자리 절사(다음 전량청산·표시가 정책 준수)
        self._save()   # 체결마다 책 영속 (다일 진화 — 다음 실행이 직전 상태 로드)

    def _save(self):
        """현금·포지션을 state_file 에 원자적·내구적 저장 (다일 진화). state_file 없으면 no-op.
        실패는 무음 흡수 안 함 — stderr 경고(다음 실행 stale 책 되감김=더블바이 인지)."""
        if not self._state_file:
            return
        import json
        import os
        import sys
        from pathlib import Path
        p = Path(self._state_file)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {"cash": self._cash,
                    "positions": [{"symbol": s, "qty": pos.qty, "avg_price": pos.avg_price}
                                  for s, pos in self._positions.items()]}
            tmp = f"{p}.{os.getpid()}.tmp"   # per-pid tmp — 동시저장 충돌 방지
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, allow_nan=False)   # NaN/Inf 직렬화 차단(fail-fast) — 손상값 디스크 기록 방지
                f.flush()
                os.fsync(f.fileno())          # 디스크 반영 보장 — 전원차단 시 빈/잘린 파일 방지(guardrail 패턴)
            from paths import atomic_replace
            if not atomic_replace(tmp, str(p)):   # 교체 실패 시 tmp 정리됨
                print(f"[paper] 책 교체 실패(다음 실행 stale 가능) {p}", file=sys.stderr)
        except Exception as e:
            try:
                os.remove(tmp)               # 쓰기 도중 실패 → orphan tmp 정리
            except (OSError, NameError):
                pass
            print(f"[paper] 책 저장 실패(다음 실행 stale 가능) {p}: {e!r}", file=sys.stderr)

    def reload(self):
        """디스크 책으로 인메모리 재동기화 — 공유책(일1런↔장중루프) 임계구역서 read-modify-write 용.
        다른 프로세스(run_live)가 책을 바꿨으면 흡수. state_file 없으면 no-op.
        파일 존재+로드실패(파싱/검증 오류)는 raise — 호출자가 stale 인메모리인지 *알 수 없는* 상태를
        삼키지 않고 fail-closed 판단하게 한다(run_intraday._on_bar/eod_flatten 의 기존 분기가 받음).
        파일 부재(fresh 시작)는 raise 안 함 — _load 가 존재검사 후 조기 return 이라 자동 보장."""
        if self._state_file:
            self._load(raise_on_error=True)

    def credit_cash(self, amount: float):
        """외부 현금 입금(배당 등) — 체결 경로와 분리, 즉시 영속. 양수·유한값만(손상값 차단)."""
        import math
        a = float(amount)
        if not math.isfinite(a) or a <= 0:
            raise ValueError(f"비정상 입금액 {amount!r}")
        self._cash += a
        self._save()

    def _load(self, raise_on_error: bool = False):
        """state_file 에서 현금·포지션 복원 — all-or-nothing(부분 손상이 책을 깨지 않게).
        미존재면 시드 유지. 손상/항목누락이면 self 미변경(시드 유지) + stderr 경고(침묵복원 차단).
        raise_on_error=True(reload() 전용)면 경고 후 예외를 호출자에 전파 — __init__ 경로(기본 False)는
        기존 동작(무예외 흡수) 그대로 유지."""
        import json
        import sys
        from pathlib import Path
        p = Path(self._state_file)
        if not p.exists():
            return
        try:
            import math
            data = json.loads(p.read_text(encoding="utf-8"))
            new_cash = float(data["cash"])
            if not math.isfinite(new_cash):
                raise ValueError(f"non-finite cash {new_cash}")     # NaN/Inf 손상 책 거부(시드 유지)
            new_pos = {}
            for d in data.get("positions", []):
                q, ap = float(d["qty"]), float(d["avg_price"])
                if not (math.isfinite(q) and math.isfinite(ap)) or q <= 0 or ap <= 0:
                    raise ValueError(f"비정상 포지션 {d.get('symbol')}: qty={q} avg={ap}")
                fq = floor_qty(q)                       # 기존 >2dp 책 로드 시 즉시 2자리 정규화(정책)
                if fq <= 0:
                    continue                            # 0.01주 미만 legacy dust → 개별 드롭(책 거부 아님)
                new_pos[d["symbol"]] = Position(d["symbol"], fq, ap)
            # 전부 검증·파싱 성공 후에야 원자적 대입(all-or-nothing) — 중간 실패 시 cash 만 바뀌고 보유
            # 증발하는 정합붕괴 방지. 비유한/음수는 손상으로 보고 시드 유지(intraday_guard._load 패턴).
            self._cash, self._positions = new_cash, new_pos
        except Exception as e:
            print(f"[paper] 책 로드 실패({'전파' if raise_on_error else '시드 유지'}) {p}: {e!r}",
                  file=sys.stderr)
            if raise_on_error:
                raise

    def cancel_order(self, order_id: str) -> bool:
        o = self._orders.get(order_id)
        if o and o.status == OrderStatus.SUBMITTED:
            o.status = OrderStatus.CANCELLED
            return True
        return False

    def get_order(self, order_id: str) -> Order:
        return self._orders[order_id]
