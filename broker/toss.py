"""TossBroker — 토스증권 Open API 어댑터 (openapi.tossinvest.com, v1.1.1).

BaseBroker 7메서드를 토스 REST 에 매핑. PaperBroker 로 검증된 라이브 경로(선택→리스크→
가드레일→Executor→체결)에 무수정 연결된다.

매핑:
  connect       POST /oauth2/token (client_credentials) → 토큰 + GET /api/v1/accounts → accountSeq
  get_account   GET /api/v1/buying-power(USD) + /api/v1/holdings → cash·equity (USD 기준)
  get_positions GET /api/v1/holdings → items (⚠ marketCountry=="US" 만 — KR 보유주식 미관리)
  get_quote     GET /api/v1/prices → lastPrice
  place_order   POST /api/v1/orders (quantity-based)
  cancel_order  POST /api/v1/orders/{id}/cancel
  get_order     GET /api/v1/orders/{id} → status·체결

⚠️ 실거래 전용 — 토스 Open API 에 별도 paper/sandbox 엔드포인트 없음. 모의는 PaperBroker 사용.
⚠️ 계좌뷰는 **USD 기준**(cash=USD 매수가능금액, equity=cash+USD 평가액). 계좌가 KRW 자동환전
   방식이면 USD 매수가능금액이 0/불안정할 수 있어 가드레일(일손실·드로다운)이 오작동 가능 →
   실거래 전 계좌 펀딩 방식 확인 필수. currency 인자로 조정 가능.
⚠️ Executor 는 기본 MARKET 주문 → 미국장 정규시간(KST 22:30~05:00)에만 체결. 마감 후 실행 시
   order-hours-closed 로 거부될 수 있음 → 실행 스케줄/주문유형은 운영에서 별도 결정.

키: 환경변수 TOSS_API_KEY / TOSS_API_SECRET (코드 하드코딩 금지). base_url 기본 = 운영 서버.
"""
import datetime
import hashlib
import os
import re
import time

from .base import (BaseBroker, AccountInfo, Quote, Position, Order, OrderRequest,
                   OrderType, OrderStatus, Side)

DEFAULT_BASE_URL = "https://openapi.tossinvest.com"

# expires_in 결측/0 시 폴백 토큰수명(초)과 만료 전 선제갱신 여유(초). 폴백은 토스 실토큰수명보다
# *짧게* 잡아 선제갱신이 실만료 前에 발화하게 한다(600s 폴백이 실수명을 넘겨 주기적 401 을 유발하던 것 차단).
_TOKEN_TTL_FALLBACK = 300.0
_TOKEN_REFRESH_MARGIN = 60.0

# 멱등키 날짜 성분용 ET 시계. date.today()(호스트=KST)는 미 정규장 중 KST 자정을 넘으며 값이
# 바뀌어 같은 의도 주문이 다른 clientOrderId 로 중복접수(oversell)됐다. ET 캘린더 날짜는
# 정규장(09:30~16:00 ET) 내내 불변 → 자정 횡단 재시도도 같은 키 유지(토스 중복차단 보존).
try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover — tzdata 없으면 pytz 폴백
    import pytz
    _ET = pytz.timezone("America/New_York")


def _now_et() -> "datetime.datetime":
    """현재 시각 → 미 동부시(UTC 경유 — 호스트 로컬TZ 무관)."""
    return datetime.datetime.now(datetime.timezone.utc).astimezone(_ET)

