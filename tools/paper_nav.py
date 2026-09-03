"""paper NAV vs SPY 총수익 산출 — DoD A6 의 "주 1회 명령 1개".

runs.jsonl 의 paper ok-런에서 NAV(equity) 시계열을 뽑아 §B 지표를 출력한다:
  1. 누적 NAV 수익률 − 동기간 SPY 총수익 (초과수익 %p)
  2. MDD (paper NAV, seed 포함 기준)

  python tools/paper_nav.py                     # 전 기간 (pre-T0 shakedown 포함)
  python tools/paper_nav.py --since 2026-07-23  # T0 이후만 (§B 집계 창)
  python tools/paper_nav.py --selftest          # 네트워크·파일 없이 산식 자가검증

규약:
  - NAV 기준선 = fresh seed $100k (--base). SPY 기준선 = 첫 집계 세션 직전 종가
    (같은 시점에 SPY 를 샀다면). SPY 총수익 = auto_adjust 종가 비율(배당 반영).
  - ⚠ Claude 세션(MSIX 컨테이너)에서 실행 금지 — 캐시/저널 경로가 오버레이로 갈라진다.
    사용자 셸 또는 스케줄 태스크에서 실행할 것 (lessons/2026-07-11-claude-msix-overlay.md).
"""
import argparse
import json
import os
import statistics
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 루트 모듈(paths·data) import


def load_paper_navs(log_dir):
    """runs.jsonl(+.1) → (navs, cash) — paper·ok 만, 세션당 마지막 레코드 승리.
    navs = {session_date: equity}. cash = {session_date: weights 합==0}(§B-6 레짐 주의 트리거 표본,
    누락 weights 는 빈 dict 취급 → 합 0 = cash 로 fail-closed 판정)."""
    navs = {}
    cash = {}
    for name in ("runs.jsonl.1", "runs.jsonl"):        # 오래된 파일 먼저 → 최신이 덮어씀
        p = log_dir / name
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                                # 손상 라인 스킵 (A5 무결 감시는 별도)
            if r.get("broker") != "paper" or r.get("status") != "ok":
                continue
            eq = (r.get("account") or {}).get("equity")
            try:
                s = date.fromisoformat(r.get("session") or "")
            except ValueError:
                continue
            if isinstance(eq, (int, float)) and eq > 0:
                navs[s] = float(eq)
                w = r.get("weights")
                cash[s] = sum(w.values()) == 0 if isinstance(w, dict) else True
    return dict(sorted(navs.items())), dict(sorted(cash.items()))


def metrics(navs, base, spy_base, spy_last):
    """(nav_ret, spy_tr, excess_pp, mdd) — navs 는 session→equity 정렬 dict."""
    eqs = [base] + list(navs.values())                  # seed 포함 — 첫 런부터의 낙폭도 MDD 에 반영
    nav_ret = eqs[-1] / base - 1.0
    spy_tr = spy_last / spy_base - 1.0
    peak, mdd = eqs[0], 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1.0)
    return nav_ret, spy_tr, (nav_ret - spy_tr) * 100.0, mdd


def excess_series(navs, base, spy_base, spy_by_date):
    """§B-5 표본 — 연속 세션쌍마다 d = NAV 등락률 − SPY 등락률. 첫 쌍은 (base, spy_base) 사용
    (metrics() 기준선 규약과 동일). SPY 종가는 세션 일자 키로 spy_by_date 에서 조회 — 결측 세션은
    그 쌍을 스킵(양쪽 동시, §B 부록 1)."""
    eqs = [base] + list(navs.values())
    spies = [spy_base] + [spy_by_date.get(s) for s in navs]
    d = []
    for i in range(1, len(eqs)):
        s0, s1 = spies[i - 1], spies[i]
        if s0 is None or s1 is None:
            continue
        d.append((eqs[i] / eqs[i - 1] - 1.0) - (s1 / s0 - 1.0))
    return d


def tstat(d):
    """(t, ir_ann) — §B-5: t = mean/(stdev/√n), ir_ann = mean/stdev×√252. n<40 또는 stdev==0 이면
    None(표본 부족·무변동 — 판정 불가). stdlib statistics 만 사용(신규 의존 0)."""
    n = len(d)
    if n < 40:
        return None
    sd = statistics.stdev(d)
    if sd == 0:
        return None
    m = statistics.mean(d)
    return m / (sd / n ** 0.5), m / sd * 252 ** 0.5


