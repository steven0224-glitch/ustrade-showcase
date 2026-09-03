"""운영 원샷 진입점 — cron/Task Scheduler 가 호출. 1회 리밸런스 + 저널 + 알림.

흐름: 데이터 → live_engine.run_once → logs/runs.jsonl 저널 → notify 알림.
브로커: env BROKER=paper|toss (기본 paper). toss 는 broker/toss.py 로 구현됨(실거래; TOSS_API_KEY/SECRET 필요, README 토스 절).
전략: --strategy canslim(기본, A 텔레그램 시그널 코어신호 이식) | momentum(기존).

  python run_live.py                              # paper + canslim, 1회 실행
  python run_live.py --universe sp100             # canslim 권장 유니버스
  python run_live.py --strategy momentum          # 기존 모멘텀+FMP 경로
  python run_live.py --broker toss                # 토스 실거래(TOSS_API_KEY/SECRET 필요)
  python run_live.py --reset-halt                 # 정지 해제 후 실행
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import data
import universe as uni
from live_engine import RunConfig, run_once
from calendar_util import last_completed_session
from paths import LOG_DIR, append_jsonl_rotating
from broker import PaperBroker, TossBroker
from notify import notify, has_channel
from startup import startup_jitter


def make_broker(kind: str, prices, cash_cap=None, paper_cash=None, state_file=None):
    if kind == "toss":
        from broker import ManagedBroker
        from paths import STATE_DIR
        b = TossBroker(paper=False)
        b.connect()
        # 관리 슬리브로 감쌈 — 자동매매가 자기 매수분만 다루고 기존 보유분은 절대 안 건드림.
        return ManagedBroker(b, STATE_DIR / "toss_sleeve.json", cash_cap=cash_cap)
    snap = {s: float(prices[s].iloc[-1]) for s in prices.columns}
    cash = paper_cash if paper_cash is not None else float(os.environ.get("PAPER_CASH", "100000"))
    # state_file 설정 시(페르소나) 현금·포지션 디스크 영속 → 다일 진화(스케줄 재실행 간 책 유지).
    # 수수료 = 토스 패리티 0.1% 기본(USTRADE_PAPER_FEE_RATE), 단 명목 $10 이하 거래는 무료
    # (USTRADE_PAPER_FREE_BELOW) — 실거래 예행연습 비용 현실화.
    fee = float(os.environ.get("USTRADE_PAPER_FEE_RATE") or 0.001)
    free_below = float(os.environ.get("USTRADE_PAPER_FREE_BELOW") or 10.0)
    return PaperBroker(cash=cash, price_fn=lambda s: snap[s], commission=fee,
                       free_below=free_below, state_file=state_file)


def _last_reselect_session(persona: str):
    """이 페르소나 저널(runs.jsonl)에서 마지막 실선정(status ok + selection.final 비공집합)의 session.

    reselect_days 게이트의 앵커 — hold/skip/error/부분체결 레코드는 재선정으로 안 침
    (실패한 재선정일은 다음 실행이 다시 시도). 저널 회전(runs.jsonl.1) 직후엔 앵커가 없어
    한 번 조기 재선정될 수 있음(무해 — 재선정 자체는 항상 안전한 동작)."""
    f = LOG_DIR / "runs.jsonl"
    if not f.exists():
        return None
    try:
        lines = f.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            r = json.loads(ln)
        except Exception:
            continue
        if r.get("persona") != persona or r.get("status") != "ok":
            continue
        if (r.get("selection") or {}).get("final"):
            return r.get("session")
    return None


def _journal(rec: dict):
    f = LOG_DIR / "runs.jsonl"

    def _append():
        append_jsonl_rotating(f, rec)   # 5MB 초과 시 1개 백업으로 회전 + append (paths 공유 헬퍼)
    # 전역 runs.jsonl append 를 RunLock 으로 직렬화 — archive_paper_runs 의 read-modify-write 와 경합 시
    # append 가 lost-update 로 소실되던 것 차단(동일 STATE_DIR/run.lock 도메인). 락 점유 중이면 best-effort
    # 무락 append(저널 누락보다 낫고, archive 동시실행은 드묾).
    from broker.guardrail import RunLock, LockBusy
    try:
        with RunLock():
            _append()
    except LockBusy:
        _append()


def _alert(res: dict, ts: str, kind: str):
    s = res["status"]
    # degrade 알림 — status 분기 밖(P2 잔건①): partial/error 런도 selection 을 채우므로(live_engine)
    # ok 분기 안에 갇혀 있으면 그런 런에서 무력화가 조용히 묻힌다. degraded_reasons(신규, buffett/wood
    # 가 전파)가 있으면 구체 사유로, 없으면(momentum/canslim — 아직 미전파) 일반 문구로.
    # "모멘텀만으로 거래됨" 고정문구는 buffett/wood 에 부정확해 제거.
    sel = res.get("selection") or {}
    if sel.get("screen_degraded"):
        reasons = sel.get("degraded_reasons")
        detail = " — " + " | ".join(reasons) if reasons else ""
        notify(f"⚠️ 펀더 스크린 무력화(레이트/키?){detail}", "error", ts)
    if s == "ok":
        fills = [o for o in res["orders"] if o["status"] == "FILLED"]
        # _acct_snapshot 실패({"acct_error":True})면 'account' 키가 없다 — .get 가드로 KeyError 방지
        # (체결 통지 자체가 예외로 통째 소실되던 것 차단).
        acct = res.get("account")
        if fills:
            def _fmt(o):
                # 금액주문(소수주 BUY)은 $금액으로, 그 외는 수량으로(:g — 소수주 매도도 소수 표시).
                if o.get("amount") is not None:
                    return f"{o['side']} {o['symbol']} ${o['amount']:,.0f}"
                return f"{o['side']} {o['symbol']} {o['qty']:g}"
            txt = ", ".join(_fmt(o) for o in fills)
            if acct:
                notify(f"[{kind}] 체결: {txt} | 자산 {acct['equity']:,.0f} "
                       f"(당일 {res['daily_pnl']:+.2%})", "ok", ts)
            else:
                notify(f"[{kind}] 체결됨·계좌조회 실패: {txt} | 자산 확인 불가(수동 확인 필요) "
                       f"(당일 {res['daily_pnl']:+.2%})", "error", ts)
        else:
            notify(f"[{kind}] 변경 없음 | 자산 {acct['equity']:,.0f}" if acct
                   else f"[{kind}] 변경 없음·계좌조회 실패(수동 확인 필요)", "info", ts)
        rec = res.get("reconcile", {})
        if rec and not rec.get("ok", True):
            if rec.get("verified") is False:
                notify("⚠️ 포지션 정합성 검증 실패(브로커 조회 불가) — 수동 확인 필요", "error", ts)
            else:
                notify(f"⚠️ 포지션 정합성 드리프트 — 확인 필요: {rec.get('drift')}", "error", ts)
    elif s == "halted":
        notify(f"거래 정지 상태: {res['reason']}", "halt", ts)
    elif s == "tripped":
        notify(f"가드레일 트립: {res['reason']}", "halt", ts)
    elif s == "partial":
        notify(f"부분체결/거부 — 재조정 필요: {res['reason']}", "error", ts)
    elif s == "already_ran":
        notify(f"중복 실행 스킵: {res['reason']}", "info", ts)
    elif s == "stale":
        notify(f"데이터 stale — 거래 보류: {res['reason']}", "error", ts)
    elif s == "locked":
        notify(f"중복 실행 차단(락): {res['reason']}", "info", ts)
    elif s == "skip":
        notify(f"거래 보류(선택 공집합): {res['reason']}", "info", ts)
    elif s == "hold":
        notify(f"[{kind}] 보유 유지: {res['reason']}", "info", ts)
    elif s == "error":
        notify(f"실행 에러: {res['reason']}", "error", ts)


def _normalize_cfg(cfg: RunConfig) -> RunConfig:
    """전략별 설정 보정 — canslim 펀더 검증 후보풀은 설계상 ≥12 (live_select_canslim 기본 12).
    RunConfig 기본 pool=8(모멘텀용)이 디스패치에서 canslim 후보풀을 8 로 축소시키던 것 교정."""
    if cfg.strategy in ("canslim", "canslim_rdcf") and cfg.pool < 12:
        cfg.pool = 12
    return cfg


# 대시보드 편집 설정(server.api_settings 가 영속) → RunConfig override. 화이트리스트 필드만,
# 서버가 이미 범위검증해 기록하지만 여기서도 키만 허용(파일 변조 방어). 파일 없으면 무동작.
# persona(모의 프리셋)엔 미적용 — 페르소나 고유 설정 보존.
_DASH_SETTING_KEYS = ("top_n", "max_pe", "min_margin", "min_market_cap", "max_market_cap", "vol_target")


def _apply_dashboard_settings(cfg: RunConfig) -> RunConfig:
    from paths import STATE_DIR
    fpath = STATE_DIR / "control_settings.json"
    if not fpath.exists():
        return cfg
    try:
        saved = json.loads(fpath.read_text(encoding="utf-8")) or {}
    except Exception:
        return cfg
    applied = {}
    for k in _DASH_SETTING_KEYS:
        if k in saved and saved[k] is not None:
            setattr(cfg, k, saved[k])
            applied[k] = saved[k]
    if applied:
        from logsetup import get_logger
        get_logger("run_live").info("대시보드 설정 적용: %s", applied)
    return cfg


def run(cfg: RunConfig = None, broker_kind: str = None, reset_halt: bool = False,
        force: bool = False, cash_cap: float = None, persona: str = None,
        paper_cash: float = None, confirm_live: bool = False,
        cli_overrides: dict = None) -> dict:
    cfg = _normalize_cfg(cfg or RunConfig())
    broker_kind = broker_kind or os.environ.get("BROKER", "paper")
    if not persona:                                   # 페르소나 프리셋엔 대시보드 설정 미적용
        cfg = _apply_dashboard_settings(cfg)
        # CLI 명시값 > 대시보드. §B 실험 파라미터(top_n=5)는 예약태스크 인자가 권위이고,
        # 대시보드 control_settings.json 이 그걸 조용히 덮으면 사전등록이 무효가 된다.
        for _k, _v in (cli_overrides or {}).items():
            setattr(cfg, _k, _v)
    if not cfg.fractional and os.environ.get("USTRADE_FRACTIONAL") in ("1", "true", "True"):
        cfg.fractional = True
    # 페르소나(모의매매 전략 프리셋) — 항상 paper + 책 디스크 영속(다일 진화). 실거래 무관.
    state_file = None
    persona_lock = None
    div_marker = None
    ks_namespace = broker_kind
    if persona:
        broker_kind = "paper"
        ks_namespace = f"paper_{persona}"   # 페르소나별 killswitch 격리 (USTRADE_HOME 오설정에도 안 덮어씀)
        # 책·락 경로를 run_intraday 와 *동일 소스*(persona_home)에서 도출 → USTRADE_HOME 오설정에도
        # 일1런↔장중루프가 같은 책/run.lock 을 공유(split-brain·락 미직렬화 차단). env 단독 의존 제거.
        from run_intraday import persona_home
        _home = persona_home(persona)
        state_file = Path(_home) / "state" / f"paper_book_{persona}.json"   # 페르소나별 책 격리
        persona_lock = Path(_home) / "state" / "run.lock"                   # 장중루프 공유책 락과 동일
    elif broker_kind == "paper":
        # §B 실험 북(기본 paper 런) — 07-12 수리: state_file 없이는 매 런 fresh $100k 로 시작해
        # "누적 NAV"(§B 지표 1)가 원리적으로 측정 불가였다(실측: state/ 에 book 파일 부재).
        # 페르소나와 동일 기전으로 디스크 영속 + 배당 총수익 회계(dividends.py — 벤치마크가
        # SPY 총수익이라 미계상 시 전략만 ~0.3~0.4%p/12주 불리). T0 리셋 = 이 두 파일 삭제.
        from paths import STATE_DIR
        state_file = STATE_DIR / "paper_book.json"
        div_marker = STATE_DIR / "dividends_last.paper.txt"
    # 정수주 모드만 cost_buffer 사이징 헤드룸 필요 — fractional 은 orderAmount 가 정확금액 집행 + fee_reserve 로 대체.
    if broker_kind == "toss" and not cfg.fractional and not cfg.cost_buffer:
        # 비용버퍼 0.5% 기본 — review 자동튜닝이 실현 슬리피지로 조정한 값이 있으면 그걸(범위제한) 사용.
        from review import read_tuned_cost_buffer
        cfg.cost_buffer = read_tuned_cost_buffer(default=0.005)
    ts = datetime.now().isoformat(timespec="seconds")
    # 실거래 명시확인 게이트 — env BROKER=toss 단독 활성화는 우발 주입(머신 env 잔류·cron 오설정)에
    # 취약. dashboard 제어경로(server.api_run)가 confirm=true 를 요구하듯 CLI/직접호출도 명시 확인
    # (--confirm-live 또는 USTRADE_LIVE_CONFIRM=1) 없으면 toss 실거래 거부. paper·persona 는 무관.
    if broker_kind != "paper" and not (confirm_live
            or os.environ.get("USTRADE_LIVE_CONFIRM") in ("1", "true", "True")):
        notify("실거래 거부 — 명시 확인 없음(--confirm-live 또는 USTRADE_LIVE_CONFIRM=1 필요). "
               "env BROKER 단독으론 실거래 차단.", "error", ts)
        return {"status": "error", "reason": "실거래 확인 게이트 미통과 — --confirm-live/USTRADE_LIVE_CONFIRM 필요"}
    # go-live 안전 — 실거래(paper 아님)인데 알림 채널 미설정이면 거래 거부. 무인 실거래서
    # 트립·크래시·미체결이 아무에게도 안 알려진 채 도는 상황 차단(OPS-2). paper 는 허용(개발).
    if broker_kind != "paper" and not has_channel():
        notify("실거래 거부 — 알림 채널 미설정(TELEGRAM_*/SLACK_WEBHOOK_URL). 무인 실거래엔 필수.",
               "error", ts)
        return {"status": "error", "reason": "실거래인데 알림 채널 미설정 — 거래 거부"}
    # 거래 대상 세션 = ET 기준 직전 종료 NYSE 세션 (naive 로컬시각·DST·공휴일 문제 회피)
    session = last_completed_session()
    if session is None:
        notify("거래일 판정 실패 (최근 12일 NYSE 세션 없음)", "error", ts)
        return {"status": "error", "reason": "no recent NYSE session"}
    today = session.isoformat()
    end_excl = (session + timedelta(days=1)).isoformat()   # yfinance end 는 미포함 → +1일로 세션봉 포함

    # 토스 실거래: 관리 슬리브 필수 (기존 보유분 보호). 미설정이면 거부 — 사고 차단.
    sleeve_protected = set()
    if broker_kind == "toss":
        from paths import STATE_DIR
        from broker import load_sleeve
        from broker.managed import _norm
        sp = STATE_DIR / "toss_sleeve.json"
        if not sp.exists():
            notify("실거래 거부 — 토스 관리 슬리브 미설정. `python toss_setup.py` 먼저 실행.",
                   "error", ts)
            return {"status": "error", "reason": "토스 슬리브 미설정 — toss_setup.py 먼저 실행"}
        sleeve_protected = load_sleeve(sp)["protected"]

    # 재선정 주기 게이트(reselect_days>0) — 마지막 실선정으로부터 N일 미경과면 hold(보유 유지).
    # --force 는 게이트도 우회(수동 즉시 재선정 레버). 저널 앵커 없음(첫 도입/회전 직후)=재선정.
    reselect_due = True
    if persona and getattr(cfg, "reselect_days", 0) > 0 and not force:
        last = _last_reselect_session(persona)
        if last:
            try:
                from datetime import date
                reselect_due = (date.fromisoformat(today) - date.fromisoformat(last)).days >= cfg.reselect_days
            except ValueError:
                pass   # 저널 session 형식 이상 — 재선정(안전한 기본)으로 진행

    try:
        prices = data.load_panel(uni.get_universe(cfg.universe), "2022-01-01", end_excl)
        # 보호종목은 후보에서 제외 → 전략이 기존 보유분을 타겟으로 삼지 않음(방어 3).
        # 정규화 비교 — 토스(BRK.B)와 유니버스(BRK-B) 표기 불일치가 제외를 무력화하지 못하게.
        if sleeve_protected:
            drop = [c for c in prices.columns if _norm(c) in sleeve_protected]
            if drop:
                prices = prices.drop(columns=drop)
        broker = make_broker(broker_kind, prices, cash_cap=cash_cap,
                             paper_cash=paper_cash, state_file=state_file)
        res = run_once(prices, broker, cfg, today=today,
                       reset_halt=reset_halt, force=force, ks_namespace=ks_namespace,
                       lock_path=persona_lock, reselect_due=reselect_due,
                       dividends_marker=div_marker)
    except Exception as e:
        notify(f"실행 크래시: {e}", "error", ts)
        # broker·persona 태그 부착(정상저널과 동일) — 크래시도 review real_only 가 paper 를 일관 배제(공유홈 누수 방지)
        _journal({"ts": ts, "session": today, "broker": broker_kind,
                  **({"persona": persona} if persona else {}),
                  "status": "crash", "reason": str(e)})
        return {"status": "crash", "reason": str(e)}

    # 표면화 — 선정 저널에 데이터 품질 필드 부착(전 페르소나 공통 단일 지점).
    # missing_ratio: 펀더 결측 비율(0~1). fmp_stale_*: 이번 런이 만료캐시 폴백을 썼으면 횟수·최대나이(일).
    sel = res.get("selection")
    if isinstance(sel, dict):
        cand = sel.get("candidates") or []
        if cand and isinstance(sel.get("missing"), list):
            sel["missing_ratio"] = round(len(sel["missing"]) / len(cand), 2)
        try:
            import fmp_client as _fc
            if getattr(_fc, "STALE_HITS", 0):
                sel["fmp_stale_hits"] = _fc.STALE_HITS
                sel["fmp_stale_max_age_d"] = round(_fc.STALE_MAX_AGE_D, 1)
        except Exception:
            pass
    _journal({"ts": ts, "session": today, "broker": broker_kind,
              **({"persona": persona} if persona else {}),
              **{k: res[k] for k in ("status", "reason", "weights", "orders", "account", "positions",
                                     "daily_pnl", "reconcile", "selection", "risk", "acct_error",
                                     "dividends")
                 if k in res}})
    _alert(res, ts, broker_kind)
    if res.get("dividends"):   # 배당 입금 표면화 — 분기 1~2회 이벤트, 저널이 원본·알림은 관측용
        try:
            _dtxt = ", ".join(f"{e['symbol']} ${e['amount']:,.2f}" for e in res["dividends"])
            notify(f"[{broker_kind}] 배당 입금: {_dtxt}", "info", ts)
        except Exception:
            pass
    # 스캐너 캐시 워밍업(곁들이기) — 대시보드 라다/AI 가 보는 가격을 라이브와 같은 캐시로 신선화.
    # 거래 본체와 분리·graceful: 실패해도 거래 결과에 영향 없음(대시보드 신선도는 비핵심).
    try:
        _dash = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
        if _dash not in sys.path:
            sys.path.insert(0, _dash)
        import build_data as _bd
        ok, tot = _bd.refresh_scan_cache()
        print(f"[warm] 스캐너 캐시 워밍업: {ok}/{tot}")
    except Exception as e:
        print(f"[warm] 스캐너 캐시 워밍업 스킵: {e}")
    return res


def main():
    import personas as _personas   # choices 를 레지스트리에서 파생(아래 --persona) — 순수 dict 모듈이라 저비용
    import live_engine as _le      # 이미 module-level 로드됨(sys.modules 히트) — 전략 등록표 참조용
    ap = argparse.ArgumentParser(description="운영 원샷 리밸런스")
    ap.add_argument("--broker", default=None, help="paper|toss (기본 env BROKER)")
    ap.add_argument("--strategy", default="canslim", choices=["canslim", "momentum"],
                    help="canslim=A 텔레그램 시그널 코어신호 이식(기본) | momentum=기존 모멘텀+FMP")
    ap.add_argument("--universe", default=None,
                    help="유니버스 (기본 diversified). canslim 은 후보풀 넓은 sp100 권장")
    ap.add_argument("--top-n", dest="top_n", type=int, default=None,
                    help="보유 종목수(등비중). 미지정 시 RunConfig 기본 3. 명시하면 대시보드 "
                         "control_settings.json 보다 우선 — §B 실험은 태스크 인자가 권위. persona 엔 미적용")
    # choices 는 personas 레지스트리 ∩ 일1런 전략 등록표에서 파생 — 하드코딩 리스트였을 때 신규
    # 페르소나(buffett_v2)가 argparse 단계에서 거부돼 태스크가 매일 exit 2 로 죽었다.
    # ⚠️ 교집합이어야 한다. PERSONAS 전체를 열면 장중전용 페르소나(livermore·chartist·
    # livermore_swing·*_ctl 5종)가 통과하는데, 그 strategy 는 _STRATEGIES 에 **없어서**
    # live_engine 의 `_STRATEGIES.get(s) or select` 가 명시 error 없이 **모멘텀으로 폴백**한다
    # (미등록 전략은 `s in _STRATEGIES` 가드에도 안 걸림) — 장중루프와 공유하는 책에 모멘텀
    # 선정분이 그대로 체결돼 들어간다. argparse 가 마지막 방어선이라 여기서 막는다.
    _daily_personas = sorted(n for n, p in _personas.PERSONAS.items()
                             if p["strategy"] in _le._STRATEGIES)
    ap.add_argument("--persona", default=None, choices=_daily_personas,
                    help="모의매매 페르소나 — paper 전용 전략프리셋(다일 진화 책). 지정 시 broker/strategy/universe/fractional 무시. livermore·chartist 계열은 일1런 엔진이 없어 choices 에서 제외(run_intraday 장중 전용)")
    ap.add_argument("--cash-cap", dest="cash_cap", type=float, default=None,
                    help="토스 관리 슬리브가 쓸 최대 현금(USD). 미지정 시 env TOSS_MANAGED_CASH, "
                         "둘 다 없으면 계좌 가용현금 전부")
    ap.add_argument("--reset-halt", dest="reset_halt", action="store_true")
    ap.add_argument("--force", action="store_true", help="당일 중복실행 락 무시 (수동 재실행용)")
    ap.add_argument("--fractional", action="store_true",
                    help="소수주 모드 — BUY=금액주문(orderAmount $), SELL=소수 수량(토스 US 시장가매도). 기본 정수주")
    ap.add_argument("--confirm-live", dest="confirm_live", action="store_true",
                    help="실거래(toss) 명시 확인 — 없으면 env BROKER=toss 라도 실거래 거부(우발 활성화 차단)")
    a = ap.parse_args()
    # 기동 지터 — 페르소나 3종(setup_paper_tasks.ps1)이 동일 ustrade-entry 트리거로 동시 발사돼
    # cold cache 에 yfinance/FMP·공유캐시를 같은 찰나에 강타하는 것 분산. 대화형 수동 run·env 0 이면
    # no-op. 데이터 fetch(run→data.load_panel) 전이어야 효과 → parse_args 직후.
    d = startup_jitter()
    if d:
        print(f"[jitter] 기동 지터 {d:.1f}s — 동시발사 분산")
    if a.persona:
        import personas
        p = personas.get(a.persona)
        cfg = RunConfig(strategy=p["strategy"], universe=p["universe"])
        for k, v in p["overrides"].items():
            setattr(cfg, k, v)
        res = run(cfg=cfg, broker_kind="paper", reset_halt=a.reset_halt, force=a.force,
                  persona=a.persona, paper_cash=p["cash"])
    else:
        cfg = RunConfig(strategy=a.strategy)
        if a.fractional:
            cfg.fractional = True
        if a.universe:
            cfg.universe = a.universe
        if a.top_n:
            cfg.top_n = a.top_n
        cap = a.cash_cap
        if cap is None and os.environ.get("TOSS_MANAGED_CASH"):
            cap = float(os.environ["TOSS_MANAGED_CASH"])
        res = run(cfg=cfg, broker_kind=a.broker, reset_halt=a.reset_halt, force=a.force, cash_cap=cap,
                  confirm_live=a.confirm_live,
                  cli_overrides={"top_n": a.top_n} if a.top_n else None)
    print(f"status: {res['status']}" + (f" | {res.get('reason','')}" if res.get("reason") else ""))
    # 종료코드 매핑(OPS-1) — Task Scheduler/모니터링이 실패를 구분하도록. main() 이 None 을
    # 반환하면 sys.exit(None)=0 이라 모든 크래시/트립이 '성공'으로 보이던 버그.
    benign = {"ok", "already_ran", "locked", "skip", "hold"}   # 정상/무해 → 0
    soft = {"stale", "partial"}                         # 거래 보류/부분 → 1 (주의)
    s = res["status"]
    return 0 if s in benign else (1 if s in soft else 2)   # 그 외(halted/tripped/error/crash) → 2


if __name__ == "__main__":
    sys.exit(main())
