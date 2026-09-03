"""대시보드 데이터 생성기 — 실 엔진 상태 + 라이브 지수/뉴스 + 스캐너 계산.

데이터 소스 (우선순위·폴백):
  · Portfolio/요약/Control  : 실 엔진 저널(runs.jsonl) + killswitch + toss_sleeve  → 없으면 시뮬레이션
  · Market 지수             : yfinance 라이브(^GSPC/^IXIC/^DJI/^VIX)               → 실패 시 캐시 내부지표
  · Market Insight 뉴스      : yfinance 종목 뉴스                                    → 실패 시 정적 샘플
  · Radar / AI Analysis     : 라이브 data.load 캐시 모멘텀 스캔 (계좌 무관 — '스캐너' 뷰, 자동 신선화) → 오프라인 시 정적 data_cache 폴백
  · Sparkline               : 캐시 종가 / 저널 equity 곡선

사용:
  python dashboard/build_data.py            # 라이브 시도(지수·뉴스), 실 저널 우선
  python dashboard/build_data.py --offline  # 네트워크 안 씀(지수=내부지표, 뉴스=샘플)
  python dashboard/build_data.py --sim      # 포트폴리오 강제 시뮬(실 저널 무시)
산출물: dashboard/data.js  (window.DASH_DATA = {...})
"""
import argparse
import concurrent.futures as _cf
import glob
import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

# 대시보드 표시 시각은 전부 한국시간(KST) — 원천 ts 는 VM(=UTC) 의 naive datetime.now().
# generated_at 이 +0000 로 VM=UTC 실측 확인됨. naive 는 UTC 로 간주해 Asia/Seoul 로 변환.
_KST = ZoneInfo("Asia/Seoul")
_UTC = ZoneInfo("UTC")


