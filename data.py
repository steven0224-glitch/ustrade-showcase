"""yfinance 데이터 로더 + CSV 캐시.

단일 종목 OHLCV 를 받아 표준 컬럼(Open/High/Low/Close/Volume)으로 정규화.
캐시는 data_cache/ 에 CSV 로 저장 (pyarrow 불필요).
"""
import os
import shutil
import time
from pathlib import Path
import certifi
import pandas as pd

# libcurl(yfinance→curl_cffi)은 한글 유저명 경로의 CA 번들을 못 읽음(error 77).
# certifi 경로가 비ASCII면 ASCII 경로(C:\Users\Public)로 복사 후 env 지정.
_ca = certifi.where()
if not _ca.isascii():
    _safe_dir = r"C:\Users\Public\.ustrade"
    os.makedirs(_safe_dir, exist_ok=True)
    _safe_ca = os.path.join(_safe_dir, "cacert.pem")
    if not os.path.exists(_safe_ca):
        # 동시기동(여러 페르소나)이 같은 머신-전역 경로를 동시 복사해도 torn PEM 방지 —
        # per-pid tmp + 원자교체(멱등: 먼저 쓴 쪽이 이김, 정적 인증서라 내용 동일). paths import 前이라 os.replace 직접.
        _tmp_ca = f"{_safe_ca}.{os.getpid()}.tmp"
        try:
            shutil.copy(_ca, _tmp_ca)
            os.replace(_tmp_ca, _safe_ca)
        except OSError:
            try:
                os.remove(_tmp_ca)
            except OSError:
                pass
    os.environ["CURL_CA_BUNDLE"] = _safe_ca
    os.environ["SSL_CERT_FILE"] = _safe_ca

import yfinance as yf  # noqa: E402  (cert env 설정 후 import)

from paths import DATA_CACHE  # noqa: E402
from logsetup import get_logger  # noqa: E402

CACHE_DIR = str(DATA_CACHE)
_log = get_logger("data")


def _purge_legacy(ticker: str, start: str):
    """옛 캐시키({ticker}_{start}_{end}.csv) 잔재 + 교체실패 orphan(.tmp) 제거."""
    try:
        for p in Path(CACHE_DIR).glob(f"{ticker}_{start}_*.csv"):
            p.unlink()
        for p in Path(CACHE_DIR).glob(f"{ticker}_{start}.csv.{os.getpid()}.tmp"):
            try:
                p.unlink()   # 자기 pid orphan 만 (타 pid live tmp 안 건드림 — POSIX cross-pid unlink 레이스 방지)
            except OSError:
                pass
    except OSError:
        pass


def load(ticker: str, start: str, end: str, force: bool = False) -> pd.DataFrame:
    os.makedirs(CACHE_DIR, exist_ok=True)
    # 캐시키에서 end 제외 — end(=세션+1)가 매일 바뀌어 ① 날짜 넘어 캐시 히트 0(매 실행 재다운로드)
    # ② 옛 CSV 무한 누적 하던 문제(M-A). 종목·start 당 단일 파일에 최장 히스토리 저장 후 슬라이스.
    path = os.path.join(CACHE_DIR, f"{ticker}_{start}.csv")
    need_through = pd.Timestamp(end) - pd.Timedelta(days=1)   # end 는 exclusive → 마지막 필요 봉
    if os.path.exists(path) and not force:
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
            if getattr(df.index, "tz", None) is not None:   # 옛 tz-aware 캐시 → naive 정규화
                df.index = df.index.tz_localize(None)
            if not df.empty and df.index.max() >= need_through:   # 캐시가 요청 끝까지 커버
                return df[df.index < pd.Timestamp(end)]
        except Exception:
            pass   # 캐시 손상/형식이상 → 재다운로드

    # 일시적 네트워크/레이트 실패 재시도 (무인 새벽 실행서 한 번 끊겨 거래 누락되는 것 방지)
    raw, last_err = None, None
    for attempt in range(3):
        try:
            raw = yf.download(ticker, start=start, end=end, auto_adjust=True, progress=False)
            if raw is not None and len(raw) > 0:
                break
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(1.0 * (attempt + 1))
    if raw is None or len(raw) == 0:
        raise ValueError(f"데이터 없음/다운로드 실패: {ticker} {start}~{end} ({last_err})")

    # yfinance 신버전은 MultiIndex 컬럼 (Price, Ticker) 반환 → 1레벨로 평탄화
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    # 인덱스 tz-naive 강제 — yfinance 버전에 따라 tz-aware(UTC) 일봉을 줄 때가 있어 신선도
    # 비교(df.index.max() >= need_through)·session_gap 이 tz-aware vs naive 로 TypeError 나는 것 방지.
    raw.index = pd.DatetimeIndex(raw.index)
    if raw.index.tz is not None:
        raw.index = raw.index.tz_localize(None)

    df = raw[["Open", "High", "Low", "Close", "Volume"]].dropna()
    df.index.name = "Date"
    # 원자적 캐시 쓰기 — per-pid tmp + 재시도 교체. 공유 캐시(USTRADE_CACHE_HOME) 동시쓰기서
    # reader torn-read + writer PermissionError(Windows 동시접근) 둘 다 흡수. 실패해도 df 는 반환(다음 실행 재생성).
    from paths import atomic_replace
    _tmp = f"{path}.{os.getpid()}.tmp"
    df.to_csv(_tmp)
    atomic_replace(_tmp, path)
    _purge_legacy(ticker, start)
    return df[df.index < pd.Timestamp(end)]


