"""거래소 캘린더 유틸 — NYSE 세션 기준 '거래일' 판정 (H2 타임존/휴장일).

naive datetime.now() 대신 미 동부(ET) + NYSE 정규장 캘린더로 거래일 도출.
미장 마감=한국 새벽이라 KST 날짜와 US 세션 날짜가 어긋나는 문제, DST, 공휴일,
조기폐장을 pandas_market_calendars(XNYS) 가 처리.
"""
from datetime import datetime, timezone

import pandas as pd
import pandas_market_calendars as mcal

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover — tzdata 없으면 pytz 폴백
    import pytz
    ET = pytz.timezone("America/New_York")

_NYSE = mcal.get_calendar("XNYS")


def now_et() -> datetime:
    """현재 시각을 미 동부시로 (UTC 경유 — 호스트 로컬TZ 무관)."""
    return datetime.now(timezone.utc).astimezone(ET)


def _schedule(start, end):
    return _NYSE.schedule(start_date=pd.Timestamp(start).strftime("%Y-%m-%d"),
                          end_date=pd.Timestamp(end).strftime("%Y-%m-%d"))


def is_session(d) -> bool:
    """해당 날짜가 NYSE 정규장 세션인지 (주말·공휴일 False)."""
    return not _schedule(d, d).empty


def last_completed_session(asof: datetime = None):
    """ET 기준 '이미 종료된' 가장 최근 정규장 세션 date.

    미장 마감 후 도는 cron 이 거래 대상으로 삼을 세션. 장중이면 직전 세션 반환.
    조기폐장일은 그 날의 실제 close 시각 기준. 반환 None = 최근 12일 세션 없음(비정상).
    """
    asof = asof or now_et()
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc).astimezone(ET)
    s = _schedule(asof.date() - pd.Timedelta(days=12), asof.date())
    if s.empty:
        return None
    closed = s["market_close"][s["market_close"] <= pd.Timestamp(asof)]
    if closed.empty:
        return None
    return closed.index[-1].date()


def is_regular_open(asof: datetime = None) -> bool:
    """지금(ET) NYSE 정규장이 열려있나 — 로컬 캘린더 계산(네트워크 불필요).

    브로커 시장시간 API(toss.market_open) 일시 실패 시 폴백용. 조기폐장(반장)도 반영.
    """
    asof = asof or now_et()
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc).astimezone(ET)
    s = _schedule(asof.date(), asof.date())
    if s.empty:
        return False
    ts = pd.Timestamp(asof)
    return bool(s["market_open"].iloc[0] <= ts <= s["market_close"].iloc[0])


def minutes_since_open(asof: datetime = None):
    """오늘 ET 정규장 개장 후 경과 분 (장중 아니면 None) — 로컬 계산.

    heartbeat 의 '장중 청산 cron 사망' 감지에서 개장 직후 오경보를 막는 가드용.
    """
    asof = asof or now_et()
    if asof.tzinfo is None:
        asof = asof.replace(tzinfo=timezone.utc).astimezone(ET)
    s = _schedule(asof.date(), asof.date())
    if s.empty:
        return None
    ts = pd.Timestamp(asof)
    o, c = s["market_open"].iloc[0], s["market_close"].iloc[0]
    if ts < o or ts > c:
        return None
    return (ts - o).total_seconds() / 60.0


def session_gap(data_date, ref_date) -> int:
    """data_date → ref_date 사이 거래일 간격 (0=동일/미래, 1=한 세션 stale ...).

    staleness 판정용. data 가 ref 보다 최신이면(미래) 0.
    """
    a = pd.Timestamp(data_date).normalize()
    b = pd.Timestamp(ref_date).normalize()
    if a >= b:
        return 0
    return max(0, len(_schedule(a, b)) - 1)
