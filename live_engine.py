"""라이브 엔진 — 단일 리밸런스 실행의 정식 로직 (DRY 단일소스).

선택(모멘텀+펀더멘털) → 리스크오버레이(레짐+vol) → 킬스위치 가드 → 체결.
live_rebalance.py(CLI 데모)와 run_live.py(운영 스케줄)가 공유.
거래·가드 로직은 여기 한 곳에만 — 실거래 안전상 중복 금지.
"""
import time
from dataclasses import dataclass

from live_select import select   # 모멘텀+FMP (기본 바인딩; 테스트가 live_engine.select 오버라이드)
from live_risk import apply_overlay, regime_on
from calendar_util import session_gap
from broker import (Executor, KillSwitch, HaltError, OrderStatus, GuardedBroker,
                    RunLock, LockBusy, Side, OrderRequest, OrderType)

# 전략 디스패치 — canslim(A 텔레그램 시그널 코어신호 이식)은 cfg.strategy=="canslim" 시 사용.
# A(형제 디렉토리) 모듈 부재 시 _select_canslim=None → canslim 요청은 명시적 error, momentum 정상.
# momentum/그외는 글로벌 select 로 폴백 — 기존 테스트의 live_engine.select monkeypatch 훅 보존.
try:
    from live_select_canslim import select as _select_canslim
except Exception:
    _select_canslim = None
try:
    from live_select_buffett import select as _select_buffett   # paper 페르소나(버핏 가치) — FMP, A엔진 불요
except Exception:
    _select_buffett = None
try:
    from live_select_wood import select as _select_wood         # paper 페르소나(우드 성장) — FMP, A엔진 불요
except Exception:
    _select_wood = None
try:
    from live_select_buffett_v2 import select as _select_buffett_v2   # buffett A/B 실험군(ROIC·섹터중립)
except Exception:
    _select_buffett_v2 = None
try:
    from live_select_canslim_rdcf import select as _select_canslim_rdcf   # canslim A/B 실험군(역DCF 밸류틸트)
except Exception:
    _select_canslim_rdcf = None
# ⚠️ 미등록 전략명은 `_STRATEGIES.get(...) or select` 로 **모멘텀에 조용히 폴백**한다 —
# buffett_v2 를 여기 넣지 않으면 페르소나가 모멘텀을 돌리면서 로그만 buffett_v2 로 남는다.
_STRATEGIES = {"canslim": _select_canslim, "buffett": _select_buffett, "wood": _select_wood,
               "buffett_v2": _select_buffett_v2, "canslim_rdcf": _select_canslim_rdcf}


@dataclass
class RunConfig:
    universe: str = "diversified"
    strategy: str = "momentum"        # momentum(글로벌 select) | canslim(A 코어신호 이식). 운영(run_live)은 canslim 기본
    lookback: int = 126
    top_n: int = 3
    pool: int = 8
    min_margin: float = 0.0
    max_pe: float = 80.0
    min_market_cap: float = None      # 시총 하한(USD, 예 10e9=$10B). None=무동작. screen() 에서 적용(momentum/buffett 경로). canslim/wood 는 자체 게이트라 미적용.
    max_market_cap: float = None      # 시총 상한(USD). None=무동작.
    use_pead: bool = False            # PEAD(어닝 서프라이즈) 틸트 — 기본 off. ⚠️ IC 검증(eval_factor earnings_surprise) 통과 후만 on. momentum 경로만 적용.
    pead_weight: float = 0.3          # PEAD z-score 가중(0~1). use_pead on 시 모멘텀 (1-w) + PEAD w.
    use_alpha: bool = False           # Alpha Zoo 틸트(alpha101_032) — 기본 off. ⚠️ eval_factor --factor zoo confirmed_alive 통과분만. momentum 경로만 적용(canslim/buffett/wood 는 **_ 로 무시).
    alpha_weight: float = 0.3         # alpha z-score 가중(0~1). use_alpha on 시 모멘텀 (1-w) + alpha w.
    # canslim(oneil) 안전 다이얼 — canslim 경로 전용(디스패치가 strategy=="canslim" 일 때만 전달).
    # 기본값은 select() 기본과 동일 → 미설정 시 동작 불변. persona overrides 로 켤 수 있음(예전엔 디스패치
    # 미전달로 영구 기본=死코드였음). min_score=펀더점수 하한(0=off), value_trap_gate=Piotroski value trap 제외,
    # min_proximity=52주 고가 근접도 하한.
    min_score: int = 0
    value_trap_gate: bool = False
    min_proximity: float = 0.85
    reselect_days: int = 0            # N일 주기 재선정(0=매일). 비재선정일은 KS가드+레짐 감시만, 리밸런스 스킵
                                      # — buffett 주1회(7): 일일 재선정 churn 연 4~7% 누수 절감(전략감사 후속)
    vol_target: float = 0.20
    regime_ma: int = 200
    alloc: float = 0.95
    cost_buffer: float = 0.0          # 라이브 매수 사이징 비용버퍼(토스 등 비용속성 없는 브로커용; run_live 가 toss 에 0.5% 주입). fractional 모드선 미사용.
    fractional: bool = False          # 소수주 모드 — BUY=orderAmount(달러), SELL=소수 quantity(토스 US). off=정수주(기존 동작 불변)
    min_order_usd: float = 5.0        # fractional: 이 금액 미만 매수/트림은 주문 안 함(churn·sub-min거부 방지). 전량청산은 면제
    fee_reserve: float = 0.005        # fractional: 매수예산 haircut investable*(1-reserve) — orderAmount gross/net 미확정 방어
    max_staleness_sessions: int = 3   # 데이터 마지막봉이 기준세션 대비 N세션 초과 stale 면 거래 거부 (0=비활성)
    fill_timeout: float = 30.0        # 시장가 체결확인 폴링 최대 대기(초) — toss 비동기 체결 대비
    fill_poll_interval: float = 2.0   # 체결확인 폴링 간격(초)


