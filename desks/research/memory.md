# research — 축적 메모리

> append-only. 손편집 금지 — `python desks/desk_memory.py append research ...` 를 쓸 것.

---

### 2026-07-07 [hazard]

감사·분석 대상을 **프롬프트에 적힌 경로로 식별하지 말 것.** 실제 사례: "미국주식
자동매매 프로젝트를 전면 감사하라"는 발주였는데, 범위로 명시된 구성요소(docs/,
ledger/settle/scorecard, 부서 에이전트 5개)가 그 경로에는 하나도 없었다. 범위에
언급된 고유 파일명을 Projects 전체에 Glob 하니 유일 히트가 형제 프로젝트
(텔레그램_시그널_알리미)였다.

리서치에도 그대로 적용된다: 데이터가 있어야 할 곳에 없으면, 없는 게 아니라
다른 곳에 있을 가능성을 먼저 확인한다.

_근거: lessons/2026-07-07-audit-runtime-not-repo.md_

**규칙: 대상 식별은 경로가 아니라 스코프 항목 대조로 한다.**

---

## 아직 비어 있는 영역

스크리너별 노이즈 특성(어떤 시장 국면에서 어떤 스크리너가 헛것을 많이 잡는가)이
아직 기록되지 않았다. 이것이 이 데스크가 시간이 지나며 갖게 될 가장 값진 자산이다.
포스트모템에서 "종목 선정이 애초에 틀렸다"로 분류된 건이 나오면 여기로 온다.

### 2026-07-20 [rejected]

OpenAlice(TraderAlice/OpenAlice)를 신규 페르소나로 편입하자는 제안 기각 — 전략 엔진이 아니라 LLM 에이전트용 트레이딩 워크스페이스 인프라(TS/AGPL, 사람 승인게이트 전제)로 이식할 전략 자체가 없음. LLM 재량 판단은 결정론 매매루프 원칙 위반 + 백테스트 불가 + 무인 cron에 API 결번·비용 리스크. 파생 아이디어(LLM 재량 페르소나, 섀도 선행)만 queue-post-freeze 로 큐잉.

_근거: github.com/TraderAlice/OpenAlice README(2026-07-20) · HOUSE.md §7 · 2026-07-02 Vibe-Trading 채택(결정론 루프 불변)_

**규칙: 외부 리포 편입 검토는 전략인가 인프라인가 판별부터 — 인프라는 페르소나 후보가 아니다.**

### 2026-07-21 [rejected]

OpenAlice 6에이전트 적대적 재검토(07-21): 어제 기각 유지 확정. 종합이 건진 이식가능 후보 4개를 critic이 하우스 실코드/기각이력 대조로 전부 무효화 — MaxPositionSizeGuard=40퍼캡+position_bound 킬스위치로 이미 존재, VWAP=volume_profile_backtest.py에서 이미 기각(V1/V2), 가드 초크포인트=GuardedBroker가 이미 단일 통과점, JSONL회전=범용 로깅 YAGNI. 유일 net-new=MFI(볼륨가중RSI, 미보유 지표1종)이나 볼륨오버레이 전적 5기각/1통과 불리 prior + 검증전제인 KIS섀도 아직 미라이브(로컬스모크만). 사용자 확정(07-21): MFI 흘려보냄, 큐 대기 그대로. LLM재량 페르소나는 결정론불변식 위반, 회피경로(오프라인 컴파일)는 research에서 strategy 통상업무라 채택대상 소멸. AGPL 코드이식 봉쇄.

_근거: workflow wf_a7176daa-dae synthesis+critic; HOUSE.md 3-90; auto-memory ustrade-kis-volume-shadow; research/volume_profile_backtest.py; 07-21 사용자 확정_

**규칙: 외부리포 이식가능후보 목록은 하우스 자체 코드/기각이력 대조 전엔 값이 아니다 — sunk-cost 낙관은 인하우스 prior로만 반증된다.**

### 2026-07-23 [rejected]

