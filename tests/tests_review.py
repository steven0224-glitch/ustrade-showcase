"""review(자기검토·재귀검증·cost_buffer 자동튜닝) 검증 — 네트워크 0, 순수 함수 위주.

핵심: 재귀검증이 보호종목매매·더블바이·드리프트를 잡는가 + 자동튜닝 안전 envelope
(최소표본·하드클램프·스텝제한)이 지켜지는가 + run_live 가 읽는 read_tuned 클램프.
"""
import sys
import tempfile
from pathlib import Path

import review
from review import (round_trips, verify_invariants, buy_slippages, compute_tune,
                    extract_fills, read_tuned_cost_buffer, BUF_MIN, BUF_MAX, BUF_STEP, MIN_SAMPLE)
from dashboard.build_data import persona_stats

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _f(symbol, side, qty, fill, ref=None, session="2026-06-22", ts="2026-06-22T23:00:00"):
    return {"session": session, "ts": ts, "src": "runs", "symbol": symbol, "side": side,
            "qty": qty, "fill": fill, "ref": ref}


def test_round_trips_fifo():
    print("[RT] FIFO 라운드트립 매칭 + 실현 P&L")
    fills = [_f("KMI", "BUY", 1, 31.79, ts="...01"), _f("KMI", "SELL", 1, 31.68, ts="...02")]
    rt = round_trips(fills)
    check("라운드트립 1건", len(rt["trips"]) == 1, rt)
    check("P&L = 1*(31.68-31.79) = -0.11", abs(rt["trips"][0]["pnl"] - (-0.11)) < 1e-9, rt["trips"])
    check("미청산 없음", rt["open"] == {}, rt["open"])


def test_round_trips_partial_open():
    print("[RT] 부분청산 → 잔량 미청산으로 남음")
    fills = [_f("AAA", "BUY", 3, 100, ts="..1"), _f("AAA", "SELL", 1, 110, ts="..2")]
    rt = round_trips(fills)
    check("닫힌 1건(1주)", len(rt["trips"]) == 1 and rt["trips"][0]["qty"] == 1)
    check("미청산 2주", rt["open"].get("AAA") == 2, rt["open"])


def test_symbol_alias_fifo_continuity():
    print("[★별칭] 티커 개명(BK→BNY) 전후 FIFO 라운드트립이 안 끊김")
    from review import _norm, SYMBOL_ALIASES
    check("BK→BNY 별칭 등록", SYMBOL_ALIASES.get("BK") == "BNY", SYMBOL_ALIASES)
    check("_norm(BK)==BNY", _norm("BK") == "BNY", _norm("BK"))
    check("_norm(bk) 소문자도 동일", _norm("bk") == "BNY", _norm("bk"))
    recs = [{"session": "s", "ts": "t1", "_src": "runs", "orders": [
        {"side": "BUY", "symbol": "BK", "qty": 5, "fill": 40.0, "status": "FILLED"}]},
            {"session": "s", "ts": "t2", "_src": "runs", "orders": [
        {"side": "SELL", "symbol": "BNY", "qty": 5, "fill": 45.0, "status": "FILLED"}]}]
    fills = extract_fills(recs)
    rt = round_trips(fills)
    check("개명 전후 라운드트립 매칭(끊기지 않음)",
          len(rt["trips"]) == 1 and rt["trips"][0]["symbol"] == "BNY", rt)
    check("미청산 없음", rt["open"] == {}, rt["open"])


def test_verify_protected_sale():
    print("[★검증] 보호종목 매매 → 중대 위반")
    fills = [_f("CONL", "SELL", 1, 7.0)]
    audit = verify_invariants([], fills, {"CONL"})
    check("위반 검출", any("보호종목" in v for v in audit["violations"]), audit)


def test_verify_double_buy():
    print("[★검증] 더블바이(같은 세션·심볼·수량 BUY 2회) → 위반")
    fills = [_f("NVDA", "BUY", 2, 200, ts="..a"), _f("NVDA", "BUY", 2, 201, ts="..b")]
    audit = verify_invariants([], fills, set())
    check("더블바이 검출", any("더블바이" in v for v in audit["violations"]), audit)


