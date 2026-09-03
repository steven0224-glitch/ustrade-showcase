"""Stage 8 (최종 멀티에이전트 감사 후속 수정) 검증 — 네트워크 불필요.

OPS-1  run_live 종료코드 매핑 (None→exit0 버그)
STRAT-2 과소선택 등비중>단일한도 → 영구정지 대신 비중 캡
STRAT-1 FMP 스크린 NaN/결측 조용한 통과 차단 (+ live_select 전필드결측→missing)
STRAT-3 선택 공집합 → 전량청산 아니라 skip(보류)
GUARD-1 고점대비 누적 드로다운 한도 (다일 그라인드다운)
GUARD-2 명목캡 자산 비례화 (작은 계좌 2배 사이징버그 트립)
ROBUST  RunLock 좀비락 pid 생존확인 후에만 회수
ROBUST-HB RunLock 보유 중 하트비트 mtime 갱신 → 살아있는 장기 실행 탈취 방지
EXEC-1  매수를 가용현금 예산 내로 캡 (풀투자 회전 시 현금부족 거부 방지)
실행:  & $py tests_stage8.py
"""
import os
import sys
import time

import numpy as np
import pandas as pd

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ───── OPS-1 — run_live.main 종료코드 매핑 ─────
def test_ops1_exit_code():
    print("[OPS-1] run_live.main() 상태→종료코드 (None→exit0 버그 차단)")
    import run_live
    orig_run, orig_argv = run_live.run, sys.argv
    sys.argv = ["run_live.py"]
    cases = [("ok", 0), ("already_ran", 0), ("locked", 0), ("skip", 0),
             ("stale", 1), ("partial", 1),
             ("halted", 2), ("tripped", 2), ("error", 2), ("crash", 2)]
    try:
        for status, expect in cases:
            run_live.run = (lambda s: lambda **kw: {"status": s, "reason": ""})(status)
            code = run_live.main()
            check(f"status={status} → exit {expect}", code == expect, f"got {code}")
    finally:
        run_live.run, sys.argv = orig_run, orig_argv


# ───── STRAT-2 — 과소선택 비중 캡 (영구정지 아님) ─────
def test_strat2_underpopulation_cap():
    print("[STRAT-2] 과소선택 등비중 50%>40% → 영구정지 아니라 한도로 캡")
    from tests_stage1 import _use_temp_state, _fake_select
    from tests_stage4 import _Broker
    import live_engine
    from live_engine import RunConfig, run_once

    _use_temp_state()
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.5, "BBB": 0.5})   # 스크린이 2종목만 남긴 상황
    try:
        res = run_once(None, _Broker(fill=True),
                       RunConfig(top_n=3, vol_target=0.0, max_staleness_sessions=0),
                       today="2026-06-03", force=True)
    finally:
        live_engine.select = orig
    check("과소선택 → tripped 아님(ok)", res["status"] == "ok", res["status"])
    check("비중이 40% 한도로 캡됨",
          res.get("weights") and all(w <= 0.40 + 1e-9 for w in res["weights"].values()),
          res.get("weights"))
    check("weight_capped 플래그 기록", "weight_capped" in res.get("selection", {}),
          res.get("selection"))


# ───── STRAT-1 — FMP 스크린 NaN/결측 누수 차단 ─────
def test_strat1_screen_nan():
    print("[STRAT-1] 스크린 NaN/적자 조용한 통과 차단 + 전필드결측→missing")
    import fmp_factors as ff
    snap = pd.DataFrame({
        "net_margin": [-0.10, 0.20, np.nan],
        "pe":         [np.nan, 90.0, np.nan],
        "debt_equity": [1.0, 1.0, np.nan],
    }, index=["LOSS", "HIPE", "BLANK"])
    passed, fails = ff.screen(snap, min_net_margin=0.0, max_pe=80.0)
    check("적자(net_margin<0) 탈락", "LOSS" in fails, fails)
    check("고PE(90>80) 탈락", "HIPE" in fails, fails)

    # live_select: 전 필드 결측 행은 snap 에서 빠져 missing 으로 분류(조용한 통과 방지)
    import live_select
    idx = pd.bdate_range(end="2026-05-29", periods=200)
    prices = pd.DataFrame({"AAA": np.linspace(100, 200, 200),
                           "BBB": np.linspace(100, 160, 200),
                           "CCC": np.linspace(100, 130, 200)}, index=idx)

    def fake_snap(cands, fmp=None):
        df = pd.DataFrame({"net_margin": [0.2, np.nan, 0.1], "pe": [15.0, np.nan, 20.0]},
                          index=["AAA", "BBB", "CCC"])
        return df.loc[[c for c in ["AAA", "BBB", "CCC"] if c in cands]]

    orig = live_select.ff.snapshot
    live_select.ff.snapshot = fake_snap
    try:
        w, info = live_select.select(prices, top_n=3, pool=8)
    finally:
        live_select.ff.snapshot = orig
    check("전필드결측(BBB) → missing 분류(조용한 통과 아님)", "BBB" in info["missing"], info["missing"])


