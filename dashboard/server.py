"""대시보드 서버 — 정적 서빙 + 라이브 API + 제어(토큰 가드).

엔드포인트:
  GET  /                     index.html (+ data.js 폴백) 정적 서빙
  GET  /api/dashboard        대시보드 JSON (60s 캐시, ?refresh=1 강제)
  GET  /api/symbol/{tk}      종목 종가+MA 시계열 (차트용)
  GET  /api/health           상태 + control 활성 여부
  POST /api/control/run      run_live 1회 실행(백그라운드 잡)  [토큰]
  GET  /api/job/{id}         잡 상태
  POST /api/control/halt     수동 HALT (즉시 전면 정지, 무거래 안전)  [토큰]
  POST /api/control/resume   HALT 해제 + killswitch reset            [토큰]

안전:
  · control 엔드포인트는 env DASH_TOKEN 설정 시에만 활성. 미설정=읽기 전용.
  · run 기본 broker=paper(모의). toss(실매매)는 confirm=true 필수.
  · 폰 접근: DASH_HOST=0.0.0.0 으로 실행(같은 와이파이) — 토큰 없으면 control 잠김.

실행:
  python dashboard/server.py
  $env:DASH_TOKEN="비밀값"; $env:DASH_HOST="0.0.0.0"; python dashboard/server.py   # 폰+제어
"""
import hmac
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _p in (HERE, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi import Body, FastAPI, Header, HTTPException, Request   # noqa: E402
from fastapi.staticfiles import StaticFiles                  # noqa: E402
from starlette.responses import PlainTextResponse            # noqa: E402

import build_data                                            # noqa: E402
from logsetup import get_logger                              # noqa: E402

_log = get_logger("dashboard")
DASH_TOKEN = os.environ.get("DASH_TOKEN")        # control(매매·정지) 활성 토큰
DASH_SITE_PASS = os.environ.get("DASH_SITE_PASS")  # 설정 시 전 사이트 접근 차단(외부 노출용)
_TICKER_RE = re.compile(r"^[A-Za-z0-9.\-]{1,12}$")   # 티커 화이트리스트(경로순회·glob 인젝션 차단)


def _ct_eq(a, b) -> bool:
    """상수시간 문자열 비교 — 토큰/패스 타이밍 사이드채널 방지. None/빈값은 False."""
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


app = FastAPI(title="ustrade dashboard")


_READ_DATA_PATHS = ("/api/dashboard", "/api/symbol", "/api/translate")  # 계좌·시세 데이터 반환 read 엔드포인트


def _site_pass_ok(request: Request) -> bool:
    """?k= 또는 dashpass 쿠키가 DASH_SITE_PASS 와 일치하는지 — site_gate 와 동일 비교(상수시간)."""
    return (_ct_eq(request.query_params.get("k"), DASH_SITE_PASS)
            or _ct_eq(request.cookies.get("dashpass"), DASH_SITE_PASS))


@app.middleware("http")
async def site_gate(request: Request, call_next):
    """외부 터널 노출 대비 — DASH_SITE_PASS 설정 시 전 경로를 패스로 가드.

    ?k=<pass> 로 1회 접속 → 쿠키 저장. /api/health 만 예외(헬스체크).
    """
    if request.url.path.lower().endswith((".py", ".pyc")):     # 소스(.py/.pyc) 정적 서빙 차단 — 소스 노출 방지(대소문자 무관)
        return PlainTextResponse("not found", status_code=404)
    if not DASH_SITE_PASS or request.url.path == "/api/health":
        return await call_next(request)
    # 계좌 데이터 read 엔드포인트 명시 재확인(defense-in-depth) — 아래 블랑켓 가드가 이미 커버하지만,
    # 향후 리팩터로 블랑켓 범위가 좁아져도 이 경로들만은 항상 패스를 요구하도록 이중화.
    if request.url.path.startswith(_READ_DATA_PATHS) and not _site_pass_ok(request):
        return PlainTextResponse("접근 차단 — URL 끝에 ?k=<DASH_SITE_PASS> 붙여 접속", status_code=401)
    if _ct_eq(request.query_params.get("k"), DASH_SITE_PASS):
        resp = await call_next(request)
        # 터널(cloudflared)은 https 종단 후 X-Forwarded-Proto: https 로 전달 → 그 때만 Secure
        # (로컬 http localhost 는 Secure 면 쿠키 미저장되므로 https 일 때만).
        https = request.headers.get("x-forwarded-proto", "") == "https" or request.url.scheme == "https"
        resp.set_cookie("dashpass", DASH_SITE_PASS, max_age=86400,
                        httponly=True, samesite="lax", secure=https)
        return resp
    if _ct_eq(request.cookies.get("dashpass"), DASH_SITE_PASS):
        return await call_next(request)
    return PlainTextResponse("접근 차단 — URL 끝에 ?k=<DASH_SITE_PASS> 붙여 접속", status_code=401)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """전 응답 보안헤더 — 터널 공개 노출 대비 XSS/클릭재킹/MIME스니핑 완화막.
    site_gate 뒤에 정의 → 더 바깥(outermost)이라 site_gate 의 조기응답(.py 404·401)에도 적용.
    앱이 인라인 script/style/onclick 이라 CSP 는 'unsafe-inline' 허용(외부 origin·프레이밍·base 만 차단)."""
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Content-Security-Policy",
                            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
    return resp

_cache = {"t": 0.0, "data": None}
_cache_lock = threading.Lock()   # 콜드/만료 시 동시요청이 build() 중복 실행(thundering herd) 차단
_CACHE_TTL = 60.0


def get_dashboard(force=False):
    now = time.monotonic()
    if not force and _cache["data"] is not None and now - _cache["t"] < _CACHE_TTL:
        return _cache["data"]
    with _cache_lock:
        # 락 대기 중 다른 요청이 이미 빌드했으면 그 결과 재사용(double-checked) — 중복 build 방지
        now = time.monotonic()
        if not force and _cache["data"] is not None and now - _cache["t"] < _CACHE_TTL:
            return _cache["data"]
        try:
            data = build_data.build()
        except Exception as e:
            # build 실패 시 폰에 빈 화면 대신: 마지막 성공 스냅샷을 stale 플래그와 함께 폴백, 없으면 503.
            _log.error("[dashboard] build 실패: %s", e)
            if _cache["data"] is not None:
                stale = dict(_cache["data"])
                meta = dict(stale.get("meta") or {})
                meta["stale"] = True
                meta["stale_reason"] = f"데이터 생성 실패 — 마지막 성공본 표시 ({type(e).__name__})"
                stale["meta"] = meta
                return stale
            raise HTTPException(503, f"데이터 생성 실패: {e}")
        _cache["data"], _cache["t"] = data, now
        return _cache["data"]


@app.get("/api/dashboard")
def api_dashboard(refresh: int = 0):
    return get_dashboard(force=bool(refresh))


@app.get("/api/symbol/{tk}")
def api_symbol(tk: str):
    if not _TICKER_RE.match(tk):     # 경로순회·glob 인젝션 차단(미검증 tk 가 파일 glob 에 유입)
        raise HTTPException(400, "잘못된 티커")
    return build_data.symbol_series(tk.upper())


@app.get("/api/symbol/{tk}/intra")
def api_symbol_intra(tk: str, r: str = "1d"):
    if not _TICKER_RE.match(tk):     # 일봉과 동일 티커 화이트리스트
        raise HTTPException(400, "잘못된 티커")
    if r not in ("1d", "5d", "1mo"):        # 범위 화이트리스트
        raise HTTPException(400, "잘못된 범위")
    return build_data.symbol_intraday(tk.upper(), r)


@app.get("/api/health")
def api_health():
    return {"ok": True, "control": bool(DASH_TOKEN)}


# ───────── 뉴스 번역 프록시 (무키) ─────────
# 브라우저 CSP(connect-src 'self') 때문에 클라이언트가 외부로 직접 못 감 → 서버가 대신 호출·캐시.
# 엔진: Google 무료 gtx(문장분할·자연스러움 우수) 우선 → 실패 시 MyMemory → 원문.
_tr_cache = {}
_tr_lock = threading.Lock()


def _tr_google(q, to):
    """Google 무료 gtx — 뉴스체 번역 품질이 MyMemory 보다 훨씬 자연스러움."""
    import json as _json
    import urllib.parse
    import urllib.request
    url = "https://translate.googleapis.com/translate_a/single?" + urllib.parse.urlencode(
        {"client": "gtx", "sl": "auto", "tl": to, "dt": "t", "q": q})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        data = _json.loads(r.read().decode("utf-8", "replace"))
    segs = data[0] if isinstance(data, list) and data else []
    return "".join(s[0] for s in segs if s and s[0]).strip()   # 문장 세그먼트 이어붙임


def _tr_mymemory(q, to):
    import json as _json
    import urllib.parse
    import urllib.request
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode({"q": q[:480], "langpair": f"en|{to}"})
    req = urllib.request.Request(url, headers={"User-Agent": "ustrade-dash/1.0"})
    with urllib.request.urlopen(req, timeout=6) as r:
        data = _json.loads(r.read().decode("utf-8", "replace"))
    out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
    up = out.upper()
    if out and "MYMEMORY WARNING" not in up and "INVALID" not in up and "QUERY LENGTH" not in up:
        return out
    return ""


@app.get("/api/translate")
def api_translate(q: str = "", to: str = "ko"):
    q = (q or "").strip()[:1500]
    if not q:
        return {"text": ""}
    to = to if re.match(r"^[a-z]{2}$", to or "") else "ko"   # 대상언어 인젝션 차단
    key = (to, q)
    with _tr_lock:
        if key in _tr_cache:
            return {"text": _tr_cache[key]}
    text = ""
    for fn in (_tr_google, _tr_mymemory):
        try:
            out = (fn(q, to) or "").strip()
        except Exception:
            out = ""
        if out:
            text = out
            break
    if not text:
        text = q   # 전부 실패 → 원문
    with _tr_lock:
        if len(_tr_cache) > 500:
            _tr_cache.clear()
        _tr_cache[key] = text
    return {"text": text}


# ───────── control (토큰 가드) ─────────
_jobs = {}
_seq = [0]


def _auth(token):
    if not DASH_TOKEN:
        raise HTTPException(403, "control 비활성 — 서버에 DASH_TOKEN 설정 필요(읽기 전용)")
    if not _ct_eq(token, DASH_TOKEN):
        raise HTTPException(401, "토큰 불일치")


def _run_job(jid, broker, confirm_live=False):
    try:
        import run_live
        res = run_live.run(broker_kind=broker, confirm_live=confirm_live)
        _jobs[jid] = {"status": "done",
                      "result": {"status": res.get("status"), "reason": res.get("reason", "")}}
    except Exception as e:
        _jobs[jid] = {"status": "error", "result": {"reason": str(e)}}
    try:
        get_dashboard(force=True)
    except Exception:
        pass


@app.post("/api/control/run")
def api_run(body: dict = Body(default={}), x_dash_token: str = Header(default=None)):
    _auth(x_dash_token)
    broker = (body or {}).get("broker", "paper")
    # confirm 은 화이트리스트 검사 — truthy-만 보면 'false'/'no' 같은 비어있지않은 문자열도 통과(거부 의도 역전).
    _confirmed = (body or {}).get("confirm") in (True, 1, "1", "true", "True")
    if broker != "paper" and not _confirmed:
        raise HTTPException(400, "toss(실매매)는 confirm=true 필요")
    _log.warning("[control] run 요청 — broker=%s (실매매 트리거 감사로그)", broker)
    _seq[0] += 1
    jid = str(_seq[0])
    _jobs[jid] = {"status": "running", "result": None}
    if len(_jobs) > 20:    # done/error 잡 무한 누적 방지 — 최근 20개만 유지(상시 구동 서버 메모리 가드)
        for old in sorted(_jobs, key=int)[:-20]:
            _jobs.pop(old, None)
    # api_run 이 위에서 non-paper 에 confirm=true 강제(400 가드) → 통과분만 confirm_live=True 전달
    # (run_live 의 실거래 명시확인 게이트 충족). paper 는 confirm_live=False(게이트 무관).
    threading.Thread(target=_run_job, args=(jid, broker, (broker != "paper") and _confirmed), daemon=True).start()
    return {"job_id": jid, "broker": broker}


@app.get("/api/job/{jid}")
def api_job(jid: str):
    return _jobs.get(jid, {"status": "unknown"})


@app.post("/api/control/halt")
def api_halt(x_dash_token: str = Header(default=None)):
    _auth(x_dash_token)
    from paths import STATE_DIR, persona_homes
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "HALT").write_text("dashboard manual halt", encoding="utf-8")
    # 페르소나 계열(각자 USTRADE_HOME=persona_home, buffett/wood/oneil/livermore)은 계정 스코프
    # STATE_DIR 밖이라 위 쓰기가 안 닿는다 — persona_homes() 순회로 대칭 배선(계정 스코프 쓰기는 불변).
    for _home in persona_homes():
        _pd = _home / "state"
        _pd.mkdir(parents=True, exist_ok=True)
        (_pd / "HALT").write_text("dashboard manual halt", encoding="utf-8")
    try:
        from notify import notify
        notify("[dashboard] 수동 HALT — 자동매매 정지", "halt")
    except Exception:
        pass
    get_dashboard(force=True)
    return {"ok": True, "halted": True}


