"""모의매매 페르소나 — 전략 프리셋 (paper 전용, 실거래 무관).

각 페르소나 = 전략 + 유니버스 + 시드자본 + RunConfig override. `run_live --persona <name>`.
서로 다른 투자철학으로 같은 자본($100000)·기간을 돌려 selection_review 가 어느 전략이 돈 됐나 비교.

전략 엔진(일1런 선정):
  buffett — live_select_buffett (정통 가치·우량 스크린, FMP). 주1회 재선정(reselect_days) —
            가치 철학상 일일 재선정은 churn 누수(연 4~7%)만 낳음. 비재선정일은 가드+레짐 감시만.
  buffett_v2 — live_select_buffett_v2. buffett 의 A/B 실험군(12주): 하드컷을 랭킹 페널티로,
            품질축을 ROIC 중심으로, 가치축을 FCF·이익수익률로, z 를 섹터중립으로 바꾼 판.
            선정 외 다이얼은 buffett 과 동일값 — 성과差가 선정 로직에서만 나오게 통제.
  wood    — live_select_wood (파괴성장 프록시: 고P/S·저배당·모멘텀, FMP)
  oneil   — canslim (A엔진 CANSLIM 성장 — 봇 실거래 전략, paper 로도 검증)

장중 액티브(run_intraday.py — 실시간 호가로 장중 진입·청산·트레일링):
  intraday=True 인 페르소나만 장중 루프가 굴림. intraday_cfg = 룰 한도(intraday_rules·intraday_guard 권위).
  watchlist = 장중 감시 종목(없으면 일1런 선정분). livermore·chartist 는 일1런 없이 장중 전용.
  chartist — 차트분석 가격구조(S/R 존·SR Flip 되돌림·추세선·거래량·캔들). 순수 기술적, 펀더 스크린 무.
  thrust_k — 적응형 thrust 임계(유효임계=max(thrust_min, k×σ3), intraday_rules._thrust_min_eff).
             고정임계는 종목간 통과율 불균등(고베타 헐거움) — 2026-07-08 감사 후속.

대조군(_ctl) — 큐레이션 편향 분리 실험:
  livermore·chartist 정적 16종은 2023-25 승자 후시안 큐레이션 → 룰 엣지와 종목선택 효과가 분리 불가
  (전략감사). _ctl 페르소나는 룰·다이얼을 원본과 *동일 dict 참조*로 공유(드리프트 구조 차단)하고
  watchlist 만 기계적 규칙(sp500 20거래일 평균 달러거래대금 상위 16, 판단 0)으로 대체.
  성과差(원본−대조군) ≈ 큐레이션 효과. 갱신은 분기 1회 tools/refresh_sp500.py 때 함께(수동).

스윙 변형(livermore_swing) — EOD 청산 구조비용 분리 실험:
  livermore(장중전용·EOD청산)와 동일 watchlist 로 오버나이트 보유형을 병행 — 진입은 직전 20세션
  고점 돌파(피벗, 엔진이 day_high 주입), 청산은 hw 트레일(8%, ctx state 디스크 영속으로 세션 관통).
  EOD 청산의 두 구조비용(러너 절단 + 오버나이트 드리프트 포기)이 성과差로 계측됨.
  오버나이트 전제 4요건: ①현금바닥(max_deploy, IntradayGuard) ②룰상태 영속(persist_state →
  run_intraday 가 intraday_rules_state_*.json 저장/복원) ③갭 손절(보호청산이 워밍업 게이트 없이
  개장 첫 바부터 평가 — 갭다운은 당시 스팟 청산, 명목 stop 초과 실현 가능) ④당일 스코프 없는 진입.
"""

# 장중 다이얼 — 원본·대조군이 같은 dict 를 참조(대조군 실험의 전제: 다이얼 튜닝이 자동 동기화).
# 룰(intraday_rules)·가드(intraday_guard)·트레이더(run_intraday)는 cfg 를 읽기만 한다(쓰기 금지).
_LIVERMORE_CFG = {"opening_range_bars": 15, "stop_pct": 0.03, "entry_frac": 0.20,
                  # add_frac 0.08(0.10 아님) — intraday_guard 가 *체결후* 비중 검사라(entry 0.20+
                  # add×2 0.10×2=0.40 이 캡 0.40 과 동일하면 상승분만큼 add#2 가 상시 캡초과 거부됨,
                  # 2026-08 감사) 0.20+0.08×2=0.36<0.40 로 여유 확보(상승 ~11%까지 add 통과).
                  "add_frac": 0.08, "max_adds": 2, "thrust_min": 0.001, "thrust_k": 0.75,
                  "max_trades_per_day": 20, "intraday_max_loss": 0.05,
                  "max_position_weight": 0.40, "min_hold_seconds": 90,
                  # EOD 전량청산 — 오버나이트 보유가 현금기아×트레일 세션리셋과 겹쳐
                  # 책 동결(06-26 실증). 매 세션 풀현금 ORB 재가동(장중 전용 정체성).
                  "eod_flatten": True}

