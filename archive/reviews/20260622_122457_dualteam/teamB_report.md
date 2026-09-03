# Team B (정합성-호의) — counts {'critical': 0, 'major': 5, 'minor': 7}

### [Major] broker/executor.py:26-29, 60-68 (Executor.plan, buy_mult / budget headroom)
**라이브 toss 경로에서 비용버퍼(buy_mult)가 1.0 — EXEC-1 보호 무력**

plan() 은 commission/spread/slippage 를 getattr(self.broker, "_commission"/"_spread"/"_slippage", 0.0) 로 읽는다(26-28행). 그러나 이 속성들은 PaperBroker 에만 정의돼 있다(paper.py:24-26). 라이브에서 self.broker 는 GuardedBroker→(__getattr__ 위임)→ManagedBroker→TossBroker 인데, ManagedBroker 는 __getattr__ 위임이 없고(managed.py 에 명시 메서드만 패스스루) TossBroker/GuardedBroker 도 이 속성을 정의하지 않는다. 따라서 셋 다 AttributeError→default 0.0 → buy_mult=(1+0/2)(1+0)(1+0)=1.0. 결과적으로 라이브 매수는 시세 그대로 unit=price 로 사이징되어 수수료·스프레드·슬리피지 헤드룸이 0. docstring(executor.py:19-22)이 명시한 'EXEC-1: 풀투자 회전 시 수수료/슬리피지로 마지막 매수가 현금부족 거부되던 것 방지' 보호장치가 라이브에서만 조용히 꺼진다. cash_cap=$100·KMI 소수주 단계에선 절대 오버가 cents 수준이라 즉시 사고는 아니나, 명시된 안전마진이 실거래 경로에서 부재라는 사실은 검증 전제와 어긋난다. (검산: (1+0/2)*(1+0)*(1+0)=1.0 확인.)

- 근거: executor.py:26-29 getattr default 0.0; paper.py:24-26 만이 _commission/_spread/_slippage 정의; managed.py 에 __getattr__ 위임 부재(명시 패스스루만, 137-151행); guardrail.py:397-402 GuardedBroker.__getattr__ 는 _inner(ManagedBroker)로 위임 → 거기서도 미정의; grep 결과 _commission 정의는 paper.py 한 곳뿐.
- 권고: 실비용 배수를 브로커 속성 추정에 의존하지 말고 명시 파라미터로 주입(예: Executor(broker, alloc, cost_buffer=0.005)) 하거나, ManagedBroker/TossBroker 에 _commission/_spread/_slippage 를 정의(또는 ManagedBroker 가 inner 로 위임)해 라이브에서도 buy_mult>1 이 적용되게 한다.

### [Major] run_exit.py:_run_locked (107-124) + broker/managed.py:place_order(187-200)·record_fills(226-227)
**청산 SELL은 멱등키·pending 없어 늦은체결 시 다음 cron이 같은 주식 재매도(oversell)**

run_exit.py는 미 정규장 중 15분마다 호출되며(파일 헤더 cron 예시 12행) 매 실행 RunLock으로 보호되지만 already_traded/mark_traded를 쓰지 않는다(run_exit.py에 mark_traded 호출 없음). SELL 경로는 멱등 보호장치가 없다: managed.py place_order는 BUY만 _pending 영속(198-199행)하고 SELL은 그냥 통과시킨다. record_fills의 SELL 분기(226-227행)는 체결분(fq)만큼만 managed를 줄이고 pending도 dedup 마커도 남기지 않는다. _await_fills 타임아웃(run_exit.py:118 = 30s)에 SELL이 SUBMITTED(0 체결)로 남으면 record_fills가 managed를 줄이지 않고(_TERMINAL=FILLED/REJECTED/CANCELLED만, live_engine.py:60·PARTIAL/SUBMITTED 제외) 토스 holdings도 미정산이라 다음 15분 실행의 get_positions(managed.py:154-166)가 동일 수량을 그대로 노출 → check_exits가 같은 200MA/손절 트리거로 동일 심볼·수량 SELL을 또 제출(run_exit.py:111). 보유 3주에 대해 두 건의 SELL(각 3주)이 모두 토스에 열려 oversell 위험. place_order SELL 가드(managed.py:189)는 's in self._managed'만 확인하고 이미 진행 중인 매도 의도는 검사하지 않는다.

