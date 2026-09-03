"""intraday_rules.py — 페르소나별 장중 대응룰 (순수함수, 결정론).

각 룰 = `rule(bars, pos, cash, ctx) -> list[Signal]`. 부작용 없음(ctx['state'] 트레일링 추적만).
입력은 합성 1분봉 리스트(run_intraday.BarAggregator), 현 포지션(broker.base.Position|None), 현금,
ctx(={'sym','cfg','state'}). 출력 Signal 을 IntradayTrader 가 PaperBroker 로 체결.

⚠️ 거래량 확인 강등: Toss /prices 는 실시간 거래량 미제공 → 돌파의 거래량 동반을 *가격 velocity
   (thrust)* 로 프록시. 정통 CANSLIM 의 볼륨 돌파확인은 유료 실시간 피드 필요. 가격전용이라
   지연신호 오염 0(15분지연 yfinance 볼륨을 결정경로에 안 섞음 — 늦은 진입 방지).

페르소나:
  oneil     — CANSLIM 성장: 피벗 돌파 진입, 7-8% 손절, 20-25% 익절.
  wood      — 파괴성장 모멘텀: MA 회복 진입, 신고가 추가, MA 이탈 트림/손절.
  livermore — 테이프 리더(Jesse Livermore): 오프닝레인지 돌파, 추세 피라미딩, 타이트 트레일손절, 반전청산.
  livermore_swing — 피벗(직전 20세션 고점, ctx day_high) 돌파 진입 + 피라미딩, 넓은 hw 트레일(8%),
              오버나이트 보유(EOD 청산 없음 — 상태는 run_intraday 가 디스크 영속). 보호청산은
              워밍업 게이트 없이 개장 첫 바부터 평가(갭다운 = 당시 스팟 청산, 명목 stop 초과 가능).
  chartist  — 차트분석 가격구조: 추세방향으로 저항 돌파 후 *되돌림(SR Flip)* 에서 캔들확인 진입,
              레벨 손절 + R:R 익절(돌파 순간 추격 아님 — 돌아오는 자리를 노림).
"""
from run_intraday import Signal


# ───────────────────────── 지표 헬퍼 ─────────────────────────
def _ma(vals, n):
    if len(vals) < n or n <= 0:
        return None
    return sum(vals[-n:]) / n


def _thrust(bars, n=3):
    """가격 velocity — 최근 n바 수익률. 양수=상승 추진력(거래량 확인 프록시).
    ⚠️ 윈도우에 갭충전 합성봉(BarAggregator n=0, OHLC=직전 close)이 끼면 가격연속이 깨져, (1) 갭 뒤
    첫 실봉 점프를 추진력으로 *오독*(거짓양성), (2) 평탄화로 정당 돌파를 *억제*(거짓음성)하는 양방향
    오염을 낸다. → 최근 n+1봉이 *전부 실거래봉*일 때만 산출, 합성 포함 시 0(갭 직후 실봉 n개 누적까지
    진입 보류). 평시(샘플 들어오는 정상 바)는 n>0 이라 영향 없음."""
    if len(bars) < n + 1:
        return 0.0
    window = bars[-(n + 1):]
    if any(b.n == 0 for b in window):                        # 갭충전 합성봉 포함 → 추진력 판정 보류
        return 0.0
    a, b = window[0].close, window[-1].close
    return (b - a) / a if a else 0.0


def _thrust_min_eff(bars, cfg, floor_default):
    """유효 thrust 임계 = max(고정 floor, thrust_k × σ3). σ3 = 최근 실거래봉 1봉수익률 표준편차 × √3.

    고정 임계는 종목간 노이즈 격차로 선별력이 불균등(2026-07-08 5d 1분봉 실측: 0.1% 임계 통과율
    AAPL 16% vs MSTR 39% — 고베타일수록 헐거워 whipsaw 비용이 큰 종목에서 오히려 장식화, 감사
    '고베타에서 노이즈 이하' 판정의 실체). k×σ3 는 종목별 노이즈에 적응해 통과율을 균등화
    (k=0.75 에서 ~8-16% 수렴). floor 는 저변동 구간 백스톱으로 잔존.
    thrust_k 미설정(0)이면 기존 고정 floor 동작 그대로 — 테스트·레거시 불변.
    σ 는 실거래봉(n>0)만으로 추정(합성 평탄봉 0 수익률이 σ 를 눌러 임계를 무력화하는 것 차단),
    실봉 15개 미만 웜업 구간은 floor 만 적용(σ 추정 불가)."""
    floor = cfg.get("thrust_min", floor_default)
    k = cfg.get("thrust_k", 0.0)
    if k <= 0:
        return floor
    closes = [b.close for b in bars[-31:] if b.n > 0]
    if len(closes) < 15:
        return floor
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(1, len(closes)) if closes[i - 1]]
    if len(rets) < 14:
        return floor
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / len(rets)
    return max(floor, k * (var ** 0.5) * (3 ** 0.5))


