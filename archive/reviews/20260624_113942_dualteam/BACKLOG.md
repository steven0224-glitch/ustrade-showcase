# BACKLOG - 잔여 수정 항목

## Major (34)

### Major-1  `broker/toss.py:283,289 (int(req.qty) 절삭)`
- int(req.qty) 절삭으로 분수 basis(<1주) SELL 이 quantity '0' 주문으로 토스 전송 — 좀비 포지션·가드 오트립
- rec: place_order 진입에서 int(req.qty)<=0 이면 즉시 REJECTED 반환(전송 금지). run_exit/panic SELL 수량을 int() 내림하고 0 이면 스킵. 분수 basis 는 별도 수동확인 알림.

### Major-2  `run_exit.py:41-145 (15분 주기, already_traded/주문 dedupe 부재)`
- 장중청산이 15분마다 재실행되나 in-flight 추적 없어 미체결 폴링 실패 시 재매도(oversell)
- rec: run_exit 에 '직전 N분 내 동일 심볼 SELL 제출' in-flight 추적(저널/state) 추가, 미체결 주문 잔존 시 그 종목 재SELL 스킵. clientOrderId 결함 수정과 병행.

### Major-3  `run_exit.py:72-76 (is_halted 조기반환, resume_if_new_day 미호출) + namespace 'toss' 공유`
- 어떤 사유의 킬스위치 HALT 든 보호용 장중 청산 오버레이를 함께 비활성화 — 가장 필요한 순간 자동 손절이 꺼짐
- rec: 장중 청산은 위험 축소 방향이므로 daily_loss/error-window HALT 에서도 동작하게 분리(panic 의 GuardedBroker 우회처럼)하거나, 최소 run_exit 시작에 resume_if_new_day() 호출. exit 경로 record_error 트립이 청산 자체를 막지 않게 게이트 검토.

### Major-4  `run_exit.py:116-120 (청산 중 tripped/error 반환 경로)`
- 청산 중 가드레일 트립/예외 시 알림 없이 dict 만 반환 — 리스크관리 청산 실패가 사일런트
- rec: run_exit 의 tripped/error 반환 직전 notify(reason,'halt'/'error',ts) 추가. run_live._alert 와 동등 알림 커버리지 부여.

### Major-5  `run_exit.py:46-48 (TOSS 자격증명 미설정 분기)`
- 청산 루프가 토스 키 결측 시 알림 없이 error 반환 — 슬리브 분기와 비대칭, 손절 보호 무성 중단
- rec: line 48 return 전 notify('청산 거부 — TOSS 자격증명 미설정','error',ts) 추가(슬리브 분기와 동일). 또는 heartbeat 가 exits.jsonl 감시.

### Major-6  `heartbeat.py:23-55 (_traded_sessions/check, runs.jsonl 만 감시)`
- dead-man-switch 가 일일진입(runs.jsonl)만 감시 — 장중 청산 cron 사망은 미탐지
- rec: heartbeat 에 정규장 시간대 동안 exits.jsonl(또는 panics.jsonl) 최근 실행 timestamp 가 N분 이내인지 점검하는 별도 체크 추가. 장중 청산 cron dead-man-switch 를 독립 배치.

### Major-7  `notify.py:22-44 (_telegram/_slack)`
- 알림 전송이 HTTP 응답·ok 플래그 미검사 — 전달 실패를 성공으로 오인(마지막 통보 채널의 무성실패)
- rec: _telegram 에서 resp 상태와 resp.json().get('ok') 검사 후 False 시 _log.error 로 채널실패를 로컬로그에 명시. _slack 도 status_code 2xx 검사. 최소 전송실패를 ustrade.log ERROR 로 남길 것.

### Major-8  `panic_exit.py:72-99 (place_error 누락 + status 'ok' + 알림이 plan 대비 미청산 미표기)`
- 비상 전량청산이 일부 종목 미청산에도 'ok' 보고 — 잔존 노출 미표면화
- rec: panic 결과에 plan 대비 미청산 failed=[(sym,qty_remaining)] 계산·반환, failed 비어있지 않으면 'ok' 대신 'partial' 또는 error-레벨 알림으로 '청산실패/잔존: SYM N주 — 토스앱 수동매도' 발송.

### Major-9  `run_exit.py:122-137 (_await_fills 후 부분/미체결 청산주문 처리)`
- 청산 주문 부분체결·취소실패가 알림·정합성검증 없이 흘러감 — 잔존수량·oversell 위험 무성
- rec: 청산 주문별 filled_qty 와 요청 qty 대조해 잔존수량 산출, 잔존>0 이면 notify(error). cancel_order False/예외 카운트해 1건↑ 시 경보.