- 근거: run_exit.py:111(루프 내 무조건 place_order SELL), run_exit.py:118(timeout 30s), broker/managed.py:189·226-227(SELL pending/dedup 부재, fq만 차감), live_engine.py:60(_TERMINAL이 PARTIAL/SUBMITTED 제외), run_exit.py 전체에 mark_traded/already_traded 없음
- 권고: SELL에도 pending(또는 outstanding-order id) 추적을 도입: place_order SELL 시 미체결 매도 의도를 영속하고, get_positions/exit 평가 시 진행 중 SELL 수량을 차감하거나, run_exit가 직전 미종결 주문을 get_order로 조회해 동일 심볼 재제출을 차단. 최소한 record_fills 후 SUBMITTED 잔존 SELL이 있으면 해당 심볼을 당 실행 청산 후보에서 제외.

### [Major] broker/guardrail.py:21 STATE_FILE / :175,248,253 roll_day·last_equity / :299 check_total_drawdown·hwm ↔ run_live.py:28-38 make_broker (paper cash=100000 vs toss cash_cap=100)
**단일 killswitch.json 을 paper·toss 가 공유 → baseline·hwm 스케일 혼입 오염**

KillSwitch 는 last_equity/hwm/day_start_equity 를 단 하나의 STATE_FILE=STATE_DIR/'killswitch.json'(guardrail.py:21)에 영속하며 broker 모드 키잉이 전혀 없다. run_live.make_broker(run_live.py:28-38)는 BROKER=paper 면 PaperBroker(cash 기본 $100,000, 또는 PAPER_CASH), BROKER=toss 면 ManagedBroker(cash_cap=$100, equity≈$132)를 만든다. 두 경로 모두 live_engine.run_once→roll_day(acct.equity)(live_engine.py:203)·check_total_drawdown(line 205)을 같은 state 파일에 쓴다. roll_day 는 새 날 day_start_equity=last_equity(guardrail.py:248)로 잡고 매 실행 last_equity=equity(line 253)를 저장하며, check_total_drawdown 은 hwm=max(hwm,equity)(line 299) 단조증가로 영속한다. 따라서 개발용 paper 실행($100k 스케일)이 last_equity·hwm 을 $100,000 로 오염시키면, 다음 toss 실행에서 day_start_equity·hwm 이 $100,000 인데 실제 toss equity 는 ≈$132 → check_daily_loss 는 dd=132/100000-1≈-99.9% < -5% 로, check_total_drawdown 은 dd≈-99.9% < -20% 로 즉시 트립. 특히 total_drawdown 트립은 roll_day/resume_if_new_day 의 자동해제 대상이 아니라(halt_kind=='daily_loss' 만 해제, guardrail.py:251,264) 수동 reset 전까지 영구정지된다. paper/toss 가 같은 USTRADE_HOME(기본 동일, paths.py:16-23)을 쓰는 한 가드가 오염된다. (검산: 132/100000-1≈-0.99868, 약 -99.9% 확인.)

- 근거: guardrail.py:21(단일 STATE_FILE), :175(_default_state last_equity/hwm), :246-253(roll_day baseline=last_equity 이월·last_equity 저장), :292-308(check_total_drawdown hwm 단조·영구트립), :251 및 :263-265(daily_loss 만 자동해제, total_drawdown 제외); run_live.py:36-38(PaperBroker cash 100000 기본), :35 및 managed.py:177(toss cash_cap min), live_engine.py:203-205(roll_day·check_total_drawdown 동일 ks); paths.py:16-27(STATE_DIR 모드 무관 단일)
- 권고: killswitch state 파일을 broker 모드별로 분리하라(예 STATE_DIR/f'killswitch.{broker_kind}.json' 또는 KillSwitch 에 namespace 인자). 또는 roll_day 진입 시 저장된 last_equity 와 현재 equity 의 스케일 급변(예 10배 이상 괴리)을 감지해 baseline 을 이월하지 말고 현재 equity 로 재seed + 경고. 최소한 toss 실거래 전 paper 실행이 같은 state 를 건드리지 않도록 USTRADE_HOME 을 모드별로 분리.

### [Major] broker/toss.py:116-124, place_order via _request (243-263)
**네트워크 타임아웃이 미체결 단정으로 raise + clientOrderId 매 호출 uuid → 재시도 시 더블체결 위험**

