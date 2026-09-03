"""selection_review 검증 — 네트워크 0 (fake 레코드 + closes_fn 주입). 순수 관측 리포트.

실행:  python tests_selection_review.py
"""
import selection_review as sr

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


_RECS = [
    {"session": "2026-01-02", "broker": "toss", "risk": {"regime": "ON"},
     "selection": {"final": ["AAA", "BBB"], "scores": {"AAA": 2, "BBB": 1},
                   "piotroski": {"AAA": 8, "BBB": 6}, "canslim": ["AAA", "BBB"],
                   "analyst": ["AAA"], "momentum_only": ["BBB"]}},
    {"session": "2026-01-03", "broker": "toss", "risk": {"regime": "OFF"},
     "selection": {"final": ["CCC"], "scores": {"CCC": 3}, "piotroski": {"CCC": 5},
                   "canslim": ["CCC"], "analyst": [], "momentum_only": []}},
    {"session": "2026-01-04", "broker": "toss", "selection": {"final": []}},   # 빈 final → 무시
]

_CLOSES = {
    "AAA": [100, 101, 105, 110],   # h=3 → 110/100-1 = +0.10
    "BBB": [50, 49, 48, 45],       # h=3 → 45/50-1 = -0.10
    "CCC": [200, 210],             # 데이터 부족(h=3) → pending
}


def _cf(ticker, session):
    return _CLOSES.get(ticker)


def test_load_picks():
    print("[LOAD] runs 레코드 → 픽별 신호팩트, 빈 final 무시")
    picks = sr.load_picks(_RECS)
    check("픽 3개 (AAA/BBB/CCC, 빈 final 제외)", len(picks) == 3, len(picks))
    aaa = next(p for p in picks if p["ticker"] == "AAA")
    check("AAA score=2", aaa["score"] == 2, aaa["score"])
    check("AAA piotroski=8", aaa["piotroski"] == 8, aaa["piotroski"])
    check("AAA canslim=True analyst=True momentum=False", aaa["canslim"] and aaa["analyst"] and not aaa["momentum"])
    check("AAA regime=ON", aaa["regime"] == "ON", aaa["regime"])
    bbb = next(p for p in picks if p["ticker"] == "BBB")
    check("BBB momentum=True analyst=False", bbb["momentum"] and not bbb["analyst"])


def test_forward_return():
    print("[FWD] 사후수익률 — closes[h]/closes[0]-1, 데이터부족/비정상 None")
    check("정상 +10%", abs(sr.forward_return([100, 101, 105, 110], 3) - 0.10) < 1e-9)
    check("데이터 부족 → None", sr.forward_return([100, 101], 3) is None)
    check("진입가<=0 → None", sr.forward_return([0, 1, 2, 3], 2) is None)
    check("빈 리스트 → None", sr.forward_return([], 3) is None)


def test_evaluate_pending():
    print("[EVAL] 픽 + 사후수익 부착, 데이터부족 픽은 pending 제외")
    picks = sr.load_picks(_RECS)
    evaluated, pending = sr.evaluate(picks, horizon=3, closes_fn=_cf)
    check("평가 2건 (CCC pending)", len(evaluated) == 2, len(evaluated))
    check("pending 1건", pending == 1, pending)
    aaa = next(o for o in evaluated if o["ticker"] == "AAA")
    check("AAA fwd_return +0.10", abs(aaa["fwd_return"] - 0.10) < 1e-9, aaa["fwd_return"])
    # 조회 예외도 graceful (pending 처리)
    ev2, pend2 = sr.evaluate(picks, horizon=3, closes_fn=lambda t, s: (_ for _ in ()).throw(RuntimeError("x")))
    check("조회 전부 예외 → 평가0 pending3 (throw 안 함)", ev2 == [] and pend2 == 3, (ev2, pend2))