def test_verify_clean():
    print("[검증] 정상 기록 → 위반 0")
    fills = [_f("KMI", "BUY", 1, 31.79, ref=31.7), _f("KMI", "SELL", 1, 31.68)]
    audit = verify_invariants([{"status": "ok", "reconcile": {"ok": True}}], fills, {"CONL"})
    check("위반 0", audit["violations"] == [], audit["violations"])


def test_verify_drift_incident():
    print("[검증] reconcile 드리프트 → incident")
    recs = [{"status": "ok", "session": "2026-06-22", "reconcile": {"ok": False, "drift": [{"symbol": "X"}]}}]
    audit = verify_invariants(recs, [], set())
    check("드리프트 incident", any("드리프트" in i for i in audit["incidents"]), audit)


def test_verify_bad_fill_price():
    print("[검증] 체결가<=0 → 위반")
    audit = verify_invariants([], [_f("X", "BUY", 1, 0.0)], set())
    check("0 체결가 위반", any("비정상 체결가" in v for v in audit["violations"]), audit)


def test_extract_fills_filters():
    print("[EXTRACT] FILLED·fill>0 만 추출")
    recs = [{"session": "s", "ts": "t", "_src": "runs", "orders": [
        {"side": "BUY", "symbol": "A", "qty": 1, "fill": 10.0, "ref": 9.9, "status": "FILLED"},
        {"side": "BUY", "symbol": "B", "qty": 1, "fill": 0.0, "status": "FILLED"},      # 0가 → 제외
        {"side": "SELL", "symbol": "C", "qty": 1, "fill": 5.0, "status": "REJECTED"},   # 미체결 → 제외
    ]}]
    fills = extract_fills(recs)
    check("1건만(A)", len(fills) == 1 and fills[0]["symbol"] == "A", fills)


def test_invalid_fill_surfaces_inv2():
    print("[★검증] extract_fills 가 사전제외한 fill<=0/qty<=0 도 invalid_out 경유 INV-2 위반으로 발동")
    recs = [{"session": "s", "ts": "t", "_src": "runs", "orders": [
        {"side": "BUY", "symbol": "A", "qty": 1, "fill": 10.0, "ref": 9.9, "status": "FILLED"},
        {"side": "BUY", "symbol": "B", "qty": 1, "fill": 0.0, "status": "FILLED"},   # 이전엔 무음제외 → INV-2 발동 불가
        {"side": "BUY", "symbol": "C", "qty": 0, "fill": 10.0, "status": "FILLED"},  # qty<=0 도 동일 결함
    ]}]
    invalid = []
    fills = extract_fills(recs, invalid_out=invalid)
    check("정상 체결 1건만 fills 에(기존 동작 불변)", len(fills) == 1 and fills[0]["symbol"] == "A", fills)
    check("걸러진 2건이 invalid_out 에 수집", len(invalid) == 2, invalid)
    check("무인자 호출은 하위호환(그냥 무음 필터, mcp_server/report_html 불변)",
          extract_fills(recs) == fills, extract_fills(recs))
    audit = verify_invariants(recs, fills, set(), invalid)
    n_bad = sum(1 for v in audit["violations"] if "비정상" in v)
    check("INV-2 가 사전제외분(B,C) 2건 모두 위반 보고", n_bad == 2, audit["violations"])


def test_buy_slippage_signal():
    print("[SLIP] (체결가-기준가)/기준가 = 순수 시장가 슬리피지")
    s = buy_slippages([_f("KMI", "BUY", 1, 31.90, ref=31.79)])
    check("슬리피지 ≈ 0.346%", abs(s[0] - (31.90 - 31.79) / 31.79) < 1e-9, s)
    check("SELL·ref없음 제외", buy_slippages([_f("X", "SELL", 1, 10, ref=10)]) == [], "sell excluded")


def test_tune_min_sample_gate():
    print("[★튜닝] 표본<MIN → 변경 안 함(과적합 차단)")
    t = compute_tune([0.006] * (MIN_SAMPLE - 1), current=0.005)
    check("changed False", t["changed"] is False, t)
    check("proposed==current", t["proposed"] == 0.005, t)


def test_tune_within_bounds_and_step():
    print("[★튜닝] 표본 충분 → 클램프+스텝 안에서만 이동")
    # 정상 슬리피지 0.6% → 0.5%→0.6% (스텝 0.2% 이내)
    t = compute_tune([0.006] * 10, current=0.005)
    check("0.6%로 상향", abs(t["proposed"] - 0.006) < 1e-9, t)
    check("범위 내", BUF_MIN <= t["proposed"] <= BUF_MAX, t)


