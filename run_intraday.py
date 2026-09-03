"""run_intraday.py — 장중 액티브 트레이딩 루프 (paper 전용, 실주문 0).

일1런 선정(run_live)이 "오늘 무엇을 들까"를 정하면, 이 루프가 장중 내내 실시간 호가를 읽으며
"지금 사고/팔까"를 판정한다. 차트(1분봉) 움직임에 진입·청산·트레일링으로 대응하는 페르소나
(oneil·wood·livermore)를 굴린다.

설계:
  · 호가 = TossQuoteClient(실시간 스팟). 루프가 스팟을 샘플링해 1분봉 합성(BarAggregator) —
    Toss /prices 는 분봉·거래량 미제공이라 가격 velocity 로 컨비션 프록시(거래량 확인 강등).
  · 체결 = PaperBroker(price_fn=실시간 스팟, state_file=책). 새 체결코드 불요 — 다일 진화 책 영속.
  · 안전 = 실주문 0. 호가는 주문 메서드 부재의 TossQuoteClient 로만, 체결은 PaperBroker 로만.
    매매 자격증명은 이 헤드리스·비청취 루프에만(대시보드 tailnet 청취엔 0).
  · 라이프사이클 = 장개장 기동, 장마감 자가종료, 단일인스턴스 락.

핵심 엔진(BarAggregator·IntradayTrader)은 (quote_fn·clock·rule·broker) 주입으로 네트워크 없이
결정론 단위테스트된다. main() 은 실 컴포넌트 배선만.
"""
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable, Optional

from broker.base import OrderRequest, Side, OrderType, OrderStatus, floor_qty
# 락은 run_live(일1런)와 *같은* 구현을 쓴다 — 두 프로세스가 한 파일에 서로 다른 steal/heartbeat
# 규칙을 적용하던 것이 이 서브시스템의 락 버그 원천이었다(자체 O_EXCL 기계 + mtime 백데이트 해킹).
from broker.guardrail import RunLock, LockBusy

MAXBARS = 240            # 롤링 보관 1분봉 수(4h) — 장중 지표 충분
SAMPLE_SECONDS = 12      # 스팟 샘플 간격(초) — 1분봉당 ~5샘플
BAR_SECONDS = 60
KS_POLL_SECONDS = 30     # 킬스위치 파일 폴링 최소간격(초) — 샘플마다 읽지 않게 캡(디스크 I/O)
MIN_TRADE_NOTIONAL = 5.0  # 부분매도(트림) 최소 명목($) — 미만이면 전량청산(dust 매도 차단, 아래 _execute)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    import pytz
    _ET = pytz.timezone("America/New_York")


# ───────────────────────── 1분봉 합성 ─────────────────────────
@dataclass
class Bar:
    start: float        # 버킷 시작 epoch 초
    open: float
    high: float
    low: float
    close: float
    n: int              # 샘플 수