def test_bucketize():
    print("[BUCKET] 신호 차원별 집계 — n/avg/hit")
    picks = sr.load_picks(_RECS)
    evaluated, _ = sr.evaluate(picks, horizon=3, closes_fn=_cf)
    dims = sr.bucketize(evaluated)
    check("piotroski ≥8 = AAA, avg +10%", abs(dims["piotroski"]["≥8"]["avg"] - 0.10) < 1e-9, dims["piotroski"])
    check("piotroski 6-7 = BBB, avg -10%", abs(dims["piotroski"]["6-7"]["avg"] + 0.10) < 1e-9)
    check("canslim in = 2건, avg 0%, 승률 50%",
          dims["canslim"]["in"]["n"] == 2 and abs(dims["canslim"]["in"]["avg"]) < 1e-9
          and abs(dims["canslim"]["in"]["hit"] - 0.5) < 1e-9, dims["canslim"])
    check("전체 n=2 hit=50%", dims["_overall"]["n"] == 2 and abs(dims["_overall"]["hit"] - 0.5) < 1e-9)


def test_render_and_run():
    print("[RENDER] 마크다운 리포트 + run() 통합 (throw 안 함)")
    md, dims, meta = sr.run(horizon=3, recs=_RECS, closes_fn=_cf)
    check("리포트에 제목·차원 포함", "신호 성과" in md and "Piotroski" in md and "CANSLIM" in md)
    check("meta n_picks=3 n_eval=2 pending=1", meta["n_picks"] == 3 and meta["n_eval"] == 2 and meta["pending"] == 1, meta)
    # 빈 입력도 graceful — '데이터 없음' 안내
    md0, _d0, meta0 = sr.run(horizon=3, recs=[], closes_fn=_cf)
    check("빈 입력 → 평가0, 안내문", meta0["n_eval"] == 0 and "쌓인 픽이 없음" in md0)


def test_paper_included_by_default():
    print("[PAPER] 기본 real_only=False → load_journals 에 paper 포함 요청 전달 (모의매매 픽도 추적)")
    captured = {}
    orig = sr.load_journals

    def fake(*a, real_only=True, **k):
        captured["real_only"] = real_only
        return _RECS

    sr.load_journals = fake
    try:
        picks = sr.load_picks()   # recs=None → 내부 load_journals 호출
        check("load_journals(real_only=False) 호출 — paper 런 포함", captured.get("real_only") is False, captured)
        check("픽 정상 로드", len(picks) == 3, len(picks))
        captured.clear()
        sr.load_picks(real_only=True)
        check("real_only=True 명시 시 그대로 전달(실거래만)", captured.get("real_only") is True, captured)
    finally:
        sr.load_journals = orig


def test_persona_dimension():
    print("[PERSONA] persona 태그 → 픽 부착 + bucketize persona 차원 (전략 비교)")
    recs = [
        {"session": "2026-01-02", "persona": "buffett", "selection": {"final": ["AAA"]}},
        {"session": "2026-01-02", "persona": "wood", "selection": {"final": ["BBB"]}},
        {"session": "2026-01-02", "selection": {"final": ["CCC"]}},   # 태그 없음 → real
    ]
    picks = sr.load_picks(recs)
    pmap = {p["ticker"]: p["persona"] for p in picks}
    check("AAA=buffett BBB=wood CCC=real", pmap.get("AAA") == "buffett" and pmap.get("BBB") == "wood"
          and pmap.get("CCC") == "real", pmap)
    closes = {"AAA": [100, 110], "BBB": [100, 90], "CCC": [100, 105]}
    ev, _ = sr.evaluate(picks, horizon=1, closes_fn=lambda t, s: closes.get(t))
    dims = sr.bucketize(ev)
    check("persona 차원 3 버킷", "persona" in dims and set(dims["persona"]) == {"buffett", "wood", "real"},
          dims.get("persona"))
    check("buffett avg +10%", abs(dims["persona"]["buffett"]["avg"] - 0.10) < 1e-9, dims["persona"])


def test_multihome_run():
    print("[MULTIHOME] log_dirs 여러 home(페르소나 별 home) 저널 합쳐 비교")
    calls = []
    orig = sr.load_journals

    def fake(log_dir=None, real_only=True):
        calls.append(str(log_dir))
        is_b = "_b" in str(log_dir)
        return [{"session": "s", "persona": "buffett" if is_b else "wood",
                 "selection": {"final": ["AAA" if is_b else "BBB"]}}]

    sr.load_journals = fake
    try:
        md, dims, meta = sr.run(horizon=1, log_dirs=["/home_b", "/home_w"],
                                closes_fn=lambda t, s: [100, 110])
    finally:
        sr.load_journals = orig
    check("두 home 모두 로드", len(calls) == 2, calls)
    check("두 페르소나 픽 합쳐짐 (n_picks=2)", meta["n_picks"] == 2, meta)
    check("persona 차원 buffett+wood", set(dims.get("persona", {})) == {"buffett", "wood"}, dims.get("persona"))


