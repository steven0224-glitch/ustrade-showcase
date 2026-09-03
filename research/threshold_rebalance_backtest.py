"""오프라인 연구 백테스트 — 임계(드리프트) 리밸런싱 vs 캘린더 (queue-post-freeze 2026-07-22 ①단계).

가설(반증 가능): "종목별 |보유비중−목표비중| > 밴드일 때만 주문하는 임계 리밸런싱이
현행 일일 캘린더 리밸 대비 회전율·비용을 유의미하게 줄이면서 net 성과·경로 이탈
(추적오차)을 실질적으로 해치지 않는 밴드가 존재한다." — 모든 밴드에서 비용 절감보다
성과 이탈이 크면 기각.

방법론: 라이브 지오메트리(등비중 × vol스케일(RW20) × 레짐게이트) 로 목표비중 생성 —
전 변형 공통. 변형은 '언제 목표로 되돌리나'만 다름:
  daily(현행)   : 매일 전 종목 목표로 (밴드 0)
  weekly        : 월요일만 전 종목 목표로 (buffett reselect_days=7 유사)
  band X%p      : 매일 점검, |드리프트| > X%p(절대) 인 종목만 목표로
  rel Y%        : 매일 점검, |드리프트| > Y%×목표비중(상대) 인 종목만 — N 무관 일반화.
                  ⚠ 절대밴드는 종목당 목표비중(등비중 28 → ≤3.6%)보다 크면 진입 자체가
                  불발되는 퇴화가 있음(발동 가능 입력 검산 — strategy memory 2026-07-07).
                  상대밴드는 f<1 이면 진입(|0−tw|=tw>f·tw) 항상 발동.
레짐 OFF 전환은 밴드 무관 전량 청산(라이브 불변식 — 목표 0 은 안전 규칙이라 밴드 미적용).
비용 = 거래대금 × 편도 0.175%. 무-lookahead(목표는 t-1 정보, 체결은 t 시가 근사=전일 종가 비중).
민감도: 밴드 격자 {1,2,3,5,8}%p 자체가 민감도 스윕.

⚠️ 생존편향(overlay_common) — 변형 간 상대 비교 전용.
⚠️ 선정 churn(종목 교체) 은 이 사본에 없음 — 등비중 고정 바스켓이라 여기서의 회전은
   전부 '드리프트 되돌림 + 스케일 변화' 몫. 라이브 채택 시 선정 churn 절감분은 별도 상방.

  python research/threshold_rebalance_backtest.py
"""
from datetime import date

import numpy as np

from overlay_common import (TRADING_DAYS, VOL_TARGET, PER_SIDE_COST, WARMUP,
                            load_panel_and_spy, risk_on_series, rolling_vol,
                            ann_metrics, out_path)


def simulate(rets, risk_on, vols, mode, weekdays=None, band=0.0, target=VOL_TARGET, start=WARMUP):
    """보유가치 벡터 시뮬. mode ∈ {'daily','weekly','band'}.

    일 t: (1) t-1 정보로 목표비중 tw = scale/N (레짐OFF=0)
          (2) 리밸 판정 — daily=항상 / weekly=월요일 / band=|cw-tw|>band 종목만
              + 레짐 OFF 전환일은 무조건 전량 목표(=0)로
          (3) 거래비용 차감 → t 수익률 적용
    반환 {net, to_yr, cost_bps_yr, max_drift}."""
    n, N = rets.shape
    h = np.zeros(N)                     # 종목별 보유 가치
    cash = 1.0
    net = np.zeros(n)
    cost_sum = to_sum = 0.0
    max_drift = 0.0
    prev_on = False
    for t in range(start, n):
        v = vols[t - 1]
        on = bool(risk_on[t - 1])
        scale = min(1.0, target / v) if (on and np.isfinite(v) and v > 1e-12) else 0.0
        eq = cash + h.sum()             # 리밸 전 자본 — 당일 수익률 기준점(비용이 수익률에 반영되게)
        tw = np.full(N, scale / N) if scale > 0 else np.zeros(N)
        cw = h / eq if eq > 0 else np.zeros(N)

        if scale > 0:
            max_drift = max(max_drift, float(np.abs(cw - tw).max()))
        if mode == "daily":
            do = np.ones(N, dtype=bool)
        elif mode == "weekly":
            do = np.ones(N, dtype=bool) if weekdays[t] == 0 else np.zeros(N, dtype=bool)
        elif mode == "relband":
            do = np.abs(cw - tw) > band * tw    # tw=0 → 임계 0 → 잔여보유 즉시 청산
        else:
            do = np.abs(cw - tw) > band
        if prev_on and not on:          # 레짐 OFF 전환 — 밴드 무관 전량 청산(라이브 불변식)
            do = np.ones(N, dtype=bool)

        if do.any() and eq > 0:
            delta = (tw[do] - cw[do]) * eq
            traded = float(np.abs(delta).sum())
            cost = traded * PER_SIDE_COST
            h[do] = tw[do] * eq
            cash = eq - h.sum() - cost
            cost_sum += cost
            to_sum += traded
        h *= (1.0 + rets[t])
        net[t] = (cash + h.sum()) / eq - 1.0 if eq > 0 else 0.0   # 비용+시장수익 모두 포함
        prev_on = on
    years = (n - start) / TRADING_DAYS
    return {"net": net[start:], "to_yr": to_sum / years,
            "cost_bps_yr": cost_sum / years * 1e4, "max_drift": max_drift}