class BarAggregator:
    """스팟 샘플 → 고정폭 OHLC 바. add(ts,price) 가 *버킷 경계 넘을 때* 직전 완성바를 반환(아니면 None).

    Toss /prices 는 분봉을 안 주므로 루프가 직접 합성. 결정론 — 시각은 호출측이 ts 로 주입."""

    def __init__(self, bar_seconds: int = BAR_SECONDS):
        self.w = int(bar_seconds)
        self._cur: Optional[Bar] = None
        self._bucket: Optional[int] = None

    def add(self, ts: float, price: float) -> list:
        """샘플 1개 → 이번 샘플로 *닫힌* 바들의 리스트(0/1/다수). 피드 갭(호가 끊김)으로 건너뛴
        버킷은 직전 close 평탄바(n=0)로 메워, self.bars 가 벽시계 연속 1분봉이 되게 한다 —
        룰 window(피벗·MA·오프닝레인지)가 '직전 N분' 시간연속을 가정하므로."""
        b = int(ts // self.w)
        if self._bucket is None:                 # 첫 샘플
            self._bucket, self._cur = b, Bar(b * self.w, price, price, price, price, 1)
            return []
        if b == self._bucket:                    # 같은 버킷 — 갱신
            c = self._cur
            c.high = max(c.high, price); c.low = min(c.low, price)
            c.close = price; c.n += 1
            return []
        if b < self._bucket:                     # 시각 역행(클럭 이상) — 무시
            return []
        done = [self._cur]                        # 직전 바 확정
        close = self._cur.close
        # 갭 평탄충전 — 단 MAXBARS 로 캡(클럭점프/거대 ts 로 수백만 바 생성=메모리폭주 방지;
        # 캡 초과 갭은 어차피 룰 window 밖이라 채워도 무의미, 초과분은 생략).
        gap = min(b - self._bucket, MAXBARS + 1)
        for k in range(1, gap):
            done.append(Bar((self._bucket + k) * self.w, close, close, close, close, 0))
        self._bucket, self._cur = b, Bar(b * self.w, price, price, price, price, 1)
        return done


# ───────────────────────── 시그널 ─────────────────────────
@dataclass
class Signal:
    """장중룰 산출 의도. BUY=금액(소수주), SELL=수량, SELL_ALL=전량청산."""
    action: str                      # "BUY" | "SELL" | "SELL_ALL"
    amount: Optional[float] = None   # BUY 달러금액
    qty: Optional[float] = None      # SELL 수량
    reason: str = ""
    protective: bool = False         # 보호청산(손절·트레일·반전·익절) — 가드 정지·min-hold 무관 항상 허용
    pyramid: bool = False            # 피라미딩 추가매수 — adds 카운터는 *체결 시* 증가(거부 시 슬롯 미소모)


# ───────────────────────── 장중 트레이더(엔진) ─────────────────────────
class IntradayTrader:
    """페르소나 1종의 장중 실행 엔진 — 샘플→1분봉→룰→체결→저널. 전부 주입 → 결정론 테스트.

    rule(bars, pos, cash, ctx) -> list[Signal] (순수함수, intraday_rules 제공).
    guard(name, sym, sig, pos, acct) -> bool (P4; None=전부 허용).
    """

    def __init__(self, name: str, broker, quote_fn: Callable[[str], float], rule: Callable,
                 watchlist, cfg: dict = None, guard=None, log_dir: str = None,
                 bar_seconds: int = BAR_SECONDS, book_lock: str = None, regime_on: bool = True,
                 day_levels: dict = None, rule_state_file: str = None, accum_flags: dict = None,
                 resumed_today: bool = False, killswitch_file: str = None):
        self.name = name
        # 오늘 이미 활동 여부(build_traders 가 가드 선복원으로 판별) — flatten_carryover 가 *같은 세션
        # 내 재시작*(bars=1 보호청산이 이미 커버, P1-B1)과 *전일 이월*(처리 경로 없었음, P2-B5)을 구분.
        # 기본 False = 직접구성(테스트 등) 시 보유 있으면 이월로 간주(보수적 — 미상 시 flatten 허용).
        self._resumed_today = bool(resumed_today)
        self.broker = broker
        self.quote_fn = quote_fn
        self.rule = rule
        self.cfg = dict(cfg or {})
        self.guard = guard
        self.log_dir = log_dir
        # 공유책(일1런 daily_run 페르소나) 상호배제 락 경로(=일1런 STATE_DIR/run.lock). 세션 중
        # run_live 재실행과 동시쓰기(last-writer-wins 클로버) 방지 — 매매 직전 락 잡고 디스크 재동기화.
        self.book_lock = book_lock
        self._bar_seconds = bar_seconds   # 세션중 신규 워치종목(일1런 중간매수분) agg 생성용(CS-6)
        self._regime_on = bool(regime_on) # 세션중 신규 워치종목 ctx 주입용
        # 일봉 파생 레벨(sym → 직전 N세션 고점 등) — 스윙 룰 진입 피벗(ctx['day_high']). 세션중 불변.
        self._day_levels = dict(day_levels or {})
        # 매집 게이트(sym → bool) — accum_gate 다이얼 켠 룰만 소비(ctx['accum_ok'], 미포함=fail-open).
        self._accum = dict(accum_flags or {})
        # 룰 상태(트레일 hw 등) 디스크 영속 경로 — 오버나이트 페르소나(persist_state)만 배선.
        # 없으면(당일 전용·테스트) 인메모리 그대로 — 기존 동작 불변.
        self.rule_state_file = rule_state_file
        # 킬스위치(일1런 namespace paper_<persona> + 수동 HALT) — *읽기 전용* 소비(B6). 이 루프의
        # 유일한 수동 정지 수단(종전엔 프로세스 kill 뿐). halted=true → 신규진입·피라미딩 차단,
        # 보호청산은 계속(보유가 살아있는 장중에 탈출로까지 막는 게 더 위험).
        self.killswitch_file = killswitch_file
        self.ks_halted = False
        self._ks_checked = 0.0                # 마지막 폴 시각(KS_POLL_SECONDS 스로틀)
        self.watchlist = list(watchlist)
        self.aggs = {s: BarAggregator(bar_seconds) for s in self.watchlist}
        self.bars = {s: [] for s in self.watchlist}
        # 세션 개장(09:30 ET) epoch — livermore ORB 앵커 게이트. 장중 크래시/재부팅 재시작 시 bars 가
        # 재시작 시각부터 재축적돼 첫 orK 바가 재시작레인지로 오산출되던 것을, session_open+grace 창 밖
        # 첫 바 감지로 진입 스킵(intraday_rules.livermore_rule). 세션중 불변이라 1회 산출·전 종목 ctx 주입.
        self._session_open = _session_open_epoch()
        # SPY 200MA 레짐 — 약세장(OFF)이면 룰이 신규/추가 진입 BUY 를 차단(보호청산·트림은 무관 항상
        # 허용). 세션 시작 1회 산출값을 전 종목 ctx 에 주입(일봉 레짐은 장중 불변). 룰은 ctx.get(
        # "regime_on", True) 로 읽어 기본 허용(테스트·미주입 시 기존 동작 불변).
        self.ctx = {s: {"sym": s, "cfg": self.cfg, "state": {}, "regime_on": bool(regime_on),
                        "session_open": self._session_open, "day_high": self._day_levels.get(s),
                        "accum_ok": self._accum.get(s, True)}
                    for s in self.watchlist}
        self._load_rule_state()          # persist_state 페르소나 — 보유종목 트레일 앵커(hw 등) 세션 관통 복원
        # ctx['state'](트레일 hw·피라미딩 adds)가 정합하는 포지션 평단 지문 — reload 시 외부(run_live
        # 리밸런스)가 포지션을 바꿨는지 판정용(CS-1). 자기 체결은 지문 갱신해 오탐 방지.
        self._known_avg = {}             # sym -> ctx state 가 기준한 avg_price
        self.fills = []                  # 마지막 스냅샷 이후 체결(저널용)
        self.lock_miss = 0               # 공유책 락 미획득 누적(관측 — snapshot 레코드로 표면화)
        # 일중손실 baseline = *개장 자산*(보유분 포함). 가드의 지연캡처는 야간보유(oneil/wood)의
        # 개장~첫시그널 하락분을 baseline 에서 누락시키므로, 기동 시 브로커 equity 로 선seed.
        if self.guard is not None and hasattr(self.guard, "seed_day_start"):
            try:
                self.guard.seed_day_start(self.broker.get_account().equity)
            except Exception:
                pass

    def _sync_watchlist_holdings(self):
        """현 책 보유종목 중 워치 미포함분을 워치에 편입(CS-6) — 일1런이 세션 중(중간 재실행) 신규
        매수한 종목이 장중 손절/트림 보호를 못 받고 방치되던 것 차단. 공유책 reload(_on_bar)가 디스크
        변경을 인메모리에 반영한 뒤, 다음 sample 시작에 in-memory 보유로 워치 확장(추가 디스크 read 0).
        신규 종목은 agg/bars/ctx 초기화. 기존 보유·자기매수분은 이미 워치라 no-op."""
        try:
            held = [p.symbol for p in self.broker.get_positions() if p.qty > 1e-9]
        except Exception:
            return
        for s in held:
            if s not in self.aggs:
                self.aggs[s] = BarAggregator(self._bar_seconds)
                self.bars[s] = []
                self.ctx[s] = {"sym": s, "cfg": self.cfg, "state": {}, "regime_on": self._regime_on,
                               "session_open": self._session_open, "day_high": self._day_levels.get(s),
                               "accum_ok": self._accum.get(s, True)}
                self.watchlist.append(s)

    # ── 킬스위치(읽기 전용 소비) ─────────────────────────────────────────────
    def poll_killswitch(self, ts: float = None) -> bool:
        """일1런 킬스위치·수동 HALT 를 폴링해 self.ks_halted 갱신(B6). 파일은 절대 쓰지 않는다.

        KS_POLL_SECONDS 스로틀 — 12초 샘플마다 읽으면 페르소나×종목 없이도 하루 수천 회 stat/read.
        트립 전이에서만 알림 1건 + 저널 1건(매 폴 반복 안 함). 해제(사람이 reset)도 저널 1건 —
        래치를 인메모리로 들고 있으면 해제를 못 보고 종일 굶는다. 파일이 권위."""
        if not self.killswitch_file:
            return False
        t = time.time() if ts is None else float(ts)
        if self._ks_checked and (t - self._ks_checked) < KS_POLL_SECONDS:
            return self.ks_halted
        self._ks_checked = t
        halted, reason = _read_killswitch(self.killswitch_file)
        if halted != self.ks_halted:
            self.ks_halted = halted
            if self.guard is not None:                # 가드 allow() 진입 게이트에 반영(보호청산은 통과)
                self.guard.ks_halted = halted
            rec = {"ts": _now_iso(), "persona": self.name,
                   "action": "KILLSWITCH_HALT" if halted else "KILLSWITCH_CLEAR",
                   "symbol": "", "qty": 0, "price": 0,
                   "reason": (reason or "킬스위치 정지") if halted else "킬스위치 해제 — 진입 재개"}
            self._journal_action(rec)
            if halted:
                print(f"[intraday:{self.name}] 킬스위치 정지 감지 — 신규진입 차단(보호청산 유지): {reason}",
                      file=sys.stderr)
                try:
                    from notify import notify
                    notify(f"장중 루프 진입 정지 — {self.name}: {reason} (보호청산은 계속)", "halt")
                except Exception:                     # 알림 실패가 매매를 막지 않게(notify 계약과 동일)
                    pass
        elif self.guard is not None and getattr(self.guard, "ks_halted", None) != halted:
            self.guard.ks_halted = halted             # 가드 교체·첫 폴 동기화(전이 없을 때도 정합)
        return self.ks_halted

    # ── 룰 상태 영속(오버나이트 페르소나) ────────────────────────────────────
    def _load_rule_state(self):
        """rule_state_file 의 {sym: state} 복원 — *현재 책 보유종목만*(flat 종목의 armed/hw 잔재는
        폐기 = 스테일 무장·유령 트레일 방지). 비유한 float(nan/inf) 포함 종목은 그 종목만 신규 seed
        — nan 트레일 임계는 비교가 전부 False 라 보호청산이 영구 침묵한다(가드 불변식 위반). 복원
        실패·미존재는 무해: 룰의 prev_hw 기본이 avg_price 라 트레일이 평단 기준 자가재구축."""
        if not self.rule_state_file or not os.path.exists(self.rule_state_file):
            return
        # 보유종목을 *먼저* 워치에 편입(B14). 아래 복원 조건의 `sym not in self.ctx` 가 사실상
        # watchlist 소속 검사였어서, watchlist 를 편집해 보유종목이 목록에서 빠지면 그 종목의 다일 hw
        # 트레일이 복원 대상에서 탈락 → 다음 바에 평단 재앵커(러너 이익 반납) + _save_rule_state 가
        # 빈 상태로 덮어써 영구 소실. 샘플루프가 어차피 하는 편입을 여기서 선행할 뿐(멱등, 디스크 read 0).
        self._sync_watchlist_holdings()
        import math
        try:
            with open(self.rule_state_file, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[intraday:{self.name}] 룰상태 로드 실패(신규 seed): {e!r}", file=sys.stderr)
            return
        try:
            held = {p.symbol for p in self.broker.get_positions() if p.qty > 1e-9}
        except Exception:
            held = set()
        for sym, st in (raw or {}).items():
            if sym not in held or sym not in self.ctx or not isinstance(st, dict):
                continue
            if any(isinstance(v, float) and not math.isfinite(v) for v in st.values()):
                continue                        # 손상 항목 — 그 종목만 신규 seed(전체 폐기 아님)
            self.ctx[sym]["state"].update(st)

    def _save_rule_state(self):
        """ctx['state'] 비공집합 종목만 원자적 저장(guard._save 패턴). rule_state_file 없으면 no-op.
        크래시 시 마지막 저장 이후 분(分) 단위 hw 전진만 소실 — 트레일 폭이 소폭 넓어질 뿐 보호는 유지."""
        if not self.rule_state_file:
            return
        data = {s: c["state"] for s, c in self.ctx.items() if c.get("state")}
        tmp = f"{self.rule_state_file}.{os.getpid()}.tmp"
        try:
            os.makedirs(os.path.dirname(self.rule_state_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            from paths import atomic_replace
            if not atomic_replace(tmp, self.rule_state_file):
                print(f"[intraday:{self.name}] 룰상태 교체 실패(다음 저장서 재시도)", file=sys.stderr)
        except Exception as e:
            try:
                os.remove(tmp)
            except OSError:
                pass
            print(f"[intraday:{self.name}] 룰상태 저장 실패: {e!r}", file=sys.stderr)

    def sample(self, ts: float):
        """모든 워치종목 1샘플 — 바 완성 시 룰 평가·체결. 호가 실패는 그 종목만 skip."""
        self.poll_killswitch(ts)                # 수동/일1런 정지 반영(읽기 전용, KS_POLL_SECONDS 스로틀)
        self._sync_watchlist_holdings()         # 일1런 세션중 신규매수분 워치 편입(CS-6, in-memory 기반)
        dirty = False                           # 이번 샘플서 룰 평가(=상태 변이 가능) 발생 여부
        for sym in self.watchlist:
            try:
                px = float(self.quote_fn(sym))
            except Exception:
                continue
            if px <= 0:
                continue
            closed = self.aggs[sym].add(ts, px)
            if not closed:
                continue
            lst = self.bars[sym]
            lst.extend(closed)                  # 닫힌 바 전부 보관(룰 window 시간연속)
            if len(lst) > MAXBARS:
                del lst[:-MAXBARS]
            # 한 샘플이 여러 바를 닫아도(거대 클럭점프 → 평탄 gap-fill) _on_bar 는 1회만 — 평탄바(n=0)는
            # 동일가라 룰상 no-op 이고, bars[-1] 최종상태로 1회 평가하면 충분. 매 바 호출 시 공유책 경로의
            # 락 획득·reload(디스크 read) 가 최대 MAXBARS 회 폭주하던 것 제거(결과 동일).
            try:
                self._on_bar(sym)               # 한 종목 예외가 다른 종목·페르소나로 안 번지게 격리
                dirty = True
            except Exception as e:
                print(f"[intraday:{self.name}] _on_bar 예외 {sym}: {e!r}", file=sys.stderr)
        if dirty:
            self._save_rule_state()             # 룰상태(hw 트레일 등) 변이 가능 시점마다 영속(분당 ≈1회)

    def _eval(self, sym: str):
        """현재 브로커 상태로 룰 평가 → 시그널 리스트. 락 임계구역서 reload 직후 재평가하면
        stale 진입·중복매수(동시 run_live 가 이미 매수)를 차단(post-reload pos 가 _flat 아님)."""
        pos = self.broker.get_position(sym)
        try:
            acct = self.broker.get_account()
            cash = acct.cash
            # equity(현금+보유평가액) 를 ctx 에 매 평가 주입 — 룰의 entry_frac/add_frac 사이징 기준
            # (감사: 절대금액 하드코딩→equity 비율 전환). 조회실패는 cash 와 동일하게 fail-closed(0.0)
            # → frac*0=0 금액 시그널이 _execute 에서 amt<=0 로 무해 no-op(그 사이클 진입 스킵).
            self.ctx[sym]["equity"] = acct.equity
        except Exception:
            cash = 0.0
            self.ctx[sym]["equity"] = 0.0
        try:
            return self.rule(self.bars[sym], pos, cash, self.ctx[sym]) or []
        except Exception as e:
            print(f"[intraday:{self.name}] rule 예외 {sym}: {e!r}", file=sys.stderr)
            return []

    def _note_lock_miss(self, where: str, sym: str = "-"):
        """공유책 락 미획득 관측 — 종전엔 완전 무음이라 '장중에 왜 아무 것도 안 샀나'가 사후 추적
        불가였다(일1런 장기점유·좀비락 모두 정상 동작과 구분 안 됨). 누적 카운트는 snapshot
        레코드의 lock_miss 로 대시보드까지 표면화. 매매 판단엔 관여하지 않는다(관측 전용)."""
        self.lock_miss += 1
        print(f"[intraday:{self.name}] 공유책 락 미획득({where} {sym}) — 보류 #{self.lock_miss}",
              file=sys.stderr)

    def _on_bar(self, sym: str):
        if self.book_lock is None:                  # 비공유책(livermore) — 직접 체결
            for sig in self._eval(sym):
                self._execute(sym, sig)
            return
        # 공유책(oneil/wood) — 일1런 run.lock 도메인 직렬화(RunLock = run_live 와 동일 구현·동일
        # steal 규칙·하트비트. 자체 락 기계 + mtime 백데이트로 상대 프로토콜을 속이던 것 제거).
        # 락 못 잡으면(일1런이 책 read-modify-write 중) 보호청산 포함 *전부 보류*(다음 바 재평가).
        # 락 없이 stale 인메모리로 _save 하면 run_live 의 동시쓰기를 lost-update 로 클로버
        # (유령매도/현금이중계상) — 이를 막는 게 1바 청산지연(paper·무해)보다 우선. _eval 도 호출
        # 안 함(룰의 hw 부작용이 보류 바에서 add 기회를 태우지 않게). 가드는 여전히 보호청산 항상
        # 허용(불변식② 유지); 락은 동시 run_live 시 *타이밍만* 직렬화(평시 일1런은 _await_daily_runs
        # 로 선완료 → 장중 단독 → 락 항상 즉시 획득, 영향 0).
        # ponytail: 임계구역마다 RunLock 하트비트 스레드 1개 생성(바당 ~ms). 바 수 × 종목 수 규모라
        #           무시 가능 — 문제되면 guardrail 에 no-heartbeat 옵션 추가가 업그레이드 경로.
        # steal_dead_after=120: 죽은 pid 락은 2분 내 회수(기본 30분 대신) — run_live 크래시 시
        # 이 보호청산이 장기간 막히는 것 방지. Windows 는 보유자가 fd 를 연 채면 rename steal 이
        # sharing violation 으로 실패해 *살아있는* 락은 짧은 값이어도 오탈취 불가(broker/guardrail.py:96-99).
        try:
            with RunLock(Path(self.book_lock), steal_dead_after=120):
                try:
                    self.broker.reload()             # run_live 변경 흡수(stale 인메모리 덮어쓰기 방지)
                except Exception as e:
                    # reload 실패 = 인메모리가 stale 인지 *알 수 없음* → fail-closed(이 바 스킵, 락 반납).
                    # 삼키고 진행하면 위 주석이 막으려던 바로 그 클로버(stale 책으로 _execute→_save).
                    print(f"[intraday:{self.name}] 책 reload 실패 — 이 바 스킵 {sym}: {e!r}",
                          file=sys.stderr)
                    return
                self._reconcile_ctx_state(sym)       # 외부 포지션 변경 시 stale 트레일/피라미딩 상태 폐기(CS-1)
                for sig in self._eval(sym):          # post-reload 재평가 → stale 진입·중복매수 차단
                    self._execute(sym, sig)
        except LockBusy:
            self._note_lock_miss("_on_bar", sym)

    def _reconcile_ctx_state(self, sym: str):
        """reload 직후 ctx['state'](트레일 hw·피라미딩 adds)를 현 포지션과 정합화 — 외부(run_live
        리밸런스)가 포지션을 신규/교체(평단 변동)했는데 옛 보유 기준 트레일·피라미딩 상태가 잔존해
        잘못된 트레일손절·과다 피라미딩으로 오발화하던 것 차단(CS-1). flat 이면 상태·지문 비움.
        자기 체결은 _execute 가 _known_avg 를 갱신하므로 여기서 오탐(자기 피라미딩→clear)은 안 남."""
        pos = self.broker.get_position(sym)
        st = self.ctx[sym]["state"]
        if pos is None or pos.qty <= 1e-9:
            st.clear()
            self._known_avg.pop(sym, None)
            return
        cur = float(pos.avg_price)
        known = self._known_avg.get(sym)
        if known is None or abs(known - cur) > 1e-6:   # 외부 변경(또는 첫 관측) → 트레일/피라미딩 상태 폐기
            st.clear()
            self._known_avg[sym] = cur

    def _execute(self, sym: str, sig: Signal):
        # 킬스위치 정지 중 — 신규진입·피라미딩 차단, 보호청산(SELL/SELL_ALL protective)만 통과(B6).
        # 가드(allow)에도 같은 게이트가 있으나 guard=None 배선(직접구성·테스트)에도 정지가 유효해야
        # 하므로 체결 직전 이 한 곳에서 확정한다 — BUY 에 protective 플래그가 붙어도 우회 불가.
        if self.ks_halted and not (sig.protective and sig.action in ("SELL", "SELL_ALL")):
            return
        pos = self.broker.get_position(sym)
        try:
            acct = self.broker.get_account()
        except Exception:
            acct = None
        try:
            last_px = float(self.quote_fn(sym))     # 시가 기준 비중캡(원가 avg 와 혼용 방지)
        except Exception:
            last_px = None
        if self.guard is not None and not self.guard.allow(self.name, sym, sig, pos, acct, last_px):
            return
        if sig.action == "BUY":
            amt = float(sig.amount or 0.0)
            if amt <= 0:
                return
            req = OrderRequest(sym, Side.BUY, qty=0.0, order_type=OrderType.MARKET, amount=amt)
        elif sig.action in ("SELL", "SELL_ALL"):
            if pos is None or pos.qty <= 1e-9:
                return
            # SELL_ALL=보유 전량 정확청산(절사 안 함). 부분트림(SELL)=2자리 절사(정책, 소수주 둘째자리까지만).
            if sig.action == "SELL_ALL":
                qty = pos.qty
            else:
                raw = min(float(sig.qty or 0.0), pos.qty)
                qty = floor_qty(raw)                          # 부분트림 2자리 절사(정책)
                if qty <= 1e-9 and raw > 1e-9:                # 트림이 <0.01주로 절사 소멸(고가·소수주) → 최소 거래증분만
                    qty = min(0.01, pos.qty)                  #   (보유<0.01주면 전량). floor→0 조기반환으로 dust-flush 우회하던 무한 no-op 회귀 차단.
                # executor._plan_fractional 대칭 — 잔량 dust면 전량청산, 트림명목<MIN & 잔량 건강이면 무거래밴드.
                px = last_px if (last_px and last_px > 0) else (pos.avg_price or 0.0)
                if px > 0 and 0 < (pos.qty - qty) * px < MIN_TRADE_NOTIONAL:
                    qty = pos.qty                         # 트림 후 잔량이 dust → 전량청산(1회 클린 종결)
                elif px > 0 and qty * px < MIN_TRADE_NOTIONAL:
                    return                                # 트림 명목<MIN & 잔량 건강 → 미세트림 스킵(무거래밴드, churn 차단)
            if qty <= 1e-9:
                return
            req = OrderRequest(sym, Side.SELL, qty=qty, order_type=OrderType.MARKET)
        else:
            return
        try:
            order = self.broker.place_order(req)   # BUY 는 가격필수 — Toss 일시 호가실패 시 raise
        except Exception as e:
            print(f"[intraday:{self.name}] 주문 실패 {sym} {sig.action}: {e!r}", file=sys.stderr)
            return                                 # 그 주문만 skip — 루프·다른 종목·페르소나로 전파 금지
        if order.status == OrderStatus.FILLED:
            # 체결 후 평단 지문 갱신 — 자기 체결(매수·피라미딩=평단변동)이 다음 reconcile 에서
            # '외부 변경'으로 오판돼 트레일/피라미딩 상태가 폐기되지 않게(CS-1 오탐 방지). flat=제거.
            # npos 를 note_fill 앞에서 조회 — 매도가 포지션을 flat 으로 만들었는지 판정에 필요.
            npos = self.broker.get_position(sym)
            flat = npos is None or npos.qty <= 1e-9
            if not flat:
                self._known_avg[sym] = float(npos.avg_price)
            else:
                self._known_avg.pop(sym, None)
            if self.guard is not None:
                # 매도가 포지션을 flat 으로 만들면(트림→dust 전량청산 포함) 비보호라도 재진입 쿨다운 기록
                # — 트림 청산 직후 다음 바 즉시 재매수(whipsaw) 차단.
                # notional = 체결 명목($) — 회전 비용 계측 전용(B10). 보호청산은 회전캡 면제 유지.
                self.guard.note_fill(sym, sig, flattened=(flat and sig.action in ("SELL", "SELL_ALL")),
                                     notional=order.filled_qty * order.avg_fill_price)
            if sig.pyramid:                      # 피라미딩 슬롯은 *체결 성사 시*에만 소모(가드 거부 시 보존)
                st = self.ctx[sym]["state"]
                st["adds"] = st.get("adds", 0) + 1
            rec = {"ts": _now_iso(), "persona": self.name, "action": sig.action, "symbol": sym,
                   "qty": round(order.filled_qty, 6), "price": round(order.avg_fill_price, 4),
                   "reason": sig.reason}
            self.fills.append(rec)
            self._journal_action(rec)

    def eod_flatten(self):
        """세션 자연마감 시 전 보유 청산 — cfg `eod_flatten` 페르소나만(no-op 기본).

        오버나이트 보유가 ①현금 기아(잔여 cash < entry/add_frac×equity → 매수측 영구 불통과,
        intraday_rules 게이트)와 ②트레일 앵커 세션리셋(ctx state 인메모리 → 매일 avg_price
        재앵커, 며칠짜리 실고점 망각)과 겹치면 책이 좀비 buy-and-hold 로 동결된다(livermore
        2026-06-26 실증). 마감 전량청산으로 매 세션 풀현금 ORB 재가동 = 장중 전용 정체성 복원.
        protective=True — 가드 halt·min-hold·쿨다운 무관 항상 허용(탈출로 불변식과 동일 경로).
        공유책(book_lock)이면 _on_bar 와 동일한 락 규율로 직렬화(무락 _save 클로버 방지)."""
        if not self.cfg.get("eod_flatten"):
            return

        def _flatten():
            for p in list(self.broker.get_positions()):
                if p.qty <= 1e-9:
                    continue
                self._execute(p.symbol, Signal("SELL_ALL", reason="EOD 청산", protective=True))

        if self.book_lock is None:                   # 비공유책(livermore/chartist) — 직접
            _flatten()
            return
        try:
            with RunLock(Path(self.book_lock), steal_dead_after=120):   # 근거: _on_bar 주석(:302-304)
                try:
                    self.broker.reload()
                except Exception as e:               # reload 실패 → 청산 보류(stale 책 전량청산 금지)
                    print(f"[intraday:{self.name}] eod_flatten 책 reload 실패 — 청산 보류: {e!r}",
                          file=sys.stderr)
                    return
                _flatten()
        except LockBusy:                             # 일1런이 책 쓰는 중 — 보류(클로버 방지 우선, paper 무해)
            self._note_lock_miss("eod_flatten")

    def flatten_carryover(self):
        """개장 처리 — *전일 이월 포지션* 1회 flat(P2-B5). eod_flatten() 은 세션 자연마감에서만 발화하는
        단발이라, 크래시·16시 이후 재시작으로 그 기회를 놓치면 포지션이 다음 세션까지 이월돼도
        처리 경로가 없었다. main() 이 개장 대기 통과 직후(샘플루프·_sync_watchlist_holdings 전) 호출.

        resumed_today(build_traders 가 가드 day_start_equity 선복원 여부로 판별)로 *같은 세션 내
        재시작*과 구분 — 그 경우는 보호청산이 이미 bars 1개부터 평가되므로(P1-B1) 트레일로 계속
        관리한다. resumed_today=False(오늘 첫 기동)인데 보유가 있으면 그 보유는 정의상 전일 이전
        잔재 — 시장가 전량청산 + 저널 1건(reason 으로 EOD 정상청산과 구분) + stderr 경보.
        eod_flatten() 과 동일 청산/락 경로 재사용(_execute SELL_ALL protective)."""
        if not self.cfg.get("eod_flatten") or self._resumed_today:
            return

        def _flatten():
            for p in list(self.broker.get_positions()):
                if p.qty <= 1e-9:
                    continue
                print(f"[intraday:{self.name}] 이월 포지션 개장청산 {p.symbol} {p.qty}주(EOD 청산 누락 복구)",
                      file=sys.stderr)
                self._execute(p.symbol, Signal("SELL_ALL", reason="이월 포지션 개장청산", protective=True))

        if self.book_lock is None:                   # 비공유책(livermore/chartist) — 직접
            _flatten()
            return
        try:
            with RunLock(Path(self.book_lock), steal_dead_after=120):   # 근거: _on_bar 주석(:302-304)
                try:
                    self.broker.reload()
                except Exception as e:               # reload 실패 → 청산 보류(stale 책 전량청산 금지)
                    print(f"[intraday:{self.name}] flatten_carryover 책 reload 실패 — 청산 보류: {e!r}",
                          file=sys.stderr)
                    return
                _flatten()
        except LockBusy:                             # 일1런이 책 쓰는 중 — 보류(클로버 방지 우선, paper 무해)
            self._note_lock_miss("flatten_carryover")

    # ── 저널(대시보드 피드) ──────────────────────────────────────────────────
    def _journal_action(self, rec: dict):
        if not self.log_dir:
            return
        _append_jsonl(os.path.join(self.log_dir, "intraday.jsonl"), rec)

    def snapshot(self, session: str):
        """현 책을 runs.jsonl 레코드로 기록 — 기존 대시보드(read_engine_state)가 무수정 픽업.
        직전 스냅샷 이후 체결을 orders 로 첨부. log_dir 없으면 no-op."""
        if not self.log_dir:
            self.fills = []
            return
        try:
            acct = self.broker.get_account()
            if self.guard is not None and hasattr(self.guard, "mark_equity"):
                self.guard.mark_equity(acct.equity)   # 마감 equity 영속 → *다음날* 갭 baseline(B7)
            # 보호선(손절/목표) 동봉 — 트레일 hw·chartist 레벨은 인메모리(ctx)에만 있어 저널 스냅샷이
            # 유일한 관측창. 산식 = intraday_rules.protective_levels(룰과 동일식). 관측 전용(매매 무관).
            from intraday_rules import protective_levels
            rkey = getattr(self.rule, "__name__", "").replace("_rule", "")
            positions = []
            for p in self.broker.get_positions():
                d = {"symbol": p.symbol, "qty": round(p.qty, 6), "avg": round(p.avg_price, 4)}
                lv = protective_levels(rkey, (self.ctx.get(p.symbol) or {}).get("state"),
                                       self.cfg, p.avg_price, bars=self.bars.get(p.symbol))
                if lv.get("stop") is not None:
                    d["stop"] = lv["stop"]
                if lv.get("target") is not None:
                    d["target"] = lv["target"]
                positions.append(d)
        except Exception as e:
            print(f"[intraday:{self.name}] snapshot 실패: {e!r}", file=sys.stderr)
            return
        orders = [{"symbol": f["symbol"], "side": ("BUY" if f["action"] == "BUY" else "SELL"),
                   "qty": f["qty"], "fill": f["price"], "status": "FILLED",
                   "reason": f.get("reason", "")} for f in self.fills]   # 장중 룰 사유(돌파/손절/트림/익절…) — 대시보드 기록칸
        halted = bool(getattr(self.guard, "halted", False)) if self.guard is not None else False
        ds = getattr(self.guard, "day_start_equity", None) if self.guard is not None else None
        daily_pnl = (acct.equity / ds - 1.0) if ds else 0.0       # 대시보드 당일손익(개장자산 baseline)
        rec = {"ts": _now_iso(), "broker": "paper", "persona": self.name, "session": session,
               "status": "intraday", "intraday": True, "halted": halted,   # 일중손실 정지 표면화(대시보드)
               "ks_halted": self.ks_halted,         # 외부 킬스위치 정지(B6) — 일중손실 halt 와 구분 표면화
               "daily_pnl": round(daily_pnl, 4),
               "lock_miss": self.lock_miss,         # 공유책 락 미획득 누적 — 무음 보류 관측(0 이 정상)
               "account": {"cash": round(acct.cash, 2), "equity": round(acct.equity, 2)},
               "positions": positions, "orders": orders,
               "reconcile": {"ok": True, "drift": []}}
        if self.guard is not None and hasattr(self.guard, "turnover_summary"):
            rec["turnover"] = self.guard.turnover_summary()   # 하루 회전 요약(보호청산 건수·명목, B10)
        path = os.path.join(self.log_dir, "runs.jsonl")
        if self.book_lock:                          # 공유책(oneil/wood) — 일1런 run.lock 도메인 직렬화:
            try:                                    # run_live._journal(RunLock) append 와 인터리브/lost-update 방지
                with RunLock(Path(self.book_lock), steal_dead_after=120):   # 근거: _on_bar 주석(:302-304)
                    _append_jsonl(path, rec)
            except LockBusy:
                self._note_lock_miss("snapshot")
                _append_jsonl(path, rec)            # 락 못 잡음(일1런 진행중) → best-effort 무락(저널누락보다 나음)
        else:
            _append_jsonl(path, rec)                # 비공유책(livermore) — 전용 runs.jsonl, 경합 없음
        self.fills = []


# ───────────────────────── 유틸 ─────────────────────────
def _now_et() -> datetime:
    return datetime.now(timezone.utc).astimezone(_ET)


def _now_iso() -> str:
    # run_live._journal 과 *동일* 포맷(naive 호스트로컬·초단위). oneil/wood 는 일1런과 같은 runs.jsonl 을
    # 공유하는데, ts 포맷이 다르면(UTC '+00:00' vs naive KST) 대시보드 사전식 ts 정렬에서 장중 레코드가
    # 아침 run_live 레코드 뒤로 밀려, 패널·intraday배지·halted·daily_pnl 이 종일 아침상태로 동결된다.
    return datetime.now().isoformat(timespec="seconds")


def et_session(now: datetime = None) -> str:
    return (now or _now_et()).strftime("%Y-%m-%d")


def _session_open_epoch(now: datetime = None) -> float:
    """오늘(ET) 정규장 개장(09:30 ET)의 epoch 초 — livermore ORB 앵커 게이트 기준(트레이더 구성 시 1회 산출).
    장중 크래시/VM 재부팅 재시작으로 bars 가 재시작 시각부터 재축적되면, 이 기준+grace 창 밖에서 시작한
    첫 바를 감지해 그날 ORB 진입을 스킵(intraday_rules.livermore_rule). DST 는 _ET(zoneinfo) 가 처리."""
    n = now or _now_et()
    return n.replace(hour=9, minute=30, second=0, microsecond=0).timestamp()


_SESSION_MIN = {}        # ET date -> (개장분, 마감분) | None(휴장). 날짜당 캘린더 1회 조회 캐시
_SESSION_WARNED = [False]


def _session_minutes(day):
    """그날(ET) 정규장 (개장분, 마감분) — 자정 기준 분. 휴장일 None, 캘린더 불가 시 (570, 960) 폴백.

    조기마감(반일장 13:00 ET, 연 2~3일: 추수감사절 다음날·크리스마스이브 등)을 하드코딩 목록 없이
    NYSE 캘린더에서 직접 읽는다 — calendar_util 이 이미 쓰는 pandas_market_calendars(XNYS)를 그대로
    재사용(신규 의존성 0, 조회 패턴은 dashboard.build_data.next_session_iso 와 동일). 16:00 고정창은
    조기마감일에 EOD 청산을 3시간 늦은 스테일 호가로 내보낸다."""
    if day in _SESSION_MIN:
        return _SESSION_MIN[day]
    try:
        import calendar_util as cu
        s = cu._NYSE.schedule(start_date=day.isoformat(), end_date=day.isoformat())
        if s.empty:
            out = None                                   # 휴장(주말·공휴일)
        else:
            o = s["market_open"].iloc[0].tz_convert(cu.ET)
            c = s["market_close"].iloc[0].tz_convert(cu.ET)
            out = (o.hour * 60 + o.minute, c.hour * 60 + c.minute)
    except Exception as e:                               # 캘린더 불가 → 종전 고정창(공휴일·조기마감 미반영)
        if not _SESSION_WARNED[0]:                       # 경고 1회만(12초 루프서 로그 폭주 방지)
            _SESSION_WARNED[0] = True
            print(f"[intraday] NYSE 캘린더 조회 실패 — 09:30~16:00 고정창 폴백: {e!r}", file=sys.stderr)
        return (570, 960)                                # 캐시 안 함 — 일시 실패면 다음 호출서 재시도
    _SESSION_MIN[day] = out
    return out


def market_is_open(now: datetime = None) -> bool:
    """미 정규장 여부 — 공휴일·조기마감(13:00)까지 NYSE 캘린더로 판정(_session_minutes).
    캘린더 불가 시에만 평일 09:30~16:00 고정창 폴백."""
    now = now or _now_et()
    if now.weekday() >= 5:
        return False
    b = _session_minutes(now.date())
    if b is None:
        return False                                     # 휴장일
    return b[0] <= now.hour * 60 + now.minute < b[1]


def _wait_until_open(max_wait=4 * 3600, poll=30, sleep_fn=None, now_fn=None):
    """개장 전(평일·09:30 ET 이전)에 기동되면 개장까지 대기 후 True. 주말·마감후면 즉시 False.
    폴백 트리거(-Daily 고정시각)가 DST 따라 개장 전 발화해도 루프가 즉시 죽지 않고 개장에 맞춰 가동."""
    sleep_fn = sleep_fn or time.sleep
    now_fn = now_fn or _now_et
    start = time.time()
    while time.time() - start < max_wait:
        now = now_fn()
        if market_is_open(now):
            return True
        if now.weekday() >= 5:
            return False                       # 주말 — 대기 무의미
        b = _session_minutes(now.date())
        if b is None or now.hour * 60 + now.minute >= b[1]:
            return False                       # 휴장일 or 오늘 마감 이후(조기마감이면 13:00 기준)
        sleep_fn(poll)                         # 개장 전 → 대기
    return market_is_open()


def _append_jsonl(path: str, rec: dict):
    from paths import append_jsonl_rotating
    try:
        append_jsonl_rotating(path, rec)
    except Exception as e:
        print(f"[intraday] 저널 실패 {path}: {e!r}", file=sys.stderr)


def killswitch_path(home, name: str) -> str:
    """페르소나 킬스위치 상태파일 — 일1런(run_live)이 namespace `paper_<persona>` 로 쓰는 바로 그 파일
    (run_live.py:191). run_live 태스크는 USTRADE_HOME=persona_home 이라 guardrail.STATE_DIR=<home>/state."""
    return os.path.join(str(home), "state", f"killswitch.paper_{name}.json")


def _read_killswitch(path: str):
    """(halted, reason) — 일1런 킬스위치의 *읽기 전용* 소비. 장중루프는 이 파일들을 절대 쓰지 않는다
    (소유권=일1런·guardrail; 쓰면 두 프로세스가 서로의 래치를 덮는다).

    판정은 guardrail.KillSwitch.is_halted 와 동형: 수동 HALT 파일 · 영속실패 마커(.halt) · 상태 JSON
    halted. KillSwitch 를 인스턴스화하지 않는 이유 = 그 클래스는 *이 프로세스의* STATE_DIR(USTRADE_HOME)
    에 묶여 있어 페르소나별 home 을 못 읽고, __init__ 이 디렉터리를 만든다(읽기전용 계약 위반).
    JSON 손상은 fail-closed(halted) — guardrail._load_state_file 과 동일 철학(의심스러우면 정지).
    장중에서 halted 는 '신규진입 차단'일 뿐 보호청산은 계속되므로 이 보수성의 비용은 진입 스킵뿐."""
    state_dir = os.path.dirname(path)
    if os.path.exists(os.path.join(state_dir, "HALT")):
        return True, "수동 HALT 파일 존재 (state/HALT)"
    marker = path[:-5] + ".halt" if path.endswith(".json") else path + ".halt"
    if os.path.exists(marker):
        return True, "킬스위치 영속실패 마커 존재 — 수동 확인 필요"
    if not os.path.exists(path):
        return False, ""                       # 킬스위치 미생성(일1런 없는 장중전용 페르소나) = 정상
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            raise ValueError("dict 아님")
    except Exception as e:
        return True, f"킬스위치 상태파일 손상 — 수동 확인 필요 ({type(e).__name__})"
    return bool(d.get("halted")), str(d.get("reason") or "")


# ───────────────────────── main 배선 ─────────────────────────
def persona_home(name: str) -> str:
    """페르소나 격리 home — paths.persona_homes(머신 env 정규 파서)에서 매칭,
    없으면 C:\\ustrade-paper-<name> 기본. 각 페르소나 책·로그 격리(일1런과 동일 home 공유)."""
    from paths import persona_homes
    for h in persona_homes():
        if h.name == f"ustrade-paper-{name}":
            return str(h)
    return os.path.join("C:\\", f"ustrade-paper-{name}")


def _last_selection_final(home, session=None):
    """home/logs/runs.jsonl 의 일1런 selection.final — '오늘 브레인이 고른' 종목.
    페르소나 일1런(run_live)은 USTRADE_HOME=persona_home 이라 같은 home/logs 에 저널링(레코드에
    "session"=거래대상세션(=last_completed_session) 포함). 장중 스냅샷엔 selection 키가 없어 일1런만 골라짐.

    ⚠️ session 지정 시 그 세션과 일치하는 일1런 레코드만 채택(신선도 가드). 당일 일1런이 미시작·skip·
    stale·halt·crash 로 끝나(selection 없거나 구세션) runs.jsonl 의 마지막 selection 이 *전일분*이면
    [] 반환 → 호출측이 보유/정적 폴백(어제 픽으로 오늘 장중 신규진입하던 stale 오염 차단). session 키는
    run_live 가 last_completed_session().isoformat() 으로 박으므로, 장중 루프도 동일 함수로 기대세션을
    도출해 비교(et_session 달력일과 다를 수 있어 today 단순비교는 정당 픽을 기각 → 반드시 동일소스 사용).
    session=None 이면 신선도 무관 마지막 채택(테스트·명시 폴백용). 파일/파싱 실패는 빈 리스트."""
    path = os.path.join(str(home), "logs", "runs.jsonl")
    final = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if '"selection"' not in line:            # 일1런 레코드만(장중 스냅샷은 selection 무)
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if session is not None and rec.get("session") != session:
                    continue                             # 신선도: 기대세션(당일 일1런)만 채택
                sel = rec.get("selection")
                # selection.final 은 오버레이 前 원시픽 — 레짐 OFF(약세장 전량현금) 또는 미거래(tripped/error/
                # partial: weights 키 없음)면 브레인이 진입 안 한 픽이므로 워치서 배제. 안 그러면 장중이
                # (레짐 게이트 fail-open 시) 약세장 보호로 0된 픽에 신규진입해 레짐 보호를 무력화.
                traded = bool(rec.get("weights")) and (rec.get("risk") or {}).get("regime") != "OFF"
                if isinstance(sel, dict) and sel.get("final") and traded:
                    final = list(sel["final"])           # append-only → (필터 후) 마지막이 최신
    except OSError:
        pass
    return final


def _daily_watchlist(home, broker, session=None):
    """daily_run 페르소나 장중 워치 = 일1런 선정분(selection.final, 신선도 가드) ∪ 현 책 보유종목.
    '브레인이 오늘 고른 것'을 장중이 매매·관리(정적 하드코딩 단절 해소). 보유종목은 선정서 빠져도
    포함 — 일1런이 산 비선정 종목도 장중 손절/트림 보호받게(무보호 방치 차단). 당일 선정이 stale 면
    picks=[] 이라 보유분만 남음(어제 픽 신규진입 차단). 둘 다 비면 빈 리스트(→ 정적 폴백)."""
    out, seen = [], set()
    picks = _last_selection_final(home, session=session) if home else []
    held = []
    if broker is not None:
        try:
            held = [p.symbol for p in broker.get_positions()]
        except Exception:
            held = []
    for t in list(picks) + held:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _watchlist_for(meta, home=None, broker=None, session=None):
    """장중 워치리스트. daily_run 페르소나(oneil/wood)는 일1런 선정분(신선도 가드)∪보유를 권위로 동적
    산출 — 당일 선정·보유가 모두 비거나(첫 런·stale·home 미상) 정적 meta['watchlist'] 로 폴백. 비-daily_run
    (livermore — 일1런 없음)은 항상 정적 curated watchlist."""
    static = list(meta.get("watchlist", []))
    if not meta.get("daily_run"):
        return static
    return _daily_watchlist(home, broker, session=session) or static


def _day_levels_for(personas_map, load_fn=None, session=None):
    """스윙 페르소나(intraday_cfg.pivot_days>0) watchlist 의 sym → 직전 N세션 *일봉 고점*(피벗).
    직전 완결세션까지의 일봉만 사용(point-in-time — 당일 미완결봉 무). 종목별 실패는 그 종목만
    누락(=룰 진입 fail-closed, 보호청산 무관). 스윙 페르소나 없으면 즉시 {} (네트워크 0).
    load_fn/session 주입 → 결정론 테스트. 동일 sym 을 여러 스윙 페르소나가 다른 pivot_days 로
    보면 최대 N 채택(현재 단일 스윙이라 무관 — 다중화 시 페르소나별 분리 필요)."""
    need = {}
    for name, meta in personas_map.items():
        if not meta.get("intraday"):
            continue
        n = int((meta.get("intraday_cfg") or {}).get("pivot_days") or 0)
        if n <= 0:
            continue
        for s in meta.get("watchlist", []):
            need[s] = max(need.get(s, 0), n)
    if not need:
        return {}
    if load_fn is None:
        import data
        load_fn = data.load
    if session is None:
        try:
            from calendar_util import last_completed_session
            session = last_completed_session()
        except Exception as e:
            print(f"[intraday] day_levels 세션 산출 실패 — 스윙 진입 fail-closed: {e!r}", file=sys.stderr)
            return {}
        if session is None:
            return {}
    out = {}
    for s, n in need.items():
        try:
            start = (session - timedelta(days=n * 2 + 15)).isoformat()   # 달력일 여유(주말·휴장 흡수)
            end = (session + timedelta(days=1)).isoformat()              # exclusive → 기준세션봉 포함
            highs = load_fn(s, start, end)["High"].dropna().tail(n)
            if len(highs) > n // 2:              # 윈도우 절반 이하(신규상장·데이터 갭)면 피벗 미확정
                out[s] = float(highs.max())
        except Exception as e:
            print(f"[intraday] day_levels {s} 실패 — 그 종목 진입 스킵: {e!r}", file=sys.stderr)
    return out


def _accum_flags_for(personas_map, load_fn=None, session=None):
    """accum_gate 켠 스윙 페르소나 watchlist 의 sym → 매집 충족 여부(직전 M일 가격 횡보 ∧ OBV 상승).
    직전 완결세션까지 일봉(yfinance=합산 거래량)으로 세션 1회 산출 — point-in-time, 지연 오염 0.
    켠 페르소나 없으면 즉시 {} (네트워크 0 — 기본 off 시 무비용). 산출 실패 심볼은 미포함
    → 룰이 fail-open(진입 허용)으로 처리(품질 필터 결손이 전략을 멈추면 안 됨).
    실증 근거: research/volume_profile_backtest.py S-O1 (2026-07-09)."""
    need = {}
    for name, meta in personas_map.items():
        cfg = meta.get("intraday_cfg") or {}
        if not (meta.get("intraday") and cfg.get("accum_gate")):
            continue
        m = int(cfg.get("accum_days", 15))
        fp = float(cfg.get("flat_pct", 0.10))
        for s in meta.get("watchlist", []):
            need[s] = (max(need[s][0], m), min(need[s][1], fp)) if s in need else (m, fp)
    if not need:
        return {}
    if load_fn is None:
        import data
        load_fn = data.load
    if session is None:
        try:
            from calendar_util import last_completed_session
            session = last_completed_session()
        except Exception as e:
            print(f"[intraday] accum 세션 산출 실패 — 게이트 fail-open: {e!r}", file=sys.stderr)
            return {}
        if session is None:
            return {}
    out = {}
    for s, (m, fp) in need.items():
        try:
            start = (session - timedelta(days=m * 2 + 20)).isoformat()
            end = (session + timedelta(days=1)).isoformat()
            df = load_fn(s, start, end)
            c = df["Close"].dropna().tolist()
            v = df["Volume"].reindex(df["Close"].dropna().index).fillna(0).tolist()
            if len(c) < m + 2:
                continue                                  # 데이터 부족 — 미포함(fail-open)
            rng = (max(c[-m:]) - min(c[-m:])) / c[-1]
            obv = 0.0
            obv_hist = [0.0]
            for i in range(1, len(c)):
                if c[i] > c[i - 1]:
                    obv += v[i]
                elif c[i] < c[i - 1]:
                    obv -= v[i]
                obv_hist.append(obv)
            out[s] = bool(rng < fp and obv_hist[-1] > obv_hist[-1 - m])
        except Exception as e:
            print(f"[intraday] accum {s} 산출 실패 — fail-open: {e!r}", file=sys.stderr)
    return out


def build_traders(quote_fn, personas_map, rules_map, home_fn=persona_home, guard_factory=None,
                  today=None, regime_on=True, daily_session=None, day_levels=None,
                  accum_flags=None):
    """intraday=True 페르소나별 IntradayTrader 구성 — 전부 주입 → 네트워크 없이 테스트.
    각 페르소나 별 home(책 영속·로그 격리). 룰 미존재 페르소나는 skip.
    today(=ET 세션 날짜)는 가드 상태파일의 날짜 키 — 장중 크래시→재시작 시 *당일* 가드(halt·baseline) 복원.
    regime_on=SPY 200MA 레짐 bool — 약세장(False)이면 룰이 진입 BUY 차단(_build_traders 가 산출 주입,
    테스트는 기본 True). daily_session=일1런 신선도 기대세션(=last_completed_session().isoformat());
    None 이면 자동 산출(실패 시 신선도 가드 비활성=레거시 동작). 워치리스트는 daily_run 페르소나면
    당일 일1런 선정분(신선도 가드)∪보유로 동적 산출."""
    from broker.paper import PaperBroker
    today = today or et_session()
    if daily_session is None:                            # 일1런이 저널링한 session 키와 동일 소스로 도출
        try:
            from calendar_util import last_completed_session
            _s = last_completed_session()
            # 세션 산출 실패/불가 → fail-closed 센티넬(어떤 실제 세션과도 불일치) → 당일 픽 0 → daily_run
            # 워치는 보유∪정적만. None(레거시)로 두면 _last_selection_final 이 신선도 무시하고 *전일 픽* 을
            # 로드해 당일 승인 없이 신규진입(look-ahead)하던 구멍 차단(R3감사).
            daily_session = _s.isoformat() if _s else "__stale_guard__"
        except Exception as e:
            print(f"[intraday] 세션 산출 실패 — 신선도 fail-closed(전일 픽 신규진입 차단): {e!r}", file=sys.stderr)
            daily_session = "__stale_guard__"
    traders = []
    for name, meta in personas_map.items():
        if not meta.get("intraday"):
            continue
        rule = rules_map.get(name)
        if rule is None:
            continue
        home = home_fn(name)
        state_file = os.path.join(str(home), "state", f"paper_book_{name}.json")
        log_dir = os.path.join(str(home), "logs")
        # 수수료 = 토스 패리티 0.1% + 명목 $10 이하 무료 — run_live make_broker 와 동일 정책.
        # cash 폴백 2000.0 = 구 $2000 시드 잔재(현 페르소나 4종 전원 cash 키 보유) — cash 키 없이
        # 신규 페르소나가 추가되면 조용히 $2000 로 시딩되는 함정이니 새 페르소나엔 cash 필수 지정.
        broker = PaperBroker(cash=float(meta.get("cash", 2000.0)), price_fn=quote_fn,
                             commission=float(os.environ.get("USTRADE_PAPER_FEE_RATE") or 0.001),
                             free_below=float(os.environ.get("USTRADE_PAPER_FREE_BELOW") or 10.0),
                             state_file=state_file)
        guard_state = os.path.join(str(home), "state", f"intraday_guard_{name}.json")
        guard = guard_factory(meta, state_file=guard_state, today=today) if guard_factory else None
        # 가드가 *오늘자* day_start_equity 를 이미 선복원했으면(guard._load, 이 시점=트레이더 생성
        # 前이라 seed_day_start 미개입) 오늘 이미 활동 이력 있음(크래시→재시작) — flatten_carryover
        # 가 같은 세션 재시작과 전일 이월을 구분하는 유일한 신호(P2-B5, 신규 파일 불요).
        resumed_today = guard is not None and getattr(guard, "day_start_equity", None) is not None
        # daily_run 페르소나(oneil/wood)는 일1런과 책 공유 → run.lock 으로 세션 중 동시쓰기 직렬화.
        book_lock = os.path.join(str(home), "state", "run.lock") if meta.get("daily_run") else None
        watchlist = _watchlist_for(meta, home=home, broker=broker,   # daily_run → 당일 일1런 선정분(신선)∪보유
                                   session=daily_session)
        # 오버나이트 페르소나(persist_state) — 룰상태(트레일 hw) 세션 관통 영속 파일 배선.
        rule_state = (os.path.join(str(home), "state", f"intraday_rules_state_{name}.json")
                      if (meta.get("intraday_cfg") or {}).get("persist_state") else None)
        traders.append(IntradayTrader(name, broker, quote_fn, rule, watchlist,
                                      cfg=meta.get("intraday_cfg", {}), guard=guard, log_dir=log_dir,
                                      book_lock=book_lock, regime_on=regime_on,
                                      day_levels=day_levels, rule_state_file=rule_state,
                                      accum_flags=accum_flags, resumed_today=resumed_today,
                                      # 일1런과 동일 namespace 킬스위치 + <home>/state/HALT — 읽기 전용
                                      # 소비(B6). 일1런 없는 장중전용 페르소나도 배선 = 수동 정지선 확보.
                                      killswitch_file=killswitch_path(home, name)))
    return traders


def _resolve_regime():
    """SPY 200MA 레짐 bool 산출(live_risk.regime_on) — 장중 진입게이트용. 판정불가/실패 시 True
    (fail-open: 일1런이 이미 자기 레짐 반영했고, 데이터 hiccup 으로 전 진입 봉쇄는 과함). 1회 산출."""
    try:
        from live_risk import regime_on as _r
        r = _r()
    except Exception as e:
        print(f"[intraday] 레짐 산출 실패 — 진입허용(fail-open): {e!r}", file=sys.stderr)
        return True
    if r is None:
        print("[intraday] SPY 레짐 판정불가(데이터 부족) — 진입게이트 비활성(fail-open)", file=sys.stderr)
        return True
    if not r:
        print("[intraday] SPY 레짐 OFF(약세장) — 장중 신규/추가 진입 차단(보호청산은 허용)", file=sys.stderr)
    return bool(r)


def _persona_map(only=None):
    """personas.PERSONAS — only(집합) 지정 시 그 이름만. 미지정=전체."""
    import personas
    if not only:
        return personas.PERSONAS
    return {n: m for n, m in personas.PERSONAS.items() if n in only}


def _build_traders(quote_client, only=None):
    """실 배선 — personas.PERSONAS + intraday_rules.RULES + IntradayGuard + SPY 레짐 게이트.
    only 지정 시 해당 페르소나만 배선(개장 별도기동 태스크가 장중전용만 굴리게)."""
    try:
        import intraday_rules
    except Exception as e:
        print(f"[intraday] intraday_rules 로드 실패: {e!r}", file=sys.stderr)
        return []
    try:
        from intraday_guard import IntradayGuard
    except Exception:
        IntradayGuard = None
    pmap = _persona_map(only)
    return build_traders(quote_client.last, pmap, intraday_rules.RULES,
                         guard_factory=IntradayGuard, regime_on=_resolve_regime(),
                         day_levels=_day_levels_for(pmap),   # 스윙 피벗(없으면 {} — 네트워크 0)
                         accum_flags=_accum_flags_for(pmap))  # 매집 게이트(기본 off — {} 무비용)


def _daily_running(home) -> bool:
    """home/state/run.lock 이 살아있는 일1런(run_live)에 잡혀있나. 권위=PID 폴백(락 파일의 PID 가
    살아있나), 빠른경로=최근 mtime(<5분). RunLock 하트비트는 900s 주기라 mtime 분기는 fast-path 일 뿐,
    정합은 PID 폴백이 보장(틱 사이 mtime age 가 300~900s 여도 live PID 로 True). 진행중이면 책 로드 보류."""
    lock = os.path.join(str(home), "state", "run.lock")
    try:
        age = time.time() - os.stat(lock).st_mtime
    except OSError:
        return False                    # 락 부재 = 진행 안 함(단 '완료'인지 '미시작'인지는 호출측이 판별)
    try:
        from broker.guardrail import _pid_alive
        pid = int(Path(lock).read_text(encoding="utf-8").strip() or "0")
        if pid > 0 and _pid_alive(pid):
            return True                 # 권위: 살아있는 보유 프로세스
    except Exception:
        pass
    return age < 300                    # 빠른경로: 최근 갱신(PID 판정 불가 시 보수적으로 진행중 간주)


def _await_daily_runs(personas_map, timeout=1200, poll=10, appear_grace=300,
                      now_fn=None, running_fn=None, sleep_fn=None):
    """oneil/wood 는 일1런과 책을 공유 → 일1런이 *시작해서 끝낼* 때까지 대기 후 로드(last-writer-wins 회피).
    락 부재는 '완료'와 '아직 시작 안 함'을 구분 못 하므로, 락이 한 번 떠서(진행 관측) 사라질 때까지
    기다린다. appear_grace 안에 락이 한 번도 안 뜨면 일1런 미예정으로 보고 진행. timeout 초과 시 경고 후 진행."""
    now_fn = now_fn or time.time
    running_fn = running_fn or _daily_running
    sleep_fn = sleep_fn or time.sleep
    homes = {n: persona_home(n) for n, m in personas_map.items()
             if m.get("intraday") and m.get("daily_run")}
    if not homes:
        return
    seen = set()
    start = now_fn()
    while homes and now_fn() - start < timeout:
        done = []
        for name, home in homes.items():
            if running_fn(home):
                seen.add(name)                       # 일1런 진행 관측
            elif name in seen:
                done.append(name)                    # 떴다 사라짐 = 완료(책 영속 끝)
            elif now_fn() - start > appear_grace:
                done.append(name)                    # grace 내 미관측 = 일1런 미예정 → 진행
        for n in done:
            homes.pop(n, None)
        if not homes:
            return
        sleep_fn(poll)
    if homes:
        print(f"[intraday] 일1런 대기 timeout — 진행(레이스 가능): {list(homes)}", file=sys.stderr)


def persona_lock_path(name, home_fn=persona_home) -> str:
    """장중 루프 프로세스 락 = *책 옆*(persona_home/state/intraday.lock).

    종전 락은 cache_base()(=USTRADE_HOME, **계정 스코프**) + '--only 문자열' 키였다. 결과:
      · SYSTEM 태스크(%LOCALAPPDATA%=systemprofile)와 유저 셸이 서로 *다른 락 파일*을 보고
      · --only 조합만 달라도(스모크 `--only livermore,chartist` vs 등록 태스크의 동적 산출 목록)
        다른 락을 잡아, **같은 책**을 두 프로세스가 PaperBroker last-writer-wins 로 덮어썼다.
    보호 대상이 프로세스가 아니라 *책*이므로 락도 책 경로에서 도출한다. persona_home 은 머신
    env(USTRADE_PERSONA_HOMES) 또는 절대경로 기본값이라 계정·셸·태스크 무관하게 동일 파일이다.
    페르소나 단위라 짝실험(livermore↔livermore_swing, *_ctl) 동시 가동은 그대로 가능.
    일1런 run.lock 과는 별 파일 — run.lock 을 세션 내내 쥐면 run_live 가 굶는다(그건 _on_bar 가
    바 단위로만 잡는다)."""
    return os.path.join(str(home_fn(name)), "state", "intraday.lock")


def _acquire_persona_locks(personas_map, stack, home_fn=persona_home) -> set:
    """intraday 페르소나별 프로세스 락 획득 → 성공한 이름 집합(해제 책임은 stack=ExitStack).
    일부만 점유돼 있으면 나머지는 정상 가동 — 한 페르소나의 중복 기동이 다른 페르소나까지
    멈추지 않게(짝실험 한쪽만 도는 상황 허용)."""
    owned = set()
    for name, meta in sorted(personas_map.items()):
        if not meta.get("intraday"):
            continue
        try:
            stack.enter_context(RunLock(Path(persona_lock_path(name, home_fn)), steal_dead_after=120))
        except (LockBusy, OSError) as e:
            print(f"[intraday] {name} 스킵 — 다른 인스턴스가 책 점유 중: {e!r}", file=sys.stderr)
            continue
        owned.add(name)
    return owned


def main(argv=None):
    import argparse
    from contextlib import ExitStack
    ap = argparse.ArgumentParser(description="장중 액티브 트레이딩 루프 (paper)")
    ap.add_argument("--once", action="store_true", help="1틱만 실행하고 종료(스모크)")
    ap.add_argument("--ignore-hours", action="store_true", help="장중게이트 무시(테스트)")
    ap.add_argument("--only", default="", help="쉼표구분 페르소나만 배선(예: livermore,chartist). 미지정=전체")
    a = ap.parse_args(argv)
    only = {n.strip() for n in a.only.split(",") if n.strip()} or None

    if not a.ignore_hours and not _wait_until_open():
        print("[intraday] 장 마감/주말 — 종료")
        return 0

    from paths import cache_base
    vol_shadow = None                                 # KIS 볼륨 섀도(관찰 전용)
    with ExitStack() as stack:
        # 페르소나(=책)별 프로세스 락. 종전 cache_base+--only 키 락의 이중가동 구멍은 persona_lock_path 참조.
        owned = _acquire_persona_locks(_persona_map(only), stack)
        if not owned:
            print("[intraday] 가동 대상 없음(활성 장중 페르소나 0 또는 전부 타 인스턴스 점유) — 종료")
            return 0
        try:
            from broker.toss_quote import TossQuoteClient
            qc = TossQuoteClient()
            qc.connect()
        except Exception as e:
            print(f"[intraday] Toss 호가 연결 실패 — 종료: {e!r}", file=sys.stderr)
            return 1
        if not a.ignore_hours:                 # 스모크(--ignore-hours)는 즉시 1틱 — 일1런 대기 생략
            _await_daily_runs(_persona_map(owned))  # 일1런(oneil/wood) 책 쓰기 끝낸 뒤 로드(레이스 회피). owned=장중전용이면 no-op
        traders = _build_traders(qc, only=owned)     # 락 잡은 페르소나만 배선(점유당한 책은 손대지 않음)
        if not traders:
            print("[intraday] 활성 장중 페르소나 없음 — 종료")
            return 0
        # 개장 처리 — 전일 이월 포지션 1회 flat(P2-B5). watchlist 밖 이월종목도 broker.get_positions
        # 직접조회라 포함 — 반드시 _sync_watchlist_holdings(샘플루프 내부)보다 먼저 호출. 스모크
        # (--once/--ignore-hours)는 eod_flatten 과 동일 원칙(:1046-1048 부근 주석) — 책 불가촉, 실행 안 함.
        if not a.once and not a.ignore_hours:
            for tr in traders:
                try:
                    tr.flatten_carryover()
                except Exception as e:
                    print(f"[intraday] {tr.name} flatten_carryover 예외: {e!r}", file=sys.stderr)
        # KIS 분당 거래량 섀도 — 관찰 전용(매매 경로 0 접촉). env 키 없으면 휴면. 두 intraday
        # 태스크 동시 가동 시 이중수집 방지로 자체 락(첫 획득자만 수집; KIS 토큰은 디스크 공유라 무해).
        try:
            from broker.kis_quote import KISQuoteClient, VolumeShadow
            _kc = KISQuoteClient.from_env()
            if _kc is None:
                print("[intraday] KIS 볼륨 섀도 휴면(KIS_APP_KEY 미설정)")
            else:
                _kc.connect()
                try:
                    # 계정 스코프(cache_base()) 자체는 별건 이슈 — 여기선 안 건드림. steal_dead_after
                    # 만 위 락들과 통일(관찰전용 락이라 무해 — 죽은 홀더 회수만 빨라짐, 살아있으면 불변).
                    stack.enter_context(RunLock(Path(str(cache_base())) / "volume_shadow.lock",
                                                 steal_dead_after=120))
                except LockBusy:
                    print("[intraday] 볼륨 섀도 — 다른 인스턴스 수집 중(중복 스킵)")
                else:
                    _syms = sorted({s for tr in traders for s in tr.watchlist})
                    vol_shadow = VolumeShadow(_kc, _syms)
                    print(f"[intraday] KIS 볼륨 섀도 가동 — {len(_syms)}종 분당 수집(관찰 전용)")
        except Exception as e:
            print(f"[intraday] KIS 볼륨 섀도 초기화 실패(매매 무관, 휴면): {e!r}", file=sys.stderr)
        session = et_session()
        last_snap = 0.0
        def _safe(fn, tr, what):
            try:
                fn()
            except Exception as e:
                print(f"[intraday] {tr.name} {what} 예외: {e!r}", file=sys.stderr)
        while a.ignore_hours or market_is_open():
            ts = time.time()
            for tr in traders:                # 페르소나 단위 격리 — 한 루프 크래시가 전체를 안 죽임
                _safe(lambda: tr.sample(ts), tr, "sample")
            if vol_shadow is not None:        # 분 경계에서만 실동작 — 예외는 매매와 격리
                try:
                    vol_shadow.tick(ts)
                except Exception as e:
                    print(f"[intraday] 볼륨 섀도 tick 예외(매매 무관): {e!r}", file=sys.stderr)
            if a.once:
                break                         # 아래 마감 스냅샷 1회로 충분 — 루프내 스냅샷과 이중기록 제거
            if ts - last_snap >= 60:          # 분당 스냅샷(대시보드 신선)
                for tr in traders:
                    _safe(lambda: tr.snapshot(session), tr, "snapshot")
                last_snap = ts
            time.sleep(SAMPLE_SECONDS)
        # EOD 청산은 *자연 마감*에서만. 종전엔 --once 를 안 봐서(주석은 본다고 서술), 16:00 을 넘긴
        # 스모크 1틱이 실책을 전량청산했다 — 스모크는 어느 시각에 돌든 책을 건드리면 안 된다.
        if not a.once and not a.ignore_hours and not market_is_open():
            for tr in traders:                # EOD 청산(eod_flatten cfg 페르소나만) — 마감 스냅샷이 flat 책을 찍도록 먼저
                _safe(lambda: tr.eod_flatten(), tr, "eod_flatten")
        for tr in traders:                    # 마감 스냅샷
            _safe(lambda: tr.snapshot(session), tr, "snapshot")
        return 0


if __name__ == "__main__":
    sys.exit(main())
