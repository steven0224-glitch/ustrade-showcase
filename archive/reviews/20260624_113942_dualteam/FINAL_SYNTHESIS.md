# FINAL_SYNTHESIS — 미국주식 자동매매 시스템 전체 점검 (10축)

- **대상(identity)**: 미국주식 토스증권 자동매매 실거래 시스템 (단일 git repo, main HEAD)
- **작업**: 보안·동작정상성·쓰레기데이터 + 추가 7축(신뢰성·금전·시간·관측성·전략튜닝·테스트·환경드리프트) = 10축 교차검증
- **일시**: 2026-06-24 11:39 ~ 12:26 (47분, 27 에이전트, 3.3M 토큰, 611 tool calls)
- **팀**: A=레드팀(adversarial·高recall) / B=블루팀(charitable·高precision) · 모드 interleave

## §1 요약 카운트 (코드 결정론 병합)

| 분류 | 건수 |
|------|------|
| CONFIRMED Critical | 3 |
| CONFIRMED Major | 34 |
| CONFIRMED Minor | 37 |
| CONFIRMED Info | 27 |
| — 양팀 동시발견(both, 최고신뢰) | 0 |
| — 교차확인(cross-confirm) | 101 |
| LOWCONF | 0 |
| DISPUTED | 0 |
| NOTE | 0 |
| 기각(phantom) | 0 |
| 타이브레이커 가동 | 0 |

> ⚠ **both=0 주의**: 두 팀이 *같은 위치*를 동시에 짚은 항목은 0. 페르소나·focus 를 갈라 오류공간을 분담시킨 결과(skill §9.3 예측대로) co-discovery 앵커가 없다. 대신 101건 전부 상대팀 3-lens(재현·반증·대안) 패널이 교차확인했고 refute·기각·DISPUTED 가 0이다. 즉 신뢰도는 'cross-confirm'으로 균질하되, lens 패널이 한 건도 반증하지 않은 점은 lens 가 관대했을 가능성도 내포 — Critical/Major 는 아래 file:line 을 직접 열어 최종 확인 권장.

**레드팀·블루팀 교차검증에서 두 팀이 독립 1패스로 본 결과를 상호 보정한 끝에 총 74건이 CONFIRMED로 확정됐고(Critical 3 / Major 34 / Minor 37, 101건 교차확인), LOWCONF·DISPUTED·기각(phantom) 항목은 모두 0이라 신뢰도는 전건 cross-confirm 수준으로 균질하다. 가장 무거운 Critical 3건은 모두 토스 멱등키와 청산 경로에 집중되는데, broker/toss.py:281-283의 clientOrderId 날짜 성분이 KST 로컬 날짜라 미 세션이 KST 자정을 넘기면 동일 의도 주문이 다른 키로 중복 접수돼 oversell이 발생하고, 같은 키가 order_type·limit_price를 누락한 채 (symbol, side, int(qty))만 써서 의도가 다른 합법 주문을 충돌 차단하거나 중복 유발하며, run_exit.py:108-145의 청산 결과 처리는 전량 미체결·거부·부분 체결 시에도 알림 없이 status='ok'를 반환해 실패한 손절을 무성으로 은폐한다. Major 34건도 int(req.qty) 절삭으로 분수 SELL이 수량 0 주문이 되는 문제, 15분 주기 장중청산의 in-flight 추적 부재로 인한 재매도, 킬스위치 HALT가 보호용 청산 오버레이까지 함께 꺼버리는 결함 등 청산·멱등성 계열에 몰려 있어 실거래 투입 전 우선 차단이 필요하다. DISPUTED가 없어 별도 판단 보류 항목은 없다.**

## §2 상위 CONFIRMED (신뢰도 순)

| # | 위치 | severity | 요지 |
|---|------|----------|------|
| 1 | `broker/toss.py:281-283 (clientOrderId day = datetime.date.today())` | Critical | 멱등키 날짜 성분이 KST 로컬 날짜 — 미 세션이 KST 자정을 넘어 동일 의도 주문이 다른 키로 중복 접수(oversell) |
| 2 | `broker/toss.py:282-283 (sha1 키 구성 필드)` | Critical | 멱등키가 order_type·limit_price 누락 + (symbol,side,int(qty)) 만 사용 — 의도 다른 합법 주문 충돌 차단/중복 유발 |
| 3 | `run_exit.py:108-145 (_run_locked 청산 체결 결과 처리)` | Critical | 청산 트리거 후 전량 미체결/거부/부분 시 알림 없이 status='ok' 반환 — 실패한 손절이 무성으로 은폐, 손실 포지션 무방비 방치 |
| 4 | `broker/toss.py:283,289 (int(req.qty) 절삭)` | Major | int(req.qty) 절삭으로 분수 basis(<1주) SELL 이 quantity '0' 주문으로 토스 전송 — 좀비 포지션·가드 오트립 |
| 5 | `run_exit.py:41-145 (15분 주기, already_traded/주문 dedupe 부재)` | Major | 장중청산이 15분마다 재실행되나 in-flight 추적 없어 미체결 폴링 실패 시 재매도(oversell) |
| 6 | `run_exit.py:72-76 (is_halted 조기반환, resume_if_new_day 미호출) + namespace 'toss' 공유` | Major | 어떤 사유의 킬스위치 HALT 든 보호용 장중 청산 오버레이를 함께 비활성화 — 가장 필요한 순간 자동 손절이 꺼짐 |
| 7 | `run_exit.py:116-120 (청산 중 tripped/error 반환 경로)` | Major | 청산 중 가드레일 트립/예외 시 알림 없이 dict 만 반환 — 리스크관리 청산 실패가 사일런트 |
| 8 | `run_exit.py:46-48 (TOSS 자격증명 미설정 분기)` | Major | 청산 루프가 토스 키 결측 시 알림 없이 error 반환 — 슬리브 분기와 비대칭, 손절 보호 무성 중단 |
| 9 | `heartbeat.py:23-55 (_traded_sessions/check, runs.jsonl 만 감시)` | Major | dead-man-switch 가 일일진입(runs.jsonl)만 감시 — 장중 청산 cron 사망은 미탐지 |
| 10 | `notify.py:22-44 (_telegram/_slack)` | Major | 알림 전송이 HTTP 응답·ok 플래그 미검사 — 전달 실패를 성공으로 오인(마지막 통보 채널의 무성실패) |
| 11 | `panic_exit.py:72-99 (place_error 누락 + status 'ok' + 알림이 plan 대비 미청산 미표기)` | Major | 비상 전량청산이 일부 종목 미청산에도 'ok' 보고 — 잔존 노출 미표면화 |
| 12 | `run_exit.py:122-137 (_await_fills 후 부분/미체결 청산주문 처리)` | Major | 청산 주문 부분체결·취소실패가 알림·정합성검증 없이 흘러감 — 잔존수량·oversell 위험 무성 |

## §3 DISPUTED / LOWCONF / 기각 / NOTE

전부 **0건**. 타이브레이커 불필요, 사용자 판단 보류 항목 없음. (모든 발견이 cross-confirm 또는 severity별 CONFIRMED)

## §4 전체 CONFIRMED 상세 (severity 순)

### Critical (3)

#### Critical-1 · `broker/toss.py:281-283 (clientOrderId day = datetime.date.today())` _(team 레드)_
- **요지**: 멱등키 날짜 성분이 KST 로컬 날짜 — 미 세션이 KST 자정을 넘어 동일 의도 주문이 다른 키로 중복 접수(oversell)
- **상세**: place_order 의 clientOrderId 는 day=datetime.date.today().isoformat()(호스트 KST 날짜)로 만든다(281). 그러나 엔진 전체의 거래일 기준은 calendar_util.last_completed_session() 의 ET/NYSE 세션 날짜다(run_live.py:99, run_exit.py:52). 미 정규장은 KST 자정을 가로지르므로(EDT 22:30~05:00, EST 23:30~06:00) 같은 NYSE 세션에서 KST 23시대 제출 주문과 KST 00~02시대 재실행/재시도 주문은 day 가 달라 clientOrderId 가 달라진다 → 토스 멱등 차단 무력화 → 동일 포지션 이중 매도/매수. 청산 cron(*/15 23,0-4)이 자정을 넘나들고 run_exit 은 already_traded 검사 없이 재실행되므로 자정 직후 동일 SELL 이 새 키로 재접수될 위험이 직접적이다. run_exit cancel(except: pass) 실패까지 겹치면 잔존 DAY 주문 + 새 키 재제출로 중복 성립.
- **권고**: clientOrderId 의 날짜 성분을 datetime.date.today() 대신 엔진의 세션일(calendar_util.last_completed_session(), ET 세션 날짜)로 주입. place_order/OrderRequest 시그니처에 session_date 를 전달해 자정 횡단에도 동일 키가 나오게 한다.
- **근거**: broker/toss.py:281-283; calendar_util.py:37 last_completed_session(ET); run_live.py:99-103 / run_exit.py:52-53(today=세션일); toss.py:19-20 주석 'KST 22:30~05:00'; README.md:334 청산 cron 자정 횡단; run_exit.py:124-131 cancel except: pass

#### Critical-2 · `broker/toss.py:282-283 (sha1 키 구성 필드)` _(team 레드)_
- **요지**: 멱등키가 order_type·limit_price 누락 + (symbol,side,int(qty)) 만 사용 — 의도 다른 합법 주문 충돌 차단/중복 유발
- **상세**: clientOrderId = sha1(f'{day}|{symbol}|{side}|{int(qty)}')(282-283). orderType·limit_price·실행 컨텍스트(run_live 진입 vs run_exit 손절 vs panic)가 키에 안 들어간다. 결과: (1) 같은 세션일에 의미가 다른 두 주문이 symbol·side·정수수량만 우연히 같으면 키 충돌로 둘째가 토스에서 중복 거부 — 예: run_live 진입 SELL 후 같은 날 run_exit 손절 SELL 이 같은 수량이면 합법 청산이 조용히 거부돼 포지션 미청산. (2) MARKET vs LIMIT, 가격만 다른 정정성 주문도 같은 키. int(qty) 절삭으로 수량 식별도 거칠다. 단일 멱등 방어선이 이 키 하나뿐인데 구성이 취약.
- **권고**: 키에 order_type, limit_price(LIMIT 시), 실행 출처/논리적 시퀀스 식별자를 포함. plan 단계에서 결정론적 intent_id 를 발급해 OrderRequest 에 실어 키로 사용. 최소한 orderType 을 키 문자열에 추가.
- **근거**: broker/toss.py:282-283 sha1(f'{day}|{req.symbol}|{req.side.value}|{int(req.qty)}'); run_exit.py:115 와 live_engine.py:228-229(Executor.plan)가 동일 symbol/side/qty SELL 을 같은 날 낼 수 있음

#### Critical-3 · `run_exit.py:108-145 (_run_locked 청산 체결 결과 처리)` _(team 레드)_
- **요지**: 청산 트리거 후 전량 미체결/거부/부분 시 알림 없이 status='ok' 반환 — 실패한 손절이 무성으로 은폐, 손실 포지션 무방비 방치
- **상세**: 200MA이탈/손절 트리거로 SELL 제출 후 _await_fills(30s), 잔존 미체결 cancel. line 139 filled=[FILLED만], line 140 notify 는 filled 비어있지 않을 때만. 모든 청산 SELL 이 REJECTED/타임아웃/미체결이면 filled=[] → 알림 전무, line 144 는 무조건 {status:'ok', exits:[...]} 반환. 즉 '반드시 팔았어야 할 손실 포지션이 안 팔렸는데' 아무 알림 없이 정상(ok)으로 저널 기록. '다음 cron 재시도'는 트리거 유지·장중에만 동작하고 매 run 잔존 DAY 주문을 무조건 cancel 하므로 매번 처음부터 재체결 필요, 반복 거부 시 에스컬레이션 없음.
- **권고**: exits 의도 qty vs 실제 filled qty 를 종목별 대조해 잔존>0 이면(또는 len(filled)<len(exits)) notify(level='error','청산 미체결 — 즉시 수동확인') 발송, status 를 'exit_partial'/'exit_failed' 로 강등. run_live._alert 처럼 미체결을 명시 경보화.
- **근거**: run_exit.py:139 filled=[o for o in orders if o.status==OrderStatus.FILLED]; line 140 if filled: notify; line 144 무조건 return status:'ok'. live_exit.to_exit 는 트리거만 판정

### Major (34)

#### Major-1 · `broker/toss.py:283,289 (int(req.qty) 절삭)` _(team 레드)_
- **요지**: int(req.qty) 절삭으로 분수 basis(<1주) SELL 이 quantity '0' 주문으로 토스 전송 — 좀비 포지션·가드 오트립
- **상세**: place_order 가 int(req.qty) 로 절삭(키 283, 바디 289 quantity:str(int(req.qty))). Executor.plan 은 0 을 안 내지만, run_exit.py:115·panic_exit.py:75 는 ManagedBroker.get_positions() p.qty 를 그대로 SELL 수량으로 쓴다. managed basis 는 float 이고 _prune 은 1e-9 이하만 제거(managed.py:259-263) → 부분체결/분수 basis 0.4 가 살아남으면 int(0.4)=0 → quantity:'0' 전송. 0수량 주문 처리 미정의(거부/에러/무한 미체결). _await_fills 미종결 시 30초 낭비 후 partial → 에러 누적 → 가드 오트립 가능. 분수 잔량이 영구 미청산 좀비 포지션화.
- **권고**: place_order 진입에서 int(req.qty)<=0 이면 즉시 REJECTED 반환(전송 금지). run_exit/panic SELL 수량을 int() 내림하고 0 이면 스킵. 분수 basis 는 별도 수동확인 알림.
- **근거**: broker/toss.py:283,289; broker/managed.py:161 qty=min(p.qty,basis)(float), 259-263 _prune; run_exit.py:115, panic_exit.py:75

#### Major-2 · `run_exit.py:41-145 (15분 주기, already_traded/주문 dedupe 부재)` _(team 레드)_
- **요지**: 장중청산이 15분마다 재실행되나 in-flight 추적 없어 미체결 폴링 실패 시 재매도(oversell)
- **상세**: run_exit.run 은 cron 15분 주기 호출인데 KillSwitch.already_traded() 검사가 전혀 없다(run_live.py:142 와 대비). 매 회 reconcile_basis→get_positions→check_exits→SELL 흐름을 새로 돈다. 한 회차에서 SELL 제출했으나 _await_fills 30초 내 미종결 + cancel 실패(False)면 basis 미차감(record_fills 는 filled_qty>0 만, managed.py:226-228) 포지션 그대로 → 다음 15분 회차가 같은 종목 재SELL. 같은 로컬일이면 toss 멱등키가 막지만 그 멱등 자체가 위 Critical(자정횡단·키충돌)로 신뢰 불가 → 중복 매도 체결.
- **권고**: run_exit 에 '직전 N분 내 동일 심볼 SELL 제출' in-flight 추적(저널/state) 추가, 미체결 주문 잔존 시 그 종목 재SELL 스킵. clientOrderId 결함 수정과 병행.
- **근거**: run_exit.py:12(cron 15분), 78-131(already_traded 부재, cancel 실패 시 status 유지), broker/managed.py:226-228

#### Major-3 · `run_exit.py:72-76 (is_halted 조기반환, resume_if_new_day 미호출) + namespace 'toss' 공유` _(team 레드)_
- **요지**: 어떤 사유의 킬스위치 HALT 든 보호용 장중 청산 오버레이를 함께 비활성화 — 가장 필요한 순간 자동 손절이 꺼짐
- **상세**: run_exit 는 진입(run_live)과 동일 namespace='toss' 킬스위치 공유(72). is_halted()=True 면 청산 시도 없이 'halted' 조기반환(75-76). live_engine 과 달리 resume_if_new_day()/roll_day() 를 호출하지 않으므로 daily_loss·에러윈도우·total_drawdown·상태손상 등 어떤 HALT 든 보호용 장중 손절/추세이탈 청산이 함께 꺼진다 — 손실이 커져 daily_loss 트립난 바로 그 순간 자동 손절이 비활성. SELL 루프 transient(5xx/네트워크/401) 오류가 record_error(119)로 에러윈도우 초과 시 HaltError→'crash'→킬스위치 트립 → 이후 run_exit 영구 'halted'. 유일 탈출구는 수동 panic_exit(전량청산)뿐.
- **권고**: 장중 청산은 위험 축소 방향이므로 daily_loss/error-window HALT 에서도 동작하게 분리(panic 의 GuardedBroker 우회처럼)하거나, 최소 run_exit 시작에 resume_if_new_day() 호출. exit 경로 record_error 트립이 청산 자체를 막지 않게 게이트 검토.
- **근거**: run_exit.py:72-76, 119 record_error; guardrail.py:217-220 is_halted, 273-283 resume_if_new_day(live_engine.py:138 에서만 호출)

#### Major-4 · `run_exit.py:116-120 (청산 중 tripped/error 반환 경로)` _(team 레드)_
- **요지**: 청산 중 가드레일 트립/예외 시 알림 없이 dict 만 반환 — 리스크관리 청산 실패가 사일런트
- **상세**: line 116-117 청산 SELL 중 HaltError→{status:'tripped'} 반환 notify 없음; 118-120 그 외 예외→record_error 후 {status:'error'} notify 없음. run_live.py 는 _alert()가 tripped/error 를 'halt'/'error' 알림으로 변환하나 run_exit.py 엔 그런 디스패처 부재(main 에서 종료코드만 매핑). record_error 3회 누적 killswitch trip 도 여기서 알림 없음. exits.jsonl 저널만 남아 능동 확인 안 하면 모름.
- **권고**: run_exit 의 tripped/error 반환 직전 notify(reason,'halt'/'error',ts) 추가. run_live._alert 와 동등 알림 커버리지 부여.
- **근거**: run_exit.py:117 return status:tripped / :120 return status:error — 직전/직후 notify 부재; grep 상 run_exit notify 는 거부/크래시/halt/데이터부족/체결성공에만 존재

