# 전체 시스템 점검 — 수렴 루프 Round 8 DEEPER (2026-06-27)

방법: dual-team-review 심화 2단계 (20 에이전트, 1.99M 토큰). R7 새 동시성 코드 적대검증 + 금전보존
불변식·크래시복구·락 인터리빙 증명.

## 신뢰도 집계
- **CONFIRMED 11 (Critical 0 · Major 5 · Minor 6)** — 전건 cross-confirm, DISPUTED/LOWCONF 0
- R7 수정엔 회귀·deadlock 없음(적대검증 통과). 새 Major 5는 동시성·아키텍처·toss-비활성 축.

## 수정 결과: 2 contained 수정 + 아키텍처/비활성 잔여 명시 노트

### 수정 (2)
| 위치 | 결함 | 수정 |
|------|------|------|
| archive_paper_runs.py | intraday snapshot 무락 append ↔ archive RMW lost-update(M2) | 장중(market_is_open) archive 변경 거부(--dry 허용) — 분당 snapshot 경합 창 차단 |
| archive_paper_runs.py | 권위브로커 산정이 RunLock 밖 — TOCTOU 오분류 이관 | `_authoritative_broker()` 호출을 RunLock 안으로 이동 |

### 노트 — 아키텍처/비활성/반증(강제수정이 더 위험, 명시 보존)
- **M1 (dashboard api_resume HALT 해제)**: paper 컨텍스트 resume 이 공유 전역 HALT 해제. 그러나 이는 **운영자 명시 제어 경로**(halt/resume 쌍, guardrail:267 주석이 정당 경로로 명시, Info 반론)이고 게이트를 걸면 **운영자 resume 버튼이 작동 불가**. 현재 toss 비활성 → 무영향. **근본수정 = toss 활성화 시 namespace별 HALT 파일 분리**(공유 HALT 한계). R7 reset() 게이트가 무의도 부작용 경로(run_live persona reset)는 이미 차단. → server.py 변경 revert + 주석 명시.
- **M3 (book·intraday_guard 2파일 비원자)**: 체결~note_fill 사이 SIGKILL 시 trades 1 과소·min-hold 1건 누락(전 페르소나, 락은 동시성용이라 크래시-원자성 무관). 근본수정=두 state 단일 atomic 또는 재시작 시 book 당일체결로 guard.trades 재시드 — 아키텍처 변경. 1-trade 슬랙·paper·희소 크래시창. (halt 래치는 allow() 트립 시 즉시 _save 되어 별개 보존.)
- **M4 (toss 금액주문 멱등키에 amount 포함)**: 응답유실 재플랜 시 amount 변동→키 갈림→중복주문. 단 amount-in-key 는 **부분체결 잔액 재주문용 의도 설계**(toss.py:313). toss **비활성**. 근본수정=응답유실을 'unknown'으로 분류 후 get_order 정산 reconcile 게이트 — **toss 활성화 前 필수 항목**으로 문서화.
- **M5 (교차 프로토콜 run.lock)**: Info 재판정이 _LOCK_HARD_SEC 6h 상한 + _pid_alive conservative-True 로 **현실적 무위험** 입증(PID 재사용 오회수는 안전측). 수정 불요.
- Minor: already_traded↔mark_traded 크래시창(RunLock+reload 일부 흡수, paper), 대시보드 MTM 에폭(display-only) — 노트.

## 검증
- 캐노니컬 게이트 10/10 PASS. archive 임포트 스모크 OK(market_is_open 연동). server revert 후 정상.

## 루프 상태
- 심화 2단계(R8): R7 수정 회귀 0. 새 발견은 **아키텍처 동시성·toss-비활성·refuted-safe** 축 — fixable 축의 가치 체감.
- 결론: **심각도·실효성 기준 수렴 도달**. 잔여 Major 는 (a)공유 HALT 분리 (b)2파일 atomic (c)toss 멱등 reconcile — 전부 **toss 활성화/대규모 리팩터 시점** 항목이거나 paper-희소-크래시. R9(최종 확인) 후 종료·배포.
