"""KISQuoteClient — 한국투자증권(KIS Developers) 해외주식 시세·누적거래량 *조회 전용* 클라이언트.

용도: Toss /prices 는 거래량 미제공 → 장중 루프가 KIS 무료 실시간(미국 0분지연, 나스닥 마켓센터)
      REST 현재체결가(HHDFS00000300)의 tvol(당일 누적 거래량)을 분당 폴링해 분당 거래량을 산출,
      **관찰 전용 섀도 저널**(volume_shadow.jsonl)에 축적한다. 매매 결정경로 미접촉 — 다이버전스/
      볼륨 게이트는 오프라인 백테스트 통과 前까지 룰에 배선하지 않는다(하우스 룰).

누적 tvol 을 diff 하므로 폴링 주기와 무관하게 분당 거래량이 정확(놓치는 체결 없음) — 웹소켓
(HDFSCNT0 체결 스트림) 불필요. 소스가 나스닥 마켓센터 단독(합산 테이프의 ~50% 표본)이라
절대치는 과소집계 — 동일 피드로 히스토리를 쌓아 상대 비교(상대거래량·OBV)에만 쓰는 전제.

⚠️ 구조적 안전 — 실주문 0 보장: place_order/cancel_order 메서드를 코드에 아예 갖지 않는다
   (TossQuoteClient 와 동일 패턴). 시세 조회 외 어떤 계좌 조작도 불가.

키: 환경변수 KIS_APP_KEY / KIS_APP_SECRET (미설정 → from_env() 가 None = 섀도 비활성).
    KIS_BASE 로 도메인 재정의 가능(기본 실전 https://openapi.koreainvestment.com:9443).

토큰: KIS 는 발급 레이트 제한이 있어(과발급 시 EGW00133) 디스크 캐시(kis_token.json)를
    프로세스 간 공유 — 재기동·다중 태스크가 토큰을 재발급하지 않고 재사용한다.
    만료 기록은 응답의 실만료(access_token_token_expired)로 클램프 — KIS 는 유효기간 내
    재요청에 '기존 토큰'을 반환하면서 expires_in 은 전체값(86400)으로 줘서, 그대로 믿으면
    만료 과대평가로 죽은 토큰을 계속 쓴다(2026-07-22 개장 65분 EGW00123 결손의 근본원인).
    토큰 오류(EGW00121/00123)는 HTTP 상태코드 무관 강제 재발급 1회로 자가회복.

거래소코드(EXCD): 심볼별 NAS→NYS→AMS 순서로 시도해 성공 코드를 디스크 캐시(자가해결,
    하드코딩 맵 불요). 잘못된 거래소는 output.last 가 공란으로 옴.

필드 출처: 공식 github.com/koreainvestment/open-trading-api examples_llm/overseas_stock/price
    (HHDFS00000300, output.last/tvol). 키 수령 후 `python broker/kis_quote.py --smoke NVDA` 로
    실응답 필드 검증할 것.
"""
import json
import os
import time

TOKEN_PATH = "/oauth2/tokenP"
PRICE_PATH = "/uapi/overseas-price/v1/quotations/price"
PRICE_TR_ID = "HHDFS00000300"
DEFAULT_BASE = "https://openapi.koreainvestment.com:9443"
EXCD_CANDIDATES = ("NAS", "NYS", "AMS")
TOKEN_ERR_CODES = ("EGW00121", "EGW00123")   # 유효하지 않은 token / 기간이 만료된 token
_TOKEN_TTL_FALLBACK = 6 * 3600       # expires_in 결측 시 보수적 폴백(실제 24h)
_TOKEN_REFRESH_MARGIN = 600          # 만료 10분 전 선제 재발급


class KISAPIError(Exception):
    def __init__(self, code, message="", status=None):
        super().__init__(f"{code}: {message}" + (f" (HTTP {status})" if status else ""))
        self.code, self.message, self.status = code, message, status


def _num(v, default=0.0):
    try:
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else default
    except (TypeError, ValueError):
        return default