#### Major-5 · `run_exit.py:46-48 (TOSS 자격증명 미설정 분기)` _(team 레드)_
- **요지**: 청산 루프가 토스 키 결측 시 알림 없이 error 반환 — 슬리브 분기와 비대칭, 손절 보호 무성 중단
- **상세**: line 47-48 if not (toss.api_key and toss.api_secret): return {status:'error', reason:'TOSS 미설정'} — notify 없음. 바로 아래 슬리브 미설정(49-51)은 notify 호출하는데 키 결측 경로만 조용. VM env 유실/오설정(재부팅·드리프트) 시 장중 손절 cron 이 매번 조용히 죽고 main()은 종료코드 2만 남겨 cron 로그 능동 확인 없이는 모름.
- **권고**: line 48 return 전 notify('청산 거부 — TOSS 자격증명 미설정','error',ts) 추가(슬리브 분기와 동일). 또는 heartbeat 가 exits.jsonl 감시.
- **근거**: run_exit.py:47-48 키 결측 분기 notify 부재 vs 50 슬리브 결측 분기 notify 존재 — 비대칭

#### Major-6 · `heartbeat.py:23-55 (_traded_sessions/check, runs.jsonl 만 감시)` _(team 레드)_
- **요지**: dead-man-switch 가 일일진입(runs.jsonl)만 감시 — 장중 청산 cron 사망은 미탐지
- **상세**: heartbeat.check()는 _traded_sessions()로 logs/runs.jsonl(run_live 데일리 리밸런스) session 기록만 본다(24). 장중 손절 청산은 별도 cron(run_exit.py, exits.jsonl)·별도 주기(*/15분)인데 heartbeat 가 이를 전혀 안 본다. run_exit cron 만 DST/리부트/크래시로 죽으면 데일리 진입이 정상이라 heartbeat 가 'OK' 보고하는 동안 보호용 손절 기계가 조용히 죽어있을 수 있다. archive/reviews/20260622_best_practices_gap.md:32 가 동일 갭 미해결 명시.
- **권고**: heartbeat 에 정규장 시간대 동안 exits.jsonl(또는 panics.jsonl) 최근 실행 timestamp 가 N분 이내인지 점검하는 별도 체크 추가. 장중 청산 cron dead-man-switch 를 독립 배치.
- **근거**: heartbeat.py:24 f=LOG_DIR/'runs.jsonl' — exits.jsonl 미참조; archive/reviews/20260622_best_practices_gap.md:32 동일 갭

#### Major-7 · `notify.py:22-44 (_telegram/_slack)` _(team 레드)_
- **요지**: 알림 전송이 HTTP 응답·ok 플래그 미검사 — 전달 실패를 성공으로 오인(마지막 통보 채널의 무성실패)
- **상세**: _telegram 은 requests.post 가 예외만 안 던지면 return True(30). 텔레그램 API 는 잘못된 chat_id·봇 차단·토큰 폐기 시 HTTP 200+{ok:false}/4xx 를 반환하는데 resp.status_code·resp.json()['ok'] 를 전혀 검사 안 함. '봇 차단/chat_id 오타/토큰 만료'로 실제 미전송돼도 sent=['tg'] 기록, 로그엔 '→ tg'. 무인 실거래의 마지막 통보 채널(킬스위치·청산·크래시 알림)이 조용히 죽어도 시스템은 '보냄'으로 착각.
- **권고**: _telegram 에서 resp 상태와 resp.json().get('ok') 검사 후 False 시 _log.error 로 채널실패를 로컬로그에 명시. _slack 도 status_code 2xx 검사. 최소 전송실패를 ustrade.log ERROR 로 남길 것.
- **근거**: notify.py:28-30 requests.post(...); return True — 응답 미검사; 41-42 _slack 동일

#### Major-8 · `panic_exit.py:72-99 (place_error 누락 + status 'ok' + 알림이 plan 대비 미청산 미표기)` _(team 레드)_
- **요지**: 비상 전량청산이 일부 종목 미청산에도 'ok' 보고 — 잔존 노출 미표면화
- **상세**: 종목별 place_order 예외 시 _journal('place_error')만 하고 그 종목은 orders/filled 에서 통째 누락(76-77). REJECTED·PARTIAL·SUBMITTED-후-cancel 도 filled 에 없다. 그런데도 status:'ok' 반환(97), run() 알림은 filled 또는 '(체결없음)'만(135) — plan(청산대상) 대비 어떤 종목이 청산 실패해 잔존인지 비교·명시 안 함. 비상에서 절반만 비워졌는데 'ok' 면 운영자가 직접 plan vs filled 대조해야 잔존 노출 인지. ks.trip 은 신규진입만 막고 기존 노출은 그대로.
- **권고**: panic 결과에 plan 대비 미청산 failed=[(sym,qty_remaining)] 계산·반환, failed 비어있지 않으면 'ok' 대신 'partial' 또는 error-레벨 알림으로 '청산실패/잔존: SYM N주 — 토스앱 수동매도' 발송.
- **근거**: panic_exit.py:73-77(place_error 는 journal 만), 96-99(filled 만, status 항상 ok), 133-135(notify 가 filled/체결없음만)

#### Major-9 · `run_exit.py:122-137 (_await_fills 후 부분/미체결 청산주문 처리)` _(team 레드)_
- **요지**: 청산 주문 부분체결·취소실패가 알림·정합성검증 없이 흘러감 — 잔존수량·oversell 위험 무성
- **상세**: 청산 경로는 run_live(_run_once_locked)와 달리 _reconcile(사후 정합성)·partial 강등 로직이 없다. _await_fills 후 미체결 best-effort 취소(126-131, 실패 시 except pass), record_fills(132-137, 실패 시 except pass)만. 부분체결(100주 중 30주만)이면 filled 에 들어가 '[청산]' 알림은 가지만 '70주 미청산 잔존'은 어디에도 경보 안 됨. cancel 실패한 DAY 주문이 장중 늦게 체결돼 oversell 되는 위험도 조용함.
- **권고**: 청산 주문별 filled_qty 와 요청 qty 대조해 잔존수량 산출, 잔존>0 이면 notify(error). cancel_order False/예외 카운트해 1건↑ 시 경보.
- **근거**: run_exit.py:128-131 취소실패 except pass; 134-137 record_fills 실패 except pass; live_engine.py:261 bad/partial 검증 상당 로직 부재

#### Major-10 · `run_exit.py:68-71 / panic_exit.py:113-114 (toss.market_open 예외 → 전체 청산 abort)` _(team 레드)_
- **요지**: 일시적 market-calendar API 실패가 청산 run 전체를 crash 시킴 — fallback 없음
- **상세**: run_exit 는 _run_locked 시작에 toss.market_open('US') 호출(70)=/market-calendar REST. 타임아웃/5xx/네트워크 시 _request 가 max_retries 소진 후 TossAPIError raise, 이는 market_open 내부(try/except 는 ValueError/TypeError 만) 미포착 → _run_locked→run() except Exception(59)→status:'crash'. 즉 캘린더 조회 1회 일시 실패가, 실제론 장이 열려 청산 가능한 시점인데도 손절 청산 전체를 무산. panic_exit 도 run()에서 락 안 market_open 호출(114)로 동일 crash.
- **권고**: 청산 게이트에서 캘린더 조회 실패를 별도로 잡아 '캘린더 불확실 — force_open/재시도' 경고를 보내되 청산 시도 자체를 막지 않거나, 최소 'closed' 아닌 별도 status 로 분리해 crash 누적/halt 트립과 구분.
- **근거**: run_exit.py:70; toss.py:248-260 market_open(try/except 는 ValueError/TypeError 만); run_exit.py:59-62 except→crash; panic_exit.py:111-127 동일

#### Major-11 · `broker/toss.py:255-260 (market_open naive vs aware datetime 비교)` _(team 레드)_
- **요지**: market_open 의 naive vs aware 비교 TypeError 삼킴 → 항상 휴장 판정 위험(장중청산 silent 차단)
- **상세**: market_open 은 토스 startTime/endTime 을 datetime.fromisoformat 으로 파싱(255), now=datetime.now().astimezone()(tz-aware,257)와 start<=now<=end 비교. API 가 tz 오프셋 없는 naive ISO 면 aware-vs-naive 비교 TypeError → except(ValueError,TypeError)(259)가 삼켜 False(휴장) 반환. 그러면 정규장이 실제 열려있어도 run_exit.py:70·panic_exit.py:114 게이트가 'closed' 로 판단해 청산·비상매도가 영영 미실행되는 silent 고장. 토스 API 스펙 미확정(키 발급 후 구현)이라 tz 포함 여부 불확실 → 위험 큼.
- **권고**: startTime/endTime 파싱 후 tzinfo None 이면 명시적 TZ(토스 문서상 KST 또는 ET)로 localize 후 동일 TZ 정규화 비교. 파싱 실패/형식이상은 False 로 삼키지 말고 notify 로 표면화해 silent 차단 가시화.
- **근거**: broker/toss.py:255-260; 비교 대상 run_exit.py:70/panic_exit.py:114; toss.py:273 표기 미확정 주석

#### Major-12 · `live_engine.py:220-239 (sells+buys 단일 루프) + broker/executor.py:54-73 (proceeds 선반영)` _(team 레드)_
- **요지**: 매도 미체결 상태에서 매도예상대금을 예산에 넣고 매수 동시 제출 — 미정산현금 과매수 위험
- **상세**: Executor.plan 은 budget=acct.cash+proceeds(executor.py:63), proceeds 는 아직 체결 안 된 매도의 '예상' 순현금(60). _run_once_locked 는 sells+buys 를 한 루프에서 전부 place_order(228-229) 후에야 _await_fills 폴링(239). PaperBroker 는 즉시체결이라 무해하나 TossBroker 는 시장가 비동기 체결(305 SUBMITTED 반환) → 매도 PENDING 인데 매도대금 가정 매수가 동시 제출. 토스 계좌가 미정산현금/증거금 매수 허용 시 가용현금 초과 과매수 성립.
- **권고**: 매도 먼저 제출→_await_fills 종결 확인→get_account 재조회 실제 가용현금으로 매수예산 재계산 후 매수 제출(2단계 분리)하거나 매수예산을 proceeds 제외(현금만)로 보수화. 토스 미정산현금 매수 허용 여부 실증 후 결정.
- **근거**: executor.py:60,63,73; live_engine.py:228-229,239; toss.py:305-306

#### Major-13 · `broker/managed.py:175-184 (get_account) + broker/executor.py:28 (investable=equity*alloc)` _(team 레드)_
- **요지**: cash_cap 이 현금만 capping, managed 평가액은 무제한 → equity 과대 → 사이징 과대·노출 점증
- **상세**: get_account 은 cash 만 min(real.cash,cash_cap)(177), managed_val 은 보유수량×현재가 전액 합산(179-183). equity=capped_cash+무제한 managed_val. Executor 는 investable=equity*alloc(28), tgt_qty=int(w*investable/price)(44). cash_cap=$100 으로 슬리브 현금 제한해도 과거 누적 managed 평가액 $900 이면 equity=$1000, investable=$950 으로 사이징돼 목표수량이 의도(소액)보다 훨씬 큼. 단발 매수는 budget=cash+proceeds 캡(63-68)이 최종 방어선이라 현금초과 체결은 막지만, 리밸런스 회전 누적으로 managed 평가액이 불수록 cash_cap 이 운용규모 상한 기능을 못 해 노출 점증.
- **권고**: investable 산정 시 equity 대신 min(equity, cash_cap+managed_basis_at_cost) 또는 cash_cap 기반 운용규모 상한 명시 적용. 최소 managed_val 도 cash_cap 비례 캡 또는 cash_cap 을 '슬리브 총 equity 상한'으로 재정의해 사이징 입력에 반영.
- **근거**: managed.py:177,179-184; executor.py:28,44

#### Major-14 · `broker/managed.py:175-184 (get_account quote 실패 폴백)` _(team 레드)_
- **요지**: managed 평가액 산정 시 quote 실패를 avg_price 로 조용히 폴백 — 손실가드 baseline 왜곡(silent 우회)
- **상세**: ManagedBroker.get_account 은 managed 종목 평가액을 라이브 quote 로 계산하되 예외 시 p.avg_price 폴백(179-183, except: managed_val += p.qty*p.avg_price). 이 equity 가 KillSwitch.roll_day/check_daily_loss/check_total_drawdown 입력(live_engine.py:222-225)이 되는데, 시세 폭락 중 quote 실패(레이트리밋/타임아웃) 시 평가액이 매입가로 고정돼 실제보다 높게 잡힘 → 일일손실/드로다운 가드가 실제 손실을 과소평가해 트립 안 되는 우회. 무인서 가드가 조용히 무력화되는 silent 경로.
- **권고**: quote 실패 시 avg_price 폴백 대신 해당 종목을 baseline 산정에서 제외하거나 가드를 보수적(equity 하향) 처리, quote 실패를 notify/journal 가시화. 다수 종목 quote 실패 시 거래 보류.
- **근거**: broker/managed.py:179-183; live_engine.py:222-225

#### Major-15 · `broker/managed.py:189-200 (place_order pending 영속 후 inner 예외 unguarded)` _(team 레드)_
- **요지**: ManagedBroker.place_order 가 toss 거부를 catch 안 해 pending 영속 후 예외 전파 — 멱등 결함과 결합 시 중복 위험
- **상세**: BUY 경로에서 198-199 가 제출 前 self._pending 에 수량 더하고 _save() 후 200 에서 inner place_order. inner(TossBroker)가 5xx/네트워크/401 시 TossAPIError raise(toss.py:304)하면 ManagedBroker 미포착 → live_engine try/except(230-236)가 받지만, pending 은 이미 영속됐는데 주문이 토스 도달했는지 불명(전송 후 응답 분실 가능). reconcile_basis(234-257)가 다음 실행 흡수해 유령은 안 생기나, 같은 주문을 다음 실행이 재플랜할 때 위 멱등 결함(Critical 1/2)과 결합하면 중복 매수 위험.
- **권고**: place_order 가 inner 예외 시 방금 더한 pending 롤백/유지 정책 명시. in-flight 주문ID 를 pending 과 함께 영속해 재플랜 시 중복 방지. 멱등키 결함 우선 수정.
- **근거**: broker/managed.py:195-200; broker/toss.py:299-304; live_engine.py:228-236

#### Major-16 · `broker/toss.py:203-212 (get_account 환율/통화 정합)` _(team 레드)_
- **요지**: 원화계좌 자동환전 시 USD buying-power 0/불안정 또는 KRW 혼입 → 사이징·가드 동시 오작동(~1300배 과대 위험)
- **상세**: get_account 은 cashBuyingPower 를 그대로 cash 로(205-207), holdings marketValue.amount['usd'] 를 평가액으로(209-211) 쓴다. 환산 로직 전무, 토스가 USD 필드를 정확히 채운다는 가정에 100% 의존. docstring(16-18)이 'KRW 자동환전 방식이면 USD 매수가능금액 0/불안정' 경고. 그 경우 cash=0/요동 → Executor.investable/budget 0/왜곡, KillSwitch roll_day/check_daily_loss 가 스케일 급변 오작동(guardrail.py:258 재seed 발동/오트립). cashBuyingPower 가 KRW 금액을 USD 필드에 잘못 담으면 사이징이 ~1300배 과대평가 → 대량 과매수.
- **권고**: 응답 currency 필드 검증(USD 아닌데 USD 요청이면 fail-closed), cashBuyingPower 비정상(0/직전대비 환율급변폭) 시 거래 거부. 펀딩 방식(환전 시점) toss_check 실증. USD 금액↔USD equity 만 비교됨을 단위테스트 고정.
- **근거**: toss.py:8,16-18,205-207,209-211; guardrail.py:258

#### Major-17 · `README.md:334 (cron */15 23,0-4) + run_exit.py:12 + broker/toss.py:19 docstring` _(team 레드)_
- **요지**: 청산 cron 윈도가 한 DST 체제만 커버 — 겨울(EST) 마지막 1시간 청산 사각
- **상세**: 장중 청산 cron 이 KST 23:00~04:59(23,0-4)에만 발사. 미 정규장 KST 환산은 DST 따라 달라짐: EDT 22:30~05:00, EST 23:30~06:00. EST(겨울 ~11~3월) 동안 실제 세션은 06:00 KST 까지인데 cron 은 04:59 까지만 → 05:00~06:00 KST(마지막 1시간) 200MA이탈·손절 점검이 아예 안 돔. market_open 게이트는 잘못된 시각 주문은 막지만 존재하지 않는 cron tick 은 못 만들어 이 사각을 흡수 못 함. 겨울철 매일 마감 직전 1시간 보유분이 보호 청산 없이 노출.
- **권고**: 청산 cron 윈도를 두 DST 합집합(KST 22:00~06:00, */15 22,23,0-5)으로 넓히거나 VM TZ 를 America/New_York 으로 두고 ET 09:30~16:00 발사. market_open 게이트가 비개장 tick 을 무해 skip 하므로 윈도 확장 부작용 없음.
- **근거**: README.md:328-336, run_exit.py:12, broker/toss.py:19; KST/ET 오프셋 EDT 13h·EST 14h 검산