X 아티클 3건(milesdeutscher GARCH사이징·StoicTA 1-2-3시퀀스·RuujSs 퀀트포트폴리오) 갭분석: 채택 3건 queue-post-freeze 큐잉(EWMA/GARCH 변동성예측 사이징·분산비율 DR 관찰지표·임계 드리프트 리밸런싱). 기각 4건 — ①1-2-3 시퀀스 기계화: chartist SR-flip+oneil 피벗+SPY 200MA 레짐게이트가 기능 등가물, net-new(확정종가 윅배제·반대시퀀스 청산)는 룰 디테일 또는 EOD 청산구조와 충돌, TA 오버레이 prior 불리(다이버전스6·매물대·VWAP 기각 전적) ②HRP/LW 공분산 배분엔진: 기사 스스로 등비중 beat 어렵다 인정, top_n 5~10 소형포트에 이득 미미 ③Black-Litterman 뷰 블렌딩: 기대수익 뷰 인프라 부재, 재량판단 주입은 OpenAlice 기각 계열 ④모델신뢰도 CUT/REDUCE: 하우스 엔진은 룰 스크린이라 OOS R² 대응물 부재, §B 판정기준이 이미 CUT 룰

_근거: x.com milesdeutscher/2079569980346204574 · StoicTA/2079650522961715529 · RuujSs/2077040860735349183 (07-22 수집)_

**규칙: 외부 TA/퀀트 콘텐츠는 프레임워크 신규성이 아니라 하우스 기보유 기능과의 델타로 판정한다 — 구조가 같으면 델타는 추정기 교체 수준이다**

### 2026-08-14 [rejected]

Kronos(shiyu-coder, OHLCV decoder-only foundation model, HF Hub, MIT) 라이브 도입 기각 — VM(AWS) GPU 없음 + ustrade venv torch 미설치로 실거래 경로 진입 불가. 저자도 README 에 '데모, 프로덕션 퀀트 시스템 아님' 명시. 재도입 시 도입이 아니라 리서치 IC 팩터로만: Kronos 예측수익률을 eval_factor.py IC 파이프라인에 팩터로 넣고 기존 팩터 대비 IC 측정 → 유의 없으면 재기각(desk_memory append).

_근거: 2026-08-14 외부 3소스 평가(X antpalkin=제휴광고·Notion 역DCF·Kronos)_

### 2026-08-28 [rejected]

