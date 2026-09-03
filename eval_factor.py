"""팩터 IC 검증 (Alphalens 발췌) — 거래 전 팩터 예측력 게이트.

ml-for-trading 의 factor-evaluation 개념을 의존성 없이 순수 pandas 로 구현.
sweep/walkforward 가 "파라미터" 과적합을 잡았다면, 이건 "팩터 자체에 알파가 있나"를 검증.

지표:
  IC(정보계수)  : 시점별 팩터값↔미래수익 횡단면 Spearman 순위상관
  IC-IR        : mean(IC)/std(IC) — 일관성
  t-stat       : IC-IR×√N (비중첩 샘플) — 통계적 유의성
  분위수 수익   : 팩터 Q분위별 미래수익 (단조성·top-bottom 스프레드)
  decay        : horizon(1/5/21/63일)별 IC — 신호 지속성

판정 기준(미국주식 횡단면):
  |mean IC| ≳ 0.02 & IC-IR ≳ 0.3 & |t| ≳ 2  → 유의미한 예측력
  분위수 단조 + 양의 스프레드                  → 거래 가능

예:
  python eval_factor.py --universe diversified --factor all
  python eval_factor.py --universe sp100 --factor momentum_6_1
  python eval_factor.py --universe diversified --factor composite --combine momentum_12_1,low_volatility,reversal_1m
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd

import data
import universe as uni
from strategies import factors as F
from strategies import alpha_zoo as Z
from engines._plot import plt

HORIZONS = [1, 5, 21, 63]
PRIMARY = 21


def forward_return(prices, h):
    return prices.shift(-h) / prices - 1.0


def ic_series(factor, fwd, step):
    """비중첩 샘플(step) 시점별 횡단면 Spearman IC."""
    out = {}
    for d in factor.index[::step]:
        pair = pd.concat([factor.loc[d].rename("f"), fwd.loc[d].rename("r")], axis=1).dropna()
        if len(pair) >= 5:
            out[d] = pair["f"].corr(pair["r"], method="spearman")
    return pd.Series(out).dropna()


def ic_summary(ics):
    n = len(ics)
    if n == 0:
        return dict(n=0, mean_ic=np.nan, ic_std=np.nan, ic_ir=np.nan, t_stat=np.nan, hit=np.nan)
    m, s = ics.mean(), ics.std()
    ir = m / s if s > 0 else 0.0
    return dict(n=n, mean_ic=m, ic_std=s, ic_ir=ir, t_stat=ir * np.sqrt(n), hit=(ics > 0).mean())


def quantile_returns(factor, fwd, q, step):
    acc = {qi: [] for qi in range(q)}
    for d in factor.index[::step]:
        f = factor.loc[d].dropna()
        if len(f) < q * 2:
            continue
        try:
            bins = pd.qcut(f, q, labels=False, duplicates="drop")
        except ValueError:
            continue
        r = fwd.loc[d]
        for qi in range(q):
            mem = f.index[bins == qi]
            if len(mem):
                acc[qi].append(r[mem].mean())
    return pd.Series({qi: (np.nanmean(v) if v else np.nan) for qi, v in acc.items()})


def verdict(s):
    if s["n"] == 0 or np.isnan(s["mean_ic"]):
        return "데이터 부족"
    strong = abs(s["mean_ic"]) >= 0.02 and abs(s["ic_ir"]) >= 0.3 and abs(s["t_stat"]) >= 2
    return "✅ 예측력 있음" if strong else "⚠️ 약함/무의미"


# ── 랜덤컨트롤(strict) — HKUDS/Vibe-Trading bench_runner_strict 이식 ──────────
# zero-benchmark IC 게이트(위 verdict)는 공유 베타(시장·사이즈)로 통과하는 가짜 팩터를
# 못 거른다. 같은-유니버스 랜덤셔플 null 대비 paired α 의 t-stat 으로 승격 — Harvey-Liu-Zhu
# (2016) 다중검정 정신을 API 로 인코딩. random_control 은 keyword-only-no-default(생략=TypeError).

def _shuffle_within_rows(df, seed):
    """행 내 유한값만 횡단면 치환 — 분포 보존, signal→종목 매핑만 파괴(공정 null)."""
    rng = np.random.default_rng(seed)
    values = df.to_numpy(copy=True)
    if values.dtype == object:                       # pd.NA 팩터 → float null 강제(rsi/bollinger 회귀)
        values = np.where(pd.isna(values), np.nan, values).astype(float)
    for i in range(values.shape[0]):
        row = values[i]
        mask = np.isfinite(row)
        if mask.sum() < 2:
            continue
        row[mask] = rng.permutation(row[mask])
        values[i] = row
    return pd.DataFrame(values, index=df.index, columns=df.columns)


def random_ic_series(factor, fwd, step, *, n_seeds=5, base_seed=42):
    """n_seeds 행-셔플 랜덤컨트롤의 시점별 평균 IC (모든 seed 유효한 날짜만, inner join)."""
    frames = []
    for s in range(base_seed, base_seed + max(1, n_seeds)):
        ic = ic_series(_shuffle_within_rows(factor, s), fwd, step)
        if not ic.empty:
            frames.append(ic)
    if not frames:
        return pd.Series(dtype=float)
    return pd.concat(frames, axis=1, join="inner").mean(axis=1)


def _t_stat(series):
    """0 대비 1-표본 t-stat (정의불가 시 0.0)."""
    n = len(series)
    if n < 2:
        return 0.0
    sd = float(series.std(ddof=1))
    if not (sd > 0 and np.isfinite(sd)):
        return 0.0
    return float(series.mean() / (sd / np.sqrt(n)))


def strict_summary(factor, fwd, step, *, random_control, n_seeds=5):
    """signal IC 요약 + paired α(=signal_IC−random_IC) t-stat + random_ic_mean.

    random_control=False 는 명시적으로 넘겨야 함(생략=TypeError) — 실수로 null 을 빼먹어
    α 가 3~8%p 부풀던 Vibe/Bili_Stock 감사 교훈의 레일.
    """
    sig = ic_series(factor, fwd, step)
    s = ic_summary(sig)
    if random_control:
        rnd = random_ic_series(factor, fwd, step, n_seeds=n_seeds)
        common = sig.index.intersection(rnd.index)
        alpha = (sig.loc[common] - rnd.loc[common]).dropna()
        s["alpha_t"] = _t_stat(alpha)
        s["random_ic_mean"] = float(rnd.mean()) if len(rnd) else np.nan
        s["alpha_n"] = len(alpha)
    else:
        s["alpha_t"] = _t_stat(sig)   # zero-benchmark 폴백
        s["random_ic_mean"] = 0.0
        s["alpha_n"] = len(sig)
    return s


def strict_verdict(s, *, alpha_t_threshold=2.0, min_n=30):
    """랜덤컨트롤 기반 승급 판정 (Vibe categorise_strict 축약: OOS 생략)."""
    if s.get("alpha_n", 0) < min_n or np.isnan(s.get("alpha_t", np.nan)):
        return "noise"
    at = s["alpha_t"]
    if at <= -alpha_t_threshold:
        return "reversed"
    if at >= alpha_t_threshold:
        return "✅ confirmed_alive"
    return "noise"


def factor_panel(name, prices, combine, tickers=None, panel_dict=None):
    if name == "composite":
        names = combine.split(",") if combine else list(F.FACTOR_REGISTRY)
        return F.composite(prices, names), names
    if name == "earnings_surprise":
        from fmp_factors import earnings_surprise_panel
        return earnings_surprise_panel(tickers or list(prices.columns), prices.index), [name]
    if name in Z.ALPHA_REGISTRY:
        if panel_dict is None:
            raise ValueError(f"zoo 팩터 {name} 는 OHLCV 패널 필요(panel_dict)")
        return Z.compute(name, panel_dict).reindex(index=prices.index, columns=prices.columns), [name]
    return F.get_factor(name, prices), [name]


def main():
    ap = argparse.ArgumentParser(description="팩터 IC 검증")
    ap.add_argument("--universe", default="diversified")
    ap.add_argument("--factor", default="all", help="팩터명 | all | composite | zoo | (zoo 팩터명)")
    ap.add_argument("--combine", default="", help="composite 구성 (콤마구분)")
    ap.add_argument("--start", default="2014-01-01")
    ap.add_argument("--end", default="2025-01-01")
    ap.add_argument("--q", type=int, default=5)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="랜덤컨트롤 strict 게이트(paired α t-stat) 추가 — 'all' 에도 적용")
    ap.add_argument("--seeds", type=int, default=5, help="랜덤컨트롤 셔플 seed 수(기본 5)")
    args = ap.parse_args()

    tickers = uni.get_universe(args.universe)
    prices = data.load_panel(tickers, args.start, args.end, force=args.force)
    print(f"유니버스 {args.universe}: {prices.shape[1]}종목 × {prices.shape[0]}봉\n")

    fwd = {h: forward_return(prices, h) for h in HORIZONS}
    os.makedirs("results", exist_ok=True)

    # zoo 팩터는 OHLCV 패널 필요 — lazy 로드(1회 캐시)
    _panel_cache = {}
    def get_panel():
        if "d" not in _panel_cache:
            _panel_cache["d"] = data.load_ohlcv_panel(tickers, args.start, args.end, force=args.force)
        return _panel_cache["d"]

    if args.factor == "zoo":
        pdct = get_panel()
        rows = []
        for nm in Z.ALPHA_REGISTRY:
            try:
                fac, _ = factor_panel(nm, prices, args.combine, tickers, pdct)
            except Exception as e:
                print(f"  스킵 {nm}: {e}")
                continue
            s = strict_summary(fac, fwd[PRIMARY], PRIMARY, random_control=True, n_seeds=args.seeds)
            rows.append((nm, s))
        rows.sort(key=lambda r: (r[1].get("alpha_t") if r[1].get("alpha_t") is not None else -99), reverse=True)
        print(f"=== Alpha Zoo strict IC (랜덤컨트롤 {args.seeds}seed, {PRIMARY}일 forward) ===")
        print(f"{'alpha':16s} {'meanIC':>8s} {'IC-IR':>7s} {'randIC':>8s} {'α_t':>6s}  판정")
        for nm, s in rows:
            print(f"{nm:16s} {s['mean_ic']:>8.3f} {s['ic_ir']:>7.2f} {s['random_ic_mean']:>8.3f} "
                  f"{s['alpha_t']:>6.2f}  {strict_verdict(s)}")
        pd.DataFrame([{"alpha": nm, **s} for nm, s in rows]).to_csv("results/alpha_zoo_strict.csv", index=False)
        print("\nCSV: results/alpha_zoo_strict.csv")
        print("→ confirmed_alive 만 selection 틸트 후보(use_alpha flag). 나머지 채택 금지.")
        return

    if args.factor == "all":
        names = list(F.FACTOR_REGISTRY) + ["composite", "earnings_surprise"]
        rows = []
        for nm in names:
            try:
                fac, _ = factor_panel(nm, prices, args.combine, tickers)
            except Exception as e:
                print(f"  스킵 {nm}: {e}")
                continue
            if args.strict:
                s = strict_summary(fac, fwd[PRIMARY], PRIMARY, random_control=True, n_seeds=args.seeds)
            else:
                s = ic_summary(ic_series(fac, fwd[PRIMARY], PRIMARY))
            rows.append((nm, s))
        print(f"=== 팩터별 IC ({PRIMARY}일 forward, 비중첩{', +랜덤컨트롤' if args.strict else ''}) ===")
        hdr = f"{'factor':18s} {'meanIC':>8s} {'IC-IR':>7s} {'t':>6s} {'hit':>6s}"
        print(hdr + (f" {'α_t':>6s}  판정" if args.strict else "  판정"))
        for nm, s in rows:
            line = (f"{nm:18s} {s['mean_ic']:>8.3f} {s['ic_ir']:>7.2f} {s['t_stat']:>6.2f} "
                    f"{s['hit']:>6.0%}")
            print(line + (f" {s['alpha_t']:>6.2f}  {strict_verdict(s)}" if args.strict else f"  {verdict(s)}"))
        df = pd.DataFrame([{"factor": nm, **s} for nm, s in rows])
        df.to_csv("results/factor_ic_all.csv", index=False)

        fig, ax = plt.subplots(figsize=(10, 5))
        nms = [nm for nm, _ in rows]
        ics = [s["mean_ic"] for _, s in rows]
        colors = ["#2a9d8f" if abs(i) >= 0.02 else "#e76f51" for i in ics]
        ax.barh(nms, ics, color=colors)
        ax.axvline(0.02, ls="--", c="gray", lw=0.8); ax.axvline(-0.02, ls="--", c="gray", lw=0.8)
        ax.set_xlabel(f"mean IC ({PRIMARY}d)")
        ax.set_title(f"{args.universe} — 팩터별 평균 IC (|IC|≥0.02 = 녹색)")
        ax.grid(alpha=0.3, axis="x")
        fig.tight_layout()
        path = f"results/factor_ic_{args.universe}.png"
        fig.savefig(path, dpi=120); plt.close(fig)
        print(f"\nCSV: results/factor_ic_all.csv\n차트: {path}")
        return

    # 단일 팩터 상세
    is_zoo = args.factor in Z.ALPHA_REGISTRY
    fac, comp = factor_panel(args.factor, prices, args.combine, tickers,
                             get_panel() if is_zoo else None)
    label = args.factor + (f"({'+'.join(comp)})" if args.factor == "composite" else "")
    print(f"=== {label} 상세 ===\n")

    if args.strict or is_zoo:
        ss = strict_summary(fac, fwd[PRIMARY], PRIMARY, random_control=True, n_seeds=args.seeds)
        print(f"랜덤컨트롤({args.seeds}seed): randIC={ss['random_ic_mean']:+.3f} "
              f"paired α_t={ss['alpha_t']:+.2f} → {strict_verdict(ss)}\n")

    # 모멘텀과 직교성 (낮을수록 멀티팩터 분산효과 ↑)
    if args.factor != "momentum_6_1":
        mom = F.get_factor("momentum_6_1", prices)
        corrs = []
        for d in fac.index[::PRIMARY]:
            pair = pd.concat([fac.loc[d].rename("a"), mom.loc[d].rename("b")], axis=1).dropna()
            if len(pair) >= 5:
                corrs.append(pair["a"].corr(pair["b"], method="spearman"))
        if corrs:
            mc = np.nanmean(corrs)
            print(f"모멘텀(6m)과 횡단면 상관: {mc:+.2f} "
                  f"({'직교 ✅ 멀티팩터 유효' if abs(mc) < 0.3 else '중복 ⚠️ 분산효과 낮음'})\n")

    print(f"{'horizon':>8s} {'meanIC':>8s} {'IC-IR':>7s} {'t':>6s} {'hit':>6s} {'N':>5s}  판정")
    decay = {}
    for h in HORIZONS:
        s = ic_summary(ic_series(fac, fwd[h], h))
        decay[h] = s["mean_ic"]
        print(f"{h:>8d} {s['mean_ic']:>8.3f} {s['ic_ir']:>7.2f} {s['t_stat']:>6.2f} "
              f"{s['hit']:>6.0%} {s['n']:>5d}  {verdict(s)}")

    qr = quantile_returns(fac, fwd[PRIMARY], args.q, PRIMARY)
    spread = qr.iloc[-1] - qr.iloc[0]
    mono = all(qr.iloc[i] <= qr.iloc[i + 1] for i in range(len(qr) - 1))
    print(f"\n분위수 {PRIMARY}일 수익 (Q0=하위 … Q{args.q-1}=상위):")
    for qi, v in qr.items():
        print(f"  Q{qi}: {v:+.3%}")
    print(f"  top-bottom 스프레드: {spread:+.3%} | 단조증가: {'예 ✅' if mono else '아니오 ⚠️'}")

    # 차트: rolling IC + 분위수 + decay
    ic_plot = ic_series(fac, fwd[PRIMARY], 5)
    fig, axes = plt.subplots(1, 3, figsize=(16, 4.5))
    axes[0].plot(ic_plot.index, ic_plot.rolling(12).mean().values, lw=1.3)
    axes[0].axhline(0, c="k", lw=0.6); axes[0].axhline(0.02, ls="--", c="g", lw=0.7)
    axes[0].set_title(f"rolling IC ({PRIMARY}d)"); axes[0].grid(alpha=0.3)
    axes[1].bar([f"Q{qi}" for qi in qr.index], qr.values * 100,
                color=["#e76f51", "#f4a261", "#e9c46a", "#8ab17d", "#2a9d8f"][:len(qr)])
    axes[1].set_title(f"분위수 {PRIMARY}d 수익 %"); axes[1].grid(alpha=0.3, axis="y")
    axes[2].bar([str(h) for h in HORIZONS], [decay[h] for h in HORIZONS], color="#264653")
    axes[2].axhline(0.02, ls="--", c="g", lw=0.7)
    axes[2].set_title("IC decay (horizon별)"); axes[2].set_xlabel("forward days"); axes[2].grid(alpha=0.3, axis="y")
    fig.suptitle(f"{args.universe} — {label}")
    fig.tight_layout()
    path = f"results/factor_{args.universe}_{args.factor}.png"
    fig.savefig(path, dpi=120); plt.close(fig)
    print(f"\n차트: {path}")


if __name__ == "__main__":
    sys.exit(main())
