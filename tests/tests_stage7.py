"""Stage 7 (재감사 후속 — 캐시·노브 수정) 검증 — 네트워크 불필요.

M-A 가격캐시: end 를 키에서 빼 날짜 넘어 재사용 + 종목당 단일파일(무한증가 차단) + 옛키 정리.
M-B FMP캐시 TTL: 만료 시 재호출, 레이트로 재호출 실패하면 만료 캐시 폴백.
M-C lookback 노브: select 가 lookback 을 모멘텀에 실제로 전달.
실행:  & $py tests_stage7.py
"""
import os
import sys
import time
import types

import numpy as np
import pandas as pd

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ───── M-A — 가격캐시 키에서 end 제거 (날짜 넘어 재사용 + 단일파일 + 옛키 정리) ─────
def test_m_a_price_cache(tmp):
    print("[M-A] 가격캐시: 날짜 넘어 재사용·종목당 단일파일·옛 dated 키 정리")
    import data
    calls = {"n": 0}

    def fake_download(ticker, start=None, end=None, auto_adjust=True, progress=False):
        calls["n"] += 1
        idx = pd.bdate_range(start=start, end=pd.Timestamp(end) - pd.Timedelta(days=1))
        n = len(idx)
        return pd.DataFrame({"Open": np.arange(n), "High": np.arange(n), "Low": np.arange(n),
                             "Close": 100.0 + np.arange(n), "Volume": np.ones(n)}, index=idx)

    orig_dir, orig_yf = data.CACHE_DIR, data.yf
    data.CACHE_DIR = str(tmp)
    data.yf = types.SimpleNamespace(download=fake_download)
    try:
        data.load("AAA", "2022-01-01", "2026-05-30")   # 다운로드 #1
        d2 = data.load("AAA", "2022-01-01", "2026-05-30")   # 같은 요청 → 캐시
        check("동일 (start,end) 재요청은 캐시 히트(재다운로드 안 함)", calls["n"] == 1, calls)
        check("exclusive end 슬라이스 (end 이상 봉 없음)",
              d2.index.max() < pd.Timestamp("2026-05-30"), d2.index.max())

        # end 가 캐시 커버 범위 내(과거) → 재다운로드 없음
        data.load("AAA", "2022-01-01", "2026-05-20")
        check("더 이른 end(캐시 커버) → 재다운로드 안 함", calls["n"] == 1, calls)

        # end 가 캐시 너머로 전진 → 1회 재다운로드, 그 뒤 같은 end 는 캐시
        data.load("AAA", "2022-01-01", "2026-06-09")
        data.load("AAA", "2022-01-01", "2026-06-09")
        check("end 전진 시 1회만 재다운로드", calls["n"] == 2, calls)

        # 종목·start 당 캐시 파일은 1개뿐 (무한 증가 아님)
        files = list(tmp.glob("AAA_*.csv"))
        check("종목당 캐시 파일 1개 (무한증가 차단)", len(files) == 1, [p.name for p in files])

        # 옛 dated 키 파일이 있으면 정리됨
        (tmp / "AAA_2022-01-01_2099-01-01.csv").write_text("stale", encoding="utf-8")
        data.load("AAA", "2022-01-01", "2026-06-30", force=True)
        legacy = list(tmp.glob("AAA_2022-01-01_*.csv"))
        check("옛 dated 캐시키 정리됨", legacy == [], [p.name for p in legacy])
    finally:
        data.CACHE_DIR, data.yf = orig_dir, orig_yf


# ───── M-B — FMP 캐시 TTL + 레이트 시 stale 폴백 ─────
class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"{self.status_code}")


def test_m_b_fmp_ttl(tmp):
    print("[M-B] FMP 캐시 TTL: 만료 재호출 + 레이트로 실패 시 만료캐시 폴백")
    import fmp_client as fc
    fetches = {"n": 0, "status": 200}

    def fake_get(url, params=None, timeout=None):
        fetches["n"] += 1
        return _Resp(fetches["status"], [{"priceToEarningsRatioTTM": 12.3}])

    orig_dir, orig_key, orig_req = fc.CACHE_DIR, fc.load_key, fc.requests.get
    fc.CACHE_DIR = tmp
    fc.load_key = lambda: "TESTKEY"
    fc.requests.get = fake_get
    try:
        fmp = fc.FMP(min_interval=0.0, retry_402=0, cache_ttl_days=7.0)
        fmp.get("ratios-ttm", symbol="AAA")   # fetch #1 (캐시 기록)
        fmp.get("ratios-ttm", symbol="AAA")   # TTL 내 → 캐시
        check("TTL 내 재요청은 캐시(재호출 안 함)", fetches["n"] == 1, fetches)

        # 캐시 파일 mtime 을 8일 전으로 → 만료 → 재호출
        import hashlib
        ck = hashlib.md5(f"ratios-ttm|{sorted({'symbol':'AAA'}.items())}".encode()).hexdigest()
        cf = tmp / f"ratios-ttm_{ck}.json"
        old = os.path.getmtime(cf) - 8 * 86400
        os.utime(cf, (old, old))
        fmp.get("ratios-ttm", symbol="AAA")   # 만료 → fetch #2
        check("TTL 만료 → 재호출", fetches["n"] == 2, fetches)

        # 다시 만료시키고 레이트(429)로 만들면 → 만료 캐시로 폴백(예외 없음)
        os.utime(cf, (old, old))
        fetches["status"] = 429
        out = fmp.get("ratios-ttm", symbol="AAA")
        check("레이트로 갱신 실패 → 만료 캐시 폴백", out and out[0]["priceToEarningsRatioTTM"] == 12.3, out)

        # 캐시 없는 신규 심볼 + 레이트 → RateLimited 발생
        raised = False
        try:
            fmp.get("ratios-ttm", symbol="ZZZ")
        except fc.RateLimited:
            raised = True
        check("캐시 없음 + 레이트 → RateLimited raise", raised)
    finally:
        fc.CACHE_DIR, fc.load_key, fc.requests.get = orig_dir, orig_key, orig_req