place_order 의 POST /api/v1/orders 가 requests.RequestException(연결 끊김/읽기 타임아웃)으로 끝나면 _request 는 max_retries 소진 후 http_status=None 인 TossAPIError("network-error") 를 raise 한다(line 124). place_order 의 분류는 is_business_error()(4xx)만 REJECTED 로 흡수하고 그 외는 raise(toss.py:261-263) 이므로 network-error 는 raise 경로다. 그러나 읽기 타임아웃은 '주문이 서버에 도달해 접수·체결됐으나 응답만 못 받은' 상태일 수 있다. 이때 live_engine 은 이 주문을 orders 에 미포함(place_order 가 던졌으므로) → record_error 누적 → 다음 실행에서 동일 비중을 다시 plan 한다. clientOrderId 멱등키는 매 place_order 호출마다 uuid 로 새로 생성(toss.py:246)되므로 같은 주문이 재시도 시 '다른 주문'으로 접수돼 더블체결 위험이 있다. _request 내부 재시도(같은 body 재사용)는 멱등하지만, place_order 자체가 새로 호출되는 경로(에러카운트 후 다음 실행)는 멱등키가 달라 보호되지 않는다.

- 근거: toss.py:124 raise TossAPIError("network-error", str(e), None, None); toss.py:246 clientOrderId=uuid.uuid4().hex[:32] (매 호출 새 키); toss.py:261-263 e.is_business_error() 만 REJECTED, 그 외 raise; live_engine.py:208-216 raise 시 해당 주문 orders 미기록·record_error
- 권고: clientOrderId 를 OrderRequest 기준 결정론적 키(예: 날짜+symbol+side+qty 해시)로 만들거나, 주문 전송 직후 타임아웃이면 get-orders 로 clientOrderId 존재 여부를 조회해 접수 확인 후에만 raise. 최소한 reconcile/_await_fills 가 '제출은 됐으나 응답 누락' 주문을 복구할 수 있도록 placed-but-unacked 상태를 표면화할 것.

### [Major] run_exit.py:_run_locked (라인 86-99) / live_exit.py:check_exits (라인 52)
**청산 경로에 데이터 신선도 게이트가 전혀 없음 — stale 일봉으로 자동매도 가능**

run_live의 진입 경로는 live_engine.run_once(라인 151-157)에서 session_gap(last_bar, today) > cfg.max_staleness_sessions 면 status='stale'로 거래를 거부한다. 그러나 run_exit.py의 청산 경로는 동일한 신선도 게이트가 없다. _run_locked(라인 86-96)는 data.load로 일봉 종가만 받고, check_exits(live_exit.py:52)는 오직 price is None or s is None or len(s)<regime_ma(=봉 개수 부족)만 검사한다. 마지막 일봉이 며칠/주말 이월로 오래되어도 그대로 _sma(s,200)/_sma(s,50)을 계산하고, 실시간가(live)와 비교해 '200MA 이탈' 또는 '손절' 트리거를 발화시킨다. 즉 yfinance가 며칠 묵은 종가만 줄 때(피드 지연·주말 이월) 잘못된 MA 레벨로 봇 보유분(managed)을 자동 청산할 수 있다. live_exit.py:13 주석은 'MA는 일봉(느림), 현재가는 실시간'을 설계 전제로 두지만, 일봉 자체의 신선도(session_gap)를 검증하는 코드가 어디에도 없다.

- 근거: live_engine.py:151-157 stale 게이트 존재 vs run_exit.py:86-99에 session_gap 호출 부재(import도 없음); live_exit.py:52 데이터 충분성 검사가 len(s)<regime_ma 뿐 — 신선도 미검사.
- 권고: run_exit에서도 closes[s]의 last index에 대해 session_gap(closes[s].index[-1], today) > 임계(예: max_staleness_sessions)면 해당 종목을 data_ok=False(수동확인)로 강등하거나 청산 자체를 보류하라. check_exits에 freshness 임계 인자를 추가해 진입/청산이 동일 신선도 정책을 공유하게 하라.

### [Minor] broker/managed.py:177 (get_account cap) + broker/executor.py:38,41,64 (사이징 기준가)
**cash_cap 은 사이징시점 명목캡일 뿐 실지출캡 아님 — 슬리피지·수수료 초과 가능 (체결 후 대조 부재)**

