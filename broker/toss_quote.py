"""TossQuoteClient — 토스 실시간 *호가 전용* 클라이언트 (장중 루프용).

장중 액티브 트레이딩 루프(run_intraday.py)가 실시간 스팟 호가로 진입·손절을 판정한다.
yfinance 무료피드는 ~15분 지연이라 장중 손절이 늦게 발화 → 실시스템과 발산. Toss /prices
는 실시간 lastPrice 를 준다(단 분봉·거래량 없음 — 루프가 스팟을 샘플링해 자체 1분봉 합성,
컨비션은 가격 velocity 로 프록시).

⚠️ **구조적 안전 — 실주문 0 보장**: 이 클래스는 `place_order`/`cancel_order` 메서드를
   *코드에 아예 갖지 않는다*. 장중 루프는 Toss 인증 세션을 호가용으로 쥐지만, 체결은
   PaperBroker 로만 한다. TossBroker(주문 가능)를 상속/합성하지 않으므로 — 루프 코드의
   어떤 버그도 이 객체로 실주문을 낼 수 없다(설정이 아니라 타입 구조로 보장). 매매 자격증명은
   비청취·헤드리스 루프에만 — 대시보드(tailnet 청취)엔 절대 들어가지 않는다.

키: 환경변수 TOSS_API_KEY / TOSS_API_SECRET (하드코딩 금지). /prices 는 account 헤더 불요
   → connect 는 OAuth 토큰만 발급(계좌 선택 생략).
"""
import os
import time

from .base import Quote
from .toss import (_num, TossAPIError, DEFAULT_BASE_URL, TossBroker,
                   _TOKEN_TTL_FALLBACK, _TOKEN_REFRESH_MARGIN)


class TossQuoteClient:
    """토스 실시간 호가 전용 — connect(토큰) + get_quote/last. 주문 메서드 부재."""

    def __init__(self, api_key: str = None, api_secret: str = None, base_url: str = None,
                 timeout: float = 10.0, max_retries: int = 2, session=None):
        self.api_key = api_key or os.environ.get("TOSS_API_KEY")
        self.api_secret = api_secret or os.environ.get("TOSS_API_SECRET")
        self.base_url = (base_url or os.environ.get("TOSS_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._token = None
        self._token_expiry = None    # time.monotonic() 기준
        if session is not None:
            self._session = session
        else:
            import requests
            self._session = requests.Session()

    # ── 인증 (토큰만 — /prices 는 account 불요) ──────────────────────────────
    def connect(self) -> None:
        if not (self.api_key and self.api_secret):
            raise TossAPIError("no-credentials", "TOSS_API_KEY / TOSS_API_SECRET 미설정")
        tok = self._post_token()
        self._token = tok["access_token"]
        # client 당 토큰 1개·refresh 없음. 만료 _TOKEN_REFRESH_MARGIN 초 전 선제갱신(TossBroker 와 동일 정책).
        # expires_in 결측/0 이면 즉시만료로 계산돼 *매 호가 호출마다 재OAuth* 하던 것 차단 — 결측 시 폴백으로
        # 처리(장중 hot-path 스래싱 방지; 폴백은 실토큰수명보다 짧아 선제갱신이 실만료 前 발화).
        exp = _num(tok.get("expires_in"), 0)
        ttl = exp if exp > 0 else _TOKEN_TTL_FALLBACK
        self._token_expiry = time.monotonic() + ttl - _TOKEN_REFRESH_MARGIN

    def _ensure_connected(self):
        if not self._token or (self._token_expiry is not None
                               and time.monotonic() >= self._token_expiry):
            self.connect()

    def _post_token(self) -> dict:
        import requests
        url = self.base_url + "/oauth2/token"
        form = {"grant_type": "client_credentials",
                "client_id": self.api_key, "client_secret": self.api_secret}
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.request("POST", url, headers={}, params=None,
                                             json=None, data=form, timeout=self._timeout)
            except requests.RequestException as e:
                if attempt < self._max_retries:
                    time.sleep(attempt + 1)
                    continue
                raise TossAPIError("network-error", str(e), None, None)
            if 200 <= resp.status_code < 300:
                return resp.json()
            if resp.status_code >= 500 and attempt < self._max_retries:
                time.sleep(attempt + 1)
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            raise TossAPIError(payload.get("error", "oauth-error"),
                               payload.get("error_description", ""), None, resp.status_code)
        raise TossAPIError("retry-exhausted", "토큰 재시도 소진", None, None)

    def _get(self, path, params=None, _reauth=True):
        """GET (auth, account 불요). 429/5xx/네트워크 재시도. 401→1회 재인증 후 재시도."""
        import requests
        if not self._token:
            raise TossAPIError("not-connected", "connect() 를 먼저 호출하세요")
        # 선제 갱신 — 요청 조립 직전 만료 확인(진입→전송 창의 401 누출 차단; TossBroker._request 와 동일).
        if _reauth and self._token_expiry is not None and time.monotonic() >= self._token_expiry:
            self._token = None
            self._token_expiry = None
            self.connect()
        url = self.base_url + path
        headers = {"Authorization": f"Bearer {self._token}"}
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.request("GET", url, headers=headers, params=params,
                                             json=None, data=None, timeout=self._timeout)
            except requests.RequestException as e:
                if attempt < self._max_retries:
                    time.sleep(attempt + 1)
                    continue
                raise TossAPIError("network-error", str(e), None, None)
            status = resp.status_code
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            if status == 429 and attempt < self._max_retries:
                time.sleep(_num(resp.headers.get("Retry-After"), attempt + 1))
                continue
            if status >= 500 and attempt < self._max_retries:
                time.sleep(attempt + 1)
                continue
            if status == 401 and _reauth:   # 토큰 만료 → 1회 재인증 후 재시도
                self._token = None
                self._token_expiry = None
                self.connect()
                return self._get(path, params=params, _reauth=False)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            err = (payload or {}).get("error") or {}
            raise TossAPIError(err.get("code", "unknown"), err.get("message", ""),
                               err.get("requestId"), status)
        raise TossAPIError("retry-exhausted", "재시도 소진", None, None)

    # ── 호가 ─────────────────────────────────────────────────────────────────
    def get_quote(self, symbol: str) -> Quote:
        """실시간 현재가 → Quote. lastPrice 결측/0 은 raise(0.0 흘리면 거짓 손절 — TossBroker 와 동일)."""
        self._ensure_connected()
        res = self._get("/api/v1/prices",
                        params={"symbols": TossBroker._to_toss_symbol(symbol)}).get("result") or []
        if not res:
            raise TossAPIError("no-price", f"{symbol} 현재가 없음")
        lp = res[0].get("lastPrice")
        last = _num(lp, default=0.0)
        if lp is None or not (last > 0) or last in (float("inf"), float("-inf")):   # NaN/inf/0/음수 거부(코드베이스 표준)
            raise TossAPIError("no-price", f"{symbol} lastPrice 결측/비정상({lp!r})")
        # /prices 는 호가창 미제공 — bid/ask 를 last 로 근사(루프는 스팟만 사용).
        return Quote(symbol=symbol, last=last, bid=last, ask=last)

    def last(self, symbol: str) -> float:
        """현재가 스칼라 — PaperBroker price_fn 주입용(장중 체결가 = 실시간 스팟)."""
        return self.get_quote(symbol).last