_CHARTIST_CFG = {"sr_bars": 30, "ma_bars": 20, "rsi_bars": 14, "rsi_max": 72.0,
                 "breakout_buf": 0.002, "retest_tol": 0.004, "retest_max_bars": 30,
                 "stop_buf": 0.003, "swing_bars": 3, "max_chase": 0.015,
                 # 2026-08-28: min_risk_frac 거부게이트 폐기 — 112세션 진입 0 유발(risk 상한<하한 기하 모순,
                 # research/chartist_gate_replay.py). 리스크기반 사이징으로 전환(9편·7주차 2% 룰). rule 은
                 # 이제 risk_per_trade 로 명목을 정하고 max_position_weight 를 천장으로 쓴다. 아래 두 키
                 # (min_risk_frac·entry_frac)는 rule 이 더 안 읽음(inert, 회귀 대조용 잔존).
                 "risk_per_trade": 0.02, "min_risk_frac": 0.012,
                 "retest_low_tol": 0.008, "rr": 2.0, "ma_exit": 0.02,
                 "thrust_min": 0.0015, "thrust_k": 0.75,
                 "entry_frac": 0.20, "max_trades_per_day": 10, "intraday_max_loss": 0.05,
                 "max_position_weight": 0.40, "min_hold_seconds": 180,
                 # EOD 전량청산 — livermore 와 동일 구조(장중전용·일1런 없음, book_lock=None) →
                 # 동일 동결 리스크. 매 세션 풀현금 재가동.
                 "eod_flatten": True}

# 스윙 변형 다이얼 — livermore 와 별도 dict(실험축이 다름: 보유기간. _ctl 참조공유와 달리 독립 튜닝).
_LIVERMORE_SWING_CFG = {"pivot_days": 20, "breakout_buf": 0.002, "stop_pct": 0.08,
                        # add_frac 0.08 — livermore 와 동일 산술 정합(0.20+0.08×2=0.36<max_position_weight
                        # 0.40, 체결후 검사 가드에서 add#2 가 상시거부되지 않게 여유 확보, 2026-08 감사).
                        "entry_frac": 0.20, "add_frac": 0.08, "max_adds": 2,
                        "thrust_min": 0.001, "thrust_k": 0.75,
                        "max_trades_per_day": 8, "intraday_max_loss": 0.05,
                        "max_position_weight": 0.40, "min_hold_seconds": 300,
                        # 오버나이트 전제 — 현금바닥 30%(총투입 70% 캡, 동결 방지) + 룰상태(hw 트레일
                        # 앵커) 디스크 영속(세션리셋이 다일 실고점을 망각하지 않게). eod_flatten 없음.
                        "max_deploy": 0.70, "persist_state": True,
                        # OBV 매집 사전조건 — 오프라인 실증 통과(volume_profile_backtest S-O1:
                        # 기대 +0.775→+0.847R·PF 3.04·MaxDD 절반·9포인트 스윕 전부 양수).
                        # 2026-07-09 사용자 판단으로 on(첫 세션 전 = 책 히스토리 오염 0). 이로써
                        # livermore↔swing 성과差는 단일변인이 아니라 [EOD청산 구조비용 + 매집게이트]
                        # 복합差를 계측함 — 해석 시 유의. 엔진이 일봉(완결세션, 합산 거래량)에서
                        # accum_ok(15일 횡보<10% ∧ OBV 상승)를 세션 1회 산출·주입, 미산출=fail-open.
                        "accum_gate": True, "accum_days": 15, "flat_pct": 0.10}

# 대조군 watchlist — sp500 20거래일 평균 달러거래대금(ADV) 상위 16 (2026-07-08 산출, 기계적).
# GOOG·GOOGL 듀얼클래스 동시 포함도 규칙 그대로 둠(수동 개입 0 이 대조군의 정체성).
_ADV16 = ["AAPL", "AMAT", "AMD", "AMZN", "AVGO", "GOOG", "GOOGL", "INTC",
          "META", "MRVL", "MSFT", "MU", "NVDA", "SNDK", "TSLA", "WDC"]

