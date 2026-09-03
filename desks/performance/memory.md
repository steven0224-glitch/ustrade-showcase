# performance — 축적 메모리

> append-only. 손편집 금지 — `python desks/desk_memory.py append performance ...` 를 쓸 것.
> 이 데스크의 메모리는 **분석 방법론에 대한 교훈**을 담는다.
> 개별 트레이드 교훈은 여기가 아니라 소관 데스크로 보낸다 (soul.md write-back 표 참조).

---

### 2026-07-07 [confirmed]

측정 루프에는 **counter-metric 짝**과 **외부 앵커**가 있어야 한다. 자동매매 §B 실험을
loop-graph 렌즈로 감사한 결과, 계속 판정이 초과수익 AND 결번률 AND 오트립 조합으로
이미 짝지어져 있고(MDD 캡은 별도), 앵커도 확보돼 있었다 — SPY 외부 시세와 외부 태스크
덤프가 측정 독립성을 보장한다.

유일한 공백은 **paper 시뮬 체결 순환**이었다: 시뮬이 시뮬을 확인하는 구조.
L1 체결 대사 항목으로 큐잉됨 (docs/queue-post-freeze.md).

_근거: 2026-07-20 Perez loop-graph 렌즈 감사, wip/paper-dod-checklist.md_

**규칙: 성과 지표를 새로 만들 때 (1) 반대 방향 지표와 짝지었는가 (2) 측정 소스가 피측정 시스템 외부인가 를 확인한다.**

---

### 2026-07-17 [confirmed]

NAV 벤치마크 비교는 총수익 기준으로 해야 한다 (배당 포함 SPY). `tools/paper_nav.py`
가 NAV vs SPY 총수익, 초과 %p, MDD 를 계산하며 selftest 를 내장한다.
실환경 실측 예: 07-10~16 구간 NAV −0.09% vs SPY −0.13% = +0.04%p.

측정 구간이 1주일 수준이면 초과 %p 는 노이즈다. 판정에 쓰지 말고 배선 검증에만 쓸 것.

_근거: tools/paper_nav.py, wip/paper-dod-checklist.md A6_

---

### 2026-08-01 [hazard]

07-31 전면감사(CRIT4·HIGH4, aa2f619)를 08-01 데스크탑에서 3갈래 적대 역감사(실재성 재유도·경계 추적·34파일 diff 회귀 스캔, 개별 스위트 재실행 PASS). 결과: 6건 전부 실결함 핵 실재, 수리 diff 회귀 0. 단 ① C1 좀비락 TOCTOU는 Windows 파일공유 semantics(_SH_DENYNO)가 실제 더블트레이드를 차단하고 있어 심각도 과장(POSIX에선 실재) ② C4에 청구된 reload 실패 fail-closed는 무효 — PaperBroker._load(broker/paper.py:257-258)가 한 층 아래서 예외를 전량 흡수해 새 분기가 발화 불가 ③ 수리가 만든 신규 리스크: RunLock 통일로 죽은 pid 락 회수가 즉시→30분(dead AND age>1800)으로 후퇴, run_live 크래시 시 intraday 보호청산 최대 30분 스킵 ④ C3 panic_exit.run() 레벨 영구 회귀 테스트 부재.

_근거: 2026-08-01 역감사 세션 — deep-reasoner 인터리빙 재현 하네스(race.py S1/S2), 경계 추적 3건, diff 전수 스캔_

**규칙: 감사 주장은 실재성(수리 전 코드에서 재유도)·유효성(수리 후 경계)·부작용(수리 diff 회귀) 3축으로 역검증해야 종결이다. 자체 verifier 통과 = 의도 대비 구현 검증이지 의도 자체의 검증이 아니다. 동시성 판정은 플랫폼 파일 semantics(Windows 공유모드 vs POSIX)를 명시하고 내려라.**

### 2026-08-01 [confirmed]

T0 개시 2026-08-01. pre-T0 최종 스냅샷(리셋 직전 실측): last_equity 91,846.66 · day_start_equity 93,080.45(세션 07-30) · hwm 100,634.98 · last_traded_day 07-30 · book = cash 4,856.75 + CVS 276주@107.78874 + CSCO 256주@113.80110 + LLY 25주@1218.94611 · dividends 커서 07-30. 리셋 13:25:25 실행(ClaudeT0Reset 경유, state 3파일 삭제, watch 2파일·runs.jsonl 은 보존). 백업: %LOCALAPPDATA%\ustrade\state_archive\pre_t0_20260801\ (SHA256 대조 + 실사·삭제 기록 12파일). §B 판정 시계는 이 리셋부터 시작 — 첫 fresh 런 2026-08-03(월), 집계는 --since 2026-08-01.

_근거: docs/paper-trading-dod.md T0 실행 기록(2026-08-01)_

**규칙: §B 성과 집계는 이 스냅샷 이전(pre-T0 shakedown) 데이터를 절대 포함하지 말 것 — 기준선은 08-03~ fresh $100k.**

### 2026-08-01 [corrected]

위 2026-08-01 T0 항목의 '집계는 --since 2026-08-01' 은 off-by-one 오류 — 정정하면 --since 2026-07-31. paper_nav.py --since 는 런 레코드의 session(거래된 세션 날짜) 필드로 거른다. 첫 fresh 런은 2026-08-03(월) 06:10 이고 이 런이 처리하는 최신 완결 세션은 07-31(금) — 월요런이 금요 세션을 처리하는 기존 실측 패턴과 동일. pre-T0 마지막 런(07-31 23:40 실행)의 session 은 07-30 이라 07-31 과 겹치지 않는다. 08-01 로 필터링하면 fresh $100k 기준선의 첫 post-T0 레코드가 집계에서 빠진다.

_근거: 2026-08-01 정오표 검토 — docs/paper-trading-dod.md:122-124, HOUSE.md:39-41 동시 정정_

**규칙: §B 집계 --since 값은 리셋 실행일이 아니라 '첫 fresh 런이 처리하는 최신 완결 세션 날짜'로 정한다.**

### 2026-08-09 [confirmed]

§B T0 를 2026-08-09 로 재설정(v2.2). 폐기 = PC 5런(세션 07-31·08-04·08-05·08-06). 판정 규칙·전략 파라미터는 전부 불변이고 바뀐 것은 실행 머신과 시계뿐. 12주 창 = 2026-08-09~2026-11-01. 이전 PC 북(자산 98,176)과 VM top3 북(자산 91,022)은 둘 다 §B 증거가 아니다 — 각각 결번·설정 불일치.

_근거: 2026-08-09 사용자 결정(AskUserQuestion: VM fresh 재시작) + docs/paper-trading-dod.md §B v2.2_

**규칙: §B 판정문에 인용하는 수치는 T0=2026-08-09 이후 VM ustrade-entry 런만. 08-09 이전 런(PC 5런·VM top3 이력)은 _pre_t0_20260809 에 보존돼 있어도 성과 근거로 쓰지 않는다.**