def _opening_range(bars, k):
    """첫 k봉 오프닝레인지(고,저). 합성봉(n=0)은 OHLC 가 직전 close 단일가라 ORB 를 인위적으로
    붕괴(단일가→가짜 돌파)시키므로 *실거래봉만* 으로 산출. 첫 k봉이 전부 합성이면 None(ORB 미확정)."""
    real = [b for b in bars[:k] if b.n > 0]
    if not real:
        return None
    return max(b.high for b in real), min(b.low for b in real)


def _bear_reversal(bars, rev=0.005, look=5):
    """강한 음봉 + 직전 *실거래* 스윙로우 이탈 = 반전 신호.
    ⚠️ 피드 갭 평탄충전 합성봉(BarAggregator n=0, OHLC=직전 close)은 prior_low 를 실제 저점보다
    인위적으로 높게 만들어, 데이터 공백 뒤 첫 소폭 음봉에 가짜 반전청산(protective→즉시 SELL_ALL)을
    유발한다. → prior_low 는 실거래 봉(n>0)만으로 산출. 직전 look 윈도우가 전부 합성(순수 데이터공백)
    이면 스윙로우를 알 수 없어 반전 미발화."""
    last = bars[-1]
    if last.close >= last.open or not last.open:
        return False
    if (last.open - last.close) / last.open < rev:
        return False
    if len(bars) <= look:
        return False
    prior = [b for b in bars[-(look + 1):-1] if b.n > 0]      # 합성봉(n=0) 제외 — 실제 저점만
    if not prior:
        return False                                          # 직전 윈도우 전부 합성 → 판정 불가
    prior_low = min(b.low for b in prior)
    return last.close < prior_low


def _rsi(closes, n=14):
    """단순 RSI(0~100). n+1개 미만이면 None. 상승/하락 평균비. 방식 RSI편 — 과매수(70+)·중심선(50).
    (합성 평탄봉 close 는 변화 0 이라 gain/loss 양쪽에 0 기여 — RS 비율 왜곡 미미, 과매수 게이트엔 무해.)"""
    if len(closes) < n + 1:
        return None
    gain = loss = 0.0
    for i in range(-n, 0):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    if loss == 0:
        return 100.0
    rs = (gain / n) / (loss / n)
    return 100.0 - 100.0 / (1.0 + rs)


def _bull_reversal(bars):
    """되돌림 자리의 상승 반전 캔들 — 해머(긴 아래꼬리=저점 거부) 또는 상승장악형. 실거래봉만(합성 제외).
    방식 6편(캔들): '의미있는 자리에서 나온 패턴만 신뢰' → 되돌림 근접과 AND 로 게이트."""
    last = bars[-1]
    if last.n == 0 or last.high <= last.low:                 # 합성 평탄봉/무범위 → 패턴 아님
        return False
    body = abs(last.close - last.open)
    lower = min(last.open, last.close) - last.low
    upper = last.high - max(last.open, last.close)
    if lower > 0 and lower >= 2 * body and upper <= body:    # 해머(아래꼬리 거부)
        return True
    if len(bars) >= 2:                                       # 상승장악형(직전 음봉을 감싸는 양봉)
        p = bars[-2]
        if (p.n > 0 and p.close < p.open and last.close > last.open
                and last.close >= p.open and last.open <= p.close):
            return True
    return False


def _flat(pos):
    return pos is None or pos.qty <= 1e-9