### Major-10  `run_exit.py:68-71 / panic_exit.py:113-114 (toss.market_open 예외 → 전체 청산 abort)`
- 일시적 market-calendar API 실패가 청산 run 전체를 crash 시킴 — fallback 없음
- rec: 청산 게이트에서 캘린더 조회 실패를 별도로 잡아 '캘린더 불확실 — force_open/재시도' 경고를 보내되 청산 시도 자체를 막지 않거나, 최소 'closed' 아닌 별도 status 로 분리해 crash 누적/halt 트립과 구분.

### Major-11  `broker/toss.py:255-260 (market_open naive vs aware datetime 비교)`
- market_open 의 naive vs aware 비교 TypeError 삼킴 → 항상 휴장 판정 위험(장중청산 silent 차단)
- rec: startTime/endTime 파싱 후 tzinfo None 이면 명시적 TZ(토스 문서상 KST 또는 ET)로 localize 후 동일 TZ 정규화 비교. 파싱 실패/형식이상은 False 로 삼키지 말고 notify 로 표면화해 silent 차단 가시화.

### Major-12  `live_engine.py:220-239 (sells+buys 단일 루프) + broker/executor.py:54-73 (proceeds 선반영)`
- 매도 미체결 상태에서 매도예상대금을 예산에 넣고 매수 동시 제출 — 미정산현금 과매수 위험
- rec: 매도 먼저 제출→_await_fills 종결 확인→get_account 재조회 실제 가용현금으로 매수예산 재계산 후 매수 제출(2단계 분리)하거나 매수예산을 proceeds 제외(현금만)로 보수화. 토스 미정산현금 매수 허용 여부 실증 후 결정.

### Major-13  `broker/managed.py:175-184 (get_account) + broker/executor.py:28 (investable=equity*alloc)`
- cash_cap 이 현금만 capping, managed 평가액은 무제한 → equity 과대 → 사이징 과대·노출 점증
- rec: investable 산정 시 equity 대신 min(equity, cash_cap+managed_basis_at_cost) 또는 cash_cap 기반 운용규모 상한 명시 적용. 최소 managed_val 도 cash_cap 비례 캡 또는 cash_cap 을 '슬리브 총 equity 상한'으로 재정의해 사이징 입력에 반영.

### Major-14  `broker/managed.py:175-184 (get_account quote 실패 폴백)`
- managed 평가액 산정 시 quote 실패를 avg_price 로 조용히 폴백 — 손실가드 baseline 왜곡(silent 우회)
- rec: quote 실패 시 avg_price 폴백 대신 해당 종목을 baseline 산정에서 제외하거나 가드를 보수적(equity 하향) 처리, quote 실패를 notify/journal 가시화. 다수 종목 quote 실패 시 거래 보류.

### Major-15  `broker/managed.py:189-200 (place_order pending 영속 후 inner 예외 unguarded)`
- ManagedBroker.place_order 가 toss 거부를 catch 안 해 pending 영속 후 예외 전파 — 멱등 결함과 결합 시 중복 위험
- rec: place_order 가 inner 예외 시 방금 더한 pending 롤백/유지 정책 명시. in-flight 주문ID 를 pending 과 함께 영속해 재플랜 시 중복 방지. 멱등키 결함 우선 수정.

### Major-16  `broker/toss.py:203-212 (get_account 환율/통화 정합)`
- 원화계좌 자동환전 시 USD buying-power 0/불안정 또는 KRW 혼입 → 사이징·가드 동시 오작동(~1300배 과대 위험)
- rec: 응답 currency 필드 검증(USD 아닌데 USD 요청이면 fail-closed), cashBuyingPower 비정상(0/직전대비 환율급변폭) 시 거래 거부. 펀딩 방식(환전 시점) toss_check 실증. USD 금액↔USD equity 만 비교됨을 단위테스트 고정.

### Major-17  `README.md:334 (cron */15 23,0-4) + run_exit.py:12 + broker/toss.py:19 docstring`
- 청산 cron 윈도가 한 DST 체제만 커버 — 겨울(EST) 마지막 1시간 청산 사각
- rec: 청산 cron 윈도를 두 DST 합집합(KST 22:00~06:00, */15 22,23,0-5)으로 넓히거나 VM TZ 를 America/New_York 으로 두고 ET 09:30~16:00 발사. market_open 게이트가 비개장 tick 을 무해 skip 하므로 윈도 확장 부작용 없음.

