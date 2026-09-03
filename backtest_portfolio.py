"""포트폴리오(다종목) 백테스트 CLI.

예:
  python backtest_portfolio.py --universe diversified --strategy rs_momentum
  python backtest_portfolio.py --universe tech --strategy rs_momentum --top_n 3 --lookback 126
  python backtest_portfolio.py --universe AAPL,MSFT,NVDA,AMD --top_n 2 --freq M
"""
import argparse
import sys

import data
import universe as uni
from strategies import get_portfolio_strategy, PORTFOLIO_REGISTRY
from engines import metrics, portfolio_runner

PARAM_TYPES = {"lookback": int, "skip": int, "top_n": int, "freq": str}


def parse_extra(unknown):
    params, i = {}, 0
    while i < len(unknown):
        tok = unknown[i]
        if tok.startswith("--"):
            key = tok[2:]
            val = unknown[i + 1] if i + 1 < len(unknown) else None
            params[key] = PARAM_TYPES.get(key, str)(val)
            i += 2
        else:
            i += 1
    return params


def main():
    ap = argparse.ArgumentParser(description="미국주식 포트폴리오 백테스트")
    ap.add_argument("--universe", default="diversified",
                    help="megacap|tech|diversified | CSV경로 | 콤마구분 티커")
    ap.add_argument("--strategy", default="rs_momentum", choices=list(PORTFOLIO_REGISTRY))
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--benchmark", default="SPY", help="벤치마크 티커 (none=끄기)")
    ap.add_argument("--force", action="store_true")
    args, unknown = ap.parse_known_args()
    strat_params = parse_extra(unknown)

    tickers = uni.get_universe(args.universe)
    print(f"유니버스: {args.universe} ({len(tickers)}종목)")
    prices = data.load_panel(tickers, args.start, args.end, force=args.force)
    print(f"  패널 {prices.shape[0]}봉 × {prices.shape[1]}종목 ({args.start}~{args.end})")

    strat = get_portfolio_strategy(args.strategy, **strat_params)
    print(f"전략: {strat}")
    weights = strat.generate_weights(prices)
    print(f"  리밸런스 {len(weights)}회\n")

    bench = None
    if args.benchmark.lower() != "none":
        try:
            bench = data.load(args.benchmark, args.start, args.end, force=args.force)["Close"]
            bench.name = args.benchmark
        except Exception as e:
            print(f"(벤치마크 {args.benchmark} 스킵: {e})")

    label = f"{args.universe}_{args.strategy}"
    m = portfolio_runner.run(prices, weights, cash=args.cash, fee=args.fee,
                             label=label, benchmark_prices=bench)

    print(metrics.format_report(m, label=f"portfolio | {label}"))
    print(f"  리밸런스   : {m['n_rebalances']}회")
    print(f"  동일비중   : {m['benchmark_ew_final']:,.0f} (최종자산)")
    if "plot" in m:
        print(f"  차트       : {m['plot']}")


if __name__ == "__main__":
    sys.exit(main())
