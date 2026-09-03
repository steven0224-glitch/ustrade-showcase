"""자기검토·재귀검증·안전 파라미터 자동튜닝 — 봇이 자기 매매기록으로 자신을 감사하고 발전한다.

사용자 결정(자율수준) = **안전 파라미터 자동튜닝(범위제한)**. 그래서:
  1. 기록 재구성  — runs/exits/panics.jsonl 의 체결을 FIFO 라운드트립으로 묶어 실현 P&L 산출.
  2. 재귀 안전검증 — 누적된 전체 매매가 안전 불변식을 지켰는지 매번 재감사
       (보호종목 불가침 · 체결가>0 · reconcile 드리프트 · 멱등 더블바이). 위반 시 텔레그램 CRITICAL.
  3. 성과 포스트모템 — 승률 · 실현 슬리피지 · 가드트립 · 체결률.
  4. 자동튜닝 — **cost_buffer 하나만**, 실현 매수 슬리피지 기반. 거래 전략·신호·리스크 한도는
       절대 자동변경 안 함(그건 사람 몫 — 제안만).

자동튜닝 안전 envelope (이 한도를 넘는 자동변경은 구조적으로 불가):
  - 대상은 cost_buffer(매수 현금 쿠션)뿐 — '무엇을/언제 거래할지'엔 영향 0. 망가져도 사고 안 남
    (최악=현금 약간 더/덜 예약 → 주식 1주 차이).
  - 신호 = (체결가-사이징기준가)/기준가 = 순수 시장가 슬리피지(시장이동 오염 없음).
  - MIN_SAMPLE 미만이면 튜닝 안 함(소표본 과적합 차단). 하드클램프 [BUF_MIN,BUF_MAX]. 1회 ±BUF_STEP.
  - 변경 시 tuning.jsonl 기록 + 텔레그램 통지. run_live 가 tuning.json 을 읽어 적용.

  python review.py            # 검토+검증+튜닝 적용 + 리포트
  python review.py --dry-tune # 튜닝 제안만(미적용)
  python review.py --no-tune  # 리포트만(튜닝 스킵)
"""
import argparse
import json
import sys
from datetime import datetime

from paths import LOG_DIR, STATE_DIR
from notify import notify

# ── 자동튜닝 안전 한도 (이 상수들이 자율의 울타리) ──────────────────────────────
BUF_MIN, BUF_MAX = 0.003, 0.010   # cost_buffer 하드 클램프 (0.3% ~ 1.0%)
BUF_DEFAULT = 0.005               # 기본 0.5% (튜닝 전·표본부족 시)
BUF_STEP = 0.002                  # 1회 변경 최대폭 (0.2%p) — 급변 방지
MIN_SAMPLE = 8                    # 슬리피지 표본 최소 건수 — 미만이면 튜닝 안 함(과적합 차단)
SLIP_PCTL = 90                    # 매수 슬리피지 분포의 이 분위수를 버퍼 목표로(보수적 커버)

TUNING_FILE = STATE_DIR / "tuning.json"
TUNING_LOG = STATE_DIR / "tuning.jsonl"
SLEEVE_FILE = STATE_DIR / "toss_sleeve.json"


# 티커 개명 선례(BK→BNY, 2026-07) — 별칭을 정규 심볼로 통일해 FIFO 라운드트립이 개명 전후로
# 끊기지 않게 한다(다른 문자열 키라 별개 lots 큐가 돼 매칭이 깨짐). 신규 개명 발생 시 여기 추가
# (구→신 매핑). universe.py(거래 유니버스 신원)는 별개 관심사라 건드리지 않음 — 이건 감사용 정규화.
SYMBOL_ALIASES = {"BK": "BNY"}


def _norm(s) -> str:
    n = str(s).strip().upper().replace(".", "-")
    return SYMBOL_ALIASES.get(n, n)


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):   # NaN(자기불일치)·inf 거부 — fill 가드·INV-2 우회 차단
        return None
    return v


def _pctl(xs, p) -> float:
    """선형보간 분위수 (numpy 의존 회피). xs 비면 0."""
    s = sorted(xs)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    frac = k - lo
    if lo + 1 >= len(s):
        return s[lo]
    return s[lo] + (s[lo + 1] - s[lo]) * frac