### Major-18  `tools/run_tests.py:16-29 (SUITES) + tests_stage1.py:138-213, tests_stage2.py:79-100`
- 배포 게이트가 킬스위치 차단·멱등성·NaN경계 테스트를 제외 — 핵심 안전 불변식 미검증
- rec: test_c2_idempotency·test_c4_guarded_broker·NaN/0 경계를 SUITES 에 추가하거나 tests_hardening.py 로 이관. stage 전체 vectorbt 의존 전제는 stage1/2 가드 테스트엔 미해당(broker 만 의존).

### Major-19  `tests_panic.py 전체 + tests_managed.py:47-53 FakeBroker`
- Exit/panic 통합 테스트가 happy path 만 커버 — 부분체결/거부/place-error 미검증(test theater)
- rec: REJECTED/PARTIAL/place_order raise/타임아웃-미체결 시뮬 FakeBroker 변형 추가해 run_exit._run_locked·panic_exit._panic 이 (a)미청산 잔존을 알림 surface (b)basis 가 실제 체결분만 차감 검증하는 통합 테스트 추가.

### Major-20  `requirements_vm.txt vs requirements.txt (certifi 부재) + data.py:10,25`
- data.py top-level import certifi 가 VM requirements 에 미고정 — cert 검증 동작 드리프트
- rec: requirements_vm.txt 에 certifi==2026.5.20 동일 버전 핀 추가.

### Major-21  `requirements_vm.txt vs requirements.txt (schedule 부재) + scheduler.py:32,4`
- scheduler.py 가 쓰는 schedule 패키지가 VM requirements 에 없음 — 데몬 모드 시 무성 즉시종료
- rec: VM 이 원샷만 쓰면 schedule 제외를 README/DEPLOY.md·scheduler.py 상단 주석에 명시. VM 에서 scheduler 쓸 의도면 requirements_vm.txt 에 schedule==1.2.2 추가.

### Major-22  `live_engine.py:32,183 + live_select_canslim.py:44`
- 운영 canslim 이 설계값 pool=12 아닌 pool=8 로 구동 — 펀더 검증 후보풀 1/3 축소·선정 왜곡
- rec: run_live canslim 경로에서 cfg.pool 을 12 로 세팅하거나 RunConfig 에 strategy 별 pool 기본 분리. 최소 cfg.strategy=='canslim' 이고 pool 미지정이면 12 보정.

### Major-23  `live_select_canslim.py:36 vs strategies/factors.py:17 / strategies/cross_momentum.py:22`
- 라이브 canslim 12-1 모멘텀과 백테스트 12-1 산출식 off-by-one 불일치 — 운영신호≠백테스트신호
- rec: 백테스트 모멘텀과 라이브 _mom_12_1 인덱싱을 한 기준으로 통일(백테스트를 shift(20)/shift(251) 또는 라이브를 shift 의미로 정렬). 단일 산출 함수 공유로 운영신호=백테스트신호 보장.

### Major-24  `dashboard/server.py:92-96 (_auth) + :113-123 (api_run) + :131-164 (halt/resume)`
- 단일 공유 토큰으로 실매매 트리거 — 비상수시간 비교·무차단·무감사
- rec: 토큰 비교를 hmac.compare_digest 로 상수시간화. control 엔드포인트에 실패횟수 락아웃/레이트리밋 + 감사로그(누가·언제·broker) 추가. toss 실행은 토큰 외 별도 서명/OTP 또는 로컬호스트 전용 제한 검토.

### Major-25  `dashboard/data.js (git-tracked) + dashboard/build_data.py:30,820 + tools/deploy_push.ps1:35`
- 실 계좌 포지션 담긴 data.js 가 git 추적 — 배포 시 GitHub로 유출 가능
- rec: dashboard/data.js 를 .gitignore 에 추가하고 git rm --cached 로 추적 해제. 대시보드는 server.py 의 /api/dashboard(인메모리 build())만 쓰므로 정적 data.js 커밋 불필요. 또는 build_data.py main() 의 파일쓰기 제거.

### Major-26  `dashboard/tunnel.ps1:26-32 + dashboard/server.py:44-58 (site_gate)`
- DASH_SITE_PASS 미설정이어도 3초 후 공개 터널 강행
- rec: DASH_SITE_PASS(및 control 노출 시 DASH_TOKEN) 미설정이면 경고가 아니라 exit 1 로 중단. server.py 도 0.0.0.0 바인딩 시 DASH_SITE_PASS 없으면 기동 거부 검토.