def _rho1(d):
    """lag-1 자기상관 — 보고용(§B-5 HAC 미적용 사전등록, 판정선 없음). 계산 불가(n<2·무변동)면 None."""
    try:
        return statistics.correlation(d[:-1], d[1:])
    except statistics.StatisticsError:
        return None


def selftest():
    navs = {date(2026, 1, 5): 101_000.0, date(2026, 1, 6): 97_000.0, date(2026, 1, 7): 103_000.0}
    nav_ret, spy_tr, excess, mdd = metrics(navs, 100_000.0, 500.0, 510.0)
    assert abs(nav_ret - 0.03) < 1e-9, nav_ret
    assert abs(spy_tr - 0.02) < 1e-9, spy_tr
    assert abs(excess - 1.0) < 1e-9, excess
    assert abs(mdd - (97_000.0 / 101_000.0 - 1.0)) < 1e-9, mdd          # 101k 고점 → 97k
    assert metrics({date(2026, 1, 5): 99_000.0}, 100_000.0, 500.0, 500.0)[3] == 99_000.0 / 100_000.0 - 1.0

    # tstat — 알려진 d 시계열 손계산 대조(mean 0.01025, SE 0.00025 → t=41.0 정확히 나눠떨어짐)
    assert abs(tstat([0.01] * 39 + [0.02])[0] - 41.0) < 1e-9, tstat([0.01] * 39 + [0.02])
    assert tstat([1.0] * 50) is None                    # 상수 시계열 → stdev=0 → None
    assert tstat([0.01] * 39) is None                    # n=39<40 → None
    print("selftest OK")


def main():
    ap = argparse.ArgumentParser(description="paper NAV vs SPY total return (DoD A6)")
    ap.add_argument("--since", type=date.fromisoformat, default=None, help="T0 등 집계 시작 세션(포함)")
    ap.add_argument("--base", type=float, default=100_000.0, help="NAV 기준선 (fresh seed)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        selftest()
        return 0

    from paths import LOG_DIR                           # 지연 import — selftest 는 환경 무접촉
    import data as D

    navs, cash = load_paper_navs(LOG_DIR)
    if args.since:
        navs = {s: e for s, e in navs.items() if s >= args.since}
        cash = {s: c for s, c in cash.items() if s >= args.since}
    if not navs:
        print("paper ok-런 없음 (창에 레코드 0) — 판정 불가")
        return 1

    first, last = min(navs), max(navs)
    closes = D.load("SPY", start=str(first - timedelta(days=10)), end=str(last + timedelta(days=1)))["Close"]
    before = closes[[d.date() < first for d in closes.index]]
    spy_base = float(before.iloc[-1]) if len(before) else float(closes.iloc[0])
    spy_last = float(closes[[d.date() <= last for d in closes.index]].iloc[-1])
    spy_by_date = {ts.date(): float(px) for ts, px in closes.items()}

    nav_ret, spy_tr, excess, mdd = metrics(navs, args.base, spy_base, spy_last)
    d_series = excess_series(navs, args.base, spy_base, spy_by_date)
    t_ir = tstat(d_series)
    t_val, ir_val = t_ir if t_ir else (None, None)
    rho1 = _rho1(d_series)
    cash_n = sum(cash.values())

    print(f"창 {first}~{last} · 런 {len(navs)}회 · NAV {list(navs.values())[-1]:,.2f}")
    print(f"NAV 수익률 {nav_ret * 100:+.2f}% | SPY 총수익 {spy_tr * 100:+.2f}% | 초과 {excess:+.2f}%p | MDD {mdd * 100:.2f}%")
    t_str = f"{t_val:+.3f}" if t_val is not None else "n/a(n<40 또는 stdev=0)"
    ir_str = f"{ir_val:+.3f}" if ir_val is not None else "n/a"
    rho1_str = f"{rho1:+.3f}" if rho1 is not None else "n/a"
    print(f"t {t_str} | IR_ann {ir_str} | n_pairs {len(d_series)} | ρ1 {rho1_str} | cash세션 {cash_n}")
    print(json.dumps({"first": str(first), "last": str(last), "runs": len(navs),
                      "nav_ret": round(nav_ret, 6), "spy_tr": round(spy_tr, 6),
                      "excess_pp": round(excess, 4), "mdd": round(mdd, 6),
                      "n_pairs": len(d_series),
                      "t": round(t_val, 4) if t_val is not None else None,
                      "ir_ann": round(ir_val, 4) if ir_val is not None else None,
                      "rho1": round(rho1, 4) if rho1 is not None else None,
                      "cash_sessions": cash_n}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
