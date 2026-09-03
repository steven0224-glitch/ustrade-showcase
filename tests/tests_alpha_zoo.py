"""alpha_zoo + ops + 랜덤컨트롤 게이트 검증 — 네트워크 0 (합성 패널).

핵심 단언: 랜덤컨트롤 strict 게이트가 '진짜 예측력'과 '노이즈'를 구별한다
(정보팩터→confirmed_alive, 순수노이즈→noise). 이게 이식(item 1·2)의 존재이유.

실행:  python tests_alpha_zoo.py
"""
import numpy as np
import pandas as pd

from strategies import ops
from strategies import alpha_zoo as Z
import eval_factor as E

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _synth_panel(n=300, m=10, seed=0):
    """양의 랜덤워크 OHLCV 패널 dict (open/high/low/close/volume/vwap)."""
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2018-01-01", periods=n)
    cols = [f"T{i:02d}" for i in range(m)]
    steps = rng.normal(0, 0.02, size=(n, m))
    close = pd.DataFrame(100.0 * np.exp(np.cumsum(steps, axis=0)), index=idx, columns=cols)
    open_ = close.shift(1).fillna(close.iloc[0])
    hi_base = np.maximum(open_, close)
    lo_base = np.minimum(open_, close)
    high = hi_base * (1 + np.abs(rng.normal(0, 0.005, (n, m))))
    low = lo_base * (1 - np.abs(rng.normal(0, 0.005, (n, m))))
    volume = pd.DataFrame(rng.integers(1e5, 1e7, size=(n, m)).astype(float), index=idx, columns=cols)
    vwap = (open_ + high + low + close) / 4.0
    return {"open": open_, "high": high, "low": low, "close": close, "volume": volume, "vwap": vwap}


def test_ops_invariants():
    print("[OPS] look-ahead 금지 + NaN 정책")
    df = pd.DataFrame(np.arange(20.0).reshape(5, 4))
    raised = False
    try:
        ops.delta(df, 0)
    except ValueError:
        raised = True
    check("delta(d=0) → ValueError (look-ahead 금지)", raised)
    raised = False
    try:
        ops.delay(df, 0)
    except ValueError:
        raised = True
    check("delay(d=0) → ValueError", raised)

    r = ops.rank(df)
    check("rank ∈ [0,1]", float(r.min().min()) >= 0 and float(r.max().max()) <= 1)

    z = pd.DataFrame([[1.0, 0.0]])
    sd = ops.safe_div(pd.DataFrame([[1.0, 1.0]]), z)
    check("safe_div by 0 → NaN (inf 아님)", bool(pd.isna(sd.iloc[0, 1])) and np.isfinite(sd.iloc[0, 0]))


def test_all_alphas_compute():
    print("[ZOO] 전 알파 계산 — shape 일치, inf 없음, 워밍업후 유한값 존재")
    panel = _synth_panel()
    close = panel["close"]
    ok = True
    for name, fn in Z.ALPHA_REGISTRY.items():
        try:
            out = fn(panel)
        except Exception as e:
            check(f"{name} 계산", False, f"예외 {e}")
            ok = False
            continue
        shape_ok = out.shape == close.shape
        no_inf = not np.isinf(out.to_numpy(dtype=float, na_value=np.nan)).any()
        has_finite = bool(np.isfinite(out.to_numpy(dtype=float, na_value=np.nan)).any())
        if not (shape_ok and no_inf and has_finite):
            check(f"{name}", False, f"shape={shape_ok} noinf={no_inf} finite={has_finite}")
            ok = False
    check(f"전 {len(Z.ALPHA_REGISTRY)}개 알파 계산 정상", ok)


def test_shuffle_preserves_distribution():
    print("[NULL] 행내 셔플 — 유한값 multiset 보존, NaN 위치 고정")
    df = pd.DataFrame([[1.0, 2.0, np.nan, 4.0], [np.nan, np.nan, 3.0, 9.0]])
    sh = E._shuffle_within_rows(df, seed=7)
    row0_same = sorted(df.iloc[0].dropna()) == sorted(sh.iloc[0].dropna())
    nan_fixed = bool(pd.isna(sh.iloc[0, 2])) and bool(pd.isna(sh.iloc[1, 0]))
    check("행0 유한값 multiset 보존", row0_same)
    check("NaN 위치 고정", nan_fixed)
    lt2 = df.iloc[1].copy(); lt2[:] = [np.nan, np.nan, np.nan, 5.0]
    dfx = pd.DataFrame([lt2.values])
    shx = E._shuffle_within_rows(dfx, seed=1)
    check("유한값<2 행은 무변경", float(shx.iloc[0, 3]) == 5.0)
    # object(pd.NA) 팩터도 float null 로 강제 — rsi/bollinger dtype 크래시 회귀 방지
    obj = pd.DataFrame([[1.0, 2.0, pd.NA, 4.0]], dtype=object)
    sh_obj = E._shuffle_within_rows(obj, seed=3)
    check("object(pd.NA) 팩터 셔플 크래시 없음·유한값 보존",
          sorted(float(x) for x in sh_obj.iloc[0].dropna()) == [1.0, 2.0, 4.0])