### Major-27  `broker/toss.py:127-135,148-150 (_request) + :277-306 (place_order)`
- POST /orders 가 5xx·네트워크오류에 자동 재시도 — 응답유실 시 중복주문, 서버 dedupe 가정에만 의존
- rec: (1) 토스 문서로 clientOrderId 멱등(중복 시 409/기존주문 반환)을 toss_check.py 검증 케이스로 실증. (2) 보장 불확실 시 POST /orders 를 재시도 대상에서 제외하거나, 재시도 직전 GET /orders 조회 후에만 재제출.

### Major-28  `live_engine.py:228-249 + run_exit.py:114-131 + panic_exit.py:73-87 + broker/toss.py:290,308-314`
- 제출 후 미체결 DAY주문 취소가 best-effort + 취소 전 크래시 시 미취소 DAY주문이 늦게 체결돼 이중 노출
- rec: cancel 실패를 침묵 통과시키지 말고 record_error/알림으로 승격. 미체결 잔존 주문을 다음 실행 시작 시 broker open-orders 조회로 능동 취소하는 startup reconcile 추가(현재는 슬리브 basis 만 reconcile).

### Major-29  `broker/executor.py:29-33, broker/toss.py:84-107, run_live.py:88-90, review.py:31`
- 토스 실주문에 수수료·세금·환전스프레드 미반영 — 사이징 쿠션이 cost_buffer 하나뿐
- rec: TossBroker 에 실측 수수료율·예상 환전스프레드를 _commission 등으로 노출하거나, cost_buffer 기본값을 실수수료+환전스프레드 상한(≥1.0%) 기준 상향. review.py 자동튜닝은 실현 슬리피지만 보므로 수수료/세금은 별도 상수로 명시.

### Major-30  `broker/toss.py:8,16-18,203-212 + broker/executor.py:28,63,67`
- 환율 변환 코드 부재 — cashBuyingPower(USD)를 검증 없이 가용현금으로 신뢰
- rec: 실거래 전 계좌가 USD 직접펀딩인지 코드로 확인(buying-power currency 응답 검증)하거나, 원화계좌면 환율 신선도·변동 버퍼를 cost_buffer 와 별도 추가. 최소한 cashBuyingPower 가 0/비정상일 때 거래 거부 게이트 추가.

### Major-31  `live_engine.py:90-110 (_reconcile) + base.py:71 + live_engine.py:282-285`
- 주문↔체결 reconciliation 이 수량만 대조 — 체결금액/현금 정합성 미검증
- rec: _reconcile 에 체결금액(Σfilled_qty·avg_fill_price) vs 예상금액(qty·ref_price·buy_mult) 대조 추가, 임계 초과 시 drift 보고·알림.

### Major-32  `broker/toss.py:281-283 (place_order clientOrderId)`
- 멱등 주문키가 ET 세션 아닌 KST date.today() 기반 — 자정 넘김 재실행 시 중복주문 차단 실패
- rec: clientOrderId 의 day 를 date.today()(KST) 가 아니라 호출측이 보유한 ET 세션 날짜(last_completed_session 결과)로 산출. place_order(req, session_day=...) 주입 또는 last_completed_session().isoformat() 을 키 재료로. KillSwitch 멱등과 브로커 멱등이 같은 날짜 기준 공유.

### Major-33  `broker/toss.py:243-260 (market_open) + run_exit.py:70 + panic_exit.py:114`
- 장중청산 게이트가 토스 시각이 naive ISO면 TypeError→항상 False → 청산 무성 비활성 위험
- rec: 파싱한 start/end 가 naive(tzinfo None)면 기준 tz(KST 또는 ET)로 명시 localize 후 비교, 또는 전부 UTC 정규화. TypeError→False 경로를 '캘린더 파싱 실패'로 구분 로깅/알림해 무성 비활성을 관측가능화. 운영 전 toss_check.py 로 실제 startTime/endTime 포맷(오프셋 유무) 실증.

### Major-34  `notify.py:22-32 (_telegram), 35-44 (_slack) + README.md:344`
- 알림 전송이 HTTP 응답코드 미검증 — 실패해도 성공으로 보고(무성실패)
- rec: _telegram/_slack 에서 raise_for_status(또는 200<=status<300) 확인 후에만 True 반환. 텔레그램은 응답 JSON 의 ok 필드까지 확인. 실패 시 _log.warning 으로 회전로그에 남겨 사후 추적.