# ───── M-C — select 가 lookback 을 모멘텀에 실제 전달 ─────
def test_m_c_lookback_knob():
    print("[M-C] lookback 노브: select → F.momentum 으로 전달 (이전엔 무시됐음)")
    import strategies.factors as F
    idx = pd.bdate_range(end="2026-05-29", periods=300)
    prices = pd.DataFrame({"AAA": np.linspace(100, 300, 300),
                           "BBB": np.linspace(100, 150, 300),
                           "CCC": np.linspace(100, 120, 300)}, index=idx)

    # 단위: momentum_6_1 == momentum(126, 21)
    a = F.momentum_6_1(prices)
    b = F.momentum(prices, lookback=126, skip=21)
    check("momentum_6_1 == momentum(126,21)", a.equals(b))
    # 다른 lookback → 다른 값 (노브가 실제 동작)
    c = F.momentum(prices, lookback=252, skip=21)
    check("lookback 다르면 모멘텀 값 달라짐", not a.iloc[-1].equals(c.iloc[-1]))

    # select 가 lookback 을 전달하는지 (캡처)
    import live_select
    captured, orig_mom, orig_snap = {}, live_select.F.momentum, live_select.ff.snapshot

    def spy_mom(p, lookback=126, skip=21):
        captured["lookback"] = lookback
        return orig_mom(p, lookback=lookback, skip=skip)

    live_select.F.momentum = spy_mom
    live_select.ff.snapshot = lambda cands, fmp=None: pd.DataFrame()   # 네트워크 차단
    try:
        live_select.select(prices, lookback=200, top_n=2, pool=3)
    finally:
        live_select.F.momentum, live_select.ff.snapshot = orig_mom, orig_snap
    check("select(lookback=200) → 모멘텀에 200 전달", captured.get("lookback") == 200, captured)


# ───── L-D — top_n/바운드 모순 설정을 명시적 error 로 조기 차단 ─────
def test_l_d_config_sanity():
    print("[L-D] top_n 이 단일비중 한도와 모순이면 영구트립 대신 명시적 error")
    from tests_stage1 import _use_temp_state, _fake_select
    from tests_stage4 import _Broker
    import live_engine
    from live_engine import RunConfig, run_once

    # top_n=2 → 1/2=50% > 바운드 40% → 매 실행 트립하던 함정. 이제 select 前 error 로 조기 반환.
    _use_temp_state()
    res = run_once(None, None, RunConfig(top_n=2, vol_target=0.0, max_staleness_sessions=0),
                   today="2026-06-01", force=True)
    check("top_n=2 → status=error (영구정지 아님)", res["status"] == "error", res["status"])
    check("사유에 top_n/바운드 명시", "top_n" in res.get("reason", ""), res.get("reason"))

    # 정상 설정(top_n=3 → 33%<40%)은 조기차단 안 하고 정상 진행
    _use_temp_state()
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.34, "BBB": 0.33, "CCC": 0.33})
    try:
        res2 = run_once(None, _Broker(fill=True),
                        RunConfig(top_n=3, vol_target=0.0, max_staleness_sessions=0),
                        today="2026-06-02", force=True)
    finally:
        live_engine.select = orig
    check("top_n=3 → 정상 진행(ok)", res2["status"] == "ok", res2["status"])


# ───── L-E — RunLock 좀비락 동시회수 경합 → LockBusy (크래시 아님) ─────
def test_l_e_runlock_steal_race(tmp):
    print("[L-E] 좀비락 동시 회수 경합 시 FileExistsError 누출 → LockBusy 로 수렴")
    import broker.guardrail as g
    lockpath = tmp / "run.lock"
    lockpath.write_text("x", encoding="utf-8")
    old = time.time() - g._LOCK_STALE_SEC - 100   # 좀비(>1h)
    os.utime(lockpath, (old, old))

    orig_open = g.RunLock._open

    def boom(self):   # 회수 후 재생성 경합 모사 — _open 이 항상 FileExistsError
        raise FileExistsError()

    g.RunLock._open = boom
    outcome = None
    try:
        with g.RunLock(path=lockpath):
            pass
    except g.LockBusy:
        outcome = "lockbusy"
    except FileExistsError:
        outcome = "filenotcaught"
    finally:
        g.RunLock._open = orig_open
    check("좀비락 동시회수 → LockBusy (FileExistsError 누출 아님)", outcome == "lockbusy", outcome)


def main():
    import tempfile
    from pathlib import Path
    print("=" * 70)
    print(" Stage 7 (재감사 후속 — 캐시·노브·설정·락) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    test_m_a_price_cache(Path(tempfile.mkdtemp()))
    print()
    test_m_b_fmp_ttl(Path(tempfile.mkdtemp()))
    print()
    test_m_c_lookback_knob()
    print()
    test_l_d_config_sanity()
    print()
    test_l_e_runlock_steal_race(Path(tempfile.mkdtemp()))
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