# 토스 주문상태 → 내부 OrderStatus. PENDING 계열=폴링 지속(SUBMITTED). 정정/취소거부=종료(REJECTED).
_STATUS_MAP = {
    "PENDING": OrderStatus.SUBMITTED,
    "PENDING_CANCEL": OrderStatus.SUBMITTED,
    "PENDING_REPLACE": OrderStatus.SUBMITTED,
    "PENDING_NEW": OrderStatus.SUBMITTED,
    "NEW": OrderStatus.SUBMITTED,
    "ACCEPTED": OrderStatus.SUBMITTED,
    "RECEIVED": OrderStatus.SUBMITTED,
    "OPEN": OrderStatus.SUBMITTED,
    "PARTIAL_FILLED": OrderStatus.PARTIAL,
    "PARTIALLY_FILLED": OrderStatus.PARTIAL,
    "FILLED": OrderStatus.FILLED,
    "CANCELED": OrderStatus.CANCELLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "REPLACED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.CANCELLED,            # 미체결 만료 = 종료(미취소 잔존 오인 방지)
    "DONE_FOR_DAY": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "CANCEL_REJECTED": OrderStatus.REJECTED,
    "REPLACE_REJECTED": OrderStatus.REJECTED,
}


class TossAPIError(Exception):
    """토스 API 오류. http_status 가 4xx 면 비즈니스 거부(graceful), 5xx/None 이면 전송오류(transient)."""

    def __init__(self, code, message="", request_id=None, http_status=None):
        self.code = code
        self.message = message
        self.request_id = request_id
        self.http_status = http_status
        rid = f" (req {request_id})" if request_id else ""
        super().__init__(f"[{http_status} {code}] {message}{rid}")

    def is_business_error(self) -> bool:
        return self.http_status is not None and 400 <= self.http_status < 500


def _num(x, default=0.0) -> float:
    """문자열/None 숫자 → float. 토스는 정밀도 보존 위해 수치를 문자열로 반환한다."""
    if x is None:
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