## Minor (37)

### Minor-1  `dashboard/server.py:44-58 (site_gate) + dashboard/tunnel.ps1:9 — ?k=<DASH_SITE_PASS> 쿼리파라미터 인증`
- 공유 site 비번을 URL 쿼리스트링에 노출 — 히스토리/Referer/프록시/CDN 로그 유출, 실거래 control 표면 직결
- rec: site pass 를 쿼리 대신 Authorization 헤더/POST 폼으로 받고 쿠키 secure=True 추가. 가능하면 cloudflared named tunnel + Cloudflare Access(IdP)로 대체. 최소 ?k= 접속 후 즉시 쿼리없는 URL 리다이렉트로 히스토리/Referer 잔존 차단.

### Minor-2  `dashboard/server.py:77-79 → dashboard/build_data.py:59-68,781-795 (/api/symbol/{tk} glob 유입)`
- 무인증 /api/symbol path param tk 가 검증 없이 glob 에 유입 — glob 인젝션·임의 캐시파일 열람
- rec: tk 를 ^[A-Z][A-Z0-9.\-]{0,9}$ 화이트리스트 검증, 불일치 400. _longest_csv 진입 전 glob.escape(). 가능하면 알려진 유니버스 심볼만 허용.

### Minor-3  `dashboard/server.py:16,20,187,192 — DASH_HOST=0.0.0.0 가이드 + 정적 마운트 디렉토리 전체 노출 + env 미설정 무인증`
- docstring 이 0.0.0.0 바인딩 유도 + StaticFiles 디렉토리 통째 서빙 + env 미설정 시 무인증 노출
- rec: DASH_HOST 가 0.0.0.0/공인IP 일 때 DASH_SITE_PASS·DASH_TOKEN 미설정이면 기동 거부(fail-closed) 가드+단위테스트. StaticFiles 를 명시 파일(index.html,data.js,icon.svg,manifest.json) 화이트리스트로 축소. docstring 에 0.0.0.0 노출 시 SITE_PASS+방화벽 동반 명시.

### Minor-4  `dashboard/server.py:52,56,95 — site-pass/토큰 비교가 비-상수시간(!=)`
- DASH_TOKEN·site-pass 를 != 단순 비교 (timing side-channel)
- rec: hmac.compare_digest(token,DASH_TOKEN) 로 교체. site_gate ?k/cookie 비교도 동일 적용.

### Minor-5  `dashboard/install_autostart.ps1:16-26 — 시작프로그램 .cmd 에 평문 시크릿 set 유도`
- Autostart .cmd 템플릿이 DASH_TOKEN/SITE_PASS 를 디스크 평문 저장 유도
- rec: .cmd 직접 박기 대신 사용자 환경변수(setx, DPAPI) 또는 별도 .env(gitignored, ACL 제한)에서 로드하도록 가이드 변경. 최소 주석에 '평문 저장 위험·setx 권장' 경고 추가.

### Minor-6  `requirements*.txt — dashboard 의존성 fastapi/uvicorn/starlette 미고정`
- Dashboard deps(fastapi/uvicorn/starlette)가 모든 requirements 파일에 부재(unpinned)
- rec: fastapi·uvicorn·starlette 를 핀 버전으로 requirements 에 추가, 보안 패치 추적 포함. 대시보드 미사용 VM 이면 requirements-dashboard.txt 분리.

### Minor-7  `toss_check.py:22 — 진단 출력에 계좌번호 평문 print`
- toss_check 가 계좌번호·accountSeq 를 stdout 에 평문 출력
- rec: 계좌번호 마스킹(뒤 4자리만) 또는 --verbose 플래그에서만 전체 표시.

### Minor-8  `broker/guardrail.py:344-353,402-414 — SELL 명목 초과가 bad price 경로에서 미검사`
- check_order_notional 가 BUY 만 의미있게 보호 — bad price SELL 은 notional 검사 우회
- rec: SELL 도 bad price 경로에서 보유수량 대비 과대(basis·실보유 초과) 검사 추가 또는 notional 캡을 시세 불량 시 avg_price 폴백 적용. 슬리브 basis 무결성 모니터(reconcile drift 알림) 연계.

