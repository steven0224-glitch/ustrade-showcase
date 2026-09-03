"""신호 성과 HTML 리포트 — HKUDS/Vibe-Trading Shadow-Account 스타일 이식(경량판).

selection_review(신호별 사후수익 귀속) + review(실현 라운드트립)를 self-contained HTML
(inline base64 차트)로 렌더. "어떤 신호가 실제로 돈 됐나"를 사람이 한눈에 보게 —
전략 자동변경은 안 함(관측·리포트 전용, selection_review 계약 그대로).

경량 원칙(A 대비): Jinja2/weasyprint 미사용(순수 문자열+base64 PNG). PDF 없음. 차트는
**대시보드 build 경로에서만** 생성(cron 매매 배치 무지연 — build_data 훅이 호출).

  python report_html.py                 # dashboard/report.html 생성
  python report_html.py --horizon 5     # 단기 사후수익
"""
import argparse
import base64
import io
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
DASH_REPORT = os.path.join(HERE, "dashboard", "report.html")


def _persona_log_dirs():
    """기본 home + 페르소나 별도 home 의 logs (paths.persona_homes 정규 파서)."""
    from paths import LOG_DIR, persona_homes
    return [LOG_DIR] + [h / "logs" for h in persona_homes()]


def _fig_b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    from engines._plot import plt
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _bar_chart(title, labels, values, note=""):
    """avg 수익(소수) 막대 — 양수 녹색/음수 적색. 빈 데이터면 None."""
    if not labels:
        return None
    from engines._plot import plt
    fig, ax = plt.subplots(figsize=(7, max(2.2, 0.5 * len(labels) + 1)))
    colors = ["#2a9d8f" if v >= 0 else "#e76f51" for v in values]
    ax.barh(labels, [v * 100 for v in values], color=colors)
    ax.axvline(0, c="k", lw=0.6)
    ax.set_xlabel("평균 사후수익 %")
    ax.set_title(title + (f"  ({note})" if note else ""))
    ax.grid(alpha=0.3, axis="x")
    return _fig_b64(fig)


def _dim_table(dims, dim, title):
    d = dims.get(dim, {})
    if not d:
        return ""
    rows = ["<table><thead><tr><th>버킷</th><th>n</th><th>평균수익</th><th>중앙값</th><th>승률</th></tr></thead><tbody>"]
    for b in sorted(d, key=lambda k: -d[k]["avg"]):
        a = d[b]
        cls = "pos" if a["avg"] >= 0 else "neg"
        rows.append(f"<tr><td>{b}</td><td>{a['n']}</td><td class='{cls}'>{a['avg']:+.2%}</td>"
                    f"<td>{a['median']:+.2%}</td><td>{a['hit']:.0%}</td></tr>")
    rows.append("</tbody></table>")
    return f"<h3>{title}</h3>" + "".join(rows)


def _realized_section():
    """실거래 실현 라운드트립 요약 (review FIFO). 실패/무데이터면 빈 문자열(무해)."""
    try:
        import review
        recs = review.load_journals(real_only=True)
        fills = review.extract_fills(recs)
        rt = review.round_trips(fills)
        trips = rt.get("trips", [])
        if not trips:
            return ""
        total = sum(t["pnl"] for t in trips)
        wins = sum(1 for t in trips if t["pnl"] > 0)
        n = len(trips)
        best = max(trips, key=lambda t: t["pnl"])
        worst = min(trips, key=lambda t: t["pnl"])
        return (f"<h2>실현 라운드트립 (실거래 FIFO)</h2>"
                f"<div class='cards'>"
                f"<div class='card'><div class='k'>청산 트립</div><div class='v'>{n}</div></div>"
                f"<div class='card'><div class='k'>실현 P&L</div><div class='v {'pos' if total>=0 else 'neg'}'>${total:+,.2f}</div></div>"
                f"<div class='card'><div class='k'>승률</div><div class='v'>{wins/n:.0%}</div></div>"
                f"<div class='card'><div class='k'>미청산</div><div class='v'>{len(rt.get('open', {}))}</div></div>"
                f"</div>"
                f"<p class='muted'>베스트 {best['symbol']} ${best['pnl']:+,.2f} · 워스트 {worst['symbol']} ${worst['pnl']:+,.2f}</p>")
    except Exception as e:
        return f"<!-- realized section skipped: {e!r} -->"