# ── 기록 로드 ─────────────────────────────────────────────────────────────────
def load_journals(log_dir=LOG_DIR, real_only=True) -> list:
    """runs/exits/panics.jsonl (+.1 회전) 전부 읽어 시간순 레코드 리스트. 파일 없으면 빈 리스트.

    real_only=True(기본): 페이퍼(broker=='paper') 런 제외 — 실거래 감사가 dev 테스트 기록에
    오염되지 않게(같은 세션 반복 페이퍼 백테스트가 '더블바이'로 오탐되던 것 차단). exits/panics 는
    토스 전용이라 broker 필드 없이도 실거래.
    """
    recs = []
    for name in ("runs.jsonl", "runs.jsonl.1", "exits.jsonl", "exits.jsonl.1",
                 "panics.jsonl", "panics.jsonl.1"):
        f = log_dir / name
        if not f.exists():
            continue
        src = name.split(".")[0]
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if real_only and r.get("broker") == "paper":
                continue
            r["_src"] = src
            recs.append(r)
    recs.sort(key=lambda r: (r.get("session", ""), r.get("ts", "")))
    return recs


def extract_fills(recs, invalid_out: list = None) -> list:
    """레코드들에서 체결(FILLED, fill>0) 만 추출 → 표준 fill 이벤트.

    invalid_out 이 리스트로 주어지면 fill<=0/qty<=0 로 걸러진 FILLED 레코드를 거기 append —
    기존엔 여기서 무음 제외돼 verify_invariants(INV-2)에 절대 안 넘어가 구조적으로 발동 불가였다.
    호출부(mcp_server/report_html)는 그냥 무음 필터만 필요하므로 기본 None(수집 안 함, 하위호환)."""
    fills = []
    for r in recs:
        for o in (r.get("orders") or []):
            if o.get("status") != "FILLED":
                continue
            fill = _num(o.get("fill"))
            qty = _num(o.get("qty"))
            if not fill or fill <= 0 or not qty or qty <= 0:
                if invalid_out is not None:
                    invalid_out.append({"session": r.get("session", ""), "ts": r.get("ts", ""),
                                        "symbol": _norm(o.get("symbol")), "side": o.get("side"),
                                        "fill": fill, "qty": qty})
                continue
            fills.append({
                "session": r.get("session", ""), "ts": r.get("ts", ""), "src": r.get("_src"),
                "symbol": _norm(o.get("symbol")), "side": o.get("side"),
                "qty": qty, "fill": fill, "ref": _num(o.get("ref")),
            })
    return fills


# ── 라운드트립 (FIFO) ─────────────────────────────────────────────────────────
def round_trips(fills) -> dict:
    """심볼별 FIFO 로 BUY↔SELL 매칭 → 닫힌 라운드트립 + 미청산 잔량.

    realized_pnl = matched_qty*(exit-entry). cost 신호로는 안 씀(시장이동 오염) — P&L 보고용.
    """
    from collections import defaultdict, deque
    lots = defaultdict(deque)   # symbol -> deque of [qty, price]
    trips, open_pos = [], {}
    for f in sorted(fills, key=lambda x: (x["session"], x["ts"])):
        sym = f["symbol"]
        if f["side"] == "BUY":
            lots[sym].append([f["qty"], f["fill"]])
        elif f["side"] == "SELL":
            rem = f["qty"]
            while rem > 1e-9 and lots[sym]:
                lot = lots[sym][0]
                take = min(rem, lot[0])
                trips.append({"symbol": sym, "qty": take, "entry": lot[1], "exit": f["fill"],
                              "pnl": take * (f["fill"] - lot[1]), "exit_session": f["session"]})
                lot[0] -= take
                rem -= take
                if lot[0] <= 1e-9:
                    lots[sym].popleft()
            # rem>0 (보호분/외부분 매도 등 봇 매수기록 없는 SELL) → 무시(봇 라운드트립 아님)
    for sym, dq in lots.items():
        q = sum(l[0] for l in dq)
        if q > 1e-9:
            open_pos[sym] = q
    return {"trips": trips, "open": open_pos}


# ── 재귀 안전검증 (매번 전체 기록 재감사) ──────────────────────────────────────
def load_protected() -> set:
    try:
        d = json.loads(SLEEVE_FILE.read_text(encoding="utf-8"))
        return {_norm(s) for s in d.get("protected", [])}
    except Exception:
        return set()