### Minor-9  `run_exit.py:132-137 / panic_exit.py:88-91 / live_engine.py:172-177,243-249,253-258 (record_fills/취소/reconcile 삼킴)`
- basis-update·취소·reconcile 실패가 세 경로 모두 try/except: pass 로 무성 삼켜짐
- rec: 세 except 블록에서 최소 _journal/notify 또는 _log.warning 으로 'basis 갱신/취소/reconcile 실패' 를 ustrade.log 에 기록. bare pass swallow 는 무인 실거래 무성실패 원칙 위배.

### Minor-10  `broker/toss.py:308-314 (cancel_order)`
- cancel_order 가 실패를 False 로만 은폐 — 잔존주문 취소실패가 호출측서 무성 처리
- rec: cancel_order 가 실패 사유 로깅하거나 호출측이 False 반환 건수 집계해 1건↑ 시 notify. 최소 _log.warning 으로 cancel 실패를 ustrade.log 에 남길 것.

### Minor-11  `broker/toss.py:127-171 (_request 재시도 백오프) + fmp_client.py:71-90`
- 재시도 백오프가 선형·짧고 지터 없음 — 지속 429/5xx 시 빠른 소진·thundering herd
- rec: 지수 백오프+지터(min(cap,base*2^attempt)*rand) 도입과 transient 한도 상향 검토. 최소 백오프에 소량 무작위 지터.

### Minor-12  `panic_exit.py:73-91 (비상청산 주문실패/취소실패 즉시 알림 부재)`
- 비상청산 종목별 주문실패·취소실패를 저널만 남기고 즉시 알림 안 함
- rec: _panic 반환 dict 에 place_error 종목 리스트 포함, run() 알림에 '미청산/실패 N종목' 명시. plan 대비 filled 차집합 경보.

### Minor-13  `heartbeat.py 전체 (자기 감시 2차 모니터 부재)`
- dead-man-switch 의 cron 이 죽으면 아무도 모름 — 외부 워치독 미연동
- rec: heartbeat 정상 실행마다 외부 dead-man 서비스(Healthchecks.io 무료)로 HTTP ping 발사해 heartbeat 자체 사망 시 외부 알림. 코드 변경 1줄(requests.get(PING_URL)) 수준.

### Minor-14  `run_live.py:50-64 (_alert ok 분기) + live_engine.py:101-104 (_reconcile 실패 → 빈 리스트)`
- _reconcile 이 포지션 조회 실패를 '드리프트 없음(OK)'으로 둔갑 — 정합성 검증 불가가 OK 로 오인
- rec: _reconcile 이 포지션 조회 실패 시 빈 리스트 대신 'unknown' 신호 반환해 _alert 가 '정합성 검증 불가 — 확인 필요' 경보. 최소 _log.warning.

### Minor-15  `live_engine.py:90-110 (_reconcile 수량만 대조, 금액 reconciliation 부재)`
- 사후 정합성이 수량(qty)만 대조 — 체결금액/평단 vs 사이징기준가 금액 reconcile 부재
- rec: _reconcile 에 금액 차원 추가 — sum(체결 BUY 대금) vs 예약 budget 소진액 대조, |avg_fill-ref_price|/ref_price 임계 초과 주문 플래그. get_account cash 전후 델타로 교차검증.

### Minor-16  `broker/managed.py / executor.py / paper.py — 금액·수량 float 사용`
- 현금·수량·basis 전부 float, ==·누적합 — 다회 리밸런스·fractional 혼입 시 드리프트·오탐 알림
- rec: 현금·수량·평단을 Decimal 또는 정수 최소단위(주식=정수주)로 통일. reconcile float 동등성 비교에 round to share 후 비교로 fractional 오탐 차단.

### Minor-17  `run_exit.py:86 (end_excl = datetime.now().date() + 1day)`
- 청산 데이터 로드 상한이 KST 로컬 날짜 — 세션(ET) 기준과 불일치
- rec: run_live.py:104 와 동일하게 end_excl 을 (session+timedelta(days=1)).isoformat() 으로 ET 세션 기준 통일.

### Minor-18  `scheduler.py:18 (RUN_TIME 06:10 호스트 로컬) + tools/DEPLOY.md`
- scheduler.py·cron 이 VM 로컬 TZ=KST 암묵 가정 — UTC VM 이면 미 마감 전 조기실행
- rec: DEPLOY.md·SETUP_GITHUB.md 에 VM TZ 를 Asia/Seoul 고정(Set-TimeZone 'Korea Standard Time') 셋업 단계 명문화하거나 scheduler/cron 발사 시각을 ET 기준 산출(now_et 비교)로 변경.

