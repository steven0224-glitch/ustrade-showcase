# 전체 시스템 점검 — dual-team 적대 교차검증 (2026-06-27)

대상: 미국주식 자동매매 (paper 모의매매 전용), main 작업트리.
방법: dual-team-review 워크플로 (24 에이전트, 2.08M 토큰, ~21분). 두 팀 공유 view(co-discovery)
+ persona 분기(A=적대/보안 高recall, B=호의/동작·데이터 高precision) → lens 다양화 교차검증
→ refuted Critical/Major 타이브레이커 → 코드 결정론 병합.

## 신뢰도 집계
- **CONFIRMED 18 (Critical 0 · Major 7 · Minor 11)** + Info 7 (관찰)
- DISPUTED 0 · LOWCONF 0 · 기각(phantom) 0 · 보존 불변식 경고 0
- both 1 · cross-confirm 나머지 — **판정 충돌 없이 전건 합의** (높은 신뢰)

## 수정 결과: CONFIRMED 18 전건 + Info 2 = **20건 근본수정, 게이트 회귀 0**

### Major (7) — 전부 수정
| # | 위치 | 결함 | 수정 |
|---|------|------|------|
| M1 | dashboard/server.py | DASH_SITE_PASS 미설정+외부바인드 시 전 엔드포인트 무인증 노출 | main() fail-closed — 비-loopback 바인드인데 SITE_PASS 미설정이면 부팅 거부 |
| M2 | dashboard/build_data.py | data.js(실계좌 PII)를 OneDrive 동기 경로 기록 (paths.py 우회) | OUT 을 `USTRADE_DASH_DATA` env 로 재지정 가능(운영=비동기 경로) |
| M3 | run_live.py | 일1런 실주문 confirm 게이트 없음 — env BROKER=toss 단독 활성화 | `--confirm-live`/`USTRADE_LIVE_CONFIRM=1` 없으면 toss 거부. 대시보드는 confirm 통과분만 전달 |
| M4 | run_live.py / live_engine.py | 공유책 락 경로 동일성이 USTRADE_HOME 에만 의존(검증 없음) | 페르소나 책·락을 persona_home() 단일소스에서 도출. run_once 에 lock_path 주입 — env 독립 |
| M5 | intraday_guard.py | max_position_weight 캡이 flat 진입에 미적용(무제한 사이징) | allow() BUY 캡을 진입·추가 공통 post-trade 검사로 |
| M6 | intraday_guard.py | 비중캡 pre-trade 보유만 검사 — 1주문 오버슈트 허용 | (보유$ + 주문$)/eq 초과 거부 (sig.amount 사용) |
| M7 | dashboard/build_data.py | closes=SPY뿐이면 min([]) ValueError 빌드 크래시 | `if comps:` + len(rows) 0나눗셈 가드 |

### Minor (11) — 전부 수정
- m1 toss_check.py 계좌번호 평문 → 마스킹(****1234)
- m2 server.py StaticFiles data.js 무인증 → M1(부팅거부)+M2(재지정)로 흡수
- m3 run_intraday.py 백데이트 1860s(임계 1800 대비 60s 마진) → guardrail 임계서 도출 2700s
- m4 build_data.py local 폴백 페르소나 `curve` 누락 → 추가(스키마 일치)
- m5 personas.py docstring '$2000' → '$100000'
- m6 build_data.py `pct` 의 `if b` 가 b=NaN 통과 → `b and b==b`
- m7 fmp_factors.py 전 티커 실패 시 빈 DF → snap["pe"] KeyError 가드
- m8 fmp_factors.py earnings `r["date"]` KeyError(.get 비대칭) → .get + try
- m9 broker/paper.py 책 _load 비유한/음수 cash·qty 미검증 → math.isfinite+부호 검증, _save allow_nan=False
- m10 live_exit.py RSI '과열 트림' 라벨/전량매도 실행 불일치 → 라벨 '과열 청산'으로(실행과 일치)
- m11 build_data.py read_decisions 메모파싱 all-or-nothing → per-line 격리(깨진 라인만 skip)

### Info (7) — 2 수정 / 5 노트
- i6 personas.py wood thrust_min 암묵 기본 → 명시(0.001, 동작 불변) **[수정]**
- i7 페르소나 고점/저점이 48점 다운샘플서 과소표시 → _downsample 전역 극값 강제 포함 **[수정]**
- i1 toss.py `paper=True` 무영향(명명-동작 불일치) — **노트**: 기본 paper=True+tests_toss 결합으로 fail-fast 추가 시 게이트 파손. 현 위험 0(전 호출부 paper=False 명시)
- i2 server.py dashpass 쿠키 평문 — 노트(httponly+samesite+secure 완화, 기능상 필수 아님)
- i3 server.py /api/job 무인증 — 노트(M1 fail-closed 로 노출 자체 차단, 위험 0)
- i4 managed.py _intent_qty drift — 노트(reconcile 자가치유, 무해)
- i5 intraday_rules.py _opening_range 데드코드 — 노트(ORB 동작 영향 0, 선보존 위해 미삭제)

## 추가 선수정 (게이트 baseline)
- tests/tests_stage5.py heartbeat 테스트 격리 결함(실시계 누수 → 장중 실행 시 오발) → minutes_since_open/STATE_DIR temp 격리. time-bomb 제거.

## 검증
- 캐노니컬 배포 게이트 `tools/run_tests.py`: **ALL 10 SUITES PASS** (canslim 포함, 27 check)
- 전체 pytest 18스위트: 17 PASS (stage6=vectorbt 미설치 — 설계상 배포게이트 제외)
- 신규 동작 직접 검증: M5/M6 캡(flat 대형진입·add 오버슈트 거부), i7 극값보존, M3 confirm 게이트(무확인 toss 거부)

## 보안 불변식 재확인 (점검에서 위반 0)
- 시크릿 전부 os.environ.get (하드코딩 0)
- 실거래 경로: TossQuoteClient 무주문 유지, 장중루프 PaperBroker 전용. M3 로 우발 실거래 차단 강화
- 대시보드: M1 으로 외부노출 시 SITE_PASS 강제(fail-closed)
