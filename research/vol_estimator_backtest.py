"""오프라인 연구 백테스트 — vol 타겟 스케일의 σ 추정기 교체 (queue-post-freeze 2026-07-22 ①단계).

가설(반증 가능): "EWMA(λ=0.94) 1-step 예측이 현행 20봉 rolling std 대비 (a) vol-target
추적 RMSE 를 낮추고 (b) net Sharpe 를 해치지 않으며 (c) 위기 구간(2020/2022) 실현변동성
초과분(blow-through)을 줄인다." — (a)(c) 개선 없거나 (b) 악화면 교체안 기각.

방법론: 라이브 지오메트리 재현(레짐 SPY>200MA → 스케일 clip(0.20/σ,0,1) → 디레버리지만),
무-lookahead(σ·레짐 모두 t-1 정보 → t 적용), 비용 = |Δscale|×편도 0.175% (스케일 변화가
유발하는 북 회전만 — 등비중 내부 churn 은 전 추정기 공통이라 비교에서 상쇄, 절대치 아님).
다레짐 15년(2011~), 민감도: RW 16/24(±20%), λ 0.90/0.97, vol_target 0.15/0.25.

⚠️ 생존편향(overlay_common 참조): diversified 28 = 생존 대형주 — 상대 비교 전용.
⚠️ 라이브 배선은 이 스크립트 소관 아님 — §B 판정 이후 별도 착수(동결 원칙).

  python research/vol_estimator_backtest.py           # 전체 (GARCH 포함, scipy 필요)
  python research/vol_estimator_backtest.py --fast    # GARCH 제외
"""
import argparse
from datetime import date

import numpy as np
import pandas as pd

from overlay_common import (TRADING_DAYS, VOL_TARGET, PER_SIDE_COST,
                            load_panel_and_spy, risk_on_series, rolling_vol,
                            ewma_vol, ann_metrics, out_path)

# 전 추정기 공통 시뮬 시작점 — GARCH 최소 학습(504=63×8)까지 기다려 비교 구간 통일
# (안 하면 GARCH 만 첫 2년 NaN→현금이 돼 CAGR/Sharpe 비교가 불공정).
SIM_START = 510


# ───────── GARCH(1,1) — scipy MLE (연구 전용, 라이브 후보는 무의존 EWMA) ─────────
def _garch_nll(params, r):
    w, a, b = params
    if w <= 0 or a < 0 or b < 0 or a + b >= 0.999:
        return 1e12
    v = np.var(r)
    nll = 0.0
    for x in r:
        v = w + a * x * x + b * v
        if v <= 0:
            return 1e12
        nll += np.log(v) + x * x / v
    return nll


def _fit_garch(r):
    """MLE 적합. 실패 시 None. 초기값 = variance-targeting 근처."""
    try:
        from scipy.optimize import minimize
    except ImportError:
        return None
    var = float(np.var(r))
    x0 = np.array([var * 0.05, 0.08, 0.90])
    try:
        res = minimize(_garch_nll, x0, args=(r,), method="Nelder-Mead",
                       options={"maxiter": 800, "xatol": 1e-10, "fatol": 1e-8})
        w, a, b = res.x
        if w > 0 and a >= 0 and b >= 0 and a + b < 0.999:
            return float(w), float(a), float(b)
    except Exception:
        pass
    return None


def garch_vol(rets, refit=63, window=1250, min_train=504):
    """1-step-ahead GARCH(1,1) σ (연환산). vols[t] = info ≤ t 로 만든 t+1 예측.
    refit 마다 재적합 + σ² 경로 재구성(연속성), 사이엔 재귀 갱신. 적합실패 = 직전 파라미터 유지."""
    r = np.asarray(rets, dtype=float)
    out = np.full(len(r), np.nan)
    params, v = None, None
    for t in range(len(r)):
        if t >= min_train and (params is None or t % refit == 0):
            lo = max(0, t - window)
            p = _fit_garch(r[lo:t])
            if p is not None:
                params = p
                w, a, b = params
                v = float(np.var(r[lo:t]))        # σ² 경로 재구성
                for x in r[lo:t]:
                    v = w + a * x * x + b * v
        if params is None or v is None:
            continue
        w, a, b = params
        v = w + a * r[t] * r[t] + b * v           # r_t 관측 후 t+1 예측
        out[t] = np.sqrt(v * TRADING_DAYS)
    return out