### Minor-19  `run_live.py:160-161, run_exit.py:157-158, panic_exit.py:151-152 (TOSS_MANAGED_CASH float 파싱)`
- TOSS_MANAGED_CASH malformed 시 float() 크래시 — 검증·기본값 없음
- rec: _num()류 안전 파서(toss.py:80 존재)로 감싸 malformed 시 명시적 에러·notify 후 정지 또는 명확한 기본값. 빈문자열/콤마/통화기호 단위테스트 추가.

### Minor-20  `tools/run_tests.py:26 + vm_update.ps1:63 vs live_select_canslim.py:23-28`
- 게이트 주석이 A엔진 경로를 C:\텔레그램_시그널_알리미로 오기 — 실제는 형제 디렉토리
- rec: run_tests.py:26·vm_update.ps1:63 주석을 형제경로(.../Projects/텔레그램_시그널_알리미)로 정정. VM 에 A엔진이 형제 경로로 배치돼야 게이트 통과함을 DEPLOY.md 명시.

### Minor-21  `tests_toss.py 전반 + broker/toss.py:127-171`
- 토스 브로커 retry/타임아웃/network-error 경로가 게이트 테스트에서 미검증
- rec: tests_toss.py(또는 tests_hardening.py)에 max_retries>=1 로 429→재시도→200, 연속5xx→retry-exhausted, RequestException→network-error 추가. 주문 POST 재시도 시 동일 clientOrderId 재전송되는지 단언해 중복차단 행위검증.

### Minor-22  `live_select_canslim.py:1-18 + backtest_portfolio.py:37 / backtest_risk.py:3`
- 운영 신호(12-1 canslim+펀더)가 포트폴리오 백테스트(6-1 rs_momentum)로 검증 안 됨
- rec: 최소 12-1 모멘텀 게이트(펀더 제외)라도 백테스트/eval_factor IC 로 라이브 동일 산출식 robust 확인. 펀더 틸트 검증 불가를 운영 문서 명시·보수적 다이얼 유지.

### Minor-23  `data_cache/ (디스크 189 CSV) vs data.py:47,34-40 + run_live.py:120`
- data_cache 189개 전부 구포맷 orphan — 현행 로더가 안 읽고 일부는 영원히 미정리(죽은 캐시·중복)
- rec: data_cache/ 구포맷 CSV 일괄 정리(또는 _purge_legacy 를 start 무관 {ticker}_*_*.csv 패턴 확장). 종목당 단일 최장 히스토리로 통합. gitignore 되어 git 영향 없음.

### Minor-24  `tests_stage2.py:30-44 (test_h2_calendar)`
- 캘린더 테스트가 'DST 인지' 라벨만 달고 DST 전환 경계·조기폐장을 실제 검증 안 함(theater)
- rec: DST 전환 직후 평일(2026-03-09,2026-11-02) now_et().utcoffset()=-4h/-5h, last_completed_session 전환 처리, 조기폐장일 market_close=13:00 ET 단언 추가.

### Minor-25  `dashboard/server.py:52,54 (site_gate)`
- 사이트 패스를 URL 쿼리(?k=)로 전달 + 쿠키 secure 누락
- rec: 패스를 쿼리 대신 POST 폼/헤더로 1회 교환하거나 즉시 URL 에서 제거(리다이렉트). set_cookie 에 secure=True 추가.

### Minor-26  `dashboard/server.py:50,82-84 (/api/health)`
- health 가 site_gate 우회하며 control 활성 여부 노출
- rec: health 응답에서 control 노출 제거(단순 {'ok':true}) 또는 control 상태는 인증된 경로에서만 반환.

### Minor-27  `dashboard/build_data.py:59-68,781-788 + dashboard/server.py:77-79 (symbol_series→_longest_csv)`
- 미검증 ticker 가 파일 glob 에 직접 들어감(경로순회 표면)
- rec: tk 를 정규식(^[A-Z0-9.\-]{1,8}$) 화이트리스트 검증 후 처리. CACHE 절대경로 prefix 검사(os.path.realpath startswith) 추가.

### Minor-28  `broker/guardrail.py:402-414 (GuardedBroker.place_order) ↔ run_exit.py:115-117`
- 위험축소 SELL 이 fat-finger 명목캡에 걸려 청산 차단·영구정지 가능 (논리역전)
- rec: GuardedBroker.place_order 에서 req.side==Side.SELL 이면 check_order_notional 건너뛰거나 SELL 전용 캡 별도. 이미 BUY/SELL 구분(409-411)하므로 분기 추가는 surgical.