def test_marketcap_filter():
    print("[MCAP] 시총 경계 필터 — 하한/상한 탈락 + 결측 무탈락(데이터갭 보존)")
    import fmp_factors as ff
    snap = pd.DataFrame({
        "net_margin": [0.2, 0.2, 0.2, 0.2],
        "pe":         [20.0, 20.0, 20.0, 20.0],
        "market_cap": [5e8, 50e9, 3e12, np.nan],   # 마이크로 / 대형 / 초대형 / 결측
    }, index=["MICRO", "BIG", "MEGA", "NOCAP"])
    # 하한 $10B → MICRO 탈락, 결측은 통과(NaN 비교 False = 데이터갭 보존)
    passed, fails = ff.screen(snap, min_market_cap=10e9)
    check("시총 하한 미달(MICRO) 탈락", "MICRO" in fails, fails)
    check("시총 결측(NOCAP) 무탈락(데이터갭 보존)", "NOCAP" in passed, passed)
    check("대형(BIG) 통과", "BIG" in passed, passed)
    # 상한 $1T → MEGA 탈락
    passed2, fails2 = ff.screen(snap, max_market_cap=1e12)
    check("시총 상한 초과(MEGA) 탈락", "MEGA" in fails2, fails2)
    check("기본(None) = 전원 통과(무동작)", len(ff.screen(snap)[0]) == 4, ff.screen(snap)[0])


# ───── STRAT-3 — 선택 공집합 → skip(보류), 청산 아님 ─────
def test_strat3_empty_skip():
    print("[STRAT-3] 선택 공집합 → 전량청산 아니라 skip(포지션 유지)")
    from tests_stage1 import _use_temp_state
    from tests_stage4 import _Broker
    from broker.base import Position
    import live_engine
    from live_engine import RunConfig, run_once

    _use_temp_state()
    orig = live_engine.select
    live_engine.select = lambda *a, **k: ({}, {"final": [], "candidates": []})
    broker = _Broker(fill=True, positions=[Position("OLD", 10, 100.0)])
    try:
        res = run_once(None, broker, RunConfig(vol_target=0.0, max_staleness_sessions=0),
                       today="2026-06-04", force=True)
    finally:
        live_engine.select = orig
    check("선택 공집합 → status=skip", res["status"] == "skip", res["status"])
    check("주문 0건(청산 안 함)", not res.get("orders"), res.get("orders"))