#### Major-18 · `tools/run_tests.py:16-29 (SUITES) + tests_stage1.py:138-213, tests_stage2.py:79-100` _(team 레드)_
- **요지**: 배포 게이트가 킬스위치 차단·멱등성·NaN경계 테스트를 제외 — 핵심 안전 불변식 미검증
- **상세**: 배포 게이트(deploy_push.ps1→run_tests.py) SUITES=[tests_managed,toss,exit,hardening,panic,review,canslim] 만 실행, stage1~8 은 '백테스트/리서치 무관'(13-14 주석)으로 제외. 그러나 '트립 킬스위치가 GuardedBroker.place_order 를 HaltError 로 차단하는가'(tests_stage1.py:161-203 test_c4)·'같은 날 재실행 시 already_ran 으로 중복매매 차단'(138-156 test_c2)·NaN 시세 경계(tests_stage2.py:91-100)가 전부 stage1/2 에만 존재. grep: 게이트 7스위트 중 GuardedBroker 행위단언 0건(tests_panic.py:5 는 '가드 우회' 반대맥락 주석). 게이트 통과 'ALL PASS' 가 떠도 최상위 안전 불변식(정지=주문불가·재실행=노옵)이 회귀 검증 안 됨.
- **권고**: test_c2_idempotency·test_c4_guarded_broker·NaN/0 경계를 SUITES 에 추가하거나 tests_hardening.py 로 이관. stage 전체 vectorbt 의존 전제는 stage1/2 가드 테스트엔 미해당(broker 만 의존).
- **근거**: tools/run_tests.py:13-29; tests_stage1.py:138-156,161-203; tests_stage2.py:79-100; grep 게이트 GuardedBroker 행위단언 0건

#### Major-19 · `tests_panic.py 전체 + tests_managed.py:47-53 FakeBroker` _(team 레드)_
- **요지**: Exit/panic 통합 테스트가 happy path 만 커버 — 부분체결/거부/place-error 미검증(test theater)
- **상세**: tests_panic.py 전 시나리오·_panic 오케스트레이션이 FakeBroker 사용, place_order 가 항상 FILLED·filled_qty=req.qty(49), get_order 도 항상 FILLED(53). 핵심 복원력 케이스 — 청산주문 거부, 부분체결, place_order 예외, _await_fills 타임아웃 후 잔존 — 가 _panic/_run_locked 경로로 한 번도 미테스트. tests_exit.py 도 check_exits(트리거 판정)만 단위검증. 게이트 통과해도 청산 실패모드 안전망이 실재하지 않음.
- **권고**: REJECTED/PARTIAL/place_order raise/타임아웃-미체결 시뮬 FakeBroker 변형 추가해 run_exit._run_locked·panic_exit._panic 이 (a)미청산 잔존을 알림 surface (b)basis 가 실제 체결분만 차감 검증하는 통합 테스트 추가.
- **근거**: tests_managed.py:47-53; tests_panic.py:39-117; tests_exit.py:31-96

#### Major-20 · `requirements_vm.txt vs requirements.txt (certifi 부재) + data.py:10,25` _(team 레드)_
- **요지**: data.py top-level import certifi 가 VM requirements 에 미고정 — cert 검증 동작 드리프트
- **상세**: data.py:10 이 top-level import certifi, certifi.where()로 한글경로 CA 우회(15-23). data.py 는 run_live.py:19 가 import → 실거래 진입 import 체인 포함. certifi 는 requirements.txt:12 엔 핀(2026.5.20)이나 requirements_vm.txt 엔 없다. requests 전이의존으로 설치는 되나 버전 미고정 → VM certifi 가 PC 와 달라질 수 있고 비ASCII경로 우회가 cacert.pem 경로 형태에 의존하므로 cross-env CA 검증 동작이 갈릴 위험. requirements 헤더가 재현성 표방하므로 VM 핀 누락은 정책 위반.
- **권고**: requirements_vm.txt 에 certifi==2026.5.20 동일 버전 핀 추가.
- **근거**: requirements.txt:12; requirements_vm.txt(certifi 없음); data.py:10,15-23,25; run_live.py:19

#### Major-21 · `requirements_vm.txt vs requirements.txt (schedule 부재) + scheduler.py:32,4` _(team 레드)_
- **요지**: scheduler.py 가 쓰는 schedule 패키지가 VM requirements 에 없음 — 데몬 모드 시 무성 즉시종료
- **상세**: scheduler.py(상시 루프 대안, run_live.run() 데몬 발사)는 32 에서 import schedule, 36 schedule.every().day.at(). schedule==1.2.2 는 requirements.txt:11 엔 있으나 requirements_vm.txt 엔 없다. VM 이 cron/Task Scheduler 원샷만 쓰면 무해하나, vm_update.ps1 은 requirements_vm.txt 만 설치(57)하므로 VM 에서 scheduler.py 데몬 기동 순간 ImportError. scheduler.py:33-34 가 graceful(미설치→안내 후 return)이라 크래시는 아니나 스케줄러가 조용히 즉시 종료해 봇이 영영 안 도는 무성실패.
- **권고**: VM 이 원샷만 쓰면 schedule 제외를 README/DEPLOY.md·scheduler.py 상단 주석에 명시. VM 에서 scheduler 쓸 의도면 requirements_vm.txt 에 schedule==1.2.2 추가.
- **근거**: requirements.txt:11; requirements_vm.txt(schedule 없음); scheduler.py:4,32-37; vm_update.ps1:57

#### Major-22 · `live_engine.py:32,183 + live_select_canslim.py:44` _(team 레드)_
- **요지**: 운영 canslim 이 설계값 pool=12 아닌 pool=8 로 구동 — 펀더 검증 후보풀 1/3 축소·선정 왜곡
- **상세**: RunConfig.pool 기본값 8(live_engine.py:32, momentum 용)인데 run_once 가 cfg.pool 을 그대로 canslim select 에 전달(183). live_select_canslim.select 설계 default 는 pool=12(44)이고 docstring(12)도 12 전제. run_live.py 는 RunConfig(strategy='canslim')만 만들고 pool 미지정 → 운영 canslim 펀더 검증 후보풀이 12→8 로 작게 돔. 모멘텀 통과 후보가 줄어 CANSLIM/애널 교차검증 랭킹 틸트 모수가 작아지고 top_n=3 선정이 모멘텀 8위까지만 보고 결정 → 선정 결과가 설계와 체계적으로 달라짐.
- **권고**: run_live canslim 경로에서 cfg.pool 을 12 로 세팅하거나 RunConfig 에 strategy 별 pool 기본 분리. 최소 cfg.strategy=='canslim' 이고 pool 미지정이면 12 보정.
- **근거**: live_engine.py:32 pool:int=8; 183 pool=cfg.pool; live_select_canslim.py:44 select(...,pool=12,...); run_live.py:156 RunConfig(strategy=a.strategy)

#### Major-23 · `live_select_canslim.py:36 vs strategies/factors.py:17 / strategies/cross_momentum.py:22` _(team 레드)_
- **요지**: 라이브 canslim 12-1 모멘텀과 백테스트 12-1 산출식 off-by-one 불일치 — 운영신호≠백테스트신호
- **상세**: 라이브 _mom_12_1 은 s.iloc[-21]/s.iloc[-252]-1(36), 백테스트 모멘텀(factors.momentum:17, cross_momentum:22)은 prices.shift(21)/prices.shift(252)-1. 최신봉에서 shift(21).iloc[-1]=iloc[-22], shift(252).iloc[-1]=iloc[-253] → 라이브와 정확히 한 행씩 어긋남. 검산(길이300): 라이브 mom=4.7143(행279/48) vs 백테스트 4.8125(행278/47) SAME=False. 12-1 모멘텀을 백테스트로 검증해도 라이브 canslim 실사용 신호값과 다름. docstring 의 'backtest↔live 정합' 주장이 repo 내부 백테스트에 대해 깨짐(라이브는 A 엔진은 충실 미러).
- **권고**: 백테스트 모멘텀과 라이브 _mom_12_1 인덱싱을 한 기준으로 통일(백테스트를 shift(20)/shift(251) 또는 라이브를 shift 의미로 정렬). 단일 산출 함수 공유로 운영신호=백테스트신호 보장.
- **근거**: live_select_canslim.py:36; strategies/factors.py:17; strategies/cross_momentum.py:22; 검산 SAME=False

#### Major-24 · `dashboard/server.py:92-96 (_auth) + :113-123 (api_run) + :131-164 (halt/resume)` _(team 블루)_
- **요지**: 단일 공유 토큰으로 실매매 트리거 — 비상수시간 비교·무차단·무감사
- **상세**: POST /api/control/run 은 _auth 통과 시 run_live.run(broker_kind='toss') 를 백그라운드 실행(body broker='toss'+confirm=true 면 실거래). _auth 는 `if token != DASH_TOKEN`(:95) 단일 문자열 비교라 (1) 상수시간 비교가 아니어서 타이밍 사이드채널 노출, (2) 시도 제한/락아웃/감사로그 부재로 토큰 무차별 대입을 막지 못함, (3) 토큰은 만료·로테이션 없는 단일 공유 시크릿. 같은 토큰으로 halt·resume(killswitch reset)도 가능. tunnel.ps1 로 공개 URL 노출 시 control 면이 인터넷에 노출.
- **권고**: 토큰 비교를 hmac.compare_digest 로 상수시간화. control 엔드포인트에 실패횟수 락아웃/레이트리밋 + 감사로그(누가·언제·broker) 추가. toss 실행은 토큰 외 별도 서명/OTP 또는 로컬호스트 전용 제한 검토.
- **근거**: dashboard/server.py:92-96(_auth, != 비교), :113-123(api_run→run_live.run(broker_kind=broker)), :131-164(halt/resume)

#### Major-25 · `dashboard/data.js (git-tracked) + dashboard/build_data.py:30,820 + tools/deploy_push.ps1:35` _(team 블루)_
- **요지**: 실 계좌 포지션 담긴 data.js 가 git 추적 — 배포 시 GitHub로 유출 가능
- **상세**: dashboard/data.js 는 git 추적 파일이며 build_data.py main() 이 OUT=dashboard/data.js 에 실 계좌 데이터를 덮어쓴다(:30 OUT, :820 open(OUT,'w')). 커밋된 스냅샷도 meta.source='broker:toss', source_label='실 계좌 · toss'(data.js:9-10), holdings(KMI 등)·cash 포함. deploy_push.ps1:35 의 `git add -A` 가 추적 파일을 전부 스테이징하므로, 실 계좌 연결 머신에서 build_data.py 실행 후 배포하면 보유종목·수량·현금이 GitHub(private)로 push 됨. 비공개라도 자격증명 유출 시 금융 포지션 통째 노출.
- **권고**: dashboard/data.js 를 .gitignore 에 추가하고 git rm --cached 로 추적 해제. 대시보드는 server.py 의 /api/dashboard(인메모리 build())만 쓰므로 정적 data.js 커밋 불필요. 또는 build_data.py main() 의 파일쓰기 제거.
- **근거**: git ls-files(data.js 추적), dashboard/build_data.py:30,820, dashboard/data.js:9-13, tools/deploy_push.ps1:35

#### Major-26 · `dashboard/tunnel.ps1:26-32 + dashboard/server.py:44-58 (site_gate)` _(team 블루)_
- **요지**: DASH_SITE_PASS 미설정이어도 3초 후 공개 터널 강행
- **상세**: tunnel.ps1 은 cloudflared 빠른 터널로 로컬 8765 를 trycloudflare.com 공개 URL 에 노출한다. DASH_SITE_PASS 가 없으면 경고만 출력하고 Start-Sleep 3초 뒤 cloudflared 실행(:26-32) — 즉 사이트 게이트(server.py:44-58)가 비활성인 채 누구나 /api/dashboard(실 계좌 포트폴리오)·control UI 에 접근 가능. 게이트는 server.py 의 env DASH_SITE_PASS 에만 의존하고 tunnel.ps1 이 이를 강제하지 않는다.
- **권고**: DASH_SITE_PASS(및 control 노출 시 DASH_TOKEN) 미설정이면 경고가 아니라 exit 1 로 중단. server.py 도 0.0.0.0 바인딩 시 DASH_SITE_PASS 없으면 기동 거부 검토.
- **근거**: dashboard/tunnel.ps1:26-32, dashboard/server.py:44-58(site_gate, DASH_SITE_PASS 의존)

#### Major-27 · `broker/toss.py:127-135,148-150 (_request) + :277-306 (place_order)` _(team 블루)_
- **요지**: POST /orders 가 5xx·네트워크오류에 자동 재시도 — 응답유실 시 중복주문, 서버 dedupe 가정에만 의존
- **상세**: _request 재시도 루프는 method 무관하게 POST /orders 도 429·5xx·RequestException(연결리셋·타임아웃)에 최대 max_retries(기본2)회 재시도(:132-135 network, :148-150 5xx). '서버는 접수했으나 응답 유실'(체결 직후 reset, 504 등) 상황에서 클라이언트가 같은 주문을 재POST 한다. 유일 중복방지는 clientOrderId(:282-283, day|symbol|side|int(qty) sha1)로, 토스가 이 키로 중복접수를 거부해줄 것이라는 가정에 전적 의존(:279 주석). 재시도 전 GET 으로 동일 키 존재확인·전송 전후 reconcile 폴백이 전무. 토스가 멱등 보장 안 하거나 윈도가 다르면 무인 24/7 에서 동일 신호 2주.
- **권고**: (1) 토스 문서로 clientOrderId 멱등(중복 시 409/기존주문 반환)을 toss_check.py 검증 케이스로 실증. (2) 보장 불확실 시 POST /orders 를 재시도 대상에서 제외하거나, 재시도 직전 GET /orders 조회 후에만 재제출.
- **근거**: broker/toss.py L127-135(network 재시도), L143-150(429/5xx 재시도), L277-306(clientOrderId 만 유일방어, 존재확인 없음)

#### Major-28 · `live_engine.py:228-249 + run_exit.py:114-131 + panic_exit.py:73-87 + broker/toss.py:290,308-314` _(team 블루)_
- **요지**: 제출 후 미체결 DAY주문 취소가 best-effort + 취소 전 크래시 시 미취소 DAY주문이 늦게 체결돼 이중 노출
- **상세**: 세 주문경로 모두 '제출 → _await_fills(30s) → 미종결 DAY주문 cancel_order' 순서다. timeInForce=DAY(toss.py:290)라 미체결 주문은 장중 늦게 체결될 수 있고 이를 막는 게 말미 cancel 루프다. 그러나 (1) cancel_order 는 best-effort(toss.py:308-314 실패 시 False·예외삼킴, live_engine.py:246 if 무시)라 취소 실패가 조용히 통과, (2) place 루프(L228-229)와 cancel 루프(L243-249) 사이 크래시/강제종료 시 제출된 DAY주문이 미취소 잔존. 재시작 후 run_once 는 mark_traded 미설정이라 현 포지션 기준 재plan 하는데, 미취소 DAY주문이 그 사이 체결되면 초과 보유분이 생겨 reconcile drift 로만 사후 감지. 24/7 무인에서 이중매수·이중매도 경로.
- **권고**: cancel 실패를 침묵 통과시키지 말고 record_error/알림으로 승격. 미체결 잔존 주문을 다음 실행 시작 시 broker open-orders 조회로 능동 취소하는 startup reconcile 추가(현재는 슬리브 basis 만 reconcile).
- **근거**: live_engine.py L243-249(best-effort cancel, 예외 pass), run_exit.py L125-131, panic_exit.py L80-87; toss.py L308-314(실패 시 False/흡수); timeInForce DAY=toss.py L290

#### Major-29 · `broker/executor.py:29-33, broker/toss.py:84-107, run_live.py:88-90, review.py:31` _(team 블루)_
- **요지**: 토스 실주문에 수수료·세금·환전스프레드 미반영 — 사이징 쿠션이 cost_buffer 하나뿐
- **상세**: buy_mult=(1+spread/2)·(1+slippage)·(1+commission)·(1+cost_buffer)인데, 비용배수는 broker._commission/_spread/_slippage getattr 로 읽는다(executor.py:29-31). TossBroker 는 이 세 속성을 노출하지 않아 commission=spread=slippage=0.0 → buy_mult=1+cost_buffer 만 남는다. 기본 cost_buffer=0.005(run_live.py:90), 클램프 0.3~1.0%(review.py:31). 즉 미국주식 수수료·SEC/TAF fee·KRW→USD 환전스프레드가 매수 사이징에 구조적으로 0. 실 round-trip 비용이 0.5%(환전스프레드만 통상 0.1~1%) 초과하면 마지막 주문이 insufficient-buying-power 거부→status=partial. 과매수는 int floor 가 막지만 비용이 사이징·회계에 미모델링되어 cost_buffer 가 유일 방어선.
- **권고**: TossBroker 에 실측 수수료율·예상 환전스프레드를 _commission 등으로 노출하거나, cost_buffer 기본값을 실수수료+환전스프레드 상한(≥1.0%) 기준 상향. review.py 자동튜닝은 실현 슬리피지만 보므로 수수료/세금은 별도 상수로 명시.
- **근거**: broker/executor.py:29-33(getattr), broker/toss.py(속성 부재), run_live.py:88-90, review.py:31

