"""intraday_guard.py — 장중 회전·손실 가드레일 (페르소나별, paper).

장중 액티브 매매는 과회전·일중 폭락 노출 위험이 일1런보다 크다. 4겹 차단:
  1. 일중 최대손실 → 당일 페르소나 정지(halt): day_start 대비 손실이 한도 넘으면 신규 진입 중단.
     단 **보호청산(손절·트레일·반전·익절)은 정지 후에도 허용** — 빠져나갈 길은 막지 않는다.
  2. 회전 캡(churn): 일중 총 체결수 한도 — 넘으면 신규 매수 차단(보호청산은 계속 허용).
  3. 단일 비중 캡: 보유가 equity 의 max_position_weight 이상이면 추가매수 거부(집중 폭주 방지).
  4. 최소보유(min-hold): 매수 직후 비보호 매도(트림 등)를 일정시간 차단(즉시 플립 churn 방지).
     보호청산은 min-hold 무관 즉시 허용(손절이 지연되면 안 됨).

상태(체결수·정지·day_start·마지막매수시각)는 state_file 에 *날짜 키*로 영속 → 장중 크래시→스케줄러
재시작(StartWhenAvailable) 시 *당일* 레코드면 복원. 복원 없으면 halt 래치 소실 + baseline 이 당일
중간 저점으로 재설정돼, '일중 1회 정지' 가 '재시작 후 한도' 로 약화된다(가드 불변식 붕괴). state_file
없으면(단위테스트·미배선) 인메모리만. now_fn 주입 → 결정론 테스트.
실거래 봇 GuardConfig 와 별개(장중 paper 전용). 한도는 personas 의 intraday_cfg 권위.
"""
import time

# 전일 마감 equity 를 다음 세션 baseline 으로 승계하는 최대 달력일 간격(B7). 금~월 3일·연휴 4일까지만
# 인정 — 더 오래 안 돈 뒤(장기 정지·머신 오프)의 값을 쓰면 *다일 누적손실*이 하루 한도로 오트립된다.
CARRY_MAX_DAYS = 4