# ───── DIAL-WIRE — canslim 안전다이얼 디스패치 배선(死코드 해소) ─────
def test_canslim_dials_wired():
    print("[DIAL-WIRE] canslim 다이얼(min_score·value_trap_gate·min_proximity) 디스패치→canslim 전달 / momentum 미전달")
    from tests_stage1 import _use_temp_state
    from tests_stage4 import _Broker
    import live_engine
    from live_engine import RunConfig, run_once

    _use_temp_state()
    cap = {}
    def _capture(prices, **kw):
        cap.update(kw)
        return {}, {"final": []}                       # 빈 weights → overlay 前 skip(네트워크 0)
    orig_strat = live_engine._STRATEGIES.get("canslim")
    orig_sel = live_engine.select
    live_engine._STRATEGIES["canslim"] = _capture
    try:
        run_once(None, _Broker(fill=True),
                 RunConfig(strategy="canslim", top_n=5, vol_target=0.0, max_staleness_sessions=0,
                           min_score=2, value_trap_gate=True, min_proximity=0.9),
                 today="2026-06-03", force=True)
        check("canslim min_score=2 전달", cap.get("min_score") == 2, cap.get("min_score"))
        check("canslim value_trap_gate=True 전달", cap.get("value_trap_gate") is True)
        check("canslim min_proximity=0.9 전달", cap.get("min_proximity") == 0.9, cap.get("min_proximity"))
        # momentum 경로엔 미전달(글로벌 select 는 **_ 없어 전달 시 TypeError — 조건부 디스패치로 차단)
        mom = {}
        def _capture_mom(prices, **kw):
            mom.update(kw)
            return {}, {}
        live_engine.select = _capture_mom
        run_once(None, _Broker(fill=True),
                 RunConfig(strategy="momentum", top_n=5, vol_target=0.0, max_staleness_sessions=0,
                           min_score=2),
                 today="2026-06-03", force=True)
        check("momentum 경로엔 min_score 미전달", "min_score" not in mom, mom)
    finally:
        live_engine._STRATEGIES["canslim"] = orig_strat
        live_engine.select = orig_sel


# ───── GUARD-1 — 고점대비 누적 드로다운 한도 ─────
def test_guard1_total_drawdown():
    print("[GUARD-1] 다일 그라인드다운(-4%/일) → 일일한도 안 걸리고 누적DD 트립")
    from tests_stage1 import _use_temp_state, _fake_select
    from tests_stage4 import _Broker
    from broker import KillSwitch
    import live_engine
    from live_engine import RunConfig, run_once

    _use_temp_state()
    orig = live_engine.select
    live_engine.select = _fake_select({"AAA": 0.34, "BBB": 0.33, "CCC": 0.33})
    cfg = RunConfig(top_n=3, vol_target=0.0, max_staleness_sessions=0)
    res, eq = None, 100000.0
    try:
        for d in range(1, 12):
            res = run_once(None, _Broker(fill=True, equity=eq), cfg,
                           today=f"2026-06-{d:02d}", force=True)
            if res["status"] == "tripped":
                break
            eq *= 0.96   # 매일 -4% (일일 5% 한도엔 안 걸림)
    finally:
        live_engine.select = orig
    check("점진 하락 누적 → tripped", res["status"] == "tripped", res["status"])
    check("트립 사유가 누적 드로다운(일일손실 아님)", "드로다운" in res.get("reason", ""),
          res.get("reason"))

    # 누적DD 정지는 새 날에도 자동해제 안 됨 (daily_loss 와 달리 중대)
    ks_next = KillSwitch(today="2026-06-20")
    ks_next.resume_if_new_day()
    h, _ = ks_next.is_halted()
    check("누적DD 정지는 새 날 자동해제 안 됨", h)


# ───── GUARD-1b — 누적DD 정지 reset 시 hwm 재seed (복구 가능) ─────
def test_guard1b_reset_rebases_hwm():
    print("[GUARD-1b] 누적DD 정지 reset → hwm 재seed(즉시 재트립/복구불가 방지), 타 정지는 hwm 보존")
    from tests_stage1 import _use_temp_state
    from broker import KillSwitch, GuardConfig, HaltError

    _use_temp_state()
    cfg = GuardConfig(max_total_drawdown=0.20)
    ks = KillSwitch(cfg, today="2026-07-01")
    ks.check_total_drawdown(100000)        # hwm=100k
    try:
        ks.check_total_drawdown(75000)     # -25% (정상 실행 = scale-jump 아님) → 즉시 trip
    except HaltError:
        pass
    check("누적DD 트립됨", ks.is_halted()[0])
    ks.reset()
    check("reset 후 hwm 재seed(None)", ks.state.get("hwm") is None, ks.state.get("hwm"))
    retripped = False
    try:
        ks.check_total_drawdown(75000)   # 재seed → 같은 자산이 새 고점 → 재트립 안 함
    except HaltError:
        retripped = True
    check("reset 후 같은 자산 재트립 안 함(복구 가능)", not retripped, "재트립됨")

    # 대조 — daily_loss 정지 reset 은 hwm 보존(누적가드 무력화 방지)
    _use_temp_state()
    ks2 = KillSwitch(cfg, today="2026-07-02")
    ks2.check_total_drawdown(100000)       # hwm=100k
    ks2.trip("일일손실 테스트", kind="daily_loss")
    ks2.reset()
    check("daily_loss reset 은 hwm 보존", ks2.state.get("hwm") == 100000, ks2.state.get("hwm"))


