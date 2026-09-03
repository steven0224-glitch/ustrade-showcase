# 전체 시스템 점검 — 수렴 루프 Round 2 (2026-06-27)

방법: dual-team-review (23 에이전트, 2.13M 토큰). Round 1(7f174bf) 수정 회귀검증 + 잔여·심층 발굴.

## 신뢰도 집계
- **CONFIRMED 10 (Critical 0 · Major 3 · Minor 7)** + Info 다수
- DISPUTED 0 · LOWCONF 0 · 기각 0 · 불변식 경고 0 — 전건 합의(both 0, 전부 cross-confirm)

## 수정 결과: CONFIRMED 10 전건 근본수정, 게이트 회귀 0

### Major (3)
| # | 위치 | 결함 | 수정 |
|---|------|------|------|
| 1 | build_data.py:1079 | insights breadth `above/len(rows)` 무가드(R1 M7 누락) — SPY 단독 시 빌드 크래시 | `if rows:` 로 Breadth insight 감쌈 |
| 2 | live_engine.py:259 | `record_error`가 던진 HaltError가 형제 except 못잡고 escape → 부분체결 누락(불변식 위반) | record_error 를 try/except HaltError 로 감싸 status=error+orders 반환 |
| 3 | run_exit.py:145 | 동일 클래스(청산 경로) — 손절 미집행 은폐(exit_incomplete 우회) | 동일 수정 |

### Minor (7)
- server.py:259 fail-closed loopback 화이트리스트 IPv6 풀폼/대괄호/127.0.0.0/8 미인식(과차단) → `ipaddress.is_loopback` 정규화
- live_exit.py:11 docstring + run_exit.py:186 help '트림' 잔존(R1 m10 부분수정) → '청산'으로 통일
- live_engine.py:177 전-NaN 패널 `prices.index[-1]` IndexError → `len(prices.index)` 가드
- selection_review.py:120 `_pio_bucket` float 비유한/비숫자 미가드 → try/except + isfinite(NaN 오분류·throw 방지)
- selection_review.py:134 `_score_bucket` float() 비숫자 ValueError → try/except 흡수
- run_exit.py:112 end_excl 호스트로컬(KST) vs today ET 불일치 → today(ET) 기준 +1일

### Info (미수정, 노트) — 위험 0/UX/도달불가
- build_data.py:580 equity_curve NaN 흡수(선재 약점) / OUT 기본값 마운트 디렉토리(M1 fail-closed 로 노출차단)
- build_data local 폴백 intraday/daily_pnl_pct 키 누락(프론트 가드됨) / sw.js 오프라인 stale UX 갭
- data.py load sort_index/drop_dup 미적용(yfinance 정상정렬) / panic·run_exit session=None KST 폴백(캘린더 장애 시만)
- live_select_buffett 빈Series 폴백(도달 경로 없음) / scheduler 대안경로 RunLock(하위 보장)
- **검증 통과 확인(Info)**: confirm 게이트 정상경로 과차단 없음, lock_path None/Path 분기 정합, M4 책경로 cron 일치(STATE_DIR==persona_home), backdate 산술(2700>1800)

## 검증
- 캐노니컬 게이트 `tools/run_tests.py`: **ALL 10 SUITES PASS** (회귀 0)
- 직접검증: server IPv6 loopback 7종 통과/외부 4종 거부, selection_review 비숫자·NaN→'n/a', build_data 빈 rows 무크래시

## 루프 상태
Round 2 = 10 CONFIRMED 수정 → **0 아님 → Round 3 진행**. 수렴 기준 = CONFIRMED(Major+Minor) 0건.
