"""selection_review.py — 신호 성과 사후추적 (report-only).

runs.jsonl 의 selection 팩트(canslim/piotroski/analyst/momentum_only/score/regime)를 픽별로 뽑아
**진입 세션 대비 H 거래일 사후 수익률**을 붙이고, 신호 차원별로 집계해 "어떤 신호가 실제로 돈 됐나"를
사람이 검토할 마크다운 리포트로 낸다.

안전경계 (review.py 의 cost_buffer 자동튜닝과 명확히 분리):
  - **전략·신호·리스크 한도 절대 자동변경 안 함.** 이 모듈은 순수 관측·리포트.
  - 실거래 경로(run_live/exit/panic)와 무관 — 언제 실행해도 거래에 영향 0. DM-free.
  - paper(모의매매) 픽도 포함 — 선택은 브로커 무관(실행만 모의, 픽·사후수익은 실데이터). review 슬리피지 감사만 real-only.
  - 실패 graceful (데이터 부족·조회실패 픽은 pending 으로 집계 제외, throw 안 함).
  - 신규 API 키 불필요 (data.py = 기존 yfinance 캐시 재사용).

  python selection_review.py                 # 기본 H=20 거래일, LOG_DIR/selection_review/<date>.md 기록
  python selection_review.py --horizon 5     # 단기
  python selection_review.py --no-write       # 파일 안 쓰고 stdout 만
"""
import argparse
import json
import math
from datetime import date
from pathlib import Path

from paths import LOG_DIR
from review import load_journals, _num, _norm

DEFAULT_HORIZON = 20   # 거래일 (≈1개월)


def load_picks(recs=None, real_only=False) -> list:
    """레코드 → 픽 관측 리스트. 각 = {session, ticker, score, piotroski, canslim, analyst, momentum, regime}.

    각 (세션, final 종목) 1관측. 같은 종목이 여러 세션에 뽑히면 각각 별개 관측(재진입=새 베팅).

    real_only=False(기본): paper(모의매매) 런도 포함 — 선택(픽)은 브로커 무관이라(픽=실 신호,
    사후수익=실 시장가) paper 픽도 신호 성과추적에 유효. review.py 실거래 슬리피지 감사만 real_only=True.
    """
    if recs is None:
        recs = load_journals(real_only=real_only)
    out = []
    for r in recs:
        sel = r.get("selection") or {}
        final = sel.get("final") or []
        if not final:
            continue
        scores = sel.get("scores") or {}
        piotroski = sel.get("piotroski") or {}
        canslim = set(sel.get("canslim") or [])
        analyst = set(sel.get("analyst") or [])
        momentum = set(sel.get("momentum_only") or [])
        # 실제 진입 = 실행 후 보유 포지션. 미보유 final(레짐OFF 청산·미배분·미체결·partial 미체결)은
        # 유령 성과귀속 방지 위해 제외. positions 는 ok/tripped/error/partial 저널 모두에 있음(_acct_snapshot).
        held = {_norm(p.get("symbol")) for p in (r.get("positions") or [])}
        has_pos = "positions" in r             # 레거시·skip·crash 레코드는 키 없음 → 필터 미적용(유지)
        regime = ((r.get("risk") or {}).get("regime")) or ""
        persona = r.get("persona") or "real"   # paper 페르소나 태그(buffett/wood/oneil) — 없으면 real
        session = r.get("session", "")
        for t in final:
            if has_pos and _norm(t) not in held:   # 실제 미보유 픽 제외(미진입·청산·미체결)
                continue
            out.append({
                "session": session, "ticker": _norm(t),
                "score": scores.get(t),
                "piotroski": piotroski.get(t),
                "canslim": t in canslim, "analyst": t in analyst,
                "momentum": t in momentum, "regime": regime, "persona": persona,
            })
    return out


def _default_closes(ticker, entry_session):
    """진입 세션부터 오늘까지 종가 리스트 (data.py 캐시/yfinance). closes[0]=진입종가."""
    import data
    df = data.load(ticker, entry_session, str(date.today()))
    return [float(x) for x in df["Close"].tolist()]


