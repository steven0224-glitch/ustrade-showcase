"""Stage 6 (U3 — 백테스트 엔진 정합) 검증 — 네트워크 불필요.

simple 과 vbt 엔진은 동일 신호·체결모델(신호봉 종가 결정→익봉 수익)이라 결과가 일치해야
한다. 이 테스트로 향후 한쪽이 바뀌어 분기(예: 룩어헤드 유입)하는 것을 잡는다.
(backtrader 는 익일 시가 체결로 의도적으로 약간 보수적 — 정합 대상서 제외.)
실행:  & $py tests_stage6.py
"""
import sys

import numpy as np
import pandas as pd

# vectorbt 는 numba 필요, numba 는 numpy<2.5 하드캡 — pylibs 섀도잉(numpy 2.5) 환경이면
# import 불가. 그 경우 명시적 SKIP (묵음 PASS 금지). 실실행은 순수 venv(PYTHONPATH 비움)로.
try:
    from engines import simple_runner, vbt_runner
    _VBT_ERR = None
except ImportError as _e:
    from engines import simple_runner
    vbt_runner, _VBT_ERR = None, _e
    if "pytest" in sys.modules:  # test_suites 경유
        import pytest
        pytest.skip(f"vectorbt 사용 불가: {_e} — 순수 venv($env:PYTHONPATH='')로 실행",
                    allow_module_level=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _fixture(seed):
    np.random.seed(seed)
    n = 400
    rets = np.random.normal(0.0005, 0.02, n)
    close = 100 * np.exp(np.cumsum(rets))
    idx = pd.bdate_range("2020-01-01", periods=n)
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": 1e6}, index=idx)
    c = pd.Series(close, index=idx)
    fast, slow = c.rolling(10).mean(), c.rolling(30).mean()
    entry = (fast > slow) & (fast.shift(1) <= slow.shift(1))
    exit_ = (fast < slow) & (fast.shift(1) >= slow.shift(1))
    sig = pd.DataFrame({"entry": entry.fillna(False), "exit": exit_.fillna(False)})
    return df, sig


def test_simple_vbt_parity():
    print("[Stage6/U3] simple ↔ vbt 엔진 결과 일치 (분기·룩어헤드 회귀 방지)")
    for seed in (7, 21, 99):
        df, sig = _fixture(seed)
        ms = simple_runner.run(df, sig, plot=False)
        mv = vbt_runner.run(df, sig, plot=False)
        check(f"[seed {seed}] n_trades 일치", ms["n_trades"] == mv["n_trades"],
              f"{ms['n_trades']} vs {mv['n_trades']}")
        check(f"[seed {seed}] total_return ±0.5%p",
              abs(ms["total_return"] - mv["total_return"]) < 0.005,
              f"{ms['total_return']:.4f} vs {mv['total_return']:.4f}")
        check(f"[seed {seed}] sharpe ±0.02",
              abs(ms["sharpe"] - mv["sharpe"]) < 0.02,
              f"{ms['sharpe']:.4f} vs {mv['sharpe']:.4f}")
        rel = abs(ms["final_equity"] - mv["final_equity"]) / ms["final_equity"]
        check(f"[seed {seed}] final_equity ±0.5%", rel < 0.005, f"rel={rel:.5f}")


def main():
    if _VBT_ERR is not None:
        print(f"SKIP: vectorbt 사용 불가 — {_VBT_ERR}")
        print("      순수 venv 필요: $env:PYTHONPATH=''; & $py tests\\tests_stage6.py")
        return 0
    print("=" * 70)
    print(" Stage 6 (U3 엔진 정합) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    test_simple_vbt_parity()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