def test_multihome_dedup():
    print("[MULTIHOME] 중복 log_dir → 1회만 집계 (기본 home 이 PERSONA_HOMES 에 들어도 픽 2배 안 됨)")
    calls = []
    orig = sr.load_journals

    def fake(log_dir=None, real_only=True):
        calls.append(str(log_dir))
        return [{"session": "s", "persona": "buffett", "selection": {"final": ["AAA"]}}]

    sr.load_journals = fake
    try:
        md, dims, meta = sr.run(horizon=1, log_dirs=["/home_x", "/home_x"],   # 같은 경로 2회
                                closes_fn=lambda t, s: [100, 110])
    finally:
        sr.load_journals = orig
    check("중복 경로 1회만 로드", len(calls) == 1, calls)
    check("픽 1건만(2배 왜곡 없음)", meta["n_picks"] == 1, meta)


def test_score_bucket_scale():
    print("[SCORE-FIX] score 버킷 — canslim 정수 그대로, buffett/wood 연속은 부호보존(int 절단으로 0버킷 붕괴 방지)")
    check("정수 1 → '1'", sr._score_bucket({"score": 1}) == "1", sr._score_bucket({"score": 1}))
    check("정수 0 → '0'", sr._score_bucket({"score": 0}) == "0")
    check("None → n/a", sr._score_bucket({"score": None}) == "n/a")
    b_pos = sr._score_bucket({"score": 0.4})
    b_neg = sr._score_bucket({"score": -0.4})
    check("+0.4 와 -0.4 다른 버킷(int 절단이면 둘다 '0'으로 뭉갬)", b_pos != b_neg, (b_pos, b_neg))
    # 정수로 반올림된 연속(buffett) 점수가 canslim 정수버킷과 안 섞임 (aliasing 방지)
    b_int = sr._score_bucket({"score": 1.0, "persona": "buffett"})
    c_int = sr._score_bucket({"score": 1, "persona": "oneil"})
    check("buffett 1.0(연속) ≠ canslim 1(정수) 버킷(aliasing 방지)", b_int != c_int, (b_int, c_int))


def test_entry_filter():
    print("[ENTRY-FIX] 실보유(positions) 기준 진입필터 — 미보유 final(레짐OFF청산·partial 미체결) 제외")
    recs = [
        {"session": "s1", "persona": "oneil", "status": "ok", "positions": [],      # 레짐OFF 청산 — 보유0
         "selection": {"final": ["AAA", "BBB"]}},
        {"session": "s2", "persona": "oneil", "status": "partial",                  # CCC 체결, DDD 미체결
         "positions": [{"symbol": "CCC", "qty": 1.0, "avg": 10.0}],
         "selection": {"final": ["CCC", "DDD"]}},
        {"session": "s3", "persona": "oneil",                                       # positions 키 없음(레거시) → 유지
         "selection": {"final": ["EEE"]}},
    ]
    tickers = sorted(p["ticker"] for p in sr.load_picks(recs))
    check("미보유(positions=[]) 픽 전부 제외(레짐OFF청산)", "AAA" not in tickers and "BBB" not in tickers, tickers)
    check("partial: 체결 CCC 포함·미체결 DDD 제외(non-ok 균일)", "CCC" in tickers and "DDD" not in tickers, tickers)
    check("positions 키 없는 레거시 → 유지(backward compat)", "EEE" in tickers, tickers)


def test_score_bucket_nonfinite():
    print("[SCORE-FIX2] _score_bucket NaN/inf → n/a (run 무throw 계약, 손상저널 graceful)")
    check("NaN → n/a", sr._score_bucket({"score": float("nan")}) == "n/a")
    check("inf → n/a", sr._score_bucket({"score": float("inf")}) == "n/a")
    check("-inf → n/a", sr._score_bucket({"score": float("-inf")}) == "n/a")


# ── DR (분산비율) — 관찰 전용 지표 ──────────────────────────────────────────
def _mk_closes(rets, p0=100.0):
    """수익률 리스트 → 종가 리스트 (결정론 합성 시계열)."""
    px = [p0]
    for r in rets:
        px.append(px[-1] * (1.0 + r))
    return px