def forward_return(closes, horizon):
    """closes[0]=진입종가, closes[horizon]=H거래일 후 종가 → 수익률. 데이터 부족이면 None(pending)."""
    if not closes or len(closes) < horizon + 1 or closes[0] <= 0:
        return None
    return closes[horizon] / closes[0] - 1.0


def evaluate(picks, horizon=DEFAULT_HORIZON, closes_fn=None):
    """각 픽에 사후수익률 부착. 데이터 부족/조회실패 픽은 제외(pending). 반환=(평가된 리스트, pending수)."""
    closes_fn = closes_fn or _default_closes
    out, pending = [], 0
    for p in picks:
        try:
            closes = closes_fn(p["ticker"], p["session"])
        except Exception:
            closes = None
        ret = forward_return(closes, horizon)
        if ret is None:
            pending += 1
            continue
        out.append({**p, "fwd_return": ret})
    return out, pending


def _dr_closes(ticker, session):
    """DR용 과거 이력 — 세션 당일까지의 종가 (forward 구간 아님, _default_closes 와 방향이 반대).

    캐시키가 {ticker}_{start} 라 start 가 세션마다 바뀌면 CSV 무한 누적(M-A 재발) —
    start 를 '세션 전년 1월 1일'로 스냅해 연 1회만 새 키."""
    import data
    from datetime import date as _d, timedelta
    try:
        s = _d.fromisoformat(str(session)[:10])
    except ValueError:
        s = _d.today()
    start = f"{s.year - 1}-01-01"
    end = (s + timedelta(days=1)).isoformat()      # data.load end 는 exclusive → 세션 당일 포함
    df = data.load(ticker, start, end)
    return [float(x) for x in df["Close"].tolist()]


def portfolio_dr(recs, closes_fn=None):
    """페르소나별 '현재 보유' 분산비율(DR) — 관찰 전용, throw 안 함.

    각 페르소나의 마지막 positions 스냅샷(qty>0) → 평가액 비중(qty×최근종가) → DR.
    반환 {persona: {"dr","port_vol","wavg_vol","n_used","n_total","session"}} — 계산불가 페르소나 제외.
    DR≈1 = 티커만 다른 사실상 한 베팅 (HOUSE.md §3 섹터한도 부재 보완 지표).
    """
    closes_fn = closes_fn or _dr_closes
    out = {}
    try:
        import diversification as dv
        last_by_persona = {}
        for r in recs or []:
            pos = [p for p in (r.get("positions") or [])
                   if p.get("symbol") and float(p.get("qty", 0) or 0) > 0]
            if pos:
                last_by_persona[r.get("persona") or "real"] = (r.get("session", ""), pos)
        for persona, (session, pos) in last_by_persona.items():
            weights, closes = {}, {}
            for p in pos:
                t = _norm(p.get("symbol"))
                qty = float(p.get("qty", 0) or 0)
                try:
                    c = closes_fn(t, session) or []
                except Exception:
                    c = []
                if c:
                    closes[t] = c
                # 종가 없으면 평단으로 비중만 잡음 — div_ratio 가 데이터부족으로 제외하되 n_total 에 반영
                px = c[-1] if c else float(p.get("avg", 0) or 0)
                if px > 0:
                    weights[t] = qty * px
            d = dv.div_ratio(weights, closes)
            if d:
                out[persona] = {**d, "session": session}
    except Exception:
        return out
    return out