gs-quant 채택 0건. 구성 실측: (1) 약 80%가 Marquee API 바인딩(measures_*, markets/*, api/gs/*) — GS 엔타이틀먼트 필요, 접근 불가. (2) backtests/ 엔진(20파일)은 옵션·스왑 instrument 중심이고 가격산출을 GS PricingContext 에 위임 — 현물주식 스택에 이식 불가. (3) timeseries/technicals·econometrics·statistics 는 pandas ewm/rolling 한 줄 래퍼(moving_average·bollinger_bands·RSI·macd·max_drawdown·zscores) — 우리가 이미 보유하거나 불요. (4) exponential_volatility(EWMA) 는 우리 research/vol_estimator_backtest.py 가 2026-07-23 에 이미 기각(추적RMSE 동률 0.1027, 위기 σ 악화) — gs-quant 가 추가 정보 없음. (5) 유일 후보였던 statistics.winsorize(mu +- 2.5 sigma 단일패스)는 우리 실패모드에 넣어보니 무용: 손실기업 1종(earnings_yield -3.0)이 sp100 급 25종 크로스섹션에 섞이면 현행 _z 의 나머지 24종 z-스프레드가 3.443 -> 0.086(기준의 2%)로 붕괴하는데, gs-quant winsorize 적용해도 0.159(5%)에 그친다 — 임계 자체가 오염된 mean/std 로 계산되기 때문. 대안 실측: MAD(median +- 2.5 MAD) 2.967(86%), rank(pct) 후 z 3.126(91%). (6) 유일하게 우리에 없는 개념은 backtests/actions.py 의 transaction_cost_exit(진입/청산 비용 모델 분리) — 우리 buy_mult 은 대칭. 소소하나 실재하는 갭. AggregateTrigger(ALL_OF/ANY_OF)·NotTrigger·TradeCountTrigger 는 우리 룰의 and 체인·max_trades_per_day 로 이미 충족.

_근거: 2026-08-28 goldmansachs/gs-quant 실독(sparse clone, timeseries·backtests·markets 전량)_

**규칙: 외부 퀀트 라이브러리는 '무엇이 들었나'가 아니라 '우리 실패모드에 넣고 재보았을 때 개선되나'로 판정한다. 이름이 맞는 함수(winsorize)가 우리 오염 시나리오에서 2%->5% 밖에 못 고치는 경우가 있다.**

### 2026-08-28 [confirmed]

chart_curriculum.md 신설 — chartist 원자료 전수 대장. 5-상태 태그(이식/부분/순수미이식/데이터게이트/성적기각/비대상). 핵심 정정: '미이식 != 기각' — 순수 미이식(8편 차트패턴·13 MACD·14 볼린저·15 ICT)은 검토 이력 전무라 자료 주면 첫 검사(완전 열림), 데이터게이트(5 매물대·12 VWAP)는 KIS 볼륨 섀도 라이브가 조건, 성적 기각(16 다이버전스=시즌2 5주차, 동일 자료)만 근거 있는 결정이라 재검사 무효. 시즌2 net-new 3종은 신호가 아니라 구조라 TA오버레이 5기각 prior 무관: ATR사이징(7주차, chartist 진입0 수리)·ADX(9주차, 횡보 whipsaw 차단)·RS(2주차, watchlist 노화 대안). FOMO 특별편은 코드화 대상 아님이나 '자리기준·R:R>=2·체크리스트 게이트'로 chartist 무추격 설계를 정당화.

_근거: 2026-08-28 차트 강의 전수 대장화 (노션 캡처 시즌1 16편 + 시즌2 10주차 + 특별편 2)_

**규칙: 차트 강의 원자료는 desks/research/chart_curriculum.md 대장에서 5-상태 태그로 관리한다. chartist 재설계 시 7주차 ATR 사이징이 min_risk_frac 대체 설계도다.**

### 2026-08-28 [corrected]

정정: 07-21 기록 'KIS섀도 아직 미라이브(로컬스모크만)'은 이제 틀렸다. VM 실측 — 섀도가 왕성하게 라이브다. volume_shadow.jsonl 900KB(현재, 8/27 19:59 UTC 수집중) + volume_shadow.jsonl.1 20MB(회전 백업, 8/27 16:04 UTC) = 총 ~21MB 실 분당거래량. kis_token.json 8/27 13:30 UTC(=09:30 EDT 개장) 매일 발급 → 사용자 카톡 알림과 일치. VM 시계 UTC, intraday-open 태스크가 SYSTEM 계정으로 돌아 파일이 systemprofile 경로에 씀(그래서 C:\ustrade* 검색이 못 잡았음, lessons 2026-07-22 재확인). ⚠️ 회전 정책: append_jsonl_rotating max_bytes=20MB, path.replace(path.1) 단일 백업 — 회전 때마다 이전 .1 덮어씀 → 히스토리 최대 ~40MB만 보존, 오래된 건 소실. 매물대/VWAP 오프라인 검증용 장기 코퍼스를 원하면 회전 전 주기 수확(harvest) 필요. 불변: 섀도는 여전히 매매경로 0 접촉(kis_quote.py:249) — KIS는 거래 안 함. 데이터게이트 ①섀도수집 = 충족, ②오프라인검증 = 착수 가능(단 코퍼스 깊이 = 수확 정책에 달림).

_근거: 2026-08-28 VM systemprofile 경로 실측(사용자 RDP ls)_

**규칙: KIS 섀도 상태는 C:\ustrade* 가 아니라 VM systemprofile LOCALAPPDATA(SYSTEM 태스크)에서 확인한다: C:\Windows\System32\config\systemprofile\AppData\Local\ustrade\{logs,state}. 데이터게이트 기각(매물대·VWAP)의 ①섀도 단계는 이제 충족됐다.**