def build_html(horizon=20, log_dirs=None) -> str:
    import selection_review as sr
    if log_dirs is None:
        log_dirs = _persona_log_dirs()
    _md, dims, meta = sr.run(horizon=horizon, log_dirs=log_dirs)
    ov = dims.get("_overall", {})
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    charts = []
    # 페르소나 성과
    per = dims.get("persona", {})
    if per:
        labs = sorted(per, key=lambda k: -per[k]["avg"])
        c = _bar_chart("페르소나(전략) 평균 사후수익", labs, [per[l]["avg"] for l in labs], "avg 내림차순")
        if c:
            charts.append(c)
    # 신호 태그 in/out
    for dim, title in (("canslim", "CANSLIM"), ("analyst", "애널리스트매수"), ("momentum_only", "모멘텀-only")):
        d = dims.get(dim, {})
        labs = [b for b in ("in", "out") if b in d]
        if len(labs) >= 1:
            c = _bar_chart(f"{title} 태그 평균 사후수익", [f"{title}:{l}" for l in labs], [d[l]["avg"] for l in labs])
            if c:
                charts.append(c)
    # 레짐
    reg = dims.get("regime", {})
    if reg:
        labs = sorted(reg, key=lambda k: -reg[k]["avg"])
        c = _bar_chart("시장 레짐 평균 사후수익", labs, [reg[l]["avg"] for l in labs])
        if c:
            charts.append(c)

    chart_html = "".join(f"<img class='chart' src='data:image/png;base64,{c}'/>" for c in charts)

    overall_html = ""
    if ov.get("n"):
        overall_html = (f"<div class='cards'>"
                        f"<div class='card'><div class='k'>평가 픽</div><div class='v'>{meta['n_eval']}</div></div>"
                        f"<div class='card'><div class='k'>평균 사후수익</div><div class='v {'pos' if ov['avg']>=0 else 'neg'}'>{ov['avg']:+.2%}</div></div>"
                        f"<div class='card'><div class='k'>중앙값</div><div class='v'>{ov['median']:+.2%}</div></div>"
                        f"<div class='card'><div class='k'>승률</div><div class='v'>{ov['hit']:.0%}</div></div>"
                        f"<div class='card'><div class='k'>pending</div><div class='v'>{meta['pending']}</div></div>"
                        f"</div>")
    else:
        overall_html = "<p class='muted'>아직 H 거래일 사후 데이터가 쌓인 픽이 없음 (시간 지나면 누적).</p>"

    tables = "".join([
        _dim_table(dims, "persona", "페르소나 (전략 비교)"),
        _dim_table(dims, "piotroski", "Piotroski 점수"),
        _dim_table(dims, "score", "총점(scores)"),
        _dim_table(dims, "canslim", "CANSLIM 태그"),
        _dim_table(dims, "analyst", "애널리스트 매수"),
        _dim_table(dims, "momentum_only", "모멘텀-only"),
        _dim_table(dims, "regime", "시장 레짐"),
    ])

    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>신호 성과 리포트</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, "Malgun Gothic", sans-serif; margin: 0; padding: 16px; max-width: 860px; margin: 0 auto; background:#0f1115; color:#e6e6e6; }}
h1 {{ font-size: 1.4rem; }} h2 {{ font-size: 1.15rem; margin-top: 1.8rem; border-bottom:1px solid #333; padding-bottom:4px; }}
h3 {{ font-size: 1rem; margin-top: 1.2rem; color:#9ad; }}
.muted {{ color:#8a8f98; font-size:.9rem; }}
.cards {{ display:flex; flex-wrap:wrap; gap:10px; margin:12px 0; }}
.card {{ background:#1a1d24; border:1px solid #2a2f3a; border-radius:10px; padding:10px 14px; min-width:96px; }}
.card .k {{ font-size:.72rem; color:#8a8f98; }} .card .v {{ font-size:1.15rem; font-weight:600; }}
.chart {{ max-width:100%; background:#fff; border-radius:8px; margin:8px 0; }}
table {{ border-collapse:collapse; width:100%; margin:6px 0 14px; font-size:.9rem; }}
th,td {{ text-align:right; padding:5px 8px; border-bottom:1px solid #262b34; }}
th:first-child, td:first-child {{ text-align:left; }}
.pos {{ color:#4ec9a0; }} .neg {{ color:#f08a6a; }}
.banner {{ background:#2a2410; border:1px solid #6b5a1e; border-radius:8px; padding:8px 12px; font-size:.85rem; color:#e0c86a; }}
</style></head><body>
<h1>📊 신호 성과 사후추적 <span class="muted">H={horizon} 거래일 · {ts}</span></h1>
<div class="banner">⚠️ 관측·리포트 전용. 전략/신호/한도는 이 결과로 <b>자동변경되지 않음</b> — 사람이 백테스트로 판단.</div>
<h2>전체</h2>
{overall_html}
{_realized_section()}
<h2>차트</h2>
{chart_html or "<p class='muted'>표시할 차트 없음.</p>"}
<h2>신호 차원별 집계</h2>
{tables}
</body></html>"""


def build_and_write_dashboard(horizon=20):
    """대시보드용 report.html 생성 — build_data 훅에서 호출(guarded). 경로/에러 반환."""
    html = build_html(horizon=horizon)
    os.makedirs(os.path.dirname(DASH_REPORT), exist_ok=True)
    tmp = f"{DASH_REPORT}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, DASH_REPORT)
    return DASH_REPORT


def main():
    ap = argparse.ArgumentParser(description="신호 성과 HTML 리포트 (report-only)")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--out", default=DASH_REPORT, help="출력 HTML 경로")
    a = ap.parse_args()
    html = build_html(horizon=a.horizon)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"OK  {a.out}  ({len(html):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