class KISQuoteClient:
    """해외주식 현재체결가(last·tvol) 조회 전용 — 주문 메서드 부재."""

    def __init__(self, app_key: str = None, app_secret: str = None, base_url: str = None,
                 timeout: float = 10.0, max_retries: int = 2, session=None,
                 token_file: str = None, excd_file: str = None, sleep_fn=time.sleep,
                 now_fn=time.time):
        self.app_key = app_key or os.environ.get("KIS_APP_KEY")
        self.app_secret = app_secret or os.environ.get("KIS_APP_SECRET")
        self.base_url = (base_url or os.environ.get("KIS_BASE") or DEFAULT_BASE).rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._sleep = sleep_fn
        self._now = now_fn
        self._token = None
        self._token_expiry = None            # epoch 초 (디스크 캐시와 동일 기준)
        if token_file is None or excd_file is None:
            from paths import cache_base
            st = os.path.join(str(cache_base()), "state")
            token_file = token_file or os.path.join(st, "kis_token.json")
            excd_file = excd_file or os.path.join(st, "kis_excd.json")
        self._token_file = token_file
        self._excd_file = excd_file
        self._excd = self._load_json(excd_file) or {}   # sym -> 확인된 EXCD
        if session is not None:
            self._session = session
        else:
            import requests
            self._session = requests.Session()

    @classmethod
    def from_env(cls, **kw):
        """env 키 없으면 None — 호출측이 섀도 자체를 비활성(휴면 모드)."""
        if not (os.environ.get("KIS_APP_KEY") and os.environ.get("KIS_APP_SECRET")):
            return None
        return cls(**kw)

    # ── 디스크 캐시 유틸 ─────────────────────────────────────────────────────
    @staticmethod
    def _load_json(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    @staticmethod
    def _save_json(path, obj):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(obj, f)
            from paths import atomic_replace
            atomic_replace(tmp, path)
        except Exception:
            pass                              # 캐시 실패는 비치명(다음 실행이 재생성)

    # ── 인증 — 토큰 디스크 공유(발급 레이트 제한 대응) ───────────────────────
    def connect(self, force: bool = False) -> None:
        if not (self.app_key and self.app_secret):
            raise KISAPIError("no-credentials", "KIS_APP_KEY / KIS_APP_SECRET 미설정")
        if not force:
            cached = self._load_json(self._token_file)
            if cached and cached.get("app_key") == self.app_key:
                exp = float(cached.get("expiry") or 0)
                if exp - self._now() > _TOKEN_REFRESH_MARGIN and cached.get("access_token"):
                    self._token, self._token_expiry = cached["access_token"], exp
                    return
        payload = self._request("POST", TOKEN_PATH, json_body={
            "grant_type": "client_credentials",
            "appkey": self.app_key, "appsecret": self.app_secret}, auth=False)
        tok = payload.get("access_token")
        if not tok:
            raise KISAPIError("oauth-error", f"access_token 결측: {sorted(payload)}")
        ttl = _num(payload.get("expires_in"), 0) or _TOKEN_TTL_FALLBACK
        expiry = self._now() + ttl
        # KIS 동일토큰 반환(alias) 시 expires_in 은 전체값으로 와 만료를 과대평가한다 —
        # 응답의 실만료 문자열(KST)로 클램프. 포맷이 바뀌면 expires_in 폴백.
        exp_s = str(payload.get("access_token_token_expired") or "").strip()
        if exp_s:
            try:
                from datetime import datetime, timedelta, timezone
                expiry = min(expiry, datetime.strptime(exp_s, "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone(timedelta(hours=9))).timestamp())
            except ValueError:
                pass
        self._token = tok
        self._token_expiry = expiry
        self._save_json(self._token_file, {"app_key": self.app_key, "access_token": tok,
                                           "expiry": self._token_expiry})

    def _ensure_connected(self):
        # 하드 만료에서만 재발급. 선제(margin) 재발급은 KIS 동일토큰 반환 탓에 만료 전 10분간
        # 같은 토큰만 반복 수령한다(무익 + 발급 레이트 소모) — 만료 직후 첫 콜의 토큰 오류는
        # _request/_price_once 의 재발급 그물이 받아 1회 재시도로 회복된다.
        if not self._token or (self._token_expiry is not None
                               and self._now() >= self._token_expiry):
            self.connect()

    # ── HTTP (재시도 + 401 → 강제 재발급 1회) ────────────────────────────────
    def _request(self, method, path, params=None, json_body=None, auth=True, _reauth=True):
        import requests
        url = self.base_url + path
        headers = {"content-type": "application/json; charset=utf-8"}
        if auth:
            headers.update({"authorization": f"Bearer {self._token}",
                            "appkey": self.app_key, "appsecret": self.app_secret,
                            "tr_id": PRICE_TR_ID, "custtype": "P"})
        for attempt in range(self._max_retries + 1):
            try:
                resp = self._session.request(method, url, headers=headers, params=params,
                                             json=json_body, data=None, timeout=self._timeout)
            except requests.RequestException as e:
                if attempt < self._max_retries:
                    self._sleep(attempt + 1)
                    continue
                raise KISAPIError("network-error", str(e))
            status = resp.status_code
            if 200 <= status < 300:
                try:
                    return resp.json()
                except ValueError:
                    return {}
            if status == 429 and attempt < self._max_retries:
                self._sleep(attempt + 1)
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            code = payload.get("msg_cd") or payload.get("error_code") or "http-error"
            # KIS 는 토큰 만료를 401 이 아니라 HTTP 500 + EGW00123 으로도 준다(07-22 실사) —
            # 상태코드 무관, 토큰 오류면 강제 재발급 1회. 비일시 오류라 5xx 재시도보다 먼저.
            if auth and _reauth and (status in (401, 403) or code in TOKEN_ERR_CODES):
                self.connect(force=True)
                return self._request(method, path, params=params, json_body=json_body,
                                     auth=auth, _reauth=False)
            if status >= 500 and attempt < self._max_retries:
                self._sleep(attempt + 1)
                continue
            raise KISAPIError(code, payload.get("msg1") or payload.get("error_description", ""),
                              status)
        raise KISAPIError("retry-exhausted", "재시도 소진")

    # ── 시세 — last + tvol(당일 누적 거래량) ─────────────────────────────────
    def _price_once(self, symbol: str, excd: str, _retok: bool = True) -> dict:
        self._ensure_connected()
        payload = self._request("GET", PRICE_PATH,
                                params={"AUTH": "", "EXCD": excd, "SYMB": symbol})
        if str(payload.get("rt_cd", "")) != "0":
            code = payload.get("msg_cd", "rt_cd!=0")
            if _retok and code in TOKEN_ERR_CODES:   # HTTP 200 이어도 토큰 오류면 재발급 1회
                self.connect(force=True)
                return self._price_once(symbol, excd, _retok=False)
            raise KISAPIError(code, payload.get("msg1", ""), None)
        return payload.get("output") or {}

    def get_snapshot(self, symbol: str) -> dict:
        """{'last','tvol','tamt','excd'}. tamt=당일 누적 거래대금 — 분당 delta 로 분당 VWAP
        (=Δtamt/Δtvol) 도출용. 거래소코드는 NAS→NYS→AMS 자가해결·캐시.
        last 공란/0 = 그 거래소에 없는 심볼(다음 후보 시도). 전 후보 실패 → raise."""
        tried = []
        cands = [self._excd[symbol]] if symbol in self._excd else list(EXCD_CANDIDATES)
        for excd in cands:
            try:
                out = self._price_once(symbol, excd)
            except KISAPIError as e:
                tried.append(f"{excd}:{e.code}")
                continue
            last = _num(out.get("last"), 0.0)
            if last > 0:
                if self._excd.get(symbol) != excd:              # 성공 코드 캐시(자가해결)
                    self._excd[symbol] = excd
                    self._save_json(self._excd_file, self._excd)
                return {"last": last, "tvol": _num(out.get("tvol"), 0.0),
                        "tamt": _num(out.get("tamt"), 0.0), "excd": excd}
            tried.append(f"{excd}:empty")
        if symbol in self._excd:                                # 캐시가 틀렸을 수 있음 → 전 후보 재시도
            del self._excd[symbol]
            remaining = [c for c in EXCD_CANDIDATES if f"{c}:empty" not in tried
                         and not any(t.startswith(c + ":") for t in tried)]
            if remaining:
                return self.get_snapshot(symbol)
        raise KISAPIError("no-price", f"{symbol} 전 거래소 실패({', '.join(tried)})")


class VolumeShadow:
    """분당 거래량 섀도 수집기 — 매 분 경계에서 전 심볼 tvol 을 폴링해 직전 분과의 delta 를
    저널(jsonl)에 축적. **관찰 전용** — 매매 경로에 아무것도 주입하지 않는다.

    delta = tvol(이번 경계) - tvol(직전 경계). 누적치 diff 라 폴링 지연·누락에 무손실
    (경계 귀속만 ±폴링주기 오차). tvol 감소(새 세션/피드 리셋)는 delta=None 으로 방어.
    심볼 단위 실패 격리 — 한 심볼 오류가 나머지 수집을 막지 않음."""

    MAX_BYTES = 20_000_000            # 저널 20MB 회전(runs.jsonl 5MB 정책의 볼륨 버전)
    BACKUPS = 12                      # 20MB×12 ≈ 240MB 보존(~8개월). 단일 백업이면 회전 때마다 소실 —
                                      # 매물대/VWAP 오프라인 검증용 히스토리를 위해 다중 백업(2026-08-28)

    def __init__(self, client, symbols, journal_path: str = None,
                 now_fn=time.time, sleep_fn=time.sleep, bucket_seconds: int = 60,
                 spacing: float = 0.05):
        self.client = client
        self.symbols = list(symbols)
        if journal_path is None:
            from paths import cache_base
            journal_path = os.path.join(str(cache_base()), "logs", "volume_shadow.jsonl")
        self.journal_path = journal_path
        self._now = now_fn
        self._sleep = sleep_fn
        self._w = int(bucket_seconds)
        self._spacing = spacing
        self._bucket = None               # 마지막 처리한 분 버킷
        self._tvol = {}                   # sym -> 직전 경계 tvol
        self._tamt = {}                   # sym -> 직전 경계 tamt(누적 거래대금) — 분당 VWAP 원자료

    def tick(self, ts: float):
        """루프 매 샘플 호출 — 분 경계를 넘었을 때만 실동작(그 외 no-op)."""
        b = int(ts // self._w)
        if self._bucket == b:
            return
        first = self._bucket is None
        prev_start = (b if first else self._bucket) * self._w   # 이 delta 가 귀속되는 분(버킷 0 falsy 함정 주의)
        self._bucket = b
        for sym in self.symbols:
            try:
                snap = self.client.get_snapshot(sym)
            except Exception as e:
                import sys
                print(f"[vol-shadow] {sym} 조회 실패(스킵): {e!r}", file=sys.stderr)
                continue
            tv, ta = snap["tvol"], snap.get("tamt", 0.0)
            prev, prev_a = self._tvol.get(sym), self._tamt.get(sym)
            self._tvol[sym], self._tamt[sym] = tv, ta
            if first:
                continue                                       # 기준선만 세움(저널 없음)
            vol = tv - prev if (prev is not None and tv >= prev) else None   # 리셋/결손 방어
            vamt = ta - prev_a if (prev_a is not None and ta >= prev_a and vol is not None) else None
            self._journal({"ts": _iso(self._now()), "bucket_start": prev_start, "sym": sym,
                           "last": round(snap["last"], 4), "tvol": tv,
                           "vol": (round(vol, 2) if vol is not None else None),
                           # 분당 거래대금 — 분당 VWAP=vamt/vol (사후 도출, 저널은 원자료만)
                           "vamt": (round(vamt, 2) if vamt is not None else None),
                           "excd": snap["excd"]})
            if self._spacing:
                self._sleep(self._spacing)                     # 레이트 여유(≤26심볼/분, 한도 20/s)

    def _journal(self, rec: dict):
        import sys
        from paths import append_jsonl_rotating
        try:
            append_jsonl_rotating(self.journal_path, rec, max_bytes=self.MAX_BYTES,
                                  backups=self.BACKUPS)   # 다중 백업 — 매물대/VWAP 오프라인 검증용 히스토리 보존
        except Exception as e:
            print(f"[vol-shadow] 저널 실패: {e!r}", file=sys.stderr)


def _iso(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch).isoformat(timespec="seconds")


# ── 스모크 — 키 수령 후 실응답 검증용 (실주문 불가 구조라 안전) ────────────────
if __name__ == "__main__":
    import argparse
    import sys as _sys
    ap = argparse.ArgumentParser(description="KIS 해외주식 시세·누적거래량 스모크")
    ap.add_argument("--smoke", nargs="+", metavar="SYM", help="심볼들 (예: NVDA ORCL)")
    ap.add_argument("--interval", type=float, default=15.0, help="두 샘플 간격(초)")
    a = ap.parse_args()
    if not a.smoke:
        ap.print_help()
        _sys.exit(0)
    c = KISQuoteClient.from_env()
    if c is None:
        print("KIS_APP_KEY / KIS_APP_SECRET 미설정 — env 에 넣고 다시.")
        _sys.exit(1)
    c.connect()
    print(f"토큰 OK (만료 {_iso(c._token_expiry)}). {a.interval:.0f}초 간격 2샘플:")
    s1 = {}
    for sym in a.smoke:
        try:
            s1[sym] = c.get_snapshot(sym)
            print(f"  {sym} [{s1[sym]['excd']}] last={s1[sym]['last']} tvol={s1[sym]['tvol']:,.0f}")
        except Exception as e:
            print(f"  {sym} 실패: {e!r}")
    time.sleep(a.interval)
    for sym in a.smoke:
        try:
            s2 = c.get_snapshot(sym)
            d = s2["tvol"] - s1[sym]["tvol"] if sym in s1 else float("nan")
            print(f"  {sym} last={s2['last']} tvol={s2['tvol']:,.0f}  Δ={d:,.0f}"
                  f"  {'✓ 실시간 흐름' if d > 0 else '(장중인데 Δ=0 이면 지연/휴장 의심)'}")
        except Exception as e:
            print(f"  {sym} 실패: {e!r}")