# ───────────────────────── oneil — CANSLIM ─────────────────────────
def oneil_rule(bars, pos, cash, ctx):
    cfg = ctx["cfg"]
    piv = cfg.get("pivot_bars", 20)
    stop = cfg.get("stop_pct", 0.07)
    target = cfg.get("target_pct", 0.20)
    entry_amt = cfg.get("entry_frac", 0.005) * ctx.get("equity", 100000.0)  # 절대금액→equity 비율(감사)
    tmin = _thrust_min_eff(bars, cfg, 0.0015)            # 적응형 임계(σ3) — thrust_k=0 이면 고정 floor
    regime_on = ctx.get("regime_on", True)               # SPY<200MA(약세) → 신규진입 차단(CANSLIM 'M')
    # 워밍업 게이트는 *진입*(피벗 산출)에만 — 보호청산(손절·익절)은 평단 단일임계라 바 이력 불요.
    # livermore_swing 패턴(P1-B1)으로 재배치: 재시작 직후 bars 부족 상태에서도 보유 보호는 즉시 평가.
    if not bars:
        return []
    c = bars[-1].close
    if _flat(pos):
        ctx["state"].clear()
        if len(bars) < piv + 1:
            return []
        real_piv = [b for b in bars[-(piv + 1):-1] if b.n > 0]   # 합성봉 제외 — 갭충전 단일가가 피벗 왜곡 차단
        pivot = max(b.high for b in real_piv) if real_piv else None
        if regime_on and pivot is not None and c > pivot and _thrust(bars, 3) > tmin and cash >= entry_amt:
            return [Signal("BUY", amount=entry_amt, reason="피벗 돌파")]
        return []
    entry = pos.avg_price
    # 손절은 진입가(평단) 단일임계 — 갭다운으로 임계 아래 한 번에 빠지면 다음 바에서 당시 스팟에 청산되어
    # 명목 stop_pct 를 초과 실현 가능(임계기반 전략 특성, 버그 아님). 장중루프 1분봉이라 갭 노출은 제한적.
    if c <= entry * (1 - stop):
        return [Signal("SELL_ALL", reason=f"{stop:.0%} 손절", protective=True)]
    if c >= entry * (1 + target):
        return [Signal("SELL_ALL", reason=f"{target:.0%} 익절", protective=True)]
    return []


# ───────────────────────── wood — 파괴성장 모멘텀 ─────────────────────────
def wood_rule(bars, pos, cash, ctx):
    cfg = ctx["cfg"]
    man = cfg.get("ma_bars", 20)
    stop = cfg.get("stop_pct", 0.05)
    equity = ctx.get("equity", 100000.0)                 # 절대금액→equity 비율 사이징(감사)
    entry_amt = cfg.get("entry_frac", 0.004) * equity
    add_amt = cfg.get("add_frac", 0.002) * equity
    max_adds = cfg.get("max_adds", 2)
    trim_frac = cfg.get("trim_frac", 0.34)
    tmin = _thrust_min_eff(bars, cfg, 0.001)             # 적응형 임계(σ3) — thrust_k=0 이면 고정 floor
    regime_on = ctx.get("regime_on", True)               # 약세장(SPY<200MA) → 신규/추가 진입 차단
    # 워밍업 게이트는 *진입*에만 — 보유 보호청산(MA손절)은 bars 1개부터 평가(P1-B1, livermore_swing
    # 패턴). MA 자체는 man 바 미만이면 None(자연 데이터부족) — 아래 ma is not None 가드가 무해 통과.
    if not bars:
        return []
    closes = [b.close for b in bars]
    c = closes[-1]
    ma = _ma(closes, man)
    st = ctx["state"]
    if _flat(pos):
        st.clear()
        if len(bars) < man + 1:
            return []
        if regime_on and ma is not None and c > ma and _thrust(bars, 3) > tmin and cash >= entry_amt:
            return [Signal("BUY", amount=entry_amt, reason="MA 회복 모멘텀")]
        return []
    # 손절은 MA*(1-stop) 임계 기반(진입가 아님) — 진입 직후 MA 가 평단 근처면 단일봉 급락이 트림만
    # 유발하고 MA-stop 은 아직 미발화일 수 있음(임계기반 특성). 보호청산은 protective 로 항상 허용.
    if ma is not None:
        if c < ma * (1 - stop):                          # 손절 — MA 아래 깊이 이탈
            st.clear()
            return [Signal("SELL_ALL", reason="MA 이탈 손절", protective=True)]
        if c < ma:                                       # 트림 — MA 가벼운 이탈(이익실현, 비보호=min-hold 적용)
            return [Signal("SELL", qty=pos.qty * trim_frac, reason="MA 이탈 트림")]
    prev_hw = st.get("hw", pos.avg_price)
    thr = _thrust(bars, 3)
    new_high = c > prev_hw
    if thr > tmin:                                       # thrust 정상일 때만 hw 전진 — 합성봉/thrust억제 중
        st["hw"] = max(prev_hw, c)                       #   신고가가 hw 에 '소비'돼 피라미딩 add 가 영구누락되는 것 방지
    adds = st.get("adds", 0)
    if regime_on and adds < max_adds and new_high and thr > tmin and cash >= add_amt:
        # adds 증가는 *체결 시*(IntradayTrader._execute) — 가드 거부된 add 가 슬롯을 유령 소모하지 않게.
        return [Signal("BUY", amount=add_amt, reason=f"강세 추가 #{adds + 1}", pyramid=True)]
    return []


