# risk — 축적 메모리

> append-only. 손편집 금지 — `python desks/desk_memory.py append risk ...` 를 쓸 것.

---

### 2026-07-07 [confirmed]

손절선은 **앵커가 무엇이냐**에 따라 완전히 다른 물건이 된다. 두 개를 섞지 말 것:

- **신규 진입 제안선** — `atr_stop` = 오늘 종가 − 2.5×ATR. 진입 시점의 리스크 산정용.
- **보유 추적선** — 샹들리에 = 60일 최고 종가 − 2.5×ATR. 보유 중 이탈 판정용.

레버리지 ETF 를 다룰 때 추가 함정: 보유 CSV 의 평단은 레버리지 ETF 가격인데 스톱
계산은 원자산 차트 기준이라 단위가 다르다. 평단을 앵커로 쓰면 안 된다.

_근거: lessons/2026-07-07-stop-proximity-anchor-math.md_

**규칙: 손절 계산에 쓰는 앵커가 진입용인지 보유용인지 명시하고, 두 함수를 분리 유지한다.**

---

## 아직 비어 있는 영역

이 데스크에는 **실전 사이징 교훈이 아직 없다.** paper 단계라 자연스럽지만,
T0 이후 첫 포스트모템부터 performance 데스크가 여기에 기록을 보내기 시작해야 한다.
비어 있는 상태가 3개월 이상 지속되면 write-back 배선이 끊긴 것이므로 점검할 것.

### 2026-07-31 [hazard]

스케일급변 재seed 를 무조건 적용하면 손실이 클수록 가드를 빠져나간다(−20% 트립, −85% 무트립) — 재seed 는 HWM 교차확인 동반 필수

_근거: 2026-07-31 전면감사 A갈래, guardrail.py 수리_

**규칙: baseline 재seed 로직은 반드시 '큰 손실일수록 잘 잡히는가' 단조성 테스트를 동반한다**

### 2026-07-31 [hazard]

§B(canslim) 는 백테스트 짝이 없어 paper 런이 최초 검증이다 — 백테스트 수치(월간 리밸런스·비용 1중·vol 풀투자 폴백)는 라이브 대비 낙관 편향. T0 판정 시 전제할 것

_근거: 2026-07-31 전면감사 리서치갈래_

**규칙: T0/§B 판정 문서에는 '백테스트 부재' 명시를 포함한다**

### 2026-08-01 [corrected]

페르소나 점검 P1 6건 수정 완료: B1(재시작 직후 보호청산 워밍업게이트 무보호창 해제) · B4(livermore+wood 피라미딩 add_frac 0.10→0.08 정합, 체결후 비중캡 상시거부 차단) · B5(전일 이월 포지션 개장 1회 flat) · A3(buffett 비양수 valuation 부호역전 마스킹) · A2(ROE 자본효율 항 추가). 전부 intraday_rules/run_intraday/personas, fmp_factors/live_select_buffett 국지 수정이며 §B 실험 경로(run_live 비-persona 분기·canslim)는 무접촉 — §B-9 사후변경금지 저촉 없음.

_근거: 2026-08 페르소나 전략감사, tests_intraday.py(P1-B1/P2-B4/P2-B5) · tests_personas.py(QV-A2/QV-A3/BUFFETT-A3)_

### 2026-08-01 [confirmed]

P2 배치 통합 완료 — 장중: 킬스위치 소비 배선·갭 baseline 승계·보호청산 계측·HWM 상태복원·조기마감.
데이터: degraded_reasons 통합·FMP 캐시 90일 상한·필수펀더 결측 탈락(net_margin+pe)·P/S 윈저화·wood z(mom) 이중계상 제거.
대시보드: 수동 HALT 버튼 페르소나 home 순회 배선(계정 스코프 동작 불변).

_근거: 2026-08-01 통합 마감 2차 — run_intraday/intraday_guard/fmp_client/fmp_factors/live_select_buffett/live_select_wood/dashboard server.py_

### 2026-08-01 [corrected]

--persona choices 를 PERSONAS 레지스트리 전체로 열면 생기던 무성 모멘텀 폴백 5경로(공유book 오염 가능) 적발·차단. livermore·chartist·livermore_swing·livermore_ctl·chartist_ctl(strategy 가 _STRATEGIES 미등록)이 argparse 를 통과하면 live_engine 의 _STRATEGIES.get(s) or select 가 명시 에러 없이 모멘텀으로 폴백 — 장중루프와 공유하는 책에 모멘텀 선정분이 체결될 수 있었다. choices 를 'PERSONAS ∩ 일1런 전략 등록표' 교집합으로 좁혀 argparse 단계에서 차단, test_v2_operational_wiring 로 회귀 고정. 부수 발견(미수정·큐잉): buffett_v2 신설로 쓰게 된 공유 _z 는 정확한 상수열에서 std 가 부동소수 오차(~1e-17)로 0 이 아니게 나와 무의미한 위상 노이즈가 z-score 로 증폭되는 결함이 있다 — v2 전용 _z_tol 로는 고쳤지만 _z 원본은 buffett(v1)·wood 대조군이 쓰고 있어 12주 A/B 대조군 동결 규칙상 지금은 못 고친다.