# ───────── 시뮬 ─────────
def simulate(ew_ret, risk_on, vols, target=VOL_TARGET, start=SIM_START):
    """스케일 시계열 + net 수익률. scale_t = t-1 정보(vols[t-1], risk_on[t-1]).
    비용 = |Δscale| × 편도. 반환 dict(net, gross, scale, cost)."""
    n = len(ew_ret)
    net = np.zeros(n)
    gross = np.zeros(n)
    scale = np.zeros(n)
    cost_sum = 0.0
    prev = 0.0
    for t in range(start, n):
        v = vols[t - 1]
        if risk_on[t - 1] and np.isfinite(v) and v > 1e-12:
            s = min(1.0, target / v)
        else:
            s = 0.0
        c = abs(s - prev) * PER_SIDE_COST
        gross[t] = s * ew_ret[t]
        net[t] = gross[t] - c
        scale[t] = s
        cost_sum += c
        prev = s
    return {"net": net[start:], "gross": gross[start:], "scale": scale[start:],
            "cost": cost_sum, "start": start}


def track_rmse(gross, scale, target=VOL_TARGET, win=21):
    """risk-on(scale>0) 구간에서 21봉 실현변동성 vs 타겟 RMSE — '타겟에 얼마나 붙는가'."""
    rv = pd.Series(gross).rolling(win).std(ddof=0).to_numpy() * np.sqrt(TRADING_DAYS)
    on = scale > 0
    ok = on & np.isfinite(rv)
    if ok.sum() < 50:
        return float("nan")
    return float(np.sqrt(np.mean((rv[ok] - target) ** 2)))