# 서로 다른 결정론 패턴 30봉 — 완전 동행도 완전 역행도 아닌 시계열
_R1 = [0.01, -0.005, 0.003, -0.002, 0.008, -0.006] * 5
_R2 = [-0.004, 0.009, -0.001, 0.006, -0.007, 0.002] * 5
_C1, _C2 = _mk_closes(_R1), _mk_closes(_R2)


def test_div_ratio_math():
    print("[DR] div_ratio — 동행=1, 비동행>1, 데이터부족 None, 관찰전용 무throw")
    import diversification as dv
    same = dv.div_ratio({"A": 1.0, "B": 1.0}, {"A": _C1, "B": list(_C1)})
    check("완전 동행 두 종목 → DR≈1.0", same and abs(same["dr"] - 1.0) < 1e-6, same)
    mix = dv.div_ratio({"A": 1.0, "B": 1.0}, {"A": _C1, "B": _C2})
    check("비동행 두 종목 → DR>1.0", mix and mix["dr"] > 1.0, mix)
    single = dv.div_ratio({"A": 3.0}, {"A": _C1})
    check("1종목 → DR=1.0 (정의)", single and abs(single["dr"] - 1.0) < 1e-6, single)
    check("데이터 부족(<21행) → None", dv.div_ratio({"A": 1}, {"A": _C1[:10]}) is None)
    check("빈 비중 → None", dv.div_ratio({}, {"A": _C1}) is None)
    part = dv.div_ratio({"A": 1.0, "B": 1.0, "C": 1.0}, {"A": _C1, "B": _C2})   # C 데이터 없음
    check("데이터 없는 종목 제외 + n_used<n_total", part and part["n_used"] == 2 and part["n_total"] == 3, part)
    check("음수/0 비중 필터", dv.div_ratio({"A": -1.0, "B": 0.0}, {"A": _C1, "B": _C2}) is None)


def test_portfolio_dr():
    print("[DR] portfolio_dr — 페르소나별 마지막 positions 스냅샷 → DR (관찰 전용)")
    recs = [
        {"session": "2026-01-02", "persona": "oneil",
         "positions": [{"symbol": "AAA", "qty": 10, "avg": 100.0}]},           # 옛 스냅샷(덮임)
        {"session": "2026-01-05", "persona": "oneil",
         "positions": [{"symbol": "AAA", "qty": 10, "avg": 100.0},
                       {"symbol": "BBB", "qty": 20, "avg": 50.0}]},
        {"session": "2026-01-05", "persona": "wood", "positions": []},          # 보유 0 → 제외
        {"session": "2026-01-05", "persona": "buffett",
         "positions": [{"symbol": "CCC", "qty": 5, "avg": 10.0}]},              # 종가조회 실패 페르소나
    ]

    def cf(t, s):
        return {"AAA": _C1, "BBB": _C2}.get(t) or (_ for _ in ()).throw(RuntimeError("x"))

    m = sr.portfolio_dr(recs, closes_fn=cf)
    check("oneil DR 계산(마지막 스냅샷 2종목)", "oneil" in m and m["oneil"]["n_used"] == 2, m.get("oneil"))
    check("oneil 세션 = 마지막 positions 세션", m.get("oneil", {}).get("session") == "2026-01-05")
    check("보유 0 페르소나 제외", "wood" not in m, sorted(m))
    check("조회실패 페르소나 graceful 제외(throw 안 함)", "buffett" not in m, sorted(m))


def test_render_dr_section():
    print("[DR] 리포트 렌더 — dr_map 있으면 섹션 표기, 없으면 미표기")
    recs = _RECS + [{"session": "2026-01-05", "persona": "oneil",
                     "positions": [{"symbol": "AAA", "qty": 1, "avg": 100.0},
                                   {"symbol": "BBB", "qty": 1, "avg": 50.0}]}]
    md, _dims, meta = sr.run(horizon=3, recs=recs,
                             closes_fn=lambda t, s: {"AAA": _C1, "BBB": _C2}.get(t))
    check("DR 섹션 렌더 + meta['dr']", "분산비율" in md and "oneil" in meta.get("dr", {}), meta.get("dr"))
    md0, _d0, meta0 = sr.run(horizon=3, recs=_RECS, closes_fn=_cf)   # positions 없음 → DR 없음
    check("dr_map 비면 섹션 미표기", "분산비율" not in md0 and meta0.get("dr") == {})