def intraday_interference(recs) -> dict:
    """A12 — 일1런이 장중 취득 포지션을 되돌리는 빈도·규모 계측 (관찰 전용, 거래 로직 무접촉).

    공유책 페르소나(intraday=True ∧ daily_run=True — 현재 oneil·wood, `personas.py`)만 이
    시나리오가 성립한다: 장중 액티브 룰이 연 포지션을, 다음 일1런 리밸런스가 매도/트림한다.
    세션순으로 훑으며 직전 장중 스냅샷의 positions(브로커 실보유)를 held 로 들고 있다가,
    다음 일1런 레코드의 SELL 주문 심볼과 교집합을 낸다(전량청산·트림 구분 없이 SELL 이면 매도/트림).

    ponytail: rule_state 파일(intraday_rules_state_*.json)은 persist_state 페르소나
    (livermore_swing) 전용인데 그 페르소나는 daily_run 이 없어 이 시나리오 자체가 성립하지
    않는다 — 장중 저널의 positions(ground truth)만으로 충분해 별도 리더를 안 만든다.

    반환 {persona: {"n_daily","n_interfered","rate","tickers":{sym:n}}} — **장중 스냅샷을 한 번이라도
    낸 페르소나만**(시나리오 자체가 성립하는 대상). daily_run 뿐이고 intraday 가 없는 페르소나
    (buffett 등)는 간섭이 구조적으로 불가능하므로 "0%"가 아니라 아예 제외한다.
    """
    by_persona = {}
    for r in recs or []:
        p = r.get("persona")
        if not p:
            continue
        by_persona.setdefault(p, []).append(r)
    out = {}
    for persona, prs in by_persona.items():
        prs = sorted(prs, key=lambda r: (r.get("session", ""), r.get("ts", "")))
        held, n_daily, n_hit, hits, saw_intraday = set(), 0, 0, {}, False
        for r in prs:
            if r.get("intraday"):
                held = {_norm(p2.get("symbol")) for p2 in (r.get("positions") or []) if p2.get("symbol")}
                saw_intraday = True
                continue
            if "selection" not in r:      # 장중 스냅샷 외·locked/already_ran/hold 등 admin no-op 은 제외
                continue
            n_daily += 1
            sold = {_norm(o.get("symbol")) for o in (r.get("orders") or [])
                    if o.get("side") == "SELL" and o.get("symbol")}
            touched = sold & held
            if touched:
                n_hit += 1
                for t in touched:
                    hits[t] = hits.get(t, 0) + 1
        if n_daily and saw_intraday:
            out[persona] = {"n_daily": n_daily, "n_interfered": n_hit,
                            "rate": n_hit / n_daily, "tickers": hits}
    return out


def _median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _agg(obs):
    """관측 묶음 → {n, avg, median, hit(승률)}. 수익률은 소수(0.05=+5%)."""
    rets = [o["fwd_return"] for o in obs]
    n = len(rets)
    if n == 0:
        return {"n": 0, "avg": 0.0, "median": 0.0, "hit": 0.0}
    return {"n": n, "avg": sum(rets) / n, "median": _median(rets),
            "hit": sum(1 for r in rets if r > 0) / n}


def _pio_bucket(p):
    v = p.get("piotroski")
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):       # 비숫자(손상 저널) → graceful('throw 안 함' 계약, _score_bucket 대칭)
        return "n/a"
    if not math.isfinite(v):              # NaN → '≤5' 오분류 방지
        return "n/a"
    return "≥8" if v >= 8 else ("6-7" if v >= 6 else "≤5")


def _score_bucket(p):
    """점수 버킷 — 스케일 혼재 처리. canslim 정수점수(0/1/2)는 그대로,
    buffett/wood 연속 z-score 합은 int() 절단(부호 소실·0버킷 붕괴) 대신 0.5폭 부호보존 구간."""
    v = p.get("score")
    if v is None:
        return "n/a"
    try:
        v = float(v)
    except (TypeError, ValueError):       # 비숫자 문자열(손상 저널) → float() ValueError 흡수(isfinite 前)
        return "n/a"
    if not math.isfinite(v):              # NaN/inf(손상 저널 등) → graceful('throw 안 함' 계약 유지)
        return "n/a"
    # 연속 z-score 전략(buffett/wood)은 우연히 정수로 반올림돼도 canslim 정수버킷('0'/'1')과 안 섞이게
    # 항상 구간 라벨. canslim/real 정수 카운트(0/1/2)만 정수 라벨.
    # ⚠️ 연속 z 엔진 페르소나 목록 — 신규 추가 시 여기도 넣어야 한다(빠지면 점수가 우연히 정수로
    # 반올림된 날만 canslim 정수버킷에 섞여 A/B 비교표가 조용히 오염된다). buffett_v2 = buffett 과
    # 같은 연속 z 엔진.
    continuous = p.get("persona") in ("buffett", "buffett_v2", "wood")
    if not continuous and abs(v - round(v)) < 1e-9:   # 정수값(canslim 카운트) → 그대로
        return str(int(round(v)))
    lo = math.floor(v * 2) / 2.0          # 0.5 폭 구간 (부호 보존)
    return f"{lo:+.1f}~{lo + 0.5:+.1f}"