class IntradayGuard:
    def __init__(self, meta: dict, now_fn=time.time, state_file=None, today=None):
        cfg = (meta or {}).get("intraday_cfg", {})
        self.max_trades = int(cfg.get("max_trades_per_day", 20))
        self.max_loss = float(cfg.get("intraday_max_loss", 0.05))      # day_start 대비 손실 한도
        self.max_pos_w = float(cfg.get("max_position_weight", 0.40))
        self.min_hold = float(cfg.get("min_hold_seconds", 120))
        # 총투입 캡(현금바닥) — 오버나이트 페르소나용: 투자합이 equity 의 이 비율 넘는 매수 거부.
        # 기본 1.0 = 비활성(기존 페르소나 동작 불변). 0.70 이면 현금 30% 상시 확보(동결 방지).
        self.max_deploy = float(cfg.get("max_deploy", 1.0))
        self.now_fn = now_fn
        self.state_file = state_file
        self.today = today        # 세션 날짜 키(ET) — 다른 날 레코드는 무시(신규 seed)
        self.trades = 0
        self.halted = False
        self.ks_halted = False    # 외부 킬스위치(일1런 namespace·수동 HALT) 트립 — 비영속(장중루프가 읽기만)
        self.day_start_equity = None
        self.last_equity = None   # 최근 관측 equity(mark_equity) — *다음 세션* baseline 앵커 원천(B7)
        self.carry_equity = None  # 직전 세션 마감 equity(_load 가 날짜 불일치 레코드서 승계) — 갭 가시화
        # 회전 계측(B10) — 보호청산은 회전캡 *면제*(막으면 역효과)라 캡 숫자엔 안 잡힌다. 재량/보호를
        # 나눠 건수·명목을 따로 누적해 하루 비용을 관측만 한다(차단 없음, snapshot.turnover 로 노출).
        self.prot_exits = 0
        self.prot_notional = 0.0
        self.churn_notional = 0.0
        self.last_buy_ts = {}     # sym -> 마지막 매수/트림(비보호) epoch (min-hold 기준)
        self.last_exit_ts = {}    # sym -> 마지막 보호청산 epoch (재진입 쿨다운 기준)
        self._load()              # *오늘* 레코드 있으면 복원(재시작 시 halt·baseline 보존)

    # ── 영속(재시작 내구) ─────────────────────────────────────────────────────
    def _load(self):
        """state_file 의 *오늘* 레코드만 복원 — halt 래치·baseline·회전수·매수시각. 다른 날·손상·미존재는
        무시(신규 seed). day_start_equity 는 당일 첫 기록값을 보존해 재시작이 baseline 을 끌어내리지 않게 함."""
        if not self.state_file:
            return
        import json
        import sys
        from pathlib import Path
        p = Path(self.state_file)
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if self.today is not None and d.get("date") != self.today:
                self.carry_equity = self._carry_from(d)  # 전일 마감 equity 만 승계(갭 baseline, B7)
                return                                  # 다른 날 레코드 — 그 외는 신규 seed
            # 전부 로컬에 파싱 성공 후에야 self 에 일괄 대입(all-or-nothing) — 중간 필드 손상이 halt
            # 래치만 떨궈 baseline·정지가 반쪽 복원되는 정합붕괴 방지(paper._load 와 동일 패턴).
            import math
            dse = d.get("day_start_equity")
            new_dse = float(dse) if dse is not None else None
            if new_dse is not None and not math.isfinite(new_dse):
                raise ValueError("non-finite day_start_equity")   # nan/inf 손상 → 신규 seed(검증 완성)
            new_halted = bool(d.get("halted", False))
            new_trades = int(d.get("trades", 0))
            new_lbt = {str(k): float(v) for k, v in (d.get("last_buy_ts") or {}).items()}
            new_let = {str(k): float(v) for k, v in (d.get("last_exit_ts") or {}).items()}
            le = d.get("last_equity")
            new_le = float(le) if le is not None else None
            if new_le is not None and not math.isfinite(new_le):
                raise ValueError("non-finite last_equity")
            new_pe, new_pn = int(d.get("prot_exits", 0)), float(d.get("prot_notional", 0.0))
            new_cn = float(d.get("churn_notional", 0.0))
            self.day_start_equity, self.halted, self.trades, self.last_buy_ts, self.last_exit_ts = \
                new_dse, new_halted, new_trades, new_lbt, new_let
            self.last_equity = new_le
            self.prot_exits, self.prot_notional, self.churn_notional = new_pe, new_pn, new_cn
        except Exception as e:
            print(f"[intraday_guard] 상태 로드 실패(신규 seed) {p}: {e!r}", file=sys.stderr)

    def _carry_from(self, d: dict):
        """직전 세션 레코드에서 *마감 equity* 만 승계(B7) — 오버나이트 보유(livermore_swing·oneil·wood)의
        갭 손실이 개장 equity seed 로 baseline 에서 통째로 증발하던 것 차단. 날짜 간격 CARRY_MAX_DAYS
        초과·비유한·비양수·파싱실패는 None → 호출측이 현행(개장 equity) fallback."""
        import math
        from datetime import date
        try:
            eq = float(d.get("last_equity"))
            gap = (date.fromisoformat(str(self.today)) - date.fromisoformat(str(d.get("date")))).days
        except (TypeError, ValueError):
            return None
        if not math.isfinite(eq) or eq <= 0:
            return None
        return eq if 0 < gap <= CARRY_MAX_DAYS else None

    def _save(self):
        """상태전이(seed·halt 래치·체결) 시에만 원자적·내구 저장(paper._save 패턴). state_file 없으면 no-op."""
        if not self.state_file:
            return
        import json
        import os
        import sys
        from pathlib import Path
        p = Path(self.state_file)
        tmp = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            data = {"date": self.today, "day_start_equity": self.day_start_equity,
                    "halted": self.halted, "trades": self.trades,
                    "last_equity": self.last_equity,          # 다음 세션 baseline 앵커(B7)
                    "prot_exits": self.prot_exits, "prot_notional": self.prot_notional,
                    "churn_notional": self.churn_notional,    # 회전 계측(B10, 관측 전용)
                    "last_buy_ts": self.last_buy_ts, "last_exit_ts": self.last_exit_ts}
            tmp = f"{p}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            from paths import atomic_replace
            if not atomic_replace(tmp, str(p)):
                print(f"[intraday_guard] 상태 교체 실패(다음 재시작 stale) {p}", file=sys.stderr)
        except Exception as e:
            if tmp:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            print(f"[intraday_guard] 상태 저장 실패 {p}: {e!r}", file=sys.stderr)

    def seed_day_start(self, equity):
        """일중손실 baseline seed(루프 기동 시 1회). 미설정일 때만(복원된 baseline 보존).
        지연캡처(첫 시그널 시점)는 보유 페르소나의 개장~첫시그널 하락을 baseline 에서 누락시킨다.

        baseline 우선순위 = *전일 마감 equity*(carry_equity) > 개장 equity(B7). 개장 equity 로 seed 하면
        오버나이트 갭 손실이 가드 밖으로 빠진다 — 스윙(livermore_swing)·일1런 보유(oneil/wood)가 그 대상.
        eod_flatten 페르소나는 마감이 flat 이라 두 값이 사실상 같아 동작 불변(청산 누락 시에만 갭 포착)
        → 페르소나 분기 없이 일관 적용."""
        base = self.carry_equity or equity
        if self.day_start_equity is None and base and base > 0:
            self.day_start_equity = float(base)
            if equity and equity > 0:
                self.last_equity = float(equity)
            self._save()

    def mark_equity(self, equity):
        """현재 equity 관측 기록(스냅샷 주기 호출) — *다음 세션* baseline 앵커 원천(B7). 매매 판단 무관.
        값이 변할 때만 영속(분당 최대 1회 — 이미 분당 스냅샷 append 가 도는 경로라 증분 I/O 무시가능)."""
        if not equity or equity <= 0:
            return
        eq = float(equity)
        if self.last_equity is not None and abs(self.last_equity - eq) < 1e-9:
            return
        self.last_equity = eq
        self._save()

    def turnover_summary(self) -> dict:
        """하루 회전 요약(계측 전용 — 어떤 차단에도 안 쓰임, B10). 보호청산은 회전캡 면제라(캡에 걸려
        손절이 막히면 역효과) 캡 숫자만으론 비용이 안 보인다: BUY 20회/일 상한이면 명목 4×equity 회전,
        왕복 0.2% 가정 시 최대 0.8%/일. 실제로 얼마를 태웠는지는 이 명목 누적으로만 관측된다."""
        return {"trades": self.trades, "prot_exits": self.prot_exits,
                "prot_notional": round(self.prot_notional, 2),
                "churn_notional": round(self.churn_notional, 2)}

    @staticmethod
    def _is_protective(sig) -> bool:
        """보호청산(손절·트레일·반전·익절) = 정지·min-hold 무관 항상 허용. 권위는 Signal.protective
        명시 플래그 하나뿐 — intraday_rules 의 보호청산 5종이 전부 이 플래그를 세우므로 종전
        reason 키워드 폴백("손절/트레일/반전/익절" 포함 여부)은 도달 불가 죽은 코드였고, 반대로
        비보호 트림의 reason 에 그 단어가 섞이면 min-hold·회전캡을 조용히 우회하는 통로였다."""
        return sig.action in ("SELL", "SELL_ALL") and bool(getattr(sig, "protective", False))

    def allow(self, name, sym, sig, pos, acct, last_px=None) -> bool:
        eq = acct.equity if acct is not None else None
        if self.day_start_equity is None and eq:
            self.seed_day_start(eq)                        # baseline 첫 기록(전일마감 앵커 우선) → 재시작 내구

        # 1) 일중 최대손실 → 정지. 정지 후엔 보호청산만 허용(탈출 보장).
        if not self.halted and eq and self.day_start_equity \
                and eq <= self.day_start_equity * (1 - self.max_loss):
            self.halted = True
            self._save()                                   # 정지 래치 → 재시작에도 정지 유지
        # 1-b) 외부 킬스위치(일1런 killswitch.paper_<persona> · 수동 HALT) — 장중루프는 *읽기만* 하고
        #      여기서 소비. 일1런의 "halted → orders 0" 과 달리 보호청산은 계속 허용(장중은 보유가
        #      살아있고, 정지 중 트레일·손절까지 막으면 무방비 노출이 더 위험). 래치는 파일이 권위라
        #      영속 안 함 — 사람이 reset 하면 다음 폴에서 자동 해제.
        if self.halted or self.ks_halted:
            return self._is_protective(sig)

        if sig.action == "BUY":
            if eq is None or eq <= 0:                           # 계좌조회 실패/전손(eq=0) → 한도 검증 불가
                return False                                   #    → 신규매수 거부(fail-closed). eq=0 falsy 단락 캡우회 차단.
            if self.trades >= self.max_trades:                 # 2) 회전 캡
                return False
            # 5) 재진입 쿨다운 — 보호청산(손절·트레일·반전) 직후 같은 종목 즉시 재매수 차단(whipsaw 루프
            #    방지). watchlist 가 청산종목을 세션내내 보유(제거 안 함)해 _flat→진입분기 재무장되므로
            #    가드에서 막음. min_hold 재사용(별 knob 불요). 보호청산·타 종목·쿨다운 경과 후는 무관.
            le = self.last_exit_ts.get(sym)
            if le is not None and (self.now_fn() - le) < self.min_hold:
                return False
            # 3) 단일 비중 캡 — *체결 後* 예상 비중으로 검사(진입·추가매수 공통). pre-trade 보유만 보면
            #    (a) flat 진입은 보유 0 이라 캡이 한 번도 평가 안 되고(무제한 사이징), (b) add 는 캡을
            #    막 넘기는 그 주문이 통과(1주문 오버슈트). order_amt(sig.amount,$)를 더한 (보유$+주문$)/eq
            #    가 캡 초과면 거부 — 시가(last_px) 우선, 없으면 평단.
            px = last_px if (last_px and last_px > 0) else (pos.avg_price if pos is not None else None)
            held_val = pos.qty * px if (pos is not None and px and px > 0) else 0.0
            order_val = float(getattr(sig, "amount", 0.0) or 0.0)
            if eq and (held_val + order_val) > self.max_pos_w * eq:
                return False
            # 6) 총투입 캡(현금바닥, max_deploy<1 만) — (전 종목 투자합 + 주문)/equity 초과 거부.
            #    오버나이트 페르소나가 풀투자 후 현금기아로 동결되던 것의 앞단 차단. 투자합 = eq - cash.
            if self.max_deploy < 1.0:
                cash = getattr(acct, "cash", None)
                if cash is None:
                    return False                               # 현금 미상 → 검증 불가 = 거부(fail-closed)
                invested = max(0.0, eq - float(cash))
                if invested + order_val > self.max_deploy * eq:
                    return False
            return True

        # SELL / SELL_ALL — 보호청산(손절·트레일·반전·익절)은 회전캡·min-hold 무관 항상 허용(탈출 보장).
        # 비보호(트림)는 진입과 *동일* 회전캡 + min-hold 적용 — wood MA직하 정체구간서 트림이 매 바
        # 무한 재발화해 한 포지션 청산에 수십 체결을 찍어(거래수·수수료·orders 부풀림→selection_review
        # 비교 오염) churn 하던 것 차단(트림도 turnover 다).
        if not self._is_protective(sig):                        # 2)+4) 비보호 트림: 회전캡 + 최소보유
            if self.trades >= self.max_trades:                 # 회전 캡(트림도 과회전 차단)
                return False
            lb = self.last_buy_ts.get(sym)
            if lb is not None and (self.now_fn() - lb) < self.min_hold:
                return False
        return True

    def note_fill(self, sym, sig, ts=None, flattened=False, notional=None):
        """체결 성사 후 호출 — 회전수 카운트 + 동작 시각 기록(min-hold/쿨다운 기준). 재시작 내구 저장.

        회전캡(self.trades)은 *재량* 매매(매수·트림)만 카운트 — 보호청산(손절·트레일·반전·익절)은
        강제 리스크관리이지 churn 이 아니라 제외(손절 다수가 회전 예산을 잠식해 정당 재진입을 막던 것
        차단). 매수·비보호트림은 last_buy_ts(min-hold 기준) 갱신, 보호청산은 last_exit_ts(재진입 쿨다운) 갱신.
        flattened=True(이 매도가 포지션을 전량 소진) 면 비보호 트림이어도 last_exit_ts 를 올려 재진입
        쿨다운을 건다 — 트림→dust 전량청산 직후 다음 바 즉시 재매수(whipsaw)를 차단(회전캡 카운트는 유지).
        notional($ 체결명목, 선택)은 회전 계측(B10)에만 쓰인다 — 재량/보호를 갈라 누적, 차단엔 미사용."""
        t = ts if ts is not None else self.now_fn()
        protective = self._is_protective(sig)
        amt = abs(float(notional or 0.0))
        if not protective:                                         # 매수·비보호트림 → 회전 +1, min-hold 시각
            self.trades += 1
            self.last_buy_ts[sym] = t
            self.churn_notional += amt
        else:                                                      # 보호청산 — 캡 면제, 비용만 계측(B10)
            self.prot_exits += 1
            self.prot_notional += amt
        if protective or flattened:                                # 보호청산 or 포지션 flat → 재진입 쿨다운 시각
            self.last_exit_ts[sym] = t
        self._save()
