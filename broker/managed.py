"""ManagedBroker — '관리 슬리브' 래퍼. 자동매매가 *자기가 산 수량만* 다루고 기존 보유분은
절대 건드리지 않게 강제한다 (단일 토스 계좌를 기존 포트폴리오와 공유하는 경우의 안전장치).

배경: 토스는 계좌 1개만 개설 가능 → 기존 보유종목(레버리지 ETF 등)과 자동매매가 한 계좌를
공유한다. 목표비중 리밸런서(Executor)는 '목표에 없는 보유종목 = 청산'으로 보므로, 가만 두면
사용자의 기존 종목을 전량 매도해버린다. 이 래퍼가 그걸 원천 차단한다.

슬리브 상태 파일 `toss_sleeve.json`:
    {"protected": [설정 시점 보유종목 — 절대 매매 금지],
     "managed":   {심볼: 자동매매가 *확정 보유한 수량(basis)*},
     "pending":   {심볼: 제출했으나 아직 확정 안 된 매수 수량(의도)}}
모든 심볼은 canonical form(_norm: 대문자·'.'→'-'·trim) 으로 저장·비교한다.

방어 (불변식: 기존/보호 보유분은 절대 매도·매수되지 않는다):
  1. get_positions()  → managed 심볼을 **min(실수량, basis)** 로, canonical 심볼로 노출.
        보호분 숨김 + 사용자가 같은 종목을 추가 매수(co-mingle)해도 봇은 자기 basis 만큼만 본다.
  2. place_order()    → SELL 은 managed&¬protected, BUY 은 ¬protected. BUY 는 제출 前 pending 영속.
  3. (호출부) 후보 유니버스에서 protected 제외 → 전략이 기존종목을 타겟으로 삼지 않음.
  4. basis 는 체결로 갱신하되, **크래시·30s초과·부분체결로 유실되지 않게** pending 의도로그 +
        reconcile_basis(다음 실행 시작 시 `min(실수량, basis+pending)` 로 채택)로 자가복구.

사이징: get_account().equity = (cap 적용) 현금 + managed(=cap된) 평가액 → 슬리브 기준.
"""
import json
import os
import sys
import tempfile

from .base import AccountInfo, Order, OrderStatus, Position, Side

_TERMINAL = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED)


def _norm(s) -> str:
    """심볼 canonical form — 대문자, '.'→'-', 공백 제거. 네임스페이스 불일치 방어."""
    return str(s).strip().upper().replace(".", "-")


