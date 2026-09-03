"""Stage 2 (HIGH 수정) 검증 — 네트워크 불필요 (monkeypatch).

H1 staleness 가드 / H2 NYSE 캘린더(ET) / H3 비정상시세 가드 / H4 다운로드 실패율.
실행:  & $py tests_stage2.py
"""
import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from broker.base import OrderRequest, Side, OrderType
from tests_stage1 import FakeBroker, _fake_select, _use_temp_state

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _prices(end, periods=260, cols=("AAA", "BBB", "CCC")):
    idx = pd.bdate_range(end=end, periods=periods)
    return pd.DataFrame({c: np.linspace(100, 200, periods) for c in cols}, index=idx)


# ───────────────────────── H2 — NYSE 캘린더 (ET) ─────────────────────────
def test_h2_calendar():
    print("[H2] NYSE 세션/ET — 휴장일·주말·장중·DST 인지")
    import calendar_util as c
    check("Memorial Day(2026-05-25) 휴장", not c.is_session("2026-05-25"))
    check("토요일(2026-05-30) 휴장", not c.is_session("2026-05-30"))
    check("화요일(2026-05-26) 개장", c.is_session("2026-05-26"))
    sat = datetime(2026, 5, 30, 16, 0, tzinfo=timezone.utc).astimezone(c.ET)
    check("토요일 기준 직전 세션 = 금(05-29)", str(c.last_completed_session(sat)) == "2026-05-29",
          c.last_completed_session(sat))
    # 화 장중(10:00 ET, 마감 전) → 직전 세션 = 금 05-22 (05-25 휴장)
    tue_open = datetime(2026, 5, 26, 14, 0, tzinfo=timezone.utc).astimezone(c.ET)
    check("장중(마감 전)이면 직전 세션", str(c.last_completed_session(tue_open)) == "2026-05-22",
          c.last_completed_session(tue_open))
    check("session_gap 인접세션=1", c.session_gap("2026-05-22", "2026-05-26") == 1)
    check("session_gap 동일=0", c.session_gap("2026-05-26", "2026-05-26") == 0)
    check("session_gap 미래데이터=0", c.session_gap("2026-06-10", "2026-05-26") == 0)


# ───────────────────────── H1 — 데이터 신선도 가드 ─────────────────────────
def test_h1_staleness():
    print("[H1] 마지막 봉이 너무 오래되면 status=stale (거래 보류)")
    _use_temp_state()
    import live_engine
    from live_engine import RunConfig, run_once

    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.33, "BBB": 0.33, "CCC": 0.33})
    try:
        cfg = RunConfig(vol_target=0.0, max_staleness_sessions=3)
        # stale: 마지막봉 2026-04-01, 기준 2026-06-01 → 수십 세션 gap
        stale_prices = _prices("2026-04-01")
        r_stale = run_once(stale_prices, FakeBroker(fill=True), cfg, today="2026-06-01")
        # fresh: 마지막봉 2026-05-29(금), 기준 2026-06-01(월) → gap 1 ≤ 3
        fresh_prices = _prices("2026-05-29")
        r_fresh = run_once(fresh_prices, FakeBroker(fill=True), cfg, today="2026-06-01", force=True)
        # 비활성(0): stale 데이터라도 통과
        cfg0 = RunConfig(vol_target=0.0, max_staleness_sessions=0)
        r_off = run_once(stale_prices, FakeBroker(fill=True), cfg0, today="2026-06-01", force=True)
    finally:
        live_engine.select = orig

    check("오래된 데이터 → stale", r_stale["status"] == "stale", r_stale["status"])
    check("stale 사유에 세션수 표기", "stale" in r_stale.get("reason", ""))
    check("최신 데이터 → 거래 진행(ok)", r_fresh["status"] == "ok", r_fresh["status"])
    check("max_staleness=0 면 가드 비활성", r_off["status"] == "ok", r_off["status"])


# ───────────────────────── H3 — 비정상 시세 가드 ─────────────────────────
def test_h3_bad_quote():
    print("[H3] 0/NaN/음수 시세 → 사이징·주문 거부 (나눗셈 폭주 차단)")
    from broker import Executor, GuardedBroker, KillSwitch

    # Executor.plan: 0 시세 → ValueError
    for bad, label in [(0.0, "0"), (float("nan"), "NaN"), (-5.0, "음수")]:
        exe = Executor(FakeBroker(fill=True, price=bad))
        raised = False
        try:
            exe.plan({"AAA": 0.5, "BBB": 0.5})
        except ValueError:
            raised = True
        check(f"plan: 시세 {label} → ValueError", raised)

    # GuardedBroker 경계: 0 시세 → ValueError
    _use_temp_state()
    ks = KillSwitch(today="2026-06-01")
    gb = GuardedBroker(FakeBroker(fill=True, price=float("nan")), ks)
    raised = False
    try:
        gb.place_order(OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    except ValueError:
        raised = True
    check("GuardedBroker: NaN 시세 → ValueError", raised)

    # run_once 통합: 비정상 시세 → status=error (3회 전엔 정지 아님)
    import live_engine
    from live_engine import RunConfig, run_once
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.33, "BBB": 0.33, "CCC": 0.33})
    try:
        cfg = RunConfig(vol_target=0.0, max_staleness_sessions=0)
        r = run_once(_prices("2026-05-29"), FakeBroker(fill=True, price=0.0), cfg,
                     today="2026-06-01", force=True)
    finally:
        live_engine.select = orig
    check("run_once: 비정상 시세 → error", r["status"] == "error", r["status"])


# ───────────────────────── H4 — 다운로드 실패율 임계 ─────────────────────────
def test_h4_panel_failrate():
    print("[H4] 다운로드 실패율 임계 초과 → raise (조용한 유니버스 축소 차단)")
    import data
    orig = data.load
    fail_set = {"B", "D"}

    def flaky(t, s, e, force=False):
        if t in fail_set:
            raise ValueError("net err")
        return pd.DataFrame({"Close": [1.0, 2.0, 3.0]}, index=pd.bdate_range("2024-01-01", periods=3))

    data.load = flaky
    try:
        # 2/5 = 40% > 20% → raise
        raised = False
        try:
            data.load_panel(["A", "B", "C", "D", "E"], "2024-01-01", "2024-02-01")
        except ValueError as ex:
            raised = "실패율" in str(ex)
        check("실패율 40% > 20% → raise", raised)

        # 1/5 = 20% (>20% 아님) → 통과, 실패종목 제외
        global_fail = {"B"}
        fail_set.clear(); fail_set.update(global_fail)
        panel = data.load_panel(["A", "B", "C", "D", "E"], "2024-01-01", "2024-02-01")
        check("실패율 20% (한도 이내) → 통과", panel is not None and "B" not in panel.columns,
              list(panel.columns))
        check("성공 종목만 패널에", set(panel.columns) == {"A", "C", "D", "E"}, list(panel.columns))
    finally:
        data.load = orig


def main():
    print("=" * 70)
    print(" Stage 2 (HIGH) 검증 — 네트워크 없음")
    print("=" * 70)
    for t in (test_h2_calendar, test_h1_staleness, test_h3_bad_quote, test_h4_panel_failrate):
        print()
        t()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