def test_tune_step_caps_extreme():
    print("[★튜닝] 극단 슬리피지여도 1회 ±STEP 만 + 하드클램프")
    t = compute_tune([0.05] * 10, current=0.005)   # target=clamp(0.05)->0.01, 하지만 스텝 0.002
    check("current+STEP 로 제한(0.007)", abs(t["proposed"] - (0.005 + BUF_STEP)) < 1e-9, t)
    check("BUF_MAX 절대 초과 안 함", t["proposed"] <= BUF_MAX, t)


def test_tune_floor_on_low_slip():
    print("[★튜닝] 음수/낮은 슬리피지 → BUF_MIN 바닥, 스텝제한")
    t = compute_tune([-0.01] * 10, current=0.005)   # 음수→0, target=BUF_MIN(0.003)
    check("0.003 바닥으로(스텝 안)", abs(t["proposed"] - max(BUF_MIN, 0.005 - BUF_STEP)) < 1e-9, t)
    check(">=BUF_MIN", t["proposed"] >= BUF_MIN, t)


def test_load_excludes_paper(tmp):
    print("[LOAD] 실거래 감사 — 페이퍼 런 제외(테스트 노이즈 차단)")
    import json as _j
    (tmp / "runs.jsonl").write_text(
        _j.dumps({"ts": "t1", "session": "s", "broker": "paper", "status": "ok",
                  "orders": [{"side": "BUY", "symbol": "CVX", "qty": 173, "fill": 100.0, "status": "FILLED"}]}) + "\n"
        + _j.dumps({"ts": "t2", "session": "s", "broker": "toss", "status": "ok",
                    "orders": [{"side": "BUY", "symbol": "KMI", "qty": 1, "fill": 31.79, "status": "FILLED"}]}) + "\n",
        encoding="utf-8")
    recs = review.load_journals(log_dir=tmp, real_only=True)
    syms = [o["symbol"] for r in recs for o in (r.get("orders") or [])]
    check("페이퍼 CVX 제외", "CVX" not in syms, syms)
    check("실거래 KMI 포함", "KMI" in syms, syms)
    check("real_only=False면 둘 다", "CVX" in [o["symbol"] for r in review.load_journals(log_dir=tmp, real_only=False)
                                              for o in (r.get("orders") or [])])


def test_read_tuned_clamp(tmp):
    print("[READ] run_live가 읽는 cost_buffer — 미존재 default / 범위밖 클램프")
    orig = review.TUNING_FILE
    try:
        review.TUNING_FILE = tmp / "nope.json"
        check("미존재 → default 0.005", read_tuned_cost_buffer(0.005) == 0.005)
        p = tmp / "tuning.json"
        review.TUNING_FILE = p
        p.write_text('{"cost_buffer": 0.007}', encoding="utf-8")
        check("정상값 0.007 그대로", abs(read_tuned_cost_buffer() - 0.007) < 1e-9)
        p.write_text('{"cost_buffer": 0.05}', encoding="utf-8")
        check("범위밖 0.05 → BUF_MAX 클램프", abs(read_tuned_cost_buffer() - BUF_MAX) < 1e-9)
        p.write_text('{"cost_buffer": 0.0001}', encoding="utf-8")
        check("범위밖 0.0001 → BUF_MIN 클램프", abs(read_tuned_cost_buffer() - BUF_MIN) < 1e-9)
        p.write_text('garbage{', encoding="utf-8")
        check("손상 → default(throw 안 함)", read_tuned_cost_buffer(0.005) == 0.005)
    finally:
        review.TUNING_FILE = orig