def bucketize(evaluated):
    """신호 차원별 버킷 집계. dims[차원][버킷] = {n, avg, median, hit}. dims['_overall'] = 전체."""
    dim_fns = {
        "persona": lambda p: p.get("persona") or "real",   # 페르소나(전략) 비교 — 핵심 차원
        "piotroski": _pio_bucket,
        "score": _score_bucket,
        "canslim": lambda p: "in" if p["canslim"] else "out",
        "analyst": lambda p: "in" if p["analyst"] else "out",
        "momentum_only": lambda p: "in" if p["momentum"] else "out",
        "regime": lambda p: p.get("regime") or "n/a",
    }
    dims = {}
    for dim, fn in dim_fns.items():
        buckets = {}
        for o in evaluated:
            buckets.setdefault(fn(o), []).append(o)
        dims[dim] = {b: _agg(obs) for b, obs in buckets.items()}
    dims["_overall"] = _agg(evaluated)
    return dims


def render_report(dims, horizon, n_picks, n_eval, pending, dr_map=None, interference_map=None) -> str:
    """마크다운 리포트. 사람이 '어떤 신호가 돈 됐나' 한눈에."""
    L = [f"# 신호 성과 사후추적 (H={horizon} 거래일)", ""]
    L.append(f"- 픽 관측 {n_picks} · 평가 {n_eval} · pending(데이터부족/조회실패) {pending}")
    if interference_map:
        L.append("")
        L.append("## 일1런 vs 장중 간섭 (A12 — 관찰 전용, 거래 로직 무변경)")
        L.append("> 일1런 리밸런스가 장중 액티브 룰이 보유 중이던 종목을 매도/트림한 빈도·규모.")
        L.append("")
        L.append("| 페르소나 | 일1런 n | 간섭 n | 빈도 | 간섭 종목(횟수) |")
        L.append("|---|---|---|---|---|")
        for persona in sorted(interference_map):
            d = interference_map[persona]
            tks = ", ".join(f"{t}×{n}" for t, n in sorted(d["tickers"].items(), key=lambda kv: -kv[1]))
            L.append(f"| {persona} | {d['n_daily']} | {d['n_interfered']} | {d['rate']:.0%} | {tks or '—'} |")
    if dr_map:
        L.append("")
        L.append("## 현재 포트폴리오 분산비율 (DR — 관찰 전용)")
        L.append("> DR≈1 = 티커만 다른 사실상 한 베팅 · DR² ≈ 유효 독립베팅 수. "
                 "섹터 한도가 코드에 없으므로(HOUSE.md §3) 이 표가 중복베팅 육안점검 보조.")
        L.append("")
        L.append("| 페르소나 | 세션 | 보유(계산/전체) | DR | 유효베팅≈DR² | 포트σ | 가중평균σ |")
        L.append("|---|---|---|---|---|---|---|")
        for persona in sorted(dr_map, key=lambda k: dr_map[k]["dr"]):
            d = dr_map[persona]
            L.append(f"| {persona} | {d['session']} | {d['n_used']}/{d['n_total']} | {d['dr']:.2f} "
                     f"| {d['dr'] ** 2:.1f} | {d['port_vol']:.1%} | {d['wavg_vol']:.1%} |")
    ov = dims.get("_overall", {})
    if ov.get("n"):
        L.append(f"- 전체 평균 사후수익 {ov['avg']:+.2%} · 중앙값 {ov['median']:+.2%} · 승률 {ov['hit']:.0%}")
    L.append("")
    L.append("> ⚠️ 관측·리포트 전용. 전략/신호는 이 결과로 **자동변경 안 됨** — 사람이 판단(A엔진 백테스트로 수동).")
    L.append("")
    if not ov.get("n"):
        L.append("아직 H 거래일 사후 데이터가 쌓인 픽이 없음 (시간 지나면 누적).")
        return "\n".join(L)
    order = ["persona", "piotroski", "score", "canslim", "analyst", "momentum_only", "regime"]
    titles = {"persona": "페르소나 (전략 비교 — avg 내림차순 = 성과 순위)",
              "piotroski": "Piotroski 점수", "score": "총점(scores)", "canslim": "CANSLIM 태그",
              "analyst": "애널리스트 매수", "momentum_only": "모멘텀-only", "regime": "시장 레짐"}
    for dim in order:
        L.append(f"## {titles[dim]}")
        L.append("| 버킷 | n | 평균수익 | 중앙값 | 승률 |")
        L.append("|---|---|---|---|---|")
        for b in sorted(dims[dim], key=lambda k: -dims[dim][k]["avg"]):
            a = dims[dim][b]
            L.append(f"| {b} | {a['n']} | {a['avg']:+.2%} | {a['median']:+.2%} | {a['hit']:.0%} |")
        L.append("")
    return "\n".join(L)