#### Major-30 · `broker/toss.py:8,16-18,203-212 + broker/executor.py:28,63,67` _(team 블루)_
- **요지**: 환율 변환 코드 부재 — cashBuyingPower(USD)를 검증 없이 가용현금으로 신뢰
- **상세**: get_account()은 /buying-power?currency=USD 의 cashBuyingPower(:207)·holdings marketValue.usd(:209-211)를 그대로 USD cash·equity 로 쓴다. 코드 전역에 환율(KRW↔USD) 변환·검증이 전무. 사이징 체인이 이 cash 를 확정 USD 로 가정(executor.py:28 investable=equity·alloc, :63 budget=cash+proceeds, :67 afford=int(budget/unit)). docstring 자신이 경고하듯(toss.py:16-18) 원화 자동환전 계좌면 USD cashBuyingPower 가 실시간 환율로 산출돼 0/불안정 가능. 이때 ① 부풀려진 시점에 사이징하면 환전 후 현금부족 거부, ② get_account~place_order 간 환율변동분만큼 헤드룸 소실. buy_mult 에 FX 버퍼가 없어 FX 변동이 곧 과매수→거부(partial). 강제 가드(원화계좌 거부·환율 신선도) 없고 수기 확인 의존.
- **권고**: 실거래 전 계좌가 USD 직접펀딩인지 코드로 확인(buying-power currency 응답 검증)하거나, 원화계좌면 환율 신선도·변동 버퍼를 cost_buffer 와 별도 추가. 최소한 cashBuyingPower 가 0/비정상일 때 거래 거부 게이트 추가.
- **근거**: broker/toss.py:8,16-18,203-212; broker/executor.py:28,63,67; 전역 grep 환율 변환 부재

#### Major-31 · `live_engine.py:90-110 (_reconcile) + base.py:71 + live_engine.py:282-285` _(team 블루)_
- **요지**: 주문↔체결 reconciliation 이 수량만 대조 — 체결금액/현금 정합성 미검증
- **상세**: _reconcile()는 expected(직전수량+체결수량) vs actual(브로커 실수량)을 수량으로만 대조(:99 d=filled_qty, :108 abs(e-a)>1e-6). 체결금액·현금차감 정합성은 어디서도 미대조. ref_price 와 avg_fill_price 차이는 review 슬리피지 측정용으로만 기록되고(base.py:71 '거래엔 미사용'), 이 슬리피지로 인한 실제 현금차감이 예상과 맞는지 검증 로직이 없다. 시장가 체결이 ref_price 대비 크게 불리하게 체결돼 현금이 예상보다 더 빠져나가도 수량만 맞으면 reconcile.ok=True 통과(:284). 금액 기준 over-spend 가 무성으로 통과 가능.
- **권고**: _reconcile 에 체결금액(Σfilled_qty·avg_fill_price) vs 예상금액(qty·ref_price·buy_mult) 대조 추가, 임계 초과 시 drift 보고·알림.
- **근거**: live_engine.py:90-110(수량만 비교), base.py:71(ref_price 거래 미사용), live_engine.py:282-285

#### Major-32 · `broker/toss.py:281-283 (place_order clientOrderId)` _(team 블루)_
- **요지**: 멱등 주문키가 ET 세션 아닌 KST date.today() 기반 — 자정 넘김 재실행 시 중복주문 차단 실패
- **상세**: clientOrderId(토스 브로커 레벨 중복접수 차단의 유일 키)가 day=date.today().isoformat() 즉 호스트(KST) 캘린더 날짜로 해시된다. 그러나 거래 세션의 단일 진실원은 ET 기준 last_completed_session()(run_live/run_exit/panic 이 KillSwitch.today·journal·staleness 비교에 사용). 미 정규장은 KST 22:30~05:00 으로 자정을 넘긴다. 동일 ET 세션을 겨냥한 같은 주문이라도 1차가 KST 23:50(날짜X), --force 재실행/재시도가 KST 00:10(날짜X+1)에 나가면 day 가 달라져 SHA1 키 변경 → 토스가 논리적 중복 인식 못 함 → 동일 주문 2회 접수. 주석(279-280)의 보장이 자정 경계에서 깨지고, 장중청산이 바로 그 시간대에 동작.
- **권고**: clientOrderId 의 day 를 date.today()(KST) 가 아니라 호출측이 보유한 ET 세션 날짜(last_completed_session 결과)로 산출. place_order(req, session_day=...) 주입 또는 last_completed_session().isoformat() 을 키 재료로. KillSwitch 멱등과 브로커 멱등이 같은 날짜 기준 공유.
- **근거**: broker/toss.py:281 day=date.today().isoformat(); 282-283 cid=sha1; run_live.py:99-103 session=last_completed_session(); run_exit.py:52/panic_exit.py:109 동일; guardrail.py:169,384,387; README.md:328,400