def verify_invariants(recs, fills, protected, invalid_fills=None) -> dict:
    """누적 매매가 안전 불변식을 지켰는지 자가감사. violations(중대) + incidents(관찰) 반환.

    invalid_fills: extract_fills(invalid_out=...) 가 fill<=0/qty<=0 로 걸러낸 레코드 목록(선택) —
    INV-2 가 fills 뿐 아니라 이 목록도 위반으로 본다(사전제외로 INV-2 를 우회하지 못하게)."""
    violations, incidents = [], []

    # INV-1: 보호종목은 봇이 절대 매매하면 안 됨 (슬리브 보호의 최후 사후검증)
    if protected:
        for f in fills:
            if f["symbol"] in protected:
                violations.append(f"보호종목 매매 발견: {f['side']} {f['symbol']} "
                                  f"{f['qty']:.0f}@{f['fill']} ({f['session']}) — 슬리브 보호 위반!")

    # INV-2: 체결가는 양수여야 함 (0/음수 체결가로 거짓 청산/사이징 방지)
    for f in fills:
        if not f["fill"] or f["fill"] <= 0:
            violations.append(f"비정상 체결가: {f['symbol']} fill={f['fill']} ({f['session']})")
    for f in (invalid_fills or []):   # extract_fills 가 사전제외한 fill<=0/qty<=0 레코드도 위반으로 표면화
        violations.append(f"비정상 체결 레코드(사전제외): {f['symbol']} fill={f['fill']} "
                          f"qty={f['qty']} ({f['session']})")

    # INV-3: reconcile 드리프트 (브로커 실제≠기대) — 사후 정합성 깨진 실행
    for r in recs:
        rc = r.get("reconcile")
        if isinstance(rc, dict) and rc.get("ok") is False:
            incidents.append(f"정합성 드리프트 {r.get('session')}: {rc.get('drift')}")

    # INV-4: 멱등 더블바이 — 같은 (세션·심볼·side·수량) BUY 가 서로 다른 실행에서 2회+ FILLED
    seen = {}
    for f in fills:
        if f["side"] != "BUY":
            continue
        key = (f["session"], f["symbol"], round(f["qty"]))
        seen.setdefault(key, set()).add(f["ts"])
    for key, tss in seen.items():
        if len(tss) > 1:
            violations.append(f"더블바이 의심: {key[1]} {key[2]}주 @ {key[0]} 가 {len(tss)}개 실행에서 체결 "
                              f"(멱등키가 막았어야 함)")

    # 관찰: 가드 트립/정지/크래시 빈도
    for r in recs:
        st = r.get("status")
        if st in ("tripped", "halted", "crash", "error", "partial"):
            incidents.append(f"{st} @ {r.get('session') or r.get('ts')}: {r.get('reason','')}"[:160])

    return {"violations": violations, "incidents": incidents}


# ── 성과 포스트모템 ───────────────────────────────────────────────────────────
def buy_slippages(fills) -> list:
    """FILLED 매수의 (체결가-기준가)/기준가 — 순수 시장가 슬리피지(양수=불리). ref 있는 것만."""
    out = []
    for f in fills:
        if f["side"] == "BUY" and f["ref"] and f["ref"] > 0 and f["fill"] and f["fill"] > 0:
            out.append((f["fill"] - f["ref"]) / f["ref"])
    return out


def postmortem(recs, fills, rt) -> dict:
    trips = rt["trips"]
    wins = [t for t in trips if t["pnl"] > 0]
    slips = buy_slippages(fills)
    n_orders = sum(len(r.get("orders") or []) for r in recs)
    n_filled = sum(1 for r in recs for o in (r.get("orders") or []) if o.get("status") == "FILLED")
    trips_count = len(trips)
    return {
        "runs": len([r for r in recs if r.get("_src") == "runs"]),
        "fills": len(fills),
        "round_trips": trips_count,
        "win_rate": (len(wins) / trips_count) if trips_count else None,
        "realized_pnl": round(sum(t["pnl"] for t in trips), 4),
        "open_positions": rt["open"],
        "buy_slip_n": len(slips),
        "buy_slip_med": round(_pctl(slips, 50), 5) if slips else None,
        "buy_slip_p90": round(_pctl(slips, 90), 5) if slips else None,
        "fill_rate": (n_filled / n_orders) if n_orders else None,
        "trips_detail": trips[-10:],
    }


# ── cost_buffer 범위제한 자동튜닝 ──────────────────────────────────────────────
def read_tuned_cost_buffer(default: float = BUF_DEFAULT) -> float:
    """run_live 가 호출 — tuning.json 의 cost_buffer(클램프) 반환, 없으면 default. 절대 throw 안 함."""
    try:
        v = _num(json.loads(TUNING_FILE.read_text(encoding="utf-8")).get("cost_buffer"))
        if v is None:
            return default
        return min(BUF_MAX, max(BUF_MIN, v))
    except Exception:
        return default