def _dump_orders(orders) -> list:
    out = []
    for o in orders:
        d = {"side": o.request.side.value, "symbol": o.request.symbol,
             "qty": o.request.qty, "fill": o.avg_fill_price,
             "ref": o.request.ref_price,
             "status": o.status.value, "message": o.message,
             "reason": getattr(o.request, "reason", "")}   # 매매 사유(전략 근거) — 대시보드 기록칸
        # 금액주문(소수주 BUY): request.qty=0(달러로 주문) → 실제 체결 소수주수를 qty 로 보존하고
        # 주문 달러금액을 amount 로 기록. 미보존 시 저널·알림이 'BUY X 0' + review P&L 라운드트립
        # 매칭 불가(체결주수 0). 미체결이면 filled_qty=0 → amount 가 의도 보존.
        if getattr(o.request, "amount", None) is not None:
            d["amount"] = o.request.amount
            d["qty"] = o.filled_qty or 0.0
        out.append(d)
    return out


def _acct_snapshot(broker) -> dict:
    """계좌·포지션 스냅샷 (실패해도 throw 안 함 — 에러 경로 보고용).

    실패 시 {"acct_error": True} — 빈 {} 로 무성 흡수하면 저널이 '조회실패'와 '레거시 무키'를
    구분 못 해 대시보드가 정상처럼 보임. account/positions 키는 절대 위조하지 않음
    (selection_review 가 positions 키 존재로 필터를 켜므로 가짜 빈 리스트는 유령 배제 유발)."""
    try:
        acct = broker.get_account()
        return {"account": {"cash": round(acct.cash, 2), "equity": round(acct.equity, 2)},
                "positions": [{"symbol": p.symbol, "qty": p.qty, "avg": round(p.avg_price, 2)}
                              for p in broker.get_positions()]}
    except Exception:
        return {"acct_error": True}


_TERMINAL = {OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELLED}


def _flatten_positions(gbroker, reason: str) -> list:
    """보호 전량청산 — 누적DD 트립 시 롱 동결 해소(전략감사 후속, GUARD-1).

    GuardedBroker 경유 — 손실성 halt(daily_loss/total_drawdown/error)는 위험축소 SELL 을 허용
    (guardrail.exit_blocked)하므로 가드 경계 *안*에서 실행된다(우회 없음). 수동 HALT 등 하드
    정지면 place_order 가 거부 → 빈 결과(사람 개입 존중). 종목별 독립 시도 — 한 종목 실패가
    나머지 청산을 막지 않고, 잔여분은 다음 실행의 halted 경로가 재시도(멱등)."""
    orders = []
    try:
        positions = gbroker.get_positions()
    except Exception:
        return orders
    for p in positions:
        if p.qty <= 1e-9:
            continue
        try:
            orders.append(gbroker.place_order(
                OrderRequest(p.symbol, Side.SELL, p.qty, OrderType.MARKET, reason=reason)))
        except Exception:
            continue
    return orders