# ───────────────────────── livermore — 테이프 리더 ─────────────────────────
def livermore_rule(bars, pos, cash, ctx):
    cfg = ctx["cfg"]
    orK = cfg.get("opening_range_bars", 15)
    stop = cfg.get("stop_pct", 0.03)
    equity = ctx.get("equity", 100000.0)                 # 절대금액→equity 비율 사이징(감사)
    entry_amt = cfg.get("entry_frac", 0.004) * equity
    add_amt = cfg.get("add_frac", 0.002) * equity
    max_adds = cfg.get("max_adds", 2)
    tmin = _thrust_min_eff(bars, cfg, 0.001)             # 적응형 임계(σ3) — thrust_k=0 이면 고정 floor
    regime_on = ctx.get("regime_on", True)               # 약세장(SPY<200MA) → 신규/추가 진입 차단(추세 일치)
    # 워밍업 게이트는 *진입*(ORB 확정)에만 — 보유 보호청산(트레일·반전)은 bars 1개부터 평가(P1-B1,
    # livermore_swing 과 동일 패턴 — hw 는 prev_hw=avg_price 폴백이라 orK 이력 불요).
    if not bars:
        return []
    c = bars[-1].close
    st = ctx["state"]
    if _flat(pos):
        st.clear()
        if len(bars) < orK + 1:
            return []
        # 오프닝레인지 = 첫 orK 바 고점, ctx 에 *세션 1회 고정*. bars[:orK] 직접 쓰면 MAXBARS 트림으로 240분 후
        # bars[0] 이 앞으로 밀려 '슬라이딩 윈도우'로 변질됨 → 1회 캡처로 차단(ctx['orange'] 는 st.clear() 무관 보존).
        # 앵커 게이트: bars 는 IntradayTrader 인메모리라 장중 크래시/VM 재부팅으로 재시작되면 *재시작 시각*부터
        # 재축적된다. 그러면 첫 orK 바가 개장 레인지가 아니라 재시작 레인지가 되어 orh 가 오산출된다. 엔진이 주입한
        # session_open(09:30 ET) + grace 창 밖에서 첫 바가 시작하면 ORB 를 *미확정*으로 남겨 그날 진입을 스킵한다
        # (오레벨 진입보다 미진입이 안전). 보유 포지션의 트레일/반전 청산은 orh 불요라 아래에서 계속 동작(재시작
        # 중 보호 유지). session_open 미주입(테스트·레거시)이면 게이트 비활성 → 기존 동작 불변.
        orng = ctx.get("orange")
        if orng is None:
            so = ctx.get("session_open")
            if so is None or bars[0].start <= so + cfg.get("or_anchor_grace_sec", 1800):
                orng = _opening_range(bars, orK)              # 실거래봉만 — 합성봉 단일가 붕괴 차단
                if orng is not None:                          # 확정 시에만 캐시(재시작 창밖·전부합성이면 미확정 유지)
                    ctx["orange"] = orng
        # ORB 확정 + 상단 돌파 + thrust + 레짐ON 때만 진입. 미확정(재시작 창밖/전부합성)이면 진입 스킵.
        if orng is not None and regime_on and c > orng[0] and _thrust(bars, 3) > tmin and cash >= entry_amt:
            return [Signal("BUY", amount=entry_amt, reason="ORB 돌파")]
        return []
    prev_hw = st.get("hw", pos.avg_price)
    hw = max(prev_hw, c)
    st["hw"] = hw                                        # 트레일 손절 기준 — 실제 고점 추적(항상 전진)
    if c <= hw * (1 - stop):                             # 트레일 손절(고점 대비)
        return [Signal("SELL_ALL", reason=f"트레일손절 {stop:.0%}", protective=True)]
    if _bear_reversal(bars):                             # 반전 청산
        return [Signal("SELL_ALL", reason="반전청산", protective=True)]
    # 피라미딩 신고가는 별도 add_hw — thrust 억제(합성봉/저thrust) 중 신고가가 hw 에 소비돼 add 가
    # 영구누락되지 않게 thrust 정상일 때만 전진(트레일 hw 와 분리해 트레일 보호는 실고점 유지).
    thr = _thrust(bars, 3)
    prev_ahw = st.get("add_hw", pos.avg_price)
    add_new_high = c > prev_ahw
    if thr > tmin:
        st["add_hw"] = max(prev_ahw, c)
    adds = st.get("adds", 0)
    if regime_on and adds < max_adds and add_new_high and thr > tmin and cash >= add_amt:
        # adds 증가는 *체결 시*(IntradayTrader._execute) — 거부된 add 가 슬롯을 유령 소모하지 않게.
        return [Signal("BUY", amount=add_amt, reason=f"피라미딩 #{adds + 1}", pyramid=True)]
    return []


