"""라이브 리밸런스 CLI (데모) — live_engine.run_once 를 PaperBroker 로 실행·출력.

전체 무인 경로: 데이터 → 선택(모멘텀+펀더멘털) → 리스크(레짐+vol) → 킬스위치 → 체결.
운영 스케줄 실행은 run_live.py (저널·알림 포함). 로직은 live_engine 공유.

  python live_rebalance.py --universe diversified --top_n 3 --vol_target 0.20
  python live_rebalance.py --top_n 1            # 포지션바운드 트립 데모
  python live_rebalance.py --reset-halt         # 정지 해제
"""
import argparse
import sys

import data
import universe as uni
from live_engine import RunConfig, run_once
from broker import PaperBroker


def main():
    ap = argparse.ArgumentParser(description="라이브 리밸런스 데모 (PaperBroker)")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--lookback", type=int, default=126)
    ap.add_argument("--top_n", type=int, default=3)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--min_margin", type=float, default=0.0)
    ap.add_argument("--max_pe", type=float, default=80.0)
    ap.add_argument("--vol_target", type=float, default=0.20)
    ap.add_argument("--regime_ma", type=int, default=200)
    ap.add_argument("--cash", type=float, default=100000.0)
    ap.add_argument("--today", default="")
    ap.add_argument("--reset-halt", dest="reset_halt", action="store_true")
    args = ap.parse_args()

    prices = data.load_panel(uni.get_universe(args.universe), "2022-01-01", "2025-01-01")
    snap_px = {s: float(prices[s].iloc[-1]) for s in prices.columns}
    broker = PaperBroker(cash=args.cash, price_fn=lambda s: snap_px[s], commission=0.0005)

    cfg = RunConfig(universe=args.universe, lookback=args.lookback, top_n=args.top_n,
                    pool=args.pool, min_margin=args.min_margin, max_pe=args.max_pe,
                    vol_target=args.vol_target, regime_ma=args.regime_ma)
    # 데모는 데이터 종료일(과거)을 기준세션으로 — staleness 가드가 데모를 막지 않도록
    today = args.today or prices.index[-1].strftime("%Y-%m-%d")
    res = run_once(prices, broker, cfg, today=today,
                   reset_halt=args.reset_halt, force=True)   # 데모: 당일 중복실행 락 우회

    st = res["status"]
    if st == "halted":
        print(f"🛑 거래 정지: {res['reason']}\n   해제: --reset-halt 또는 state/HALT 삭제")
        return
    if st == "already_ran":
        print(f"⏭  {res['reason']}")
        return
    if st in ("tripped", "error", "partial", "stale"):
        icon = "🛑" if st == "tripped" else "⚠️"
        print(f"{icon} {st}: {res['reason']}")
        for o in res.get("orders", []):
            print(f"  {'✓' if o['status']=='FILLED' else '✗'} {o['side']:4s} {o['symbol']:6s} "
                  f"{o['qty']:>4.0f}주 [{o['status']}] {o['message']}")
        if "selection" in res:
            print(f"   선택: {res['selection']['final']}")
        return

    sel, risk = res["selection"], res["risk"]
    print(f"모멘텀 후보 {sel['candidates']}")
    for t, why in sel["fails"].items():
        print(f"  ✗ {t}: {why}")
    if sel["missing"]:
        print(f"데이터 결측(플래그): {sel['missing']}")
    print(f"모멘텀만 top → {sel['momentum_only']} / 필터 후 → {sel['final']}")
    if risk:
        print(f"리스크: 레짐 {risk['regime']} | 노출 {risk.get('scale', 1):.0%}")
    print(f"\n=== 체결 (가드 통과, 당일손익 {res['daily_pnl']:+.2%}) ===")
    for o in res["orders"]:
        print(f"  {'✓' if o['status']=='FILLED' else '✗'} {o['side']:4s} {o['symbol']:6s} "
              f"{o['qty']:>4.0f}주 @ {o['fill']:,.2f} [{o['status']}] {o['message']}")
    a = res["account"]
    print(f"\n계좌: 현금 {a['cash']:,.0f} | 총자산 {a['equity']:,.0f}")
    print("✅ 무인 경로 (선택→리스크→킬스위치→체결).")


if __name__ == "__main__":
    sys.exit(main())
