"""워크포워드 분석 (Walk-Forward Optimization).

롤링: train_years 로 best 파라미터 선택 → 다음 test_years 구간에 적용 → 전진.
모든 OOS 구간을 끊김 없이 이어붙여 하나의 연속 out-of-sample 곡선 생성.

핵심 비교:
  WFO (매 구간 재최적화) vs 고정 robust 파라미터 vs 동일비중 vs SPY
  → 재최적화가 실제 이득인가, 아니면 노이즈를 쫓는가?
     (흔히 고정 sensible 파라미터가 WFO 를 이김 = 과최적화 경고)

예:
  python walkforward.py --universe diversified
  python walkforward.py --universe tech --train_years 3 --test_years 1 --fixed_lookback 126 --fixed_top_n 3
"""
import argparse
import itertools
import os
import sys

import numpy as np
import pandas as pd

import data
import universe as uni
from strategies import RelativeStrengthMomentum
from engines import metrics
from engines.portfolio_runner import _simulate
from engines._plot import plt


def combo_weights(prices, lookbacks, top_ns, skip, freq):
    """모든 조합의 비중을 전체패널서 1회 생성 (룩어헤드 없음)."""
    out = {}
    for lb, tn in itertools.product(lookbacks, top_ns):
        strat = RelativeStrengthMomentum(lookback=lb, skip=skip, top_n=tn, freq=freq)
        try:
            out[(lb, tn)] = strat.generate_weights(prices)
        except Exception:
            pass
    return out


def sharpe_on(prices, weights, lo, hi, cash, fee):
    p = prices.loc[lo:hi]
    w = weights.loc[(weights.index > lo) & (weights.index <= hi)]
    if len(p) < 30 or len(w) == 0:
        return np.nan
    eq, pr = _simulate(p, w, cash, fee)
    return metrics.compute(eq, pr, len(w))["sharpe"]


