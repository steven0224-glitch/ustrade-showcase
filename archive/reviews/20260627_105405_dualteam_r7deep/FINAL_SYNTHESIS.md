# 전체 시스템 점검 — 수렴 루프 Round 7 DEEP-DIVE (2026-06-27)

방법: dual-team-review 심화모드 (23 에이전트, 2.5M 토큰). 표면 위생 소진 후 **호출그래프·상태전이표·
동시성 인터리빙·금전 수학·보안경계 end-to-end 추적**. 사용자 지시("반복할수록 더 깊이")로 심도 상향.

## 신뢰도 집계 — 심화가 심각도 재발굴
- **CONFIRMED 18 (Critical 1 · Major 4 · Minor 13)** — 전건 cross-confirm, DISPUTED/LOWCONF/기각 0
- R4~R6 위생성 테일과 달리, 심층 추적이 **표면스캔이 놓친 핵심 안전결함**(killswitch namespace 누설·
  락 경합·HWM 영구하향·공유책 lost-update)을 끌어냄.

## 수정: Critical 1 + Major 4 + Minor 6 = 11건 근본수정, 전체 pytest 회귀 0

### Critical (1)
| 위치 | 결함 | 수정 |
|------|------|------|
| guardrail.py reset() | paper_<persona> reset 이 namespace 무관 **전역 HALT 삭제** → toss 실거래 정지 무성 해제 | `self._namespace` 추가, paper* namespace reset 은 전역 HALT 미삭제(전역/실거래·dashboard 만 해제) |

### Major (4)
| 위치 | 결함 | 수정 |
|------|------|------|
| archive_paper_runs + run_live._journal | runs.jsonl 무락 RMW ↔ 동시 append → 권위 레코드 lost-update | 양쪽 동일 RunLock(STATE_DIR/run.lock) 도메인으로 직렬화(_journal 은 LockBusy 시 best-effort) |
| guardrail.resume_if_new_day | 정지해제 후 roll_day 미호출 경로(run_exit)서 stale day_start_equity | resume 시 day_start_equity=None 무효화 → 다음 check_daily_loss 가 재seed |
| live_engine._run_once_locked | 공유책 _load 가 RunLock 밖 → 장중루프 체결 lost-update 클로버 | RunLock 진입 직후 broker.reload()(reload-in-lock, intraday 패턴) |
| guardrail.roll_day:282 | 스케일 재seed 가 누적DD HWM 영구 하향 → GUARD-1 무력화 | hwm 은 `max(기존,현재)` 보존(하향 금지, 단조증가 불변식) |

### Minor (6 수정)
- broker/paper.py: 0/NaN/inf 체결가 금액주문이 현금 차감+FILLED → **현금 증발**. 비정상 체결가 REJECT.
- dashboard/server.py: api_run confirm 게이트가 truthy-만 검사('false' 문자열 통과) → 화이트리스트 + confirm_live 결속.
- intraday_guard.py: eq=0.0 falsy 단락으로 비중캡 미평가(fail-open) → `eq<=0` fail-closed.
- heartbeat.py: 장중 intraday session 레코드가 일1런 dead-man 게이트 마스킹 → `not rec.get('intraday')` 필터.
- guardrail.py: max_consecutive_errors 주석 off-by-one 정정(>=N).

### 노트(미수정 — 잔여 Minor: 아키텍처/희소/display, 후속 라운드 후보)
- 스케일 재seed 가 >80% 단발 실손실 daily_loss 트립 자동해제(fail-open) — M4 가 HWM(영속) 보호, daily_loss 는 당일·휘발이라 위험 낮음, data/broker 글리치 필요
- PID 재사용 시 백데이트 무력화 → 최대 6h LockBusy(희소) / 교차프로토콜 락 회수 비결정(O_EXCL 최종방어 유지)
- persona runs.jsonl append 무락 인터리브 / 체결후 book·guard·journal 3파일 비원자(크래시 시 회전캡 1 느슨)
- 대시보드 라이브MTM equity↔daily_pnl 에폭 불일치·'장중' 라벨 stale·equity곡선 stale 보유 누락(display-only)

## 검증 (전체 pytest)
- 전체 pytest: stage1/4/5/7/8 등 전 스위트 통과(stage6=vectorbt만). tests_hardening/panic/exit/managed/intraday 전부 PASS.
- 캐노니컬 10/10. 직접검증: C1 paper reset HALT 보존·toss reset 해제, M4 hwm 보존(100), m1 0체결가 거부+현금보존, m8 confirm.

## 루프 상태
- **심화 1단계(R7)에서 Critical 1 재출현** — 표면 수렴(R4~R6)이 심각도 수렴은 아니었음. 깊이가 새 층위 결함 노출.
- 사용자 계획: 2~3 deep 라운드(R7 완료) → R8(더 깊게)·R9(최종) → 수렴 종료 후 VM 배포.