def test_random_control_keyword_only():
    print("[GATE] random_control 은 keyword-only-no-default (생략=TypeError)")
    fac = pd.DataFrame(np.random.default_rng(0).normal(size=(60, 8)))
    fwd = pd.DataFrame(np.random.default_rng(1).normal(size=(60, 8)))
    raised = False
    try:
        E.strict_summary(fac, fwd, 1)   # random_control 생략
    except TypeError:
        raised = True
    check("random_control 생략 → TypeError", raised)


def test_gate_discriminates():
    print("[GATE] 정보팩터→confirmed_alive, 노이즈→noise (게이트 핵심)")
    n, m = 700, 15
    idx = pd.bdate_range("2016-01-01", periods=n)
    cols = [f"T{i:02d}" for i in range(m)]
    rng = np.random.default_rng(123)
    fwd = pd.DataFrame(rng.normal(size=(n, m)), index=idx, columns=cols)
    # 정보팩터: 미래수익 + 소음 (횡단면 IC 높음). 노이즈팩터: 독립난수.
    informative = fwd + rng.normal(0, 0.5, size=(n, m))
    noise = pd.DataFrame(rng.normal(size=(n, m)), index=idx, columns=cols)

    s_inf = E.strict_summary(informative, fwd, E.PRIMARY, random_control=True, n_seeds=5)
    s_noise = E.strict_summary(noise, fwd, E.PRIMARY, random_control=True, n_seeds=5)
    v_inf = E.strict_verdict(s_inf)
    v_noise = E.strict_verdict(s_noise)
    check("정보팩터 confirmed_alive", "confirmed" in v_inf, f"α_t={s_inf['alpha_t']:.2f} → {v_inf}")
    check("정보팩터 randIC≈0 (셔플이 신호 파괴)", abs(s_inf["random_ic_mean"]) < 0.05, s_inf["random_ic_mean"])
    check("노이즈팩터 confirmed 아님", "confirmed" not in v_noise, f"α_t={s_noise['alpha_t']:.2f} → {v_noise}")


def test_factor_panel_zoo_alignment():
    print("[EVAL] factor_panel(zoo명, panel_dict) → prices 정렬 프레임")
    panel = _synth_panel(n=260, m=8)
    prices = panel["close"]
    fac, comp = E.factor_panel("alpha101_101", prices, "", list(prices.columns), panel)
    check("prices 인덱스/컬럼 정렬", fac.shape == prices.shape and list(fac.columns) == list(prices.columns))
    check("comp 라벨", comp == ["alpha101_101"])
    raised = False
    try:
        E.factor_panel("alpha101_101", prices, "", list(prices.columns))   # panel_dict 누락
    except ValueError:
        raised = True
    check("panel_dict 누락 → ValueError", raised)


def test_alpha_tilt_wiring():
    print("[TILT] live_select use_alpha — 재랭킹/폴백/기본불변 (네트워크 0)")
    import fmp_factors as ff
    import live_select as ls
    from strategies import alpha_zoo as Z
    from strategies import factors as F
    panel = _synth_panel(n=300, m=8, seed=3)
    prices = panel["close"]
    ohlcv_fn = lambda tickers, start, end, **k: panel   # 주입 훅(다운로드 대체)

    # FMP 경로 스텁 — 네트워크/키 불요, 후보순서만 검증
    orig_snap, orig_screen = ff.snapshot, ff.screen
    ff.snapshot = lambda *a, **k: __import__("pandas").DataFrame()
    ff.screen = lambda snap, **k: ([], {})
    try:
        mom = F.momentum(prices, lookback=126, skip=21).iloc[-1].dropna().sort_values(ascending=False)
        pool = list(mom.head(8).index)

        # use_alpha=False → 순수 모멘텀 순서(결정론 불변)
        _w0, i0 = ls.select(prices, pool=8, top_n=3, use_alpha=False)
        check("off: 후보=순수 모멘텀", i0["candidates"] == pool)
        check("off: alpha_used False", i0["alpha_used"] is False)

        # use_alpha=True, weight=1.0 → 순수 alpha 랭킹으로 재정렬
        ac = Z.compute("alpha101_032", panel).iloc[-1].reindex(pool)
        expected = list(ac.dropna().sort_values(ascending=False).index)
        _w1, i1 = ls.select(prices, pool=8, top_n=3, use_alpha=True, alpha_weight=1.0, ohlcv_fn=ohlcv_fn)
        check("on: 후보=alpha 랭킹", i1["candidates"] == expected, f"{i1['candidates']} vs {expected}")
        check("on: alpha_used True", i1["alpha_used"] is True)
        check("on: 재정렬 실제 발생(모멘텀≠alpha)", i1["candidates"] != pool)

        # ohlcv_fn 실패 → 모멘텀 폴백, throw 안 함
        boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("net down"))
        _w2, i2 = ls.select(prices, pool=8, top_n=3, use_alpha=True, ohlcv_fn=boom)
        check("실패: 모멘텀 폴백", i2["candidates"] == pool and i2["alpha_used"] is False)
    finally:
        ff.snapshot, ff.screen = orig_snap, orig_screen


def main():
    print("=" * 60)
    print("alpha_zoo / ops / 랜덤컨트롤 게이트")
    print("=" * 60)
    test_ops_invariants()
    test_all_alphas_compute()
    test_shuffle_preserves_distribution()
    test_random_control_keyword_only()
    test_gate_discriminates()
    test_factor_panel_zoo_alignment()
    test_alpha_tilt_wiring()
    print("-" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("실패:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