def test_violation_notify_before_tune_and_tune_isolated(tmp):
    print("[★순서] CRITICAL 위반 notify가 apply_tune(무보호 파일쓰기)보다 먼저 + 튜닝 예외 격리(경보 안 삼킴)")
    import json as _j
    (tmp / "runs.jsonl").write_text(
        _j.dumps({"ts": "t1", "session": "s", "broker": "toss", "status": "ok",
                  "orders": [{"side": "BUY", "symbol": "KMI", "qty": 1, "fill": 31.79, "status": "FILLED"}]}) + "\n",
        encoding="utf-8")
    captured = []
    orig = (review.notify, review.apply_tune, review.verify_invariants, review.LOG_DIR, review.TUNING_FILE)
    review.notify = lambda m, *a, **k: captured.append(m)
    review.verify_invariants = lambda *a, **k: {"violations": ["가짜 위반 — 순서검증용"], "incidents": []}
    review.LOG_DIR = tmp                        # _write_report 가 실 운영 로그를 건드리지 않게
    review.TUNING_FILE = tmp / "tuning.json"     # read_tuned_cost_buffer 가 실 운영 상태를 안 읽게

    def _boom_apply(tune, ts):
        raise RuntimeError("tuning.json 쓰기 실패(디스크 가득 등 모사)")
    review.apply_tune = _boom_apply
    try:
        # 수리 전 코드는 apply_tune 이 여기서 위로 raise → 아래 위반 notify 가 영영 발송 안 됨(CRIT 버그).
        res = review.run_review(do_tune=True, dry=False, log_dir=tmp)
        check("run_review 크래시 없이 완주(튜닝 예외 격리)", True)
        check("위반 audit 그대로 보고", res["audit"]["violations"] == ["가짜 위반 — 순서검증용"], res["audit"])
        viol_idx = next((i for i, m in enumerate(captured) if "자기검증 위반" in m), None)
        tune_fail_idx = next((i for i, m in enumerate(captured)
                              if "튜닝" in m and ("실패" in m or "오류" in m)), None)
        check("위반 CRITICAL notify 발송됨(튜닝 예외에 안 삼켜짐)", viol_idx is not None, captured)
        check("튜닝 실패 notify 도 별도 발송됨", tune_fail_idx is not None, captured)
        check("위반 notify 가 튜닝실패 notify 보다 먼저(순서 교환 확인)",
              viol_idx is not None and tune_fail_idx is not None and viol_idx < tune_fail_idx, captured)
    finally:
        review.notify, review.apply_tune, review.verify_invariants, review.LOG_DIR, review.TUNING_FILE = orig


def test_selection_review_wiring(tmp):
    print("[SEL-WIRE] review 곁들이기 — 정상 시 리포트 기록, 실패해도 무해(throw 안 함, 종료코드 무영향)")
    import selection_review as sr
    orig_log, orig_run = review.LOG_DIR, sr.run
    review.LOG_DIR = tmp
    try:
        sr.run = lambda *a, **k: ("# 더미 신호리포트", {}, {"n_picks": 3, "n_eval": 2, "pending": 1, "horizon": 20})
        review._run_selection_review()
        files = list((tmp / "selection_review").glob("*_h20.md"))
        check("정상 → selection_review 리포트 1건 기록",
              len(files) == 1 and "더미" in files[0].read_text(encoding="utf-8"), files)
        def _boom(*a, **k):
            raise RuntimeError("network down")
        sr.run = _boom
        threw = False
        try:
            review._run_selection_review()
        except Exception:
            threw = True
        check("실패해도 throw 안 함(graceful — 튜닝/검증·종료코드 무영향)", not threw)
    finally:
        review.LOG_DIR, sr.run = orig_log, orig_run


def _tr(tk, side, qty, fill, ts):
    return {"side": side, "tk": tk, "qty": qty, "fill": fill, "ts": ts}


def test_persona_stats_win_loss():
    print("[PSTAT] 승자+패자 FIFO 라운드트립 → 승률/손익비/기대값/연속손실/MDD")
    trades = [
        _tr("AAA", "BUY", 1, 100.0, "..1"), _tr("AAA", "SELL", 1, 120.0, "..2"),   # +20 승
        _tr("BBB", "BUY", 1, 50.0, "..3"), _tr("BBB", "SELL", 1, 40.0, "..4"),     # -10 패
    ]
    curve = [100.0, 110.0, 90.0, 100.0]   # peak 110 → 90/110-1 = -18.18%
    st = persona_stats(trades, seed=100.0, curve=curve)
    check("n_trades=2", st["n_trades"] == 2, st)
    check("win_rate=50.0", abs(st["win_rate"] - 50.0) < 1e-9, st)
    check("avg_win=20.0", abs(st["avg_win"] - 20.0) < 1e-9, st)
    check("avg_loss=-10.0", abs(st["avg_loss"] - (-10.0)) < 1e-9, st)
    check("profit_factor=2.0 (20/10)", abs(st["profit_factor"] - 2.0) < 1e-9, st)
    check("expectancy=5.0 ((20-10)/2)", abs(st["expectancy"] - 5.0) < 1e-9, st)
    check("max_consec_losses=1", st["max_consec_losses"] == 1, st)
    check("mdd≈-18.18%", abs(st["mdd"] - (-18.18)) < 0.02, st)


