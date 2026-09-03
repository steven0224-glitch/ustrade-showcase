"""TossBroker 검증 — mocked HTTP(네트워크 0). 토스 Open API v1.1.1 매핑 정확성.

FakeSession 으로 토스 응답을 흉내내어 connect/계좌/포지션(US필터)/시세/주문/취소/조회 +
에러 분기(4xx 거부 vs 5xx raise)를 검증한다. 실거래/실주문 일절 없음.

실행:  & $py tests_toss.py
"""
import sys
import time

from broker.toss import TossBroker, TossAPIError
from broker.base import OrderRequest, Side, OrderType, OrderStatus

PASS, FAIL = [], []
BASE = "https://test.local"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


class FakeResp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """routes: {(METHOD, path): (status, payload)} 또는 callable(call)->(status,payload)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, data=None, timeout=None):
        path = url[len(BASE):] if url.startswith(BASE) else url
        call = {"method": method, "path": path, "headers": headers or {}, "params": params,
                "json": json, "data": data}
        self.calls.append(call)
        r = self.routes.get((method, path))
        if r is None:
            return FakeResp(404, {"error": {"code": "stock-not-found", "message": "no route"}})
        status, payload = r(call) if callable(r) else r
        return FakeResp(status, payload)


# 공통 라우트
_TOKEN = ("POST", "/oauth2/token")
_ACCOUNTS = ("GET", "/api/v1/accounts")
_BP = ("GET", "/api/v1/buying-power")
_HOLD = ("GET", "/api/v1/holdings")

_BASE_ROUTES = {
    _TOKEN: (200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 86400}),
    _ACCOUNTS: (200, {"result": [{"accountNo": "123", "accountSeq": 7, "accountType": "BROKERAGE"}]}),
    _BP: (200, {"result": {"currency": "USD", "cashBuyingPower": "3500.5"}}),
    _HOLD: (200, {"result": {
        "marketValue": {"amount": {"krw": "0", "usd": "1785"}},
        "items": [
            {"symbol": "AAPL", "marketCountry": "US", "quantity": "10",
             "averagePurchasePrice": "155.3", "currency": "USD"},
            {"symbol": "005930", "marketCountry": "KR", "quantity": "100",
             "averagePurchasePrice": "65000", "currency": "KRW"},
        ]}}),
}


def _broker(extra=None):
    routes = dict(_BASE_ROUTES)
    if extra:
        routes.update(extra)
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    return b, sess


# ───── connect ─────
def test_connect():
    print("[CONNECT] 토큰 발급 + BROKERAGE 계좌 accountSeq 저장")
    b, sess = _broker()
    b.connect()
    check("토큰 저장", b._token == "tok", b._token)
    check("accountSeq=7 선택", b._account_seq == 7, b._account_seq)
    # 토큰 발급은 Authorization 헤더 없이
    tok_call = next(c for c in sess.calls if c["path"] == "/oauth2/token")
    check("토큰 요청에 Authorization 없음", "Authorization" not in tok_call["headers"], tok_call["headers"])
    check("토큰 요청 form grant_type", (tok_call["data"] or {}).get("grant_type") == "client_credentials",
          tok_call["data"])


def test_connect_no_creds():
    print("[CONNECT] 키 미설정 → TossAPIError")
    # 위생화 — 머신에 실제 TOSS_API_KEY 가 setx 돼 있으면 __init__ 가 env 키를 주워 테스트가 거짓실패.
    # env 를 잠시 비워 '키 없음' 조건을 결정론으로 만든다.
    import os
    saved = {k: os.environ.pop(k, None) for k in ("TOSS_API_KEY", "TOSS_API_SECRET")}
    try:
        b = TossBroker(api_key=None, api_secret=None, base_url=BASE, session=FakeSession({}))
        raised = False
        try:
            b.connect()
        except TossAPIError as e:
            raised = e.code == "no-credentials"
        check("키 없으면 no-credentials raise", raised)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# ───── 계좌 ─────
def test_get_account():
    print("[ACCOUNT] USD 매수가능금액 + USD 평가액 → cash·equity")
    b, sess = _broker()
    b.connect()
    acct = b.get_account()
    check("cash = USD 매수가능금액 3500.5", acct.cash == 3500.5, acct.cash)
    check("equity = cash + USD 평가액(1785) = 5285.5", acct.equity == 5285.5, acct.equity)
    check("buying_power = cash", acct.buying_power == 3500.5, acct.buying_power)
    bp_call = next(c for c in sess.calls if c["path"] == "/api/v1/buying-power")
    check("buying-power 에 X-Tossinvest-Account 헤더", bp_call["headers"].get("X-Tossinvest-Account") == "7",
          bp_call["headers"])
    check("buying-power currency=USD", (bp_call["params"] or {}).get("currency") == "USD", bp_call["params"])


# ───── 포지션 (US 필터 — 치명적 안전) ─────
def test_get_positions_us_only():
    print("[POSITIONS] ⚠ marketCountry=='US' 만 반환 — KR 보유주식 미관리")
    b, _ = _broker()
    b.connect()
    pos = b.get_positions()
    syms = [p.symbol for p in pos]
    check("US 종목(AAPL)만 반환", syms == ["AAPL"], syms)
    check("KR 종목(005930) 제외 — 리밸런서 청산대상 오인 차단", "005930" not in syms, syms)
    check("수량 10", pos[0].qty == 10.0, pos[0].qty)
    check("평균매입가 155.3", pos[0].avg_price == 155.3, pos[0].avg_price)


# ───── 시세 ─────
def test_get_quote():
    print("[QUOTE] /prices lastPrice → last")
    b, _ = _broker({("GET", "/api/v1/prices"):
                    (200, {"result": [{"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD"}]})})
    b.connect()
    q = b.get_quote("AAPL")
    check("last = 185.70", q.last == 185.70, q.last)
    check("bid/ask = last 근사", q.bid == 185.70 and q.ask == 185.70, (q.bid, q.ask))


def test_get_quote_not_found():
    print("[QUOTE] 미존재 종목 → 404 → TossAPIError")
    b, _ = _broker()   # /prices 라우트 없음 → 404
    b.connect()
    raised = False
    try:
        b.get_quote("ZZZZ")
    except TossAPIError as e:
        raised = e.http_status == 404
    check("404 → TossAPIError(http_status=404)", raised)


# ───── 주문 생성 ─────
def test_place_order_market():
    print("[ORDER] 시장가 매수 → 바디 매핑 + orderId 반환")
    b, sess = _broker({("POST", "/api/v1/orders"):
                       (200, {"result": {"orderId": "OID123", "clientOrderId": "x"}})})
    b.connect()
    req = OrderRequest(symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    o = b.place_order(req)
    check("orderId 반환", o.order_id == "OID123", o.order_id)
    check("status=SUBMITTED(비동기 체결 폴링)", o.status == OrderStatus.SUBMITTED, o.status)
    body = next(c for c in sess.calls if c["path"] == "/api/v1/orders")["json"]
    check("symbol", body["symbol"] == "AAPL", body)
    check("side=BUY", body["side"] == "BUY", body)
    check("orderType=MARKET", body["orderType"] == "MARKET", body)
    check("quantity 정수문자열 '10'", body["quantity"] == "10", body)
    check("timeInForce=DAY", body["timeInForce"] == "DAY", body)
    check("clientOrderId 멱등키 존재", bool(body.get("clientOrderId")), body)
    check("MARKET 엔 price 없음", "price" not in body, body)
    ord_call = next(c for c in sess.calls if c["path"] == "/api/v1/orders")
    check("주문에 X-Tossinvest-Account 헤더", ord_call["headers"].get("X-Tossinvest-Account") == "7",
          ord_call["headers"])


def test_place_order_limit_price_fmt():
    print("[ORDER] 지정가 → US 달러 소수 가격 포맷")
    b, sess = _broker({("POST", "/api/v1/orders"): (200, {"result": {"orderId": "OID9"}})})
    b.connect()
    req = OrderRequest(symbol="AAPL", side=Side.SELL, qty=5, order_type=OrderType.LIMIT,
                       limit_price=185.5)
    b.place_order(req)
    body = next(c for c in sess.calls if c["path"] == "/api/v1/orders")["json"]
    check("price=185.50 (소수 2자리)", body.get("price") == "185.50", body)


def test_place_order_business_reject():
    print("[ORDER] 4xx(잔고부족) → REJECTED Order 반환(raise 아님, PaperBroker 동일)")
    b, _ = _broker({("POST", "/api/v1/orders"):
                    (422, {"error": {"code": "insufficient-buying-power",
                                     "message": "주문 가능 금액이 부족합니다.", "requestId": "r1"}})})
    b.connect()
    req = OrderRequest(symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    o = b.place_order(req)   # raise 하면 안 됨
    check("status=REJECTED", o.status == OrderStatus.REJECTED, o.status)
    check("거부 사유 메시지에 코드", "insufficient-buying-power" in o.message, o.message)


def test_place_order_transient_raises():
    print("[ORDER] 5xx(전송오류) → raise (상위에서 에러카운트→가드 정지)")
    b, _ = _broker({("POST", "/api/v1/orders"):
                    (500, {"error": {"code": "internal-error", "message": "일시 오류"}})})
    b.connect()
    req = OrderRequest(symbol="AAPL", side=Side.BUY, qty=10, order_type=OrderType.MARKET)
    raised = False
    try:
        b.place_order(req)
    except TossAPIError as e:
        raised = e.http_status == 500
    check("5xx → TossAPIError raise", raised)


# ───── 주문 조회 (상태 매핑) ─────
def _order_resp(status, filled="0", avg=None):
    return (200, {"result": {"orderId": "OID123", "symbol": "AAPL", "side": "BUY",
                             "orderType": "MARKET", "timeInForce": "DAY", "status": status,
                             "quantity": "10", "currency": "USD", "orderedAt": "2026-03-28T09:30:00+09:00",
                             "execution": {"filledQuantity": filled, "averageFilledPrice": avg,
                                           "filledAmount": None, "commission": None, "tax": None,
                                           "filledAt": None, "settlementDate": None}}})


def test_get_order_filled():
    print("[GET_ORDER] FILLED + 체결수량/평균가 매핑")
    b, _ = _broker({("GET", "/api/v1/orders/OID123"): _order_resp("FILLED", "10", "185.25")})
    b.connect()
    o = b.get_order("OID123")
    check("status=FILLED", o.status == OrderStatus.FILLED, o.status)
    check("filled_qty=10", o.filled_qty == 10.0, o.filled_qty)
    check("avg_fill_price=185.25", o.avg_fill_price == 185.25, o.avg_fill_price)


def test_get_order_status_map():
    print("[GET_ORDER] 토스 상태 → 내부 OrderStatus 매핑")
    cases = [("PARTIAL_FILLED", OrderStatus.PARTIAL), ("CANCELED", OrderStatus.CANCELLED),
             ("REJECTED", OrderStatus.REJECTED), ("PENDING", OrderStatus.SUBMITTED),
             ("REPLACED", OrderStatus.CANCELLED), ("CANCEL_REJECTED", OrderStatus.REJECTED)]
    for toss_st, want in cases:
        b, _ = _broker({("GET", "/api/v1/orders/OID123"): _order_resp(toss_st)})
        b.connect()
        o = b.get_order("OID123")
        check(f"{toss_st} → {want.value}", o.status == want, o.status)


# ───── 취소 ─────
def test_cancel_order():
    print("[CANCEL] 200 → True, 409 → False")
    b, _ = _broker({("POST", "/api/v1/orders/OID123/cancel"): (200, {"result": {"orderId": "CXL1"}})})
    b.connect()
    check("취소 성공 → True", b.cancel_order("OID123") is True)
    b2, _ = _broker({("POST", "/api/v1/orders/OID9/cancel"):
                     (409, {"error": {"code": "already-filled", "message": "이미 체결"}})})
    b2.connect()
    check("이미 체결 취소 → False", b2.cancel_order("OID9") is False)


# ───── 멱등키 (중복주문 방지 — 실거래 oversell 직결) ─────
def _cid_of(sess):
    return next(c for c in sess.calls if c["path"] == "/api/v1/orders")["json"]["clientOrderId"]


def _place_cid(req):
    b, sess = _broker({("POST", "/api/v1/orders"): (200, {"result": {"orderId": "O"}})})
    b.connect()
    b.place_order(req)
    return _cid_of(sess)


def test_idem_key_includes_intent():
    print("[IDEMPOTENCY] 멱등키가 order_type·limit_price 포함 — 의도 다른 동수량 주문 충돌 방지 (C2)")
    mkt = _place_cid(OrderRequest("AAPL", Side.SELL, 10, OrderType.MARKET))
    same = _place_cid(OrderRequest("AAPL", Side.SELL, 10, OrderType.MARKET))
    lim = _place_cid(OrderRequest("AAPL", Side.SELL, 10, OrderType.LIMIT, limit_price=185.0))
    lim2 = _place_cid(OrderRequest("AAPL", Side.SELL, 10, OrderType.LIMIT, limit_price=190.0))
    check("동일 의도 = 동일 키 (멱등 보존)", mkt == same, (mkt, same))
    check("MARKET vs LIMIT = 다른 키 (충돌 방지)", mkt != lim, (mkt, lim))
    check("지정가 다르면 = 다른 키 (재가격 허용)", lim != lim2, (lim, lim2))


def test_idem_key_uses_et_date():
    print("[IDEMPOTENCY] 멱등키 날짜=ET — KST 자정 횡단(장중) 재시도도 같은 키 (C1 oversell 방지)")
    import datetime as _dt
    import broker.toss as _t
    if not hasattr(_t, "_now_et"):
        check("toss._now_et 존재(ET 날짜 시드)", False, "미구현 — date.today()(KST) 사용 중")
        return
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        import pytz
        ET = pytz.timezone("America/New_York")
    saved = _t._now_et

    def at(et_dt):
        _t._now_et = lambda: et_dt
        return _place_cid(OrderRequest("AAPL", Side.SELL, 10, OrderType.MARKET))
    try:
        # EDT 10:30 = KST 23:30(3/30) · EDT 11:30 = KST 00:30(3/31, 자정 넘김) — 둘 다 ET 세션일 3/30
        a = at(_dt.datetime(2026, 3, 30, 10, 30, tzinfo=ET))
        b = at(_dt.datetime(2026, 3, 30, 11, 30, tzinfo=ET))
        c = at(_dt.datetime(2026, 3, 31, 10, 30, tzinfo=ET))   # 다음 ET 세션
        check("KST 자정 넘겨도 같은 ET일 → 같은 키", a == b, (a, b))
        check("다음 ET 세션 → 다른 키 (날짜 성분이 ET)", a != c, (a, c))
    finally:
        _t._now_et = saved


_CAL = ("GET", "/api/v1/market-calendar/US")


def test_market_open_naive_times():
    print("[MARKET-OPEN] naive ISO 윈도도 TypeError 없이 판정 (항상휴장 버그 방지)")
    openw = {_CAL: (200, {"result": {"today": {"regularMarket": {
        "startTime": "2000-01-01T00:00:00", "endTime": "2099-12-31T23:59:59"}}}})}
    b, _ = _broker(openw); b.connect()
    check("naive 윈도(과거~미래) → 개장 True", b.market_open("US") is True, b.market_open("US"))
    closedw = {_CAL: (200, {"result": {"today": {"regularMarket": {
        "startTime": "2000-01-01T00:00:00", "endTime": "2000-01-02T00:00:00"}}}})}
    b2, _ = _broker(closedw); b2.connect()
    check("naive 윈도(과거 닫힘) → 휴장 False", b2.market_open("US") is False, b2.market_open("US"))


def test_order_no_retry_on_5xx():
    print("[IDEMPOTENCY] 주문 POST 는 5xx 재시도 안 함 — 응답유실 중복주문 차단")
    calls = {"n": 0}

    def order_route(call):
        calls["n"] += 1
        return (500, {"error": {"code": "srv", "message": "일시오류"}})
    routes = dict(_BASE_ROUTES); routes[("POST", "/api/v1/orders")] = order_route
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=2, session=FakeSession(routes))
    b.connect()
    raised = False
    try:
        b.place_order(OrderRequest("AAPL", Side.BUY, 1, OrderType.MARKET))
    except TossAPIError:
        raised = True
    check("주문 5xx → raise(transient)", raised)
    check("주문 POST 재시도 안 함 (max_retries=2여도 1회)", calls["n"] == 1, calls["n"])


def test_place_order_fractional_and_zero():
    print("[ORDER] 소수 수량 — 토스는 US 시장가매도(MARKET+SELL)만 허용 / BUY·LIMIT·0 은 REJECTED")
    routes = {("POST", "/api/v1/orders"): (200, {"result": {"orderId": "O"}})}
    # 0주 → REJECTED, 미전송 (좀비 포지션 방지)
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("AAPL", Side.SELL, 0.0, OrderType.MARKET))
    check("0주 SELL → REJECTED", o.status == OrderStatus.REJECTED, o.status)
    check("0주 미전송", not any(c["path"] == "/api/v1/orders" for c in sess.calls))
    # 0.5 BUY → REJECTED (소수 매수 불가 — 토스 제약)
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("AAPL", Side.BUY, 0.5, OrderType.MARKET))
    check("0.5주 BUY → REJECTED (소수매수 불가)", o.status == OrderStatus.REJECTED, o.status)
    check("소수BUY 미전송", not any(c["path"] == "/api/v1/orders" for c in sess.calls))
    # 0.5 SELL LIMIT → REJECTED (소수는 시장가만)
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("AAPL", Side.SELL, 0.5, OrderType.LIMIT, limit_price=100.0))
    check("0.5주 SELL LIMIT → REJECTED (소수는 시장가만)", o.status == OrderStatus.REJECTED, o.status)
    # 0.5 SELL MARKET → ACCEPTED, quantity '0.5' 전송
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("AAPL", Side.SELL, 0.5, OrderType.MARKET))
    check("0.5주 SELL MARKET → SUBMITTED (소수매도 허용)", o.status == OrderStatus.SUBMITTED, o.status)
    sent = [c for c in sess.calls if c["path"] == "/api/v1/orders"]
    check("소수 수량 '0.5' 전송", bool(sent) and sent[0]["json"]["quantity"] == "0.5",
          sent[0]["json"].get("quantity") if sent else None)
    # 정수 매수는 그대로 '10' (기존 동작 불변)
    b, sess = _broker(routes); b.connect()
    b.place_order(OrderRequest("AAPL", Side.BUY, 10, OrderType.MARKET))
    sent = [c for c in sess.calls if c["path"] == "/api/v1/orders"]
    check("정수 매수 quantity '10' 불변", bool(sent) and sent[0]["json"]["quantity"] == "10",
          sent[0]["json"].get("quantity") if sent else None)


def test_place_order_amount():
    print("[ORDER] 금액주문(orderAmount) — quantity 대신 달러금액, quantity XOR, 멱등키에 금액 포함")
    routes = {("POST", "/api/v1/orders"): (200, {"result": {"orderId": "O"}})}
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=63.5))
    check("금액주문 → SUBMITTED", o.status == OrderStatus.SUBMITTED, o.status)
    body = [c for c in sess.calls if c["path"] == "/api/v1/orders"][0]["json"]
    check("orderAmount '63.50' 전송", body.get("orderAmount") == "63.50", body.get("orderAmount"))
    check("quantity 미포함 (XOR)", "quantity" not in body, list(body))
    cid1 = body.get("clientOrderId")
    # 금액 0 → REJECTED, 미전송
    b, sess = _broker(routes); b.connect()
    o = b.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=0.0))
    check("금액 0 → REJECTED", o.status == OrderStatus.REJECTED, o.status)
    check("금액0 미전송", not any(c["path"] == "/api/v1/orders" for c in sess.calls))
    # 다른 금액 → 다른 clientOrderId (부분체결 후 잔액 재주문이 중복차단 안 되게)
    b, sess = _broker(routes); b.connect()
    b.place_order(OrderRequest("MU", Side.BUY, 0.0, OrderType.MARKET, amount=40.0))
    cid2 = [c for c in sess.calls if c["path"] == "/api/v1/orders"][0]["json"]["clientOrderId"]
    check("금액 다르면 멱등키 다름", cid1 != cid2, (cid1, cid2))


# ───── 선제 토큰갱신 (진입→전송 창의 401 누출 차단) ─────
def test_proactive_refresh_before_expiry():
    print("[AUTH] 토큰 만료 임박 → 요청 *전* 선제 재인증 (401 발생 없이 새 토큰으로 전송)")
    # 첫 발급 토큰=tok1, 재발급 토큰=tok2 로 구분. buying-power 는 유효토큰만 200, 그 외엔 401.
    # 선제갱신이 없다면 만료된 tok1 로 요청 → 401. 있으면 tok2 로 나가 401 자체가 안 뜬다.
    tok_state = {"n": 0}

    def token_route(call):
        tok_state["n"] += 1
        return (200, {"access_token": f"tok{tok_state['n']}", "expires_in": 86400})

    seen401 = {"hit": False}

    def bp_route(call):
        bearer = (call["headers"] or {}).get("Authorization", "")
        if bearer != "Bearer tok2":       # 만료된 tok1(또는 무토큰)로 오면 401
            seen401["hit"] = True
            return (401, {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다"}})
        return (200, {"result": {"currency": "USD", "cashBuyingPower": "2090.55"}})

    routes = dict(_BASE_ROUTES)
    routes[_TOKEN] = token_route
    routes[_BP] = bp_route
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    b.connect()
    check("최초 토큰=tok1", b._token == "tok1", b._token)
    # 토큰 만료를 강제(진입→전송 사이 만료를 시뮬). 선제갱신이 요청 전 tok2 로 재발급해야 한다.
    b._token_expiry = time.monotonic() - 1.0
    tok_calls_before = sum(1 for c in sess.calls if c["path"] == "/oauth2/token")
    acct = b.get_account()
    tok_calls_after = sum(1 for c in sess.calls if c["path"] == "/oauth2/token")
    check("get_account 성공", acct.cash == 2090.55, acct.cash)
    check("요청 전 재인증 발생(tok2)", b._token == "tok2", b._token)
    check("선제 재발급 1회 발생", tok_calls_after == tok_calls_before + 1,
          (tok_calls_before, tok_calls_after))
    check("401 은 아예 발생하지 않음(반응형 아님)", not seen401["hit"], seen401)


def test_proactive_refresh_before_order_post():
    print("[AUTH] 만료 임박 시 주문 POST 는 *조립 전* 선제갱신 — POST 재시도 아님, 멱등키 불변")
    tok_state = {"n": 0}

    def token_route(call):
        tok_state["n"] += 1
        return (200, {"access_token": f"tok{tok_state['n']}", "expires_in": 86400})

    order_calls = []

    def order_route(call):
        bearer = (call["headers"] or {}).get("Authorization", "")
        order_calls.append({"bearer": bearer, "cid": (call["json"] or {}).get("clientOrderId")})
        if bearer != "Bearer tok2":       # 만료토큰이면 401 (선제갱신 없으면 여기 걸림)
            return (401, {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다"}})
        return (200, {"result": {"orderId": "OID9"}})

    routes = dict(_BASE_ROUTES)
    routes[_TOKEN] = token_route
    routes[("POST", "/api/v1/orders")] = order_route
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    b.connect()
    b._token_expiry = time.monotonic() - 1.0    # 진입~전송 사이 만료 시뮬
    req = OrderRequest("AAPL", Side.SELL, 3, OrderType.MARKET)
    o = b.place_order(req)
    check("주문 SUBMITTED", o.status == OrderStatus.SUBMITTED, o.status)
    check("POST 는 정확히 1회(재시도 없음)", len(order_calls) == 1, len(order_calls))
    check("POST 는 새 토큰 tok2 로 전송", order_calls[0]["bearer"] == "Bearer tok2", order_calls[0]["bearer"])
    check("clientOrderId 존재·결정론", bool(order_calls[0]["cid"]) and len(order_calls[0]["cid"]) == 32,
          order_calls[0]["cid"])


def test_persistent_401_backstop():
    print("[AUTH] 재인증 후에도 401 지속(자격증명 취소) → 여전히 raise (가드 에러카운트 경로 보존, 무한루프·무성성공 아님)")
    tok_calls = {"n": 0}

    def token_route(call):
        tok_calls["n"] += 1
        return (200, {"access_token": f"tok{tok_calls['n']}", "expires_in": 86400})

    bp_hits = {"n": 0}

    def bp_always_401(call):
        bp_hits["n"] += 1
        return (401, {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다"}})

    routes = dict(_BASE_ROUTES)
    routes[_TOKEN] = token_route
    routes[_BP] = bp_always_401
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    b.connect()
    raised = None
    try:
        b.get_account()
    except TossAPIError as e:
        raised = e
    check("여전히 TossAPIError raise", raised is not None)
    check("401 transient 로 상향(가드 에러카운트)", raised is not None and raised.http_status == 401,
          raised.http_status if raised else None)
    # 반응형 재인증 1회만 시도 후 종결 — 무한루프 아님(buying-power 호출이 유한).
    check("무한루프 아님(재인증 1회 후 종결)", bp_hits["n"] == 2, bp_hits["n"])


def test_order_no_resend_on_401():
    print("[AUTH] 주문 POST(retry=False) — 401 재인증 성공해도 동일 body 재전송 금지(응답유실 시 중복주문 방지)")
    tok_state = {"n": 0}

    def token_route(call):
        tok_state["n"] += 1
        return (200, {"access_token": f"tok{tok_state['n']}", "expires_in": 86400})

    order_calls = {"n": 0}

    def order_route(call):
        order_calls["n"] += 1
        return (401, {"error": {"code": "invalid-token", "message": "유효하지 않은 토큰입니다"}})

    routes = dict(_BASE_ROUTES)
    routes[_TOKEN] = token_route
    routes[("POST", "/api/v1/orders")] = order_route
    sess = FakeSession(routes)
    b = TossBroker(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    b.connect()
    tok_calls_before = tok_state["n"]
    raised = None
    try:
        b.place_order(OrderRequest("AAPL", Side.BUY, 1, OrderType.MARKET))
    except TossAPIError as e:
        raised = e
    check("401 → raise(REJECTED 로 흡수 안 함, 상위 에러카운트 경로 보존)", raised is not None)
    check("주문 POST 는 정확히 1회(재인증해도 동일 body 재전송 안 함)", order_calls["n"] == 1, order_calls["n"])
    check("재인증 자체는 시도됨(다음 호출은 새 토큰 사용 가능)", tok_state["n"] == tok_calls_before + 1,
          (tok_calls_before, tok_state["n"]))


def main():
    print("=" * 70)
    print(" TossBroker 검증 — mocked HTTP(네트워크 없음)")
    print("=" * 70)
    print()
    test_connect(); print()
    test_connect_no_creds(); print()
    test_get_account(); print()
    test_get_positions_us_only(); print()
    test_get_quote(); print()
    test_get_quote_not_found(); print()
    test_place_order_market(); print()
    test_place_order_limit_price_fmt(); print()
    test_place_order_business_reject(); print()
    test_place_order_transient_raises(); print()
    test_get_order_filled(); print()
    test_get_order_status_map(); print()
    test_cancel_order(); print()
    test_idem_key_includes_intent(); print()
    test_idem_key_uses_et_date(); print()
    test_market_open_naive_times(); print()
    test_order_no_retry_on_5xx(); print()
    test_place_order_fractional_and_zero(); print()
    test_place_order_amount(); print()
    test_proactive_refresh_before_expiry(); print()
    test_proactive_refresh_before_order_post(); print()
    test_persistent_401_backstop(); print()
    test_order_no_resend_on_401()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