@app.post("/api/control/resume")
def api_resume(body: dict = Body(default={}), x_dash_token: str = Header(default=None)):
    _auth(x_dash_token)
    from paths import STATE_DIR, persona_homes
    # 주: api_resume 의 전역 HALT 해제는 운영자의 명시적 제어 경로(대시보드 halt/resume 쌍)다. 현재 HALT
    # 파일은 paper·toss 공유 단일 파일이라, toss 활성화 시 namespace별 HALT 분리가 근본수정(그 전엔
    # 공유 HALT 해제가 toss 도 함께 풀 수 있음 — toss 비활성 동안은 무영향). R7 게이트가 run_live persona
    # reset 의 무의도 HALT 삭제(부작용 경로)는 이미 차단.
    f = STATE_DIR / "HALT"
    if f.exists():
        f.unlink()
    # halt 와 대칭 — 페르소나 home 들도 순회 해제(전 home 순회 삭제, 계정 스코프 동작은 위와 불변).
    for _home in persona_homes():
        _pf = _home / "state" / "HALT"
        if _pf.exists():
            _pf.unlink()
    try:
        from broker.guardrail import KillSwitch
        KillSwitch(namespace=(body or {}).get("broker", "toss")).reset()
    except Exception as e:
        # reset 실패를 'ok·halted:false' 로 보고하면 실제론 정지인데 재개됐다 오인(무성실패).
        _log.error("[control] resume reset 실패: %s", e)
        return {"ok": False, "halted": True, "error": str(e)}
    try:
        from notify import notify
        notify("[dashboard] HALT 해제 — 자동매매 재개", "ok")
    except Exception:
        pass
    get_dashboard(force=True)
    return {"ok": True, "halted": False}