cash_cap 은 get_account().cash=min(real.cash, cash_cap) 로 사이징 입력만 제한한다(managed.py:177). 매수 수량은 quote.last(=executor.py:38 price)로 산정되고 affordability 도 int(budget/unit), unit=price·buy_mult=1.0 로 동일 시세를 쓴다(executor.py:41,64). 그러나 toss MARKET 은 미지의 실체결가로 채워지고(toss.py:243-265, place_order 가 자체 현금검사·수수료반영 없음 — 토스 서버 위임) 봇이 사이징한 명목 $100 와 실제 출금액이 분리된다. 실제 계좌현금 ~$1,167 ≫ $100 이라 토스가 소액 초과를 거부하지 않으므로, 슬리피지+수수료만큼 cash_cap 을 초과 지출할 수 있다. 코드 어디에도 체결 후 실지출을 cash_cap 과 대조하는 경로가 없다(cash_cap grep 결과 get_account 사이징에만 등장). KMI 1~3주 규모에선 초과액이 미미하나, '$100 한도 내 정확 동작'이라는 검증 목표를 엄밀히는 충족하지 못한다. (buy_mult=1.0 헤드룸 부재가 근본 원인 중 하나 — Major buy_mult finding 과 연계.)

- 근거: managed.py:177 cash=min(real.cash,cash_cap); executor.py:38 price=get_quote().last, 41 tgt_qty=int(w*investable/price), 64 afford=int(budget/unit), unit=price*buy_mult(=1.0 라이브); toss.py:243-265 place_order 무현금검사; grep: cash_cap 은 사이징 외 경로 부재.
- 권고: 엄밀 캡이 목표라면 buy_mult 에 보수적 비용버퍼를 넣어(buy_mult finding 참조) 명목예산을 cash_cap×(1-버퍼) 로 낮추거나, 사이징 기준가를 ask×(1+slippage) 로 올려 실지출이 cash_cap 을 넘지 않도록 헤드룸을 둔다. 첫 실거래는 toss MARKET 대신 LIMIT 으로 상한가를 박는 것도 고려.

### [Minor] broker/managed.py:place_order(195-200) vs record_fills(219-225)
**BUY 제출이 5xx/네트워크로 raise되면 pending은 영속되나 record_fills 미실행 — force 재실행 시 재매수 가능**

place_order는 _pending 영속·_save(198-199행) '후' self._broker.place_order(200행)를 호출한다. 토스 place_order가 5xx/네트워크(toss.py:263 raise)로 던지면 live_engine.py의 for 루프(208-209)가 예외로 빠져 record_fills(223)와 mark_traded(245)에 도달하지 못하고 status=error로 반환된다(live_engine.py:213-216). 이때 pending에는 의도가 남고 basis(managed)는 미갱신. 안전성은 전적으로 다음 실행 reconcile_basis의 min(real, managed+pending) cap(managed.py:250)에 의존한다 — 주문이 실제 체결됐으면 real에 잡혀 흡수, 미도달이면 real에 없어 무시. 동일 프로세스 내 즉각 복구 경로는 없으므로 다음 cron까지 basis가 부정확하다. cap이 이중매수는 막지만, error 반환 후 force 재실행(run_live --force)으로 같은 날 즉시 재시도하면 reconcile_basis가 먼저 돌아(live_engine.py:161-166) real 미반영 상태에서 pending을 흡수하지 못하고 Executor가 diff를 다시 계산 → 이미 토스에 도달한 주문과 별개로 재매수 가능.

- 근거: broker/managed.py:198-200(pending 영속이 submit보다 선행), broker/toss.py:263(5xx/네트워크 raise), live_engine.py:208-216(예외 시 record_fills·mark_traded 미도달), live_engine.py:161-166(reconcile_basis는 실행 시작 시 1회만), run_live.py:150·158(--force가 already_traded 우회)
- 권고: force 재실행 경로에서 직전 error 실행의 미확정 주문(pending 잔존)이 있으면 사용자 확인을 요구하거나, place_order가 던진 직후 reconcile_basis를 한 번 호출해 실보유 재확인 후 plan을 재산출. 최소한 error 반환 시 알림에 'pending 미확정 — force 재실행 전 토스 주문내역 확인' 경고 포함.

### [Minor] broker/toss.py:153-169, _ensure_connected / connect 토큰 만료
**connect()는 만료 직전 자동갱신하나 _request 도중 401 만료 시 재인증 없이 REJECTED 오분류**