def test_persona_stats_zero_trades():
    print("[PSTAT] 0건 → 전부 0, profit_factor=None")
    st = persona_stats([], seed=100.0, curve=[100.0])
    check("n_trades=0", st["n_trades"] == 0, st)
    check("win_rate=0", st["win_rate"] == 0.0, st)
    check("profit_factor None", st["profit_factor"] is None, st)
    check("expectancy=0", st["expectancy"] == 0.0, st)
    check("max_consec_losses=0", st["max_consec_losses"] == 0, st)


def test_persona_stats_partial_fill_fifo():
    print("[PSTAT] 부분체결 FIFO — 매도 1건이 두 매수 lot 에 걸침")
    trades = [
        _tr("CCC", "BUY", 2, 10.0, "..1"),    # lot1: 2주 @10
        _tr("CCC", "BUY", 2, 20.0, "..2"),    # lot2: 2주 @20
        _tr("CCC", "SELL", 3, 30.0, "..3"),   # 3주 매도 → lot1 전량(2)+lot2 일부(1)
    ]
    st = persona_stats(trades, seed=None, curve=[])
    # 트립1: 2*(30-10)=40, 트립2: 1*(30-20)=10 → 2건 모두 승, 평균승=(40+10)/2=25
    check("n_trades=2 (분할 라운드트립)", st["n_trades"] == 2, st)
    check("win_rate=100.0", abs(st["win_rate"] - 100.0) < 1e-9, st)
    check("avg_win=25.0 ((40+10)/2)", abs(st["avg_win"] - 25.0) < 1e-9, st)
    check("profit_factor None(손실 없음)", st["profit_factor"] is None, st)
    check("expectancy=25.0", abs(st["expectancy"] - 25.0) < 1e-9, st)


def test_load_excludes_paper_crash(tmp):
    print("[LOAD] paper 크래시 레코드(broker=paper)도 real_only 배제 — 페르소나 크래시 실거래 감사 누수 차단")
    import json as _j
    (tmp / "runs.jsonl").write_text(
        _j.dumps({"ts": "t1", "session": "s", "broker": "paper", "persona": "wood",
                  "status": "crash", "reason": "boom"}) + "\n"
        + _j.dumps({"ts": "t2", "session": "s", "broker": "toss", "status": "ok", "orders": []}) + "\n",
        encoding="utf-8")
    recs = review.load_journals(log_dir=tmp, real_only=True)
    statuses = [r.get("status") for r in recs]
    check("paper 크래시 배제(real audit 무오염)", "crash" not in statuses, statuses)
    check("실거래 ok 포함", "ok" in statuses, statuses)


def main():
    print("=" * 70)
    print(" review(자기검토·재귀검증·자동튜닝) 검증")
    print("=" * 70)
    print()
    tmp = Path(tempfile.mkdtemp())
    pure = [test_round_trips_fifo, test_round_trips_partial_open, test_symbol_alias_fifo_continuity,
            test_verify_protected_sale,
            test_verify_double_buy, test_verify_clean, test_verify_drift_incident,
            test_verify_bad_fill_price, test_extract_fills_filters, test_invalid_fill_surfaces_inv2,
            test_buy_slippage_signal,
            test_tune_min_sample_gate, test_tune_within_bounds_and_step, test_tune_step_caps_extreme,
            test_tune_floor_on_low_slip, test_persona_stats_win_loss, test_persona_stats_zero_trades,
            test_persona_stats_partial_fill_fifo]
    for t in pure:
        t(); print()
    test_load_excludes_paper(tmp); print()
    test_load_excludes_paper_crash(tmp); print()
    test_read_tuned_clamp(tmp); print()
    test_violation_notify_before_tune_and_tune_isolated(tmp); print()
    test_selection_review_wiring(tmp); print()
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