# ── A12 — 일1런 vs 장중 간섭 계측 (관찰 전용) ────────────────────────────────
def test_intraday_interference():
    print("[A12] 일1런이 장중 취득 포지션을 되돌리는 빈도·규모 — 순수 사후 대조, 거래 로직 무접촉")
    recs = [
        # oneil: 장중이 AAA·BBB 를 보유한 채 마감 → 다음날 일1런이 AAA 매도(간섭) + CCC 매수(무관)
        {"session": "s1", "persona": "oneil", "intraday": True,
         "positions": [{"symbol": "AAA", "qty": 5}, {"symbol": "BBB", "qty": 3}]},
        {"session": "s2", "persona": "oneil", "selection": {"final": ["BBB", "CCC"]},
         "orders": [{"symbol": "AAA", "side": "SELL"}, {"symbol": "CCC", "side": "BUY"}]},
        # 다음 세션: 장중 보유 BBB 뿐인데 매도가 없음 → 간섭 아님(분모만 +1)
        {"session": "s2", "persona": "oneil", "intraday": True,
         "positions": [{"symbol": "BBB", "qty": 3}]},
        {"session": "s3", "persona": "oneil", "selection": {"final": ["BBB"]}, "orders": []},
        # buffett: intraday 없음(daily_run 뿐) → 시나리오 자체가 성립 안 함, 출력에서 제외돼야 함
        {"session": "s1", "persona": "buffett", "selection": {"final": ["ZZZ"]},
         "orders": [{"symbol": "ZZZ", "side": "SELL"}]},
    ]
    m = sr.intraday_interference(recs)
    check("oneil 만 계산됨(buffett 은 intraday 無 → 제외)", set(m) == {"oneil"}, sorted(m))
    o = m["oneil"]
    check("일1런 2건(s2·s3) 집계", o["n_daily"] == 2, o)
    check("간섭 1건(s2: AAA 매도 ∩ 장중보유)", o["n_interfered"] == 1, o)
    check("빈도 50%", abs(o["rate"] - 0.5) < 1e-9, o)
    check("간섭 종목 AAA×1 (CCC 매수는 무관, BBB 무매도는 비간섭)", o["tickers"] == {"AAA": 1}, o["tickers"])
    # 장중 스냅샷 자체가 없는 페르소나(레코드 0건)는 빈 dict — throw 없이 graceful
    check("빈 입력 → 빈 dict", sr.intraday_interference([]) == {})
    check("intraday 레코드 자체가 없는 페르소나만 있으면 빈 dict",
          sr.intraday_interference([{"session": "s1", "persona": "wood",
                                     "selection": {"final": ["X"]}, "orders": []}]) == {})


def test_render_interference_section():
    print("[A12] 리포트 렌더 — interference_map 있으면 섹션 표기, 없으면 미표기")
    recs = [
        {"session": "s1", "persona": "oneil", "intraday": True,
         "positions": [{"symbol": "AAA", "qty": 1}]},
        {"session": "s2", "persona": "oneil", "selection": {"final": []},
         "orders": [{"symbol": "AAA", "side": "SELL"}]},
    ]
    md, _dims, meta = sr.run(horizon=3, recs=recs, closes_fn=lambda t, s: None)
    check("A12 섹션 렌더 + meta['interference']",
          "일1런 vs 장중 간섭" in md and "oneil" in meta.get("interference", {}), meta.get("interference"))
    md0, _d0, meta0 = sr.run(horizon=3, recs=_RECS, closes_fn=_cf)   # intraday 레코드 없음 → 섹션 없음
    check("interference 없으면 섹션 미표기", "일1런 vs 장중 간섭" not in md0 and meta0.get("interference") == {})


def main():
    print("=" * 70)
    print(" selection_review 검증 — 네트워크 없음 (관측·리포트 전용)")
    print("=" * 70)
    print()
    for t in (test_load_picks, test_forward_return, test_evaluate_pending,
              test_bucketize, test_render_and_run, test_paper_included_by_default,
              test_persona_dimension, test_multihome_run, test_multihome_dedup,
              test_score_bucket_scale, test_entry_filter, test_score_bucket_nonfinite,
              test_div_ratio_math, test_portfolio_dr, test_render_dr_section,
              test_intraday_interference, test_render_interference_section):
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