def main():
    panel, spy = load_panel_and_spy()
    rets_df = panel.pct_change(fill_method=None).fillna(0.0)
    rets = rets_df.to_numpy()
    weekdays = panel.index.dayofweek.to_numpy()
    risk_on = risk_on_series(spy)
    ew_ret = rets_df.mean(axis=1).to_numpy()
    vols = rolling_vol(ew_ret, 20)                 # 현행 추정기 — 전 변형 공통
    print(f"패널 {panel.shape[0]}봉 × {panel.shape[1]}종목  {panel.index[0].date()} ~ {panel.index[-1].date()}")
    print(f"편도비용 {PER_SIDE_COST:.4%} · ⚠️ 생존편향: 변형 상대 비교 전용\n")

    variants = [("daily(현행)", "daily", 0.0), ("weekly(월)", "weekly", 0.0)] + \
               [(f"band {b*100:g}%p", "band", b) for b in (0.005, 0.01, 0.02, 0.03)] + \
               [(f"rel {int(f*100)}%", "relband", f) for f in (0.25, 0.50)]

    rows, base_net = [], None
    for label, mode, band in variants:
        sim = simulate(rets, risk_on, vols, mode, weekdays=weekdays, band=band)
        m = ann_metrics(sim["net"])
        if base_net is None:
            base_net = sim["net"]
        te = float((sim["net"] - base_net).std(ddof=0) * np.sqrt(TRADING_DAYS))
        rows.append({"label": label, **m, "te": te, **{k: sim[k] for k in ("to_yr", "cost_bps_yr", "max_drift")}})

    hdr = ["변형", "CAGR", "Sharpe", "MDD", "회전/yr", "비용bps/yr", "추적오차(vs현행)", "최대드리프트"]
    lines = ["# 임계 리밸런싱 백테스트 — " + date.today().isoformat(), "",
             f"- 구간 {panel.index[0].date()}~{panel.index[-1].date()} · diversified {panel.shape[1]}종목"
             f" · 목표 = 등비중×vol스케일(RW20)×레짐 · 편도 {PER_SIDE_COST:.4%}",
             "- ⚠️ 생존편향 — 변형 상대 비교 전용. 선정 churn 은 미포함(라이브 절감분은 별도 상방).", "",
             "| " + " | ".join(hdr) + " |", "|" + "---|" * len(hdr)]
    for r in rows:
        lines.append(f"| {r['label']} | {r['cagr']:+.2%} | {r['sharpe']:.2f} | {r['mdd']:.1%} "
                     f"| {r['to_yr']:.2f} | {r['cost_bps_yr']:.1f} | {r['te']:.2%} | {r['max_drift']:.1%} |")

    base = rows[0]
    lines += ["", "## 판정 (가설 대조)"]
    best = None
    for r in rows[2:]:                                    # band 변형만
        save = base["cost_bps_yr"] - r["cost_bps_yr"]
        cagr_gap = r["cagr"] - base["cagr"]
        verdict = save > 0 and cagr_gap > -0.0015 and r["te"] < 0.01   # 비용↓ & CAGR -15bps 내 & TE<1%p
        lines.append(f"- {r['label']}: 비용 절감 {save:.1f}bps/yr · CAGR 차 {cagr_gap:+.2%} · TE {r['te']:.2%}"
                     f" → {'후보' if verdict else '탈락'}")
        if verdict and (best is None or save > best[1]):
            best = (r["label"], save)
    lines.append(f"- **종합: {'가설 유지 — ' + best[0] + ' 를 strategy 검수로' if best else '가설 기각 — 현행 일일 리밸 유지'}**")
    md = "\n".join(lines)
    print("\n" + md)
    p = out_path(f"threshold_rebalance_backtest_{date.today().isoformat()}.md")
    with open(p, "w", encoding="utf-8") as f:
        f.write(md + "\n")
    print(f"\n[기록] {p}")


if __name__ == "__main__":
    main()