def load_panel(tickers, start: str, end: str, force: bool = False,
               max_fail_frac: float = 0.2) -> pd.DataFrame:
    """다종목 종가 패널 (index=날짜, columns=티커). 종목별 단일 캐시 재사용.

    실패율이 max_fail_frac 초과면 raise — 데이터 피드 이상으로 유니버스가 조용히
    축소돼 선택이 왜곡되는 것을 차단 (H4). 소수 실패는 print 후 진행.
    """
    tickers = list(tickers)
    closes, failed = {}, []
    for t in tickers:
        try:
            closes[t] = load(t, start, end, force=force)["Close"]
        except Exception as e:
            failed.append(t)
            _log.warning("스킵 %s: %s", t, e)
    if failed and len(failed) / len(tickers) > max_fail_frac:
        raise ValueError(f"다운로드 실패율 {len(failed)}/{len(tickers)} > {max_fail_frac:.0%} "
                         f"— 데이터 피드 이상 의심: {failed}")
    if not closes:
        raise ValueError("패널 비어있음 — 다운로드 전부 실패")
    panel = pd.DataFrame(closes).dropna(how="all")
    return panel


_OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume")


def load_ohlcv_panel(tickers, start: str, end: str, force: bool = False,
                     max_fail_frac: float = 0.2) -> dict:
    """다종목 OHLCV 패널 dict — alpha_zoo 연산자용.

    반환: {"open","high","low","close","volume","vwap"} 각각 wide DataFrame
    (index=날짜, columns=티커). vwap 은 (O+H+L+C)/4 전형가 — Vibe base.vwap 의
    equity_us 폴백과 동일(장중 vwap 없음). load_panel 과 같은 캐시/실패율 정책.
    """
    tickers = list(tickers)
    frames, failed = {}, []
    for t in tickers:
        try:
            df = load(t, start, end, force=force)
            frames[t] = df[list(_OHLCV_FIELDS)]
        except Exception as e:
            failed.append(t)
            _log.warning("스킵 %s: %s", t, e)
    if failed and len(failed) / len(tickers) > max_fail_frac:
        raise ValueError(f"다운로드 실패율 {len(failed)}/{len(tickers)} > {max_fail_frac:.0%} "
                         f"— 데이터 피드 이상 의심: {failed}")
    if not frames:
        raise ValueError("패널 비어있음 — 다운로드 전부 실패")

    panel = {}
    for field, key in zip(_OHLCV_FIELDS, ("open", "high", "low", "close", "volume")):
        panel[key] = pd.DataFrame({t: f[field] for t, f in frames.items()}).dropna(how="all")
    panel["vwap"] = (panel["open"] + panel["high"] + panel["low"] + panel["close"]) / 4.0
    return panel