def _halt_result(status: str, reason: str, ks, gbroker, orders) -> dict:
    """정지/트립 보고 — halt_kind 가 누적DD(total_drawdown)면 잔여 롱을 보호청산해 병합.

    daily_loss(익일 자동해제)는 청산 안 함 — 하루 쉼이 설계 의도. total_drawdown 은 자동해제가
    없어(수동 reset 대기) 보유 지속=대기 내내 추가하락 라이딩이므로, 현금화가 '스위치 내림'의
    완성이다(보유 지속이 옳았다면 사람이 reset 후 재진입 판단)."""
    orders = list(orders)
    if ks.state.get("halt_kind") == "total_drawdown":
        flat = _flatten_positions(gbroker, "누적DD 트립 보호청산")
        if flat:
            orders += flat
            reason += " — 잔여 포지션 보호 전량청산"
    return {"status": status, "reason": reason, "orders": _dump_orders(orders),
            **_acct_snapshot(gbroker)}


def _await_fills(broker, orders, timeout: float, interval: float):
    """미종결 주문을 종료상태까지 폴링 (시장가가 즉시체결 안 되는 브로커 대비).

    PaperBroker 처럼 즉시 FILLED 면 루프 미진입(대기 0). toss 등 비동기 체결만 폴링.
    """
    pending = [o for o in orders if o.status not in _TERMINAL]
    if not pending:
        return orders
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        time.sleep(interval)
        still = []
        for o in pending:
            try:
                u = broker.get_order(o.order_id)
                o.status, o.filled_qty, o.avg_fill_price = u.status, u.filled_qty, u.avg_fill_price
                o.message = u.message
            except Exception:
                pass
            if o.status not in _TERMINAL:
                still.append(o)
        pending = still
    return orders


def _reconcile(prior_qty: dict, orders, broker) -> list:
    """사후 정합성 — (직전 포지션 + 체결분) 기대치 vs 브로커 실제 포지션 대조.

    드리프트 = 브로커 상태가 우리 가정과 어긋남(체결 누락·비동기 desync·외부 변경).
    실패해도 throw 안 함(빈 리스트). 무인 실거래서 잘못된 가정으로 계속 매매하는 것 방지용.
    """
    expected = dict(prior_qty)
    for o in orders:
        if o.status == OrderStatus.FILLED:
            d = o.filled_qty if o.request.side == Side.BUY else -o.filled_qty
            expected[o.request.symbol] = expected.get(o.request.symbol, 0) + d
    try:
        actual = {p.symbol: p.qty for p in broker.get_positions()}
    except Exception:
        return None    # 검증 불가 — '드리프트 없음([])' 으로 둔갑하지 않음(fail-open 차단). 호출측이 미검증 처리.
    drift = []
    for s in set(expected) | set(actual):
        e, a = expected.get(s, 0), actual.get(s, 0)
        if abs(e - a) > 1e-6:
            drift.append({"symbol": s, "expected": e, "actual": a})
    return drift