# ───── GUARD-2 — 명목캡 자산 비례화 ─────
def test_guard2_notional_proportional():
    print("[GUARD-2] 명목캡 = 단일한도·자산·버퍼 → 2배 사이징버그 트립")
    from tests_stage1 import _use_temp_state
    from broker import KillSwitch, GuardConfig, HaltError

    _use_temp_state()
    ks = KillSwitch(GuardConfig(max_position_weight=0.40, order_notional_buffer=1.5),
                    today="2026-06-07")
    ks.roll_day(100000)   # run_equity=100k → 비례한도 = 0.40·100k·1.5 = 60k
    ok = True
    try:
        ks.check_order_notional(40000, "AAA")   # 정상 ~40% 주문
    except HaltError:
        ok = False
    check("정상 주문(40k < 60k) 통과", ok)
    tripped = False
    try:
        ks.check_order_notional(80000, "AAA")   # 2배 사이징버그 (절대캡 1M 밑이지만 비례한도 초과)
    except HaltError:
        tripped = True
    check("2배 주문(80k > 60k 비례한도) → 트립", tripped)


# ───── ROBUST — RunLock 좀비락 pid 생존확인 ─────
def test_robust_runlock_pid(tmp):
    print("[ROBUST] 좀비락 회수는 보유 pid 사망 시에만 (살아있으면 거부)")
    import broker.guardrail as g
    lock = tmp / "run.lock"

    # 1) 좀비(>1h) + 죽은 pid → 회수 성공
    lock.write_text("999999", encoding="utf-8")   # Windows pid 는 4의 배수 → 999999 무효
    old = time.time() - g._LOCK_STALE_SEC - 100
    os.utime(lock, (old, old))
    acquired = False
    try:
        with g.RunLock(path=lock):
            acquired = True
    except g.LockBusy:
        pass
    check("좀비락 + 죽은 pid → 회수", acquired)

    # 2) 좀비(>1h) + 살아있는 pid(자기 자신) → 회수 거부
    lock.write_text(str(os.getpid()), encoding="utf-8")
    os.utime(lock, (old, old))
    busy = False
    try:
        with g.RunLock(path=lock):
            pass
    except g.LockBusy:
        busy = True
    check("좀비락 + 살아있는 pid → 회수 거부(LockBusy)", busy)

    # 3) 6h(_LOCK_HARD_SEC) 초과면 pid 살아있어도 회수 (pid 재사용 대비)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    older = time.time() - g._LOCK_HARD_SEC - 100
    os.utime(lock, (older, older))
    acq3 = False
    try:
        with g.RunLock(path=lock):
            acq3 = True
    except g.LockBusy:
        pass
    check("6h 초과 좀비락 → pid 생존과 무관하게 회수", acq3)


# ───── ROBUST-HB — 락 보유 중 하트비트 mtime 갱신 (장기 실행 탈취 방지) ─────
def test_runlock_heartbeat(tmp):
    print("[ROBUST-HB] 락 보유 중 하트비트가 mtime 갱신 → 살아있는 장기 실행 좀비 오인·탈취 방지")
    import broker.guardrail as g
    lock = tmp / "run.lock"
    with g.RunLock(path=lock) as lk:
        check("하트비트 스레드 가동", lk._hb is not None and lk._hb.is_alive())
        # mtime 을 6h+ 전(hard-steal 후보)으로 강제 → _touch 가 현재로 되돌려 age 리셋하는지
        old = time.time() - g._LOCK_HARD_SEC - 100
        os.utime(lock, (old, old))
        lk._touch()
        age = time.time() - lock.stat().st_mtime
        check("_touch 후 age 가 stale 한도(1h) 미만으로 리셋", age < g._LOCK_STALE_SEC,
              f"age={age:.0f}s")
    check("락 해제 후 하트비트 스레드 정지", not lk._hb.is_alive())
    check("락 파일 제거됨", not lock.exists())