def compute_tune(slips, current: float) -> dict:
    """실현 매수 슬리피지 → 새 cost_buffer 제안. 안전 envelope 전부 적용.

    target = clamp(p90(양수 슬리피지), MIN, MAX) 를 current 에서 ±STEP 안으로만 이동.
    표본 MIN_SAMPLE 미만이면 변경 안 함.
    """
    pos = [max(0.0, s) for s in slips]
    n = len(pos)
    if n < MIN_SAMPLE:
        return {"changed": False, "current": current, "proposed": current, "n": n,
                "reason": f"표본 부족 (n={n} < {MIN_SAMPLE}) — 과적합 방지로 튜닝 보류"}
    target = min(BUF_MAX, max(BUF_MIN, _pctl(pos, SLIP_PCTL)))
    stepped = min(current + BUF_STEP, max(current - BUF_STEP, target))   # ±STEP 제한
    stepped = min(BUF_MAX, max(BUF_MIN, stepped))                         # 재클램프(안전)
    changed = abs(stepped - current) > 1e-6
    return {"changed": changed, "current": current, "proposed": round(stepped, 5), "n": n,
            "target_raw": round(target, 5),
            "reason": (f"실현 슬리피지 p{SLIP_PCTL}={target:.3%} → cost_buffer {current:.3%}→{stepped:.3%}"
                       if changed else f"이미 적정 ({current:.3%}, target {target:.3%})")}