def run_once(prices, broker, cfg: RunConfig, today: str, reset_halt: bool = False,
             force: bool = False, ks_namespace: str = "", lock_path=None,
             reselect_due: bool = True, dividends_marker=None) -> dict:
    """1회 리밸런스 실행. 구조화 결과 반환.

    status: ok | halted | tripped | error | partial | already_ran | stale | locked | skip | hold
      partial      = 일부 주문 거부/부분체결 (성공 기록 안 함, 에러누적, 다음 실행이 재조정)
      already_ran  = 당일 이미 거래 완료 (중복매매 방지; force=True 로 우회)
      stale        = 데이터가 기준세션 대비 너무 오래됨 (거래 보류)
      locked       = 다른 실행이 진행 중 (동시 실행 더블트레이드 방지)
      skip         = 선택 종목 공집합 (데이터/스크린 결과 없음) — 거래 보류, 포지션 유지(청산 안 함)
      hold         = 재선정 주기(reselect_days) 비도래 보유일 — KS가드·레짐 감시만, 리밸런스 스킵

    reselect_due=False(호출측이 저널로 판단)면 hold 경로 — 단 레짐 OFF 는 정상 경로로 계속 진행해
    기존 오버레이 청산을 태운다(보호는 매일, churn 은 주기당 1회).

    전체 임계구역을 프로세스 간 락(RunLock)으로 보호 — 동시 cron/수동 실행이
    already_traded 를 레이스로 우회해 중복 체결하는 것을 차단.
    """
    try:
        # lock_path 지정 시(페르소나: persona_home/state/run.lock) 그 경로로 — 장중루프 공유책 락과
        # 동일 소스. 미지정이면 기본 LOCK_FILE(STATE_DIR/run.lock).
        from pathlib import Path
        with RunLock(path=Path(lock_path) if lock_path else None):
            # 배당 입금(dividends_marker 전달 시 — run_live 기본 paper 북 전용) — already_traded/halted
            # 게이트 *앞*: 배당은 거래 여부와 무관하게 들어온다(실계좌 동일). 이 시점 보유수량 =
            # 리밸런스 전 = ex-date 권리 수량. 락 안 + reload 로 lost-update 차단. fail-open(런 보호).
            div_events = []
            if dividends_marker:
                _rl = getattr(broker, "reload", None)
                if callable(_rl):
                    try:
                        _rl()
                    except Exception:
                        pass
                from dividends import process_dividends
                div_events = process_dividends(broker, today, dividends_marker)
            res = _run_once_locked(prices, broker, cfg, today, reset_halt, force, ks_namespace,
                                   reselect_due=reselect_due)
            if div_events:
                res["dividends"] = div_events
            return res
    except LockBusy as e:
        return {"status": "locked", "reason": str(e)}