@app.post("/api/note")
def api_note(body: dict = Body(default={}), x_dash_token: str = Header(default=None)):
    """의사결정 저널 수동 메모 추가 (토큰 가드)."""
    _auth(x_dash_token)
    text = ((body or {}).get("text") or "").strip()
    if not text:
        raise HTTPException(400, "빈 메모")
    import datetime as _dt
    import json as _json
    from paths import STATE_DIR
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    rec = {"ts": (body or {}).get("ts", ""), "text": text[:500],
           "at": _dt.datetime.now().isoformat(timespec="seconds")}
    with open(STATE_DIR / "decision_notes.jsonl", "a", encoding="utf-8") as f:
        f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    get_dashboard(force=True)
    return {"ok": True}


# 편집 가능 설정 — 화이트리스트(키)·타입·범위 검증만 허용. 라이브 매매 파라미터를 웹UI로
# 바꾸므로 토큰 가드 + 임의 키 거부 + 범위 강제(엔진 RunConfig 와 의미 일치). run_live 가
# state/control_settings.json 을 읽어 RunConfig override.
def _coerce_setting(key, raw):
    """(value, error) — 화이트리스트 키만, 타입·범위 검증. None/빈값 → 필터 해제(시총만)."""
    if key in ("min_market_cap", "max_market_cap"):
        if raw in (None, "", "null"):
            return None, None                       # 시총 필터 해제
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, f"{key}: 숫자 아님"
        if v < 0:
            return None, f"{key}: 음수 불가"
        return v, None
    if key == "top_n":
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            return None, "top_n: 정수 아님"
        return (v, None) if 1 <= v <= 20 else (None, "top_n: 1~20")
    if key == "max_pe":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, "max_pe: 숫자 아님"
        return (v, None) if 1.0 <= v <= 1000.0 else (None, "max_pe: 1~1000")
    if key == "min_margin":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, "min_margin: 숫자 아님"
        return (v, None) if -1.0 <= v <= 1.0 else (None, "min_margin: -1~1")
    if key == "vol_target":
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None, "vol_target: 숫자 아님"
        return (v, None) if 0.0 <= v <= 2.0 else (None, "vol_target: 0~2")
    return None, f"{key}: 허용되지 않은 설정"