PERSONAS = {
    "buffett": {
        "label": "워런버핏형 가치·우량",
        "strategy": "buffett",
        "universe": "sp500",
        "cash": 100000.0,
        "overrides": {"top_n": 5, "pool": 20, "lookback": 252, "max_pe": 25.0,
                      "min_margin": 0.08, "vol_target": 0.12, "regime_ma": 200, "fractional": True,
                      "value_trap_gate": True,   # Piotroski reliable&F<5 veto(희석·부채급증·발생액 포함)
                      "reselect_days": 7},       # 주1회 재선정 — 일일 churn 연 4~7% 누수 절감(전략감사)
        # 가치·장기보유 철학 — 장중매매 비적용(intraday 미설정).
    },
    # ── buffett A/B 실험군 (12주, 2026-08-04 개시) — v1 과 **선정 로직만** 다르다 ──────────
    # 아래 오버라이드는 buffett 과 의도적으로 동일값(top_n·pool·lookback·vol_target·regime_ma·
    # reselect_days·fractional·value_trap_gate·universe·cash). 다른 건 max_pe/min_margin 뿐인데,
    # 이건 '컷 완화'가 아니라 *컷을 페널티로 옮긴 것*이다 — 감점은 quality_value_score_v2 안에 있다.
    # 판정 전까지 이 dict 와 buffett dict 를 함께 바꾸지 말 것(한쪽만 바뀌면 12주가 무효).
    "buffett_v2": {
        "label": "버핏 v2(ROIC·섹터중립)",
        "strategy": "buffett_v2",
        "universe": "sp500",
        "cash": 100000.0,
        "overrides": {"top_n": 5, "pool": 20, "lookback": 252,
                      "max_pe": 60.0,      # 극단 PE 만 배제(v1 25 → 페널티로 이전)
                      "min_margin": 0.0,   # 적자만 배제(v1 8% → 페널티로 이전)
                      "vol_target": 0.12, "regime_ma": 200, "fractional": True,
                      "value_trap_gate": True,
                      "reselect_days": 7},
        # 가치·장기보유 철학 — 장중매매 비적용(v1 과 동일).
    },
    "wood": {
        "label": "캐시우드형 파괴성장",
        "strategy": "wood",
        "universe": "growth",
        "cash": 100000.0,
        "overrides": {"top_n": 7, "pool": 18, "lookback": 63, "vol_target": 0.30,
                      "regime_ma": 200, "fractional": True},
        "intraday": True,
        "daily_run": True,            # 일1런 + 장중루프 공유책 → 장중루프가 일1런 완료 후 로드(레이스 회피)
        "watchlist": ["TSLA", "NVDA", "AMD", "PLTR", "SHOP", "COIN", "CRWD", "NET", "RBLX", "XYZ"],
        "intraday_cfg": {"ma_bars": 20, "stop_pct": 0.05, "entry_frac": 0.20,
                         # add_frac 0.08 — livermore 와 동일 결함 검산 확인(0.20+0.10×2=0.40==cap 이라
                         # 체결후검사서 add 상시거부) 후 산술 정합: 0.20+0.08×2=0.36<0.40(캡의 90%, 2026-08 감사).
                         "add_frac": 0.08, "max_adds": 2, "trim_frac": 0.34,
                         "thrust_min": 0.001, "thrust_k": 0.75,
                         # 일중손실캡 6%→5% — 타 페르소나와 정렬(전략감사 보류분 채택)
                         "max_trades_per_day": 16, "intraday_max_loss": 0.05,
                         "max_position_weight": 0.40, "min_hold_seconds": 120},
    },
    "oneil": {
        "label": "오닐 CANSLIM 성장",
        "strategy": "canslim",
        "universe": "sp500",
        "cash": 100000.0,
        "overrides": {"top_n": 5, "pool": 25, "vol_target": 0.20, "regime_ma": 200, "fractional": True,
                      "value_trap_gate": True},   # 기구현 다이얼 활성화 — Piotroski value trap 제외
        "intraday": True,
        "daily_run": True,            # 일1런 + 장중루프 공유책 → 장중루프가 일1런 완료 후 로드(레이스 회피)
        "watchlist": ["NVDA", "AAPL", "MSFT", "AMD", "AVGO", "META", "GOOGL", "AMZN", "CRM", "NFLX"],
        "intraday_cfg": {"pivot_bars": 20, "stop_pct": 0.07, "target_pct": 0.20,
                         "entry_frac": 0.25, "thrust_min": 0.0015, "thrust_k": 0.75,
                         "max_trades_per_day": 12, "intraday_max_loss": 0.05,
                         "max_position_weight": 0.40, "min_hold_seconds": 180},
    },
    # ── canslim A/B 실험군 (12주, 2026-08-14 개시) — oneil 과 **선정 로직만** 다르다 ──────────
    # oneil(canslim)과 동일 다이얼(top_n·pool·vol_target·regime_ma·fractional·value_trap_gate·
    # universe·cash). 다른 건 strategy 하나뿐 — canslim_rdcf 는 canslim 위에 역DCF 소프트 틸트를
    # 얹는다(완벽하게 가격된 종목 감점). 성과差가 밸류틸트에서만 나오게 통제.
    # ⚠️ 판정 전까지 이 dict 와 oneil dict 를 함께 바꾸지 말 것(한쪽만 바뀌면 12주가 무효).
    # 장중 루프는 붙이지 않는다(oneil 과 달리 intraday 미설정) — 밸류틸트 효과를 일1런 선정에서만 계측.
    "canslim_rdcf": {
        "label": "오닐 CANSLIM + 역DCF 밸류틸트",
        "strategy": "canslim_rdcf",
        "universe": "sp500",
        "cash": 100000.0,
        "overrides": {"top_n": 5, "pool": 25, "vol_target": 0.20, "regime_ma": 200, "fractional": True,
                      "value_trap_gate": True},
    },
    "livermore": {
        "label": "리버모어형 트레이더",
        "strategy": "livermore",          # 일1런 엔진 없음 — 장중 전용(run_live 미등록)
        "universe": "tech",               # 장중루프는 watchlist 사용 — universe 는 미사용이나 실재 키로(가짜 'movers' 폴백 제거)
        "cash": 100000.0,
        "intraday": True,
        "watchlist": ["NVDA", "TSLA", "AMD", "META", "AMZN", "AAPL", "MSFT", "GOOGL", "NFLX", "AVGO",
                      "PLTR", "SMCI", "COIN", "MARA", "MSTR", "ARM"],
        "intraday_cfg": _LIVERMORE_CFG,
    },
    "chartist": {
        "label": "차트분석 S/R 되돌림",
        "strategy": "chartist",           # 일1런 엔진 없음 — 장중 전용(run_live 미등록, livermore 형)
        "universe": "tech",               # 장중루프는 watchlist 사용 — universe 는 실재 키(폴백 안전)
        "cash": 100000.0,
        "intraday": True,
        # 유동성 큰 대형주 — 깨끗한 S/R 존·되돌림 구조. 형제 페르소나와 겹쳐도 책 격리라 무관.
        "watchlist": ["NVDA", "AMD", "TSLA", "AAPL", "MSFT", "META", "AMZN", "PLTR", "AVGO", "GOOGL",
                      "SMCI", "ARM", "COIN", "MU", "MRVL", "ORCL"],
        "intraday_cfg": _CHARTIST_CFG,
    },
    "livermore_swing": {
        "label": "리버모어 스윙(오버나이트)",
        "strategy": "livermore_swing",    # 일1런 엔진 없음 — 장중 진입·다일 보유(run_live 미등록)
        "universe": "tech",               # 장중루프는 watchlist 사용 — 실재 키(폴백 안전)
        "cash": 100000.0,
        "intraday": True,
        # livermore 와 동일 16종(복사본) — 보유기간 축만 다른 짝 실험(watchlist 변인 통제).
        "watchlist": ["NVDA", "TSLA", "AMD", "META", "AMZN", "AAPL", "MSFT", "GOOGL", "NFLX", "AVGO",
                      "PLTR", "SMCI", "COIN", "MARA", "MSTR", "ARM"],
        "intraday_cfg": _LIVERMORE_SWING_CFG,
    },
    # ── 비큐레이션 대조군 — 룰·다이얼 원본과 동일(참조 공유), watchlist 만 기계적 ADV16 ──
    "livermore_ctl": {
        "label": "리버모어 대조군(ADV16)",
        "strategy": "livermore",
        "universe": "sp500",              # watchlist 원천 유니버스(문서용) — 장중루프는 watchlist 사용
        "cash": 100000.0,
        "intraday": True,
        "watchlist": list(_ADV16),
        "intraday_cfg": _LIVERMORE_CFG,   # 원본과 같은 객체 — 다이얼 변경 자동 동기(실험 유효성)
    },
    "chartist_ctl": {
        "label": "차트분석 대조군(ADV16)",
        "strategy": "chartist",
        "universe": "sp500",
        "cash": 100000.0,
        "intraday": True,
        "watchlist": list(_ADV16),
        "intraday_cfg": _CHARTIST_CFG,    # 원본과 같은 객체 — 다이얼 변경 자동 동기(실험 유효성)
    },
}


def get(name: str) -> dict:
    p = PERSONAS.get(name)
    if p is None:
        raise KeyError(f"미지의 페르소나: {name} (가능: {', '.join(PERSONAS)})")
    return p
