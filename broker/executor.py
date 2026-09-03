"""Executor — 목표비중 → 주문 변환. 라이브 리밸런스 코어.

전략(rs_momentum)+리스크오버레이가 만든 목표비중을 받아,
현재 계좌·포지션과 비교해 차이만큼 매수/매도 주문 생성·제출.
어떤 BaseBroker 와도 동작 (PaperBroker 로 검증 → TossBroker 로 라이브).

매도 먼저, 매수 나중 (현금 확보 순서). 정수 주식 (소수점은 추후).
"""
import math

from .base import BaseBroker, OrderRequest, Side, OrderType, floor_qty


class Executor:
    def __init__(self, broker: BaseBroker, alloc: float = 0.95, cost_buffer: float = 0.0,
                 fractional: bool = False, min_order_usd: float = 5.0, fee_reserve: float = 0.005):
        self.broker = broker
        self.alloc = alloc   # 투자 비율 (나머지 현금 버퍼)
        # 라이브 비용버퍼 — 브로커가 _commission/_spread/_slippage 속성을 노출 안 하면(토스) buy_mult=1.0
        # 이라 시장가 체결가>last 시 cash_cap 초과. 명시 버퍼로 매수 사이징에 헤드룸 확보(EXEC-1 라이브 보강).
        self.cost_buffer = max(0.0, float(cost_buffer or 0.0))
        # 소수주 모드 — BUY=orderAmount($), SELL=소수 quantity. fee_reserve=매수예산 haircut(gross/net 방어),
        # min_order_usd=무거래밴드(churn·sub-min거부 방지; 전량청산은 면제).
        self.fractional = bool(fractional)
        self.min_order_usd = max(0.0, float(min_order_usd or 0.0))
        self.fee_reserve = min(0.10, max(0.0, float(fee_reserve or 0.0)))

    def _buy_mult(self) -> float:
        """시장가 매수 실비용 배수(+라이브 버퍼) — plan 과 cap_buys_to_cash 공유(단일소스)."""
        commission = float(getattr(self.broker, "_commission", 0.0) or 0.0)
        spread = float(getattr(self.broker, "_spread", 0.0) or 0.0)
        slippage = float(getattr(self.broker, "_slippage", 0.0) or 0.0)
        return (1.0 + spread / 2.0) * (1.0 + slippage) * (1.0 + commission) * (1.0 + self.cost_buffer)

    def cap_buys_to_cash(self, buys: list, cash: float) -> list:
        """매수 배치를 *실현* 현금 이내로 재캡 — 사이징 근거였던 매도가 실체결됐는지 무관하게
        배포 매수대금 Σ ≤ 실현현금 불변식 강제(EXEC-2). run_once 가 매도 체결확인 후 refresh 한
        get_account().cash 를 넘겨, 미체결 매도로 자금 못 댄 후행 매수를 축소/드롭한다.

        입력 buys 는 plan 이 낸 순서(비중 큰 것 우선) 유지 — 예산 소진 시 뒤쪽부터 잘림.
        정수: 요청수량을 실현현금이 감당하는 만큼으로 재사이징(afford). 소수(amount): 잔여예산으로 클램프
        (min_order_usd 미달분은 드롭). 매도가 전부 체결(정상경로)이면 캡=plan 예산 이상이라 no-op.
        """
        if cash < 0:
            cash = 0.0
        out = []
        if self.fractional:
            budget = cash * (1.0 - self.fee_reserve)   # plan 과 동일 haircut
            for r in buys:
                amt = math.floor(min(float(r.amount or 0.0), budget) * 100) / 100.0
                if amt >= self.min_order_usd:
                    r.amount = amt
                    out.append(r)
                    budget -= amt
            return out
        buy_mult = self._buy_mult()
        budget = cash
        for r in buys:
            price = r.ref_price
            unit = (price or 0.0) * buy_mult
            afford = int(budget / unit) if unit > 0 else 0
            q = min(int(r.qty), afford)
            if q > 0:
                r.qty = q
                out.append(r)
                budget -= q * unit
        return out

    def plan(self, target_weights: dict) -> list:
        """목표비중 → OrderRequest 리스트 (제출 전 계획). 매도→매수 순.

        매수는 '가용현금 예산'(현재현금 + 매도 예상 순현금) 내로 캡 — 풀투자 회전 시
        수수료/스프레드/슬리피지로 마지막 매수가 현금부족 거부(→partial)되던 것 방지(EXEC-1).
        """
        acct = self.broker.get_account()
        positions = {p.symbol: p for p in self.broker.get_positions()}
        if self.fractional:
            return self._plan_fractional(target_weights, acct, positions)
        investable = acct.equity * self.alloc
        commission = float(getattr(self.broker, "_commission", 0.0) or 0.0)
        spread = float(getattr(self.broker, "_spread", 0.0) or 0.0)
        slippage = float(getattr(self.broker, "_slippage", 0.0) or 0.0)
        buy_mult = self._buy_mult()   # 시장가 매수 실비용 배수(+라이브 버퍼) — cap_buys_to_cash 와 공유
        sell_mult = (1.0 - spread / 2.0) * (1.0 - slippage) * (1.0 - commission)  # 매도 순수령 배수

        symbols = set(positions) | set(target_weights)
        sells, buy_cands, proceeds = [], [], 0.0
        for sym in symbols:
            w = target_weights.get(sym, 0.0)
            cur_qty = positions[sym].qty if sym in positions else 0
            if w > 0:
                price = self.broker.get_quote(sym).last
                if price is None or price != price or price <= 0:   # None/NaN/0/음수
                    raise ValueError(f"{sym} 비정상 시세 (last={price}) — 사이징 불가")
                tgt_qty = int(w * investable / price)
            else:
                price, tgt_qty = None, 0   # 전량 청산 — 시세 불필요(위험축소라 시세 불량이어도 진행)
            diff = tgt_qty - cur_qty
            if diff > 0:
                buy_cands.append((sym, diff, price))
            elif diff < 0:
                qty = -diff
                _r = "리밸런스 편출(목표 0%)" if w <= 0 else f"비중축소(목표 {w:.0%})"
                sells.append(OrderRequest(sym, Side.SELL, qty, OrderType.MARKET, reason=_r))
                sp = price if price is not None else self._safe_price(sym)   # 청산분 예상대금(불량/실패 시 0=보수적)
                proceeds += qty * sp * sell_mult

        # 비중 큰 주문부터 예산 배정 (예산 빠듯하면 큰 포지션 우선 충족)
        budget = acct.cash + proceeds
        buys = []
        for sym, qty, price in sorted(buy_cands, key=lambda x: -x[1] * x[2]):
            unit = price * buy_mult
            afford = int(budget / unit) if unit > 0 else 0
            q = min(qty, afford)
            if q > 0:
                # ref_price=사이징 기준가 — 체결 후 (체결가-기준가)/기준가 로 시장가 슬리피지 측정(review 자동튜닝).
                _w = target_weights.get(sym, 0.0)
                buys.append(OrderRequest(sym, Side.BUY, q, OrderType.MARKET, ref_price=price,
                                         reason=f"진입/증량(목표 {_w:.0%})"))
                budget -= q * unit
        return sells + buys

    def _safe_price(self, sym):
        """best-effort 시세 — 매도 예상대금 추정용(불량/실패 시 0=보수적, 매수예산 축소)."""
        try:
            p = self.broker.get_quote(sym).last
            return p if (p is not None and p == p and p > 0) else 0.0
        except Exception:
            return 0.0

    def _plan_fractional(self, target_weights: dict, acct, positions: dict) -> list:
        """소수주 계획 — BUY=orderAmount($), SELL=소수 quantity(토스 US 시장가매도). 매도 먼저(현금확보)→매수.

        달러델타 사이징: target$ = w·investable, delta$ = target$ − 현보유$. 정수반올림(=기존 무거래밴드)이
        사라지므로 min_order_usd 명시 밴드로 churn·sub-min거부 방지. 전량청산(w≤0)은 밴드 면제(항상 전량).
        매도 트림이 잔량 < min_order_usd 면 dust-closeout(전량 매도). fee_reserve haircut 으로 orderAmount
        gross/net 미확정에도 매수합이 매수가능금액 초과(→last reject)하지 않게 헤드룸 확보.
        """
        investable = acct.equity * self.alloc * (1.0 - self.fee_reserve)
        symbols = set(positions) | set(target_weights)
        sells, buy_cands, proceeds = [], [], 0.0
        for sym in symbols:
            w = target_weights.get(sym, 0.0)
            cur_qty = positions[sym].qty if sym in positions else 0.0
            if w <= 0:                                       # 전량청산 — 보유 전량 소수 매도(밴드 면제)
                if cur_qty > 1e-9:
                    sells.append(OrderRequest(sym, Side.SELL, cur_qty, OrderType.MARKET,
                                              reason="리밸런스 편출(목표 0%)"))
                    proceeds += cur_qty * self._safe_price(sym) * (1.0 - self.fee_reserve)   # 순현금 haircut(트림·비-frac 패리티)
                continue
            price = self.broker.get_quote(sym).last
            if price is None or price != price or price <= 0:
                raise ValueError(f"{sym} 비정상 시세 (last={price}) — 사이징 불가")
            delta = w * investable - cur_qty * price
            if delta > 0:
                buy_cands.append((sym, delta, price))
            elif delta < 0:
                if -delta < self.min_order_usd:
                    continue                                 # 미세 트림 → 무거래 밴드(밴드 먼저! dust-closeout 보다 앞)
                raw = min(-delta / price, cur_qty)
                sell_qty = floor_qty(raw)                    # 부분트림 2자리 절사(정책)
                if sell_qty <= 0 and raw > 0:                # 정당트림(밴드통과)이 <0.01주로 절사 소멸(고가주)
                    sell_qty = min(0.01, cur_qty)            #   → 최소 거래증분만 매도(보유<0.01주면 전량); 트림누락·드리프트 방지
                remain_usd = (cur_qty - sell_qty) * price
                if 0 < remain_usd < self.min_order_usd:
                    sell_qty = cur_qty                       # 실트림인데 잔량이 dust면 전량 매도(레거시 잔량은 정확 청산)
                if sell_qty > 1e-9:
                    sells.append(OrderRequest(sym, Side.SELL, sell_qty, OrderType.MARKET,
                                              reason=f"비중축소(목표 {w:.0%})"))
                    proceeds += sell_qty * price * (1.0 - self.fee_reserve)   # 매도 순현금(haircut, 레거시 sell_mult 패리티)
        # 매수 예산 = (haircut)현금 + 매도 예상 순현금. 비중 큰 주문부터 배정, sub-min 은 주문 안 함(거부 아님).
        budget = acct.cash * (1.0 - self.fee_reserve) + proceeds
        buys = []
        for sym, delta, price in sorted(buy_cands, key=lambda x: -x[1]):
            amt = math.floor(min(delta, budget) * 100) / 100.0   # 센트 내림 — Σ주문 ≤ 예산 불변식(올림 초과 방지)
            if amt >= self.min_order_usd:
                buys.append(OrderRequest(sym, Side.BUY, 0.0, OrderType.MARKET,
                                         ref_price=price, amount=amt,
                                         reason=f"진입/증량(목표 {target_weights.get(sym, 0.0):.0%})"))
                budget -= amt
        return sells + buys