def main():
    ap = argparse.ArgumentParser(description="워크포워드 분석")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--train_years", type=int, default=3)
    ap.add_argument("--test_years", type=int, default=1)
    ap.add_argument("--lookbacks", default="63,126,189,252,378")
    ap.add_argument("--top_ns", default="2,3,5,8,10")
    ap.add_argument("--skip", type=int, default=21)
    ap.add_argument("--freq", default="M")
    ap.add_argument("--fixed_lookback", type=int, default=126)
    ap.add_argument("--fixed_top_n", type=int, default=3)
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--benchmark", default="SPY")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    top_ns = [int(x) for x in args.top_ns.split(",")]

    tickers = uni.get_universe(args.universe)
    prices = data.load_panel(tickers, args.start, args.end, force=args.force)
    print(f"유니버스 {args.universe}: {prices.shape[1]}종목 × {prices.shape[0]}봉")

    weights = combo_weights(prices, lookbacks, top_ns, args.skip, args.freq)
    fixed_key = (args.fixed_lookback, args.fixed_top_n)
    if fixed_key not in weights:
        print(f"경고: 고정 파라미터 {fixed_key} 그리드에 없음 → 추가")
        strat = RelativeStrengthMomentum(lookback=fixed_key[0], skip=args.skip,
                                         top_n=fixed_key[1], freq=args.freq)
        weights[fixed_key] = strat.generate_weights(prices)

    idx = prices.index
    t0 = idx[0]
    oos_start = t0 + pd.DateOffset(years=args.train_years)
    print(f"롤링: train {args.train_years}년 → test {args.test_years}년 전진")
    print(f"OOS 시작: {oos_start.date()}\n")

    segments, combined_w = [], []
    seg_start = oos_start
    end_ts = pd.Timestamp(args.end)

    while seg_start < end_ts:
        tr_lo = seg_start - pd.DateOffset(years=args.train_years)
        tr_hi = seg_start
        te_hi = min(seg_start + pd.DateOffset(years=args.test_years), end_ts)

        # train 구간 best 파라미터 선택
        best, best_sh = None, -np.inf
        for key, w in weights.items():
            sh = sharpe_on(prices, w, tr_lo, tr_hi, args.cash, args.fee)
            if not np.isnan(sh) and sh > best_sh:
                best_sh, best = sh, key
        if best is None:
            break

        # 선택 파라미터의 OOS 비중 → 누적
        w_best = weights[best]
        seg_w = w_best.loc[(w_best.index > tr_hi) & (w_best.index <= te_hi)]
        combined_w.append(seg_w)

        # 이 구간 OOS 성과 (참고용 개별)
        oos_sh = sharpe_on(prices, w_best, tr_hi, te_hi, args.cash, args.fee)
        segments.append({
            "oos_window": f"{tr_hi.date()}~{te_hi.date()}",
            "chosen_lb": best[0], "chosen_tn": best[1],
            "train_sharpe": round(best_sh, 2), "oos_sharpe": round(oos_sh, 2) if not np.isnan(oos_sh) else None,
        })
        seg_start = seg_start + pd.DateOffset(years=args.test_years)

    if not combined_w:
        print("=" * 60)
        print("[판정 불가] train 윈도우에서 유효 파라미터 선택 실패(전 조합 sharpe 결측) — "
              "OOS/train 데이터 부족. train_years·기간·유니버스 확인")
        return

    seg_df = pd.DataFrame(segments)
    print("구간별 선택 파라미터:")
    print(seg_df.to_string(index=False))

    # 연속 OOS 곡선 ----------------------------------------------------
    combined = pd.concat(combined_w)
    combined = combined[~combined.index.duplicated(keep="last")].sort_index()
    oos_prices = prices.loc[oos_start:]

    wfo_eq, wfo_pr = _simulate(oos_prices, combined, args.cash, args.fee)
    m_wfo = metrics.compute(wfo_eq, wfo_pr, len(combined))

    # 고정 robust 파라미터 (같은 OOS 기간)
    w_fix = weights[fixed_key]
    w_fix_oos = w_fix.loc[w_fix.index >= oos_start]
    fix_eq, fix_pr = _simulate(oos_prices, w_fix_oos, args.cash, args.fee)
    m_fix = metrics.compute(fix_eq, fix_pr, len(w_fix_oos))

    # 동일비중
    ew_target = pd.DataFrame(1.0 / oos_prices.shape[1],
                             index=combined.index, columns=oos_prices.columns)
    ew_eq, ew_pr = _simulate(oos_prices, ew_target, args.cash, args.fee)
    m_ew = metrics.compute(ew_eq, ew_pr, len(ew_target))

    # SPY
    spy_eq = None
    if args.benchmark.lower() != "none":
        try:
            spy = data.load(args.benchmark, args.start, args.end)["Close"].loc[oos_start:]
            spy_eq = args.cash * (spy / spy.iloc[0])
            m_spy = metrics.compute(spy_eq, spy_eq.pct_change().fillna(0), 0)
        except Exception as e:
            print(f"(SPY 스킵: {e})")
            m_spy = None
    else:
        m_spy = None

    n_distinct = seg_df[["chosen_lb", "chosen_tn"]].drop_duplicates().shape[0]
    n_switch = ((seg_df[["chosen_lb", "chosen_tn"]].shift() != seg_df[["chosen_lb", "chosen_tn"]]).any(axis=1).sum() - 1)

    def line(name, m):
        if m is None or "error" in m:
            return f"  {name:16s}: —"
        return (f"  {name:16s}: CAGR {m['cagr']:+7.2%} | Sharpe {m['sharpe']:.2f} | "
                f"MDD {m['max_drawdown']:7.2%} | 최종 {m['final_equity']:,.0f}")

    print("\n" + "=" * 70)
    print(f"OOS 비교 ({oos_start.date()}~{end_ts.date()})")
    print(line("WFO (재최적화)", m_wfo))
    print(line(f"고정 ({fixed_key[0]},{fixed_key[1]})", m_fix))
    print(line("동일비중", m_ew))
    print(line(f"{args.benchmark} 매수보유", m_spy))
    print("-" * 70)
    print(f"  WFO 파라미터: {len(seg_df)}구간 중 {n_distinct}종 선택, {n_switch}회 전환")
    if m_wfo is None or "error" in m_wfo or m_fix is None or "error" in m_fix:
        print("  판정: 판정 불가 (OOS 데이터 부족)")   # line() 과 동일 가드 — <2점 OOS 곡선서 KeyError 방지
    else:
        verdict = "WFO 우위 ✅ 재최적화 이득" if m_wfo["sharpe"] > m_fix["sharpe"] else \
                  "고정 우위 ⚠️ 재최적화가 노이즈 추종 (과최적화 경고)"
        print(f"  판정: {verdict} (Sharpe {m_wfo['sharpe']:.2f} vs {m_fix['sharpe']:.2f})")
    print("=" * 70)

    # 플롯 -------------------------------------------------------------
    os.makedirs("results", exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 5.5))
    ax.plot(wfo_eq.index, wfo_eq.values, label="WFO (재최적화)", lw=1.9)
    ax.plot(fix_eq.index, fix_eq.values, label=f"고정 ({fixed_key[0]},{fixed_key[1]})", lw=1.5)
    ax.plot(ew_eq.index, ew_eq.values, label="동일비중", lw=1.0, alpha=0.7)
    if spy_eq is not None:
        ax.plot(spy_eq.index, spy_eq.values, label=f"{args.benchmark} 매수보유", lw=1.0, alpha=0.7, ls="--")
    for s in seg_df["oos_window"]:
        d = pd.Timestamp(s.split("~")[0])
        ax.axvline(d, color="gray", alpha=0.2, lw=0.8)
    ax.set_title(f"{args.universe} — 워크포워드 OOS (train {args.train_years}y / test {args.test_years}y)")
    ax.set_ylabel("Equity")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    path = os.path.join("results", f"walkforward_{args.universe}.png")
    fig.savefig(path, dpi=120)
    plt.close(fig)

    seg_csv = os.path.join("results", f"walkforward_{args.universe}_segments.csv")
    seg_df.to_csv(seg_csv, index=False)
    print(f"\n차트: {path}\n구간CSV: {seg_csv}")


if __name__ == "__main__":
    sys.exit(main())