def run(horizon=DEFAULT_HORIZON, recs=None, closes_fn=None, real_only=False, log_dirs=None):
    """로드→평가→집계→렌더. (report_md, dims, meta) 반환. throw 안 함. real_only=False=paper 포함.

    log_dirs=홈 logs 리스트면 여러 home(페르소나 별도 home) 저널을 합쳐 비교 — 각 픽의 persona 태그로 구분.
    """
    if recs is None and log_dirs:
        recs = []
        seen = set()
        for d in log_dirs:
            try:
                rp = Path(d).resolve()
            except Exception:
                rp = d
            if rp in seen:
                continue   # 같은 home 중복 집계 방지 (기본 home 이 PERSONA_HOMES 에 들면 픽 2배 왜곡)
            seen.add(rp)
            try:
                recs.extend(load_journals(log_dir=d, real_only=real_only))
            except Exception:
                pass
    if recs is None:
        recs = load_journals(real_only=real_only)   # 1회 로드 — 픽 평가와 DR 이 같은 레코드 공유
    picks = load_picks(recs, real_only=real_only)
    evaluated, pending = evaluate(picks, horizon, closes_fn)
    dims = bucketize(evaluated)
    dr_map = portfolio_dr(recs, closes_fn)          # 관찰 전용 — 실패 시 빈 dict(graceful)
    interference_map = intraday_interference(recs)  # A12 — 관찰 전용, 거래 로직 무접촉
    meta = {"n_picks": len(picks), "n_eval": len(evaluated), "pending": pending,
            "horizon": horizon, "dr": dr_map, "interference": interference_map}
    md = render_report(dims, horizon, len(picks), len(evaluated), pending,
                       dr_map=dr_map, interference_map=interference_map)
    return md, dims, meta


def main():
    ap = argparse.ArgumentParser(description="신호 성과 사후추적 (report-only, 전략 자동변경 없음)")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON, help="사후 수익 측정 거래일 (기본 20)")
    ap.add_argument("--no-write", action="store_true", help="파일 안 쓰고 stdout 만")
    a = ap.parse_args()
    md, _dims, meta = run(horizon=a.horizon)
    print(md)
    if not a.no_write:
        try:
            out_dir = LOG_DIR / "selection_review"
            out_dir.mkdir(parents=True, exist_ok=True)
            f = out_dir / f"{date.today().isoformat()}_h{a.horizon}.md"
            f.write_text(md, encoding="utf-8")
            print(f"\n[기록] {f}")
        except Exception as e:
            print(f"\n[기록 실패 — 무해] {e!r}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
