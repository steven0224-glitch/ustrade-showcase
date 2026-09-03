"""모의매매 페르소나 검증 — 네트워크 0 (합성 prices + ff.snapshot 몽키패치).

검증: 버핏(가치 스크린)·우드(성장 프록시) 선택 로직 + 페르소나 프리셋 + PaperBroker 다일 진화(책 영속)
+ live_engine 디스패치 등록. 실거래 경로(canslim/momentum)와 독립 — paper 전용.

실행:  python tests_personas.py
"""
import os
import sys
import tempfile

import pandas as pd

import fmp_factors as ff
import live_select_buffett as lb
import live_select_buffett_v2 as lb2
import live_select_wood as lw
import personas
import live_engine
import universe
from broker.paper import PaperBroker
from broker.base import OrderRequest, Side, OrderType, Position, Order
from broker.executor import Executor

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _prices(spec, periods=40):
    idx = pd.date_range("2024-01-01", periods=periods, freq="D")
    return pd.DataFrame({t: f(periods) for t, f in spec.items()}, index=idx)


def test_buffett_value_screen():
    print("[BUFFETT] 가치·우량 스크린 — 저PE·흑자 통과 + quality_value_score 랭킹, 고PE/적자 탈락")
    prices = _prices({
        "AAA": lambda n: [100 + i * 0.5 for i in range(n)],
        "BBB": lambda n: [100 + i * 0.4 for i in range(n)],
        "CCC": lambda n: [100 + i * 0.3 for i in range(n)],
        "DDD": lambda n: [100 + i * 0.2 for i in range(n)],
    })
    snap = pd.DataFrame({
        "pe": {"AAA": 10, "BBB": 18, "CCC": 40, "DDD": 12},
        "pb": {"AAA": 1.0, "BBB": 2.0, "CCC": 3.0, "DDD": 1.5},
        "ps": {"AAA": 2.0, "BBB": 3.0, "CCC": 5.0, "DDD": 2.5},
        "debt_equity": {"AAA": 0.3, "BBB": 0.8, "CCC": 1.0, "DDD": 5.0},
        "net_margin": {"AAA": 0.25, "BBB": 0.15, "CCC": 0.10, "DDD": 0.02},
        "div_yield": {"AAA": 0.02, "BBB": 0.01, "CCC": 0.0, "DDD": 0.0},
        "earnings_yield": {"AAA": 0.10, "BBB": 0.06, "CCC": 0.04, "DDD": 0.08},
        "fcf_yield": {"AAA": 0.08, "BBB": 0.05, "CCC": 0.03, "DDD": 0.01},
        "market_cap": {"AAA": 1e12, "BBB": 5e11, "CCC": 2e11, "DDD": 1e11},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        w, sel = lb.select(prices, lookback=20, top_n=2, pool=4, max_pe=25.0, min_margin=0.08)
    finally:
        ff.snapshot = orig
    check("최종 2종목 [AAA, BBB] (CCC=고PE·DDD=적자 탈락)", sel["final"] == ["AAA", "BBB"], sel["final"])
    check("AAA(최저PE·최고마진) 1순위", sel["final"][0] == "AAA", sel["final"])
    check("CCC P/E 탈락 사유", "CCC" in sel["fails"], sel["fails"])
    check("DDD 마진 탈락 사유", "DDD" in sel["fails"], sel["fails"])
    check("등비중 합 1.0", abs(sum(w.values()) - 1.0) < 1e-9, w)
    check("strategy 태그 buffett", sel["strategy"] == "buffett")


def test_wood_growth_screen():
    print("[WOOD] 파괴성장 — 모멘텀·배당 게이트 통과분을 고P/S 로 랭킹 (가치 스크린 없음, 적자 허용)")
    prices = _prices({
        "AAA": lambda n: [100 * (1.03 ** i) for i in range(n)],   # 최고 모멘텀
        "BBB": lambda n: [100 * (1.02 ** i) for i in range(n)],
        "CCC": lambda n: [100 * (1.01 ** i) for i in range(n)],
    }, periods=30)
    snap = pd.DataFrame({
        "pe": {"AAA": -50, "BBB": 200, "CCC": 30},      # 적자/고PE — 안 거름
        "pb": {"AAA": 20, "BBB": 10, "CCC": 4},
        "ps": {"AAA": 30, "BBB": 12, "CCC": 4},          # AAA 최고 매출프리미엄
        "debt_equity": {"AAA": 0.1, "BBB": 0.3, "CCC": 0.5},
        "net_margin": {"AAA": -0.2, "BBB": 0.05, "CCC": 0.12},
        "div_yield": {"AAA": 0.0, "BBB": 0.01, "CCC": 0.03},   # AAA 무배당(재투자)
        "earnings_yield": {"AAA": -0.02, "BBB": 0.005, "CCC": 0.03},
        "fcf_yield": {"AAA": -0.01, "BBB": 0.01, "CCC": 0.04},
        "market_cap": {"AAA": 3e11, "BBB": 2e11, "CCC": 1e11},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        w, sel = lw.select(prices, lookback=10, top_n=2, pool=3)
    finally:
        ff.snapshot = orig
    check("AAA(고P/S·무배당) 1순위", sel["final"][0] == "AAA", sel["final"])
    check("CCC(배당 3% > 1.5%) 게이트 배제", sel["div_gated"] == ["CCC"], sel["div_gated"])
    check("최종 2종목", len(sel["final"]) == 2, sel["final"])
    check("적자 AAA 안 걸러짐(가치 스크린 없음)", "AAA" in sel["final"], sel["final"])
    check("등비중 합 1.0", abs(sum(w.values()) - 1.0) < 1e-9, w)
    check("strategy 태그 wood", sel["strategy"] == "wood")


def test_wood_core_dropna_guard():
    print("[WOOD-FIX] core-dropna 가드 — ratios_ttm 실패(pe/net_margin 결측)면 ps/div_yield 있어도 missing 강등(모멘텀 위장 차단)")
    prices = _prices({
        "GOOD": lambda n: [100 * (1.02 ** i) for i in range(n)],
        "BAD": lambda n: [100 * (1.03 ** i) for i in range(n)],   # 최고 모멘텀이지만 펀더 오염
    }, periods=30)
    snap = pd.DataFrame({
        # BAD: ratios_ttm 엔드포인트만 실패 시나리오 — pe/net_margin(ratios 소스) 는 NaN 인데 ps/
        # div_yield(같은 ratios 소스)는 값이 남아있고(구캐시·부분응답 등 비정상) market_cap(key_metrics)만 정상.
        "pe": {"GOOD": 20.0, "BAD": None},
        "pb": {"GOOD": 5.0, "BAD": None},
        "ps": {"GOOD": 3.0, "BAD": 99.0},          # BAD 는 위장 고P/S(가드 없으면 성장점수 1위로 보임)
        "debt_equity": {"GOOD": 0.3, "BAD": None},
        "net_margin": {"GOOD": 0.1, "BAD": None},
        "div_yield": {"GOOD": 0.01, "BAD": 0.0},
        "earnings_yield": {"GOOD": 0.03, "BAD": None},
        "fcf_yield": {"GOOD": 0.02, "BAD": None},
        "market_cap": {"GOOD": 1e11, "BAD": 5e10},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        w, sel = lw.select(prices, lookback=10, top_n=2, pool=2)
    finally:
        ff.snapshot = orig
    check("BAD(core 결측) → missing 강등(위장 성장점수로 편입 안 됨)", "BAD" in sel["missing"], sel)
    check("final = [GOOD, BAD] (BAD 는 점수 없이 데이터갭 폴백으로 뒤에)", sel["final"] == ["GOOD", "BAD"], sel["final"])
    check("BAD scores 미기록(위장값 미채용)", "BAD" not in sel["scores"], sel["scores"])


def test_buffett_piotroski_isolation():
    print("[FIX] buffett value_trap veto — piotroski 종목별 예외 격리(A엔진 hiccup 이 veto 전체를 안 죽임)")
    prices = _prices({
        "AAA": lambda n: [100 + i * 0.5 for i in range(n)],
        "BBB": lambda n: [100 + i * 0.4 for i in range(n)],
    })
    snap = pd.DataFrame({
        "pe": {"AAA": 10, "BBB": 12}, "pb": {"AAA": 1.0, "BBB": 1.5},
        "ps": {"AAA": 2.0, "BBB": 2.5}, "debt_equity": {"AAA": 0.2, "BBB": 0.4},
        "net_margin": {"AAA": 0.25, "BBB": 0.20}, "div_yield": {"AAA": 0.02, "BBB": 0.02},
        "earnings_yield": {"AAA": 0.10, "BBB": 0.08}, "fcf_yield": {"AAA": 0.08, "BBB": 0.06},
        "market_cap": {"AAA": 1e12, "BBB": 5e11},
    })

    def _pio(t):
        if t == "AAA":
            raise RuntimeError("일시적 A엔진 장애")
        return {"score": 7, "tested": 8, "reliable": True}

    _trap = lambda f: bool(f.get("reliable")) and (f.get("score") or 0) < 5
    orig_snap, orig_load = ff.snapshot, lb._load_piotroski
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    lb._load_piotroski = lambda: (_pio, _trap)
    try:
        w, sel = lb.select(prices, lookback=20, top_n=2, pool=2, max_pe=25.0,
                           min_margin=0.08, value_trap_gate=True)
    finally:
        ff.snapshot, lb._load_piotroski = orig_snap, orig_load
    check("piotroski 예외에도 throw 안 함(격리) — final 정상 산출", sel["final"] == ["AAA", "BBB"], sel["final"])
    check("예외 종목은 미신뢰 취급(veto 안 됨, 데이터갭 정책과 일관)", "AAA" not in sel["excluded_value_trap"], sel)
    check("piotroski 정보엔 예외종목 score=None 기록(크래시 없이 결측)", sel["piotroski"].get("AAA") is None, sel["piotroski"])


def test_buffett_screen_degraded_graceful():
    print("[BUFFETT] FMP 전부 결측(레이트/키) → throw 안 함, screen_degraded 플래그 + missing 폴백")
    prices = _prices({"AAA": lambda n: [100 + i for i in range(n)],
                      "BBB": lambda n: [100 + i * 0.5 for i in range(n)]})
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: pd.DataFrame()   # 빈 = 전부 데이터 갭
    try:
        w, sel = lb.select(prices, lookback=20, top_n=2, pool=2)
    finally:
        ff.snapshot = orig
    check("throw 안 함, final 은 missing 폴백", isinstance(sel["final"], list), sel)
    check("screen_degraded True", sel["screen_degraded"] is True, sel)


def test_degraded_reasons_propagate():
    print("[A4] degrade 사유 전파 — 결측률 30% 미만이어도 '미검증 편입'이면 플래그+사유(다음날 아침 사람이 앎)")
    # 10종목 중 2개(20% < 30% 임계)만 결측 → screen_degraded_flag 단독으로는 False.
    # 그런데 top_n=7 이라 결측 2개가 실제 매수분에 들어간다 = 종전엔 무성 편입.
    tk = [f"T{i}" for i in range(10)]
    prices = _prices({t: (lambda n, k=i: [100 + k * 0.01 + j * (0.5 - k * 0.03)
                                          for j in range(n)]) for i, t in enumerate(tk)})
    ok = tk[:8]
    snap = pd.DataFrame({
        "pe": {t: 12.0 for t in ok}, "pb": {t: 1.5 for t in ok}, "ps": {t: 2.0 for t in ok},
        "debt_equity": {t: 0.4 for t in ok}, "net_margin": {t: 0.18 for t in ok},
        "div_yield": {t: 0.01 for t in ok}, "earnings_yield": {t: 0.08 for t in ok},
        "fcf_yield": {t: 0.05 for t in ok}, "market_cap": {t: 1e11 for t in ok},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sb = lb.select(prices, lookback=20, top_n=9, pool=10)
        _w2, sw = lw.select(prices, lookback=10, top_n=9, pool=10)
    finally:
        ff.snapshot = orig
    check("임계 단독 판정은 False(결측 2/10=20%)", ff.screen_degraded_flag(10, 2) is False)
    for name, sel in (("buffett", sb), ("wood", sw)):
        check(f"{name} 미검증 종목이 실제 편입됨(전제)", bool(sel["final_missing"]), sel["final_missing"])
        check(f"{name} screen_degraded True(알림 발화)", sel["screen_degraded"] is True, sel)
        check(f"{name} 사유에 '펀더 미검증 편입'",
              any("미검증 편입" in r for r in sel["degraded_reasons"]), sel["degraded_reasons"])
    # 대조군 — 전원 검증되면 플래그 안 켜짐(오탐 방지)
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w3, clean = lb.select(prices, lookback=20, top_n=3, pool=8)
    finally:
        ff.snapshot = orig
    check("전원 검증 편입 → degraded False·사유 없음",
          clean["screen_degraded"] is False and clean["degraded_reasons"] == [], clean)


def test_wood_unscored_momentum_fallback_flagged():
    print("[A4-2] wood 성장점수 결측→모멘텀 폴백이 사유로 남음(missing 도 아니라 종전 완전 무성)")
    prices = _prices({"AAA": lambda n: [100 * (1.03 ** i) for i in range(n)],
                      "BBB": lambda n: [100 * (1.02 ** i) for i in range(n)]}, periods=30)
    # AAA: core(pe·net_margin) 는 있어 missing 강등 안 됨. 그런데 ps 결측 → 성장점수 NaN → mom 폴백.
    snap = pd.DataFrame({
        "pe": {"AAA": 30.0, "BBB": 40.0}, "pb": {"AAA": 8.0, "BBB": 6.0},
        "ps": {"AAA": None, "BBB": 12.0},
        "debt_equity": {"AAA": 0.2, "BBB": 0.3}, "net_margin": {"AAA": 0.05, "BBB": 0.08},
        "div_yield": {"AAA": 0.0, "BBB": 0.0}, "earnings_yield": {"AAA": 0.01, "BBB": 0.02},
        "fcf_yield": {"AAA": 0.01, "BBB": 0.02}, "market_cap": {"AAA": 2e11, "BBB": 1e11},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sel = lw.select(prices, lookback=10, top_n=2, pool=2)
    finally:
        ff.snapshot = orig
    check("AAA 는 missing 아님(기존 경로로는 무성)", "AAA" not in sel["missing"], sel["missing"])
    check("모멘텀 폴백 사유 기록", any("모멘텀 폴백" in r for r in sel["degraded_reasons"]),
          sel["degraded_reasons"])
    check("screen_degraded True(알림 발화)", sel["screen_degraded"] is True, sel)


def test_buffett_missing_margin_rejected():
    print("[A15①] buffett 하드컷 — net_margin 결측은 무데이터 통과 대신 탈락(momentum/wood 경로 불변)")
    # NOMARGIN: pe 는 있어 core dropna 를 통과 → 종전엔 마진 미검증인데 스크린 통과·랭킹 진입.
    snap = pd.DataFrame({
        "pe": {"GOOD": 12.0, "NOMARGIN": 11.0},
        "pb": {"GOOD": 1.5, "NOMARGIN": 1.2},
        "ps": {"GOOD": 2.0, "NOMARGIN": 1.8},
        "debt_equity": {"GOOD": 0.4, "NOMARGIN": 0.3},
        "net_margin": {"GOOD": 0.18, "NOMARGIN": None},
        "div_yield": {"GOOD": 0.02, "NOMARGIN": 0.02},
        "earnings_yield": {"GOOD": 0.08, "NOMARGIN": 0.09},
        "fcf_yield": {"GOOD": 0.05, "NOMARGIN": 0.06},
        "market_cap": {"GOOD": 1e12, "NOMARGIN": 9e11},
    })
    # 공유 screen 의 기본 동작(momentum/wood 경로)은 불변이어야 한다
    passed_default, fails_default = ff.screen(snap, min_net_margin=0.08, max_pe=25.0)
    check("기본 screen(require_fields 없음) → 종전대로 무데이터 통과",
          "NOMARGIN" in passed_default and not fails_default, (passed_default, fails_default))
    passed_req, fails_req = ff.screen(snap, min_net_margin=0.08, max_pe=25.0,
                                      require_fields=("net_margin",))
    check("require_fields=('net_margin',) → 탈락", "NOMARGIN" in fails_req, fails_req)
    check("탈락사유에 필드명", "net_margin" in fails_req.get("NOMARGIN", ""), fails_req)
    check("정상 종목은 그대로 통과", passed_req == ["GOOD"], passed_req)
    prices = _prices({"GOOD": lambda n: [100 + i * 0.3 for i in range(n)],
                      "NOMARGIN": lambda n: [100 + i * 0.2 for i in range(n)]})
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sel = lb.select(prices, lookback=20, top_n=2, pool=2, max_pe=25.0, min_margin=0.08)
    finally:
        ff.snapshot = orig
    check("buffett 선정에서 NOMARGIN 배제", "NOMARGIN" not in sel["final"], sel["final"])
    check("buffett fails 에 사유 기록", "NOMARGIN" in sel["fails"], sel["fails"])


def test_buffett_pct_change_fill_method():
    print("[A15②] buffett 변동성 — pct_change(fill_method=None), 결측 ffill 로 vol 과소평가 안 됨")
    prices = _prices({
        "GAPPY": lambda n: [100.0] * n,      # 아래에서 결측 주입 — ffill 되면 vol 0 으로 위장
        "STEADY": lambda n: [100 + (i % 2) * 0.4 for i in range(n)],
        "WILD": lambda n: [100 + (i % 2) * 8.0 for i in range(n)],
    })
    prices["GAPPY"] = [100.0 + (i % 2) * 6.0 for i in range(len(prices))]
    prices.iloc[5:25, prices.columns.get_loc("GAPPY")] = float("nan")   # 거래정지 구간
    captured = {}
    orig = ff.snapshot

    def fake(tickers, fmp=None):
        captured["pool"] = list(tickers)
        return pd.DataFrame()

    ff.snapshot = fake
    try:
        lb.select(prices, lookback=30, top_n=1, pool=1)
    finally:
        ff.snapshot = orig
    check("결측구간 종목이 '저변동' 1순위로 위장 안 됨(ffill 0% 수익률 제거)",
          captured.get("pool") != ["GAPPY"], captured)
    # 직접 대조 — pandas 2.x 기본값(ffill 후 계산)과 명시 None 의 변동성 차이.
    # pandas 3 은 fill_method 인자 자체를 제거(항상 None)했으므로 ffill 을 손으로 재현해 버전 무관 비교.
    a = prices["GAPPY"].pct_change(fill_method=None).tail(30).std()
    b = prices["GAPPY"].ffill().pct_change(fill_method=None).tail(30).std()
    check("명시 None 이 ffill 대비 큰 변동성(과소평가 위장 해소 방향)", a > b, (a, b))
    check("buffett 이 fill_method=None 을 명시(pandas 2.x 기본값 회귀 차단)",
          "fill_method=None" in __import__("inspect").getsource(lb.select), "미명시")


def test_fmp_stale_cache_age_cap():
    print("[A5] 만료캐시 나이 상한 — 90일 초과는 결측 처리(A4 경로 합류), env 로 조정")
    import json as _json
    import time as _time
    import fmp_client as fc
    saved = {k: os.environ.get(k) for k in ("FMP_API_KEY", "FMP_STALE_MAX_DAYS")}
    d = tempfile.mkdtemp()
    orig_dir, orig_hits, orig_rej = fc.CACHE_DIR, fc.STALE_HITS, fc.STALE_REJECTS
    try:
        os.environ["FMP_API_KEY"] = "test-dummy-key"
        os.environ.pop("FMP_STALE_MAX_DAYS", None)
        fc.CACHE_DIR = __import__("pathlib").Path(d)

        def _mk(age_days):
            f = fc.CACHE_DIR / "ratios-ttm_x.json"
            f.write_text(_json.dumps([{"priceToEarningsRatioTTM": 10.0}]), encoding="utf-8")
            os.utime(f, (_time.time() - age_days * 86400,) * 2)
            return f

        cli = fc.FMP()
        check("기본 상한 90일", cli.stale_max_days == 90.0, cli.stale_max_days)
        cli._fetch = lambda e, p: (_ for _ in ()).throw(fc.RateLimited("쿼터"))

        # 30일 캐시 → 종전대로 폴백 사용
        f = _mk(30)
        import hashlib as _h
        ck = _h.md5("ratios-ttm|[('symbol', 'AAA')]".encode()).hexdigest()
        f.rename(fc.CACHE_DIR / f"ratios-ttm_{ck}.json")
        os.utime(fc.CACHE_DIR / f"ratios-ttm_{ck}.json", (_time.time() - 30 * 86400,) * 2)
        fc.STALE_HITS, fc.STALE_REJECTS = 0, 0
        out = cli.get("ratios-ttm", symbol="AAA")
        check("30일 캐시 → 폴백 사용(기존 동작)", out and fc.STALE_HITS == 1, (out, fc.STALE_HITS))

        # 200일 캐시 → 상한 초과 → RateLimited 재발생(= 호출측 missing → A4)
        os.utime(fc.CACHE_DIR / f"ratios-ttm_{ck}.json", (_time.time() - 200 * 86400,) * 2)
        fc.STALE_HITS, fc.STALE_REJECTS = 0, 0
        threw = False
        try:
            cli.get("ratios-ttm", symbol="AAA")
        except fc.RateLimited:
            threw = True
        check("200일 캐시 → 결측 처리(raise)", threw)
        check("STALE_REJECTS 집계", fc.STALE_REJECTS == 1, fc.STALE_REJECTS)
        check("stale 폴백 미집계(사용 안 했으므로)", fc.STALE_HITS == 0, fc.STALE_HITS)

        # env 무상한(0) → 종전 동작 복원(§B 안전밸브)
        os.environ["FMP_STALE_MAX_DAYS"] = "0"
        cli2 = fc.FMP()
        cli2._fetch = lambda e, p: (_ for _ in ()).throw(fc.RateLimited("쿼터"))
        fc.STALE_HITS, fc.STALE_REJECTS = 0, 0
        out2 = cli2.get("ratios-ttm", symbol="AAA")
        check("FMP_STALE_MAX_DAYS=0 → 무상한(200일도 폴백)", bool(out2) and fc.STALE_HITS == 1,
              (out2, fc.STALE_HITS))
        os.environ["FMP_STALE_MAX_DAYS"] = "7"
        check("env 로 조이기(7일)", fc.FMP().stale_max_days == 7.0)
    finally:
        fc.CACHE_DIR, fc.STALE_HITS, fc.STALE_REJECTS = orig_dir, orig_hits, orig_rej
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def _wood_snap(ps_map, div_map, tk):
    return pd.DataFrame({
        "pe": {t: 50.0 for t in tk}, "pb": {t: 8.0 for t in tk}, "ps": ps_map,
        "debt_equity": {t: 0.3 for t in tk}, "net_margin": {t: 0.05 for t in tk},
        "div_yield": div_map, "earnings_yield": {t: 0.01 for t in tk},
        "fcf_yield": {t: 0.01 for t in tk}, "market_cap": {t: 1e11 for t in tk},
    })


def test_wood_ps_outlier_does_not_govern():
    print("[A6] wood P/S 이상치 — 한 종목의 극단값이 나머지 종목의 상대점수를 못 흔든다(윈저화)")
    n = 8
    tk = [f"T{i}" for i in range(n)]
    prices = _prices({t: (lambda m, k=i: [100 * ((1.03 - k * 0.002) ** j) for j in range(m)])
                      for i, t in enumerate(tk)}, periods=30)
    # T0 = 최고 P/S. 배당은 전부 게이트 미만(DIV_GATE)이라 이 테스트는 윈저화만 격리한다.
    base_ps = {"T0": 35.0, "T1": 30.0, "T2": 25.0, "T3": 20.0,
               "T4": 15.0, "T5": 10.0, "T6": 8.0, "T7": 6.0}
    divs = {"T0": 0.0, "T1": 0.003, "T2": 0.0, "T3": 0.002,
            "T4": 0.0, "T5": 0.001, "T6": 0.0, "T7": 0.0}
    orig = ff.snapshot
    out = {}
    for tag, ps in (("base", base_ps), ("spiked", {**base_ps, "T0": 5000.0})):   # 순위 동일·크기만 극단
        snap = _wood_snap(ps, divs, tk)
        ff.snapshot = lambda tickers, fmp=None, _s=snap: _s.loc[[t for t in tickers if t in _s.index]]
        try:
            out[tag] = lw.select(prices, lookback=10, top_n=8, pool=n)[1]
        finally:
            ff.snapshot = orig
    b, s = out["base"]["scores"], out["spiked"]["scores"]
    rest = [t for t in tk if t != "T0"]
    check("이상치 유무와 무관하게 나머지 7종 점수 동일(sd 팽창 차단)",
          all(abs(b[t] - s[t]) < 0.01 for t in rest), {t: (b[t], s[t]) for t in rest})
    check("이상치 유무와 무관하게 나머지 7종 서열 동일",
          [t for t in out["base"]["final"] if t != "T0"] ==
          [t for t in out["spiked"]["final"] if t != "T0"],
          (out["base"]["final"], out["spiked"]["final"]))
    check("최고 P/S 종목은 여전히 1위(신호 보존 — 최악값 처리 아님)",
          out["spiked"]["final"][0] == "T0", out["spiked"]["final"])
    raw = pd.Series({**base_ps, "T0": 5000.0})
    z_raw = ff._z(raw).drop("T0")
    z_win = ff._z(lw._winsor_top(raw)).drop("T0")
    check("대조 — 무처리면 나머지 z 폭 붕괴, 윈저화가 복원",
          (z_win.max() - z_win.min()) > 8 * (z_raw.max() - z_raw.min()),
          (float(z_raw.max() - z_raw.min()), float(z_win.max() - z_win.min())))
    check("단일값·전결측 입력에서도 안 죽음",
          len(lw._winsor_top(pd.Series([3.0]))) == 1 and lw._winsor_top(pd.Series(dtype=float)).empty)
    try:   # FMP 스키마 드리프트(object dtype) — nlargest 가 TypeError 로 죽던 경로
        od = lw._winsor_top(pd.Series({"A": 10.0, "B": "N/A", "C": 20.0}))
        ok = float(od["C"]) == 10.0 and pd.isna(od["B"])   # 2위값(10)으로 윈저, 문자열은 결측
    except Exception as e:
        ok = f"{type(e).__name__}: {e}"
    check("object dtype 스냅도 안 죽음(문자열=결측)", ok is True, ok)


def test_wood_momentum_not_double_counted():
    print("[A7] wood 모멘텀 이중계상 해소 — pool 게이트로만 쓰고 점수식엔 z(mom) 없음")
    n = 8
    tk = [f"T{i}" for i in range(n)]
    # 모멘텀 T0>T1>...>T7, P/S 는 정확히 역순 → z(mom) 이 점수에 남아있으면 T0 이 상위에 낀다.
    prices = _prices({t: (lambda m, k=i: [100 * ((1.05 - k * 0.006) ** j) for j in range(m)])
                      for i, t in enumerate(tk)}, periods=30)
    ps = {t: 5.0 + i * 4.0 for i, t in enumerate(tk)}          # T7 최고 P/S
    snap = _wood_snap(ps, {t: 0.0 for t in tk}, tk)            # 배당 동률 → 점수 = z(ps) 단독
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sel = lw.select(prices, lookback=10, top_n=3, pool=n)
    finally:
        ff.snapshot = orig
    check("pool 은 모멘텀 게이트 유지(전략 정체성)", len(sel["candidates"]) == n, sel["candidates"])
    check("pool 은 모멘텀 순(momentum_only 선두=T0)", sel["momentum_only"][0] == "T0", sel["momentum_only"])
    # 등비중이라 중요한 건 순서가 아니라 멤버십(집합). 윈저화가 1~2위를 동점으로 묶는 건 설계대로다.
    check("선정 = P/S 상위 3종 {T7,T6,T5} — 모멘텀 재가산 없음",
          set(sel["final"]) == {"T7", "T6", "T5"}, sel["final"])
    check("모멘텀 1위 T0 미편입(이중계상 해소)", "T0" not in sel["final"], sel["final"])
    check("점수 단조 비증가(랭킹 일관)",
          all(sel["scores"][a] >= sel["scores"][b]
              for a, b in zip(sel["final"], sel["final"][1:])), sel["scores"])
    check("degrade 사유 없음(전원 검증·채점)", sel["degraded_reasons"] == [], sel["degraded_reasons"])
    # 윈저화가 만드는 동점은 최상위 한 쌍뿐 → top_n 경계(3/4위)를 못 흔든다 = 멤버십 영향 0.
    # (고정 clip@N 은 상위 3~6종을 한꺼번에 묶어 경계 자체를 무너뜨린다 — 이 안을 고른 이유)
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _wa, full = lw.select(prices, lookback=10, top_n=n, pool=n)
    finally:
        ff.snapshot = orig
    rank = [full["scores"][t] for t in full["final"]]
    check("3/4위(선정 경계) 동점 아님 — 윈저화 동점은 1~2위에 갇힘", rank[2] > rank[3], rank)


def test_wood_dividend_gate():
    print("[DIV-GATE] wood 배당 — z 연속 페널티 폐기, 임계 초과만 pool 배제(2026-08-01 결정)")
    n = 6
    tk = [f"T{i}" for i in range(n)]
    prices = _prices({t: (lambda m, k=i: [100 * ((1.03 - k * 0.002) ** j) for j in range(m)])
                      for i, t in enumerate(tk)}, periods=30)
    # P/S 최고 2종에 고배당을 붙인다 — 게이트가 없으면 이 둘이 선정 1·2위.
    ps = {"T0": 40.0, "T1": 35.0, "T2": 30.0, "T3": 25.0, "T4": 20.0, "T5": 15.0}
    divs = {"T0": 0.025,    # 2.5% 인컴주(QCOM류) → 배제
            "T1": 0.016,    # 1.6% → 배제(임계 1.5% 초과)
            "T2": 0.015,    # 정확히 임계 → 통과(초과만 배제)
            "T3": 0.0013,   # 0.13% 토큰 배당(MRVL류) → 통과 = 이번 변경의 핵심
            "T4": 0.0,      # 무배당 → 통과
            "T5": None}     # 결측 → 통과(데이터갭으로 안 죽임)
    snap = _wood_snap(ps, divs, tk)
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sel = lw.select(prices, lookback=10, top_n=3, pool=n)
        _w2, all_sel = lw.select(prices, lookback=10, top_n=n, pool=n)
    finally:
        ff.snapshot = orig
    check("임계 초과 2종(T0 2.5%·T1 1.6%) pool 배제", sel["div_gated"] == ["T0", "T1"], sel["div_gated"])
    check("배제분은 선정에서도 빠짐", not ({"T0", "T1"} & set(sel["final"])), sel["final"])
    check("경계값(정확히 1.5%)은 통과 — 초과만 배제", "T2" in sel["final"], sel["final"])
    check("토큰 배당 0.13% 통과(종전 z 페널티가 밀어내던 케이스)", "T3" in sel["final"], sel["final"])
    check("무배당·결측 통과", {"T4", "T5"} <= set(all_sel["final"]), all_sel["final"])
    check("결측은 배제 아님(데이터갭 정책)", "T5" not in sel["div_gated"], sel["div_gated"])
    # 배당이 점수에 더는 안 들어간다 — 통과분 순위는 P/S 서열과 정확히 일치해야 한다.
    check("통과분 순위 = P/S 순(배당 연속항 제거 확인)",
          all_sel["final"] == ["T2", "T3", "T4", "T5"], all_sel["final"])
    check("배제분은 missing/unscored 로 오분류 안 됨(고의적 제외)",
          not ({"T0", "T1"} & set(sel["missing"])) and sel["degraded_reasons"] == [], sel)
    # 게이트가 pool 을 비워도 죽지 않고 '보류'(빈 weights)로 나간다 — live_engine STRAT-3 가 받는다.
    ff.snapshot = lambda tickers, fmp=None: _wood_snap(
        ps, {t: 0.05 for t in tk}, tk).loc[[t for t in tickers if t in tk]]
    try:
        w3, sel3 = lw.select(prices, lookback=10, top_n=3, pool=n)
    finally:
        ff.snapshot = orig
    check("전원 배제 → throw 없이 빈 선택(엔진이 skip=포지션 유지로 처리)",
          w3 == {} and sel3["final"] == [] and len(sel3["div_gated"]) == n, (w3, sel3["div_gated"]))
    # 구캐시(div_yield 컬럼 자체 부재) → 게이트 무동작
    nodiv = _wood_snap(ps, {t: 0.0 for t in tk}, tk).drop(columns=["div_yield"])
    ff.snapshot = lambda tickers, fmp=None: nodiv.loc[[t for t in tickers if t in nodiv.index]]
    try:
        _w4, sel4 = lw.select(prices, lookback=10, top_n=3, pool=n)
    finally:
        ff.snapshot = orig
    check("div_yield 컬럼 없는 구캐시 → KeyError 없이 전원 통과",
          sel4["div_gated"] == [] and len(sel4["final"]) == 3, sel4["final"])


def test_buffett_value_trap_veto():
    print("[BUFFETT] Piotroski value trap veto — trap 탈락·백필 + A엔진 부재 no-op·표면화")
    prices = _prices({
        "AAA": lambda n: [100 + i * 0.5 for i in range(n)],
        "BBB": lambda n: [100 + i * 0.4 for i in range(n)],
        "CCC": lambda n: [100 + i * 0.3 for i in range(n)],
        "DDD": lambda n: [100 + i * 0.2 for i in range(n)],
    })
    # 전 종목 스크린 통과 + qv 랭킹 AAA>BBB>CCC>DDD (팩터 단조)
    snap = pd.DataFrame({
        "pe": {"AAA": 10, "BBB": 12, "CCC": 14, "DDD": 16},
        "pb": {"AAA": 1.0, "BBB": 1.5, "CCC": 2.0, "DDD": 2.5},
        "ps": {"AAA": 2.0, "BBB": 2.5, "CCC": 3.0, "DDD": 3.5},
        "debt_equity": {"AAA": 0.2, "BBB": 0.4, "CCC": 0.6, "DDD": 0.8},
        "net_margin": {"AAA": 0.25, "BBB": 0.20, "CCC": 0.15, "DDD": 0.12},
        "div_yield": {"AAA": 0.02, "BBB": 0.02, "CCC": 0.01, "DDD": 0.01},
        "earnings_yield": {"AAA": 0.10, "BBB": 0.08, "CCC": 0.07, "DDD": 0.06},
        "fcf_yield": {"AAA": 0.08, "BBB": 0.06, "CCC": 0.05, "DDD": 0.04},
        "market_cap": {"AAA": 1e12, "BBB": 5e11, "CCC": 2e11, "DDD": 1e11},
    })
    fake_pio = {"AAA": {"score": 8, "tested": 8, "reliable": True},
                "BBB": {"score": 2, "tested": 8, "reliable": True},    # trap
                "CCC": {"score": 3, "tested": 4, "reliable": False},   # 저F지만 미신뢰 → 안 죽임
                "DDD": {"score": 7, "tested": 8, "reliable": True}}
    queried = []

    def _pio(t):
        queried.append(t)
        return fake_pio[t]

    _trap = lambda f: bool(f.get("reliable")) and (f.get("score") or 0) < 5   # canslim _is_value_trap 룰
    orig_snap, orig_load = ff.snapshot, lb._load_piotroski
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    lb._load_piotroski = lambda: (_pio, _trap)
    try:
        w, sel = lb.select(prices, lookback=20, top_n=2, pool=4, max_pe=25.0,
                           min_margin=0.08, value_trap_gate=True)
        check("trap BBB 탈락 → [AAA, CCC] 백필", sel["final"] == ["AAA", "CCC"], sel["final"])
        check("excluded_value_trap=[BBB]", sel["excluded_value_trap"] == ["BBB"], sel)
        check("미신뢰 CCC 안 죽임(데이터갭 정책)", "CCC" in sel["final"], sel["final"])
        check("lazy 중단 — DDD 미조회", "DDD" not in queried, queried)
        check("piotroski 점수 info 기록", sel["piotroski"].get("AAA") == 8
              and sel["piotroski"].get("BBB") == 2, sel["piotroski"])
        check("등비중 합 1.0", abs(sum(w.values()) - 1.0) < 1e-9, w)

        lb._load_piotroski = lambda: (None, None)            # A엔진 부재
        w2, sel2 = lb.select(prices, lookback=20, top_n=2, pool=4, max_pe=25.0,
                             min_margin=0.08, value_trap_gate=True)
        check("A 부재 → no-op(기존 랭킹 유지)", sel2["final"] == ["AAA", "BBB"], sel2["final"])
        check("A 부재 → veto_unavailable 표면화", sel2["veto_unavailable"] is True, sel2)

        lb._load_piotroski = orig_load
        w3, sel3 = lb.select(prices, lookback=20, top_n=2, pool=4, max_pe=25.0,
                             min_margin=0.08)                # 게이트 off(기본)
        check("게이트 off → veto 키 비활성", sel3["excluded_value_trap"] == []
              and sel3["veto_unavailable"] is False, sel3)
    finally:
        ff.snapshot, lb._load_piotroski = orig_snap, orig_load


def test_personas_presets():
    print("[PRESET] 페르소나 3종 프리셋 — 전략·자본·override")
    check("buffett 전략·유니버스", personas.get("buffett")["strategy"] == "buffett"
          and personas.get("buffett")["universe"] == "sp500")
    check("wood 전략", personas.get("wood")["strategy"] == "wood")
    check("oneil=canslim(실거래 전략)", personas.get("oneil")["strategy"] == "canslim")
    check("전부 $100000 + fractional", all(personas.get(n)["cash"] == 100000.0
          and personas.get(n)["overrides"]["fractional"] for n in ("buffett", "wood", "oneil")))
    check("buffett·oneil value_trap_gate on", all(
        personas.get(n)["overrides"].get("value_trap_gate") is True for n in ("buffett", "oneil")))
    threw = False
    try:
        personas.get("nobody")
    except KeyError:
        threw = True
    check("미지 페르소나 → KeyError", threw)


def test_expanded_universes():
    print("[UNIVERSE] sp500/growth 확장 유니버스 — 규모·형식(대문자·dash·무중복)")
    sp500 = universe.get_universe("sp500")
    growth = universe.get_universe("growth")
    check("sp500 ~450-505 종목", 450 <= len(sp500) <= 505, len(sp500))
    check("growth ~40-50 종목", 40 <= len(growth) <= 50, len(growth))
    check("sp500 전부 대문자·dash form(점 없음)",
          all(t == t.upper() and "." not in t for t in sp500))
    check("growth 전부 대문자·dash form(점 없음)",
          all(t == t.upper() and "." not in t for t in growth))
    check("sp500 무중복", len(sp500) == len(set(sp500)), len(sp500) - len(set(sp500)))
    check("growth 무중복", len(growth) == len(set(growth)), len(growth) - len(set(growth)))


def test_paper_book_persistence():
    print("[BOOK] PaperBroker 다일 진화 — 체결이 디스크 영속, 새 인스턴스가 직전 책 로드")
    d = tempfile.mkdtemp()
    sf = os.path.join(d, "paper_book.json")
    pb = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, commission=0.0, spread=0.0,
                     slippage=0.0, state_file=sf)
    pb.place_order(OrderRequest(symbol="AAA", side=Side.BUY, qty=3, order_type=OrderType.MARKET))
    cash_after = pb._cash
    # 새 프로세스 모사 — 시드 cash 9999 무시하고 저장된 책 로드
    pb2 = PaperBroker(cash=9999.0, price_fn=lambda s: 100.0, state_file=sf)
    check("현금 로드(시드 무시)", abs(pb2._cash - cash_after) < 1e-9, (pb2._cash, cash_after))
    check("포지션 로드 AAA=3", any(p.symbol == "AAA" and abs(p.qty - 3) < 1e-9 for p in pb2.get_positions()),
          pb2.get_positions())
    # 진화 — 1주 매도 후 또 새 인스턴스
    pb2.place_order(OrderRequest(symbol="AAA", side=Side.SELL, qty=1, order_type=OrderType.MARKET))
    pb3 = PaperBroker(cash=0.0, price_fn=lambda s: 100.0, state_file=sf)
    check("다일 진화 — AAA 잔량 2", any(p.symbol == "AAA" and abs(p.qty - 2) < 1e-9 for p in pb3.get_positions()),
          pb3.get_positions())
    # state_file 없으면 무영속(기존 동작 불변)
    pb0 = PaperBroker(cash=500.0, price_fn=lambda s: 100.0)
    check("state_file 없으면 영속 안 함(기존 동작)", pb0._state_file is None)


def test_dispatch_registration():
    print("[DISPATCH] live_engine._STRATEGIES 에 buffett·wood 등록 (FMP 모듈, A엔진 불요)")
    check("buffett 등록", live_engine._STRATEGIES.get("buffett") is not None)
    check("wood 등록", live_engine._STRATEGIES.get("wood") is not None)
    check("canslim 키 보존", "canslim" in live_engine._STRATEGIES)
    # 미등록이면 `_STRATEGIES.get(x) or select` 로 모멘텀에 조용히 폴백 — 로그만 buffett_v2 로 남는다.
    check("buffett_v2 등록(미등록 시 모멘텀 무성폴백)",
          live_engine._STRATEGIES.get("buffett_v2") is not None)


def test_qv_partial_missing():
    print("[QV-FIX] 한 하위팩터 결측이 value/quality 컴포넌트 전체 0 소거 안 함(부분결측 보존)")
    # quality 동률(0 기여) → 점수=value 만. CHEAP 은 저PE/PB 강점, earnings_yield 만 결측.
    snap = pd.DataFrame({
        "pe": {"CHEAP": 3.0, "MID": 15.0, "EXP": 40.0},
        "pb": {"CHEAP": 0.3, "MID": 2.0, "EXP": 5.0},
        "earnings_yield": {"CHEAP": None, "MID": 0.05, "EXP": 0.02},   # CHEAP 결측
        "net_margin": {"CHEAP": 0.10, "MID": 0.10, "EXP": 0.10},       # 동률 → quality 0
        "debt_equity": {"CHEAP": 1.0, "MID": 1.0, "EXP": 1.0},
    })
    qv = ff.quality_value_score(snap)
    check("저PE/PB CHEAP 1위 유지(컴포넌트 통째 0 소거 안 됨)", qv.index[0] == "CHEAP", list(qv.index))


def test_qv_negative_equity_not_top():
    print("[QV-A3] 자기자본 잠식(pb<0·D/E<0)이 value·quality 동시 1위로 위장하던 부호버그")
    # pe·net_margin 을 동률로 고정 → pb/D/E 부호역전만이 순위를 결정하는 대조군.
    # 수정 전: ZOMBIE 가 -pb·-D/E 양쪽 최대라 1위. 수정 후: 비양수는 측정불가(중립).
    snap = pd.DataFrame({
        "pe": {"ZOMBIE": 15.0, "SOLID": 15.0, "OK": 15.0},
        "pb": {"ZOMBIE": -0.5, "SOLID": 1.5, "OK": 2.5},            # 음수 자본 → -pb 최대(가짜 초저평가)
        "debt_equity": {"ZOMBIE": -3.0, "SOLID": 0.4, "OK": 0.9},   # 음수 D/E → -D/E 최대(가짜 무차입)
        "net_margin": {"ZOMBIE": 0.15, "SOLID": 0.15, "OK": 0.15},
        "earnings_yield": {"ZOMBIE": 0.066, "SOLID": 0.067, "OK": 0.066},
    })
    qv = ff.quality_value_score(snap)
    check("ZOMBIE 1위 아님(부호역전 보너스 제거)", qv.index[0] != "ZOMBIE", list(qv.index))
    check("정상 우량 SOLID 1위", qv.index[0] == "SOLID", list(qv.index))
    # 최악값이 아니라 '측정불가=중립' 처리인지 — pb/DE 를 결측으로 준 스냅과 점수가 같아야 한다
    # (자사주매입 누적으로 자본이 음수인 우량주를 최악 처리하면 그것도 오판이므로).
    neutral = snap.copy()
    neutral.loc["ZOMBIE", ["pb", "debt_equity"]] = None
    z_masked, z_missing = float(qv["ZOMBIE"]), float(ff.quality_value_score(neutral)["ZOMBIE"])
    check("비양수 pb/DE = 결측과 동일 점수(중립, 최악값 처리 아님)",
          abs(z_masked - z_missing) < 1e-9, (z_masked, z_missing))


def test_buffett_negative_equity_not_selected():
    print("[BUFFETT-A3] 자본잠식 종목이 하드스크린(PE·마진)을 통과해도 최상위 선정 안 됨")
    prices = _prices({
        "ZOMBIE": lambda n: [100 + i * 0.5 for i in range(n)],
        "SOLID": lambda n: [100 + i * 0.4 for i in range(n)],
        "OK": lambda n: [100 + i * 0.3 for i in range(n)],
    })
    snap = pd.DataFrame({   # 셋 다 PE 15·마진 15% → 스크린 전원 통과, 순위는 qv 가 결정
        "pe": {"ZOMBIE": 15.0, "SOLID": 15.0, "OK": 15.0},
        "pb": {"ZOMBIE": -0.5, "SOLID": 1.5, "OK": 2.5},
        "debt_equity": {"ZOMBIE": -3.0, "SOLID": 0.4, "OK": 0.9},
        "net_margin": {"ZOMBIE": 0.15, "SOLID": 0.15, "OK": 0.15},
        "earnings_yield": {"ZOMBIE": 0.066, "SOLID": 0.067, "OK": 0.066},
        "market_cap": {"ZOMBIE": 1e11, "SOLID": 2e11, "OK": 3e11},
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        _w, sel = lb.select(prices, lookback=20, top_n=1, pool=3, max_pe=25.0, min_margin=0.08)
    finally:
        ff.snapshot = orig
    check("스크린은 전원 통과(순위 단계 검증임을 보장)", not sel["fails"], sel["fails"])
    check("final 이 ZOMBIE 아님", sel["final"] != ["ZOMBIE"], sel["final"])
    check("final = [SOLID]", sel["final"] == ["SOLID"], sel["final"])


def test_qv_roe_term():
    print("[QV-A2] ROE 항 — 마진 동률이면 자본효율이 순위를 가름 + roe 부재 구캐시는 중립 통과")
    base = {
        "pe": {"HIROE": 20.0, "LOROE": 20.0},
        "pb": {"HIROE": 3.0, "LOROE": 3.0},
        "debt_equity": {"HIROE": 0.5, "LOROE": 0.5},
        "net_margin": {"HIROE": 0.12, "LOROE": 0.12},
        "earnings_yield": {"HIROE": 0.05, "LOROE": 0.05},
    }
    qv_no = ff.quality_value_score(pd.DataFrame(base))                     # 구 FMP 캐시 = roe 컬럼 자체가 없음
    qv_yes = ff.quality_value_score(pd.DataFrame({**base, "roe": {"HIROE": 0.35, "LOROE": 0.06}}))
    check("roe 컬럼 없는 스냅 → KeyError 없이 전원 동점(중립 통과)",
          abs(float(qv_no["HIROE"]) - float(qv_no["LOROE"])) < 1e-9, dict(qv_no))
    check("고ROE 1위(동률 마진을 자본효율이 가름)", qv_yes.index[0] == "HIROE", list(qv_yes.index))
    # 자본잠식 ROE 아티팩트(BA 실측: 순이익률 +2.5% 인데 ROE -87)가 z 분포를 뭉개지 않는지.
    # 무클립이면 HIROE-LOROE 격차가 0.006 로 붕괴, clip(-1,1) 이면 0.40 유지.
    outlier = pd.DataFrame({
        "pe": {"HIROE": 20.0, "LOROE": 20.0, "WRECK": 20.0},
        "pb": {"HIROE": 3.0, "LOROE": 3.0, "WRECK": 3.0},
        "debt_equity": {"HIROE": 0.5, "LOROE": 0.5, "WRECK": 0.5},
        "net_margin": {"HIROE": 0.12, "LOROE": 0.12, "WRECK": 0.025},
        "earnings_yield": {"HIROE": 0.05, "LOROE": 0.05, "WRECK": 0.05},
        "roe": {"HIROE": 0.35, "LOROE": 0.06, "WRECK": -87.231},
    })
    qv_out = ff.quality_value_score(outlier)
    check("ROE -87 아티팩트에도 우량 대역 해상도 유지(clip)",
          float(qv_out["HIROE"]) - float(qv_out["LOROE"]) > 0.3,
          float(qv_out["HIROE"]) - float(qv_out["LOROE"]))


def test_qv_object_dtype_survives():
    print("[QV-FIX3] FMP 스키마 드리프트로 문자열이 섞여 object dtype 이 돼도 .clip 이 안 죽음")
    snap = pd.DataFrame({   # pe 에 문자열 한 칸 → 컬럼 통째 object → 기존 .clip 은 TypeError
        "pe": {"A": 10.0, "B": "N/A", "C": 20.0},
        "pb": {"A": 1.0, "B": 2.0, "C": 3.0},
        "debt_equity": {"A": 0.3, "B": 0.5, "C": 0.8},
        "net_margin": {"A": 0.20, "B": 0.15, "C": 0.10},
        "earnings_yield": {"A": 0.10, "B": 0.06, "C": 0.04},
    })
    try:
        qv = ff.quality_value_score(snap)
        ok, detail = len(qv) == 3 and qv.index[0] == "A", list(qv.index)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    check("object dtype 스냅도 정상 채점(문자열은 결측 취급)", ok, detail)


# ───────────────────────── buffett_v2 (A/B 실험군) ─────────────────────────

def test_v2_sector_shrinkage():
    print("[V2-섹터] 섹터 틸트 제거 + 표본수축 — n=1 은 전역 z 유지, n>=4 는 완전 중립, 수준 보존")
    # 근거(구조적): pool 20종에 GICS 11섹터 → 섹터당 1~4종이 불가피. 순수 섹터내 z 면 n=1 은 0,
    # n=2 는 ±0.707 고정이라 pool 절반이 랭킹에서 증발한다. 수축은 표본이 없을 때 섹터효과를
    # '추정하지 않는' 쪽으로 후퇴한다. (실제 섹터 분포는 profile 캐시가 서야 실측 가능 — 미측정)
    s = pd.Series({"A1": 10.0, "A2": 11.0, "A3": 12.0, "A4": 13.0, "B1": 100.0})
    sec = pd.Series({"A1": "A", "A2": "A", "A3": "A", "A4": "A", "B1": "B"})
    z = ff._z_sector(s, sec)
    check("n=1 섹터(B1)가 최상위 유지 — 섹터내 z 였다면 0 으로 소거됐을 값",
          z.idxmax() == "B1", dict(z.round(3)))
    check("n=4 섹터 내부 순서 보존(A1<A2<A3<A4)",
          z["A1"] < z["A2"] < z["A3"] < z["A4"], dict(z.round(3)))
    # 수축 계수 검산 — 빼는 값 = w × (섹터평균 − 전체평균), w=(n-1)/(min_n-1).
    #   전체평균 5.0 / C(n=2): w=1/3, 편차 +10 → tilt +10/3 / D(n=4): w=1, 편차 −5 → tilt −5
    s2 = pd.Series({"C1": 10.0, "C2": 20.0, "D1": 0.0, "D2": 0.0, "D3": 0.0, "D4": 0.0})
    sec2 = pd.Series({"C1": "C", "C2": "C", "D1": "D", "D2": "D", "D3": "D", "D4": "D"})
    expect = ff._z(pd.Series({"C1": 10.0 - 10.0 / 3, "C2": 20.0 - 10.0 / 3,
                              "D1": 5.0, "D2": 5.0, "D3": 5.0, "D4": 5.0}))
    got = ff._z_sector(s2, sec2)
    check("수축 계수 정확(n=2 는 1/3, n=4 는 전량 — 전체평균 대비 편차 기준)",
          bool(((got - expect).abs() < 1e-9).all()), dict((got - expect).round(6)))
    # ⚠️ 회귀 — 섹터평균을 통째로 빼면 w=1 그룹만 수준이 0 으로 붕괴해 싱글턴과 척도가 어긋난다.
    # 그 형태에서는 FCF yield 2% 싱글턴이 9% 우량주를 이겼다(z 1.117 vs 0.768). 편차만 빼면 소멸.
    fcf = pd.Series({"STP1": 0.06, "STP2": 0.07, "STP3": 0.08, "STP4": 0.09, "UTL1": 0.02})
    fsec = pd.Series({"STP1": "Staples", "STP2": "Staples", "STP3": "Staples",
                      "STP4": "Staples", "UTL1": "Utilities"})
    zf = ff._z_sector(fcf, fsec)
    check("풀 최저 팩터값 싱글턴은 최하위 — 수준 붕괴로 인한 순위역전 없음",
          zf.idxmin() == "UTL1" and zf.idxmax() == "STP4", dict(zf.round(3)))
    # 섹터 틸트는 실제로 제거돼야 한다 — 고팩터 섹터와 저팩터 섹터(각 n=4)의 z 평균이 같아짐.
    s3 = pd.Series({"H1": .30, "H2": .32, "H3": .34, "H4": .36,
                    "L1": .04, "L2": .06, "L3": .08, "L4": .10})
    sec3 = pd.Series({**{f"H{i}": "Hi" for i in range(1, 5)},
                      **{f"L{i}": "Lo" for i in range(1, 5)}})
    z3 = ff._z_sector(s3, sec3)
    check("n>=4 섹터간 평균차 완전 제거(중립) + 섹터내 순서 보존",
          abs(z3[:4].mean() - z3[4:].mean()) < 1e-12
          and bool((z3[:4].diff().dropna() > 0).all() and (z3[4:].diff().dropna() > 0).all()),
          dict(z3.round(3)))
    # 섹터 정보가 없으면 전역 z 와 완전 동일해야 한다(프로필 조회 실패 시 안전 폴백).
    check("sector=None → 전역 z 와 동일(폴백)",
          bool(((ff._z_sector(s, None) - ff._z(s)).abs() < 1e-12).all()))
    # 단일 섹터(전원 동일)도 전역 z 와 동일 — 뺄 틸트가 없음(편차 0).
    check("전원 동일 섹터 → 전역 z 와 동일(뺄 틸트 없음)",
          bool(((ff._z_sector(s, pd.Series({t: "X" for t in s.index})) - ff._z(s)).abs()
                < 1e-9).all()))
    # 일부만 섹터 결측 — 결측 종목은 보정 없이 통과(그룹에서 빠짐), 예외 없이 동작.
    sec_partial = pd.Series({"A1": "A", "A2": "A", "A3": "A", "A4": "A", "B1": None})
    check("섹터 부분결측도 crash 없이 처리", len(ff._z_sector(s, sec_partial)) == 5)


def test_v2_soft_penalty():
    print("[V2-페널티] PE·마진 하드컷이 연속 감점으로 — 위반 비례, 최대 1z 유계(탈락 아님)")
    # 다른 팩터를 전부 동률로 → z 항 전원 0 → 점수 = -페널티만 남는 순수 대조군.
    base = dict(net_margin=0.20, roic=0.15, roe=0.20, fcf_yield=0.05, earnings_yield=0.05)
    snap = pd.DataFrame({k: {t: v for t in ("PE25", "PE37", "PE50", "PE200")}
                         for k, v in base.items()})
    snap["pe"] = pd.Series({"PE25": 25.0, "PE37": 37.5, "PE50": 50.0, "PE200": 200.0})
    qv = ff.quality_value_score_v2(snap)
    check("PE25 무감점(0.0)", abs(float(qv["PE25"]) - 0.0) < 1e-9, float(qv["PE25"]))
    check("PE37.5 절반감점(-0.5)", abs(float(qv["PE37"]) + 0.5) < 1e-9, float(qv["PE37"]))
    check("PE50 만점감점(-1.0)", abs(float(qv["PE50"]) + 1.0) < 1e-9, float(qv["PE50"]))
    check("PE200 도 -1.0 로 유계(극단값이 랭킹 지배 안 함)",
          abs(float(qv["PE200"]) + 1.0) < 1e-9, float(qv["PE200"]))
    # 마진 페널티 — 8% 기준 비례
    base2 = dict(pe=20.0, roic=0.15, roe=0.20, fcf_yield=0.05, earnings_yield=0.05)
    snap2 = pd.DataFrame({k: {t: v for t in ("M8", "M4", "M0")} for k, v in base2.items()})
    snap2["net_margin"] = pd.Series({"M8": 0.08, "M4": 0.04, "M0": 0.0})
    qv2 = ff.quality_value_score_v2(snap2)
    # net_margin 은 품질 z(0.5 가중)와 페널티 양쪽에 들어간다 — 합산 기대값을 분해해서 단언.
    #   M8 = 0.5·z(+1) − 0.0 = +0.5 / M4 = 0.5·z(0) − 0.5 = −0.5 / M0 = 0.5·z(−1) − 1.0 = −1.5
    check("마진 감점 8%→0 / 4%→-0.5 / 0%→-1.0 (품질 z 0.5 가중과 합산)",
          abs(float(qv2["M8"]) - 0.5) < 1e-9 and abs(float(qv2["M4"]) + 0.5) < 1e-9
          and abs(float(qv2["M0"]) + 1.5) < 1e-9, dict(qv2.round(3)))
    # 상수 컬럼(roic·roe·수익률)이 부동소수 std 1e-17 로 ±0.8 z 를 뿜지 않는지 — _z_tol 회귀.
    check("상수 컬럼은 0 기여(부동소수 std 노이즈 증폭 차단)",
          abs(float(ff._z_tol(pd.Series([0.2, 0.2, 0.2])).abs().max())) < 1e-12,
          list(ff._z_tol(pd.Series([0.2, 0.2, 0.2]))))


def test_v2_roic_axis():
    print("[V2-품질] 레버리지로 부푼 고ROE 보다 고ROIC 우선(ROIC 1.0 / ROE 0.5 가중)")
    snap = pd.DataFrame({
        "pe": {"LEVERED": 20.0, "COMPOUNDER": 20.0},
        "net_margin": {"LEVERED": 0.10, "COMPOUNDER": 0.10},
        "roic": {"LEVERED": 0.04, "COMPOUNDER": 0.25},     # 실제 자본효율은 COMPOUNDER 압승
        "roe": {"LEVERED": 0.30, "COMPOUNDER": 0.28},      # ROE 만 보면 LEVERED 가 이김(차입 효과)
        "fcf_yield": {"LEVERED": 0.05, "COMPOUNDER": 0.05},
        "earnings_yield": {"LEVERED": 0.05, "COMPOUNDER": 0.05},
    })
    qv = ff.quality_value_score_v2(snap)
    check("COMPOUNDER 1위(ROE 만 봤다면 LEVERED 가 이겼을 구성)",
          qv.index[0] == "COMPOUNDER", dict(qv.round(3)))


def test_v2_select_cut_boundary():
    print("[V2-컷] 하드컷은 적자·PE>60 만 — v1 이 탈락시키던 고PE(25<pe<=60)·저마진은 랭킹으로 통과")
    prices = _prices({t: (lambda n, k=k: [100 + i * k for i in range(n)])
                      for t, k in (("OK", 0.5), ("HIPE", 0.4), ("LOWMARGIN", 0.3),
                                   ("LOSS", 0.2), ("EXTREME", 0.1))})
    snap = pd.DataFrame({
        "pe": {"OK": 20.0, "HIPE": 45.0, "LOWMARGIN": 18.0, "LOSS": 30.0, "EXTREME": 61.0},
        "net_margin": {"OK": 0.20, "HIPE": 0.15, "LOWMARGIN": 0.03, "LOSS": -0.01, "EXTREME": 0.10},
        "roic": {"OK": 0.20, "HIPE": 0.18, "LOWMARGIN": 0.16, "LOSS": 0.05, "EXTREME": 0.15},
        "roe": {"OK": 0.25, "HIPE": 0.22, "LOWMARGIN": 0.20, "LOSS": 0.05, "EXTREME": 0.18},
        "fcf_yield": {"OK": 0.06, "HIPE": 0.05, "LOWMARGIN": 0.04, "LOSS": 0.01, "EXTREME": 0.03},
        "earnings_yield": {"OK": 0.05, "HIPE": 0.04, "LOWMARGIN": 0.03, "LOSS": 0.01, "EXTREME": 0.02},
        "market_cap": {"OK": 3e11, "HIPE": 2e11, "LOWMARGIN": 1e11, "LOSS": 9e10, "EXTREME": 8e10},
    })
    orig_snap, orig_sec = ff.snapshot, ff.sectors
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    ff.sectors = lambda tickers, fmp=None: pd.Series({t: "Industrials" for t in tickers})
    try:
        _w, sel = lb2.select(prices, lookback=20, top_n=5, pool=5)
    finally:
        ff.snapshot, ff.sectors = orig_snap, orig_sec
    check("적자 LOSS 탈락", "LOSS" in sel["fails"], sel["fails"])
    check("극단 PE(61) EXTREME 탈락", "EXTREME" in sel["fails"], sel["fails"])
    check("고PE(45) HIPE 는 통과 — v1 이면 PE>25 로 탈락했을 종목",
          "HIPE" not in sel["fails"] and "HIPE" in sel["final"], sel["final"])
    check("저마진(3%) LOWMARGIN 통과 — v1 이면 마진<8% 로 탈락",
          "LOWMARGIN" not in sel["fails"] and "LOWMARGIN" in sel["final"], sel["final"])
    check("최우량 OK 1순위", sel["final"][0] == "OK", sel["final"])
    check("strategy 태그 buffett_v2", sel["strategy"] == "buffett_v2")


def test_v2_sector_absent_graceful():
    print("[V2-폴백] 섹터 전량 조회실패 → 전역 z 로 선정 계속 + degrade 사유 표면화")
    prices = _prices({"AAA": lambda n: [100 + i * 0.5 for i in range(n)],
                      "BBB": lambda n: [100 + i * 0.4 for i in range(n)]})
    snap = pd.DataFrame({
        "pe": {"AAA": 15.0, "BBB": 20.0}, "net_margin": {"AAA": 0.20, "BBB": 0.12},
        "roic": {"AAA": 0.25, "BBB": 0.10}, "roe": {"AAA": 0.30, "BBB": 0.15},
        "fcf_yield": {"AAA": 0.07, "BBB": 0.03}, "earnings_yield": {"AAA": 0.06, "BBB": 0.04},
        "market_cap": {"AAA": 2e11, "BBB": 1e11},
    })
    orig_snap, orig_sec = ff.snapshot, ff.sectors
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    ff.sectors = lambda tickers, fmp=None: pd.Series({t: None for t in tickers}, dtype=object)
    try:
        _w, sel = lb2.select(prices, lookback=20, top_n=2, pool=2)
    finally:
        ff.snapshot, ff.sectors = orig_snap, orig_sec
    check("섹터 없어도 선정 진행(AAA 1위)", sel["final"][0] == "AAA", sel["final"])
    check("섹터 전량결측 사유 표면화",
          any("섹터" in r for r in sel["degraded_reasons"]), sel["degraded_reasons"])


def test_v2_ab_isolation():
    print("[V2-격리] A/B 무결성 — buffett 과 buffett_v2 는 선정 다이얼(max_pe·min_margin) 외 전부 동일")
    v1, v2 = personas.PERSONAS["buffett"], personas.PERSONAS["buffett_v2"]
    check("universe 동일", v1["universe"] == v2["universe"], (v1["universe"], v2["universe"]))
    check("시드자본 동일", v1["cash"] == v2["cash"], (v1["cash"], v2["cash"]))
    check("v2 도 장중 없음(일1런 전용)", not v2.get("intraday") and not v1.get("intraday"))
    o1, o2 = v1["overrides"], v2["overrides"]
    diff = {k for k in set(o1) | set(o2) if o1.get(k) != o2.get(k)}
    # 이 단언이 깨지면 12주 실험이 무효다 — 한쪽 arm 만 튜닝된 것.
    check("차이는 max_pe·min_margin 뿐(그 외 다이얼 동일)",
          diff == {"max_pe", "min_margin"}, sorted(diff))
    check("v2 컷 = 적자·극단PE 만", o2["max_pe"] == 60.0 and o2["min_margin"] == 0.0,
          (o2["max_pe"], o2["min_margin"]))
    check("v1 컷은 불변(대조군 동결)", o1["max_pe"] == 25.0 and o1["min_margin"] == 0.08,
          (o1["max_pe"], o1["min_margin"]))


def test_v2_v1_untouched():
    print("[V2-동결] v2 추가가 v1 경로를 안 건드림 — v1 은 섹터(profile) 콜을 내지 않는다")
    prices = _prices({"AAA": lambda n: [100 + i * 0.5 for i in range(n)],
                      "BBB": lambda n: [100 + i * 0.4 for i in range(n)]})
    snap = pd.DataFrame({
        "pe": {"AAA": 12.0, "BBB": 15.0}, "pb": {"AAA": 1.5, "BBB": 2.0},
        "ps": {"AAA": 2.0, "BBB": 2.5}, "debt_equity": {"AAA": 0.3, "BBB": 0.5},
        "net_margin": {"AAA": 0.20, "BBB": 0.15}, "div_yield": {"AAA": 0.02, "BBB": 0.01},
        "earnings_yield": {"AAA": 0.08, "BBB": 0.06}, "fcf_yield": {"AAA": 0.06, "BBB": 0.04},
        "market_cap": {"AAA": 2e11, "BBB": 1e11},
    })
    calls = []
    orig_snap, orig_sec = ff.snapshot, ff.sectors
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]

    def spy(tickers, fmp=None):
        calls.append(list(tickers))
        return pd.Series({t: "Industrials" for t in tickers})

    ff.sectors = spy
    try:
        _w, sel1 = lb.select(prices, lookback=20, top_n=2, pool=2)      # v1
        check("v1 은 sectors() 미호출(FMP 콜 수 불변)", calls == [], calls)
        _w2, sel2 = lb2.select(prices, lookback=20, top_n=2, pool=2)    # v2
        check("v2 는 sectors() 호출(스크린 통과분만)", len(calls) == 1, calls)
    finally:
        ff.snapshot, ff.sectors = orig_snap, orig_sec
    check("v1 태그 유지", sel1["strategy"] == "buffett", sel1["strategy"])
    check("v2 태그", sel2["strategy"] == "buffett_v2", sel2["strategy"])


def test_v2_operational_wiring():
    print("[V2-배선] 태스크가 실제로 돌 수 있는 경로 — argparse choices · 리뷰 연속z 분류")
    # --persona choices 가 하드코딩이면 신규 페르소나는 argparse 단계에서 거부돼 스케줄 태스크가
    # 매일 exit 2 로 죽는다 — 선정 코드가 아무리 맞아도 한 줄도 안 돌아간다.
    # 잘못된 값으로 호출해 '가능한 선택지' 목록을 stderr 에서 회수한다. argparse 가 먼저 죽으므로
    # 실제 리밸런스는 한 줄도 실행되지 않는다(파서 몽키패치보다 안전·결정적).
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        r = subprocess.run([sys.executable, "run_live.py", "--persona", "__nope__"],
                           capture_output=True, text=True, timeout=180, cwd=root)
        err, rc = (r.stderr or "") + (r.stdout or ""), r.returncode
    except Exception as e:
        err, rc = f"{type(e).__name__}: {e}", None
    check("잘못된 페르소나는 argparse 가 거부(실행 전 차단)", rc == 2, (rc, err[-200:]))
    check("--persona choices 에 buffett_v2 포함(태스크 기동 가능)", "buffett_v2" in err, err[-300:])
    # choices = PERSONAS ∩ 일1런 전략 등록표. 하드코딩이면 신규 페르소나가 매일 exit 2 로 죽고,
    # 반대로 PERSONAS 전량을 열면 장중전용(strategy 미등록)이 통과해 live_engine 의
    # `_STRATEGIES.get(s) or select` 로 **모멘텀에 무성 폴백** → 장중 공유책에 모멘텀 선정분이 체결된다.
    daily = {n for n, p in personas.PERSONAS.items() if p["strategy"] in live_engine._STRATEGIES}
    intraday_only = set(personas.PERSONAS) - daily
    check("choices 가 레지스트리 파생(일1런 엔진 있는 페르소나 전원 노출)",
          all(n in err for n in daily), (sorted(daily), err[-300:]))
    check("장중전용 페르소나는 choices 에서 제외(모멘텀 무성폴백 차단)",
          intraday_only and not any(n in err for n in intraday_only),
          (sorted(intraday_only), err[-300:]))
    # selection_review 연속z 분류에서 v2 가 빠지면 점수가 우연히 정수인 날만 canslim 정수버킷과
    # 섞여 A/B 비교표가 조용히 오염된다(= 12주 판정 근거가 훼손).
    import selection_review as sr
    lab_v1 = sr._score_bucket({"persona": "buffett", "score": 1.0})
    lab_v2 = sr._score_bucket({"persona": "buffett_v2", "score": 1.0})
    lab_int = sr._score_bucket({"persona": "oneil", "score": 1.0})
    check("v2 점수 라벨 = v1 과 동일 규칙(구간 라벨)", lab_v2 == lab_v1, (lab_v1, lab_v2))
    check("canslim(oneil)은 정수 라벨로 분리 유지", lab_int == "1" and lab_v2 != lab_int,
          (lab_int, lab_v2))


def test_buffett_marketcap_only_demoted():
    print("[BUFFETT-FIX] market_cap 만 있는 펀더 공란 행 → screen 무탈락 막고 missing 강등")
    prices = _prices({"GOOD": lambda n: [100 + i * 0.3 for i in range(n)],
                      "BLANK": lambda n: [100 + i * 0.2 for i in range(n)]})
    snap = pd.DataFrame({
        "pe": {"GOOD": 12.0, "BLANK": None}, "pb": {"GOOD": 1.5, "BLANK": None},
        "ps": {"GOOD": 2.0, "BLANK": None}, "debt_equity": {"GOOD": 0.4, "BLANK": None},
        "net_margin": {"GOOD": 0.18, "BLANK": None}, "div_yield": {"GOOD": 0.02, "BLANK": None},
        "earnings_yield": {"GOOD": 0.08, "BLANK": None}, "fcf_yield": {"GOOD": 0.05, "BLANK": None},
        "market_cap": {"GOOD": 1e12, "BLANK": 5e11},   # BLANK 은 market_cap 만
    })
    orig = ff.snapshot
    ff.snapshot = lambda tickers, fmp=None: snap.loc[[t for t in tickers if t in snap.index]]
    try:
        w, sel = lb.select(prices, lookback=20, top_n=1, pool=2)
    finally:
        ff.snapshot = orig
    check("BLANK(펀더 미검증)은 missing 으로 강등(검증완료 둔갑 안 함)", "BLANK" in sel["missing"], sel.get("missing"))
    check("final 은 검증된 GOOD", sel["final"] == ["GOOD"], sel["final"])


def test_buffett_trend_nan_kept():
    print("[BUFFETT-FIX] 신규상장(base 시점 NaN)으로 trend=NaN → cand_pool 에서 조용히 안 빠짐")
    prices = _prices({"OLD": lambda n: [100 + i * 0.2 for i in range(n)]})
    prices["NEW"] = [float("nan")] * 20 + [100.0 + i for i in range(20)]   # 앞 미상장
    captured = {}
    orig = ff.snapshot

    def fake(tickers, fmp=None):
        captured["pool"] = list(tickers)
        return pd.DataFrame()

    ff.snapshot = fake
    try:
        lb.select(prices, lookback=30, top_n=2, pool=5)
    finally:
        ff.snapshot = orig
    check("NEW(trend=NaN) cand_pool 포함(유효후보 silent 오탈락 방지)", "NEW" in captured.get("pool", []), captured)


def test_load_atomic_corrupt():
    print("[BOOK-FIX] 손상 책(항목 키 누락) 로드 → all-or-nothing 시드 유지(보유 증발 방지)")
    import json
    d = tempfile.mkdtemp()
    sf = os.path.join(d, "book.json")
    with open(sf, "w", encoding="utf-8") as f:
        json.dump({"cash": 250.0, "positions": [{"symbol": "AAA", "qty": 3}]}, f)   # avg_price 누락
    pb = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, state_file=sf)
    check("부분손상 → 시드 cash 유지(cash 만 250 으로 바뀌는 정합붕괴 안 함)", abs(pb._cash - 2000.0) < 1e-9, pb._cash)
    check("보유 비어도 throw 안 함", pb.get_positions() == [])


def test_paper_missing_price_fallback():
    print("[BOOK-FIX2] 영속 보유가 당일 패널서 빠져 price_fn KeyError → get_account/청산 평단 폴백(책 동결 방지)")
    snap = {"INPANEL": 100.0}   # ORPHAN 은 패널에 없음
    pb = PaperBroker(cash=500.0, price_fn=lambda s: snap[s], commission=0.0, spread=0.0, slippage=0.0)
    pb._positions["ORPHAN"] = Position("ORPHAN", 2.0, 50.0)   # 보유하나 당일 패널 결측(다운로드 실패 등)
    acct = pb.get_account()
    check("get_account 평단 폴백(KeyError crash 없음)", abs(acct.equity - (500.0 + 2.0 * 50.0)) < 1e-9, acct.equity)
    o = pb.place_order(OrderRequest(symbol="ORPHAN", side=Side.SELL, qty=2.0, order_type=OrderType.MARKET))
    check("ORPHAN 청산 평단 체결(책 동결 방지)", o.status == o.status.FILLED, o.status)
    check("청산 후 ORPHAN 제거(자가복구)", "ORPHAN" not in pb._positions)
    threw = False
    try:
        pb.place_order(OrderRequest(symbol="NOPE", side=Side.BUY, qty=1, order_type=OrderType.MARKET))
    except Exception:
        threw = True
    check("미보유 매수 시세없음 → raise(폴백 안 함)", threw)


def test_guarded_orphan_sell_fallback():
    print("[BOOK-FIX3] GuardedBroker(배포 경로) orphan SELL — 시세없어도 inner 위임(평단폴백 청산 도달, 책동결 방지)")
    from broker.guardrail import GuardedBroker

    class _FakeKS:   # place_order orphan-SELL 경로는 is_halted 만 호출(이후 get_quote 예외→위임)
        def is_halted(self):
            return (False, "")

        def exit_blocked(self):
            return (False, "")

        def check_order_notional(self, *a):
            pass

    snap = {"INPANEL": 100.0}
    pb = PaperBroker(cash=100.0, price_fn=lambda s: snap[s], commission=0.0, spread=0.0, slippage=0.0)
    pb._positions["ORPHAN"] = Position("ORPHAN", 2.0, 50.0)
    gb = GuardedBroker(pb, _FakeKS())
    o = gb.place_order(OrderRequest(symbol="ORPHAN", side=Side.SELL, qty=2.0, order_type=OrderType.MARKET))
    check("GuardedBroker orphan SELL 체결(get_quote KeyError→inner 위임→평단폴백)", o.status == o.status.FILLED, o.status)
    check("청산 후 ORPHAN 제거(배포경로 자가복구)", "ORPHAN" not in pb._positions)
    threw = False
    try:
        gb.place_order(OrderRequest(symbol="NOPE", side=Side.BUY, qty=1, order_type=OrderType.MARKET))
    except Exception:
        threw = True
    check("GuardedBroker orphan 매수 get_quote 실패 → raise(폴백 안 함)", threw)


def test_book_qty_2dp_policy():
    print("[2DP] PaperBroker 체결·책로드가 소수주 2자리 절사 강제(정책) — float 누적꼬리·기존책 정규화")
    import json

    def _2dp(q):
        return abs(q * 100 - round(q * 100)) < 1e-6

    px = {"AAA": 240.37}
    pb = PaperBroker(cash=100000.0, price_fn=lambda s: px[s], commission=0.0005)
    pb.place_order(OrderRequest("AAA", Side.BUY, 0.0, OrderType.MARKET, amount=20000.0))
    pb.place_order(OrderRequest("AAA", Side.BUY, 0.0, OrderType.MARKET, amount=10000.0))   # 추가매수 → new_qty float 누적꼬리
    pb.place_order(OrderRequest("AAA", Side.SELL, 12.34, OrderType.MARKET))                # 부분 트림
    check("매수·추가·트림 후 보유수량 2자리(float 꼬리 제거)",
          all(_2dp(p.qty) for p in pb.get_positions()),
          [(p.symbol, p.qty) for p in pb.get_positions()])
    # 기존 >2dp 책 로드 → 2자리 정규화 + <0.01 dust 개별 드롭(책 통째 거부 아님)
    d = tempfile.mkdtemp()
    sf = os.path.join(d, "legacy.json")
    with open(sf, "w", encoding="utf-8") as f:
        json.dump({"cash": 5000.0, "positions": [
            {"symbol": "AAA", "qty": 124.51999999999998, "avg_price": 240.37},
            {"symbol": "DUST", "qty": 0.004, "avg_price": 100.0}]}, f)
    pb2 = PaperBroker(cash=1.0, price_fn=lambda s: 100.0, state_file=sf)
    loaded = {p.symbol: p.qty for p in pb2.get_positions()}
    check("기존책 로드 시 보유수량 2자리 정규화", "AAA" in loaded and _2dp(loaded["AAA"]), loaded)
    check("0.01주 미만 dust 개별 드롭(cash 유지, 책 통째 거부 아님)",
          "DUST" not in loaded and abs(pb2._cash - 5000.0) < 1e-9, (loaded, pb2._cash))


def test_order_reason_recorded():
    print("[REASON] 일1런 주문에 매매 사유 기록 — Executor 사유 부착 + _dump_orders 저널 보존(빈칸 방지)")
    px = {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0}
    pb = PaperBroker(cash=100000.0, price_fn=lambda s: px[s], commission=0.0005)
    pb.place_order(OrderRequest("AAA", Side.BUY, 0.0, OrderType.MARKET, amount=30000.0))   # 보유 → 다음 리밸런스서 편출
    exe = Executor(pb, fractional=True, min_order_usd=5.0)
    reqs = exe.plan({"BBB": 0.5, "CCC": 0.3})   # AAA 미포함 → 편출, BBB/CCC 진입
    dumped = live_engine._dump_orders([Order(order_id="x", request=r) for r in reqs])
    check("모든 주문에 사유 기록(빈칸 없음)",
          bool(dumped) and all(d.get("reason") for d in dumped),
          [(d["side"], d["symbol"], d.get("reason")) for d in dumped])
    check("편출(청산) 사유 문구", any("편출" in d.get("reason", "") for d in dumped if d["side"] == "SELL"),
          [d.get("reason") for d in dumped])
    check("진입 사유에 목표비중 포함", any("목표" in d.get("reason", "") for d in dumped if d["side"] == "BUY"),
          [d.get("reason") for d in dumped])


def test_fmp_pacing_env_override():
    print("[FMP] 무료티어 페이싱 — FMP_MIN_INTERVAL/FMP_RETRY_402 env 오버라이드(paper 태스크 전용, 실거래 불변)")
    import fmp_client
    saved = {k: os.environ.get(k) for k in ("FMP_API_KEY", "FMP_MIN_INTERVAL", "FMP_RETRY_402")}
    try:
        os.environ["FMP_API_KEY"] = "test-dummy-key"      # load_key() 통과 — __init__ 만, 네트워크 없음
        os.environ.pop("FMP_MIN_INTERVAL", None)
        os.environ.pop("FMP_RETRY_402", None)
        f0 = fmp_client.FMP()
        check("env 미설정 → 기본 min_interval=0.5(실거래/백테스트 불변)", f0.min_interval == 0.5, f0.min_interval)
        check("env 미설정 → 기본 retry_402=3", f0.retry_402 == 3, f0.retry_402)
        os.environ["FMP_MIN_INTERVAL"] = "8"
        os.environ["FMP_RETRY_402"] = "1"
        f1 = fmp_client.FMP()
        check("FMP_MIN_INTERVAL=8 → 8.0(페르소나 페이싱)", f1.min_interval == 8.0, f1.min_interval)
        check("FMP_RETRY_402=1 → 1(재시도 증폭 차단)", f1.retry_402 == 1, f1.retry_402)
        os.environ["FMP_MIN_INTERVAL"] = ""               # 빈값은 기본 폴백(or 분기)
        f2 = fmp_client.FMP()
        check("빈 FMP_MIN_INTERVAL → 기본 0.5 폴백", f2.min_interval == 0.5, f2.min_interval)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)


def test_control_personas_and_dials():
    print("[CTL] 대조군 2종(참조공유 cfg·ADV16 watchlist) + 감사 다이얼(reselect·일중캡·thrust_k)")
    from intraday_rules import RULES
    for n in ("livermore_ctl", "chartist_ctl"):
        p = personas.get(n)
        base = personas.get(n.replace("_ctl", ""))
        check(f"{n} cfg 원본과 동일 객체(다이얼 자동 동기)", p["intraday_cfg"] is base["intraday_cfg"])
        check(f"{n} watchlist 16종·원본과 상이(비큐레이션)",
              len(p["watchlist"]) == 16 and p["watchlist"] != base["watchlist"])
        check(f"{n} 장중전용(intraday·비daily_run)", p.get("intraday") is True and not p.get("daily_run"))
        check(f"{n} cash 명시(무지정 $2000 함정 회피)", p["cash"] == 100000.0)
        check(f"{n} RULES 별칭 = 원본 룰 함수", RULES[n] is RULES[n.replace("_ctl", "")])
    check("buffett 주1회 재선정(reselect_days=7)",
          personas.get("buffett")["overrides"].get("reselect_days") == 7)
    check("wood 일중손실캡 5% 정렬", personas.get("wood")["intraday_cfg"]["intraday_max_loss"] == 0.05)
    check("장중 4종 thrust_k=0.75(적응형 임계 활성)",
          all(personas.get(n)["intraday_cfg"].get("thrust_k") == 0.75
              for n in ("wood", "oneil", "livermore", "chartist")))


def test_last_reselect_session():
    print("[HOLD-J] _last_reselect_session — 마지막 실선정(ok+final)만 앵커, hold/skip/타 페르소나 무시")
    import json as _json
    import pathlib
    import run_live
    d = pathlib.Path(tempfile.mkdtemp())
    recs = [
        {"ts": "t1", "session": "2026-07-01", "persona": "buffett", "status": "ok",
         "selection": {"final": ["AAA"]}},
        {"ts": "t2", "session": "2026-07-02", "persona": "buffett", "status": "hold"},
        {"ts": "t3", "session": "2026-07-03", "persona": "wood", "status": "ok",
         "selection": {"final": ["BBB"]}},
        {"ts": "t4", "session": "2026-07-04", "persona": "buffett", "status": "skip",
         "selection": {"final": []}},
        "not-json{{",
    ]
    (d / "runs.jsonl").write_text(
        "\n".join(r if isinstance(r, str) else _json.dumps(r) for r in recs), encoding="utf-8")
    saved = run_live.LOG_DIR
    run_live.LOG_DIR = d
    try:
        check("buffett 앵커=07-01 (hold·skip·wood·깨진줄 전부 무시)",
              run_live._last_reselect_session("buffett") == "2026-07-01",
              run_live._last_reselect_session("buffett"))
        check("기록 없는 페르소나 → None(재선정 진행)", run_live._last_reselect_session("oneil") is None)
    finally:
        run_live.LOG_DIR = saved


def test_reselect_hold_gate():
    print("[HOLD] 재선정 주기 게이트 — 비도래일 hold(무주문·당일완료 기록), 레짐 OFF 는 정상 경로 계속")
    from tests_stage1 import _use_temp_state, FakeBroker, _fake_select
    _use_temp_state()
    from live_engine import RunConfig, run_once
    orig_sel, orig_reg = live_engine.select, live_engine.regime_on
    live_engine.select = _fake_select({"AAA": 0.33, "BBB": 0.33, "CCC": 0.33})
    live_engine.regime_on = lambda **k: True
    try:
        cfg = RunConfig(vol_target=0.0, reselect_days=7)
        br = FakeBroker(fill=True)
        r = run_once(None, br, cfg, today="2026-06-01", reselect_due=False)
        check("hold 상태", r["status"] == "hold", r.get("status"))
        check("무주문(리밸런스 스킵)", not r.get("orders"), r.get("orders"))
        check("equity 스냅샷 포함(자산곡선 연속)", "account" in r, sorted(r.keys()))
        check("사유에 주기·레짐 표기", "재선정" in r.get("reason", "") and "레짐" in r.get("reason", ""))
        r2 = run_once(None, br, cfg, today="2026-06-01", reselect_due=False)
        check("hold 가 당일 완료 기록(주말 재적재 방지)", r2["status"] == "already_ran", r2["status"])
        live_engine.regime_on = lambda **k: False
        r3 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-02", reselect_due=False)
        check("레짐 OFF → hold 로 멈추지 않음(청산 경로 진행)", r3["status"] != "hold", r3["status"])
        r4 = run_once(None, FakeBroker(fill=True), cfg, today="2026-06-03", reselect_due=True)
        check("재선정일(due) → 정상 리밸런스(ok)", r4["status"] == "ok", r4["status"])
    finally:
        live_engine.select, live_engine.regime_on = orig_sel, orig_reg


def test_total_dd_trip_flattens_longs():
    print("[DD-FLAT] 누적DD 트립 → 보호 전량청산(동결 해소) — 트립 순간 + halted 잔여 재시도, daily_loss 는 미청산")
    from tests_stage1 import _use_temp_state, _fake_select
    from broker.guardrail import KillSwitch
    _use_temp_state()
    from live_engine import RunConfig, run_once
    orig_sel = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.33})
    px = {"AAA": 100.0, "BBB": 50.0}
    try:
        cfg = RunConfig(vol_target=0.0)
        # 시드: 당일 baseline 은 현재와 동일(일일손실 0) + hwm 만 고점(-50% 누적DD) → total_drawdown 만 트립
        pb = PaperBroker(cash=0.0, price_fn=lambda s: px[s], commission=0.0, spread=0.0, slippage=0.0)
        pb._positions["AAA"] = Position("AAA", 100.0, 100.0)          # equity $10k
        ks = KillSwitch(today="2026-06-01")
        ks.state.update(day="2026-06-01", day_start_equity=10000.0, last_equity=10000.0, hwm=20000.0)
        ks._save()
        r = run_once(None, pb, cfg, today="2026-06-01", force=True)
        check("트립 보고(tripped)", r["status"] == "tripped", r.get("status"))
        check("누적DD 사유", "누적 드로다운" in r.get("reason", ""), r.get("reason"))
        check("보호 전량청산 표기", "보호 전량청산" in r.get("reason", ""), r.get("reason"))
        check("SELL 체결 포함", any(o["side"] == "SELL" and o["symbol"] == "AAA"
                                  and o["status"] == "FILLED" for o in r.get("orders", [])), r.get("orders"))
        check("책 비워짐(롱 동결 해소)", pb.get_positions() == [], pb.get_positions())
        # halted 상태에서 잔여 포지션(부분실패 모사) → 재실행이 멱등 재청산
        pb._positions["BBB"] = Position("BBB", 10.0, 50.0)
        r2 = run_once(None, pb, cfg, today="2026-06-01", force=True)
        check("이후 실행 halted 보고", r2["status"] == "halted", r2.get("status"))
        check("잔여 BBB 재청산(멱등)", pb.get_positions() == []
              and any(o["symbol"] == "BBB" and o["side"] == "SELL" for o in r2.get("orders", [])),
              (pb.get_positions(), r2.get("orders")))
        # 대조: daily_loss 트립은 청산하지 않음(익일 자동해제가 설계)
        _use_temp_state()
        pb3 = PaperBroker(cash=0.0, price_fn=lambda s: px[s], commission=0.0, spread=0.0, slippage=0.0)
        pb3._positions["AAA"] = Position("AAA", 100.0, 100.0)         # equity $10k
        ks3 = KillSwitch(today="2026-06-01")
        ks3.state.update(day="2026-06-01", day_start_equity=12000.0, last_equity=12000.0, hwm=12000.0)
        ks3._save()                                                    # 당일 -16.7% → daily_loss 트립, 누적DD 은 한도 내
        r3 = run_once(None, pb3, cfg, today="2026-06-01", force=True)
        check("daily_loss 트립 보고", r3["status"] == "tripped" and "일일손실" in r3.get("reason", ""),
              (r3.get("status"), r3.get("reason")))
        check("daily_loss 는 미청산(익일 자동해제 설계)", len(pb3.get_positions()) == 1,
              pb3.get_positions())
    finally:
        live_engine.select = orig_sel


def main():
    print("=" * 70)
    print(" 모의매매 페르소나 검증 — 네트워크 없음 (합성·몽키패치)")
    print("=" * 70)
    print()
    for t in (test_buffett_value_screen, test_wood_growth_screen,
              test_wood_core_dropna_guard, test_buffett_piotroski_isolation,
              test_buffett_screen_degraded_graceful,
              test_degraded_reasons_propagate, test_wood_unscored_momentum_fallback_flagged,
              test_buffett_missing_margin_rejected, test_buffett_pct_change_fill_method,
              test_fmp_stale_cache_age_cap,
              test_wood_ps_outlier_does_not_govern, test_wood_momentum_not_double_counted,
              test_wood_dividend_gate,
              test_buffett_value_trap_veto,
              test_personas_presets,
              test_expanded_universes,
              test_paper_book_persistence, test_dispatch_registration,
              test_qv_partial_missing, test_qv_negative_equity_not_top,
              test_buffett_negative_equity_not_selected, test_qv_roe_term,
              test_qv_object_dtype_survives,
              test_v2_sector_shrinkage, test_v2_soft_penalty, test_v2_roic_axis,
              test_v2_select_cut_boundary, test_v2_sector_absent_graceful,
              test_v2_ab_isolation, test_v2_v1_untouched, test_v2_operational_wiring,
              test_buffett_marketcap_only_demoted,
              test_buffett_trend_nan_kept, test_load_atomic_corrupt,
              test_paper_missing_price_fallback, test_guarded_orphan_sell_fallback,
              test_book_qty_2dp_policy, test_order_reason_recorded,
              test_fmp_pacing_env_override,
              test_control_personas_and_dials, test_last_reselect_session,
              test_reselect_hold_gate, test_total_dd_trip_flattens_longs):
        t()
        print()
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
