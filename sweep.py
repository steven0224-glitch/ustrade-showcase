"""파라미터 격자탐색 + 과적합 진단.

lookback × top_n 그리드를 전체기간 + train/test 분할로 평가.
robust 판정:
  1) In/Out-of-sample 성과 감쇠 (train서 좋은 게 test서도 좋은가)
  2) train↔test Sharpe 그리드 Spearman 순위상관 (높을수록 전이됨)
  3) plateau — 최고 셀의 3×3 이웃 평균 (고립 스파이크 vs 넓은 고원)

예:
  python sweep.py --universe diversified
  python sweep.py --universe tech --lookbacks 63,126,252 --top_ns 2,3,5 --split 2021-06-01
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


def eval_range(prices, weights, cash, fee):
    eq, pr = _simulate(prices, weights, cash, fee)
    return metrics.compute(eq, pr, len(weights))


def neighborhood_mean(grid: pd.DataFrame, ri: int, ci: int) -> float:
    sub = grid.iloc[max(0, ri - 1):ri + 2, max(0, ci - 1):ci + 2]
    return float(np.nanmean(sub.values))


def main():
    ap = argparse.ArgumentParser(description="파라미터 스윕 + 과적합 진단")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--split", default="2021-01-01", help="train/test 경계일")
    ap.add_argument("--lookbacks", default="63,126,189,252,378")
    ap.add_argument("--top_ns", default="2,3,5,8,10")
    ap.add_argument("--skip", type=int, default=21)
    ap.add_argument("--freq", default="M")
    ap.add_argument("--cash", type=float, default=10000.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--metric", default="sharpe", choices=["sharpe", "cagr"])
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    lookbacks = [int(x) for x in args.lookbacks.split(",")]
    top_ns = [int(x) for x in args.top_ns.split(",")]

    tickers = uni.get_universe(args.universe)
    prices = data.load_panel(tickers, args.start, args.end, force=args.force)
    print(f"유니버스 {args.universe}: {prices.shape[1]}종목 × {prices.shape[0]}봉")
    print(f"그리드: lookback {lookbacks} × top_n {top_ns} = {len(lookbacks)*len(top_ns)}조합")
    print(f"분할: train ≤ {args.split} < test\n")

    split = pd.Timestamp(args.split)
    tr_p, te_p = prices.loc[:split], prices.loc[split:]

    rows = []
    for lb, tn in itertools.product(lookbacks, top_ns):
        strat = RelativeStrengthMomentum(lookback=lb, skip=args.skip, top_n=tn, freq=args.freq)
        try:
            w = strat.generate_weights(prices)
        except Exception as e:
            print(f"  스킵 lb={lb} tn={tn}: {e}")
            continue
        w_tr = w.loc[w.index <= split]
        w_te = w.loc[w.index > split]

        m_all = eval_range(prices, w, args.cash, args.fee)
        m_tr = eval_range(tr_p, w_tr, args.cash, args.fee) if len(w_tr) else None
        m_te = eval_range(te_p, w_te, args.cash, args.fee) if len(w_te) else None

        rows.append({
            "lookback": lb, "top_n": tn,
            "all_cagr": m_all["cagr"], "all_sharpe": m_all["sharpe"], "all_mdd": m_all["max_drawdown"],
            "train_sharpe": m_tr["sharpe"] if m_tr else np.nan,
            "train_cagr": m_tr["cagr"] if m_tr else np.nan,
            "test_sharpe": m_te["sharpe"] if m_te else np.nan,
            "test_cagr": m_te["cagr"] if m_te else np.nan,
        })

    df = pd.DataFrame(rows)
    os.makedirs("results", exist_ok=True)
    csv_path = os.path.join("results", f"sweep_{args.universe}.csv")
    df.to_csv(csv_path, index=False)

    if df.empty:
        print("=" * 60)
        print("[진단 스킵] 모든 lookback×top_n 조합이 리밸런스 비중 생성 실패 — "
              "기간(--start/--end)·유니버스·lookback 그리드 불일치(가용 히스토리보다 lookback 큼) 확인")
        return

    mcol = args.metric
    tr_grid = df.pivot(index="lookback", columns="top_n", values=f"train_{mcol}")
    te_grid = df.pivot(index="lookback", columns="top_n", values=f"test_{mcol}")
    all_grid = df.pivot(index="lookback", columns="top_n", values=f"all_{mcol}")

    # 진단 --------------------------------------------------------------- (--metric 선택지표 사용, sharpe 하드코딩 금지)
    trc, tec = f"train_{mcol}", f"test_{mcol}"
    print("=" * 60)
    if not df[trc].notna().any() or not df[tec].notna().any():
        print(f"[진단 스킵] train/test {mcol} 전부 결측 — 리밸런스 부족(윈도우·유니버스·기간 확인)")
    else:
        spearman = df[trc].corr(df[tec], method="spearman")
        best_tr = df.loc[df[trc].idxmax()]                     # train 최고 파라미터
        test_of_best_tr = df[(df.lookback == best_tr.lookback) & (df.top_n == best_tr.top_n)].iloc[0]
        overfit_gap = best_tr[trc] - test_of_best_tr[tec]
        # plateau: train 그리드에서 3×3 이웃평균 최고 셀
        best_neigh, best_cell = -np.inf, None
        for ri, lb in enumerate(tr_grid.index):
            for ci, tn in enumerate(tr_grid.columns):
                nm = neighborhood_mean(tr_grid, ri, ci)
                if nm > best_neigh:
                    best_neigh, best_cell = nm, (lb, tn)
        print(f"[1] In/Out-of-sample (지표={mcol})")
        print(f"    train 최고 : lookback={int(best_tr.lookback)} top_n={int(best_tr.top_n)} "
              f"→ train {mcol} {best_tr[trc]:.2f}")
        print(f"    동일파라미터 test {mcol} {test_of_best_tr[tec]:.2f}  (감쇠 {overfit_gap:+.2f})")
        print(f"    test 최고  : lookback={int(df.loc[df[tec].idxmax()].lookback)} "
              f"top_n={int(df.loc[df[tec].idxmax()].top_n)} "
              f"→ test {mcol} {df[tec].max():.2f}")
        print(f"[2] train↔test Spearman 순위상관 : {spearman:+.2f}")
        print("    (+0.5↑ 전이 양호 / 0 근처·음수 = 과적합 의심)")
        print(f"[3] plateau 중심 (train 3×3 이웃평균 최고) : "
              f"lookback={best_cell[0]} top_n={best_cell[1]} (이웃평균 {best_neigh:.2f})")
        print("    → 고립 스파이크보다 이 고원 중심이 robust 후보")
    print("=" * 60)

    # 히트맵 ------------------------------------------------------------
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.6))
    for ax, grid, title in zip(
        axes, [tr_grid, te_grid, all_grid],
        [f"TRAIN {mcol}", f"TEST {mcol}", f"전체기간 {mcol}"],
    ):
        im = ax.imshow(grid.values, aspect="auto", cmap="RdYlGn", origin="lower")
        ax.set_xticks(range(len(grid.columns)))
        ax.set_xticklabels(grid.columns)
        ax.set_yticks(range(len(grid.index)))
        ax.set_yticklabels(grid.index)
        ax.set_xlabel("top_n")
        ax.set_ylabel("lookback")
        ax.set_title(title)
        for ri in range(grid.shape[0]):
            for ci in range(grid.shape[1]):
                v = grid.values[ri, ci]
                if not np.isnan(v):
                    ax.text(ci, ri, f"{v:.2f}", ha="center", va="center", fontsize=8)
        fig.colorbar(im, ax=ax, fraction=0.046)
    fig.suptitle(f"{args.universe} — 파라미터 스윕 (train/test/전체)")
    fig.tight_layout()
    heat_path = os.path.join("results", f"sweep_{args.universe}_heatmap.png")
    fig.savefig(heat_path, dpi=120)
    plt.close(fig)

    print(f"\nCSV  : {csv_path}")
    print(f"히트맵: {heat_path}")


if __name__ == "__main__":
    sys.exit(main())