def window_stats(dates, gross, lo, hi):
    """위기 구간 [lo,hi] 스케일드 실현변동성·MDD."""
    m = (dates >= lo) & (dates <= hi)
    if m.sum() < 10:
        return float("nan"), float("nan")
    g = gross[m]
    vol = g.std(ddof=0) * np.sqrt(TRADING_DAYS)
    eq = np.cumprod(1.0 + g)
    mdd = float((eq / np.maximum.accumulate(eq) - 1.0).min())
    return float(vol), mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="GARCH 제외(스모크)")
    ap.add_argument("--target", type=float, default=VOL_TARGET)
    a = ap.parse_args()

    panel, spy = load_panel_and_spy()
    rets = panel.pct_change(fill_method=None)
    ew_ret = rets.mean(axis=1).to_numpy(copy=True)  # 등비중 일일 리밸 (copy — CoW 읽기전용 뷰 회피)
    ew_ret[~np.isfinite(ew_ret)] = 0.0              # 첫 pct_change NaN → EWMA/GARCH 초기화 오염 방지
    risk_on = risk_on_series(spy)
    dates = panel.index.to_numpy()
    print(f"패널 {panel.shape[0]}봉 × {panel.shape[1]}종목  {panel.index[0].date()} ~ {panel.index[-1].date()}")
    print(f"vol_target={a.target}  비용(편도)={PER_SIDE_COST:.4%}  ⚠️ 생존편향: 상대 비교 전용\n")

    est = {
        "RW20(현행)": rolling_vol(ew_ret, 20),
        "RW16(-20%)": rolling_vol(ew_ret, 16),
        "RW24(+20%)": rolling_vol(ew_ret, 24),
        "EWMA λ.90": ewma_vol(ew_ret, 0.90),
        "EWMA λ.94": ewma_vol(ew_ret, 0.94),
        "EWMA λ.97": ewma_vol(ew_ret, 0.97),
    }
    if not a.fast:
        g = garch_vol(ew_ret)
        if np.isfinite(g).any():
            est["GARCH(1,1)"] = g
        else:
            print("(scipy 없음/적합 실패 — GARCH 스킵. 라이브 후보는 어차피 무의존 EWMA)\n")

    rows = []
    dsub = dates[SIM_START:]
    for name, vols in est.items():
        sim = simulate(ew_ret, risk_on, vols, target=a.target)
        m = ann_metrics(sim["net"])
        years = len(sim["net"]) / TRADING_DAYS
        v20, dd20 = window_stats(dsub, sim["gross"], np.datetime64("2020-02-15"), np.datetime64("2020-04-30"))
        v22, dd22 = window_stats(dsub, sim["gross"], np.datetime64("2022-01-01"), np.datetime64("2022-12-31"))
        rows.append({
            "est": name, "cagr": m["cagr"], "sharpe": m["sharpe"], "mdd": m["mdd"],
            "rmse": track_rmse(sim["gross"], sim["scale"], target=a.target),
            "avg_scale": float(sim["scale"][sim["scale"] > 0].mean()) if (sim["scale"] > 0).any() else 0.0,
            "to_yr": float(np.abs(np.diff(sim["scale"], prepend=0.0)).sum() / years),
            "cost_bps_yr": sim["cost"] / years * 1e4,
            "vol2020": v20, "mdd2020": dd20, "vol2022": v22, "mdd2022": dd22,
        })

    hdr = ["추정기", "CAGR", "Sharpe", "MDD", "추적RMSE", "평균스케일", "회전/yr", "비용bps/yr",
           "σ2020", "MDD2020", "σ2022", "MDD2022"]
    lines = ["# vol 추정기 백테스트 — " + date.today().isoformat(), "",
             f"- 구간 {panel.index[0].date()}~{panel.index[-1].date()} · diversified {panel.shape[1]}종목 등비중"
             f" · target {a.target} · 편도비용 {PER_SIDE_COST:.4%}",
             "- ⚠️ 생존편향(현재 생존 대형주) — **추정기 상대 비교 전용**, 절대 성과 해석 금지",
             "- 추적RMSE = risk-on 21봉 실현σ vs 타겟 (낮을수록 타겟 준수)", "",
             "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        lines.append(f"| {r['est']} | {r['cagr']:+.2%} | {r['sharpe']:.2f} | {r['mdd']:.1%} "
                     f"| {r['rmse']:.4f} | {r['avg_scale']:.2f} | {r['to_yr']:.1f} | {r['cost_bps_yr']:.1f} "
                     f"| {r['vol2020']:.1%} | {r['mdd2020']:.1%} | {r['vol2022']:.1%} | {r['mdd2022']:.1%} |")

    base = next(r for r in rows if r["est"].startswith("RW20"))
    cand = next((r for r in rows if r["est"] == "EWMA λ.94"), None)
    lines += ["", "## 판정 (가설 대조)"]
    if cand:
        ok_a = cand["rmse"] < base["rmse"]
        ok_b = cand["sharpe"] >= base["sharpe"] - 0.05
        ok_c = (cand["vol2020"] <= base["vol2020"]) or (cand["vol2022"] <= base["vol2022"])
        lines += [f"- (a) 추적RMSE: EWMA {cand['rmse']:.4f} vs RW20 {base['rmse']:.4f} → {'개선' if ok_a else '악화'}",
                  f"- (b) net Sharpe: {cand['sharpe']:.2f} vs {base['sharpe']:.2f} (−0.05 허용) → {'유지' if ok_b else '악화'}",
                  f"- (c) 위기 σ: 2020 {cand['vol2020']:.1%}/{base['vol2020']:.1%} · 2022 {cand['vol2022']:.1%}/{base['vol2022']:.1%} → {'개선' if ok_c else '악화'}",
                  f"- **종합: {'가설 유지 — EWMA 교체안 strategy 검수로' if (ok_a and ok_b and ok_c) else '가설 기각 — 현행 RW20 유지'}**"]
    md = "\n".join(lines)
    print("\n" + md)
    p = out_path(f"vol_estimator_backtest_{date.today().isoformat()}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(f"\n[기록] {p}")


if __name__ == "__main__":
    main()