# ───────────────────────── livermore_swing — 피벗 돌파·오버나이트 ─────────────────────────
def livermore_swing_rule(bars, pos, cash, ctx):
    """리버모어 스윙 — 직전 20세션 고점(피벗, 엔진이 ctx['day_high'] 주입) 돌파 진입 + 피라미딩,
    넓은 hw 트레일(기본 8%) 청산, 오버나이트 보유. livermore(당일 ORB·EOD청산)의 짝 실험.

    오버나이트 계약(personas 4요건의 룰 몫):
      · 보호청산 최우선·워밍업 게이트 없음 — 개장 첫 바부터 트레일 평가. 갭다운으로 임계 아래
        한 번에 빠지면 그 바 스팟에 청산(명목 stop 초과 실현 가능 — oneil 갭 주석과 동일 특성).
      · hw 는 ctx['state'] — run_intraday 가 세션 간 디스크 영속(persist_state). 복원 실패해도
        prev_hw 기본이 avg_price 라 트레일이 평단 기준으로 자가재구축(보호 공백 없음, 폭만 후퇴).
      · day_high 미주입(데이터 실패·신규상장)이면 진입만 fail-closed — 보호청산은 무관.
      · 반전청산(_bear_reversal) 미사용 — 1분봉 장중 반전으로 다일 포지션을 자르지 않는다(의도).
        레짐 OFF 도 신규/추가 차단만 — 강제청산 없음(트레일이 약세 진입 시 자연 청산).

    accum_gate(다이얼): OBV 매집 사전조건 — 직전 M일 가격 횡보+OBV 상승인 종목의
      돌파만 진입(ctx['accum_ok'], 엔진이 일봉에서 세션 1회 산출). 오프라인 실증 통과
      (research/volume_profile_backtest S-O1: +0.775→+0.847R·PF 3.04·MaxDD 절반·전 스윕 양수),
      2026-07-09 사용자 채택으로 livermore_swing 에서 on(짝실험은 복합差 계측으로 전환). ctx 미주입
      (데이터 실패)은 fail-open(진입 허용) — 품질 필터 결손이 전략 자체를 멈추면 안 됨."""
    cfg = ctx["cfg"]
    stop = cfg.get("stop_pct", 0.08)
    buf = cfg.get("breakout_buf", 0.002)
    equity = ctx.get("equity", 100000.0)
    entry_amt = cfg.get("entry_frac", 0.004) * equity
    add_amt = cfg.get("add_frac", 0.002) * equity
    max_adds = cfg.get("max_adds", 2)
    tmin = _thrust_min_eff(bars, cfg, 0.001)
    regime_on = ctx.get("regime_on", True)
    if not bars:
        return []
    c = bars[-1].close
    st = ctx["state"]
    if _flat(pos):
        st.clear()
        day_high = ctx.get("day_high")                   # 직전 20세션 고점 — 없으면 진입 불가(fail-closed)
        if cfg.get("accum_gate") and not ctx.get("accum_ok", True):
            return []                                    # 매집 미충족 종목 — 오늘 진입 스킵(fail-open: 미주입=허용)
        if (regime_on and day_high and c > day_high * (1 + buf)
                and _thrust(bars, 3) > tmin and cash >= entry_amt):
            return [Signal("BUY", amount=entry_amt, reason="20일 피벗 돌파")]
        return []
    # ── 보유(오버나이트 포함): 트레일 최우선 — livermore 와 동일 hw 산식, 폭만 스윙 스케일 ──
    prev_hw = st.get("hw", pos.avg_price)
    hw = max(prev_hw, c)
    st["hw"] = hw                                        # 다일 실고점 — persist_state 로 세션 관통
    if c <= hw * (1 - stop):
        return [Signal("SELL_ALL", reason=f"트레일손절 {stop:.0%}", protective=True)]
    # 피라미딩 — livermore 와 동일 add_hw 메커니즘(thrust 정상일 때만 전진, 슬롯은 체결 시 소모).
    thr = _thrust(bars, 3)
    prev_ahw = st.get("add_hw", pos.avg_price)
    add_new_high = c > prev_ahw
    if thr > tmin:
        st["add_hw"] = max(prev_ahw, c)
    adds = st.get("adds", 0)
    if regime_on and adds < max_adds and add_new_high and thr > tmin and cash >= add_amt:
        return [Signal("BUY", amount=add_amt, reason=f"피라미딩 #{adds + 1}", pyramid=True)]
    return []