def _kst_dt(dt):
    """datetime → KST-aware. naive(=VM UTC) 는 UTC 로 간주, 오프셋 있으면 존중."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC)
    return dt.astimezone(_KST)


def _kst_tm(iso_ts, fmt="%H:%M"):
    """naive ISO ts(=VM UTC) → KST 표시 문자열. 파싱 실패/짧으면 '--'."""
    s = str(iso_ts or "")
    if len(s) < 11:
        return "--"
    try:
        return _kst_dt(datetime.fromisoformat(s.replace("Z", "+00:00"))).strftime(fmt)
    except Exception:
        return "--"


def _timeout(fn, secs):
    """네트워크 호출 시간제한 — yf 가 멈춰도 빌드가 무한 대기하지 않게(초과 시 raise → 폴백 진행).
    초과 시 워커 스레드는 데몬처럼 버려둠(wait=False) — 다음 호출 블로킹 방지."""
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn).result(timeout=secs)
    finally:
        ex.shutdown(wait=False)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)                      # 프로젝트 모듈(paths 등) import 용
CACHE = os.path.join(ROOT, "data_cache")      # 정적 오프라인 폴백 — 온라인은 data.load(=DATA_CACHE) 우선
OUT = os.environ.get("USTRADE_DASH_DATA") or os.path.join(HERE, "data.js")  # 실데이터 PII — 운영은 비동기(비-OneDrive) 경로로 재지정 권장

START_CAPITAL = 100_000.0
HOLD_LOOKBACK = 21
TOP_HOLDINGS = 3

NAMES = {
    "AAPL": "애플", "MSFT": "마이크로소프트", "GOOGL": "알파벳", "AMZN": "아마존",
    "NVDA": "엔비디아", "META": "메타", "TSLA": "테슬라", "JPM": "JP모건",
    "BAC": "뱅크오브아메리카", "V": "비자", "MA": "마스터카드", "JNJ": "존슨앤드존슨",
    "UNH": "유나이티드헬스", "LLY": "일라이릴리", "PFE": "화이자", "XOM": "엑슨모빌",
    "CVX": "셰브런", "WMT": "월마트", "COST": "코스트코", "PG": "프록터앤드갬블",
    "KO": "코카콜라", "PEP": "펩시코", "HD": "홈디포", "MCD": "맥도날드",
    "NKE": "나이키", "DIS": "디즈니", "CAT": "캐터필러", "BA": "보잉", "SPY": "S&P 500 ETF",
}
SECTORS = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Communication", "AMZN": "Consumer",
    "NVDA": "Semiconductor", "META": "Communication", "TSLA": "Consumer", "JPM": "Financial",
    "BAC": "Financial", "V": "Financial", "MA": "Financial", "JNJ": "Healthcare",
    "UNH": "Healthcare", "LLY": "Healthcare", "PFE": "Healthcare", "XOM": "Energy",
    "CVX": "Energy", "WMT": "Consumer", "COST": "Consumer", "PG": "Staples",
    "KO": "Staples", "PEP": "Staples", "HD": "Retail", "MCD": "Consumer",
    "NKE": "Consumer", "DIS": "Communication", "CAT": "Industrial", "BA": "Industrial",
    "SPY": "Index",
}
_BADGES = ["b-fx", "b-auto", "b-fin", "b-mkt"]


# ───────────────────────── 캐시 로딩 ─────────────────────────
def _freshest_csv(ticker, min_rows=71):
    """같은 티커 캐시 CSV 스냅샷이 여러 개면 '마지막 데이터 날짜가 가장 최근' 인 것 선택.
    옛 _longest_csv 는 행수 최다를 골라 2014-2025(2024-12-31 종료) 장기 CSV 가 신선한
    2022-2026 CSV 를 가려 스캔/모멘텀이 1.5년 묵던 버그. 모멘텀·MA 계산 최소 행수 미만은
    후보 제외하되, 충분한 게 하나도 없으면 티커 유실 방지로 전체에서 freshest 선택."""
    cands = []
    for p in glob.glob(os.path.join(CACHE, f"{ticker}_*.csv")):
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            continue
        n = len(lines) - 1                        # 헤더 제외 행수
        if n < 1:
            continue
        last_date = lines[-1].split(",", 1)[0].strip()   # ISO 날짜 = 사전식 == 시간순
        cands.append((last_date, n, p))
    if not cands:
        return None
    enough = [c for c in cands if c[1] >= min_rows]
    pool = enough or cands
    pool.sort(key=lambda c: (c[0], c[1]))         # 종료일 우선, 동률이면 행수
    return pool[-1][2]


SCAN_START = "2022-01-01"     # data.load 캐시키 안정용 고정 시작일 (롤링 금지 — 매일 새 파일 누적 차단)


def _scan_end_excl():
    """data.load end(exclusive) — 직전 종료 NYSE 세션 +1일. 세션 단위라 같은 날 반복 재다운로드 방지."""
    from datetime import timedelta
    try:
        from calendar_util import last_completed_session
        s = last_completed_session()
        if s is not None:
            return (s + timedelta(days=1)).isoformat()
    except Exception:
        pass
    return (datetime.now(ZoneInfo("America/New_York")).date() + timedelta(days=1)).isoformat()


def _load_ohlcv(tk, allow_net=True):
    """스캐너 가격 OHLCV — 라이브 엔진과 같은 data.load 캐시(자동 신선화) 우선,
    오프라인/다운로드 실패 시 정적 <프로젝트>/data_cache 폴백. 둘 다 없으면 None.
    라이브 캐시(%LOCALAPPDATA%/ustrade/data_cache)로 단일화 → 대시보드 스캔 스테일 클래스 소멸."""
    if allow_net:
        try:
            import data   # cert env 설정 + yfinance 로더 (지연 import — 오프라인 경로 무비용)
            df = data.load(tk, SCAN_START, _scan_end_excl())
            if df is not None and len(df) >= 2:
                return df
        except Exception:
            pass
    path = _freshest_csv(tk)   # 폴백 — 옛 정적 캐시(오프라인/네트워크 실패용)
    if path:
        try:
            return pd.read_csv(path, index_col=0, parse_dates=True).dropna()
        except Exception:
            pass
    return None


def refresh_scan_cache(allow_net=True):
    """스캐너 유니버스(NAMES) 가격을 라이브 data.load 로 갱신해 DATA_CACHE 워밍.
    run_live cron 이 곁들여 호출 — 대시보드 안 열어도 신선 유지. 반환 (성공수, 전체수)."""
    ok = sum(1 for t in NAMES if _load_ohlcv(t, allow_net=allow_net) is not None)
    return ok, len(NAMES)


def load_closes(allow_net=True):
    closes = {}
    for t in NAMES:
        df = _load_ohlcv(t, allow_net=allow_net)
        if df is None:
            continue
        try:
            s = df["Close"].dropna()
            if len(s) > 70:
                closes[t] = s
        except Exception:
            continue
    return closes


def load_ohlcv(allow_net=True):
    """다축 radar용 풀 OHLCV 프레임 dict {tk: df}. load_closes 와 같은 data.load 캐시 재사용
    (1차 pass 가 디스크 캐시 워밍 → 여기선 캐시 히트, 네트워크 추가 비용 ~0)."""
    frames = {}
    for t in NAMES:
        df = _load_ohlcv(t, allow_net=allow_net)
        if df is None:
            continue
        try:
            if len(df["Close"].dropna()) > 70:
                frames[t] = df
        except Exception:
            continue
    return frames


def pct(a, b):
    return (a / b - 1.0) * 100.0 if (b and b == b) else 0.0   # b==b → NaN 차단(b=NaN 통과 시 NaN 전파)


def spark(s, n=30):
    return [round(float(x), 2) for x in s.iloc[-n:].tolist()]


def _downsample(lst, n=48):
    """리스트를 n개로 균등 다운샘플(전체 구간 보존) — 누적 자산곡선용. n 이하면 그대로.
    전역 최소/최대 인덱스를 강제 포함 → 다운샘플 곡선에서 계산하는 고점/저점이 원본 극값과 일치
    (균등표본만으론 표본 사이 실극값이 누락돼 장기곡선 고점/저점이 과소표시)."""
    lst = list(lst)
    m = len(lst)
    if m <= n:
        return lst
    idx = {round(i * (m - 1) / (n - 1)) for i in range(n)}
    idx.add(min(range(m), key=lst.__getitem__))   # 전역 저점 보존
    idx.add(max(range(m), key=lst.__getitem__))   # 전역 고점 보존
    return [lst[i] for i in sorted(idx)]


def persona_stats(trades, seed, curve):
    """페르소나 성과지표 — FILLED 체결(trades: side/tk/qty/fill/ts)을 심볼별 FIFO 로 매칭해
    실현 손익 라운드트립을 구하고 승률/손익비/기대값/MDD 산출. review.round_trips 와 동일 FIFO
    아이디어이나 스키마가 다름(tk/ts, session 없음) 및 max_consec_losses·mdd 를 여기서 추가."""
    from collections import defaultdict, deque
    lots = defaultdict(deque)   # tk -> deque of [qty, fill]
    realized = []               # 청산 라운드트립 realized P&L (ts 오름차순)
    for t in sorted(trades or [], key=lambda x: x.get("ts", "")):
        tk, side, qty, fill = t.get("tk"), t.get("side"), float(t.get("qty", 0) or 0), float(t.get("fill", 0) or 0)
        if not tk or qty <= 0:
            continue
        if side == "BUY":
            lots[tk].append([qty, fill])
        elif side == "SELL":
            rem = qty
            while rem > 1e-9 and lots[tk]:
                lot = lots[tk][0]
                take = min(rem, lot[0])
                realized.append(take * (fill - lot[1]))
                lot[0] -= take
                rem -= take
                if lot[0] <= 1e-9:
                    lots[tk].popleft()
            # rem>0 → 봇 매수기록 없는 매도(보호분 등), 라운드트립 아님 → 무시

    n = len(realized)
    if n == 0:
        return {"win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "profit_factor": None,
                "expectancy": 0.0, "max_consec_losses": 0, "n_trades": 0, "mdd": 0.0}
    wins = [p for p in realized if p > 0]
    losses = [p for p in realized if p < 0]
    win_rate = round(len(wins) / n * 100, 1)
    avg_win = round(sum(wins) / len(wins), 2) if wins else 0.0
    avg_loss = round(sum(losses) / len(losses), 2) if losses else 0.0
    profit_factor = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    expectancy = round(sum(realized) / n, 2)
    max_consec, cur = 0, 0
    for p in realized:
        cur = cur + 1 if p < 0 else 0
        max_consec = max(max_consec, cur)
    mdd = 0.0
    peak = None
    for v in (curve or []):
        peak = v if peak is None else max(peak, v)
        if peak:
            mdd = min(mdd, v / peak - 1.0)
    return {"win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
            "profit_factor": profit_factor, "expectancy": expectancy,
            "max_consec_losses": max_consec, "n_trades": n, "mdd": round(mdd * 100, 2)}


def norm_curve(curve, seed):
    """자산곡선 → 시드 대비 % 수익률 시계열. base=seed(있으면) 없으면 곡선 첫 값."""
    curve = curve or []
    if not curve:
        return []
    base = seed if seed else curve[0]
    if not base:
        return [0.0 for _ in curve]
    return [round((v / base - 1.0) * 100, 2) for v in curve]


def running_drawdown(curve):
    """자산곡선 → 런닝피크 대비 % 드로다운 시계열(항상 <=0)."""
    out, peak = [], None
    for v in (curve or []):
        peak = v if peak is None else max(peak, v)
        out.append(round((v / peak - 1.0) * 100, 2) if peak else 0.0)
    return out


def _daily_curve(recs, kind):
    """레코드 → 일봉 equity 곡선. 하루(YYYY-MM-DD)당 마지막 equity(recs 는 ts 오름차순이라 뒤가 최신).
    누적수익률 차트를 실시각(불규칙 스냅샷) 대신 '거래일 1점'으로 그리기 위함. 반환 (dates, vals)."""
    by_day = {}
    for r in recs:
        if r.get("broker") != kind:
            continue
        a = r.get("account")
        if not (isinstance(a, dict) and a.get("equity") is not None):
            continue
        ts = r.get("ts", "")
        if len(ts) < 10:
            continue
        by_day[ts[:10]] = round(float(a["equity"]), 2)   # 같은 날 뒤 레코드가 앞을 덮음 = 그날 종가
    days = sorted(by_day)
    return days, [by_day[d] for d in days]


def market_state():
    """NYSE 정규장 여부 — 공휴일·조기폐장·16:00 경계를 calendar_util(XNYS)로 정확판정.
    이 라벨이 라이브 MTM 오버레이 게이트(아래)라 휴장일에 '장중'이 뜨면 전일 1분봉이 '실시간'으로
    오표시된다. 캘린더 불가 시에만 weekday+시간창(<960, 공휴일 미반영) 폴백."""
    try:
        import calendar_util as cu
        return "장중" if cu.is_regular_open() else "장 마감"
    except Exception:
        try:
            now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            return "장 마감"
        if now.weekday() >= 5:
            return "장 마감"
        m = now.hour * 60 + now.minute
        return "장중" if 570 <= m < 960 else "장 마감"


def price_history(tk, n=40, allow_net=True):
    """최근 n 종가 — 라이브 data.load 캐시 우선(자동 신선화), 오프라인은 정적 캐시 폴백."""
    df = _load_ohlcv(tk, allow_net=allow_net)
    if df is not None:
        try:
            s = df["Close"].dropna()
            if len(s) >= 2:
                return [float(x) for x in s.iloc[-n:]]
        except Exception:
            pass
    return None


def live_quotes(symbols, allow_net=True):
    """장중 라이브 호가 — yfinance 1분봉 마지막 종가(무료 ~15분 지연). 보유 MTM 실시간화용.
    오프라인·실패·빈입력이면 {} → 호출측이 일봉종가로 폴백(무회귀). 보유 union 1배치 호출."""
    syms = sorted({str(s).strip().upper() for s in (symbols or []) if str(s).strip()})
    if not allow_net or not syms:
        return {}
    try:
        import data  # noqa: F401 — cert env 설정(yfinance TLS)
        import yfinance as yf
        df = yf.download(syms, period="1d", interval="1m", progress=False,
                         auto_adjust=False, threads=False)
        if df is None or len(df) == 0:
            return {}
        close = df["Close"]
        out = {}
        if hasattr(close, "columns"):          # 다중 심볼 → DataFrame(컬럼=티커)
            for s in close.columns:
                ser = close[s].dropna()
                if len(ser):
                    out[str(s)] = float(ser.iloc[-1])
        else:                                   # 단일 심볼 → Series
            ser = close.dropna()
            if len(ser):
                out[syms[0]] = float(ser.iloc[-1])
        return {k: v for k, v in out.items() if v and v > 0}
    except Exception:
        return {}


def _fmt_qty(q):
    """체결수량 표시 — 정수는 정수로, 소수주는 최대 2자리(소수주 정책). :.0f 는 0.34→'0' 오표시."""
    q = float(q or 0)
    return str(int(q)) if q == int(q) else f"{q:.2f}"


def _run_msg(r):
    st = r.get("status", "")
    if st == "ok":
        fills = [o for o in (r.get("orders") or []) if o.get("status") == "FILLED"]
        if fills:
            return "체결: " + ", ".join(f"{o['side']} {o['symbol']} {_fmt_qty(o.get('qty', 0))}" for o in fills)
        return f"변경 없음 · 자산 ${(r.get('account') or {}).get('equity', 0):,.0f}"
    return f"{st}: {r.get('reason', '')}"[:90]


def _authoritative_broker():
    """대시보드가 권위로 삼는 브로커 — runs.jsonl 마지막 account.equity 보유 레코드의 broker.
    read_engine_state 의 snap 선정과 동일 로직. paper(테스트)·toss(실매매)가 한 저널에 섞여
    있어, 실 계좌 화면(거래·수수료·결정·활동)을 이 브로커로 한정해야 paper 시뮬 noise 가
    실거래처럼 표시되지 않음(포트폴리오와 동일 브로커로 일관). None=판정 불가(필터 안 함)."""
    try:
        from paths import LOG_DIR
    except Exception:
        return None
    p = os.path.join(str(LOG_DIR), "runs.jsonl")
    if not os.path.exists(p):
        return None
    try:
        lines = open(p, encoding="utf-8").read().splitlines()
    except Exception:
        return None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            r = json.loads(ln)
        except Exception:
            continue
        a = r.get("account")
        if isinstance(a, dict) and a.get("equity") is not None:
            return r.get("broker")
    return None


def read_alerts(n=8):
    """runs/exits/panics 저널에서 최근 활동 피드 복원 (runs 는 권위 브로커만 — paper 시뮬 noise 제외)."""
    try:
        from paths import LOG_DIR
    except Exception:
        return []
    bk_auth = _authoritative_broker()
    items = []

    def tail(fname, fn):
        p = os.path.join(str(LOG_DIR), fname)
        if not os.path.exists(p):
            return
        try:
            lines = open(p, encoding="utf-8").read().splitlines()[-n:]
        except Exception:
            return
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                fn(json.loads(ln))
            except Exception:
                pass

    run_lvl = {"ok": "ok", "already_ran": "info", "locked": "info", "skip": "info",
               "stale": "warn", "partial": "warn", "halted": "halt", "tripped": "halt",
               "error": "error", "crash": "error"}
    tail("runs.jsonl", lambda r: (bk_auth is None or r.get("broker") == bk_auth) and items.append(
        {"ts": r.get("ts", ""), "level": run_lvl.get(r.get("status", ""), "info"), "msg": _run_msg(r)}))
    tail("exits.jsonl", lambda r: items.append(
        {"ts": r.get("ts", ""), "level": "info" if r.get("status") == "ok" else "warn",
         "msg": "청산 점검: " + (r.get("reason", "") or ", ".join(r.get("checked", [])))}))
    tail("panics.jsonl", lambda r: items.append(
        {"ts": r.get("ts", ""), "level": "halt" if r.get("tripped") else "info",
         "msg": "패닉청산" + (" 트립" if r.get("tripped") else "") + ": "
                + ", ".join(str(t[0]) for t in (r.get("filled") or []))}))
    items.sort(key=lambda x: x["ts"], reverse=True)
    for it in items:
        it["tm"] = _kst_tm(it.get("ts", ""))
    return items[:n]


# 브로커별 수수료율 — paper 는 토스 패리티 0.1%(run_live/run_intraday, USTRADE_PAPER_FEE_RATE). toss 는 해외주식
# 거래수수료 0.1%(토스증권 명시). 저널엔 fee 미기록이라 체결가×수량×율로 계산(율은 실제값).
_COMMISSION = {"paper": float(os.environ.get("USTRADE_PAPER_FEE_RATE") or 0.001), "toss": 0.001}
_COMMISSION_DEFAULT = 0.001
_FREE_BELOW = float(os.environ.get("USTRADE_PAPER_FREE_BELOW") or 10.0)   # 명목 이하 무료(PaperBroker 와 동률)


def read_fees(broker=None, log_dir=None):
    """수수료 집계 — FILLED 주문 × 브로커 수수료율. 오늘 / 이번달 / 전체 누적.
    broker=None : 실 권위 브로커만. broker 지정 : 그 브로커만. log_dir : 페르소나 home/logs.
    저널에 실 수수료 미기록 → 체결가×수량×수수료율 추정(paper 는 결정론적·정확).
    """
    bk = broker if broker is not None else _authoritative_broker()   # None=실 권위 브로커
    recs = _load_run_recs(include_archive=(broker is not None and log_dir is None), log_dir=log_dir)
    if not recs:
        return None
    now = datetime.now()
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    agg = {"today": 0.0, "month": 0.0, "total": 0.0,
           "today_n": 0, "month_n": 0, "total_n": 0}
    for r in recs:
        if bk is not None and r.get("broker") != bk:
            continue
        ts = r.get("ts", "")
        rate = _COMMISSION.get(r.get("broker", "paper"), _COMMISSION_DEFAULT)
        for o in r.get("orders") or []:
            if o.get("status") != "FILLED":
                continue
            notional = float(o.get("fill", 0)) * float(o.get("qty", 0))
            fee = 0.0 if notional <= _FREE_BELOW else notional * rate   # $10 이하 무료(PaperBroker 정책 반영)
            agg["total"] += fee
            agg["total_n"] += 1
            if ts[:7] == month:
                agg["month"] += fee
                agg["month_n"] += 1
            if ts[:10] == today:
                agg["today"] += fee
                agg["today_n"] += 1
    for k in ("today", "month", "total"):
        agg[k] = round(agg[k], 2)
    agg["est"] = True   # 추정치 — 저널 실수수료 미기록
    return agg


def _runs_paths(log_dir=None):
    """현 runs.jsonl + 이관 아카이브(runs.archive.jsonl). 모의거래 피드가 둘 다 읽어,
    paper 를 물리 이관했든 안 했든(로컬 이관/ VM 미이관) 동일하게 paper 기록을 모음.
    log_dir 지정 시 그 디렉토리(페르소나별 home/logs), 미지정 시 전역 LOG_DIR."""
    if log_dir is None:
        try:
            from paths import LOG_DIR
            log_dir = str(LOG_DIR)
        except Exception:
            return []
    return [os.path.join(str(log_dir), f) for f in ("runs.jsonl", "runs.archive.jsonl")]


def _load_run_recs(include_archive=False, log_dir=None):
    """runs.jsonl(+옵션 아카이브) 파싱 레코드 리스트, ts 오름차순. 단일 파일은 append 순=ts순이라
    무영향, 두 파일 병합 시 정렬이 시간순 보장. 손상 라인은 건너뜀."""
    files = _runs_paths(log_dir) if include_archive else _runs_paths(log_dir)[:1]
    recs = []
    seen = set()   # 교차파일(runs+archive) 정확일치 dedup — archive 이관 크래시로 두 파일에 잔존해도 이중집계 방지
    for p in files:
        if not os.path.exists(p):
            continue
        try:
            lines = open(p, encoding="utf-8").read().splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln or ln in seen:
                continue
            seen.add(ln)
            try:
                recs.append(json.loads(ln))
            except Exception:
                pass
    recs.sort(key=lambda r: r.get("ts", ""))
    return recs


def read_trades(n=40, broker=None, log_dir=None):
    """체결 원장 — FILLED 주문 (시간순 desc).
    broker=None : 실 권위 브로커만(runs.jsonl). broker 지정 : 그 브로커만. log_dir : 페르소나 home/logs."""
    bk = broker if broker is not None else _authoritative_broker()
    out = []
    for r in _load_run_recs(include_archive=(broker is not None and log_dir is None), log_dir=log_dir):
        if bk is not None and r.get("broker") != bk:
            continue
        ts, rbk = r.get("ts", ""), r.get("broker", "")
        for o in r.get("orders") or []:
            if o.get("status") != "FILLED":
                continue
            out.append({"ts": ts, "tm": _kst_tm(ts, "%m-%d %H:%M"),
                        "side": o.get("side", ""), "tk": o.get("symbol", ""),
                        "qty": o.get("qty", 0), "fill": round(float(o.get("fill", 0)), 2), "broker": rbk})
    out.sort(key=lambda t: t["ts"], reverse=True)
    return out[:n]


def _slim_sel(sel):
    """selection(info) → 표시용 근거 요약."""
    if not sel:
        return None
    scores = sel.get("scores", {}) or {}
    missing = set(sel.get("missing", []))
    canslim, analyst = set(sel.get("canslim", [])), set(sel.get("analyst", []))
    picks = [{"tk": t, "score": scores.get(t),
              "unverified": (t in missing) and (t not in scores),   # 펀더 데이터 없어 폴백 편입(저변동성順)
              "canslim": t in canslim, "analyst": t in analyst}
             for t in sel.get("final", [])]
    flags = []
    if sel.get("screen_degraded"):
        nmiss = sum(1 for p in picks if p["unverified"])
        flags.append(f"⚠ 펀더 스크린 열화 — {nmiss}종목 펀더데이터 없어 저변동성 폴백 편입 (FMP 레이트/쿼터)")
    if sel.get("fmp_stale_hits"):   # 만료캐시 폴백 — 며칠 지난 펀더로 골랐는지(run_live 가 저널에 부착)
        flags.append(f"⚠ FMP 만료캐시 폴백 {sel['fmp_stale_hits']}건 — 최대 {sel.get('fmp_stale_max_age_d', 0):.0f}일 전 펀더 사용")
    if sel.get("below_min_score"):
        flags.append("min_score 미달: " + ", ".join(sel["below_min_score"]))
    if sel.get("excluded_value_trap"):
        flags.append("밸류트랩 제외: " + ", ".join(sel["excluded_value_trap"]))
    return {"strategy": sel.get("strategy", ""), "picks": picks,
            "candidates": sel.get("candidates", []), "flags": flags,
            # 감시 배지(watch) 집계용 — 스크린 건강 원시필드 패스스루
            "degraded": bool(sel.get("screen_degraded")),
            "missing_ratio": sel.get("missing_ratio"),
            "fmp_stale_age": sel.get("fmp_stale_max_age_d")}


def _regime_txt(risk):
    if not isinstance(risk, dict) or not risk:
        return ""
    parts = []
    for k in ("regime", "regime_on", "regime_off", "vol_scalar", "vol_target", "scaled", "gross"):
        if k in risk:
            parts.append(f"{k}={risk[k]}")
    return " · ".join(parts)


def read_decisions(n=12, broker=None, log_dir=None):
    """의사결정 저널 — 런별 매수내역 + 선정근거(selection/risk) + 수동 메모.
    broker=None : 실 권위 브로커만. broker 지정 : 그 브로커만. log_dir : 페르소나 home/logs."""
    try:
        from paths import STATE_DIR
    except Exception:
        return []
    bk = broker if broker is not None else _authoritative_broker()
    notes = {}
    npath = os.path.join(str(STATE_DIR), "decision_notes.jsonl")
    if os.path.exists(npath):
        try:
            _lines = open(npath, encoding="utf-8").read().splitlines()
        except Exception:
            _lines = []
        for ln in _lines:                       # per-line 격리(_load_run_recs 패턴) — 깨진 라인 1개가
            ln = ln.strip()                     # 그 이후 정상 메모를 전부 침묵 누락시키지 않게.
            if not ln:
                continue
            try:
                nr = json.loads(ln)
            except Exception:
                continue
            notes.setdefault(nr.get("ts", ""), []).append(nr.get("text", ""))
    out = []
    for r in _load_run_recs(include_archive=(broker is not None and log_dir is None), log_dir=log_dir):
        if bk is not None and r.get("broker") != bk:
            continue
        ts = r.get("ts", "")
        bought = [{"side": o.get("side"), "tk": o.get("symbol"), "qty": _fmt_qty(o.get("qty")),
                   "fill": round(float(o.get("fill", 0)), 2), "reason": o.get("reason", "")}
                  for o in (r.get("orders") or []) if o.get("status") == "FILLED"]
        sel = r.get("selection") or {}
        if not bought and not sel:
            continue    # 분당 intraday 하트비트(체결·선정근거 둘 다 없음) — 판단저널 노이즈라 제외
        out.append({
            "ts": ts, "tm": _kst_tm(ts, "%m-%d %H:%M"),
            "status": r.get("status", ""), "broker": r.get("broker", ""),
            "reason": r.get("reason", ""), "bought": bought,
            "sel": _slim_sel(sel), "regime": _regime_txt(r.get("risk")),
            "has_rationale": bool(sel), "notes": notes.get(ts, []),
        })
    out.reverse()   # 최신 우선
    top = out[:n]
    # 일1런 선정 레코드가 top 에 없으면(장중 체결 다수로 밀림) 최신 1건을 끌어와 고정 — 선정 근거 항상 노출
    if out and not any(d["has_rationale"] for d in top):
        srec = next((d for d in out if d["has_rationale"]), None)
        if srec:
            top = top[:max(0, n - 1)] + [srec]
    return top


def next_session_iso():
    """다음 NYSE 정규장 개장 시각 ISO. 캘린더 있으면 휴장/조기폐장 반영, 없으면 평일 09:30 ET 폴백."""
    try:
        import calendar_util as cu
        now = cu.now_et()
        sched = cu._NYSE.schedule(start_date=now.date().isoformat(),
                                  end_date=(now + pd.Timedelta(days=12)).date().isoformat())
        future = sched["market_open"][sched["market_open"] > pd.Timestamp(now)]
        if len(future):
            return future.iloc[0].isoformat()
    except Exception:
        pass
    try:   # 폴백 — 공휴일 미반영, 평일 09:30 ET
        from datetime import datetime, timedelta
        now = datetime.now(ZoneInfo("America/New_York"))
        cand = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        while cand.weekday() >= 5:
            cand += timedelta(days=1)
        return cand.isoformat()
    except Exception:
        return None


def compute_performance(closes):
    """RS 모멘텀 전략 백테스트 (기존 strategy+simulate+metrics 재사용) → equity 곡선 + 지표."""
    try:
        from strategies.cross_momentum import RelativeStrengthMomentum
        from engines.portfolio_runner import _simulate
        from engines import metrics
        panel = pd.DataFrame({t: s for t, s in closes.items() if t != "SPY"}).dropna()
        if panel.shape[0] < 300 or panel.shape[1] < 3:
            return None
        weights = RelativeStrengthMomentum(lookback=252, skip=21, top_n=3, freq="M").generate_weights(panel)
        equity, port_ret = _simulate(panel, weights, cash=100000.0, fee=0.0005)
        m = metrics.compute(equity, port_ret, len(weights))
        if "error" in m:
            return None
        step = max(1, len(equity) // 64)
        eq = equity.iloc[::step]
        return {
            "equity": [round(float(x), 1) for x in eq],
            "dates": [d.strftime("%y/%m") for d in eq.index],
            "stats": {"total": round(m["total_return"] * 100, 1), "cagr": round(m["cagr"] * 100, 1),
                      "mdd": round(m["max_drawdown"] * 100, 1), "sharpe": round(m["sharpe"], 2),
                      "sortino": round(m["sortino"], 2), "rebal": m.get("n_trades", 0)},
            "span": f"{panel.index[0].strftime('%Y-%m')} ~ {panel.index[-1].strftime('%Y-%m')}",
        }
    except Exception:
        return None


# ───────────────────────── 실 엔진 상태 ─────────────────────────
def read_engine_state(offline=False, closes=None, broker=None, home=None, ks_namespace=None):
    """runs.jsonl + killswitch + toss_sleeve 에서 계좌·포지션·정지상태 복원.

    broker=None : 실 권위 브로커(runs.jsonl 마지막 account snapshot). broker 지정 : 그 브로커만.
    home 지정 : 해당 USTRADE_HOME(<home>/logs·state)에서 읽음 — 모의매매 페르소나별 격리 home 용.
    ks_namespace : killswitch.<ns>.json 네임스페이스(페르소나는 paper_<name>). 반환 None = 저널 없음.
    """
    try:
        from paths import STATE_DIR as _SD
    except Exception:
        return None
    if home:
        state_dir = os.path.join(str(home), "state")
        log_dir = os.path.join(str(home), "logs")
    else:
        state_dir, log_dir = str(_SD), None        # log_dir=None → _load_run_recs 가 전역 LOG_DIR
    recs = _load_run_recs(include_archive=(broker is not None and home is None), log_dir=log_dir)
    if broker is not None:
        recs = [r for r in recs if r.get("broker") == broker]
    if not recs:
        return None

    # 포지션·계좌는 "마지막 실 스냅샷"(account.equity 있는 최근 레코드) 기준. 마지막 런이
    # 할트/already_ran 처럼 account 없는 레코드면 그걸 권위로 쓰면 화면 잔고가 0으로 덮이므로
    # 건너뛰고 직전 실 스냅샷으로 폴백. 정상 런이면 snap==last 라 무회귀. 정지배지는 아래
    # killswitch/HALT 로 별도 판정이라 폴백해도 HALTED 유지. last 는 하트비트(ts/status)용.
    last = recs[-1]

    def _has_acct(r):
        a = r.get("account")
        return isinstance(a, dict) and a.get("equity") is not None

    snap = next((r for r in reversed(recs) if _has_acct(r)), last)
    _kind = snap.get("broker", "paper")
    # equity 곡선은 같은 broker 레코드만(자본 스케일 일관).
    equity_curve = [round(float(r["account"]["equity"]), 2) for r in recs
                    if r.get("broker") == _kind and isinstance(r.get("account"), dict)
                    and "equity" in r["account"]]
    _dc_days, _dc_vals = _daily_curve(recs, _kind)   # 일봉 곡선(거래일당 1점 + 날짜)
    # 보유 = 권위 positions 스냅샷(저널에 broker.get_positions() 가 매 레코드 기록됨) 1차.
    # 없으면(구버전 레코드) 옛 방식 폴백 — 그 런 FILLED 주문 net. 단 후자는 '리밸런스 델타'라
    # steady-state/할트(주문 0건) 런이면 공집합이 돼 보유종목이 화면서 사라지던 버그.
    book = {}   # sym -> {"qty":, "cost":}
    positions = snap.get("positions")
    if positions:
        for p in positions:
            sym, q, avg = p.get("symbol"), float(p.get("qty", 0) or 0), float(p.get("avg", 0) or 0)
            if sym and q > 1e-9:
                book[sym] = {"qty": q, "cost": q * avg,
                             "stop": p.get("stop"), "target": p.get("target")}   # 보호선(intraday 스냅샷 동봉분)
    else:
        for o in snap.get("orders") or []:
            if o.get("status") != "FILLED":
                continue
            sym, q, fill = o.get("symbol"), float(o.get("qty", 0)), float(o.get("fill", 0))
            if not sym or q <= 0:
                continue
            b = book.setdefault(sym, {"qty": 0.0, "cost": 0.0})
            if o.get("side") == "BUY":
                b["qty"] += q
                b["cost"] += q * fill
            else:
                if b["qty"] > 0:
                    b["cost"] -= b["cost"] / b["qty"] * min(q, b["qty"])
                b["qty"] -= q
    book = {s: v for s, v in book.items() if v["qty"] > 1e-9}
    # toss 관리 슬리브 정합 — managed 가 봇 보유의 권위(매수·청산·패닉이 runs/exits/panics 저널에
    # 나뉘어 기록돼, 마지막 runs 스냅샷 orders book 엔 패닉/장중청산 매도가 안 잡힘). managed 에 없는
    # 심볼은 청산된 것 → 보유서 제외(managed 비었으면 보유 0). positions 스냅샷이 있으면 이미 권위라 skip.
    _sleeve_reconciled = False
    if not positions and snap.get("broker") == "toss":
        sv0 = os.path.join(state_dir, "toss_sleeve.json")
        if os.path.exists(sv0):
            try:
                _mg = json.load(open(sv0, encoding="utf-8")).get("managed") or {}
            except Exception:
                _mg = None
            if _mg is not None:
                try:
                    from broker.managed import _norm as _nm
                except Exception:
                    _nm = lambda s: str(s).upper().replace(".", "-")
                _keys = {_nm(k) for k in (_mg.keys() if isinstance(_mg, dict) else _mg)}
                book = {s: v for s, v in book.items() if _nm(s) in _keys}
                _sleeve_reconciled = True
    kind = snap.get("broker", "paper")
    acct = snap.get("account") or {}
    equity = float(acct.get("equity", 0.0))
    cash = float(acct.get("cash", 0.0))
    daily_pnl_pct = float(snap.get("daily_pnl", 0.0)) * 100.0
    _dp = float(snap.get("daily_pnl", 0.0))
    _day_start = (equity / (1.0 + _dp)) if (equity > 0 and (1.0 + _dp) > 1e-9) else None   # 당일 baseline(라이브 재평가 시 당일% 재계산)

    # 현재가/시계열 = 캐시 우선, 없으면 yfinance(offline 이면 평단). build() 가 이미 로드한
    # closes 를 넘겨주면 재로드 안 함(빌드당 load_closes 2회 → 1회).
    if closes is None:
        closes = load_closes(allow_net=not offline)
    pseries = {}   # sym -> 종가 리스트 (equity 곡선 재구성용)
    holdings, invested, value_now = [], 0.0, 0.0
    for sym, b in book.items():
        avg = b["cost"] / b["qty"] if b["qty"] else 0.0
        s = closes.get(sym)
        ser = [float(x) for x in s.iloc[-40:]] if s is not None else price_history(sym, 40, allow_net=not offline)
        stale = not ser           # 가격 결손 — last_px 를 평단으로 폴백. 손익 0% 가 '진짜 본전'처럼 오인되지 않게 플래그.
        if ser:
            pseries[sym] = ser
        last_px = ser[-1] if ser else avg
        val = b["qty"] * last_px
        invested += b["cost"]
        value_now += val
        holdings.append({
            "tk": sym, "name": NAMES.get(sym, sym), "sector": SECTORS.get(sym, "—"),
            "qty": round(b["qty"], 4), "avg": round(avg, 2), "last": round(last_px, 2),
            "value": round(val, 2),
            "pnl": None if stale else round(val - b["cost"], 2),
            "pnlpct": None if stale else (round(pct(last_px, avg), 2) if avg else 0.0),
            "stale": stale,
            "stop": b.get("stop"), "target": b.get("target"),   # 보호선 — intraday 페르소나만 채워짐
        })
    holdings.sort(key=lambda h: h["value"], reverse=True)
    # 슬리브 정합 후 현금 재계산 — toss sleeve equity = 현금 + Σmanaged평가액. 청산으로 보유가
    # 빠지면(value_now 감소) 그 대금이 현금으로 돌아온 것이므로 cash = equity - value_now 로 불변식
    # 유지(스냅샷 cash 가 청산 前 값이라 '총 $132·현금 $100·보유 0' 식 갭이 생기던 것 해소).
    if _sleeve_reconciled and equity > 0:
        cash = round(max(0.0, equity - value_now), 2)

    # ── 라이브 MTM 오버레이(장중·온라인): 보유를 yfinance 장중 호가로 재평가 → 헤드라인·종목줄이
    # 장중 움직임(무료피드 ~15분 지연). 라이브 실패·장마감·오프라인이면 통째 skip = 일봉종가/동결
    # equity 폴백(무회귀). total = 휴지현금 + Σ라이브평가 (장중 매매 0 → 현금 불변이 전제).
    live_mtm_failed = False
    if not offline and book and market_state() == "장중":
        lq = live_quotes(list(book.keys()), allow_net=True)
        if lq:
            value_now = 0.0
            for h in holdings:
                px = lq.get(h["tk"])
                if px and px > 0:
                    h["last"] = round(px, 2)
                    h["value"] = round(h["qty"] * px, 2)
                    h["pnl"] = round(h["value"] - h["qty"] * h["avg"], 2) if h["avg"] else None
                    h["pnlpct"] = round(pct(px, h["avg"]), 2) if h["avg"] else 0.0
                    h["live"], h["stale"] = True, False
                value_now += h["value"]
            equity = round(cash + value_now, 2)   # 라이브 MTM 헤드라인 (하류 risk/summary 가 픽업)
            if _day_start and _day_start > 0:      # 당일%도 동일 시점 재계산 — 총자산↔당일% 시점 불일치 해소
                daily_pnl_pct = round((equity / _day_start - 1.0) * 100.0, 2)
        else:
            # 장중인데 라이브 호가 전체 실패 → 일봉종가 폴백 중임을 플래그. 안 하면 종가가
            # 현재가처럼 보여 '거짓 정상' — UI 가 배지로 표면화.
            live_mtm_failed = True

    # equity 곡선 재구성 — 저널이 빈약(<2점)하면 보유종목 MTM(현금+Σ수량·가격)으로 합성
    if len(equity_curve) < 2 and book and pseries:
        minlen = min(len(v) for v in pseries.values())
        if minlen >= 2:
            qmap = {s: v["qty"] for s, v in book.items()}
            equity_curve = [round(cash + sum(qmap[s] * pseries[s][i] for s in pseries), 2)
                            for i in range(-minlen, 0)]

    # 정지 상태
    halted, hwm, day_start = False, equity, equity
    ks = os.path.join(state_dir, f"killswitch.{ks_namespace or kind}.json")
    if os.path.exists(ks):
        try:
            with open(ks, encoding="utf-8") as f:
                k = json.load(f)
            halted = bool(k.get("halted"))
            hwm = float(k.get("hwm", equity))
            day_start = float(k.get("day_start_equity", equity))
        except Exception:
            pass
    # 수동 HALT 파일(state/HALT)도 정지로 OR — 게이트 guardrail.is_halted 는 이 파일을 killswitch
    # JSON 과 무관하게 halted 로 보는데(KILL_FILE), 대시보드 정지버튼(server.api_halt)은 JSON 을
    # 안 건드리고 이 파일만 쓴다. 안 OR 하면 긴급정지를 눌러도 표시는 ARMED 로 남는 오표시 발생.
    if os.path.exists(os.path.join(state_dir, "HALT")):
        halted = True
    # 장중 루프 일중손실정지(IntradayGuard.halted)는 killswitch/HALT 파일 안 쓰고 스냅샷 레코드에 기록 →
    # OR 로 반영(안 하면 장중 정지돼도 대시보드 ARMED 오표시).
    if last.get("halted") or snap.get("halted"):
        halted = True

    # 슬리브 — 보호분(봇 제외) + 봇 관리 basis + 미확정 매수
    protected, managed, pending = [], {}, {}
    sv = os.path.join(state_dir, "toss_sleeve.json")
    if os.path.exists(sv):
        try:
            with open(sv, encoding="utf-8") as f:
                sl = json.load(f)
            protected = sl.get("protected", []) or []
            managed = sl.get("managed", {}) or {}
            pending = sl.get("pending", {}) or {}
        except Exception:
            pass
    reconcile = snap.get("reconcile", {"ok": True, "drift": []})

    # 리스크 게이지 — killswitch 한도 대비 현재 위치
    try:
        from broker.guardrail import GuardConfig
        gc = GuardConfig()
        lim_daily, lim_dd, lim_pos = gc.max_daily_loss, gc.max_total_drawdown, gc.max_position_weight
    except Exception:
        lim_daily, lim_dd, lim_pos = 0.05, 0.20, 0.40
    maxpos_w = (max((h["value"] for h in holdings), default=0.0) / equity * 100) if equity else 0.0
    risk = {
        "daily": {"val": round(pct(equity, day_start), 2), "limit": round(-lim_daily * 100, 1)},
        "drawdown": {"val": round(min(0.0, pct(equity, hwm)), 2), "limit": round(-lim_dd * 100, 1)},
        "maxpos": {"val": round(maxpos_w, 1), "limit": round(lim_pos * 100, 1)},
    }
    # 분산비율(DR) — 관찰 전용. 섹터·테마 한도가 코드에 없어(HOUSE.md §3) 같은 베팅 중복을
    # 사람 눈이 봐야 하는 구멍을 숫자로 표면화(DR≈1 = 티커만 다른 한 베팅). 실패 무해(None=미표시).
    try:
        import diversification as _dv
        _dw = {s: b["qty"] * pseries[s][-1] for s, b in book.items() if pseries.get(s)}
        risk["div"] = _dv.div_ratio(_dw, pseries)
    except Exception:
        risk["div"] = None

    pnl = value_now - invested
    # 출처 분리(의도, F2): total=브로커 권위 스냅샷(equity), pnl/pnlpct=보유 book 의 캐시종가 MTM
    # (value_now-invested). 가격출처가 달라 'total' 과 'Σholdings.value+cash' 가 미세하게 어긋날 수
    # 있으나 소액·근접가격이라 보통 센트 단위. total 은 항상 브로커 진실값을 권위로 유지.
    return {
        "source": f"broker:{kind}",
        "summary": {"total": round(equity, 2), "pnl": round(pnl, 2),
                    "pnlpct": round(pct(value_now, invested), 2) if invested else 0.0,
                    "cash": round(cash, 2), "positions": len(holdings)},
        "holdings": holdings,
        "equity_curve": equity_curve[-30:],
        "equity_full": _downsample(equity_curve, 48),   # 전체 누적 자산곡선(페르소나 수익률 추이)
        "curve_v": _dc_vals, "curve_d": _dc_days,        # 일봉(거래일 1점) equity + 날짜 — 누적수익률 차트 x축용
        "daily_pnl_pct": round(daily_pnl_pct, 2),
        "ts": last.get("ts", ""), "session": snap.get("session", ""),
        "status": last.get("status", ""), "kind": kind,
        "intraday": bool(last.get("intraday") or snap.get("intraday")),   # 장중 액티브 트레이딩 페르소나 배지
        "halted": halted, "hwm": round(hwm, 2), "day_start": round(day_start, 2),
        "protected": protected,
        "reconcile": reconcile,
        "live_mtm_failed": live_mtm_failed,               # 장중 라이브호가 전체실패 → 종가 폴백 중(배지용)
        "acct_error": bool(last.get("acct_error")),       # 마지막 런 계좌조회 실패(잔고 표시가 구값일 수 있음)
        "sleeve": {"managed": managed, "pending": pending, "protected": protected},
        "risk": risk,
    }


def _persona_homes():
    """모의매매 페르소나 home 발견. (1) env USTRADE_PERSONA_HOMES(';' 구분, VM setup_paper_tasks.ps1)
    (2) 폴백: C:\\ 와 %LOCALAPPDATA% 에서 'ustrade-paper-*' 스캔. 반환 [(name, home), …] (personas 순서 우선)."""
    found = {}   # name -> home path
    from paths import persona_homes as _ph
    cands = [str(h) for h in _ph()]   # 정규 파서(paths) + 아래 디렉토리 스캔 폴백
    for root in ("C:\\", os.environ.get("LOCALAPPDATA")):
        if root and os.path.isdir(root):
            try:
                cands += [os.path.join(root, d) for d in os.listdir(root) if d.startswith("ustrade-paper-")]
            except Exception:
                pass
    for path in cands:
        path = path.strip().rstrip("\\/")
        base = os.path.basename(path)
        if not path or not base.startswith("ustrade-paper-") or not os.path.isdir(path):
            continue
        found.setdefault(base[len("ustrade-paper-"):], path)
    try:
        import personas as _p
        order = list(_p.PERSONAS.keys())
    except Exception:
        order = []
    names = [n for n in order if n in found] + [n for n in found if n not in order]
    return [(n, found[n]) for n in names]


def read_persona(name, home, closes=None, offline=False):
    """페르소나 모의계좌 1종 — 격리 home(<home>/logs·state)에서 요약·보유·거래·수수료·저널 복원.
    수익률 = equity/시드자본 - 1 (시드는 personas.py 권위, 없으면 MTM pnlpct 폴백)."""
    try:
        import personas as _p
        meta = _p.PERSONAS.get(name, {})
    except Exception:
        meta = {}
    st = read_engine_state(offline=offline, closes=closes, broker="paper",
                           home=home, ks_namespace=f"paper_{name}")
    if not st:
        return None
    log_dir = os.path.join(str(home), "logs")
    summ = st["summary"]
    seed = float(meta.get("cash") or 0) or None
    ret = round((summ["total"] / seed - 1) * 100, 2) if seed else summ.get("pnlpct", 0.0)
    return {
        "name": name, "label": meta.get("label", name), "strategy": meta.get("strategy", ""),
        "seed": seed, "ret": ret, "intraday": st.get("intraday", False),
        "daily_pnl_pct": st.get("daily_pnl_pct"),   # 장중 당일손익(snapshot daily_pnl 기반) — 패널 표출용
        "summary": summ, "holdings": st["holdings"], "equity_curve": st.get("equity_curve", []),
        "curve": st.get("curve_v", []),              # 누적 수익률 차트 — 일봉(거래일 1점)
        "curve_dates": st.get("curve_d", []),        # 위 곡선의 날짜(YYYY-MM-DD) — x축 라벨용
        "halted": st.get("halted"), "status": st.get("status"), "ts": st.get("ts", ""),
        "trades": read_trades(broker="paper", log_dir=log_dir),
        "fees": read_fees(broker="paper", log_dir=log_dir),
        "decisions": read_decisions(broker="paper", log_dir=log_dir),
    }


def read_trade_feed(n=40):
    """전 계좌 통합 거래 이벤트 피드 — 페르소나·실계좌의 일1런 체결(orders) + 장중 체결(intraday.jsonl)
    + 상태 이벤트(트립/크래시/HALT). ts desc, who=페르소나명/브로커. Control 탭 '거래 피드' 데이터원."""
    items = []

    def _from_home(log_dir, who):
        if log_dir is None:                           # 전역 LOG_DIR 해석(실계좌/기본 home)
            try:
                from paths import LOG_DIR
                log_dir = str(LOG_DIR)
            except Exception:
                return
        for r in _load_run_recs(log_dir=log_dir)[-400:]:
            if r.get("intraday"):                     # 분당 스냅샷 제외 — 장중 체결은 intraday.jsonl 담당
                continue
            ts = r.get("ts", "")
            for o in r.get("orders") or []:
                if o.get("status") != "FILLED":
                    continue
                items.append({"ts": ts, "who": who, "side": o.get("side", ""),
                              "tk": o.get("symbol", ""), "qty": o.get("qty", 0),
                              "px": round(float(o.get("fill", 0) or 0), 2),
                              "reason": (o.get("reason") or "")[:80], "kind": "일1런"})
            if r.get("status") in ("tripped", "crash", "error", "halted"):
                items.append({"ts": ts, "who": who, "side": r["status"].upper(), "tk": "",
                              "qty": None, "px": None,
                              "reason": (r.get("reason") or "")[:80], "kind": "상태"})
        ij = os.path.join(str(log_dir), "intraday.jsonl")
        if os.path.exists(ij):
            try:
                lines = open(ij, encoding="utf-8").read().splitlines()[-200:]
            except Exception:
                lines = []
            for ln in lines:
                try:
                    r = json.loads(ln)
                except Exception:
                    continue
                items.append({"ts": r.get("ts", ""), "who": who, "side": r.get("action", ""),
                              "tk": r.get("symbol", ""), "qty": r.get("qty", 0),
                              "px": round(float(r.get("price", 0) or 0), 2),
                              "reason": (r.get("reason") or "")[:80], "kind": "장중"})

    for nm, home in _persona_homes():
        _from_home(os.path.join(str(home), "logs"), nm)
    _from_home(None, "실계좌")   # 전역 LOG_DIR(실계좌/기본 home) — live 미가동이면 자연히 빈 기여
    items.sort(key=lambda x: x["ts"], reverse=True)
    out = items[:n]
    for x in out:
        x["tm"] = _kst_tm(x["ts"], "%m-%d %H:%M")
    return out


def read_notify_fail(state_dir=None):
    """notify_fail.flag (notify 가 '채널 설정됨+전송0건' 시 남기는 채널死 신호) → 대시보드 채널 헬스.
    파일 없으면 정상. 사람이 보는 대시보드에 채널死를 띄워 SPOF(채널死 시 모든 경보·heartbeat 백스톱이
    같은 죽은 notify() 로 무성화)를 가시화한다. notify 가 전송 성공/미설정 시 flag 를 자가치유 제거."""
    if state_dir is None:
        from paths import STATE_DIR
        state_dir = STATE_DIR
    p = os.path.join(str(state_dir), "notify_fail.flag")
    if not os.path.exists(p):
        return {"down": False, "detail": ""}
    try:
        with open(p, encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        detail = (lines[-1] if lines else "")[:160]
    except Exception:
        detail = ""
    return {"down": True, "detail": detail}


def read_selection_review():
    """LOG_DIR/selection_review/ 최신 md 리포트(신호 차원별 사후수익, review 태스크가 주기 생성).
    파일 원문 그대로 전달(관측 전용) — 없으면 None. 대시보드 Journal 탭 카드용."""
    try:
        from paths import LOG_DIR
        d = os.path.join(str(LOG_DIR), "selection_review")
        files = sorted(f for f in os.listdir(d) if f.endswith(".md"))
        if not files:
            return None
        p = os.path.join(d, files[-1])
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        return {"file": files[-1], "md": txt[-12000:]}   # 최근 리포트 1건, 크기 캡
    except Exception:
        return None


# ───────────────────────── 시뮬 포트폴리오(폴백) ─────────────────────────
def sim_portfolio(rows, closes):
    picks = rows[:TOP_HOLDINGS]
    cash_each = START_CAPITAL / TOP_HOLDINGS
    holdings, invested, value_now = [], 0.0, 0.0
    curve_syms = []
    for x in picks:
        s = closes[x["tk"]]
        entry = float(s.iloc[-(HOLD_LOOKBACK + 1)])
        last = x["last"]
        qty = int(cash_each // entry)
        if qty <= 0:
            continue
        cost = qty * entry
        invested += cost
        value_now += qty * last
        curve_syms.append((qty, s))
        holdings.append({
            "tk": x["tk"], "name": x["name"], "sector": x["sector"], "qty": qty,
            "avg": round(entry, 2), "last": round(last, 2), "value": round(qty * last, 2),
            "pnl": round(qty * last - cost, 2), "pnlpct": round(pct(last, entry), 2),
        })
    leftover = START_CAPITAL - invested
    total = value_now + leftover
    # equity 곡선 — 보유기간 일별 평가액 (거래일 1점 + 날짜)
    curve, curve_dates = [], []
    if curve_syms:
        idx = curve_syms[0][1].index   # 종목들 공유 거래일 인덱스
        for i in range(-(HOLD_LOOKBACK + 1), 0):
            v = leftover + sum(qty * float(s.iloc[i]) for qty, s in curve_syms)
            curve.append(round(v, 2))
            curve_dates.append(str(idx[i].date()))
    return {
        "source": "simulated",
        "summary": {"total": round(total, 2), "pnl": round(total - START_CAPITAL, 2),
                    "pnlpct": round(pct(total, START_CAPITAL), 2),
                    "cash": round(leftover, 2), "positions": len(holdings)},
        "holdings": holdings, "equity_curve": curve, "daily_pnl_pct": None,
        "curve_v": curve, "curve_d": curve_dates,
        "halted": False, "protected": [], "kind": "paper", "status": "sim",
    }


# ───────────────────────── 라이브 지수/뉴스 ─────────────────────────
def _index_tag(label, chg):
    """등락 기반 한 줄 코멘트(룰기반·데이터구동, 환각 없음). 해당 없으면 ''."""
    if label == "VIX":
        if chg >= 5: return "변동성 급등"
        if chg <= -5: return "변동성 진정"
        return ""
    if label == "금":
        if chg >= 0.8: return "안전선호"
        if chg <= -0.8: return "위험선호"
        return ""
    if label == "비트코인":
        if abs(chg) >= 3: return "변동성 확대"
        return ""
    if chg <= -2: return "급락"
    if chg >= 2: return "급등"
    if chg <= -0.5: return "약세"
    if chg >= 0.5: return "강세"
    return ""


def live_indices():
    import data  # noqa (cert env 설정)
    import yfinance as yf
    out = []
    specs = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥"), ("^DJI", "다우"),
             ("^VIX", "VIX"), ("BTC-USD", "비트코인"), ("GC=F", "금"), ("^KS11", "코스피")]
    for sym, label in specs:
        try:                                             # 종목별 격리 — 한 티커 실패가 전체 폴백 유발 안 함
            cl = yf.Ticker(sym).history(period="5d")["Close"].dropna()   # NaN 봉(코스피 등) 제거
            if len(cl) < 2:
                continue
            last, prev = float(cl.iloc[-1]), float(cl.iloc[-2])
            spk = spark(cl)                              # 일봉 폴백 스파크
            try:                                         # 오늘 인트라데이 모양 우선
                intra = yf.Ticker(sym).history(period="1d", interval="15m")["Close"].dropna()
                if len(intra) >= 5:
                    spk = spark(intra, 40)
            except Exception:
                pass
            chg = round(pct(last, prev), 2)
            out.append({"k": label, "p": round(last, 2), "c": chg, "abs": round(last - prev, 2),
                        "prevClose": round(prev, 2),   # 스파크 회색 기준선 = 전일 종가
                        "spark": spk, "tag": _index_tag(label, chg), "fmt": "num"})
        except Exception:
            continue
    if not out:
        raise ValueError("지수 라이브 전부 실패")
    return out


def live_news(tickers):
    import data  # noqa
    import yfinance as yf
    items, seen = [], set()
    for i, t in enumerate(tickers):
        try:
            raw = yf.Ticker(t).news or []
        except Exception:
            continue
        for n in raw:
            c = n.get("content") if isinstance(n.get("content"), dict) else n
            title = (c.get("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            prov = ((c.get("provider") or {}).get("displayName")
                    if isinstance(c.get("provider"), dict) else n.get("publisher")) or "News"
            ctu = c.get("clickThroughUrl") or c.get("canonicalUrl") or n.get("link")
            url = ctu.get("url", "") if isinstance(ctu, dict) else (ctu or "")
            body = (c.get("summary") or c.get("description") or n.get("summary") or "").strip()
            ts = n.get("providerPublishTime") or 0
            pubd = c.get("pubDate") or c.get("displayTime")
            when = None
            if isinstance(ts, (int, float)) and ts > 0:
                when = datetime.fromtimestamp(ts)
            elif pubd:
                try:
                    when = datetime.fromisoformat(str(pubd).replace("Z", "+00:00"))
                except Exception:
                    when = None
            items.append({"tm": _kst_dt(when).strftime("%H:%M") if when else "--",
                          "_sort": when.timestamp() if when else 0,
                          "cat": t, "cls": _BADGES[i % len(_BADGES)], "sc": "·",
                          "ht": title, "sn": f"{prov} · {t}", "url": url, "body": body})
    items.sort(key=lambda x: x["_sort"], reverse=True)
    for it in items:
        it.pop("_sort", None)
    if not items:
        raise ValueError("뉴스 없음")
    return items[:6]


STATIC_NEWS = [
    {"tm": "08:14", "cat": "Macro", "cls": "b-fx", "sc": 57,
     "ht": "Fed officials signal patience as inflation cools toward target",
     "sn": "Policymakers reiterate data-dependent stance; markets price gradual path."},
    {"tm": "08:05", "cat": "Tech", "cls": "b-auto", "sc": 55,
     "ht": "Chip demand stays firm on AI capex; suppliers raise guidance",
     "sn": "Semiconductor names extend leadership as data-center orders accelerate."},
    {"tm": "07:40", "cat": "Earnings", "cls": "b-fin", "sc": 52,
     "ht": "Big banks beat on net interest income, trading revenue mixed",
     "sn": "Financials in focus ahead of guidance; credit costs watched closely."},
    {"tm": "07:10", "cat": "Energy", "cls": "b-mkt", "sc": 48,
     "ht": "Crude steadies as supply discipline offsets demand worries",
     "sn": "Energy majors track oil; refiners and integrated names diverge."},
    {"tm": "06:30", "cat": "Market", "cls": "b-mkt", "sc": 45,
     "ht": "Breadth improves as cyclicals join megacap rally",
     "sn": "More names clear 20-day average; rotation signal strengthens."},
]


# ───────────────────────── 메인 ─────────────────────────
def build(offline=False, sim=False):
    """대시보드 데이터 dict 생성 (파일 안 씀 — server.py 가 직접 호출)."""
    closes = load_closes(allow_net=not offline)
    if not closes:
        raise RuntimeError(f"캐시 CSV 없음: {CACHE}")   # 라이브러리 경로 — BaseException(SystemExit)은 서버 핸들러가 못 잡아 폰 빈화면
    as_of = max(s.index.max() for s in closes.values())

    # ── 스캐너: 모멘텀 복합점수 ──
    rows = []
    for t, s in closes.items():
        if t == "SPY":
            continue
        last = float(s.iloc[-1])
        r5, r20, r60 = pct(last, float(s.iloc[-6])), pct(last, float(s.iloc[-21])), pct(last, float(s.iloc[-61]))
        ma5, ma20 = float(s.iloc[-5:].mean()), float(s.iloc[-20:].mean())
        rows.append({"tk": t, "name": NAMES[t], "sector": SECTORS[t], "last": last,
                     "r5": r5, "r20": r20, "r60": r60, "ma5": ma5, "ma20": ma20,
                     "aligned": ma5 > ma20, "comp": 0.6 * r20 + 0.4 * r60, "spark": spark(s)})
    comps = [x["comp"] for x in rows]
    if comps:   # closes 에 SPY만 남으면(SPY 외 전 종목 다운로드 실패) rows=[] → min([]) ValueError 방지
        lo, span = min(comps), (max(comps) - min(comps)) or 1.0
        for x in rows:
            x["score"] = round(40 + (x["comp"] - lo) / span * 60)
        rows.sort(key=lambda x: x["comp"], reverse=True)

    # ── 다축 스캐너: 거래량급증 / 52주신고가 근접 / 급등(5일) — OHLCV 프레임 기반 ──
    # 컴포짓 단일랭킹(radar)이 못 잡는 축. 같은 item 스키마 + note(축 지표) 로 radarItem 재사용.
    frames = load_ohlcv(allow_net=not offline)
    by_tk = {x["tk"]: x for x in rows}
    for t, df in frames.items():
        x = by_tk.get(t)
        if x is None:
            continue
        try:
            vol = pd.to_numeric(df["Volume"], errors="coerce").dropna()
            vma = float(vol.iloc[-21:-1].mean()) if len(vol) >= 21 else 0.0
            x["vol_surge"] = (float(vol.iloc[-1]) / vma) if vma > 0 else 0.0
            hi = pd.to_numeric(df["High"], errors="coerce").dropna()
            h52 = float(hi.iloc[-252:].max()) if len(hi) else 0.0
            x["near_52h"] = (x["last"] / h52) if h52 > 0 else 0.0
        except Exception:
            x["vol_surge"], x["near_52h"] = 0.0, 0.0

    # radar 조립은 위 루프(vol_surge/near_52h 계산) 뒤여야 함 — 앞에 두면 두 필드가 항상 0으로 실린다.
    radar = [{"score": x["score"], "tk": x["tk"], "name": x["name"], "sector": x["sector"],
              "price": round(x["last"], 2), "chg": round(x["r5"], 2), "spark": x["spark"],
              "r20": round(x["r20"], 2), "r60": round(x["r60"], 2), "aligned": x["aligned"],
              "vol_surge": round(x.get("vol_surge", 0.0), 2), "near_52h": round(x.get("near_52h", 0.0), 4)}
             for x in rows[:12]]

    def _axis_item(x, note):
        return {"score": x["score"], "tk": x["tk"], "name": x["name"], "sector": x["sector"],
                "price": round(x["last"], 2), "chg": round(x["r5"], 2), "spark": x["spark"], "note": note,
                "r20": round(x["r20"], 2), "r60": round(x["r60"], 2), "aligned": x["aligned"],
                "vol_surge": round(x.get("vol_surge", 0.0), 2), "near_52h": round(x.get("near_52h", 0.0), 4)}

    vol_rows = sorted([x for x in rows if x.get("vol_surge", 0) > 1.5],
                      key=lambda x: x["vol_surge"], reverse=True)[:6]
    h52_rows = sorted([x for x in rows if x.get("near_52h", 0) >= 0.90],
                      key=lambda x: x["near_52h"], reverse=True)[:6]
    surge_rows = sorted([x for x in rows if x["r5"] > 3.0],
                        key=lambda x: x["r5"], reverse=True)[:6]
    radar_volume = [_axis_item(x, f"거래량 ×{x['vol_surge']:.1f}") for x in vol_rows]
    radar_52w = [_axis_item(x, f"52주 {x['near_52h']*100:.0f}%") for x in h52_rows]
    radar_surge = [_axis_item(x, f"5일 +{x['r5']:.1f}%") for x in surge_rows]

    ai = []
    for x in rows[:6]:
        buy = x["aligned"] and x["r20"] > 0
        ai.append({"act": "BUY" if buy else "HOLD", "tk": x["tk"], "name": x["name"],
                   "conf": x["score"] if buy else max(0, round((x["score"] - 60) * 1.2)),
                   "r5": round(x["r5"], 1), "r20": round(x["r20"], 1), "aligned": x["aligned"]})

    # ── 포트폴리오: 실 저널 우선 ──
    port = None if sim else read_engine_state(offline=offline, closes=closes)
    if port is None:
        port = sim_portfolio(rows, closes)

    # ── 모의거래(paper) — 페르소나 3종(버핏·우드·오닐), 격리 home 별로. 실거래와 독립. ──
    persona_list = []
    for nm, home in _persona_homes():
        pp = read_persona(nm, home, closes=closes, offline=offline)
        if pp:
            persona_list.append(pp)
    paper = {"personas": persona_list} if persona_list else None
    # 폴백 — 페르소나 home 이 아직 없으면(로컬/미설정) 대시보드 자체 home 의 paper 기록을 단일 항목으로.
    if paper is None:
        pst = read_engine_state(offline=offline, closes=closes, broker="paper")
        if pst:
            paper = {"personas": [{
                "name": "local", "label": "로컬 모의 (페르소나 미설정)", "strategy": "",
                "seed": None, "ret": pst["summary"].get("pnlpct", 0.0),
                "summary": pst["summary"], "holdings": pst["holdings"],
                "equity_curve": pst.get("equity_curve", []),
                "curve": pst.get("curve_v", []),        # 누적수익률 차트 스키마 일치(read_persona 정상경로와 동일)
                "curve_dates": pst.get("curve_d", []),
                "halted": pst.get("halted"), "status": pst.get("status"), "ts": pst.get("ts", ""),
                "trades": read_trades(broker="paper"),
                "fees": read_fees(broker="paper"),
                "decisions": read_decisions(broker="paper"),
            }]}
    if paper:   # F1 페르소나 비교(개요 라인차트+리더보드) — 이미 만든 personas 리스트의 순수 후가공, 재조회 없음
        paper["curves"] = [{"name": p["name"], "label": p["label"], "dates": p.get("curve_dates", []),
                            "ret": norm_curve(p.get("curve", []), p.get("seed"))} for p in paper["personas"]]
        paper["stats"] = [{"name": p["name"], "label": p["label"],
                           **persona_stats(p.get("trades", []), p.get("seed"), p.get("curve", []))}
                          for p in paper["personas"]]
        # ── SPY 벤치마크 곡선 — 비교차트 오버레이. 기준=페르소나 최초 시작일의 SPY 종가(0%). ──
        _spy = closes.get("SPY")
        if _spy is None:   # 스캔 유니버스에 SPY 없는 캐시(VM) — 개별 로드 폴백(캐시됨, 온라인 1회 다운로드)
            try:
                _df = _load_ohlcv("SPY", allow_net=not offline)
                if _df is not None and len(_df):
                    _spy = pd.to_numeric(_df["Close"], errors="coerce").dropna()
            except Exception:
                _spy = None
        _all_pd = sorted({d for p in paper["personas"] for d in (p.get("curve_dates") or [])})
        if _spy is not None and _all_pd:
            sw = _spy[_spy.index >= _all_pd[0]]
            if len(sw) >= 2:
                base = float(sw.iloc[0])
                paper["spy"] = {"dates": [str(i.date()) for i in sw.index],
                                "ret": [round((float(v) / base - 1) * 100, 2) for v in sw.values]}

    # ── 지수: 라이브 → 폴백 ──
    market, mkt_live = None, False
    if not offline:
        try:
            market, mkt_live = _timeout(live_indices, 18), True
        except Exception as e:
            print(f"   [지수] 라이브 실패 → 내부지표 폴백 ({type(e).__name__})")
    if market is None:
        spy = closes.get("SPY")
        spy_last = float(spy.iloc[-1]) if spy is not None else 0.0
        above = sum(1 for x in rows if x["last"] > x["ma20"])
        breadth = above / len(rows) * 100.0 if rows else 0.0   # rows 빈(SPY만) → 0나눗셈 방지
        avg_mom = sum(x["r20"] for x in rows) / len(rows) if rows else 0.0
        market = [
            {"k": "S&P 500", "p": round(spy_last, 2),
             "c": round(pct(spy_last, float(spy.iloc[-2])), 2) if spy is not None else 0.0,
             "abs": round(spy_last - float(spy.iloc[-2]), 2) if spy is not None else 0.0,
             "prevClose": round(float(spy.iloc[-2]), 2) if spy is not None else None,
             "spark": spark(spy) if spy is not None else [], "tag": "", "fmt": "num"},
            # c 가 일간변화(델타)가 아님 — Breadth=중립50 대비 오프셋, Avg Mom=지표 레벨(=p).
            # nodelta 로 표시해 프론트가 +/- 색 변화처럼 오표기하지 않게(F5). spark/abs 없음(프론트 가드).
            {"k": "Breadth", "p": round(breadth, 1), "c": round(breadth - 50, 1), "fmt": "pct", "nodelta": True},
            {"k": "Avg Mom 20D", "p": round(avg_mom, 2), "c": round(avg_mom, 2), "fmt": "pct", "nodelta": True},
        ]
    above = sum(1 for x in rows if x["last"] > x["ma20"])
    insights = [
        {"k": market[0]["k"], "t": "광범위 지수 추세가 시스템 노출(리스크-온/오프)의 기준선"},
    ]
    if rows:   # rows 빈(SPY 단독) 시 above/len(rows) ZeroDivisionError 방지 — Breadth insight 생략
        insights.append(
            {"k": "Breadth", "t": (f"유니버스 {len(rows)}종목 중 {above}개가 20일선 위 — 추세 폭 양호"
                                   if above / len(rows) >= 0.5
                                   else f"유니버스 {len(rows)}종목 중 {above}개만 20일선 위 — 추세 폭 약함")})

    # ── 뉴스: 라이브 → 폴백 ──
    news, news_live = STATIC_NEWS, False
    if not offline:
        try:
            tks = [r["tk"] for r in radar[:4]] + ["SPY"]
            news, news_live = _timeout(lambda: live_news(tks), 10), True
        except Exception as e:
            print(f"   [뉴스] 라이브 실패 → 샘플 폴백 ({type(e).__name__})")

    # ── Strategy / Control ──
    spy = closes.get("SPY")
    spy_ret = pct(float(spy.iloc[-1]), float(spy.iloc[-(HOLD_LOOKBACK + 1)])) if spy is not None else 0.0
    # pnl 은 stale(가격결손) 종목서 None — 승리 미판정으로 취급(None > 0 TypeError 방지).
    win = round(sum(1 for h in port["holdings"] if (h["pnl"] or 0) > 0) / max(1, len(port["holdings"])) * 100)
    strategy = [
        {"name": "RS Momentum" if port["source"] == "simulated" else f"Live · {port['kind']}",
         "ret": port["summary"]["pnlpct"],
         "line": (f"실현 $0 · 평가 {port['summary']['pnl']:+,.0f}" if port["source"] == "simulated"
                  else f"세션 {port.get('session','')} · 당일 {port.get('daily_pnl_pct',0):+.2f}%"),
         "win": win, "orders": len(port["holdings"])},
        {"name": "Buy & Hold SPY", "ret": round(spy_ret, 2),
         "line": "벤치마크 · S&P 500 동일기간", "win": 0, "orders": 0},
    ]
    # 스캔/후보/확정 카운트 — 마지막 선정의 실제 수치(가짜 증가 카운터 아님). 후보=모멘텀 통과 풀,
    # 확정=펀더 스크린 후 최종 보유. 최근 결정저널의 selection 에서 취득.
    decisions = read_decisions()
    _last_sel = (decisions[0].get("sel") if decisions else None) or {}

    # 편집 가능 설정 — 영속(control_settings.json) 값 우선, 없으면 RunConfig 기본. server.api_settings
    # 가 검증·저장, run_live._apply_dashboard_settings 가 엔진에 주입. None=필터 해제(시총).
    _saved = {}
    try:
        from paths import STATE_DIR
        _sf = STATE_DIR / "control_settings.json"
        if _sf.exists():
            _saved = json.loads(_sf.read_text(encoding="utf-8")) or {}
    except Exception:
        _saved = {}
    from live_engine import RunConfig as _RC
    _dflt = _RC()
    def _ev(key, fallback):
        return _saved.get(key, fallback)
    editable = [
        {"key": "top_n", "label": "보유 종목수", "value": _ev("top_n", _dflt.top_n), "hint": "1~20"},
        {"key": "max_pe", "label": "P/E 상한", "value": _ev("max_pe", _dflt.max_pe), "hint": "1~1000"},
        {"key": "min_margin", "label": "최소 순이익률", "value": _ev("min_margin", _dflt.min_margin), "hint": "-1~1 (0.05=5%)"},
        {"key": "min_market_cap", "label": "시총 하한($)", "value": _ev("min_market_cap", ""), "hint": "USD, 빈칸=해제"},
        {"key": "max_market_cap", "label": "시총 상한($)", "value": _ev("max_market_cap", ""), "hint": "USD, 빈칸=해제"},
        {"key": "vol_target", "label": "변동성 타겟", "value": _ev("vol_target", _dflt.vol_target), "hint": "0~2 (0.20=20%)"},
    ]
    control = {
        "mode": "AUTO", "risk": "HALTED" if port.get("halted") else "ARMED",
        "channel": read_notify_fail(),   # 알림 채널 헬스(notify_fail.flag) — 채널死 SPOF 가시화
        "radar": len(rows), "next_scan": "09:30:00 ET", "cycle": "1h",
        "scan_count": len(rows),                                  # 스캔한 종목 수
        "candidate_count": len(_last_sel.get("candidates", [])),  # 모멘텀 통과 후보
        "confirmed_count": len(_last_sel.get("picks", [])),       # 펀더 스크린 후 확정 보유
        "editable": editable,                                     # 편집 가능 설정(웹UI Save → server.api_settings)
        "params": [
            {"k": "Source", "v": "Live Journal" if port["source"].startswith("broker") else "Simulated"},
            {"k": "Broker", "v": port.get("kind", "paper")},
            {"k": "Universe", "v": f"{len(rows)} names"},
            {"k": "Top N", "v": str(TOP_HOLDINGS)},
            {"k": "Status", "v": port.get("status", "—")},
            {"k": "HWM", "v": f"${port.get('hwm', port['summary']['total']):,.0f}"},
        ],
    }

    state = market_state()
    src_label = ("실 계좌 · " + port["kind"]) if port["source"].startswith("broker") else "시뮬레이션"
    risk = port.get("risk")
    if risk:   # F2 — 한도 근접도(가드레일 프록시미티). 시뮬(risk=None)은 스킵.
        def _prox(g):
            lim = g.get("limit") or 0
            return {"frac": round(abs(g.get("val", 0)) / abs(lim), 3) if lim else 0.0,
                    "breached": g.get("val", 0) <= lim}
        risk["proximity"] = {"daily": _prox(risk["daily"]), "drawdown": _prox(risk["drawdown"])}
    data = {
        "meta": {
            "title": "AI 미국주식 자동매매", "subtitle": "Quant + AI · 모멘텀 자동매매",
            "state": state, "as_of": as_of.strftime("%m/%d"),
            # %z(+0900) 로 TZ 명시 — 라벨 없으면 ET 거래일(as_of)과 혼동. astimezone=호스트 로컬TZ.
            "generated_at": datetime.now(_KST).strftime("%Y/%m/%d %H:%M:%S %z"), "currency": "$",
            "source": port["source"], "source_label": src_label,
            "live_index": mkt_live, "live_news": news_live,
            "delay_note": (f"포트폴리오={src_label} · 지수={'라이브' if mkt_live else '내부지표'} · "
                           f"뉴스={'라이브' if news_live else '샘플'} · 스캔 as of {as_of.strftime('%Y-%m-%d')}"),
            "protected": port.get("protected", []),
        },
        "summary": port["summary"], "strategy": strategy, "market": market, "insights": insights,
        "radar": radar, "radar_volume": radar_volume, "radar_52w": radar_52w, "radar_surge": radar_surge,
        "holdings": port["holdings"], "equity_curve": port.get("equity_curve", []),
        "curve": port.get("curve_v", []), "curve_dates": port.get("curve_d", []),   # 실계좌 누적곡선 일봉+날짜
        "ai": ai, "news": news, "control": control, "alerts": read_alerts(),
        "reconcile": port.get("reconcile"), "sleeve": port.get("sleeve"),
        "risk": risk, "trades": read_trades(), "paper": paper,
        "performance": compute_performance(closes), "next_session": next_session_iso(),
        "decisions": decisions, "fees": read_fees(),
        "drawdown_curve": running_drawdown(port.get("equity_curve", [])),   # F2 — 메인 계좌 드로다운 미니차트
        "audit": read_alerts(60),   # F4 — 감사 히스토리(런/청산/패닉 저널, 60건)
        "feed": read_trade_feed(),  # 통합 거래 이벤트 피드(페르소나+실계좌, Control 탭)
    }

    # ── 감시 상태(홈 배지) — dead-man 최근점검·알림채널·FMP 스크린 건강·MTM/계좌 폴백. ──
    hb = None
    try:
        from paths import STATE_DIR as _SD
        _hf = os.path.join(str(_SD), "heartbeat_status.json")
        if os.path.exists(_hf):
            hb = json.loads(open(_hf, encoding="utf-8").read())
            try:   # 점검 나이(분) — ts 는 호스트 로컬 naive(heartbeat 와 동일 규약)
                hb["age_min"] = round((datetime.now()
                                       - datetime.fromisoformat(hb.get("ts", ""))).total_seconds() / 60.0)
            except Exception:
                pass
    except Exception:
        hb = None
    fmp_watch = {"degraded": False, "stale_age_d": 0.0, "missing_ratio": 0.0}
    for p in ((paper or {}).get("personas") or []):
        d0 = ((p.get("decisions") or [{}])[0] or {}).get("sel") or {}
        fmp_watch["degraded"] = fmp_watch["degraded"] or bool(d0.get("degraded"))
        fmp_watch["stale_age_d"] = max(fmp_watch["stale_age_d"], d0.get("fmp_stale_age") or 0.0)
        fmp_watch["missing_ratio"] = max(fmp_watch["missing_ratio"], d0.get("missing_ratio") or 0.0)
    data["watch"] = {
        "heartbeat": hb,                                   # None=상태파일 없음(dead-man 미가동 의심)
        "notify_down": bool(control["channel"].get("down")),
        "fmp": fmp_watch,
        "live_mtm_failed": bool(port.get("live_mtm_failed")),
        "acct_error": bool(port.get("acct_error")),
    }
    data["selection_review"] = read_selection_review()    # 신호 성과 리포트(md) — Journal 탭

    return data


def symbol_series(tk, n=80, allow_net=True):
    """종목 상세 차트용 — OHLCV + MA5/MA20 (캔들+거래량). 라이브 data.load 캐시 우선, 오프라인은 정적 폴백."""
    cols = ["Open", "High", "Low", "Close", "Volume"]
    df = _load_ohlcv(tk, allow_net=allow_net)
    if df is not None:
        try:
            df = df[cols].dropna()
        except Exception:
            df = None
    if df is None or len(df) < 5:
        return {"tk": tk, "ok": False}
    df = df.iloc[-n:]
    close = df["Close"]
    ma5, ma20 = close.rolling(5).mean(), close.rolling(20).mean()
    nz = lambda x: None if pd.isna(x) else round(float(x), 2)
    r2 = lambda col: [round(float(x), 2) for x in df[col]]
    return {"tk": tk, "ok": True, "name": NAMES.get(tk, tk), "sector": SECTORS.get(tk, "—"),
            "dates": [d.strftime("%m/%d") for d in df.index],
            "open": r2("Open"), "high": r2("High"), "low": r2("Low"), "close": r2("Close"),
            "vol": [int(x) for x in df["Volume"]],
            "ma5": [nz(x) for x in ma5], "ma20": [nz(x) for x in ma20]}


def symbol_intraday(tk, rng):
    """종목 팝업 온디맨드 분봉 — 1d=최근장(5m)·5d=1주(30m)·1mo=1개월(90m). symbol_series 와 동일 shape."""
    spec = {"1d": ("1d", "5m", "%H:%M"), "5d": ("5d", "30m", "%m/%d %H:%M"),
            "1mo": ("1mo", "90m", "%m/%d %H:%M")}.get(rng)
    if spec is None:
        return {"tk": tk, "ok": False}
    period, interval, dfmt = spec
    try:
        import yfinance as yf
        df = yf.Ticker(tk).history(period=period, interval=interval, auto_adjust=False)
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:
        return {"tk": tk, "ok": False}
    if df.empty or len(df) < 3:
        return {"tk": tk, "ok": False}
    idx = df.index if df.index.tz is not None else df.index.tz_localize("UTC")
    df.index = idx.tz_convert(_KST)   # 표시 시각 정책: 전부 KST (미장 09:30–16:00 ET → 익일 22:30–05:00 KST)
    close = df["Close"]
    ma5, ma20 = close.rolling(5).mean(), close.rolling(20).mean()
    nz = lambda x: None if pd.isna(x) else round(float(x), 2)
    r2 = lambda col: [round(float(x), 2) for x in df[col]]
    return {"tk": tk, "ok": True, "name": NAMES.get(tk, tk), "sector": SECTORS.get(tk, "—"),
            "dates": [d.strftime(dfmt) for d in df.index],
            "open": r2("Open"), "high": r2("High"), "low": r2("Low"), "close": r2("Close"),
            "vol": [int(x) for x in df["Volume"]],
            "ma5": [nz(x) for x in ma5], "ma20": [nz(x) for x in ma20]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="네트워크 미사용(지수=내부지표, 뉴스=샘플)")
    ap.add_argument("--sim", action="store_true", help="포트폴리오 강제 시뮬(실 저널 무시)")
    args = ap.parse_args()
    data = build(offline=args.offline, sim=args.sim)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("window.DASH_DATA = ")
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write(";\n")
    s = data["summary"]
    m = data["meta"]
    print(f"OK  {OUT}")
    print(f"    portfolio={m['source']}  total=${s['total']:,.0f}  pnl={s['pnl']:+,.0f} "
          f"({s['pnlpct']:+.2f}%)  positions={s['positions']}")
    print(f"    index={'live' if m['live_index'] else 'internal'}  news={'live' if m['live_news'] else 'sample'}  "
          f"radar={len(data['radar'])}  protected={len(m['protected'])}")

    # 신호 성과 HTML 리포트 재생성 (대시보드 build 경로에서만 — cron 매매 배치 무지연).
    # guarded: 리포트 실패가 대시보드 데이터 생성을 절대 막지 않음.
    try:
        sys.path.insert(0, os.path.dirname(HERE))
        import report_html
        p = report_html.build_and_write_dashboard()
        print(f"    report={os.path.basename(p)}  (/report.html)")
    except Exception as e:
        print(f"    report=skip ({e!r})")


if __name__ == "__main__":
    main()