### Minor-29  `broker/toss.py:283,289 (place_order) ↔ panic_exit.py:54,75 / run_exit.py:115 / broker/managed.py:161,221-227`
- 1주 미만 분수 basis 의 청산 SELL 이 int() 절삭으로 quantity="0"·1주 미달 잔여·멱등키 불일치
- rec: place_order 진입부에서 int(req.qty)<=0 이면 즉시 REJECTED. 청산·패닉 plan 구성 시 qty 를 round()/floor 정수화하고 잔여 <1주는 청산불가로 로그, int() 무언절삭 의존 제거.

### Minor-30  `live_engine.py:230-236,268,274-275 + broker/guardrail.py:382-388`
- mark_traded 가 ok 경로에서만 호출 — partial/tripped/error/crash 후 재실행이 같은 신호로 재plan(diff 기반이라 대부분 자기교정)
- rec: partial 후에도 '일부 거래 시도함' 상태를 기록해 재시도 상한을 두거나, clientOrderId 를 qty 비포함(day|symbol|side)으로 좁혀 부분진행 재plan 도 중복접수로 흡수. 현 diff 기반으로 위험은 제한적이나 보호 사각.

### Minor-31  `dashboard/server.py:153-157 (/api/control/resume)`
- 킬스위치 reset 실패를 삼키고 halted:false 로 성공 응답(무성실패)
- rec: reset 예외 시 ok:False(또는 HTTP 5xx)·halted unknown 으로 응답해 대시보드가 실패를 빨갛게 표시. 최소한 warn 을 UI 에서 눈에 띄게 노출.

### Minor-32  `broker/executor.py:67,72, broker/guardrail.py:350, broker/paper.py:84 (전 금전 경로 float)`
- 전 금전 경로 float 사용 — Decimal 미사용으로 경계 회계 결정성 저하
- rec: 현금·금액 비교(현금부족, 명목캡, budget 차감)를 Decimal 또는 round(...,2) 정규화로 통일해 경계 결정성 확보. 우선순위 낮음.

### Minor-33  `run_exit.py:53,86 / panic_exit.py:110 (datetime.now().date())`
- 청산경로 end_excl·session 폴백이 KST 로컬 date — 진입경로의 ET 세션 통일과 불일치
- rec: run_exit.py:86 end_excl 을 보유한 session 에서 (session+timedelta(days=1))로 산출해 진입경로와 통일. session None 폴백은 panic 유지, run_exit 는 now_et().date() 로 폴백해 ET 기준 유지.

### Minor-34  `README.md:328-334 (청산 cron 예시) + run_exit.py:12 docstring`
- 문서 청산 cron 이 DST 미반영 고정 KST시각 — EST(겨울)엔 마감 전 ~1시간 청산 미커버
- rec: 청산 cron 예시를 두 시즌 커버하도록 KST 22:00~06:00('*/15 22-23,0-5 * * 1-5')로 넓히거나 UTC(NY 13:30~21:00 UTC)로 표기하고 DST 가 cron 자동조정 안 됨을 명시. README.md:329 'DST 는 market_open 게이트가 흡수' 문구가 타이밍 커버리지까지 보장하는 것으로 오독되지 않게 보완.

### Minor-35  `requirements_vm.txt (certifi 부재) vs requirements.txt:11 + data.py:10,15`
- certifi가 VM requirements에서 빠져 버전 미고정(공급망 드리프트)
- rec: requirements_vm.txt 에 certifi==2026.5.20 명시 추가해 PC 와 동일 버전 고정(전이의존 자동해결에 맡기지 말 것).

### Minor-36  `data_cache/ — 188개 구포맷 CSV ({ticker}_{start}_{end}.csv)`
- 구 캐시키 포맷 188개 OHLCV CSV가 현 load()에 안 읽히는 영구 orphan
- rec: data_cache/ 의 구포맷 파일 일괄 삭제(현 load() 가 재다운로드). 또는 _purge_legacy 를 시동 시 1회 전체 스윕하도록 보강. 실거래 영향 없어 우선순위 낮음.

### Minor-37  `state/killswitch.json (113B, mtime 2026-06-01)`
- 디스크 killswitch.json이 구 스키마·paper 스케일·2025 날짜 stale 잔재
- rec: 프로젝트폴더 state/killswitch.json 삭제(런타임은 LOCALAPPDATA 또는 명시 USTRADE_HOME 하위에서 재생성). go-live 전 USTRADE_HOME 설정 시 구파일 미동반 주의.