# ───────────────────────── chartist — 차트분석 S/R 되돌림 ─────────────────────────
def chartist_rule(bars, pos, cash, ctx):
    """가격구조 트레이더 — 추세방향으로 저항 돌파 후 *되돌림(SR Flip)* 에서 반전캔들 확인 진입,
    레벨 손절 + R:R 익절. 방식 1·2·3·4·6·7·9·10편의 중심축을 상태기계로 이식.

    진입: (추세 c>MA + 레짐ON) 상태에서 저항(직전 N봉 실봉 고점) 종가 돌파 + thrust(거래량 프록시) → *무장*.
          무장 후 가격이 그 레벨(이제 지지)로 되돌아와 근접 + 상승 반전캔들 + RSI 비과매수 → BUY.
    청산: 되돌림 스윙로우 아래 *레벨 손절* / 진입리스크×R:R *익절* / MA 깊은 이탈 *추세이탈 청산*(전부 보호청산).

    ⚠️ 돌파 순간 추격이 아니라 '돌아오는 자리'를 노림(2편 SR Flip) → 되돌림 없는 런어웨이 추세는 *의도적 미진입*.
    ⚠️ 거래량 확인은 _thrust 프록시(모듈 상단 주석 — Toss 실시간 볼륨 부재).
    사이징: 리스크기반(9편·7주차 2% 룰) — 허용손실=equity×risk_per_trade, 명목=equity×rpt×(c/risk),
    단일종목 캡이 천장. 2026-08-28 이전엔 고정 entry_frac + min_risk_frac 거부라 진입 0(112세션 실측)."""
    cfg = ctx["cfg"]
    look = cfg.get("sr_bars", 30)
    ma_bars = cfg.get("ma_bars", 20)
    rsi_bars = cfg.get("rsi_bars", 14)
    rsi_max = cfg.get("rsi_max", 72.0)
    buf = cfg.get("breakout_buf", 0.002)         # 종가가 저항을 이만큼 상향 초과해야 '돌파'
    tol = cfg.get("retest_tol", 0.004)           # 되돌림이 레벨에 이 오차 내 근접
    max_wait = cfg.get("retest_max_bars", 30)    # 돌파 후 되돌림 대기 만료(없으면 무장해제)
    stop_buf = cfg.get("stop_buf", 0.003)        # 되돌림 스윙로우 아래 손절 버퍼
    swing_bars = cfg.get("swing_bars", 3)        # 손절용 스윙로우 윈도우 — 되돌림 즉시 저점만(R3감사: 5는 되돌림前 눌림 저점 포함→손절 과확대, min(_,level) 캡 있어 3로 충분)
    rpt = cfg.get("risk_per_trade", 0.02)        # 허용손실 비율(9편·7주차 2% 룰) — 리스크기반 사이징 목표손실
    max_chase = cfg.get("max_chase", 0.015)      # 되돌림 진입 종가 상한 — 레벨 과도 상회 추격 차단(SR Flip 근접 유지)
    retest_low_tol = cfg.get("retest_low_tol", 0.008)  # 되돌림 저점 하한 — 레벨 크게 아래로 크래시 후 회복은 되돌림 아님(R2감사, risk 폭증 차단)
    rr = cfg.get("rr", 2.0)                       # 손익비 목표(10편)
    ma_exit = cfg.get("ma_exit", 0.02)           # MA 아래 깊이 이탈 시 추세이탈 청산
    tmin = _thrust_min_eff(bars, cfg, 0.0015)    # 적응형 임계(σ3) — thrust_k=0 이면 고정 floor
    pos_cap = cfg.get("max_position_weight", 0.40)   # 단일종목 명목 상한 — 리스크기반 사이징의 천장
    regime_on = ctx.get("regime_on", True)       # 약세장(SPY<200MA) → 신규진입 차단(4·9편, 추세 일치)
    st = ctx["state"]
    # 워밍업 게이트는 *진입*(돌파탐지)에만 — 보유 보호청산(레벨손절·R:R익절·추세이탈)은 bars 1개부터
    # 평가(P1-B1). stop/target 은 진입 시 st 에 확정된 절대값(바 이력 불요), MA 는 ma_bars 미만이면
    # None(자연 데이터부족) — 아래 ma is not None 가드가 그 구간을 무해 통과.
    if not bars:
        return []
    closes = [b.close for b in bars]
    c = closes[-1]
    ma = _ma(closes, ma_bars)

    # ── 보유 중: 레벨 손절 · R:R 익절 · 추세이탈 청산(전부 보호청산 — 가드/min-hold 무관) ──
    if not _flat(pos):
        stop = st.get("stop")
        target = st.get("target")
        if stop is not None and c <= stop:
            return [Signal("SELL_ALL", reason="레벨 손절", protective=True)]
        if target is not None and c >= target:
            return [Signal("SELL_ALL", reason=f"R:R {rr:g} 익절", protective=True)]
        if ma is not None and c < ma * (1 - ma_exit):
            return [Signal("SELL_ALL", reason="추세이탈 청산", protective=True)]
        return []

    # ── 무포지션: 돌파→되돌림 상태기계 ──
    st.pop("stop", None); st.pop("target", None)             # 직전 트레이드 잔재 정리
    if len(bars) < look + 2:
        return []
    armed = st.get("armed")
    if armed is None:                                        # 돌파 탐지
        real = [b for b in bars[-(look + 1):-1] if b.n > 0]  # 합성봉 제외(단일가 왜곡 차단)
        if not real:
            return []
        resistance = max(b.high for b in real)
        uptrend = ma is not None and c > ma
        if (regime_on and uptrend and c > resistance * (1 + buf) and _thrust(bars, 3) > tmin):
            st["armed"] = {"level": resistance, "age": 0}    # 되돌림 대기 진입
        return []

    armed["age"] += 1                                        # 되돌림 대기
    level = armed["level"]
    if armed["age"] > max_wait or c < level * (1 - tol * 2): # 대기 만료 또는 가짜돌파(레벨 하향 재이탈) → 무장해제
        st.pop("armed", None)
        return []
    near = (level * (1 - retest_low_tol) <= bars[-1].low <= level               # 저점이 레벨 실제 터치(레벨 위 잔류=미터치=되돌림 아님) ~ retest_low_tol 하락 밴드(R4)
            and level * (1 - tol) <= c <= level * (1 + max_chase))              # 종가는 레벨 근접(과도 추격 배제)
    if not (regime_on and near and _bull_reversal(bars)):    # 근접(레벨 위아래 밴드)+반전캔들+레짐 동시 성립해야 진입
        return []
    rsi = _rsi(closes, rsi_bars)
    if rsi is not None and rsi >= rsi_max:                   # 과매수 진입 보류(RSI편)
        return []
    swing_low = max(min(b.low for b in bars[-swing_bars:]), level * (1 - retest_low_tol))  # 되돌림 톨러런스 밖 옛 크래시 저점으로 손절 과확대 방지(R4 — 윈도우 크기 see-saw 근본해결)
    stop = min(swing_low, level) * (1 - stop_buf)
    risk = c - stop
    if risk <= 0:                                            # 손절이 진입가 위 = 무의미(방어)
        return []
    # ── 리스크기반 사이징(9편·7주차 2% 룰). SR Flip 은 되돌림 진입이라 risk 가 구조적으로 작다(레벨 ±1%).
    #    구 방식(고정 entry_frac + min_risk_frac 거부)은 그 작은 risk 를 '진입 금지'로 오처리 →
    #    실측 112세션 진입 0(2026-08-28 research/chartist_gate_replay.py). 정통 룰은 작은 risk 를 *증량*으로:
    #    허용손실=equity×rpt, 수량=허용손실/주당risk → 명목=equity×rpt×(c/risk). 단일종목 캡이 천장.
    #    캡 바인딩 시 실손실=캡×(risk/c) 로 rpt 보다 작아진다(예 38%×0.7%≈0.27%<2%) → 캡 사이징이 보수적.
    equity = ctx.get("equity", 100000.0)
    amount = min(equity * rpt * (c / risk), equity * pos_cap * 0.95)   # 캡의 95%(체결 슬리피지 여유 — intraday_guard 사후 비중검사 대비)
    amount = min(amount, cash)                               # 현금 상한
    if amount < cfg.get("min_order_usd", 5.0):               # 무거래 밴드(현금 소진/미소 명목)
        return []
    st.clear()                                               # 무장 해제 + 손절/익절 레벨 확정
    st["stop"] = stop
    st["target"] = c + rr * risk
    return [Signal("BUY", amount=amount, reason="SR Flip 되돌림 진입(리스크기반)")]