_SETTINGS_KEYS = ("top_n", "max_pe", "min_margin", "min_market_cap", "max_market_cap", "vol_target")


@app.post("/api/control/settings")
def api_settings(body: dict = Body(default={}), x_dash_token: str = Header(default=None)):
    """편집 가능 매매 설정 영속 (토큰 가드 + 화이트리스트 + 범위검증 + atomic write)."""
    _auth(x_dash_token)
    import json as _json
    from paths import STATE_DIR
    incoming = {k: v for k, v in (body or {}).items() if k in _SETTINGS_KEYS}
    if not incoming:
        raise HTTPException(400, "유효 설정 없음 (허용: " + ", ".join(_SETTINGS_KEYS) + ")")
    clean, errors = {}, []
    for k, raw in incoming.items():
        v, err = _coerce_setting(k, raw)
        if err:
            errors.append(err)
        else:
            clean[k] = v
    if errors:
        raise HTTPException(400, "; ".join(errors))
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fpath = STATE_DIR / "control_settings.json"
    existing = {}
    if fpath.exists():
        try:
            existing = _json.loads(fpath.read_text(encoding="utf-8")) or {}
        except Exception:
            existing = {}
    existing.update(clean)
    # 시총 None 은 필터 해제 의도 → 키 제거(엔진 default None 과 동일 의미)
    existing = {k: val for k, val in existing.items() if val is not None}
    tmp = fpath.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, fpath)                           # atomic
    _log.warning("[control] settings 갱신 — %s", clean)
    get_dashboard(force=True)
    return {"ok": True, "settings": existing}