#### Major-33 · `broker/toss.py:243-260 (market_open) + run_exit.py:70 + panic_exit.py:114` _(team 블루)_
- **요지**: 장중청산 게이트가 토스 시각이 naive ISO면 TypeError→항상 False → 청산 무성 비활성 위험
- **상세**: market_open 은 today.regularMarket.startTime/endTime 을 fromisoformat 으로 파싱해 now=datetime.now().astimezone()(aware)와 비교. 토스 응답이 오프셋 없는 naive ISO('2026-06-24T22:30:00')면 start/end 가 naive 가 되고, naive↔aware 비교는 TypeError('can't compare offset-naive and offset-aware') → except (ValueError,TypeError)가 False 반환. 결과 run_exit(:70)·panic_exit(:114)이 '미 정규장 아님'으로 판단해 --force-open 없이는 장중청산을 절대 실행 못 함. 200MA 이탈·손절 등 장중 리스크 오버레이가 조용히 전면 비활성(무성실패). 토스 시각 포맷 미검증이라 현실적 실패모드. fail-safe 방향이나 안전장치 통째로 죽음.
- **권고**: 파싱한 start/end 가 naive(tzinfo None)면 기준 tz(KST 또는 ET)로 명시 localize 후 비교, 또는 전부 UTC 정규화. TypeError→False 경로를 '캘린더 파싱 실패'로 구분 로깅/알림해 무성 비활성을 관측가능화. 운영 전 toss_check.py 로 실제 startTime/endTime 포맷(오프셋 유무) 실증.
- **근거**: broker/toss.py:255-258 fromisoformat·비교, 259-260 except→False; run_exit.py:70, panic_exit.py:114

#### Major-34 · `notify.py:22-32 (_telegram), 35-44 (_slack) + README.md:344` _(team 블루)_
- **요지**: 알림 전송이 HTTP 응답코드 미검증 — 실패해도 성공으로 보고(무성실패)
- **상세**: _telegram 은 requests.post 를 timeout 만 걸고 호출 후 무조건 return True. status_code/raise_for_status 검사 없음. 토큰 폐기·잘못된 chat_id·봇 차단·Telegram 4xx 는 비-2xx 를 돌려주지만 requests 는 4xx 에 예외를 안 던지므로 거부돼도 sent.append('tg')·로그에 '→ tg' 남는다. 운영자는 체결·트립·킬스위치·크래시 알림이 갔다 믿지만 실제로 아무도 못 받음. README.md:344 도 '알림 실패가 거래를 막지 않음(예외 삼킴)'으로 명시 — 24/7 무인 봇에서 알림이 유일 사람-루프인데 그 채널 전달 실패가 구조적으로 은폐. (screen_degraded·verify_invariants CRITICAL 경보도 이 경로로 유실 가능 — 타 worker 의 review/select Info 와 연계.)
- **권고**: _telegram/_slack 에서 raise_for_status(또는 200<=status<300) 확인 후에만 True 반환. 텔레그램은 응답 JSON 의 ok 필드까지 확인. 실패 시 _log.warning 으로 회전로그에 남겨 사후 추적.
- **근거**: notify.py:28-30 requests.post 후 resp 미사용·무조건 return True; _slack:41 동일; README.md:344

### Minor (37)

#### Minor-1 · `dashboard/server.py:44-58 (site_gate) + dashboard/tunnel.ps1:9 — ?k=<DASH_SITE_PASS> 쿼리파라미터 인증` _(team 레드)_
- **요지**: 공유 site 비번을 URL 쿼리스트링에 노출 — 히스토리/Referer/프록시/CDN 로그 유출, 실거래 control 표면 직결
- **상세**: 외부 노출 경로(tunnel.ps1→trycloudflare 공개 URL)의 유일 1차 인증이 ?k=<DASH_SITE_PASS> 쿼리(52). 평문 비밀번호가 브라우저 히스토리·Referer·cloudflared/프록시/CDN 로그·북마크에 잔존. 발급 쿠키(54)는 httponly·samesite=lax 만 있고 secure 없어 비-HTTPS 홉 노출 가능. 이 게이트 뒤에 실거래 발사 control(POST /api/control/run broker=toss)이 있어 단일 공유 패스 유출이 무단 실주문 위험 직결.
- **권고**: site pass 를 쿼리 대신 Authorization 헤더/POST 폼으로 받고 쿠키 secure=True 추가. 가능하면 cloudflared named tunnel + Cloudflare Access(IdP)로 대체. 최소 ?k= 접속 후 즉시 쿼리없는 URL 리다이렉트로 히스토리/Referer 잔존 차단.
- **근거**: dashboard/server.py:50-58,113-123; dashboard/tunnel.ps1:9

#### Minor-2 · `dashboard/server.py:77-79 → dashboard/build_data.py:59-68,781-795 (/api/symbol/{tk} glob 유입)` _(team 레드)_
- **요지**: 무인증 /api/symbol path param tk 가 검증 없이 glob 에 유입 — glob 인젝션·임의 캐시파일 열람
- **상세**: GET /api/symbol/{tk} 의 tk 는 server.py:79 에서 .upper() 만 거쳐 _longest_csv(tk)→glob.glob(CACHE/f'{ticker}_*.csv')(build_data.py:61). tk 가 영숫자 제한 없어 glob 메타문자(*,?,[)·. 통과 → CACHE 내 임의 *_*.csv 열람·열거 glob 인젝션. 캐시 미스 시 yf.Ticker(tk).history()(794-795)로 공격자 제어 문자열이 외부 API 호출. site_gate 외 별도 인증 없어 터널 공개 시 무인증 읽기 표면.
- **권고**: tk 를 ^[A-Z][A-Z0-9.\-]{0,9}$ 화이트리스트 검증, 불일치 400. _longest_csv 진입 전 glob.escape(). 가능하면 알려진 유니버스 심볼만 허용.
- **근거**: dashboard/server.py:77-79; dashboard/build_data.py:59-68,781-795

#### Minor-3 · `dashboard/server.py:16,20,187,192 — DASH_HOST=0.0.0.0 가이드 + 정적 마운트 디렉토리 전체 노출 + env 미설정 무인증` _(team 레드)_
- **요지**: docstring 이 0.0.0.0 바인딩 유도 + StaticFiles 디렉토리 통째 서빙 + env 미설정 시 무인증 노출
- **상세**: 기본 바인딩 127.0.0.1(안전,192)이나 docstring(16,20)이 DASH_HOST=0.0.0.0 실행을 안내해 LAN/공개 노출 유도. app.mount('/',StaticFiles(directory=HERE,html=True))(187)는 dashboard/ 전체 정적 서빙 — 향후 민감 파일 유입 시 무인증 노출. 또 DASH_HOST=0.0.0.0 인데 DASH_SITE_PASS 미설정이면 env 부재가 조용히 '인증 없음'으로 진행돼 매매·정지 제어면이 열림. 게이트에 dashboard 인증/바인딩 검증 없음(SUITES 미포함).
- **권고**: DASH_HOST 가 0.0.0.0/공인IP 일 때 DASH_SITE_PASS·DASH_TOKEN 미설정이면 기동 거부(fail-closed) 가드+단위테스트. StaticFiles 를 명시 파일(index.html,data.js,icon.svg,manifest.json) 화이트리스트로 축소. docstring 에 0.0.0.0 노출 시 SITE_PASS+방화벽 동반 명시.
- **근거**: dashboard/server.py:16,20,39-40,187,192-193; tools/run_tests.py SUITES(dashboard 미포함)

#### Minor-4 · `dashboard/server.py:52,56,95 — site-pass/토큰 비교가 비-상수시간(!=)` _(team 레드)_
- **요지**: DASH_TOKEN·site-pass 를 != 단순 비교 (timing side-channel)
- **상세**: control 토큰 검증이 if token != DASH_TOKEN(95) 단순 문자열 비교. site_gate 의 site-pass 비교(52,56)도 동일. 공개 터널처럼 원격 타이밍 측정 가능 환경에선 이론상 타이밍 사이드채널로 토큰 추론 시도 가능. 실주문 발사 표면이라 방어적으로 상수시간 비교가 바람직.
- **권고**: hmac.compare_digest(token,DASH_TOKEN) 로 교체. site_gate ?k/cookie 비교도 동일 적용.
- **근거**: dashboard/server.py:92-96,52,56

#### Minor-5 · `dashboard/install_autostart.ps1:16-26 — 시작프로그램 .cmd 에 평문 시크릿 set 유도` _(team 레드)_
- **요지**: Autostart .cmd 템플릿이 DASH_TOKEN/SITE_PASS 를 디스크 평문 저장 유도
- **상세**: Startup 폴더 생성 ustrade-dashboard.cmd 템플릿이 rem set DASH_TOKEN=제어비번 / rem set DASH_SITE_PASS=사이트비번(19-21)을 담는다. 사용자가 제어/외부노출을 켜려면 주석 풀고 실제 비번을 평문 .cmd 에 박게 됨 → 시크릿이 사용자 프로파일 평문 파일로 영속(같은 사용자 프로세스 읽기 가능), '시크릿은 환경변수에만' 정책과 어긋남.
- **권고**: .cmd 직접 박기 대신 사용자 환경변수(setx, DPAPI) 또는 별도 .env(gitignored, ACL 제한)에서 로드하도록 가이드 변경. 최소 주석에 '평문 저장 위험·setx 권장' 경고 추가.
- **근거**: dashboard/install_autostart.ps1:16-26

#### Minor-6 · `requirements*.txt — dashboard 의존성 fastapi/uvicorn/starlette 미고정` _(team 레드)_
- **요지**: Dashboard deps(fastapi/uvicorn/starlette)가 모든 requirements 파일에 부재(unpinned)
- **상세**: dashboard/server.py 가 fastapi·uvicorn·starlette import 하나 requirements.txt·requirements_vm.txt·requirements-dev.txt·pyproject.toml 어디에도 핀 0건. 나머지 의존성은 전부 == 핀인데 웹 노출 컴포넌트만 버전 미관리 → VM/PC 드리프트·알려진 CVE 버전 무방비 설치 위험.
- **권고**: fastapi·uvicorn·starlette 를 핀 버전으로 requirements 에 추가, 보안 패치 추적 포함. 대시보드 미사용 VM 이면 requirements-dashboard.txt 분리.
- **근거**: dashboard/server.py:33-35; requirements.txt; requirements_vm.txt; requirements-dev.txt

#### Minor-7 · `toss_check.py:22 — 진단 출력에 계좌번호 평문 print` _(team 레드)_
- **요지**: toss_check 가 계좌번호·accountSeq 를 stdout 에 평문 출력
- **상세**: toss_check.py:22 가 print(f'✓ 연결 성공 — 계좌 {b._account_no} (accountSeq={b._account_seq})')로 계좌번호·accountSeq 를 표준출력. 수동 진단 도구라 위험 제한적이나 출력이 공유 로그/스크린샷/세션 기록에 캡처되면 계좌식별정보 노출. 시크릿(키/토큰)은 미출력이라 PII 위생 수준 Minor.
- **권고**: 계좌번호 마스킹(뒤 4자리만) 또는 --verbose 플래그에서만 전체 표시.
- **근거**: toss_check.py:22

#### Minor-8 · `broker/guardrail.py:344-353,402-414 — SELL 명목 초과가 bad price 경로에서 미검사` _(team 레드)_
- **요지**: check_order_notional 가 BUY 만 의미있게 보호 — bad price SELL 은 notional 검사 우회
- **상세**: GuardedBroker.place_order(402-414)는 bad price(None/NaN/0)일 때 SELL 은 통과시키고 notional 검사를 건너뜀(408-413). 정상 의도(위험축소 매도 허용)지만 fat-finger 대량 SELL(사이징 버그로 수량 폭증)이 시세 정상일 때만 notional 캡에 걸리고 시세 불량 시 무제한 통과. managed 슬리브 SELL=managed&¬protected 1차 방어하나 basis 손상/과대계상 시 대량 매도 가능. SELL 명목 상한이 가드 사각.
- **권고**: SELL 도 bad price 경로에서 보유수량 대비 과대(basis·실보유 초과) 검사 추가 또는 notional 캡을 시세 불량 시 avg_price 폴백 적용. 슬리브 basis 무결성 모니터(reconcile drift 알림) 연계.
- **근거**: broker/guardrail.py:407-413,344-353; broker/managed.py:189

#### Minor-9 · `run_exit.py:132-137 / panic_exit.py:88-91 / live_engine.py:172-177,243-249,253-258 (record_fills/취소/reconcile 삼킴)` _(team 레드)_
- **요지**: basis-update·취소·reconcile 실패가 세 경로 모두 try/except: pass 로 무성 삼켜짐
- **상세**: 체결 후 basis 차감(record_fills), 잔존 미체결 취소, reconcile_basis 가 세 경로 모두 except: pass(로깅 없음). record_fills 내부 _save() 디스크 오류 시 청산분이 basis 미차감 채 넘어감 → min(real,basis) cap 덕에 oversell 은 안 나나 basis stale-high 로 슬리브 회계 어긋나고 reconcile/드리프트 진단을 흐림. 특히 진입 경로 record_fills 실패는 다음 실행 사이징·중복매수 판정이 어긋날 수 있는데 로그 한 줄도 없음. swallow 가 어떤 로그/알림도 안 남겨 영속 실패가 무성 누적.
- **권고**: 세 except 블록에서 최소 _journal/notify 또는 _log.warning 으로 'basis 갱신/취소/reconcile 실패' 를 ustrade.log 에 기록. bare pass swallow 는 무인 실거래 무성실패 원칙 위배.
- **근거**: run_exit.py:132-137; panic_exit.py:88-91; live_engine.py:172-177,243-249,253-258

#### Minor-10 · `broker/toss.py:308-314 (cancel_order)` _(team 레드)_
- **요지**: cancel_order 가 실패를 False 로만 은폐 — 잔존주문 취소실패가 호출측서 무성 처리
- **상세**: cancel_order 는 TossAPIError 전부를 잡아 return False(313-314). 호출측(run_exit:128, live_engine:246, panic:84)은 이 False 를 except pass 와 사실상 동일하게 무시(취소 못해도 진행). '미체결 DAY 주문 취소 실패'(나중 늦게 체결돼 oversell/doublebuy 유발 가능)가 어디에도 경보 안 됨. business error 와 transient(5xx/네트워크) 미구분으로 통째 False 라 진짜 위험한 취소실패도 묻힘.
- **권고**: cancel_order 가 실패 사유 로깅하거나 호출측이 False 반환 건수 집계해 1건↑ 시 notify. 최소 _log.warning 으로 cancel 실패를 ustrade.log 에 남길 것.
- **근거**: broker/toss.py:310-314 try cancel return True except TossAPIError return False

#### Minor-11 · `broker/toss.py:127-171 (_request 재시도 백오프) + fmp_client.py:71-90` _(team 레드)_
- **요지**: 재시도 백오프가 선형·짧고 지터 없음 — 지속 429/5xx 시 빠른 소진·thundering herd
- **상세**: TossBroker._request 재시도는 attempt+1초(1s,2s) 선형, max_retries=2(총3회), 지터 없음. 429 는 Retry-After 존중하나 그 외 5xx/네트워크는 고정 선형. 무인 24/7 에서 토스 지속 5xx/429 면 매 cron 동일 패턴으로 빠르게 소진→에러카운트 누적→킬스위치 트립. FMP 도 선형·지터 없음. 동시 다수 cron(heartbeat·run_exit·run_live)이 같은 순간 깨어 같은 백오프로 재시도하면 thundering herd.
- **권고**: 지수 백오프+지터(min(cap,base*2^attempt)*rand) 도입과 transient 한도 상향 검토. 최소 백오프에 소량 무작위 지터.
- **근거**: toss.py:132-133,144-149; fmp_client.py:82-89

#### Minor-12 · `panic_exit.py:73-91 (비상청산 주문실패/취소실패 즉시 알림 부재)` _(team 레드)_
- **요지**: 비상청산 종목별 주문실패·취소실패를 저널만 남기고 즉시 알림 안 함
- **상세**: _panic 에서 종목별 place_order 실패는 _journal(place_error)만(76-77, 의도는 한 종목 실패가 나머지 안 막기 — 옳음) 하나 '몇 종목 청산 실패' 가 최종 알림(134-135)에 미반영. 최종 알림은 filled 만 보여 '제출조차 실패한 종목' 미표시. cancel 실패(86-87)·record_fills 실패(90-91)도 except pass 무성. 비상상황에서 일부 미청산을 운영자가 텔레그램만 보고는 모를 수 있음.
- **권고**: _panic 반환 dict 에 place_error 종목 리스트 포함, run() 알림에 '미청산/실패 N종목' 명시. plan 대비 filled 차집합 경보.
- **근거**: panic_exit.py:76-77,134-135,86-87,90-91

#### Minor-13 · `heartbeat.py 전체 (자기 감시 2차 모니터 부재)` _(team 레드)_
- **요지**: dead-man-switch 의 cron 이 죽으면 아무도 모름 — 외부 워치독 미연동
- **상세**: heartbeat.py 자체가 cron 으로 도는데 이 cron 이 DST/리부트/크래시로 멈추면 미실행 감지 주체가 없다(Healthchecks.io·Cronitor·Dead Man's Snitch 류 외부 ping 흔적이 코드·docs 어디에도 없음 — grep 0건). '감시자를 감시하는 자' 부재. heartbeat 가 살아있다는 가정에 전체 무인 안전 의존.
- **권고**: heartbeat 정상 실행마다 외부 dead-man 서비스(Healthchecks.io 무료)로 HTTP ping 발사해 heartbeat 자체 사망 시 외부 알림. 코드 변경 1줄(requests.get(PING_URL)) 수준.
- **근거**: grep 'Healthchecks|deadmanssnitch|cronitor|uptimerobot' 0건; heartbeat.py 텔레그램 단일 채널 의존

#### Minor-14 · `run_live.py:50-64 (_alert ok 분기) + live_engine.py:101-104 (_reconcile 실패 → 빈 리스트)` _(team 레드)_
- **요지**: _reconcile 이 포지션 조회 실패를 '드리프트 없음(OK)'으로 둔갑 — 정합성 검증 불가가 OK 로 오인
- **상세**: _reconcile 이 get_positions 실패 시 [](드리프트 없음) 반환(live_engine.py:103-104) → run_live.py:60-62 reconcile.ok 가 True 로 해석돼 '브로커 포지션 조회 실패' 가 '정합성 OK' 로 둔갑. 'ok' 분기는 다행히 live_engine.py:261 bad 검증이 미체결 1건이라도 있으면 partial 강등하므로 전량 FILLED 만 오나, reconcile 의 데이터 결측을 OK 로 오인하는 무성 가능성이 남음.
- **권고**: _reconcile 이 포지션 조회 실패 시 빈 리스트 대신 'unknown' 신호 반환해 _alert 가 '정합성 검증 불가 — 확인 필요' 경보. 최소 _log.warning.
- **근거**: live_engine.py:101-104 except: return []; run_live.py:60-62

#### Minor-15 · `live_engine.py:90-110 (_reconcile 수량만 대조, 금액 reconciliation 부재)` _(team 레드)_
- **요지**: 사후 정합성이 수량(qty)만 대조 — 체결금액/평단 vs 사이징기준가 금액 reconcile 부재
- **상세**: _reconcile 은 expected(직전수량+체결수량) vs 브로커 실수량만 비교(97-109). 금액 차원(주문 의도 명목 vs 실체결 대금, ref_price vs avg_fill_price 슬리피지 누계)은 reconcile 안 함. ref_price 는 OrderRequest 에 있고 review 슬리피지 튜닝이 사후 활용하나, 실행 사이클 내 '예약현금 vs 실체결대금' 정합 검증·차단 단계 없음. 수량은 맞는데 금액(평단)이 크게 다른 케이스가 무성통과.
- **권고**: _reconcile 에 금액 차원 추가 — sum(체결 BUY 대금) vs 예약 budget 소진액 대조, |avg_fill-ref_price|/ref_price 임계 초과 주문 플래그. get_account cash 전후 델타로 교차검증.
- **근거**: live_engine.py:97-109; base.py:71; live_engine.py:282-284

#### Minor-16 · `broker/managed.py / executor.py / paper.py — 금액·수량 float 사용` _(team 레드)_
- **요지**: 현금·수량·basis 전부 float, ==·누적합 — 다회 리밸런스·fractional 혼입 시 드리프트·오탐 알림
- **상세**: PaperBroker._cash float 매 체결 누적(paper.py:89,98), avg_price 가중평균 float(117). ManagedBroker basis float 누적(managed.py:221,227), 1e-9 임계 prune/비교(162,251,260). _reconcile 은 abs(e-a)>1e-6 으로 정수 기대수량을 float 실수량과 비교(live_engine.py:108) — 토스가 fractional share 평단/수량 주면 정수 기대치와 1e-6 초과 드리프트로 매 실행 reconcile=NOT ok 오발(run_live.py:61-62). paper.py:6 주석 스스로 'Decimal 권장' 인지하나 미적용.
- **권고**: 현금·수량·평단을 Decimal 또는 정수 최소단위(주식=정수주)로 통일. reconcile float 동등성 비교에 round to share 후 비교로 fractional 오탐 차단.
- **근거**: paper.py:6,89,98,117; managed.py:221,227,251,260; live_engine.py:108

#### Minor-17 · `run_exit.py:86 (end_excl = datetime.now().date() + 1day)` _(team 레드)_
- **요지**: 청산 데이터 로드 상한이 KST 로컬 날짜 — 세션(ET) 기준과 불일치
- **상세**: run_exit 는 세션 today 를 last_completed_session()(ET)로 잡지만(52-53) 일봉 로드 상한 end_excl 은 datetime.now().date()+1day(KST 로컬)로 만든다(86). run_live.py:104 는 session+timedelta(days=1)(ET 세션 기준)로 일관. 정상 경로(KST 새벽)에선 KST 날짜가 ET 세션보다 하루 앞서 무해하나, VM TZ 가 UTC/ET 면 KST 가정이 깨지거나 자정 직전 실행 시 end_excl 이 세션봉 누락→data 신선도(session_gap>3) 오판으로 청산 보류(data_ok=False) 가능.
- **권고**: run_live.py:104 와 동일하게 end_excl 을 (session+timedelta(days=1)).isoformat() 으로 ET 세션 기준 통일.
- **근거**: run_exit.py:86,52-53; run_live.py:104

#### Minor-18 · `scheduler.py:18 (RUN_TIME 06:10 호스트 로컬) + tools/DEPLOY.md` _(team 레드)_
- **요지**: scheduler.py·cron 이 VM 로컬 TZ=KST 암묵 가정 — UTC VM 이면 미 마감 전 조기실행
- **상세**: scheduler.py 는 RUN_TIME(기본 06:10)을 호스트 로컬(18 주석)로 해석해 발사. 06:10 이 미 마감 후가 되려면 호스트 TZ 가 KST 여야 함. AWS Lightsail 등 기본 TZ 가 흔히 UTC 면 06:10 'UTC'=02:10 ET(개장 전)로 의도와 다른 시각 발사, last_completed_session() 이 전일 세션 반환. staleness 가드가 잘못된 날짜 거래는 막지만 발사 자체가 누락·조기실행. README.md:336 이 'cron UTC 면 환산' 경고하나 강제 아니며 tools/DEPLOY.md 에 VM TZ 설정 단계 부재.
- **권고**: DEPLOY.md·SETUP_GITHUB.md 에 VM TZ 를 Asia/Seoul 고정(Set-TimeZone 'Korea Standard Time') 셋업 단계 명문화하거나 scheduler/cron 발사 시각을 ET 기준 산출(now_et 비교)로 변경.
- **근거**: scheduler.py:18; README.md:325/336; tools/DEPLOY.md(TZ 단계 부재)

#### Minor-19 · `run_live.py:160-161, run_exit.py:157-158, panic_exit.py:151-152 (TOSS_MANAGED_CASH float 파싱)` _(team 레드)_
- **요지**: TOSS_MANAGED_CASH malformed 시 float() 크래시 — 검증·기본값 없음
- **상세**: 세 실거래 진입점 모두 cap None 이고 TOSS_MANAGED_CASH 설정 시 무조건 float(os.environ['TOSS_MANAGED_CASH']). 빈문자열·'$3500'·'3,500' 등 잘못 setx 시 ValueError 즉시 크래시. cash_cap 은 보호슬리브 cash 상한(사이징)에 직결 금전 파라미터인데 잘못된 env 가 봇을 죽이거나(그나마 안전) 어딘가 except 로 삼켜지면 cap 누락으로 과매수 위험. 게이트에 이 env 파싱 테스트 없음.
- **권고**: _num()류 안전 파서(toss.py:80 존재)로 감싸 malformed 시 명시적 에러·notify 후 정지 또는 명확한 기본값. 빈문자열/콤마/통화기호 단위테스트 추가.
- **근거**: run_live.py:160-161; run_exit.py:157-158; panic_exit.py:151-152; broker/toss.py:80(_num 미사용)

#### Minor-20 · `tools/run_tests.py:26 + vm_update.ps1:63 vs live_select_canslim.py:23-28` _(team 레드)_
- **요지**: 게이트 주석이 A엔진 경로를 C:\텔레그램_시그널_알리미로 오기 — 실제는 형제 디렉토리
- **상세**: run_tests.py:26 주석 'A엔진(C:\텔레그램_시그널_알리미) 없어 canslim import 불가', vm_update.ps1:63 도 'C:\텔레그램_시그널_알리미\engine\ repo 밖' 명시. 그러나 코드 live_select_canslim.py:26 은 _A_DIR=__file__.resolve().parent.parent/'텔레그램_시그널_알리미'(.../Projects/텔레그램_시그널_알리미, 프로젝트 형제). 디스크 확인 C:\텔레그램_시그널_알리미 부재, 형제 경로엔 engine/funda.py 존재. 문서/주석과 코드 경로 불일치 → 운영자가 잘못된 C:루트에 A엔진 복사 시 canslim import 실패. tests_canslim.py:17 top-level import → A엔진 미존재면 게이트 import 크래시(USTRADE_CI=1 만 면제, VM 미면제).
- **권고**: run_tests.py:26·vm_update.ps1:63 주석을 형제경로(.../Projects/텔레그램_시그널_알리미)로 정정. VM 에 A엔진이 형제 경로로 배치돼야 게이트 통과함을 DEPLOY.md 명시.
- **근거**: tools/run_tests.py:26; vm_update.ps1:63; live_select_canslim.py:23-28; tests_canslim.py:17; 디스크 확인(C:루트 MISSING, 형제 funda.py 존재)

#### Minor-21 · `tests_toss.py 전반 + broker/toss.py:127-171` _(team 레드)_
- **요지**: 토스 브로커 retry/타임아웃/network-error 경로가 게이트 테스트에서 미검증
- **상세**: _request 는 max_retries 까지 429/5xx/RequestException 백오프 재시도(127-150), 소진 시 retry-exhausted(171), 전송오류 network-error(135) raise — 무인봇 핵심 실패모드. tests_toss.py 는 전부 max_retries=0(79)으로 브로커 생성, grep 상 network-error/retry-exhausted/429/RequestException 단언 0건. 부분체결 후 재시도 시 중복접수 안 되는지, 429 Retry-After 존중, 타임아웃→network-error, 재시도 소진 경계가 게이트서 한 번도 미실행. 실주문 경로 재시도/멱등이 사각.
- **권고**: tests_toss.py(또는 tests_hardening.py)에 max_retries>=1 로 429→재시도→200, 연속5xx→retry-exhausted, RequestException→network-error 추가. 주문 POST 재시도 시 동일 clientOrderId 재전송되는지 단언해 중복차단 행위검증.
- **근거**: broker/toss.py:127-171; tests_toss.py:79(max_retries=0); grep 0건

#### Minor-22 · `live_select_canslim.py:1-18 + backtest_portfolio.py:37 / backtest_risk.py:3` _(team 레드)_
- **요지**: 운영 신호(12-1 canslim+펀더)가 포트폴리오 백테스트(6-1 rs_momentum)로 검증 안 됨
- **상세**: 운영 기본 전략은 canslim(12-1 모멘텀 게이트+CANSLIM/애널/Piotroski 펀더 틸트). portfolio 백테스트 기본 전략은 rs_momentum 6-1(backtest_risk.py:3, backtest_portfolio.py:37 default)이며 펀더 틸트 전무. live_select_canslim.py:16 도 'B 현행 6-1 과 의도적으로 다름' 명시. 실거래 선정 로직(12-1+펀더)의 OOS 성과·과적합을 repo 백테스트가 직접 검증 안 함. 펀더 부분은 yfinance 현재 스냅샷 의존이라 과거 재현 구조적 불가 → 백테스트 부재가 데이터 한계로 정당화되나 검증 공백 자체는 운영 리스크.
- **권고**: 최소 12-1 모멘텀 게이트(펀더 제외)라도 백테스트/eval_factor IC 로 라이브 동일 산출식 robust 확인. 펀더 틸트 검증 불가를 운영 문서 명시·보수적 다이얼 유지.
- **근거**: live_select_canslim.py:16,31; backtest_portfolio.py:37; backtest_risk.py:3

#### Minor-23 · `data_cache/ (디스크 189 CSV) vs data.py:47,34-40 + run_live.py:120` _(team 레드)_
- **요지**: data_cache 189개 전부 구포맷 orphan — 현행 로더가 안 읽고 일부는 영원히 미정리(죽은 캐시·중복)
- **상세**: 현행 data.load 캐시키는 {ticker}_{start}.csv(end 제외, data.py:47)인데 디스크 189개는 전부 구포맷 {ticker}_{start}_{end}.csv. 신포맷 매칭 0개 → 현행 로더 절대 미히트(매 실행 재다운로드), 189 CSV(~36MB) 전부 죽은 데이터. _purge_legacy(34-40)는 요청 start 의 {ticker}_{start}_*.csv 만 지우고 run_live 는 start='2022-01-01' 만 쓰므로(120) 2014/2016/2018/2021 start 파일은 영원히 미정리. 같은 종목 다중 날짜범위 중복(AAPL 5개·다수 4개)도 전형적 캐시 위생 불량. 라이브 오염 위험은 없음(로더 미독).
- **권고**: data_cache/ 구포맷 CSV 일괄 정리(또는 _purge_legacy 를 start 무관 {ticker}_*_*.csv 패턴 확장). 종목당 단일 최장 히스토리로 통합. gitignore 되어 git 영향 없음.
- **근거**: data.py:47,34-37; run_live.py:120; 디스크 신포맷 매칭 0, 189 구포맷; <200B/corrupt 0

#### Minor-24 · `tests_stage2.py:30-44 (test_h2_calendar)` _(team 레드)_
- **요지**: 캘린더 테스트가 'DST 인지' 라벨만 달고 DST 전환 경계·조기폐장을 실제 검증 안 함(theater)
- **상세**: test_h2_calendar print 라벨은 '휴장일·주말·장중·DST 인지'(30)지만 실제 단언은 Memorial Day·토요일·화요일 개장·장중 직전세션·session_gap 뿐(32-44). DST 전환일(spring-forward·fall-back) 전후 last_completed_session/now_et 의 ET 오프셋(-4↔-5) 전환 검증 케이스 없음. 조기폐장(반장, 13:00 ET) 단언도 없음. calendar_util 은 mcal 위임이라 실동작은 옳을 가능성 높으나 테스트 라벨이 커버 과장해 회귀 시 DST/반장 버그 미포착.
- **권고**: DST 전환 직후 평일(2026-03-09,2026-11-02) now_et().utcoffset()=-4h/-5h, last_completed_session 전환 처리, 조기폐장일 market_close=13:00 ET 단언 추가.
- **근거**: tests_stage2.py:30,32-44; calendar_util.py:5/40

#### Minor-25 · `dashboard/server.py:52,54 (site_gate)` _(team 블루)_
- **요지**: 사이트 패스를 URL 쿼리(?k=)로 전달 + 쿠키 secure 누락
- **상세**: site_gate 는 ?k=<DASH_SITE_PASS> 쿼리스트링으로 인증(:52). 쿼리 파라미터는 Cloudflare 엣지/프록시 액세스 로그, 브라우저 히스토리, Referer 헤더에 평문으로 남아 시크릿이 잔류. 쿠키는 httponly+samesite=lax 이나 secure 플래그가 없어(:54) HTTP 평문 구간에서 가로채질 수 있음(터널 HTTPS 라 완화되나 0.0.0.0 직접 노출 시 위험).
- **권고**: 패스를 쿼리 대신 POST 폼/헤더로 1회 교환하거나 즉시 URL 에서 제거(리다이렉트). set_cookie 에 secure=True 추가.
- **근거**: dashboard/server.py:52(query_params.get('k')), :54(set_cookie httponly/samesite, secure 누락)

#### Minor-26 · `dashboard/server.py:50,82-84 (/api/health)` _(team 블루)_
- **요지**: health 가 site_gate 우회하며 control 활성 여부 노출
- **상세**: site_gate 는 /api/health 를 무조건 통과(:50)시키고, api_health 는 {'control': bool(DASH_TOKEN)} 반환(:84). 공개 터널에서 인증 없이 control(매매·정지 제어) 면이 켜져 있는지 외부에 알려줘 공격 표적 판단을 돕는 정보 노출.
- **권고**: health 응답에서 control 노출 제거(단순 {'ok':true}) 또는 control 상태는 인증된 경로에서만 반환.
- **근거**: dashboard/server.py:50(health 게이트 예외), :82-84(control 활성 여부 반환)

#### Minor-27 · `dashboard/build_data.py:59-68,781-788 + dashboard/server.py:77-79 (symbol_series→_longest_csv)` _(team 블루)_
- **요지**: 미검증 ticker 가 파일 glob 에 직접 들어감(경로순회 표면)
- **상세**: GET /api/symbol/{tk} 의 tk 는 검증 없이 tk.upper() 후 symbol_series→_longest_csv 로 전달되고 glob.glob(os.path.join(CACHE, f'{ticker}_*.csv'))에 그대로 보간(build_data.py:61). %2F 인코딩 시 Starlette 가 디코딩하므로 ../ 류가 glob 패턴에 합쳐질 수 있다. 다만 패턴이 `_*.csv` 접미사·OHLCV 컬럼 파싱을 요구하고 응답도 정규화 시세값뿐이라 실제 악용은 매우 제한적. 입력 sanitize 부재 자체가 위생 이슈.
- **권고**: tk 를 정규식(^[A-Z0-9.\-]{1,8}$) 화이트리스트 검증 후 처리. CACHE 절대경로 prefix 검사(os.path.realpath startswith) 추가.
- **근거**: dashboard/build_data.py:59-68(_longest_csv glob), :781-788(symbol_series), server.py:77-79(tk 미검증)

#### Minor-28 · `broker/guardrail.py:402-414 (GuardedBroker.place_order) ↔ run_exit.py:115-117` _(team 블루)_
- **요지**: 위험축소 SELL 이 fat-finger 명목캡에 걸려 청산 차단·영구정지 가능 (논리역전)
- **상세**: 장중청산 SELL 은 GuardedBroker.place_order 를 거치며 정상 시세(>0)면 side 무관하게 check_order_notional(qty*price) 호출(:412-413). 명목이 절대캡(max_order_notional=1,000,000) 초과 시 KillSwitch.trip(order_notional)→HALT, HaltError 가 run_exit.py:116 에서 'tripped' 로 잡혀 미매도 종료. 한 번 트립되면 신규진입뿐 아니라 이후 청산까지 모두 막혀(수동 reset 전) 손절·추세이탈 매도 봉쇄. fat-finger 가드는 매수 과대주문 차단 목적인데 위험축소 매도방향에 동일 캡 적용은 논리역전. 단 run_exit 는 roll_day 미호출→비례캡 비활성, 절대 1M 캡만 적용→관리 슬리브 ~$100 규모에선 현재 도달 불가라 Minor.
- **권고**: GuardedBroker.place_order 에서 req.side==Side.SELL 이면 check_order_notional 건너뛰거나 SELL 전용 캡 별도. 이미 BUY/SELL 구분(409-411)하므로 분기 추가는 surgical.
- **근거**: broker/guardrail.py:412-413(side 무관 호출), 344-353(초과 시 trip+HaltError); run_exit.py:116-117; roll_day 는 live_engine.py:223 에만

#### Minor-29 · `broker/toss.py:283,289 (place_order) ↔ panic_exit.py:54,75 / run_exit.py:115 / broker/managed.py:161,221-227` _(team 블루)_
- **요지**: 1주 미만 분수 basis 의 청산 SELL 이 int() 절삭으로 quantity="0"·1주 미달 잔여·멱등키 불일치
- **상세**: toss.place_order 는 str(int(req.qty)) 로 수량 전송·양수검증 없음. 청산·패닉 경로의 qty 는 managed.get_positions()=min(real.qty, basis)(managed.py:161)의 float 인데, basis 는 record_fills 가감산(:221-227)으로 float 누적오차가 생길 수 있다. (1) 0<qty<1 분수 보유분(예 0.4주)이 plan 에 포함되면 int(0.4)=0 → quantity="0" 무효주문이 토스로 나가고, 4xx 거부 시 REJECTED 로 흡수되지만 해당 포지션은 영원히 청산 안 되고 매 실행 무효주문 반복(toss.py:302-303). (2) 3.0→2.9999999 가 되면 clientOrderId(int=2)·body('2') 모두 2 절삭→1주 미청산 잔여, 비상청산이 전량을 못 비우는 fail-safe 미달. panic_exit 은 p.qty>1e-9 만 거르고(int 절삭 후 0 미고려), run_exit 는 d['qty']=p.qty 직전달. Executor 경로는 int 수량만 생성해 안전하나 청산경로는 managed basis 를 직접 사용해 분수가 샌다.
- **권고**: place_order 진입부에서 int(req.qty)<=0 이면 즉시 REJECTED. 청산·패닉 plan 구성 시 qty 를 round()/floor 정수화하고 잔여 <1주는 청산불가로 로그, int() 무언절삭 의존 제거.
- **근거**: broker/toss.py:283·289(int 절삭·양수검증 없음); panic_exit.py:54·75(p.qty>1e-9 만, int 절삭 미고려), run_exit.py:115; managed.py:161(min float)·221-227(float 가감)

#### Minor-30 · `live_engine.py:230-236,268,274-275 + broker/guardrail.py:382-388` _(team 블루)_
- **요지**: mark_traded 가 ok 경로에서만 호출 — partial/tripped/error/crash 후 재실행이 같은 신호로 재plan(diff 기반이라 대부분 자기교정)
- **상세**: already_traded()(guardrail.py:382)는 당일 중복실행 차단 멱등락이고 mark_traded()(:275)는 status=='ok' 에서만 호출된다. partial(:268)·tripped(:231)·error(:235)·crash 는 last_traded_day 미설정이라 cron 재시도/수동 재실행이 같은 날 다시 거래 시도. Executor.plan 이 현 포지션과 목표의 diff 로 재도출(executor.py:35-47)해 체결분은 빠져 대부분 자기교정되나, 위 미취소 DAY주문 finding 과 결합하면 재plan qty 가 달라져 새 clientOrderId(int(qty) 포함)로 진짜 신규주문이 나갈 수 있다. clientOrderId 가 qty 를 키에 포함(toss.py:283)하므로 부분진행 후 재plan 은 멱등 보호를 못 받음.
- **권고**: partial 후에도 '일부 거래 시도함' 상태를 기록해 재시도 상한을 두거나, clientOrderId 를 qty 비포함(day|symbol|side)으로 좁혀 부분진행 재plan 도 중복접수로 흡수. 현 diff 기반으로 위험은 제한적이나 보호 사각.
- **근거**: live_engine.py:230-236·268(실패경로 mark_traded 미호출), :274-275(ok 에서만), guardrail.py:382-388; clientOrderId 키 int(qty)=toss.py:282-283

#### Minor-31 · `dashboard/server.py:153-157 (/api/control/resume)` _(team 블루)_
- **요지**: 킬스위치 reset 실패를 삼키고 halted:false 로 성공 응답(무성실패)
- **상세**: HALT 해제 엔드포인트가 KillSwitch(...).reset() 을 try 로 감싸고 예외 시 {'ok':True,'halted':False,'warn':str(e)} 반환. reset 이 실제 실패(state 파일 쓰기 거부·손상)해도 ok:True·halted:False 로 응답해, 운영자는 모바일 대시보드에서 '재개됨'으로 보지만 봇은 여전히 정지일 수 있다. 안전 통제 동작 실패가 성공 UI 로 가려짐(index.html 이 warn 미표시면 사실상 은폐).
- **권고**: reset 예외 시 ok:False(또는 HTTP 5xx)·halted unknown 으로 응답해 대시보드가 실패를 빨갛게 표시. 최소한 warn 을 UI 에서 눈에 띄게 노출.
- **근거**: dashboard/server.py:156-157 except Exception as e: return {'ok':True,'halted':False,'warn':str(e)}

#### Minor-32 · `broker/executor.py:67,72, broker/guardrail.py:350, broker/paper.py:84 (전 금전 경로 float)` _(team 블루)_
- **요지**: 전 금전 경로 float 사용 — Decimal 미사용으로 경계 회계 결정성 저하
- **상세**: 코드베이스 전역에 Decimal 사용이 전무(grep 0). 현금비교·수량합산·명목계산이 모두 float: executor.py:67 int(budget/unit), :72 budget-=q·unit; guardrail.py:350 notional>limit; paper.py:84 self._cash<cost+fee. 정수 주식·달러 단위라 오차폭은 작으나 $100 마이크로 계좌에서 buy_mult 곱·누적 차감 반복 시 경계값에서 float 표현오차(0.1+0.2!=0.3)로 1주 더/덜 사거나 현금부족 판정이 뒤집힐 수 있다. int floor 가 보수적이라 과매수 직결은 아니나 경계 회계 판정 결정성이 떨어짐.
- **권고**: 현금·금액 비교(현금부족, 명목캡, budget 차감)를 Decimal 또는 round(...,2) 정규화로 통일해 경계 결정성 확보. 우선순위 낮음.
- **근거**: executor.py:67,72; guardrail.py:350; paper.py:84; 전역 grep Decimal 부재

#### Minor-33 · `run_exit.py:53,86 / panic_exit.py:110 (datetime.now().date())` _(team 블루)_
- **요지**: 청산경로 end_excl·session 폴백이 KST 로컬 date — 진입경로의 ET 세션 통일과 불일치
- **상세**: (1) run_exit.py:86 end_excl=(datetime.now().date()+1)은 KST 로컬 날짜 기반이다. run_live.py:104 는 같은 값을 ET 세션(session+timedelta(days=1))으로 산출. KST date 는 항상 ET 세션일 이상이라 under-fetch 는 없고 staleness 게이트(:92)도 ET today 로 비교돼 실해는 없으나, 팀이 의도한 'ET 세션경계 통일'을 깨는 일관성 결함이며 DST 전환일에 KST↔ET 날짜차가 변동해 추론을 어렵게 한다. (2) :53·panic_exit.py:110 의 today=(session or datetime.now().date()) 폴백은 last_completed_session()==None(비정상) 시 KST date 를 KillSwitch namespace today 로 쓴다. run_live.py:100-102 는 같은 None 을 하드 거부하는데 청산경로는 조용히 KST 폴백.
- **권고**: run_exit.py:86 end_excl 을 보유한 session 에서 (session+timedelta(days=1))로 산출해 진입경로와 통일. session None 폴백은 panic 유지, run_exit 는 now_et().date() 로 폴백해 ET 기준 유지.
- **근거**: run_exit.py:53,86; panic_exit.py:110; run_live.py:100-104; calendar_util.py:22-24 now_et 미사용

#### Minor-34 · `README.md:328-334 (청산 cron 예시) + run_exit.py:12 docstring` _(team 블루)_
- **요지**: 문서 청산 cron 이 DST 미반영 고정 KST시각 — EST(겨울)엔 마감 전 ~1시간 청산 미커버
- **상세**: 장중청산 cron 예시가 고정 KST '*/15 23,0-4 * * 1-5'(KST 23:00~04:59)다. 미 정규장 KST 윈도는 DST 로 1시간 이동: EDT 22:30~05:00, EST 23:30~06:00. EST 에선 세션이 06:00 까지인데 cron 은 04:59 까지만 돌아 05:00~06:00 KST(마감 포함 마지막 1시간)를 통째 누락 → 마감 직전 200MA이탈·손절 점검이 EST 기간 내내 ~75분 전 종료. EDT 도 마지막 발사 04:45 는 마감 15분 전, 개장 첫 30분 미커버. market_open 게이트가 잘못된 거래는 막지만 '실제 마감 직전 청산' 타이밍 보장이 DST 의존적으로 깨짐.
- **권고**: 청산 cron 예시를 두 시즌 커버하도록 KST 22:00~06:00('*/15 22-23,0-5 * * 1-5')로 넓히거나 UTC(NY 13:30~21:00 UTC)로 표기하고 DST 가 cron 자동조정 안 됨을 명시. README.md:329 'DST 는 market_open 게이트가 흡수' 문구가 타이밍 커버리지까지 보장하는 것으로 오독되지 않게 보완.
- **근거**: README.md:333-334 `*/15 23,0-4 * * 1-5`; run_exit.py:12 동일 예시; README.md:328

#### Minor-35 · `requirements_vm.txt (certifi 부재) vs requirements.txt:11 + data.py:10,15` _(team 블루)_
- **요지**: certifi가 VM requirements에서 빠져 버전 미고정(공급망 드리프트)
- **상세**: data.py:10 가 무조건 import certifi 하고 :15 certifi.where() 호출하며 data.py 는 run_live.py:19(VM 라이브 진입)이 import 한다. requirements.txt:11 은 certifi==2026.5.20 으로 핀했지만 requirements_vm.txt 엔 certifi 항목이 없다. certifi 는 requests 전이의존(>=2023.5.7)이라 자동 설치돼 ModuleNotFoundError 로 죽진 않으나, VM 은 핀 안 된 최신 certifi 를·PC 는 2026.5.20 을 받아 CA 번들 신뢰스토어가 서로 달라 yfinance/FMP TLS 동작이 미묘히 갈리고 재현성이 떨어진다. 한글 유저명 ASCII-CA 우회(data.py:13-23)는 VM(영문 가정) 무발동이라 VM CA 경로는 전적으로 설치된 certifi 버전에 좌우.
- **권고**: requirements_vm.txt 에 certifi==2026.5.20 명시 추가해 PC 와 동일 버전 고정(전이의존 자동해결에 맡기지 말 것).
- **근거**: data.py:10,15, run_live.py:19; requirements.txt:11; requirements_vm.txt certifi 부재; requests Requires-Dist certifi>=2023.5.7

#### Minor-36 · `data_cache/ — 188개 구포맷 CSV ({ticker}_{start}_{end}.csv)` _(team 블루)_
- **요지**: 구 캐시키 포맷 188개 OHLCV CSV가 현 load()에 안 읽히는 영구 orphan
- **상세**: data.py:47 현 load() 는 `{ticker}_{start}.csv`(end 미포함)으로만 읽고 쓴다. 디스크 189개 중 188개가 구포맷 `{ticker}_{start}_{end}.csv`(신포맷 0개). 같은 종목이 여러 날짜범위로 중복(AAPL 5개 등 거의 모든 S&P100 종목 4개씩). 이 구포맷은 현 load() 의 조회에 절대 매칭 안 돼 실거래에 잘못 쓰일 위험 없음(무해 orphan). _purge_legacy(:34-40)는 그 종목·그 start 로 load 호출될 때만 지우는데 백테스트 start(2014/2016/2018)가 라이브 롤링 start 와 달라 대부분 영영 호출 안 됨→영구 잔존. 전부 22일+ stale. 디스크 낭비·위생 문제.
- **권고**: data_cache/ 의 구포맷 파일 일괄 삭제(현 load() 가 재다운로드). 또는 _purge_legacy 를 시동 시 1회 전체 스윕하도록 보강. 실거래 영향 없어 우선순위 낮음.
- **근거**: data.py:47,37; ls data_cache 신포맷 0·구포맷 188; mtime 2026-06-01

#### Minor-37 · `state/killswitch.json (113B, mtime 2026-06-01)` _(team 블루)_
- **요지**: 디스크 killswitch.json이 구 스키마·paper 스케일·2025 날짜 stale 잔재
- **상세**: 내용 {halted:false, reason:"", day:"2025-01-02", day_start_equity:100000.0, errors:0}. 현 guardrail.py:180-183 스키마는 last_traded_day·recent·last_equity·hwm 포함인데 디스크엔 결측(구 스키마)이고 day 가 1.5년 전, equity 가 paper $100k 스케일. paths.py:16-23 가 STATE_DIR 기본을 %LOCALAPPDATA%\ustrade 로 두므로 USTRADE_HOME 미설정 시 이 파일은 안 읽힘. guardrail.py:176 은 namespace 지정 시 killswitch.{namespace}.json 사용(run_live.py:129 ks_namespace=broker_kind)인데 이 파일은 무네임스페이스라 toss/paper 어느 쪽도 미매칭→무해 orphan. 단 USTRADE_HOME=프로젝트폴더로 잘못 설정 시 구 baseline($100k vs toss $100)·stale day 가 false-halt 유발 가능(축④/⑤ 영역, 여기선 위생 관점).
- **권고**: 프로젝트폴더 state/killswitch.json 삭제(런타임은 LOCALAPPDATA 또는 명시 USTRADE_HOME 하위에서 재생성). go-live 전 USTRADE_HOME 설정 시 구파일 미동반 주의.
- **근거**: state/killswitch.json 내용; guardrail.py:176,180-183; run_live.py:129; paths.py:20-23

### Info (27)

#### Info-1 · `전 repo grep — 시크릿 하드코딩/위험 원시함수/평문유출 부재(positive control)` _(team 레드)_
- **요지**: 하드코딩 시크릿·eval/exec/pickle/shell=True 없음, gitignore 유효
- **상세**: 양성 대조: (1) TOSS/TELEGRAM/FMP 키 전부 os.environ/load_key 로만 로드, 소스·캐시·로그·state 평문 시크릿 0건. (2) fmp_client.py:95 가 HTTPError apikey 를 *** 마스킹, 캐시키 MD5. (3) toss.py 가 Authorization 헤더를 로그/예외 미echo. (4) 전 repo eval/exec/os.system/pickle.load/yaml.load/subprocess/shell=True/Invoke-Expression/verify=False 0건. (5) git ls-files 상 state/·logs/·data_cache/·fmp_cache/·*.key·*.pem·.env 추적 0건, gitignore 충분. (6) deploy_push.ps1·vm_update.ps1 에 PAT 평문 미박힘(& git 콜연산자, IEX 없음). (7) SESSION_HANDOFF/README 에 실 토큰값 0건.
- **권고**: 현 위생 양호 — 회귀 방지로 gitleaks 류 pre-commit 훅 추가해 시크릿 평문 커밋을 CI 게이트로 차단.
- **근거**: fmp_client.py:24-30,95; broker/toss.py:90-92,121; notify.py:18,23,28; .gitignore:1-12; git ls-files; tools/deploy_push.ps1:35-46; tools/vm_update.ps1:30-45; tools/SETUP_GITHUB.md:19-24

#### Info-2 · `review.py:30-35,241,257-259 (자동튜닝 안전 envelope)` _(team 레드)_
- **요지**: cost_buffer 자동튜닝은 상·하한·스텝·표본 가드로 폭주/드리프트 차단(양호)
- **상세**: 자동튜닝 드리프트 점검: 자기 파라미터 영속화는 cost_buffer 하나로 한정, 다중 가드로 폭주 불가. (1) 하드클램프 [0.003,0.010](31,257,259), (2) 1회 ±0.002 변경폭 제한(33,258), (3) MIN_SAMPLE=8 미만이면 튜닝 보류(34,254), (4) 읽을 때 재클램프(241). 디스크 tuning.json 부재 → default 0.005. cost_buffer 는 '무엇을/언제 거래'에 영향 0(매수 현금쿠션만). 전략·신호·리스크 한도는 자동변경 안 함. 드리프트/폭주 경로 없음.
- **권고**: 현 가드 유지. 추후 다른 파라미터를 자동튜닝 대상에 추가 시 동일 clamp+step+min_sample envelope 적용.
- **근거**: review.py:31,33,34,241,257-259; state/tuning.json 부재

#### Info-3 · `state/killswitch.json (디스크, 구스키마·stale)` _(team 레드)_
- **요지**: 비네임스페이스 killswitch.json 이 18개월 묵은 paper 상태 — 라이브 무해(namespace 분리)
- **상세**: 디스크 state/killswitch.json: {halted:false, day:'2025-01-02', day_start_equity:100000.0, errors:0} — halt_kind·recent·hwm·last_traded_day 신규키 부재, day 약 18개월 stale, day_start_equity=100000 은 paper 잔재. guardrail._load 의 {**default,**loaded}(194)가 누락키 보강해 로드 손상/예외 없음. 토스 라이브는 namespace 분리로 killswitch.toss.json 을 읽으므로(guardrail.py:176, run_live.py:129) 이 파일 미참조 → 무해. resume_if_new_day 가 새 거래일 재seed. 순수 stale 위생.
- **권고**: 실거래 전환 전 state/ 초기화하거나 namespace 분리 정상동작(killswitch.toss.json 신규 생성) 1회 검증. 기능 결함 아님.
- **근거**: state/killswitch.json; guardrail.py:176,180-194,273; run_live.py:129; killswitch.toss.json 미존재

#### Info-4 · `requirements.txt vs requirements_vm.txt (pytz/dateutil/lxml/bs4 등 7종 비대칭) + calendar_util.py:12-17` _(team 레드)_
- **요지**: 두 requirements 가 7개 패키지 비대칭 — pytz 는 VM 에만, PC tz 폴백 깨질 수 있음
- **상세**: comm 대조: requirements.txt 에만 backtrader/certifi/matplotlib/schedule/vectorbt, requirements_vm.txt 에만 beautifulsoup4/curl-cffi/lxml/multitasking/peewee/python-dateutil/pytz. calendar_util.py:12-17 은 zoneinfo 실패 시 import pytz 폴백하는데 pytz 가 requirements.txt(PC)엔 없음 → PC 에서 tzdata 부재 시 폴백도 ImportError. Python 3.14 tzdata 동봉이라 드물지만 PC↔VM tz 라이브러리 출처가 달라 DST/세션경계 동작이 환경별로 갈릴 잠재 드리프트. 전이의존 한쪽만 명시핀은 재현성 정책과 상충.
- **권고**: 실거래 런타임 필요 전이의존(certifi·pytz·dateutil·lxml·bs4·curl-cffi·multitasking·peewee)을 두 파일에 동일 핀 통일하거나 단일 requirements+extras 로 일원화.
- **근거**: comm -23/-13; calendar_util.py:12-17; requirements.txt:1(재현성)

#### Info-5 · `universe.py:3-5,21-22` _(team 레드)_
- **요지**: 백테스트 유니버스가 정적 현재구성 → 생존편향(코드에 명시됨)
- **상세**: UNIVERSES 는 '오늘의 구성종목' 정적 리스트 → 상장폐지/탈락 누락으로 백테스트 절대성과가 낙관적(survivorship bias). 코드가 명시(3-5,21-22). 라이브 선정엔 정상(현재 멤버 거래). 다만 편향된 백테스트가 robust 파라미터 선정에 쓰이면 라이브 config 결정이 낙관 편향될 수 있음.
- **권고**: 엄밀 검증 필요 시 point-in-time 구성종목 도입. 현 단계는 학습/개발용임을 운영 판단에 반영.
- **근거**: universe.py:3-5,21-22

#### Info-6 · `live_demo.py / live_rebalance.py / live_filter_demo.py` _(team 레드)_
- **요지**: 3개 데모 모듈은 어디서도 import 안 됨(독립 CLI) — 운영 경로와 혼동 가능
- **상세**: 세 파일은 다른 모듈서 import 안 됨(grep 0). 각자 __main__ 데모 CLI 로 운영 경로(run_live→live_engine)와 무관. 죽은 코드는 아니나(엔트리포인트) 혼동 가능. live_rebalance 는 live_engine 공유한다고 live_engine.py:4 주석 언급하나 실제 import 참조 없음.
- **권고**: 데모임을 파일 상단 명확 표기하거나 미사용이면 정리. 운영 경로와 구분.
- **근거**: grep import 0건; live_engine.py:4

#### Info-7 · `run_live.py:135-138 (_journal) + logsetup.py:20-33 + notify.py:57-61` _(team 레드)_
- **요지**: 주문 audit trail 은 양호하나 ustrade.log 핸들러 설정 실패가 조용히 삼켜짐
- **상세**: 주문 audit(orders/account/weights/daily_pnl)은 runs.jsonl 에 구조적 기록 양호. 다만 logsetup._configure 핸들러 설정 전체가 except: pass(32-33) — LOG_DIR 생성/핸들러 부착 실패 시 회전 파일 로그(ustrade.log)가 통째 비활성인데 흔적 없음. notify 도 이 로거 의존(57-61, 역시 except pass)이라 채널 미설정 시 유일 기록인 ustrade.log 마저 조용히 사라질 수 있음.
- **권고**: logsetup 설정 실패 시 최소 sys.stderr 1회 경고 출력(print to stderr)해 cron stderr 로그에라도 남길 것. 완전 무성 지양.
- **근거**: logsetup.py:32-33; notify.py:57-61; runs.jsonl audit 는 run_live.py:135-138 정상

#### Info-8 · `.gitignore vs git ls-files` _(team 레드)_
- **요지**: 런타임 데이터 gitignore 적정, 커밋된 런타임 0건 확인
- **상세**: data_cache/·fmp_cache/·state/·logs/·results/ 모두 .gitignore 포함(2-6), git ls-files 추적 0건. .env·*.key·*.pem 도 ignore. archive/reviews/ 는 추적되나 과거 audit markdown(런타임 아님)이라 무해. fmp_cache 44 JSON 유효(corrupt/empty 0), 해시 파일명 orphan 없음. mtime 23일 stale 이나 TTL 7일+RateLimited 시 stale 폴백이라 다음 실행 재fetch(설계상 허용 degraded).
- **권고**: 현 .gitignore 유지. 향후 *.token/*.secret 패턴 추가 고려.
- **근거**: .gitignore:2-6; git ls-files 0; fmp_cache 44 JSON corrupt 0; fmp_client.py:45 TTL 7d,:64 stale 폴백

#### Info-9 · `broker/managed.py:33-35,189-194 + broker/toss.py:268-275 (_norm vs 토스 native 표기)` _(team 레드)_
- **요지**: _norm 이 토스 native 표기 전부를 canonical 화한다는 가정 — 표기 미확정이 슬리브 방어 전제
- **상세**: _norm 은 upper·'.'→'-'·trim 만(33-35). place_order 가드(189-194)·protected 비교가 _norm 에 의존. 토스 US 클래스주 native 표기가 'BRK.B' 라는 가정(toss.py:271 주석이 '미확정' 인정)이 틀려 토스가 'BRKB'/'BRK B'/접미사 변형을 주면 _norm 결과가 protected 와 불일치해 보호종목이 managed 가드를 우회(SELL 차단 실패) 가능. tests_managed.py:114-121 은 'BRK.B/BRK-B/공백/소문자' 만 커버, 실 토스 표기 미검증.
- **권고**: go-live 전 toss_check.py 로 실제 토스 US 클래스주/특수티커 native 표기 실증, _norm 의 canonical 접힘 회귀 테스트 추가. 정규화 후에도 protected 불일치 보유종목 발견 시 fail-closed.
- **근거**: broker/managed.py:33-35,189-194; broker/toss.py:268-275; tests_managed.py:114-121

#### Info-10 · `fmp_client.py:20,28-29 (load_key / KEY_FILE)` _(team 블루)_
- **요지**: FMP key plaintext-file fallback (홈 디렉토리 외부경로 — repo 밖)
- **상세**: load_key()는 env FMP_API_KEY 부재 시 ~/.config/fmp_api.key 평문 파일에서 키를 읽는다. 이 경로는 홈(~/.config)이라 repo 밖이므로 git 커밋 위험 없고(.gitignore *.key 와 무관한 외부경로) 실제 누출 미확인. 다만 시크릿이 평문 파일로 디스크에 상존하는 2차 보관면 존재. VM 은 env 만 쓰는 정책이라 이 경로는 PC 로컬 한정.
- **권고**: VM/실거래는 env FMP_API_KEY 만 사용(현 코드가 env 우선이라 이미 충족). PC 키파일 사용 시 파일 권한 제한, 정책상 키파일 의존 제거 검토.
- **근거**: fmp_client.py:20 KEY_FILE=~/.config/fmp_api.key; :28-29 read_text(utf-8-sig)

#### Info-11 · `broker/toss.py:33,90-91 (DEFAULT_BASE_URL / __init__)` _(team 블루)_
- **요지**: Toss base_url 기본값이 운영 openapi 엔드포인트(safe-default 관점)
- **상세**: DEFAULT_BASE_URL='https://openapi.tossinvest.com'(운영)이며 api_key/api_secret 미지정 시 env 자동 로드. 시크릿 취급 자체는 안전(env-only, 하드코딩 없음)하나 기본값이 운영 서버라 자격증명이 환경에 있는 한 임포트/인스턴스화만으로 실거래 API 를 가리킨다. 시크릿 누출이 아니라 안전기본값 관점의 정보성 지적.
- **권고**: 시크릿 누출 결함 아님 — env-only 정상. 운영/모의 기본 분리는 타 축과 교차확인 권장.
- **근거**: broker/toss.py:33 DEFAULT_BASE_URL; :90-91 env 로드

#### Info-12 · `tools/deploy_push.ps1:46,55, tools/vm_update.ps1:20-57, tools/SETUP_GITHUB.md:24-25` _(team 블루)_
- **요지**: 배포 스크립트에 명령주입·PAT 평문 취급 없음(점검 결과 결함 없음)
- **상세**: deploy_push.ps1 은 변수를 Invoke-Expression 없이 `& git ... commit -m $Message`(:46)처럼 인자로 전달 — 셸 보간/주입 경로 없음. PAT/시크릿을 echo·로그·커밋하지 않음. vm_update.ps1 도 git fetch/pull(--ff-only)·Get-CimInstance 가드만 사용, 외부입력 식실행 없음. SETUP_GITHUB.md:24 는 토큰을 채팅에 붙여넣지 말라 명시하고 Credential Manager/read-only PAT 안내. 결함 없음을 명시 기록.
- **권고**: 현 상태 유지. vm_update.ps1 의 신뢰경계(origin/main 코드를 VM 이 그대로 실행)는 PC+PAT 보유자=봇 실행권한임을 운영상 인지.
- **근거**: tools/deploy_push.ps1:46,55, vm_update.ps1:20-26,44-57, SETUP_GITHUB.md:24-25

#### Info-13 · `dashboard/server.py:44 (no CORSMiddleware)` _(team 블루)_
- **요지**: CORS 미들웨어 부재 — 교차출처 브라우저 호출 차단(양호)
- **상세**: server.py 에 CORSMiddleware/allow_origins 설정이 없어 기본적으로 교차출처 요청에 ACAO 헤더를 안 준다. 다른 사이트 JS 가 control 을 호출해도 응답을 못 읽음(양호). control 이 POST+커스텀헤더(X-Dash-Token)라 CSRF 도 어렵지만 토큰 가드가 유일 방어선임은 위 Major 와 연계.
- **권고**: 현 상태(CORS 미설정) 유지. 추후 allow_origins 추가 시 와일드카드(*) 금지.
- **근거**: dashboard/server.py 전체 grep cors/allow_origin/CORSMiddleware 미존재

#### Info-14 · `live_engine.py:194-204,228-229 + live_risk.py:42-44 + broker/managed.py:154-166,189` _(team 블루)_
- **요지**: 레짐 OFF 의 빈 비중은 skip 게이트 이후라 의도대로 전량 청산됨(검증 결과 안전)
- **상세**: STRAT-3 skip 게이트(:197 `if not weights`)는 apply_overlay 호출 前 선택결과에만 적용된다. 레짐 OFF 시 apply_overlay 가 {} 반환(live_risk.py:44)하고 weights={} 로 Executor.plan({})이 전 관리포지션을 SELL — '데이터결함 공집합=보류' vs '레짐 OFF=의도적 현금화' 를 구분하는 의도된 설계. 이 청산이 슬리브를 안 깨는지 전수 확인: Executor 가 보는 positions 는 managed basis 한정(managed.py:154-166, 보호분 basis<=0 continue), SELL 은 managed.py:189 필터(s∈managed & s∉protected) 통과해야 하므로 protected 는 절대 미매도. 결함 아님(안전).
- **권고**: 조치 불요. leverage ETF 11종이 모두 protected 로 들어가는지 라이브 전 toss_setup 출력으로 1회 육안 확인 권장.
- **근거**: live_engine.py:197,202-204,228-229; live_risk.py:42-44; managed.py:154-166,189

#### Info-15 · `broker call-graph: live_engine.py:218,229 / run_exit.py:79,115 / panic_exit.py:75 / run_live.py:30-35` _(team 블루)_
- **요지**: toss.place_order 직접도달 우회경로 없음 — 모든 실주문이 가드/슬리브 통과(검증)
- **상세**: place_order 전수추적: toss 주문은 (a) 진입/장중청산은 GuardedBroker→ManagedBroker→TossBroker(live_engine.py:218, run_exit.py:79), (b) 비상청산은 ManagedBroker→TossBroker 직접(panic_exit.py:75, GuardedBroker HALT 게이트만 의도적 우회, SELL-only·슬리브필터 유지)뿐. raw TossBroker.place_order 직접 호출 운영경로 없음. Executor.rebalance 직접호출은 live_demo/live_rebalance 뿐인데 둘 다 PaperBroker. make_broker(run_live.py:30-35)는 toss 를 항상 ManagedBroker 로 감쌈. 가드레일·슬리브 우회 불가 — 결함 아님.
- **권고**: 조치 불요. 향후 새 진입스크립트 추가 시 'toss 는 반드시 ManagedBroker 래핑·GuardedBroker 경유(또는 panic SELL-only 직접)' 규칙 유지.
- **근거**: grep place_order 전수; run_live.py:34-35 ManagedBroker 래핑; live_demo/live_rebalance 만 PaperBroker

#### Info-16 · `broker/managed.py:70-75,189,192,215,249-255` _(team 블루)_
- **요지**: protected 가 어떤 경로로도 managed 에 편입·매도되지 않음 — 불가침 슬리브 검증 통과
- **상세**: 보호분 불가침 코드 증명: (1) load_sleeve(:70-75)가 protected∩managed/pending 제거(disjoint). (2) place_order SELL(:189)은 s∉managed 또는 s∈protected 면 REJECTED; BUY(:192)는 s∈protected 면 REJECTED. (3) BUY 의 pending 영속도 protected 면 192 에서 먼저 거부→pending 진입 불가. (4) record_fills(:215)는 protected 면 continue. (5) reconcile_basis(:249-255)는 _pending 만 순회하는데 pending 엔 protected 없음→흡수 불가. 진입·장중청산·리밸런스·레짐현금화·비상청산 5경로 어디에도 누락 없음.
- **권고**: 조치 불요. 라이브 후 logs/runs.jsonl·exits.jsonl 의 SELL 심볼에 protected 가 한 번도 안 나오는지 주기 점검 권장.
- **근거**: managed.py:70-75,189,192-194,215,249-255; tests_managed.py:102-110,134

#### Info-17 · `run_live.py:41-47 + heartbeat.py:28-35 (저널 비원자 append)` _(team 블루)_
- **요지**: runs/exits/panics.jsonl 저널이 비원자 append — 크래시 시 마지막 줄 손상(소비자 tolerant 라 영향 제한)
- **상세**: killswitch.json(guardrail.py:197-214 temp+fsync+replace)·toss_sleeve.json(managed.py:79-103)은 원자적 쓰기로 보호되나, audit trail 저널은 f.open('a') 후 단순 append(run_live.py:46-47)라 쓰기 도중 크래시/전원차단 시 마지막 레코드가 잘릴 수 있다. 소비자(heartbeat._traded_sessions, review.load_journals)가 줄단위 try/except 로 손상줄을 건너뛰므로 정지는 없고, 최악은 heartbeat 가 세션 실행기록을 못 읽어 false dead-man 알림(fail-safe 방향). 5MB 회전 정상.
- **권고**: audit 무결성이 중요하면 저널도 write 후 flush+fsync 추가. 영향이 fail-safe 방향이라 우선순위 낮음.
- **근거**: run_live.py:41-47(비원자) vs guardrail.py:197-214(원자적); heartbeat.py:28-35(tolerant)

#### Info-18 · `broker/guardrail.py:185-195 (_load) + state/killswitch.json` _(team 블루)_
- **요지**: 손상 killswitch.json 은 fail-closed(halted=True)로 안전; namespace 분리로 라이브 미사용(확인)
- **상세**: _load(:185-195)는 JSONDecodeError/OSError/ValueError 시 {halted:True}로 fail-closed 하여 손상 state 가 주문을 차단 — 무인 안전기준 충족. 디스크 무namespace killswitch.json 은 구포맷이나 라이브 toss 경로는 namespace='toss'(killswitch.toss.json), paper 는 'paper' 를 쓰므로 무namespace 파일은 미로드. 설령 로드돼도 {**default, **loaded}(:194)로 결측키 보강돼 KeyError 없이 안전. fail-safe 설계 의도대로 작동 확인.
- **권고**: 조치 불필요(확인용). STATE_DIR 기본은 %LOCALAPPDATA%\ustrade(paths.py:20-27)라 프로젝트 파일은 USTRADE_HOME 지정 시에만 의미.
- **근거**: guardrail.py:185-195,176,194; paths.py:20-27

#### Info-19 · `review.py:31-34,257-259,241,296-300 (run_review/compute_tune)` _(team 블루)_
- **요지**: 자동튜닝 드리프트 가드는 견고; 단 자기검증 위반은 알림만 하고 자동정지 안 함
- **상세**: cost_buffer 자동변경은 [0.3%, 1.0%] 하드클램프+1회 ±0.2%p 스텝제한+MIN_SAMPLE 8 미만 보류로 폭주 불가, read_tuned_cost_buffer(:241)가 읽을 때 재클램프(이중)해 손상 tuning.json 도 범위 밖으로 못 샌다. 영속 대상은 cost_buffer 하나뿐, 전략·신호·리스크 한도는 자동변경 안 함 — 드리프트/과최적화 위험 없음(양호). 단 verify_invariants 가 보호종목 매매·더블바이를 잡아도 run_review 는 텔레그램 CRITICAL 만 보내고 자동 HALT 는 안 건다(:296 주석 '사람 결정'). 의도된 정책이나 review 미스케줄(아래 Major)·알림 무검증(notify Major)이면 위반이 아무에게도 안 닿을 수 있다.
- **권고**: 드리프트 가드 자체는 변경 불필요. review 를 스케줄 등록하고 중대 위반 시 알림에 더해 선택적 KillSwitch.trip(manual) 옵션 검토.
- **근거**: review.py:31-34,257-259,241,296-300

#### Info-20 · `live_select.py:3-7,32-34 + live_select_canslim.py:34-36,53-64 + run_live.py:63-64` _(team 블루)_
- **요지**: 라이브 선정에 lookahead 없음; 펀더 스크린 무력화는 플래그로 관측됨
- **상세**: canslim _mom_12_1 은 s.iloc[-21]/s.iloc[-252]로 스킵 모멘텀을 쓰고 가격 스크린은 패널 최종봉(직전 종료세션 종가)만 사용→미래데이터 참조 없음. momentum 경로도 .iloc[-1] 최종봉 기준이라 동일 안전. FMP 무료티어는 현재 스냅샷뿐이라 백테스트엔 못 쓴다고 명시(live_select.py:3-4). 펀더 대량결측(429) 시 스크린이 모멘텀만으로 무력화되나 screen_degraded 플래그가 켜지고 run_live.py:63-64 가 경보 발송→관측가능한 열화(양호). survivorship 은 유니버스가 고정 sp100/diversified 라 비해당.
- **권고**: 결함 없음. 단 screen_degraded 경보가 notify 무검증 버그(위 Major)로 전달 실패하면 '모멘텀만으로 거래 중' 상태가 은폐되니 notify 수정과 함께 봐야 함.
- **근거**: live_select_canslim.py:34-36,53-64; live_select.py:3-7,32-34; run_live.py:63-64

#### Info-21 · `scheduler.py:18,32-37 (RUN_TIME 호스트 로컬)` _(team 블루)_
- **요지**: scheduler.py 가 호스트 로컬TZ=KST 가정 — UTC VM 에서 06:10 은 미장 개장 전
- **상세**: scheduler.py 는 schedule.every().day.at(RUN_TIME)(기본 '06:10')을 호스트 로컬시각으로 해석한다. 호스트가 KST 가정(06:10 KST=미장 마감 후). UTC VM 이면 06:10 UTC=15:10 KST=개장(22:30 KST) 한참 전에 깨어나 last_completed_session 이 의도와 다른 세션을 가리킬 수 있다. 단 scheduler.py 는 docstring 에서 '대안·개발/단순배포용, 운영 권장은 cron+run_live 원샷'이라 운영 권장 경로 아니며, 세션 자체는 ET 자동판정이라 잘못된 세션 거래로 직결되진 않음.
- **권고**: scheduler.py 운영 사용 시 호스트 TZ=KST 보장(Set-TimeZone/TZ)하거나 RUN_TIME 을 now_et 기반 게이트로 해석. 권장 경로(cron one-shot)에선 영향 없음.
- **근거**: scheduler.py:18,36,1-3; README.md:336

#### Info-22 · `dashboard/build_data.py:202-203,214,349 + run_live.py:91` _(team 블루)_
- **요지**: 대시보드 P&L 'today/month' 버킷이 KST datetime.now() 기준 — ET 세션 아님(표시 전용)
- **상세**: build_data.py:202 now=datetime.now()(KST), :203 today/month 문자열로 runs.jsonl 의 r['ts'](run_live.py:91 datetime.now().isoformat(), 역시 KST)를 비교해 수수료/실현손익을 집계. 양쪽 KST 라 내부 일관성은 있으나 거래 세션 기준일은 ET('session' 필드)이라 대시보드 '오늘' 집계가 ET 세션 경계와 어긋날 수 있다(자정~새벽 KST 에 한 칸 밀림). 실거래·회계 무관 표시 전용. 같은 파일 next_session_iso()(:349)는 올바르게 now_et() 사용 — 혼용 존재.
- **권고**: 표시 정합이 필요하면 ts(KST) 대신 record 의 'session'(ET)으로 버킷 구성 또는 now_et() 통일. 표시 전용이라 우선순위 낮음.
- **근거**: build_data.py:202-203,214,349; run_live.py:91

#### Info-23 · `requirements.txt vs requirements_vm.txt (라인 단위 diff)` _(team 블루)_
- **요지**: PC는 백테스트 무거운 의존, VM은 런타임 전이의존 명시 — 의도된 분리
- **상세**: 공통 핵심 4종(requests/pandas/numpy/yfinance)·캘린더 버전 일치. PC 전용 matplotlib/backtrader/vectorbt 는 engines/_plot·bt_runner·vbt_runner(백테스트·플롯 전용)에서만 import 되고 라이브 경로(run_live→live_engine)는 미import 라 VM 부재 무해. schedule(PC only)도 scheduler.py:32 try/except ImportError 가드 후 import 되고 scheduler 자체가 운영 비권장이라 무해. requirements_vm.txt 가 pandas_market_calendars(언더스코어), requirements.txt 가 pandas-market-calendars(하이픈)로 표기 다르나 pip 정규화상 동일.
- **권고**: 현 분리 합리적. 표기(언더스코어 vs 하이픈) 통일 권장(기능 영향 없음).
- **근거**: requirements.txt/requirements_vm.txt 비교; live import 추적 run_live.py:13-25, live_engine.py:7-13; scheduler.py:32-35 가드

#### Info-24 · `logs/alerts.log(583B)·logs/runs.jsonl(164B) (mtime 2026-06-01)` _(team 블루)_
- **요지**: 프로젝트폴더 로그도 paper 잔재 stale orphan — 라이브 경로 미사용
- **상세**: logs/runs.jsonl 은 단일 paper broker·equity 100000·orders [] 레코드. alerts.log 는 paper 체결·'미실행 감지' 경고. 현 코드(logsetup.py·run_exit.py:37·build_data.py:140-146)는 paths.LOG_DIR(=%LOCALAPPDATA%\ustrade\logs 기본)에서 읽고 쓰므로 USTRADE_HOME 미지정 시 이 프로젝트폴더 logs/ 는 orphan. 계좌성 PII(자산액·체결)가 OneDrive 동기화 폴더에 평문 잔존 — paths.py:3-4 가 피하려던 상황의 잔재. 시크릿 평문(토큰/키)은 없음.
- **권고**: 프로젝트폴더 logs/ 정리(삭제). 실로그는 LOCALAPPDATA 하위(동기화 폴더 밖)에 보관 유지.
- **근거**: logs/runs.jsonl·alerts.log; paths.py:28; build_data.py:140-146; mtime 2026-06-01

#### Info-25 · `fmp_cache/ — 44개 JSON (mtime 2026-06-01)` _(team 블루)_
- **요지**: 22일+ 묵은 FMP 캐시지만 7일 TTL 만료로 실거래에 안 쓰임
- **상세**: fmp_cache/ 44개 전부 23일 stale 이나 cache_ttl_days=7.0(fmp_client.py:39,45)로 get()(:53-60)이 신선도 검사 후 7일 초과면 캐시 미사용·재호출(:61-69)한다. 묵은 펀더 오용을 TTL 가 차단. RateLimited(429/402) 소진 시에만 만료캐시 폴백(:63-65, 의도된 graceful degradation). 손상/빈 파일 없음. USTRADE_HOME 미지정 시 라이브는 %LOCALAPPDATA%\ustrade\fmp_cache 를 보므로 프로젝트폴더 캐시는 orphan.
- **권고**: 조치 불필요(TTL 차단). 위생상 정리 가능. 전 파일 같은 mtime 이라 일괄만료 후 재호출 버스트(44건)가 FMP 250req/day 무료쿼터를 소진할 수 있으니 min_interval·402 백오프 흡수 여부 운영 관찰.
- **근거**: fmp_client.py:39,45,56,59-69; ls fmp_cache 44개; FMP_CACHE=paths.py:30

#### Info-26 · `results/ (PNG16+CSV4, mtime 2026-06-01) · archive/reviews/` _(team 블루)_
- **요지**: 백테스트 산출 PNG/CSV는 라이브 무관 stale 위생; archive 일부는 git 커밋됨
- **상세**: results/ 는 백테스트·sweep·walkforward 산출 PNG16·CSV4(전부 2026-06-01, 손상 없음), 라이브 미사용→실거래 영향 0. git ls-files 교차확인: state/logs/data_cache/fmp_cache/results 는 git 추적 0건(gitignore 정상). 단 archive/reviews/ 의 5개 md(teamA/B_report·cross_diff·FINAL_SYNTHESIS·best_practices_gap)는 git 커밋됨 — 런타임 데이터는 아니나 과거 audit 산출물이 repo 에 누적 중.
- **권고**: results/ 는 .gitignore 정상. archive/reviews/ 커밋 md 는 의도면 유지, 아니면 별도 리포/위키 이전. 빈 20260624 디렉토리는 본 리뷰 종료 후 정리.
- **근거**: ls results/; git ls-files state/logs/data_cache/fmp_cache/results→0건, archive .md 5건 추적; .gitignore:2-6

#### Info-27 · `live_demo.py · live_filter_demo.py · live_rebalance.py` _(team 블루)_
- **요지**: 데모/구버전 리밸런스 모듈이 어디서도 import 안 되는 죽은 코드(broken ref 없음)
- **상세**: live_demo·live_filter_demo·live_rebalance 세 파일은 다른 .py(테스트·archive 제외)에서 import/from 으로 참조되지 않는다(grep 0). live_rebalance 는 구 리밸런스 데모, live_*_demo 는 데모 스크립트로 실거래 오케스트레이션과 무관. 끊긴 참조(정의 없는 모듈)·정의 없는 이름은 전 소스 AST 파싱·import grep 0건(phantom import 0). 죽은 코드는 있으나 broken reference 는 없음.
- **권고**: 데모/실험용이고 미사용이면 삭제 또는 examples/ 격리. 사용자 미요청 삭제는 보류 — 존재만 보고.
- **근거**: grep import live_demo|live_filter_demo|live_rebalance 0건; AST PARSEFAIL 0; phantom import 0

## §5 교차 대조
- both(양팀 동시발견): 0
- A-only(레드 단독→블루 교차판정): 59 — 전건 confirm
- B-only(블루 단독→레드 교차판정): 42 — 전건 confirm
- 팀A(레드) lead findings: 59 (C3/M19/m18)
- 팀B(블루) lead findings: 42 (C0/M11/m12)

## §6 로스터
**레드팀**: Secrets & Attack-Surface Hunter · Order-Path & Guardrail Breaker · Exit & Resilience Auditor · Money & Accounting Validator · Time, Calendar & Scheduler Auditor · Observability & Silent-Failure Hunter · Strategy, Tuning-Drift & Data-Hygiene Auditor · Test-Theater & Env-Drift Auditor

**블루팀**: secret-leak-injection-auditor · dashboard-deploy-exposure-auditor · order-path-guardrail-sleeve-auditor · resilience-idempotency-state-auditor · money-accounting-fx-auditor · time-calendar-scheduler-auditor · observability-strategy-test-auditor · data-hygiene-env-drift-auditor