def protective_levels(rule_key, st, cfg, avg_price, bars=None):
    """관측 전용 — *이 시점* 유효 보호선(손절/목표가). 각 룰의 청산 산식과 동일식을 여기(룰 파일)에
    상주시켜 대시보드가 로직을 재정의하지 않게 한다. 반환 {"stop":float,"target":float} (해당 없으면 키 생략).
    매매 경로에서 호출되지 않음 — run_intraday.snapshot() 이 저널 기록 시에만 사용."""
    try:
        if not avg_price or avg_price <= 0:
            return {}
        st = st or {}
        cfg = cfg or {}
        if rule_key == "chartist":                       # 진입 시 확정된 절대 레벨(st) — MA 이탈 청산은 동적이라 제외
            out = {}
            if st.get("stop") is not None:
                out["stop"] = round(float(st["stop"]), 4)
            if st.get("target") is not None:
                out["target"] = round(float(st["target"]), 4)
            return out
        if rule_key == "oneil":                          # 평단 단일임계
            sp = cfg.get("stop_pct", 0.07)
            tp = cfg.get("target_pct", 0.20)
            return {"stop": round(avg_price * (1 - sp), 4), "target": round(avg_price * (1 + tp), 4)}
        if rule_key in ("livermore", "livermore_swing"):  # 트레일(실고점 hw 대비) — 스윙은 폭 기본만 다름
            sp = cfg.get("stop_pct", 0.03 if rule_key == "livermore" else 0.08)
            hw = float(st.get("hw", avg_price))
            return {"stop": round(hw * (1 - sp), 4)}
        if rule_key == "wood":                           # MA 임계(진입가 아님) — 바 필요
            sp = cfg.get("stop_pct", 0.05)
            ma = _ma([b.close for b in (bars or [])], cfg.get("ma_bars", 20))
            if ma is None:
                return {}
            return {"stop": round(ma * (1 - sp), 4)}
    except Exception:
        pass
    return {}


RULES = {"oneil": oneil_rule, "wood": wood_rule, "livermore": livermore_rule,
         "livermore_swing": livermore_swing_rule,
         "chartist": chartist_rule,
         # 비큐레이션 대조군 — 룰은 원본과 동일 함수(다이얼도 personas 가 참조 공유), watchlist 만
         # 기계적 규칙. build_traders 가 페르소나 *이름*으로 룰을 찾으므로 별칭 등록.
         "livermore_ctl": livermore_rule, "chartist_ctl": chartist_rule}
