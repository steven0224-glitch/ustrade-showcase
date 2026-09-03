"""risk_runner 실현 슬리피지 모델 검증 — 네트워크 0 (합성 가격).

핵심 단언: (1) 기본값(spread=0,stop_gap=0)은 기존 동작과 동일(불변), (2) 갭 슬리피지가
강제청산 비용을 실제로 늘려 최종자산을 낮춘다(갭다운 손절 리스크 가시화).

실행:  python tests_slippage.py
"""
import numpy as np
import pandas as pd

from engines import risk_runner

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _scenario():
    """A 는 3일차 -20% 갭(손절 트리거), B 는 평탄. 0일차 리밸런스로 반반 진입."""
    idx = pd.bdate_range("2020-01-01", periods=8)
    A = [100, 100, 100, 80, 80, 80, 80, 80]
    B = [100] * 8
    prices = pd.DataFrame({"A": A, "B": B}, index=idx, dtype=float)
    tw = pd.DataFrame({"A": [0.5], "B": [0.5]}, index=[idx[0]])
    return prices, tw


def test_defaults_unchanged():
    print("[SLIP] 기본값(spread=0,stop_gap=0) = 명시적 0 (기존 동작 불변)")
    prices, tw = _scenario()
    eq_def, _, _ = risk_runner.simulate(prices, tw, stop_loss=0.1)
    eq_zero, _, _ = risk_runner.simulate(prices, tw, stop_loss=0.1, spread=0.0, stop_gap=0.0)
    check("기본 == 명시적 0", np.allclose(eq_def.values, eq_zero.values))


def test_gap_increases_cost():
    print("[SLIP] 갭 슬리피지 → 강제청산 비용↑ → 최종자산↓")
    prices, tw = _scenario()
    eq_no, _, _ = risk_runner.simulate(prices, tw, stop_loss=0.1, stop_gap=0.0)
    eq_gap, _, _ = risk_runner.simulate(prices, tw, stop_loss=0.1, stop_gap=0.05)
    check("stop_gap 적용 시 최종자산 더 낮음",
          eq_gap.iloc[-1] < eq_no.iloc[-1], f"{eq_gap.iloc[-1]:.2f} vs {eq_no.iloc[-1]:.2f}")
    # 손절이 실제 발동했는지(비용차가 존재)
    check("비용차 유의(손절 발동 확인)", (eq_no.iloc[-1] - eq_gap.iloc[-1]) > 0.01)


def test_spread_on_turnover():
    print("[SLIP] 스프레드 → 리밸런스 회전에도 비용 부과")
    prices, tw = _scenario()
    eq_no, _, _ = risk_runner.simulate(prices, tw)                    # 오버레이 없음
    eq_sp, _, _ = risk_runner.simulate(prices, tw, spread=0.01)
    check("spread 적용 시 최종자산 더 낮음", eq_sp.iloc[-1] < eq_no.iloc[-1],
          f"{eq_sp.iloc[-1]:.2f} vs {eq_no.iloc[-1]:.2f}")


def main():
    print("=" * 60)
    print("risk_runner 실현 슬리피지 모델")
    print("=" * 60)
    test_defaults_unchanged()
    test_gap_increases_cost()
    test_spread_on_turnover()
    print("-" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("실패:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