_ensure_connected(153) 는 각 public 메서드 진입 시점에만 토큰 만료를 검사한다. 만료 60초 전 갱신 여유(toss.py:168-169)는 진입 시점 기준으로만 적용되므로, 진입 후 실제 HTTP 호출 사이에 토큰이 만료돼 401 이 오면 _request 는 이를 4xx 비즈니스 에러로 분류해 raise 하고 자동 재인증/재시도하지 않는다. place_order 경로에서 401 은 is_business_error()=True → REJECTED Order 로 흡수(toss.py:261-262)되어, 실제로는 '토큰 만료'인데 '주문 거부'로 보고돼 미체결을 거부로 오인하게 만든다. 60초 마진 덕에 통상은 안전하나, 장시간 폴링(_await_fills 최대 timeout)·시계 드리프트 경계에서 401 의 오분류 여지가 있다.

- 근거: toss.py:154-156 진입시점만 만료검사; toss.py:140-150 4xx(401 포함) 즉시 raise·재인증 없음; toss.py:261-262 4xx→REJECTED 흡수로 401 이 거부로 보고됨
- 권고: _request 에서 401(invalid_token) 응답은 1회 connect() 재호출 후 재시도하는 경로를 추가하거나, 최소한 401 을 transient(raise)로 분류해 REJECTED 흡수에서 제외(미체결이 거부로 기록되는 것 방지).

### [Minor] broker/toss.py:279, get_order _STATUS_MAP 미지정 기본값
**미지정 토스 상태코드가 SUBMITTED로 폴백 — 종료상태 누락 시 무한폴링**

get_order 는 _STATUS_MAP.get(status, OrderStatus.SUBMITTED) 로 매핑하며 미지정 상태는 SUBMITTED(폴링 지속)로 폴백한다(toss.py:279). _STATUS_MAP(34-45)에는 EXPIRED·DONE_FOR_DAY 등 토스가 DAY 주문 미체결 마감 시 반환할 수 있는 종료상태가 없다. 그런 상태가 오면 _await_fills(live_engine.py:72-85)는 종료로 인식하지 못해 fill_timeout(기본 30s)까지 폴링하다 partial 로 보고한다. 자금손실로 직결되진 않으나(체결로 오인하진 않음), 마감 미체결을 즉시 종료로 인식하지 못해 30초 낭비·partial 오버리포팅이 발생한다.

- 근거: toss.py:34-45 _STATUS_MAP 에 EXPIRED 등 미포함; toss.py:279 unknown→SUBMITTED; live_engine.py:60 _TERMINAL={FILLED,REJECTED,CANCELLED} 만 종료, SUBMITTED 는 계속 폴링
- 권고: 토스 v1.1.1 주문상태 enum 전체를 _STATUS_MAP 에 명시 매핑(특히 EXPIRED/DONE_FOR_DAY 류 → CANCELLED 또는 별도 종료). 미지정 폴백은 SUBMITTED 유지하되 로깅으로 신규 상태 가시화.

### [Minor] broker/toss.py:217-235, market_open 시간대 비교
**market_open이 캘린더 tz와 로컬 tz 혼합 비교 — naive 응답 시 TypeError→휴장 폴백으로 청산 영구보류**

market_open 은 /api/v1/market-calendar 의 regularMarket.startTime/endTime 을 datetime.fromisoformat 로 파싱(toss.py:230-231)하고, now=datetime.now().astimezone()(로컬 tz-aware, line 232)와 비교한다. start/end 가 tz 정보를 포함한 ISO 문자열이면 fromisoformat 이 tz-aware 로 파싱돼 aware-aware 비교가 성립하지만, 토스가 tz 미포함(naive) 문자열을 반환하면 start/end 는 naive, now 는 aware 가 되어 '<=' 비교 시 TypeError 가 발생한다. 이 경우 except (ValueError, TypeError) 가 잡아 False(휴장)를 반환(toss.py:234-235)하므로, MARKET 청산이 정규장 중에도 'closed' 로 영구 보류될 수 있다(run_exit.py:70-71 에서 market_open False → status=closed). 안전측(매도 안 함)이긴 하나, 손절/200MA 청산이 무한 보류돼 리스크 축소가 실행되지 않는 정합성 결함.

- 근거: toss.py:230-232 fromisoformat(start/end) vs now().astimezone(); toss.py:234-235 TypeError→False; run_exit.py:70-71 market_open False → closed 보류
- 권고: start/end 가 naive 면 캘린더 응답의 timezone(또는 country별 거래소 tz)으로 명시 localize 후 비교. fromisoformat 파싱 결과의 tzinfo 유무를 점검해 naive/aware 를 일관되게 맞출 것.