def apply_tune(tune: dict, ts: str):
    """tuning.json 갱신 + tuning.jsonl 이력 + 텔레그램 통지. changed=False면 아무것도 안 함."""
    if not tune.get("changed"):
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    TUNING_FILE.write_text(json.dumps({"cost_buffer": tune["proposed"], "updated": ts,
                                       "n_samples": tune["n"]}, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    with TUNING_LOG.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps({"ts": ts, **tune}, ensure_ascii=False) + "\n")
    notify(f"⚙️ [자동튜닝] cost_buffer {tune['current']:.2%}→{tune['proposed']:.2%} "
           f"(실현 슬리피지 n={tune['n']})", "info", ts)


# ── 오케스트레이션 ────────────────────────────────────────────────────────────
def run_review(do_tune: bool = True, dry: bool = False, log_dir=LOG_DIR) -> dict:
    ts = datetime.now().isoformat(timespec="seconds")
    recs = load_journals(log_dir)
    invalid_fills = []
    fills = extract_fills(recs, invalid_out=invalid_fills)
    rt = round_trips(fills)
    protected = load_protected()
    audit = verify_invariants(recs, fills, protected, invalid_fills)
    perf = postmortem(recs, fills, rt)

    # 중대 위반 → 텔레그램 CRITICAL 먼저 (자동 정지는 안 함 — 사람 결정. 단 즉시 알림).
    # apply_tune(무보호 파일쓰기)보다 반드시 먼저 실행 — 튜닝 중 예외가 이 경보를 삼키지 않게(CRIT 수리).
    if audit["violations"]:
        notify("🚨 [자기검증 위반] " + " | ".join(audit["violations"][:3])
               + (f" 외 {len(audit['violations'])-3}건" if len(audit["violations"]) > 3 else "")
               + " — 즉시 확인 필요 (panic_exit/HALT 검토)", "error", ts)

    current = read_tuned_cost_buffer()
    tune = compute_tune(buy_slippages(fills), current)
    applied = False
    if do_tune and not dry:
        try:
            apply_tune(tune, ts)
            applied = True
        except Exception as e:   # 튜닝 실패를 격리 — 위 위반 경보는 이미 발송됐고, 리포트도 계속 완주해야 함
            notify(f"⚠️ [자동튜닝 실패] cost_buffer 갱신 중 오류 — 기존값 유지: {e!r}", "error", ts)

    report = _render(ts, perf, audit, tune, applied)
    _write_report(report, ts)
    return {"ts": ts, "perf": perf, "audit": audit, "tune": tune, "report": report}


def _render(ts, perf, audit, tune, applied) -> str:
    L = [f"# 자기검토 리포트 — {ts}", ""]
    L.append("## 재귀 안전검증")
    if audit["violations"]:
        L.append(f"🚨 **위반 {len(audit['violations'])}건 (중대):**")
        L += [f"- {v}" for v in audit["violations"]]
    else:
        L.append("✅ 안전 불변식 위반 0 (보호종목 불가침·체결가>0·멱등·정합성 모두 통과)")
    if audit["incidents"]:
        L.append(f"\n관찰 {len(audit['incidents'])}건:")
        L += [f"- {i}" for i in audit["incidents"][-10:]]
    L.append("\n## 성과 포스트모템")
    wr = perf["win_rate"]
    L.append(f"- 실행 {perf['runs']} · 체결 {perf['fills']} · 라운드트립 {perf['round_trips']}"
             + (f" · 승률 {wr:.0%}" if wr is not None else ""))
    L.append(f"- 실현 P&L {perf['realized_pnl']:+.2f} · 체결률 "
             + (f"{perf['fill_rate']:.0%}" if perf['fill_rate'] is not None else "—"))
    if perf["buy_slip_n"]:
        L.append(f"- 매수 슬리피지(n={perf['buy_slip_n']}): 중앙 {perf['buy_slip_med']:.2%} · "
                 f"p90 {perf['buy_slip_p90']:.2%}")
    if perf["open_positions"]:
        L.append(f"- 미청산: {perf['open_positions']}")
    L.append("\n## cost_buffer 자동튜닝 (안전 파라미터, 범위제한)")
    L.append(f"- {tune['reason']}")
    L.append(f"- 현재 {tune['current']:.3%} → 제안 {tune['proposed']:.3%} "
             f"(표본 {tune['n']}, 한도 [{BUF_MIN:.1%},{BUF_MAX:.1%}], 1회±{BUF_STEP:.1%})")
    L.append(f"- {'✅ 적용됨' if (applied and tune['changed']) else ('제안만(미적용)' if tune['changed'] else '변경 없음')}")
    L.append("\n> 자동변경은 cost_buffer(현금 쿠션)에 한정. 전략·신호·리스크 한도는 사람만 변경.")
    return "\n".join(L)


def _write_report(text, ts):
    d = LOG_DIR / "self_review"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{ts[:10]}.md").write_text(text, encoding="utf-8")


def _run_selection_review():
    """신호 성과 사후추적 리포트를 review 작업에 곁들여 실행 — **튜닝/검증 본체와 분리, 실패 무해**.
    순수 관측(전략 자동변경 0). 데이터 부족·네트워크 실패 등 어떤 예외도 review 종료코드·튜닝에 영향 0."""
    try:
        import selection_review as sr
        # 페르소나 별도 home 합쳐 비교 (paths.persona_homes 정규 파서). 미설정이면 기본 home 만.
        from paths import persona_homes
        log_dirs = [LOG_DIR] + [h / "logs" for h in persona_homes()]
        md, _dims, meta = sr.run(log_dirs=log_dirs)
        out_dir = LOG_DIR / "selection_review"
        out_dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now().date().isoformat()
        (out_dir / f"{day}_h{meta['horizon']}.md").write_text(md, encoding="utf-8")
        print(f"\n[신호 성과 추적] 픽 {meta['n_picks']} · 평가 {meta['n_eval']} · pending {meta['pending']} "
              f"→ selection_review/{day}_h{meta['horizon']}.md")
    except Exception as e:
        print(f"\n[신호 성과 추적 실패 — 무해] {e!r}")


def main():
    ap = argparse.ArgumentParser(description="자기검토·재귀검증·cost_buffer 자동튜닝")
    ap.add_argument("--no-tune", dest="no_tune", action="store_true", help="튜닝 스킵(리포트만)")
    ap.add_argument("--dry-tune", dest="dry", action="store_true", help="튜닝 제안만(미적용)")
    ap.add_argument("--no-selection", dest="no_selection", action="store_true",
                    help="신호 성과 사후추적 리포트 스킵")
    a = ap.parse_args()
    ts = datetime.now().isoformat(timespec="seconds")
    try:
        res = run_review(do_tune=not a.no_tune, dry=a.dry)
    except Exception as e:   # 크래시도 무성 실패 금지 — notify 후 비정상 종료코드(모니터링용)
        try:
            notify(f"🚨 review 크래시 — 자기검토/재귀검증 실행 실패: {e!r}", "error", ts)
        except Exception:
            pass
        print(f"ERROR — review 크래시: {e!r}", file=sys.stderr)
        return 2
    print(res["report"])
    if not a.no_selection:
        _run_selection_review()   # 신호 성과 사후추적 — 곁들이기, 실패해도 아래 종료코드 무영향
    v = res["audit"]["violations"]
    return 2 if v else 0   # 위반 있으면 비정상 종료코드(모니터링용) — selection 실패는 영향 0



if __name__ == "__main__":
    sys.exit(main())
