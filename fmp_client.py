"""FMP 클라이언트 — 무료티어(stable) 엔드포인트 + 디스크 캐시 + 레이트 배려.

키 로드 순서: 환경변수 FMP_API_KEY → C:\\Users\\...\\.config\\fmp_api.key
캐시: fmp_cache/ (250req/day 절약 — 같은 요청 재호출 안 함).
무료 가능: quote, profile, ratios-ttm, key-metrics-ttm, earnings, historical-price-eod.
유료(402): ratios/key-metrics/income period=quarter (과거 시점 펀더멘털).
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

import requests

from logsetup import get_logger
from paths import FMP_CACHE

BASE = "https://financialmodelingprep.com/stable"
KEY_FILE = Path(os.path.expanduser("~")) / ".config" / "fmp_api.key"
CACHE_DIR = FMP_CACHE

_log = get_logger("fmp_client")

# 만료캐시 폴백 사용 집계(프로세스 전역) — 런 저널이 읽어 '며칠 지난 펀더로 선정했는지' 표면화.
# 런 = 원샷 프로세스라 리셋 불필요(누적=이번 런 사용량).
STALE_HITS = 0
STALE_MAX_AGE_D = 0.0
STALE_REJECTS = 0        # 나이 상한 초과로 '결측' 처리한 캐시 수(P2-A5)


def load_key() -> str:
    k = os.environ.get("FMP_API_KEY")
    if k:
        return k.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8-sig").strip()   # BOM 허용
    raise RuntimeError(f"FMP 키 없음 — env FMP_API_KEY 또는 {KEY_FILE}")


class RateLimited(PermissionError):
    """레이트/쿼터(429·402) 재시도 소진 — 만료 캐시로 폴백 가능한 일시적 실패."""


class FMP:
    def __init__(self, min_interval: float = 0.5, retry_402: int = 3,
                 cache_ttl_days: float = 7.0, stale_max_days: float = 90.0):
        self.key = load_key()
        # 무료티어 일일쿼터 한계(실측: 402 가 호출간격 무관 — 4s 75%/8s 62%, 누적콜수에 의존). 빠른배치 +
        # 재시도(retry_402=3)가 종목당 4배 증폭으로 쿼터를 태움 → 펀더 전멸·screen_degraded.
        # env 로 호출측 무수정 조정 — paper 페르소나 태스크가 FMP_RETRY_402=0(즉시스킵, 쿼터낭비0)·
        # FMP_MIN_INTERVAL=2 설정. 기본값(0.5s·재시도3)은 실거래/백테스트 불변. 7일 캐시라 스킵분은 다음 런이 채워 자가치유.
        self.min_interval = float(os.environ.get("FMP_MIN_INTERVAL") or min_interval)
        try:
            self.retry_402 = max(0, int(os.environ.get("FMP_RETRY_402") or retry_402))   # 음수/비숫자 방어(음수→_fetch 빈 range→None)
        except (TypeError, ValueError):
            self.retry_402 = retry_402   # 402=쿼터/버스트. paper=0(즉시스킵), 기본3
        # TTL — ratios/key-metrics-ttm 캐시가 한 번 쓰이면 영영 갱신 안 되던 문제(M-B).
        # 펀더는 분기성이라 7일이면 충분히 신선 + 레이트 절약. 만료 시 재호출, 실패하면 stale 폴백.
        # env FMP_CACHE_TTL_DAYS 로 호출측 무수정 조정(paper 페르소나=30일 → 워밍 후 재호출 거의 0). 기본 불변.
        try:
            cache_ttl_days = float(os.environ.get("FMP_CACHE_TTL_DAYS") or cache_ttl_days)
        except (TypeError, ValueError):
            pass
        self.cache_ttl = cache_ttl_days * 86400.0
        # 만료캐시 폴백 나이 상한(P2-A5) — 상한 없이는 90일·1년 전 펀더로 '스크린 통과' 를
        # 계속 위장한다(TTL 은 재호출 주기일 뿐 폴백 나이를 안 막음). 초과분은 결측 취급 →
        # 호출측이 missing 으로 분류 → A4 degraded 경로에 자연 합류(정책 일관).
        # 0/음수 = 무상한(종전 동작). env FMP_STALE_MAX_DAYS 로 호출측 무수정 조정.
        try:
            stale_max_days = float(os.environ.get("FMP_STALE_MAX_DAYS") or stale_max_days)
        except (TypeError, ValueError):
            pass
        self.stale_max_days = stale_max_days if stale_max_days > 0 else None
        self._last = 0.0
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def get(self, endpoint: str, cache: bool = True, **params):
        params = {k: v for k, v in params.items() if v is not None}
        ck = hashlib.md5(f"{endpoint}|{sorted(params.items())}".encode()).hexdigest()
        cf = CACHE_DIR / f"{endpoint.replace('/', '_')}_{ck}.json"
        have_cache, fresh = cache and cf.exists(), False
        if have_cache:
            try:
                fresh = (time.time() - cf.stat().st_mtime) < self.cache_ttl
            except OSError:
                have_cache = False
        if fresh:
            try:
                return json.loads(cf.read_text(encoding="utf-8"))   # TTL 내 → 캐시 사용
            except (OSError, ValueError):
                have_cache = False   # 손상 캐시(파싱 실패) — 만료 취급, 재fetch → 실패 시 아래 have_cache 폴백 로직이 처리
        try:
            data = self._fetch(endpoint, params)
        except RateLimited:
            if have_cache:   # 레이트/쿼터로 갱신 실패 → 만료 캐시라도 사용(무스크린 거래로의 조용한 추락 방지)
                global STALE_HITS, STALE_MAX_AGE_D, STALE_REJECTS
                try:
                    age_d = (time.time() - cf.stat().st_mtime) / 86400.0
                except OSError:
                    age_d = None
                if self.stale_max_days is not None and age_d is not None \
                        and age_d > self.stale_max_days:
                    STALE_REJECTS += 1
                    _log.warning("만료캐시 나이 상한 초과 — 결측 처리: %s (%.1f일 > %.0f일)",
                                 endpoint, age_d, self.stale_max_days)
                    raise
                # 만료캐시 사용을 프로세스 전역으로 집계 — run_live 가 저널 selection 에 부착해
                # '몇 일 지난 펀더로 골랐는지' 표면화(30일 캐시로 조용히 거래되던 것 가시화).
                STALE_HITS += 1
                if age_d is not None:
                    STALE_MAX_AGE_D = max(STALE_MAX_AGE_D, age_d)
                    _log.info("만료캐시 폴백: %s (%.1f일 경과)", endpoint, age_d)
                return json.loads(cf.read_text(encoding="utf-8"))
            raise
        if cache:
            from paths import atomic_replace
            tmp = cf.parent / f"{cf.name}.{os.getpid()}.tmp"   # per-pid tmp + 재시도 교체(공유 fmp_cache 안전)
            try:
                tmp.write_text(json.dumps(data), encoding="utf-8")
                atomic_replace(str(tmp), str(cf))
            except Exception:
                try:
                    tmp.unlink()   # write 실패 시 부분기록 tmp 정리(fmp_cache reaper 부재 보완)
                except OSError:
                    pass
                # 캐싱은 best-effort — 디스크 hiccup 으로 이미 fetch(레이트쿼터 소비)한 유효 응답을 버리지 않음
        return data

    def _fetch(self, endpoint: str, params: dict):
        url = f"{BASE}/{endpoint}"
        for attempt in range(self.retry_402 + 1):
            dt = time.monotonic() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            try:
                r = requests.get(url, params={**params, "apikey": self.key}, timeout=30)
            except requests.RequestException as ce:
                # 연결/타임아웃 예외 메시지에 apikey(URL) 노출 가능 → 마스킹 후 재발생(방어심층)
                raise requests.RequestException(
                    re.sub(r"(apikey|apiKey)=[^&\s]+", r"\1=***", str(ce))) from None
            self._last = time.monotonic()
            if r.status_code == 401:
                raise PermissionError("401 키 무효 — 활성화/재발급 확인")
            if r.status_code == 429:                  # 레이트(버스트) 초과 — 백오프 재시도
                if attempt < self.retry_402:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                raise RateLimited(f"429 (재시도 소진) 레이트 초과: {endpoint}")
            if r.status_code == 402:
                if attempt < self.retry_402:
                    time.sleep(1.5 * (attempt + 1))   # 백오프
                    continue
                raise RateLimited(f"402 (재시도 소진) PREMIUM/쿼터: {endpoint}")
            try:
                r.raise_for_status()
            except requests.HTTPError as he:
                # 에러 메시지에 URL+apikey 가 들어가므로 키 마스킹 후 재발생 (로그 유출 방지)
                raise requests.HTTPError(re.sub(r"(apikey|apiKey)=[^&\s]+", r"\1=***",
                                                str(he))) from None
            try:
                data = r.json()
            except ValueError:               # 200 인데 JSON 파싱 실패(HTML 에러페이지 등) → 일시실패(만료캐시 폴백 가능)
                raise RateLimited(f"JSON 파싱 실패(200): {endpoint}") from None
            # FMP 무료티어는 200 으로 {"Error Message": ...} 에러봉투를 주기도 함(쿼터/요청오류).
            # 이걸 캐시하면 7일간 빈 펀더 → 스크린 무력화 조용히 지속(DATA-2). 캐시 말고 일시실패로
            # 처리 → 만료캐시 폴백 가능, 없으면 호출측이 missing 처리.
            if isinstance(data, dict) and ("Error Message" in data or "error" in data):
                raise RateLimited(f"FMP 에러봉투(200): {endpoint}")
            return data

    # --- 편의 ---
    def earnings(self, symbol, limit=40):
        # stable/earnings 의 limit 은 PREMIUM 전용 파라미터(무료/스타터 티어는 402 "Premium Query
        # Parameter"). 미전송하고 클라이언트단에서 최근 limit 개로 절단(과거순 정렬 후). limit=0/None=전체.
        rows = self.get("earnings", symbol=symbol)
        if isinstance(rows, list) and limit:
            rows = sorted(rows, key=lambda r: r.get("date") or "", reverse=True)[:limit]
        return rows

    def ratios_ttm(self, symbol):
        d = self.get("ratios-ttm", symbol=symbol)
        return d[0] if d else {}

    def key_metrics_ttm(self, symbol):
        d = self.get("key-metrics-ttm", symbol=symbol)
        return d[0] if d else {}

    def profile(self, symbol):
        d = self.get("profile", symbol=symbol)
        return d[0] if d else {}
