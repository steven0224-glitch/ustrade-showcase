"""백테스트 CLI — 엔진 × 전략 × 종목 조합 실행.

예:
  python backtest.py --ticker AAPL --strategy ma_cross --engine simple
  python backtest.py --ticker NVDA --strategy momentum --engine all --start 2018-01-01
  python backtest.py --ticker MSFT --strategy vcp --engine backtrader --fast 10 --slow 30

전략 파라미터는 --key value 형태로 자유롭게 전달 (해당 전략 생성자에 매핑).
"""
import argparse
import sys

import data
from strategies import get_strategy, REGISTRY
from engines import metrics, simple_runner, bt_runner

ENGINES = {
    "simple": simple_runner.run,
    "backtrader": bt_runner.run,
}


def get_vbt_runner():
    """vectorbt 는 무거우므로 필요할 때만 import."""
    from engines import vbt_runner
    return vbt_runner.run


# 전략별 파라미터 타입 힌트 (CLI 문자열 → 적절 타입 캐스팅)
PARAM_TYPES = {
    "fast": int, "slow": int,
    "lookback": int, "threshold": float,
    "base": int, "pivot": int, "vol_mult": float,
}


def parse_extra(unknown):
    params = {}
    i = 0
    while i < len(unknown):
        tok = unknown[i]
        if tok.startswith("--"):
            key = tok[2:]
            val = unknown[i + 1] if i + 1 < len(unknown) else None
            caster = PARAM_TYPES.get(key, str)
            params[key] = caster(val)
            i += 2
        else:
            i += 1
    return params


def main():
    ap = argparse.ArgumentParser(description="미국주식 백테스트")
    ap.add_argument("--ticker", required=True)
    ap.add_argument("--strategy", required=True, choices=list(REGISTRY))
    ap.add_argument("--engine", default="simple", choices=["simple", "backtrader", "vectorbt", "all"])
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--force", action="store_true", help="캐시 무시하고 재다운로드")
    args, unknown = ap.parse_known_args()

    strat_params = parse_extra(unknown)

    print(f"데이터 로드: {args.ticker} {args.start}~{args.end}")
    df = data.load(args.ticker, args.start, args.end, force=args.force)
    print(f"  {len(df)} 봉")

    strat = get_strategy(args.strategy, **strat_params)
    print(f"전략: {strat}")
    signals = strat.generate_signals(df)
    print(f"  entry {int(signals['entry'].sum())}건 / exit {int(signals['exit'].sum())}건\n")

    label = f"{args.ticker}_{args.strategy}"

    if args.engine == "all":
        runners = [("simple", simple_runner.run), ("backtrader", bt_runner.run)]
        try:
            runners.append(("vectorbt", get_vbt_runner()))
        except Exception as e:
            print(f"(vectorbt 건너뜀: {e})\n")
    elif args.engine == "vectorbt":
        runners = [("vectorbt", get_vbt_runner())]
    else:
        runners = [(args.engine, ENGINES[args.engine])]

    for ename, runner in runners:
        try:
            m = runner(df, signals, cash=args.cash, fee=args.fee, label=label)
            print(metrics.format_report(m, label=f"{ename} | {label}"))
            if "plot" in m:
                print(f"  차트       : {m['plot']}")
            print()
        except Exception as e:
            print(f"[{ename}] 실패: {e}\n")


if __name__ == "__main__":
    sys.exit(main())
