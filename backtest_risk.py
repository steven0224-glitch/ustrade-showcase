"""리스크 레이어 백테스트 — 베이스라인 vs 리스크관리 비교.

고정 파라미터 전략(기본 rs_momentum 126,3)에 레짐필터·변동성타겟·종목손절을
순차/조합 적용해 MDD 축소 효과를 정량 비교. 자산곡선 + 언더워터(낙폭) 차트.

예:
  python backtest_risk.py --universe diversified
  python backtest_risk.py --universe diversified --vol_target 0.15 --stop_loss 0.15
  python backtest_risk.py --universe tech --no-regime --stop_loss 0.10
"""
import argparse
import os
import sys

import data
import universe as uni
from strategies import RelativeStrengthMomentum
from engines import risk_runner
from engines._plot import plt


def main():
    ap = argparse.ArgumentParser(description="리스크 레이어 백테스트")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--top_n", type=int, default=3)
    ap.add_argument("--skip", type=int, default=21)
    ap.add_argument("--freq", default="M")
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    # 리스크 옵션
    ap.add_argument("--regime", dest="regime", action="store_true", default=True,
                    help="SPY 200MA 레짐필터 (기본 ON)")
    ap.add_argument("--no-regime", dest="regime", action="store_false")
    ap.add_argument("--regime_ma", type=int, default=200)
    ap.add_argument("--vol_target", type=float, default=0.15, help="연환산 목표변동성 (0=끄기)")
    ap.add_argument("--vol_lookback", type=int, default=20)
    ap.add_argument("--stop_loss", type=float, default=0.15, help="종목 손절폭 (0=끄기)")
    # 실현 슬리피지 모델 — 갭다운 손절이 nominal 보다 불리하게 체결되는 비용 가시화
    ap.add_argument("--spread", type=float, default=0.0005, help="호가 스프레드 (편도 spread/2 회전비용, 0=끄기)")
    ap.add_argument("--stop_gap", type=float, default=0.003, help="강제청산 갭 슬리피지 (손절·레짐OFF에만, 0=끄기)")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    tickers = uni.get_universe(args.universe)
    prices = data.load_panel(tickers, args.start, args.end, force=args.force)
    print(f"유니버스 {args.universe}: {prices.shape[1]}종목 × {prices.shape[0]}봉")

    strat = RelativeStrengthMomentum(lookback=args.lookback, skip=args.skip,
                                     top_n=args.top_n, freq=args.freq)
    weights = strat.generate_weights(prices)
    print(f"전략: {strat} / 리밸런스 {len(weights)}회")

    # SPY 레짐 (200MA 위 = 리스크온)
    regime = None
    if args.regime:
        spy = data.load("SPY", args.start, args.end, force=args.force)["Close"]
        spy = spy.reindex(prices.index).ffill()
        regime = spy > spy.rolling(args.regime_ma).mean()
        print(f"레짐: SPY {args.regime_ma}MA 위 = 리스크온 ({regime.mean():.0%} 기간 투자)")

    vt = args.vol_target if args.vol_target > 0 else None
    sl = args.stop_loss if args.stop_loss > 0 else None

    if args.spread or args.stop_gap:
        print(f"실현 슬리피지: 스프레드 {args.spread:.2%}(편도 {args.spread/2:.2%}) | "
              f"강제청산 갭 {args.stop_gap:.2%}")

    runs = {}
    # 베이스라인 (오버레이 전부 OFF) — 수익률은 vol 추정 레퍼런스로 재사용. 스프레드는 동일 부과(공정 비교).
    base_eq, base_pr, base_m0 = risk_runner.simulate(prices, weights, args.cash, args.fee,
                                                     spread=args.spread)
    runs["베이스라인"] = (base_eq, base_pr, base_m0)
    # 풀스택 (지정 옵션 전부)
    label_full = "리스크관리(" + "+".join(
        [x for x in [("레짐" if regime is not None else ""),
                     (f"vol{vt}" if vt else ""), (f"손절{sl}" if sl else "")] if x]) + ")"
    runs[label_full] = risk_runner.simulate(
        prices, weights, args.cash, args.fee,
        regime=regime, vol_target=vt, vol_lookback=args.vol_lookback,
        max_leverage=1.0, stop_loss=sl, ref_ret=base_pr,
        spread=args.spread, stop_gap=args.stop_gap)

    print("\n" + "=" * 72)
    for name, (eq, pr, m) in runs.items():
        gross = f" | 평균노출 {m['avg_gross']:.0%}" if "avg_gross" in m else ""
        print(f"  {name:24s}: CAGR {m['cagr']:+7.2%} | Sharpe {m['sharpe']:.2f} | "
              f"MDD {m['max_drawdown']:7.2%}{gross}")
    base_m = runs["베이스라인"][2]
    full_m = runs[label_full][2]
    dd_cut = full_m["max_drawdown"] - base_m["max_drawdown"]
    print("-" * 72)
    print(f"  MDD 변화: {base_m['max_drawdown']:.2%} → {full_m['max_drawdown']:.2%} "
          f"({dd_cut:+.2%}p) | Sharpe {base_m['sharpe']:.2f} → {full_m['sharpe']:.2f}")
    print("=" * 72)

    # 차트: 자산곡선 + 언더워터 ---------------------------------------
    os.makedirs("results", exist_ok=True)
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1], sharex=True)
    for name, (eq, pr, m) in runs.items():
        ax1.plot(eq.index, eq.values, label=name, lw=1.6)
        ax2.fill_between(eq.index, risk_runner.underwater(eq).values * 100, 0, alpha=0.4)
    ax1.set_yscale("log")
    ax1.set_ylabel("Equity (log)")
    ax1.set_title(f"{args.universe} — 리스크 레이어 효과 (전략 고정 {args.lookback},{args.top_n})")
    ax1.legend()
    ax1.grid(alpha=0.3)
    ax2.set_ylabel("낙폭 %")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    path = os.path.join("results", f"risk_{args.universe}.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"\n차트: {path}")


if __name__ == "__main__":
    sys.exit(main())