def _num(x, default=0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def load_sleeve(path) -> dict:
    """슬리브 로드 → {protected:set, managed:dict[str,float], pending:dict[str,float]}.

    모든 심볼 정규화. protected 권위적 — protected 와 겹치는 managed/pending 항목 제거(disjoint).
    managed 가 옛 list 포맷이면 basis 0 으로 수용.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"토스 슬리브 미설정: {path} — 먼저 `python toss_setup.py` 로 보유종목 스냅샷을 떠라")
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    protected = {_norm(s) for s in d.get("protected", [])}

    def _as_qty_map(raw):
        out = {}
        if isinstance(raw, dict):
            for s, q in raw.items():
                out[_norm(s)] = _num(q)
        else:                                # 옛 list 포맷
            for s in raw:
                out[_norm(s)] = 0.0
        return out

    managed = _as_qty_map(d.get("managed", {}))
    pending = _as_qty_map(d.get("pending", {}))
    for m in (managed, pending):
        for s in list(m):                    # disjoint: protected 우선
            if s in protected:
                del m[s]
    return {"protected": protected, "managed": managed, "pending": pending}


def save_sleeve(path, protected, managed, pending=None):
    """슬리브 원자적 저장. protected: 심볼 iterable, managed/pending: dict 심볼→수량. 정규화·disjoint."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    prot = {_norm(s) for s in protected}

    def _clean(m):
        out = {}
        for s, q in dict(m or {}).items():
            ns = _norm(s)
            if ns in prot:                   # protected 권위적
                continue
            out[ns] = _num(q)
        return dict(sorted(out.items()))

    payload = {"protected": sorted(prot), "managed": _clean(managed), "pending": _clean(pending)}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        from paths import atomic_replace
        if not atomic_replace(tmp, path):   # Windows 동시읽기 PermissionError 재시도(paper.py/guardrail.py 와 동일 패턴)
            print(f"[managed] 슬리브 교체 실패(다음 호출 재시도) {path}", file=sys.stderr)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class ManagedBroker:
    """브로커 데코레이터 — 슬리브(자기 매수분 수량)만 노출하고 보호분 매매를 차단."""

    def __init__(self, broker, state_path, cash_cap: float = None):
        self._broker = broker
        self._state_path = str(state_path)
        self._cash_cap = cash_cap
        s = load_sleeve(self._state_path)
        self._protected = s["protected"]      # set[norm sym]
        self._managed = s["managed"]           # dict[norm sym -> basis qty]
        self._pending = s["pending"]           # dict[norm sym -> 미확정 매수 의도 qty]
        self._last_px = {}                     # norm sym -> 마지막 성공 시세(_intent_qty quote 실패 폴백)

    @property
    def name(self):
        return getattr(self._broker, "name", "broker") + "+managed"

    @property
    def protected(self):
        return set(self._protected)

    @property
    def managed(self):
        return dict(self._managed)

    @property
    def pending(self):
        return dict(self._pending)

    def _save(self):
        save_sleeve(self._state_path, self._protected, self._managed, self._pending)

    def reload(self):
        """디스크 슬리브로 인메모리 재동기화 — live_engine._run_once_locked 이 RunLock 임계구역
        진입 직후 호출(PaperBroker.reload 와 동일 패턴). 락 밖(__init__)에서 읽은 stale 슬리브를
        락 안 place_order/record_fills 가 그대로 덮어써 동시 실행(run_exit 등)의 갱신을 잃는
        lost-update 방지."""
        s = load_sleeve(self._state_path)
        self._protected = s["protected"]
        self._managed = s["managed"]
        self._pending = s["pending"]

    # ── passthrough ───────────────────────────────────────────────────────
    def connect(self):
        return self._broker.connect()

    def disconnect(self):
        return self._broker.disconnect()

    def get_quote(self, symbol):
        q = self._broker.get_quote(symbol)
        if q is not None and _num(getattr(q, "last", 0.0)) > 0:
            self._last_px[_norm(symbol)] = float(q.last)   # 마지막 성공 시세 캐시(_intent_qty 폴백)
        return q

    def get_order(self, order_id):
        return self._broker.get_order(order_id)

    def cancel_order(self, order_id):
        return self._broker.cancel_order(order_id)

    # ── 포지션: managed 를 min(실수량, basis) 로 cap, canonical 심볼로 노출 ──
    def get_positions(self):
        out = []
        for p in self._broker.get_positions():
            s = _norm(p.symbol)
            basis = self._managed.get(s, 0.0)
            if basis <= 0:
                continue
            qty = min(p.qty, basis)
            if qty > 1e-9:
                # canonical 심볼로 노출 — Executor diff/타겟 네임스페이스(유니버스=하이픈)와 일치시켜
                # BRK.B vs BRK-B 같은 표기차로 인한 유령 매도+재매수 churn 방지(diff 경계 정규화).
                out.append(Position(symbol=s, qty=qty, avg_price=p.avg_price))
        return out

    def get_position(self, symbol):
        for p in self.get_positions():
            if p.symbol == _norm(symbol):
                return p
        return None

    # ── 계좌: 슬리브 기준 equity ───────────────────────────────────────────
    def get_account(self) -> AccountInfo:
        real = self._broker.get_account()
        cash = real.cash if self._cash_cap is None else min(real.cash, self._cash_cap)
        managed_val = 0.0
        for p in self.get_positions():
            # quote 실패 시 avg_price(원가) 폴백은 하락 종목을 과대평가→손실 과소→손절가드 약화(fail-open).
            # 평가 불가면 그대로 전파(fail-closed) — 호출측(사이징/킬스위치)이 error 로 거래 보류한다.
            managed_val += p.qty * self._broker.get_quote(p.symbol).last
        return AccountInfo(cash=cash, equity=cash + managed_val, buying_power=cash)

    def _intent_qty(self, req) -> float:
        """주문 의도를 '주수'로 환산 — pending 예약/해소 단위 통일. 금액주문(amount)은 현재가로 추정주수.
        reconcile_basis 가 매 런 실보유로 cap 하고 pending 을 비우므로 추정오차는 자가치유(크래시복구만 담당)."""
        amt = getattr(req, "amount", None)
        if amt is not None:
            px = None
            try:
                px = self._broker.get_quote(req.symbol).last
            except Exception:
                px = None
            if not (px and px > 0):                       # 시세 실패/불량 → 마지막 성공 시세 폴백
                px = self._last_px.get(_norm(req.symbol))  #   (예약 0 → 크래시 시 포지션 유실 방지)
            return float(amt) / px if (px and px > 0) else 0.0
        return _num(req.qty)

    def _reserved(self, req) -> float:
        """place_order 예약 시각에 req 에 stash 한 *정확한* 예약 주수. 해소(record_fills·거부 롤백)가
        예약 때와 동일 값으로 pending 을 차감하게 해, 금액주문에서 예약↔해소 사이 호가가 드리프트하면
        _intent_qty 를 두 번 재계산해 pending 이 0 으로 안 빠지고 잔존→reconcile 이 사용자 co-mingle 흡수하던
        결함을 차단. stash 없으면(reconcile-only 등) _intent_qty 재추정 폴백."""
        rq = getattr(req, "_reserved_qty", None)
        return _num(rq) if rq is not None else self._intent_qty(req)

    # ── 주문 가드 + 매수 의도 영속(크래시 안전) ─────────────────────────────
    def place_order(self, req) -> Order:
        s = _norm(req.symbol)
        if req.side == Side.SELL and (s not in self._managed or s in self._protected):
            return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                         message=f"매도 차단 — {req.symbol} 은 관리종목 아님/보호종목(기존 보유분 보호)")
        if req.side == Side.BUY and s in self._protected:
            return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                         message=f"매수 차단 — {req.symbol} 은 보호종목(기존 보유분과 분리)")
        if req.side == Side.BUY:
            # 제출 前에 의도 영속 → 크래시/30s초과/늦은체결로 체결분이 슬리브에서 유실돼도
            # 다음 실행 reconcile_basis 가 실보유와 대조해 복구(중복매수·자본동결 방지).
            # 예약 주수를 req 에 stash — 해소도 *동일 값*으로(호가 드리프트 무관, _reserved 참조).
            iq = self._intent_qty(req)
            req._reserved_qty = iq
            self._pending[s] = self._pending.get(s, 0.0) + iq
            self._save()
        o = self._broker.place_order(req)
        if req.side == Side.BUY and getattr(o, "status", None) == OrderStatus.REJECTED:
            # 비즈니스 거부(미접수 확정) → 방금 올린 pending 즉시 롤백(과대계상 해소; reconcile 대기 불필요).
            # 전송오류(예외)는 주문이 실제 접수됐을 수 있어 롤백 안 함 — 예외 전파, reconcile 가 실보유로 정산.
            self._pending[s] = max(0.0, self._pending.get(s, 0.0) - self._reserved(req))
            self._prune()
            self._save()
        return o

    # ── 체결 반영 (live_engine 이 _await_fills 직후·reconcile 前 호출) ────────
    def record_fills(self, orders):
        """체결분으로 basis 갱신. BUY 체결분 += basis 및 종결 시 pending 차감, SELL 체결분 −= basis.

        보호종목은 절대 편입 안 함. 미종결(SUBMITTED/PARTIAL) 매수는 pending 유지 →
        다음 실행 reconcile_basis 가 실보유와 대조해 흡수.
        """
        changed = False
        for o in orders:
            req = getattr(o, "request", None)
            if req is None:
                continue
            s = _norm(req.symbol)
            if s in self._protected:
                continue
            fq = _num(getattr(o, "filled_qty", 0.0))
            status = getattr(o, "status", None)
            if status == OrderStatus.CANCELLED and req.side == Side.BUY and getattr(o, "order_id", None):
                # live_engine 의 잔존취소 루프가 cancel_order()==True 만으로 o.status 를 CANCELLED 로
                # 강제하고 filled_qty 는 갱신 안 함 — cancel 요청과 거의 동시에 실제 체결되면 stale
                # fq(과소)인 채 basis 미반영·pending 만 소진돼 실보유가 관리분에서 유실된다. 재조회로
                # 진짜 체결량을 채택(가능한 방어; live_engine 자체 수정은 소유 밖).
                try:
                    real = self._broker.get_order(o.order_id)
                    fq = max(fq, _num(getattr(real, "filled_qty", 0.0)))
                except Exception:
                    pass
            if req.side == Side.BUY:
                if fq > 0:
                    self._managed[s] = self._managed.get(s, 0.0) + fq
                    changed = True
                if status in _TERMINAL:                  # 종결 → 예약분(_reserved) 전액 해소(예약↔해소 동일값=드리프트 무관)
                    self._pending[s] = max(0.0, self._pending.get(s, 0.0) - self._reserved(req))
                    changed = True
                elif fq > 0 and getattr(req, "amount", None) is None:
                    # 비종결 부분체결(수량주문=정확 예약) → 체결분만 차감(basis+pending 이중계상 방지).
                    # 금액주문(추정 예약)은 비종결 중 pending 유지 — 추정<실체결 시 raw fq 차감이 pending 을 조기 0
                    # 으로 만들어 잔여체결분을 reconcile 이 복구 못(basis<real 무손절)하던 것 방지(종결 시 _reserved 해소).
                    self._pending[s] = max(0.0, self._pending.get(s, 0.0) - fq)
                    changed = True
            elif req.side == Side.SELL and fq > 0:
                self._managed[s] = max(0.0, self._managed.get(s, 0.0) - fq)
                changed = True
        self._prune()
        if changed:
            self._save()

    # ── 자가복구: 미확정 매수 의도를 실보유와 대조해 basis 로 흡수 ────────────
    def reconcile_basis(self):
        """실행 시작 시(거래 前) 호출. pending(미확정 매수)을 실보유와 대조해 basis 로 흡수한다.

        basis[s] = min(실보유, basis[s] + pending[s]) — 크래시/늦은/부분 체결로 유실된 봇 매수분 복구,
        실보유 cap 으로 ① 거부매수 과대계상해도 유령 없음 ② 사용자 co-mingle 미흡수(봇 의도 상한까지만).
        pending 은 **one-shot** — 이번 reconcile 후 무조건 비운다(고아 거부-BUY pending 이 누적돼 후일 사용자
        co-mingle 을 봇 basis 로 흡수하는 것을 원천 차단). pending 없는 장기보유 심볼은 여기서 건드리지 않고,
        노출 상한은 get_positions 의 read-time min(실보유, basis) 가 강제한다.

        ※ 알려진 DEFER(토스 라이브 前 sell-durability 필요, 현 paper-bound): (a) 크래시-후-완전매도 + 동일종목
          co-mingle 시 stale basis 가 co-mingle 잔여를 봇으로 오인(get_positions 캡이 노출은 실보유로 제한),
          (b) 크래시가 하필 이 one-shot reconcile 실행 중 real 이 transient 결측인 순간과 겹치면 그 매수 미복구
          (희귀 이중실패, 안전방향=미관리). 관련 시도(sell_pending·cap-clamp·per-symbol 보존)는 모두 더 나쁜
          회귀를 유발해 원본 one-shot 형태로 회귀함.
        """
        if not self._pending:
            return
        real = {}
        try:
            for p in self._broker.get_positions():
                real[_norm(p.symbol)] = real.get(_norm(p.symbol), 0.0) + _num(p.qty)
        except Exception:
            return                                       # 실보유 조회 실패 → pending 보존(다음 기회)
        for s, pq in self._pending.items():
            newb = min(real.get(s, 0.0), self._managed.get(s, 0.0) + _num(pq))
            if newb > 1e-9:
                self._managed[s] = newb
            else:
                self._managed.pop(s, None)
        self._pending = {}                               # one-shot — 무조건 비움(고아 pending 누적→co-mingle 흡수 차단)
        self._prune()
        self._save()

    def _prune(self):
        for s in [k for k, v in self._managed.items() if v <= 1e-9]:
            del self._managed[s]
        for s in [k for k, v in self._pending.items() if v <= 1e-9]:
            del self._pending[s]