def _run_once_locked(prices, broker, cfg, today, reset_halt, force, ks_namespace="",
                     reselect_due=True) -> dict:
    ks = KillSwitch(today=today, namespace=ks_namespace)
    if reset_halt:
        ks.reset()
    ks.resume_if_new_day()   # 새 거래일이면 일일손실 정지 자동해제 (is_halted 前 — 안 하면 영구정지)
    halted, reason = ks.is_halted()
    if halted:
        # 누적DD 정지면 잔여 롱 보호청산 시도(동결 해소, 멱등) — 그 외 kind 는 보고만.
        return _halt_result("halted", reason, ks, GuardedBroker(broker, ks), [])
    if not force and ks.already_traded():
        return {"status": "already_ran", "reason": f"{today} 이미 거래 완료 (중복매매 방지)"}

    # 공유책(페르소나) 재적재 — 이 함수는 RunLock 임계구역 안이다. 락 밖 PaperBroker.__init__._load 의
    # stale 인메모리가 장중루프(run_intraday) 커밋을 lost-update 로 덮어쓰는 것 차단(run_intraday._on_bar
    # 의 reload-in-lock 과 동일 패턴). reload 메서드가 아예 없는 브로커(TossBroker 자체)만 no-op.
    # 실패는 삼키지 않고 raise(수리 2026-08-01) — ManagedBroker.reload 는 애초 load_sleeve 가 무가드로
    # 던지도록 설계돼 있는데 여기서 무음 흡수되고 있었다(PaperBroker.reload 도 이제 파일 손상 시 raise,
    # broker/paper.py). 상위 run_once → run_live.py 크래시 핸들러(notify+crash 저널)가 fail-closed 로
    # 수용 — 위 주석이 막으려던 stale 책 위 리밸런스(lost-update)를 여기서도 막는다.
    _reload = getattr(broker, "reload", None)
    if callable(_reload):
        _reload()

    # 재선정 주기 게이트(reselect_days>0) — 비도래일은 리스크 가드(일일손실·누적DD)와 레짐 감시만
    # 수행하고 리밸런스를 스킵(보유 유지, churn 절감). 레짐 OFF 는 return 하지 않고 아래 정상
    # 경로로 계속 진행 → apply_overlay 가 {} 를 반환해 기존 검증된 청산 경로를 그대로 태운다.
    # 판정불가(None)는 보유 유지 — 데이터 hiccup 으로 멀쩡한 책을 청산하지 않는다(fail-hold).
    if not reselect_due:
        gb = GuardedBroker(broker, ks)
        try:
            acct = gb.get_account()
            ks.roll_day(acct.equity)
            dd = ks.check_daily_loss(acct.equity)
            ks.check_total_drawdown(acct.equity)
        except HaltError as e:
            return _halt_result("tripped", str(e), ks, gb, [])
        try:
            reg = regime_on(regime_ma=cfg.regime_ma)
        except Exception:
            reg = None
        if reg is not False:
            ks.mark_traded()   # 당일 처리 완료 — 주말/중복 실행이 hold 레코드를 재적재하지 않게
            return {"status": "hold",
                    "reason": (f"재선정 보유일(주기 {cfg.reselect_days}일, "
                               f"레짐 {'ON' if reg else '판정불가'}) — 리밸런스 스킵"),
                    "daily_pnl": dd, "risk": {"regime": "ON" if reg else "판정불가"},
                    **_acct_snapshot(gb)}
        # 레짐 OFF → 정상 경로 계속(선정→오버레이 {} → 전량 현금화)

    # L-D — 구조적 분산 불가 설정 조기 차단: 1/top_n 이 단일비중 한도보다 크면 등비중이
    # 매 실행 check_targets 를 트립(영구정지)시킴. 암호 같은 바운드 트립 대신 명시적 error.
    if cfg.top_n and 1.0 / cfg.top_n > ks.cfg.max_position_weight + 1e-9:
        return {"status": "error",
                "reason": (f"설정 모순: top_n={cfg.top_n} → 단일비중 {1/cfg.top_n:.0%} "
                           f"> 바운드 {ks.cfg.max_position_weight:.0%} (top_n↑ 또는 바운드↑)")}

    # 빈(0행) 가격 패널 방어 — select 의 momentum/.iloc[-1] 이 빈 패널에서 IndexError 를 내므로 진입 전
    # skip(다페르소나 동시 crash 차단). ※ 운영 data.load_panel 은 all-fail 시 ValueError raise 라(data.py
    # H4) 그 경로는 run_live 가 crash 로 흡수 — 이 가드는 테스트·미래 호출자가 빈 DataFrame 을 직접 넘기는
    # 경우의 방어선. prices is None 은 staleness 게이트가 관용하는 입력(테스트 select 훅)이라 제외 —
    # 빈 DataFrame 만 차단해 None 관용 회귀(stage 스위트) 없이 방어.
    if prices is not None and len(prices.index) == 0:
        return {"status": "skip", "reason": "가격 패널 비어있음 — 거래 보류"}

    # H1 — 데이터 신선도: 마지막 봉이 기준 세션 대비 너무 오래되면 거래 거부 (stale 피드 보호)
    if prices is not None and len(prices.index) and cfg.max_staleness_sessions > 0:
        last_bar = prices.index[-1]   # len 가드 — 전-NaN 패널 dropna 후 0행이면 index[-1] IndexError(crash) 방지
        gap = session_gap(last_bar, today)
        if gap > cfg.max_staleness_sessions:
            return {"status": "stale",
                    "reason": (f"데이터 {gap}세션 stale (마지막봉 {last_bar.date()}, "
                               f"기준 {today}, 한도 {cfg.max_staleness_sessions})")}
        # 종목별 stale 컬럼 제외 — 패널 last bar 는 '가장 신선한 티커 하나'로 정해져 위 게이트가
        # 우회될 수 있음. 종목별 마지막 유효봉이 한도 초과 stale 이면 후보에서 빼 거짓신호 차단.
        stale_cols = [c for c in prices.columns
                      if len(prices[c].dropna().index) == 0
                      or session_gap(prices[c].dropna().index[-1], today) > cfg.max_staleness_sessions]
        if stale_cols:
            prices = prices.drop(columns=stale_cols)
            if prices.shape[1] == 0:
                return {"status": "stale", "reason": f"전 종목 stale — 거래 보류 ({len(stale_cols)}개 제외)"}

    # 슬리브 자가복구 — 이전 실행의 미확정 매수(크래시/30s초과/늦은체결)를 실보유와 대조해
    # basis 로 흡수(중복매수·자본동결 방지). ManagedBroker 만 해당. 거래(select) 前에 실행.
    rec_basis = getattr(broker, "reconcile_basis", None)
    if rec_basis is not None:
        try:
            rec_basis()
        except Exception:
            pass

    # 선택(전략 디스패치) + 리스크
    if cfg.strategy in _STRATEGIES and _STRATEGIES[cfg.strategy] is None:
        _why = {"canslim": "A(텔레그램 시그널) 모듈 미발견"}.get(cfg.strategy, "선택모듈 로드 실패")
        return {"status": "error", "reason": f"{cfg.strategy} 전략 사용 불가 — {_why}"}
    sel_fn = _STRATEGIES.get(cfg.strategy) or select   # momentum/그외 → 글로벌 select(테스트 훅)
    _sel_kw = dict(lookback=cfg.lookback, top_n=cfg.top_n, pool=cfg.pool,
                   min_margin=cfg.min_margin, max_pe=cfg.max_pe,
                   min_market_cap=cfg.min_market_cap, max_market_cap=cfg.max_market_cap,
                   use_pead=cfg.use_pead, pead_weight=cfg.pead_weight,
                   use_alpha=cfg.use_alpha, alpha_weight=cfg.alpha_weight)
    if cfg.strategy in ("canslim", "canslim_rdcf"):    # canslim 계열 안전다이얼 — momentum select 은
        _sel_kw.update(min_score=cfg.min_score,        # 이 kwargs 를 안 받으므로(**_ 없음) canslim 계열일 때만 전달
                       value_trap_gate=cfg.value_trap_gate, min_proximity=cfg.min_proximity)
    elif cfg.strategy in ("buffett", "buffett_v2"):     # buffett 계열도 Piotroski veto 다이얼 공유(그 외 kwargs 는 canslim 전용)
        _sel_kw.update(value_trap_gate=cfg.value_trap_gate)
    weights, sel = sel_fn(prices, **_sel_kw)
    # 보호종목 방어 다중화(엔진 계층) — run_live 호출지점 외에 엔진에서도 protected 를 타겟에서 제외.
    # ManagedBroker 만 .protected 보유(없으면 무시). 정규화 비교(BRK.B↔BRK-B). 비면 아래 skip 가 잡음.
    prot = getattr(broker, "protected", None)
    if prot and weights:
        from broker.managed import _norm
        dropped = [t for t in weights if _norm(t) in prot]
        if dropped:
            weights = {t: w for t, w in weights.items() if _norm(t) not in prot}
            sel["protected_dropped"] = sorted(dropped)
    # STRAT-3 — 선택 공집합(모멘텀/스크린 결과 없음)은 '보류'(현 포지션 유지)로 처리. 빈 비중을
    # 그대로 흘리면 Executor 가 전 종목 청산 → 데이터 결함에 전량 현금화하는 사고. (레짐 OFF 의
    # 의도적 현금화는 apply_overlay 가 따로 {} 반환 → 그 경로는 정상 청산이라 여기서 안 걸림.)
    if not weights:
        return {"status": "skip",
                "reason": "선택 종목 없음 (모멘텀/스크린 결과 공집합) — 거래 보류(포지션 유지)",
                "selection": sel}
    risk = {}
    if cfg.vol_target > 0:
        weights, risk = apply_overlay(prices, weights, vol_target=cfg.vol_target,
                                      regime_ma=cfg.regime_ma)

    # STRAT-2 — 과소선택(스크린이 종목수를 top_n 미만으로 줄임)으로 1/len(final) 이 단일비중
    # 한도를 넘으면 check_targets 가 영구정지(L-D 와 같은 함정의 런타임 경로). 영구정지 대신
    # 비중을 한도로 캡(나머지 현금) → 분산제약 지키며 거래 지속.
    cap = ks.cfg.max_position_weight
    over = {t: w for t, w in weights.items() if w > cap + 1e-9}
    if over:
        weights = {t: min(w, cap) for t, w in weights.items()}
        sel["weight_capped"] = sorted(over)

    # 가드 + 체결 — GuardedBroker 가 주문마다 HALT/명목 재확인 (가드 경계 강제, 우회 불가).
    # 주문을 하나씩 누적 → 루프 중간에 트립/에러가 나도 '이미 체결된 주문'을 보고에 남김
    # (유령 실포지션 방지 + 다음 실행이 plan 으로 재조정).
    gbroker = GuardedBroker(broker, ks)
    exe = Executor(gbroker, alloc=cfg.alloc, cost_buffer=cfg.cost_buffer,
                   fractional=cfg.fractional, min_order_usd=cfg.min_order_usd, fee_reserve=cfg.fee_reserve)
    orders = []
    try:
        acct = gbroker.get_account()
        ks.roll_day(acct.equity)
        dd = ks.check_daily_loss(acct.equity)
        ks.check_total_drawdown(acct.equity)   # GUARD-1 — 고점대비 누적 드로다운 한도
        ks.check_targets(weights)
        prior_qty = {p.symbol: p.qty for p in gbroker.get_positions()}   # 사후 정합성용
        # EXEC-2 — 매도 먼저 제출·체결확인 후 *실현* 현금으로 매수 재사이징. plan 의 매수예산은
        # '매도 예상 순현금'(추정)이라, 매도가 거부/미체결이면 자금 못 댄 매수가 과대배포된다.
        # 매도 체결 후 get_account().cash(실현) 로 cap_buys_to_cash → 미체결 매도분 매수는 축소/드롭.
        planned = exe.plan(weights)
        sells = [r for r in planned if r.side == Side.SELL]
        buys = [r for r in planned if r.side == Side.BUY]
        for r in sells:
            orders.append(gbroker.place_order(r))
        if buys:
            _await_fills(gbroker, orders, cfg.fill_timeout, cfg.fill_poll_interval)   # 매도 실현 확인
            realized_cash = gbroker.get_account().cash
            for r in exe.cap_buys_to_cash(buys, realized_cash):
                orders.append(gbroker.place_order(r))
    except HaltError as e:
        # 누적DD 트립이면 _halt_result 가 잔여 롱을 보호청산해 orders 에 병합(동결 해소).
        return {**_halt_result("tripped", str(e), ks, gbroker, orders),
                "selection": sel, "risk": risk}
    except Exception as e:
        try:
            ks.record_error(str(e))   # 에러 누적 → 한도 시 자동 정지(trip→HaltError raise 가능)
        except Exception:
            # record_error 가 던지는 HaltError(누적정지)·OSError(_save 실패) 등을 흡수 — 형제 except
            # HaltError(위)는 이미 통과해 못 잡는다. 무엇이 나오든 부분체결(orders)을 status='error' 로
            # 반드시 반환(불변식②: 체결분 보고 보존). record_error 는 best-effort 누적.
            pass
        return {"status": "error", "reason": str(e), "orders": _dump_orders(orders),
                "selection": sel, "risk": risk, **_acct_snapshot(gbroker)}

    # 체결 확인 — 비동기 체결 브로커 대비 종료상태까지 폴링(즉시체결이면 즉시 반환)
    orders = _await_fills(gbroker, orders, cfg.fill_timeout, cfg.fill_poll_interval)

    # 잔존 미체결 주문 취소(best-effort) — 미취소 DAY주문이 장중 늦게 체결되면 다음 런 재플랜이
    # 같은 의도를 또 매수(더블바이). 취소 성공분은 CANCELLED 로 표시 → record_fills 가 pending 해소.
    for o in orders:
        if o.status not in _TERMINAL and o.order_id:
            try:
                if gbroker.cancel_order(o.order_id):
                    o.status = OrderStatus.CANCELLED
            except Exception:
                pass

    # 관리 슬리브 basis 갱신 — 체결분으로 managed 수량 반영 (reconcile 前이라야 정합).
    # ManagedBroker 만 record_fills 보유(PaperBroker/TossBroker 는 없음 → getattr None).
    rec = getattr(gbroker, "record_fills", None)
    if rec is not None:
        try:
            rec(orders)
        except Exception:
            pass

    # 체결 결과 검증 — 미체결(거부/부분/취소/대기)을 성공으로 기록하지 않음
    bad = [o for o in orders if o.status != OrderStatus.FILLED]
    if bad:
        msg = "; ".join(f"{o.request.symbol} {o.status.value}: {o.message}" for o in bad)
        try:
            ks.record_error("주문 미완료: " + msg)   # 에러 누적 (한도 시 자동 정지)
        except HaltError:
            pass   # 정지 설정됨 — partial 로 보고하고 다음 실행이 재조정
        return {"status": "partial", "reason": msg, "orders": _dump_orders(orders),
                "selection": sel, "risk": risk, **_acct_snapshot(gbroker)}

    # 사후 정합성 — 브로커 실제 포지션이 (직전+체결) 기대치와 맞는지 대조
    drift = _reconcile(prior_qty, orders, gbroker)

    ks.record_success()
    ks.mark_traded()   # 당일 거래 완료 기록 → 중복 실행 차단

    return {
        "status": "ok",
        "daily_pnl": dd,
        "selection": sel,
        "risk": risk,
        "weights": {k: round(v, 4) for k, v in weights.items()},
        "orders": _dump_orders(orders),
        # drift==[] 만 검증완료-무드리프트. None 은 검증불가(조회실패) → ok=False 로 표면화(fail-open 차단).
        "reconcile": {"ok": drift == [], "drift": drift or [], "verified": drift is not None},
        **_acct_snapshot(gbroker),
    }