_근거: run_live.py main()·live_engine.py _STRATEGIES, tests_personas.py test_v2_operational_wiring / fmp_factors.py _z_tol_

**규칙: A/B 대조군 동결 중 발견된 공유코드 결함은 즉시 수정 대신 판정 종료일까지 큐잉 — 대조군 거동 변경이 실험을 무효화한다(2026-10-27 이후 _z_tol 통합)**

### 2026-08-08 [confirmed]

실거래 전환 조건 (사용자 명시): 오류 재발 없음 AND 연 수익률 >=15%. 동기 = 시스템 구축 재미, 궁극 목표 = 계좌 연동 후 무인 트레이딩. §B paper 판정과 별개의 상위 관문.

_근거: 2026-08-08 온톨로지 인터뷰 (vault 05_Decisions/me_ontology.md)_

### 2026-08-28 [confirmed]

chartist(SR Flip) 진입 0건은 셋업 부재가 아니라 min_risk_frac(1.2%)과 retest_low_tol(0.8%)의 산술 모순이다. 실측 게이트 통과: 돌파·무장 67 → near 400 → 반전캔들 36 → RSI 35 → risk 하한 0(전멸). 최종 후보 35건 risk/c 평균 0.483%·최대 0.708%로 요구치 1.2%의 59%에 불과. 원인: stop=min(swing_low,level)*0.997 인데 swing_low 가 level*(1-retest_low_tol) 로 하한 클램프돼 risk 상한이 구조적으로 ~1.1%(c=level 기준). 대조군 chartist_ctl 은 동일 112 세션 1건(SNDK/WDC 고변동, risk 최대 1.690%) — 짝실험이 데이터를 생산하지 못한다. 봉 확대도 실패: 5분봉 risk 최대 1.051%로 여전히 미달(상한은 봉길이가 아니라 retest_low_tol x max_chase 기하가 결정), 15분봉은 세션당 26봉 < sr_bars 30+2 라 평가 자체가 0. 임계 인하는 -EV 복귀: R:R 2.0·왕복비용 0.35%에서 손익분기 승률 p=1/3+0.0035/(3r) 이므로 r=0.708%(실측 상한)면 50%, r=0.5%면 57% 필요. min_risk_frac 자체가 entry_frac 20% 고정사이징의 땜빵이며 정통 2% 룰은 작은 리스크를 거부가 아니라 증량으로 처리한다.

_근거: 2026-08-28 chartist 1분봉 리플레이(112 종목·세션, 실제 yfinance 1m)_

**규칙: 진입거부형 하한(min_risk_frac)을 도입·변경할 때는 그 룰의 stop 배치 기하가 만들 수 있는 risk 상한을 먼저 계산해 하한 < 상한 을 확인한다. 두 감사가 각각 옳아도 교집합이 공집합일 수 있다.**

### 2026-08-28 [confirmed]

chartist 진입 0 수리 — min_risk_frac(1.2%) 거부게이트 폐기 → 리스크기반 사이징(9편·7주차 2% 룰). intraday_rules.chartist_rule: amount=min(equity×risk_per_trade×(c/risk), equity×max_position_weight×0.95), 현금·min_order 상한. personas._CHARTIST_CFG risk_per_trade=0.02 추가(chartist_ctl 공유객체라 양 arm 동일수정=짝실험 보존). 실증: research/chartist_gate_replay.py 진입 0→21건(112 종목·세션, 동일 7일 실데이터), x_min_risk_block 0, 계측사본 불일치 0. 게이트 ALL 11 SUITES PASS. risk done-criteria: 최악손실=캡38%×손절거리~0.5-1%≈0.2-0.4% equity/거래(rpt 2%보다 작음, 캡이 먼저 바인딩=보수적) · 40%캡 준수(0.95여유+intraday_guard 사후검사) · 손절가 st[stop] 진입시 확정. ⚠️ 실험 리셋: chartist/chartist_ctl가 0건에서 실데이터 수집 시작(기존 데이터=0이라 손실 없음). ⚠️ 아직 미배포 — 워킹트리 게이트통과, commit/push→VM autopull은 별도 사용자 승인 필요(외부반영).

_근거: 2026-08-28 사용자 명시 지시(전체 강의 판단해 chartist 업데이트, 진입 0 불가) + chartist_gate_replay 실증 + 게이트 11스위트_

**규칙: SR Flip 류 되돌림 진입(risk 구조적 소형)은 고정 명목+min_risk_frac 거부가 아니라 리스크기반 사이징으로 처리한다. 캡이 천장이라 실손실=캡×(risk/c)로 목표 rpt보다 작아진다(보수적). 사이징 변경은 risk 데스크 done-criteria(달러손실·40%캡·손절가) 충족 후에만.**