### [Minor] live_engine.py:181-184 (_run_once_locked) / live_risk.py:18-36
**vol_target=0 이면 레짐(SPY 200MA) 필터까지 통째로 우회됨**

라이브 엔진은 risk = {} 후 if cfg.vol_target > 0: weights, risk = apply_overlay(...) 로만 오버레이를 호출한다(live_engine.py:182-184). apply_overlay 안에는 변동성 타겟뿐 아니라 레짐 필터(SPY<200MA → 전량 현금, live_risk.py:18-36)가 함께 들어 있다. 따라서 사용자가 RunConfig.vol_target=0(변동성 사이징만 끄려는 의도)으로 설정하면 베어마켓 현금화 보호인 레짐 필터까지 동시에 비활성화되어, SPY가 200일선 아래여도 봇이 정상 매수를 계속한다. 두 개의 독립적 통제(변동성 목표 vs 레짐)가 하나의 조건으로 묶여 있는 결합 결함이다. 운영 기본값은 vol_target=0.20(>0)이라 현재 KMI 검증 경로는 안전하나, 향후 vol_target=0 설정 시 자금손실 방향(약세장에서도 매수)으로 조용히 무력화된다.

- 근거: live_engine.py:181-184 (risk={}; if cfg.vol_target > 0: ... apply_overlay), live_risk.py:18-36 (regime 필터가 apply_overlay 내부에 위치), live_engine.py:36 (vol_target: float = 0.20 기본값)
- 권고: 레짐 필터와 변동성 타겟을 분리한다. 레짐 판정은 항상 apply_overlay 를 호출해 수행하고(또는 별도 호출), vol_target<=0 일 때는 apply_overlay 내부에서 scale=1.0 로만 처리하도록 분기하라. 즉 live_engine 의 if cfg.vol_target > 0 게이트를 제거하고 vol_target 비활성화는 live_risk 내부에서 다루게 한다.

### [Minor] run_exit.py:run (라인 52-53) + run_exit.py:_run_locked (라인 86)
**run_exit 가 ET 세션 대신 로컬(KST) 날짜로 fail-open·end_excl 산출 — 킬스위치 baseline 키 오염 및 세션경계 off-by-one**

두 결함이 같은 root(run_exit 가 진입 경로와 달리 ET 세션이 아닌 호스트 로컬(KST) 날짜를 씀)를 공유한다. (a) 라인 52-53: session = last_completed_session(); today = (session or datetime.now().date()).isoformat(). last_completed_session 은 docstring(calendar_util.py:41-42)에서 'None=최근 12일 세션 없음(비정상)'이라 명시하고, 진입 경로 run_live.py:96-98 은 None 이면 거래를 거부(error)하는데, 청산 경로는 None 일 때 조용히 datetime.now().date()(KST)로 폴백한다. today 는 KillSwitch(today=today)(run_exit.py:73)의 일일 baseline·resume_if_new_day 키로 쓰이므로, 비정상 캘린더에서 ET 세션이 아닌 KST 날짜가 baseline 키가 되어 일일손실 정지 baseline 의 거래일 정합이 어긋난다(미장 마감=KST 새벽이라 ET 세션일과 KST 날짜가 상시 1일 어긋남). (b) 라인 86: end_excl = (datetime.now().date() + timedelta(days=1)).isoformat() 도 KST '오늘' 기준이라, 진입 경로 run_live.py:100 의 (session + timedelta(days=1)) 대비 KST 자정~05:00 구간에서 end_excl 이 ET 세션보다 하루 앞서 잡혀(yfinance 가 빈 미래 날짜 요청) 세션경계 off-by-one 이 된다. 실해는 보통 없으나(미래봉 무시) 진입/청산 간 세션경계 정의 불일치.

- 근거: run_exit.py:52-53 의 'session or datetime.now().date()' 폴백 vs run_live.py:96-98 의 명시적 None 거부; run_exit.py:86 (datetime.now().date()) vs run_live.py:100 (session+1); calendar_util.py:41-42 docstring(None=비정상), :22-24 now_et 제공하나 라인 52-86 에서 미사용.
- 권고: 진입 경로와 동일하게 (1) session is None 이면 청산을 보류(error/closed)하라 — datetime.now().date() 폴백 제거(최소한 now_et().date() 로 폴백). (2) end_excl 도 last_completed_session() 결과(이미 today 로 보유)에서 (session + timedelta(days=1)) 로 산출해 ET 세션경계를 통일하라.
