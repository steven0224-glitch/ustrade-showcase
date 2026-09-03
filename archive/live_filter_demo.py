"""라이브 펀더멘털 필터 데모 — 모멘텀 top 후보 × FMP 무료 품질/가치 스크린.

무인 라이브 선택 보강: 모멘텀 상위 N개 후보 중 펀더멘털 불량(적자·고PE) 제거,
나머지에서 최종 보유 선택. FMP 무료티어(현재 스냅샷)의 현실적 활용.

⚠️ 라이브 틸트 전용 — 과거 펀더멘털은 유료라 historical 백테스트 불가.

  python live_filter_demo.py --universe diversified --candidates 8 --final 3
"""
import argparse
import sys

import data
import universe as uni
from strategies import factors as F
import fmp_factors as ff


def main():
    ap = argparse.ArgumentParser(description="라이브 펀더멘털 필터 데모")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--candidates", type=int, default=8, help="모멘텀 상위 후보 수")
    ap.add_argument("--final", type=int, default=3, help="필터 후 최종 보유 수")
    ap.add_argument("--min_margin", type=float, default=0.0)
    ap.add_argument("--max_pe", type=float, default=80.0)
    ap.add_argument("--min_market_cap", type=float, default=None, help="시총 하한 USD (예 1e10=$10B)")
    ap.add_argument("--max_market_cap", type=float, default=None, help="시총 상한 USD")
    args = ap.parse_args()

    tickers = uni.get_universe(args.universe)
    prices = data.load_panel(tickers, "2022-01-01", "2025-01-01")

    # 최신 모멘텀 랭킹 → 상위 후보
    mom = F.momentum_6_1(prices).iloc[-1].dropna().sort_values(ascending=False)
    cand = list(mom.head(args.candidates).index)
    print(f"모멘텀 상위 {args.candidates} 후보: {cand}\n")

    snap = ff.snapshot(cand)
    passed, fails = ff.screen(snap, min_net_margin=args.min_margin, max_pe=args.max_pe,
                              min_market_cap=args.min_market_cap, max_market_cap=args.max_market_cap)
    qv = ff.quality_value_score(snap)

    print("펀더멘털 스냅샷:")
    print(snap[["pe", "pb", "net_margin", "debt_equity", "earnings_yield"]].round(3).to_string())
    print(f"\n탈락 ({len(fails)}):")
    for t, why in fails.items():
        print(f"  ✗ {t}: {why}")

    # 최종 = 모멘텀 순서 유지하며 통과 종목 중 상위 final
    final = [t for t in cand if t in passed][:args.final]
    print(f"\n모멘텀만 top{args.final}      : {cand[:args.final]}")
    print(f"펀더멘털 필터 후 top{args.final}: {final}")
    diff = set(cand[:args.final]) - set(final)
    if diff:
        print(f"  → 필터가 제거: {sorted(diff)} (펀더멘털 불량)")
    else:
        print("  → 변경 없음 (top 후보가 펀더멘털도 통과)")

    print("\n품질/가치 점수 순위 (높을수록 우량):")
    for t, v in qv.head(args.candidates).items():
        mark = "✓" if t in passed else "✗"
        print(f"  {mark} {t}: {v:+.2f}")


if __name__ == "__main__":
    sys.exit(main())