@app.get("/report.html")
def report_page():
    """일일 리포트 — 없거나 1시간 넘게 낡았으면 지연 재생성 후 서빙.
    (생성 훅이 build_data CLI main 에만 있어 서버 단독 운영(VM)에선 파일이 안 생기던 것)"""
    from starlette.responses import FileResponse
    try:
        import report_html
        p = report_html.DASH_REPORT
        if (not os.path.exists(p)) or (time.time() - os.path.getmtime(p) > 3600):
            report_html.build_and_write_dashboard()
        return FileResponse(p, media_type="text/html")
    except Exception as e:                            # 리포트 실패가 대시보드를 못 깨게
        _log.warning("[report] 생성 실패: %r", e)
        return PlainTextResponse(f"리포트 생성 실패: {e!r}", status_code=503)


# React v2 병행 배포 — C:\dev\ustrade-dash 빌드 산출물(dashboard/v2/). 없으면 스킵(빌드 전 서버 기동 보호).
_V2_DIR = os.path.join(HERE, "v2")
if os.path.isdir(_V2_DIR):
    app.mount("/v2", StaticFiles(directory=_V2_DIR, html=True), name="v2")

# 정적 서빙은 맨 끝(api 라우트 우선)
app.mount("/", StaticFiles(directory=HERE, html=True), name="static")


def main():
    import uvicorn
    # pythonw(GUI 서브시스템)는 stdout/stderr가 None → uvicorn 로깅 설정이 예외로 즉사(exit 1).
    # 자동시작 .cmd가 pythonw로 띄우므로 파일로 대체(2026-07-10 실측 — 무음사 방지 겸 사후 진단용).
    if sys.stdout is None or sys.stderr is None:
        _gui_log = open(os.path.join(HERE, "pythonw.log"), "a", buffering=1,
                        encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = _gui_log
        if sys.stderr is None:
            sys.stderr = _gui_log
    host = os.environ.get("DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("DASH_PORT", "8765"))
    # fail-closed — 외부 바인드(비-loopback: 0.0.0.0/LAN/터널)인데 DASH_SITE_PASS 미설정이면 전 데이터
    # (실계좌 포함 /api/dashboard·정적 data.js)가 무인증 노출. 운영자 실수를 부팅 단계서 차단(site_gate 다층).
    def _is_loopback(h):
        h = (h or "").strip().strip("[]")            # 대괄호 IPv6 표기([::1]) 정규화
        if h == "localhost":
            return True
        try:
            import ipaddress
            return ipaddress.ip_address(h).is_loopback   # 127.0.0.0/8·::1·풀폼·IPv4매핑 전부 인식
        except ValueError:
            return False                                  # 호스트네임/0.0.0.0 등 → 외부 간주(보수)
    if not _is_loopback(host) and not DASH_SITE_PASS:
        print(f"거부 — 외부 바인드({host})인데 DASH_SITE_PASS 미설정. 무인증 전체노출 위험. "
              f"DASH_SITE_PASS 설정 후 재실행(또는 host=127.0.0.1).", file=sys.stderr)
        return 2
    print(f"dashboard → http://{host}:{port}   control={'ON' if DASH_TOKEN else 'OFF(read-only)'}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