# ───── GUARD-3 — 비정상 자산값(NaN/inf) fail-closed ─────
def test_guard3_nonfinite_equity():
    print("[GUARD-3] NaN/inf 자산 → 손실/드로다운 가드 무력화 대신 fail-closed(trip)")
    from tests_stage1 import _use_temp_state
    from broker import KillSwitch, HaltError

    # roll_day(NaN) → trip(bad_equity), 오염값 영속 차단 (NaN<-limit 가 항상 False 라 통과되던 것)
    _use_temp_state()
    ks = KillSwitch(today="2026-06-10")
    tripped = False
    try:
        ks.roll_day(float("nan"))
    except HaltError:
        tripped = True
    check("roll_day(NaN) → trip", tripped and ks.is_halted()[0])
    check("trip kind=bad_equity", ks.state.get("halt_kind") == "bad_equity",
          ks.state.get("halt_kind"))
    check("NaN last_equity 영속 안 됨", ks.state.get("last_equity") is None,
          ks.state.get("last_equity"))

    # check_daily_loss(inf) → trip (dd<-limit 가 inf 에 False 라 손실판정 통과되던 것 차단)
    _use_temp_state()
    ks2 = KillSwitch(today="2026-06-11")
    ks2.state["day_start_equity"] = 100000.0
    di = False
    try:
        ks2.check_daily_loss(float("inf"))
    except HaltError:
        di = True
    check("check_daily_loss(inf) → trip", di)

    # check_total_drawdown(NaN) → trip, hwm 가 NaN 으로 영속 오염되는 것 차단
    _use_temp_state()
    ks3 = KillSwitch(today="2026-06-12")
    ks3.check_total_drawdown(100000.0)        # hwm=100k
    dt = False
    try:
        ks3.check_total_drawdown(float("nan"))
    except HaltError:
        dt = True
    check("check_total_drawdown(NaN) → trip", dt)
    check("hwm NaN 오염 안 됨(100k 유지)", ks3.state.get("hwm") == 100000.0,
          ks3.state.get("hwm"))


# ───── EXEC-1 — 매수 가용현금 예산 캡 ─────
def test_exec1_cash_budget():
    print("[EXEC-1] 풀투자(alloc=1.0) 매수 시 수수료/슬리피지로 거부되던 것 → 예산 캡")
    from broker import PaperBroker, Executor
    from broker.base import OrderStatus
    snap = {"A": 100.0}
    b = PaperBroker(cash=100000, price_fn=lambda s: snap[s],
                    commission=0.0005, spread=0.0005, slippage=0.0005)
    exe = Executor(b, alloc=1.0)   # 버퍼 0 → 수수료/슬리피지로 마지막 매수 현금부족 거부되던 조건
    orders = [b.place_order(r) for r in exe.plan({"A": 1.0})]
    rejected = [o for o in orders if o.status != OrderStatus.FILLED]
    check("alloc=1.0 풀매수 시 현금부족 거부 0건", not rejected,
          [o.message for o in rejected])
    check("매수 체결됨(수량>0)", any(o.request.qty > 0 for o in orders))


def main():
    import tempfile
    from pathlib import Path
    print("=" * 70)
    print(" Stage 8 (최종 멀티에이전트 감사 후속 수정) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    test_ops1_exit_code(); print()
    test_strat2_underpopulation_cap(); print()
    test_strat1_screen_nan(); print()
    test_marketcap_filter(); print()
    test_strat3_empty_skip(); print()
    test_canslim_dials_wired(); print()
    test_guard1_total_drawdown(); print()
    test_guard1b_reset_rebases_hwm(); print()
    test_guard2_notional_proportional(); print()
    test_guard3_nonfinite_equity(); print()
    test_robust_runlock_pid(Path(tempfile.mkdtemp())); print()
    test_runlock_heartbeat(Path(tempfile.mkdtemp())); print()
    test_exec1_cash_budget()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
