"""라이브 경로 dry-run — rs_momentum 목표비중 → PaperBroker 리밸런스 end-to-end.

토스 없이 전체 체결 파이프라인 검증. 토스 발급 시 PaperBroker→TossBroker 교체만.
연속 두 리밸런스일로 turnover(매도+매수 diff) 동작 확인.

  python live_demo.py --universe diversified --lookback 126 --top_n 3
"""
import argparse
import sys

import data
import universe as uni
from strategies import RelativeStrengthMomentum
from broker import PaperBroker, Executor, OrderStatus


def target_from_row(row):
    return {s: float(w) for s, w in row.items() if w > 0}


def show(broker, title):
    acct = broker.get_account()
    print(f"  [{title}] 현금 {acct.cash:,.0f} | 총자산 {acct.equity:,.0f}")
    for p in broker.get_positions():
        px = broker.get_quote(p.symbol).last
        print(f"     {p.symbol:6s} {p.qty:>4.0f}주 @ {p.avg_price:,.2f} (현재 {px:,.2f}, 평가 {p.qty*px:,.0f})")


def main():
    ap = argparse.ArgumentParser(description="라이브 경로 dry-run (PaperBroker)")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--top_n", type=int, default=3)
    ap.add_argument("--cash", type=float, default=100000.0)
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2025-01-01")
    args = ap.parse_args()

    prices = data.load_panel(uni.get_universe(args.universe), args.start, args.end)
    weights = RelativeStrengthMomentum(lookback=args.lookback, top_n=args.top_n).generate_weights(prices)
    d1, d2 = weights.index[-2], weights.index[-1]
    t1, t2 = target_from_row(weights.loc[d1]), target_from_row(weights.loc[d2])

    # 가변 시세 스냅샷 (리밸런스일별 종가)
    snap = {}
    broker = PaperBroker(cash=args.cash, price_fn=lambda s: snap[s], commission=0.0005)
    exe = Executor(broker, alloc=0.95)

    print(f"유니버스 {args.universe} | 전략 rs_momentum({args.lookback},{args.top_n})\n")

    # 1차 리밸런스 (d1)
    print(f"=== 1차 리밸런스 {d1.date()} → {list(t1)} ===")
    snap.update({s: float(prices.loc[d1, s]) for s in set(t1) | set(p.symbol for p in broker.get_positions())})
    orders = exe.rebalance(t1)
    for o in orders:
        tag = "✓" if o.status == OrderStatus.FILLED else "✗"
        print(f"  {tag} {o.request.side.value:4s} {o.request.symbol:6s} {o.request.qty:>4.0f}주 "
              f"@ {o.avg_fill_price:,.2f} [{o.status.value}] {o.message}")
    show(broker, "체결후")

    # 2차 리밸런스 (d2) — 보유 갱신 위해 d2 시세로
    print(f"\n=== 2차 리밸런스 {d2.date()} → {list(t2)} ===")
    held = set(p.symbol for p in broker.get_positions())
    snap.update({s: float(prices.loc[d2, s]) for s in set(t2) | held})
    orders = exe.rebalance(t2)
    if not orders:
        print("  (변경 없음 — 목표=현보유)")
    for o in orders:
        tag = "✓" if o.status == OrderStatus.FILLED else "✗"
        print(f"  {tag} {o.request.side.value:4s} {o.request.symbol:6s} {o.request.qty:>4.0f}주 "
              f"@ {o.avg_fill_price:,.2f} [{o.status.value}] {o.message}")
    show(broker, "체결후")

    print("\n✅ 라이브 경로 검증 완료 (PaperBroker). 토스 발급 시 TossBroker 로 교체만.")


if __name__ == "__main__":
    sys.exit(main())