class TossBroker(BaseBroker):
    name = "toss"

    def __init__(self, api_key: str = None, api_secret: str = None, base_url: str = None,
                 paper: bool = True, currency: str = "USD", market_country: str = "US",
                 timeout: float = 10.0, max_retries: int = 2, session=None):
        self.api_key = api_key or os.environ.get("TOSS_API_KEY")
        self.api_secret = api_secret or os.environ.get("TOSS_API_SECRET")
        self.base_url = (base_url or os.environ.get("TOSS_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        # paper 인자는 호출부 시그니처 호환용으로만 수용 — 토스엔 sandbox 없어 항상 실거래.
        # 속성으로 보관 안 함(읽는 곳 0 이던 오도적 플래그 제거, deprecated).
        self.currency = currency
        self._market_country = market_country
        self._timeout = timeout
        self._max_retries = max_retries
        self._token = None
        self._token_expiry = None    # time.monotonic() 기준 만료 시각
        self._account_seq = None
        self._account_no = None
        if session is not None:
            self._session = session
        else:
            import requests
            self._session = requests.Session()

    # ── HTTP ────────────────────────────────────────────────────────────────
    def _request(self, method, path, *, auth=True, account=False, params=None,
                 json_body=None, form=None, oauth=False, _reauth=True, retry=True):
        """단일 HTTP 호출. 성공 시 파싱된 JSON(envelope 포함) 반환, 실패 시 TossAPIError.

        429/5xx/네트워크오류는 max_retries 까지 백오프 재시도. 그 외 4xx 는 즉시 raise.
        retry=False(주문 POST 등 비멱등 호출): 재시도 안 함 — 응답유실 후 재시도가 중복주문을
        낼 수 있어 앱 레벨 멱등 재플랜(동일 clientOrderId)에 위임.
        """
        import requests
        # 선제 갱신 — 매 인증요청 직전 만료를 확인. 메서드 진입 시점의 _ensure_connected 만으론 진입→전송 사이
        # (특히 get_account 처럼 한 메서드가 여러 _request 를 연쇄할 때) 토큰이 만료돼 401 이 새는 창이 남는다.
        # 여기서 재발급하므로 POST 는 *조립 전* 새 토큰으로 나간다 — POST 재시도가 아니라 사전갱신(중복주문 불가).
        # oauth/_reauth=False(재인증 후 재호출)엔 적용 안 함(재귀 방지, connect 스스로 토큰을 세팅).
        if auth and not oauth and _reauth and self._token and self._token_expiry is not None \
                and time.monotonic() >= self._token_expiry:
            self._token = None
            self._token_expiry = None
            self.connect()
        url = self.base_url + path
        headers = {}
        if auth:
            if not self._token:
                raise TossAPIError("not-connected", "connect() 를 먼저 호출하세요")
            headers["Authorization"] = f"Bearer {self._token}"
        if account:
            if self._account_seq is None:
                raise TossAPIError("no-account", "계좌 미선택 — connect() 재호출 필요")
            headers["X-Tossinvest-Account"] = str(self._account_seq)

        max_r = self._max_retries if retry else 0
        for attempt in range(max_r + 1):
            try:
                resp = self._session.request(method, url, headers=headers, params=params,
                                             json=json_body, data=form, timeout=self._timeout)
            except requests.RequestException as e:   # 연결/타임아웃 등 전송오류
                if attempt < max_r:
                    time.sleep(attempt + 1)
                    continue
                raise TossAPIError("network-error", str(e), None, None)

            status = resp.status_code
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            # 재시도 대상(429/5xx)
            if status == 429 and attempt < max_r:
                ra = resp.headers.get("Retry-After")
                time.sleep(_num(ra, attempt + 1))
                continue
            if status >= 500 and attempt < max_r:
                time.sleep(attempt + 1)
                continue
            # 401(토큰 만료/무효) → 1회 재인증 후 동일 요청 재시도. 토큰 조기만료를 비즈니스거부로
            # 오인해 유효주문을 REJECTED 처리하던 것 차단. connect()가 _request(oauth)를 부르므로
            # _reauth=False 로 재귀 방지(재인증 후에도 401이면 정상 raise → 상위 transient 처리).
            # retry=False(주문 POST 등 비멱등 호출)는 재인증까지만 하고 *재전송 안 함* — 응답유실
            # 여부가 불명인 채 동일 body 를 다시 쏘면 retry=False 계약(비멱등 재시도 금지) 위반이라
            # clientOrderId 중복차단(미실증)에만 기대게 된다. raise → 호출자가 미체결로 취급.
            if status == 401 and auth and not oauth and _reauth:
                self._token = None
                self._token_expiry = None
                self.connect()
                if not retry:
                    raise TossAPIError("reauth-no-resend",
                                       "401 후 재인증 성공 — 비멱등 요청(retry=False)은 재전송 안 함",
                                       None, status)
                return self._request(method, path, auth=auth, account=account, params=params,
                                     json_body=json_body, form=form, oauth=oauth, _reauth=False, retry=retry)
            # 종결 — 에러 envelope 파싱 후 raise
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if oauth:
                raise TossAPIError(payload.get("error", "oauth-error"),
                                   payload.get("error_description", ""), None, status)
            err = (payload or {}).get("error") or {}
            raise TossAPIError(err.get("code", "unknown"), err.get("message", ""),
                               err.get("requestId"), status)
        raise TossAPIError("retry-exhausted", "재시도 소진", None, None)

    def _ensure_connected(self):
        if not self._token or (self._token_expiry is not None
                               and time.monotonic() >= self._token_expiry):
            self.connect()

    # ── 인증 ────────────────────────────────────────────────────────────────
    def connect(self) -> None:
        if not (self.api_key and self.api_secret):
            raise TossAPIError("no-credentials", "TOSS_API_KEY / TOSS_API_SECRET 미설정")
        tok = self._request("POST", "/oauth2/token", auth=False, oauth=True, form={
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.api_secret,
        })
        self._token = tok["access_token"]
        # client 당 토큰 1개·refresh 없음. 만료 _TOKEN_REFRESH_MARGIN 초 전 선제갱신. expires_in 결측/0 이면
        # 즉시만료로 계산돼 매 요청마다 재OAuth(+accounts 조회) 하던 것 차단 — 결측 시 폴백(toss_quote 와 동일).
        exp = _num(tok.get("expires_in"), 0)
        ttl = exp if exp > 0 else _TOKEN_TTL_FALLBACK
        self._token_expiry = time.monotonic() + ttl - _TOKEN_REFRESH_MARGIN
        # _reauth=False — 재인증 직후 accounts 가 또 401 이면 connect() 재귀(→RecursionError 크래시) 대신
        # TossAPIError(401, transient) 로 raise 되어 상위(place_order/청산)가 에러카운트→가드 정지로 흡수.
        accts = self._request("GET", "/api/v1/accounts", _reauth=False).get("result", [])
        brokerage = [a for a in accts if a.get("accountType") == "BROKERAGE"]
        if not brokerage:
            raise TossAPIError("no-account", "종합매매(BROKERAGE) 계좌가 없습니다")
        self._account_seq = brokerage[0]["accountSeq"]
        self._account_no = brokerage[0].get("accountNo")

    def disconnect(self) -> None:
        self._token = None
        self._token_expiry = None
        self._account_seq = None

    # ── 조회 ────────────────────────────────────────────────────────────────
    def get_account(self) -> AccountInfo:
        self._ensure_connected()
        bp = self._request("GET", "/api/v1/buying-power", account=True,
                           params={"currency": self.currency}).get("result") or {}
        cash = _num(bp.get("cashBuyingPower"))
        hold = self._request("GET", "/api/v1/holdings", account=True).get("result") or {}
        amount = ((hold.get("marketValue") or {}).get("amount")) or {}
        key = "usd" if self.currency == "USD" else "krw"
        pos_val = _num(amount.get(key))
        return AccountInfo(cash=cash, equity=cash + pos_val, buying_power=cash)

    def get_positions(self) -> list:
        self._ensure_connected()
        hold = self._request("GET", "/api/v1/holdings", account=True).get("result") or {}
        out = []
        for it in hold.get("items", []):
            # ⚠ 전략 유니버스(US) 밖 보유종목은 절대 반환 안 함 — 리밸런서가 청산 대상으로
            # 오인해 사용자의 KR 주식을 팔아치우는 사고 차단.
            if it.get("marketCountry") != self._market_country:
                continue
            out.append(Position(symbol=it["symbol"],
                                qty=_num(it.get("quantity")),
                                avg_price=_num(it.get("averagePurchasePrice"))))
        return out

    def get_quote(self, symbol: str) -> Quote:
        self._ensure_connected()
        res = self._request("GET", "/api/v1/prices",
                            params={"symbols": self._to_toss_symbol(symbol)}).get("result") or []
        if not res:
            raise TossAPIError("no-price", f"{symbol} 현재가 없음")
        lp = res[0].get("lastPrice")
        last = _num(lp, default=0.0)
        if lp is None or not (last > 0) or last in (float("inf"), float("-inf")):   # NaN/inf/0/음수 거부
            # lastPrice 결측/0/NaN/inf 를 흘리면 청산룰(가격<200MA)이 거짓 트리거돼 관리종목을
            # 시장가로 강제 청산함. no-price 와 동일하게 raise → 호출측이 데이터부족=수동확인 처리.
            raise TossAPIError("no-price", f"{symbol} lastPrice 결측/비정상({lp!r})")
        # bid/ask 는 last 로 근사 — 라이브 주문은 MARKET(호가 미사용). 정밀 호가 필요 시 /orderbook.
        return Quote(symbol=symbol, last=last, bid=last, ask=last)

    def market_open(self, country: str = "US") -> bool:
        """지금 해당국 정규장이 열려있나 — MARKET 주문 체결 가능 시간 판정(장중 청산 게이트용).

        /market-calendar/{country} 의 today.regularMarket 윈도(KST)와 현재시각 비교. 휴장이면 False.
        """
        self._ensure_connected()
        cal = self._request("GET", f"/api/v1/market-calendar/{country}").get("result") or {}
        today = cal.get("today") or {}
        rm = today.get("regularMarket")
        if not rm or not rm.get("startTime") or not rm.get("endTime"):
            return False
        try:
            start = datetime.datetime.fromisoformat(rm["startTime"])
            end = datetime.datetime.fromisoformat(rm["endTime"])
            now = datetime.datetime.now(datetime.timezone.utc)   # tz-aware UTC (호스트 로컬TZ 무관)
            # 토스가 naive ISO(타임존 미표기)로 줄 수 있음 → 문서상 KST 윈도이므로 KST(UTC+9) 부여.
            # 안 하면 naive vs aware 비교가 TypeError→캐치→항상 False→장중청산 무성 비활성.
            if start.tzinfo is None:
                kst = datetime.timezone(datetime.timedelta(hours=9))
                start, end = start.replace(tzinfo=kst), end.replace(tzinfo=kst)
            return start <= now <= end
        except (ValueError, TypeError):
            return False

    # ── 주문 ────────────────────────────────────────────────────────────────
    def _fmt_price(self, symbol: str, price: float) -> str:
        if symbol.isdigit():                 # KR 6자리 = 정수(원) 호가
            return str(int(round(price)))
        return f"{price:.2f}" if price >= 1 else f"{price:.4f}"   # US 달러 소수

    @staticmethod
    def _to_toss_symbol(symbol: str) -> str:
        """내부 canonical(하이픈) 심볼 → 토스 전송 표기. 클래스주 'BRK-B'→'BRK.B'(미 표준 점표기 가정).
        get_positions 가 주는 토스 native 표기를 ManagedBroker._norm 이 하이픈으로 canonical 화하므로,
        아웃바운드(주문·시세)는 역변환해야 표기 불일치로 BRK-B 매수·시세가 전량 실패하지 않는다.
        ⚠ 토스 US 클래스주 표기 미확정 — sp100 운영 전 toss_check.py 로 BRK-B/BRK.B 실증 필수.
        끝이 '-<단일대문자>' 인 클래스주만 변환(일반 하이픈 티커 오변환 방지). KMI 등은 영향 없음."""
        return re.sub(r"-([A-Z])$", r".\1", str(symbol).strip().upper())

    def place_order(self, req: OrderRequest) -> Order:
        self._ensure_connected()
        # 결정론 멱등키 — 같은 거래일(ET)·같은 의도(심볼·side·주문유형·지정가·수량/금액)면 같은 키 →
        # 재플랜·운영자 재실행/자동재시도 시 토스가 중복접수 차단. day=ET 날짜(KST 자정 횡단에도 안정 —
        # C1 oversell 방지). order_type·limit_price 포함(의도 다른 동량 주문의 키 충돌 방지: MARKET vs LIMIT, 재가격).
        day = _now_et().date().isoformat()
        amt = getattr(req, "amount", None)
        if amt is not None:
            # 금액주문(orderAmount, $) — 소수주 매수. 토스는 quantity XOR orderAmount. 정규장만 체결.
            a = float(amt)
            if a <= 0:
                return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                             message=f"금액 0 거부 (요청 {amt})")
            # 키에 금액 포함 — 부분체결 후 잔액 재주문이 다른 키로 정상 접수되게(stable-key 면 잔액매수 차단됨).
            cid = hashlib.sha1(
                f"{day}|{req.symbol}|{req.side.value}|{req.order_type.value}"
                f"|{req.limit_price}|amt{a:.2f}".encode()).hexdigest()[:32]
            body = {
                "clientOrderId": cid,
                "symbol": self._to_toss_symbol(req.symbol),
                "side": req.side.value,
                "orderType": req.order_type.value,
                "orderAmount": f"{a:.2f}",
                "timeInForce": "DAY",
            }
        else:
            # 수량 기반. 0/음수는 REJECTED(좀비 포지션·가드 오트립 방지).
            q = float(req.qty)
            if q <= 1e-9:
                return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                             message=f"수량 0 거부 (요청 {req.qty}주; 분수<최소/음수)")
            # 토스는 소수 수량을 'US 시장가 매도(MARKET+SELL)' 에만 허용 — 그 외 소수는 REJECTED(정수 강제).
            is_frac = abs(q - round(q)) > 1e-9
            if is_frac and not (req.side == Side.SELL and req.order_type == OrderType.MARKET):
                return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                             message=f"소수 수량 거부 — 토스는 US 시장가 매도만 소수 허용 (요청 {req.qty})")
            qstr = (f"{q:.6f}".rstrip("0").rstrip(".")) if is_frac else str(int(round(q)))
            cid = hashlib.sha1(
                f"{day}|{req.symbol}|{req.side.value}|{req.order_type.value}"
                f"|{req.limit_price}|{qstr}".encode()).hexdigest()[:32]
            body = {
                "clientOrderId": cid,
                "symbol": self._to_toss_symbol(req.symbol),
                "side": req.side.value,
                "orderType": req.order_type.value,
                "quantity": qstr,                     # 정수 또는 (US 시장가 매도) 소수 문자열.
                "timeInForce": "DAY",                 # 토스 DAY/CLS — 시스템 기본 DAY(GTC 미지원→DAY)
            }
            if req.order_type == OrderType.LIMIT:
                if req.limit_price is None:
                    return Order(order_id="", request=req, status=OrderStatus.REJECTED,
                                 message="지정가 주문에 limit_price 없음")
                body["price"] = self._fmt_price(req.symbol, req.limit_price)
        try:
            res = self._request("POST", "/api/v1/orders", account=True, json_body=body, retry=False).get("result") or {}
        except TossAPIError as e:
            # 4xx 비즈니스거부(잔고부족·잘못된 주문 등)=PaperBroker 처럼 REJECTED 반환. 단 401(토큰만료)은
            # _request 재인증 후에도 실패한 transient → REJECTED 로 흡수하지 않고 raise(상위 에러카운트).
            if e.is_business_error() and e.http_status != 401:
                return Order(order_id="", request=req, status=OrderStatus.REJECTED, message=str(e))
            raise                          # 5xx/네트워크/401 = 전송오류 → 상위에서 에러카운트(가드 정지)
        return Order(order_id=res.get("orderId", ""), request=req,
                     status=OrderStatus.SUBMITTED, message="")

    def cancel_order(self, order_id: str) -> bool:
        self._ensure_connected()
        try:
            self._request("POST", f"/api/v1/orders/{order_id}/cancel", account=True, json_body={})
            return True
        except TossAPIError:
            return False

    def get_order(self, order_id: str) -> Order:
        self._ensure_connected()
        o = self._request("GET", f"/api/v1/orders/{order_id}", account=True).get("result") or {}
        ex = o.get("execution") or {}
        status = _STATUS_MAP.get(o.get("status"), OrderStatus.SUBMITTED)   # unknown=폴링 지속
        # 응답으로 OrderRequest 재구성(보고/일관성용. _await_fills 는 status·체결만 사용).
        side = req_side = o.get("side")
        try:
            from .base import Side
            side = Side(req_side) if req_side in ("BUY", "SELL") else Side.BUY
        except Exception:
            from .base import Side
            side = Side.BUY
        otype = OrderType.LIMIT if o.get("orderType") == "LIMIT" else OrderType.MARKET
        req = OrderRequest(symbol=o.get("symbol", ""), side=side, qty=_num(o.get("quantity")),
                           order_type=otype, limit_price=(_num(o.get("price")) if o.get("price") else None))
        return Order(order_id=order_id, request=req, status=status,
                     filled_qty=_num(ex.get("filledQuantity")),
                     avg_fill_price=_num(ex.get("averageFilledPrice")),
                     message=o.get("status", ""))
