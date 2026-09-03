# Team A (실거래안전-적대) — counts {'critical': 6, 'major': 7, 'minor': 5}

### [Critical] broker/guardrail.py:21,236-254,292-309 (KillSwitch STATE_FILE/roll_day/check_total_drawdown) + reset:220-233 + run_live.py:32-38,123 (make_broker)
**단일 killswitch.json 에 paper $100k 와 toss $21k/$100 baseline·HWM 혼입 → KMI 첫 실거래 false-halt + 수동 JSON 편집 강요 deadlock**

STATE_FILE = STATE_DIR/'killswitch.json' 는 브로커 무관 단 하나(guardrail.py:21, broker 키 부재). roll_day(253)는 매 실행 last_equity 영속, check_total_drawdown(299-300)은 hwm=max 영속. make_broker 에서 paper 는 PAPER_CASH $100,000(run_live.py:37), toss 는 sleeve cash_cap $100(run_live.py:35,123) — 둘 다 같은 STATE_DIR 의 같은 파일에 쓴다. 시나리오: 개발 중 paper 실행 → last_equity=hwm=100000 영속. 오늘 밤 toss 첫 실행(equity≈$100): 새 날이면 roll_day 가 day_start_equity=prior last_equity=100000(247-249). check_daily_loss: dd=100/100000−1=−99.9%<−5% → trip. check_total_drawdown: hwm=max(100000,100)=100000, dd=−99.9%<−20% → trip(kind=total_drawdown). 즉 KMI 첫 실거래가 baseline 오염으로 즉시 false-halt. 더 나아가 이 trip 은 kind='total_drawdown'(307)이라 새 날 자동해제(daily_loss 한정, 251·263-265) 대상이 아니고, reset()은 total_drawdown 일 때 hwm=None 재seed 하나(224) 오염 baseline(day_start_equity)은 그대로라 reset 후에도 daily_loss 로 재트립 → 운영자가 killswitch.json 을 손으로 편집하지 않으면 toss 거래를 시작할 수 없는 deadlock. (2개 worker finding 의 cause→consequence 병합)

- 근거: guardrail.py:21(단일 STATE_FILE), 172-175(_default_state broker 키 없음), 247-253(baseline=prior last_equity·last_equity 영속), 296-308(hwm=max 영속·dd 트립), 224(reset total_drawdown 만 hwm 재seed), 251&263-265(자동해제 daily_loss 한정); run_live.py:35-38,123(paper $100k vs toss sleeve, 동일 STATE_DIR)
- 권고: killswitch state 를 브로커별 네임스페이스(killswitch_{broker}.json)로 분리하거나 paper/toss 가 USTRADE_HOME(STATE_DIR)을 공유하지 않게 하라. roll_day/check_total_drawdown 가 '직전 equity 대비 비현실적 스케일 점프(예 >5배)' 면 baseline·hwm 을 현재 equity 로 재seed+경고. go-live 전 toss_setup 또는 run_live(toss) 진입부에 killswitch.json 삭제/초기화 절차 명시. reset 시 total_drawdown 이면 hwm 뿐 아니라 day_start_equity 도 None 재seed.

### [Critical] run_exit.py:93-96 + broker/toss.py:208-215 get_quote + live_exit.py:52,60-65 check_exits + broker/guardrail.py:388-392
**토스 lastPrice 결측 시 get_quote 가 0.0 반환 → 관리종목 거짓 청산(시장가 강제 매도)**

run_exit._run_locked 는 live[s]=gbroker.get_quote(s).last 로 실시간가를 채운다. TossBroker.get_quote(toss.py:213)는 last=_num(res[0].get('lastPrice')) 이고 _num(None)=0.0(63-70)이라, prices 응답에 res[0]는 있으나 lastPrice 가 null/누락이면 예외 없이 last=0.0 반환. run_exit 의 except(96행)는 raise 시에만 None 처리하므로 0.0 은 통과. check_exits(live_exit.py:52)는 `if price is None or s is None ...` 만 거르고 price=0.0 은 통과 → 0.0<sma200(60-62) '200MA 이탈' + 0.0<avg_price*(1-stop)(63-65) '손절' 동시 트리거 → to_exit 가 자동청산 대상으로 올림. GuardedBroker.place_order(388-392)는 BUY 만 price<=0 거부하고 SELL(청산)은 위험축소라 허용 → 빈 호가 한 번에 봇 보유분이 시장가로 강제 청산. (executor 경로는 executor.py:39-40 이 price<=0 raise 로 막지만 run_exit/check_exits 경로엔 그 가드가 없음 — 별개 진입점)

- 근거: broker/toss.py:213 `last=_num(res[0].get('lastPrice'))` + _num 기본 0.0(63-70); run_exit.py:93-96 except 만 None 처리; live_exit.py:52 가드에 price>0 없음, 60-65 0.0 비교 트리거; guardrail.py:388-392 SELL bad-price 허용
- 권고: get_quote 가 last<=0(lastPrice 부재) 시 0.0 반환하지 말고 예외를 던지거나, live_exit.check_exits 가드를 `if price is None or not (price>0) or s is None ...` 로 강화해 price=0.0 을 data_ok=False 로 자동청산에서 제외하라(최소수정).

### [Critical] live_engine.py:_await_fills(63-85)·_run_once_locked(229-245) + broker/managed.py:reconcile_basis(234-257) + broker/toss.py:243-265(SUBMITTED·DAY tif:251); grep: live 경로 cancel_order 0건
**미체결 잔존 DAY주문 미취소 → 다음 런 재플랜으로 더블바이(KMI 1주 검증서도 발생 가능)**

place_order(toss)는 SUBMITTED 만 반환(264)하고 _await_fills 는 30s 폴링 후 미체결이면 취소 없이 PARTIAL/SUBMITTED 반환(취소 호출 없음 — grep 결과 live_engine/run_exit/run_live 어디서도 cancel_order 미호출, 정의·테스트만 존재). 미체결이면 run_once 는 status='partial' 로 mark_traded 없이 종료(231-239, mark_traded 는 ok 경로 245). 토스 DAY 주문(tif:251)은 장중 잔존. 다음 cron 재시도서 already_traded()=False → 재플랜. reconcile_basis 는 get_positions(체결분만 반영, 열린주문 미반영) 기반이라 diff(tgt-cur) 여전히 양수 → 두 번째 BUY. 이후 첫 주문도 체결되면 2배 포지션 + cash_cap 초과. KMI 1주 검증서 첫 주문이 30s 안에 안 잡히면 다음 런이 1주 더 산다.

- 근거: live_engine.py:68-85(취소 없는 폴링),231-245(partial return·mark_traded ok 경로만); broker/toss.py:251(DAY),264(SUBMITTED 반환); broker/managed.py:245(get_positions 기반 reconcile); grep: live 경로 cancel_order 호출 0건
- 권고: 재플랜 前 직전 미체결 주문을 cancel_order 로 정리하거나, 제출 order_id/clientOrderId 를 상태파일 breadcrumb 로 남겨 다음 런이 get_order 로 오픈주문을 조회·합산(cur_qty+오픈수량)해 diff 계산. 또는 mark_traded 를 '주문 제출 직후'로 옮기고 partial 도 당일 재거래 차단(force 로만 우회).

### [Critical] broker/toss.py:place_order(258-263)·_request(116-151) 5xx/타임아웃 raise 경로 + live_engine.run_once(200-216)·_reconcile(88-108); SELL pending 미보호
**주문 응답 유실(타임아웃/5xx) 시 체결된 주문을 미체결로 오인 → order_id 소실·유령포지션, 특히 SELL 청산**

MARKET 주문이 토스에 실제 접수·체결됐으나 응답 유실(read timeout 또는 접수 後 502/503)되면 _request 가 TossAPIError(network-error/5xx) raise(120-124,137-139,151), place_order 는 is_business_error()==False 라 re-raise(263). run_once 의 plan 루프(208-209)가 이 raise 로 빠져 해당 Order 가 orders 에 append 안 됨 → order_id 봇 기록서 소실. _request 재시도(max 2)는 같은 clientOrderId(246) 로 재POST 하나 소진 後 키째 버려짐. BUY 는 pending(195-199)+reconcile_basis(234-257)가 실보유 대조로 일부 흡수하나, SELL(청산)은 pending 보호 없음(managed.py:226 은 fq>0 일 때만 차감) → 체결된 매도가 기록 안 되면 봇이 보유 중으로 오인 → run_exit 재매도/run_live 잘못된 prior_qty 사이징. _reconcile(88-108)은 drift 보고만, 복구 못함.

- 근거: broker/toss.py:116-151,243-265; broker/managed.py:186-257(SELL pending 부재); live_engine.py:88-108,200-216
- 권고: place_order 전송오류(5xx/네트워크) 시 clientOrderId 를 슬리브/저널에 영속하고 다음 런 시작 시 GET /orders?clientOrderId= 로 접수/체결 여부 조회·복구하는 reconcile-by-clientOrderId 경로 추가. SELL 도 pending 의도로그를 두어 미확인 매도를 다음 런이 검증.

### [Critical] broker/toss.py:place_order(247)·get_quote(210)·cancel/get_order 진입부 — 아웃바운드 심볼 미정규화 + managed.py:188-200(s=_norm 검사하나 원본 req 전달)
**내부→토스 아웃바운드 심볼이 역정규화 안 됨 — 점 표기 클래스주(BRK-B/BRK.B) 계약 불일치로 sp100 운영 시 매수·시세 전량 실패**

심볼 네임스페이스가 두 갈래: 유니버스/weights/OrderRequest.symbol 은 하이픈(universe.py:24 BRK-B), ManagedBroker._norm 은 게이팅·basis 비교용으로만 정규화(managed.py:33-35)하며 토스로 나가는 호출엔 관여 안 함. TossBroker.place_order 는 body['symbol']=req.symbol 을 그대로(247), get_quote 도 params={'symbols':symbol} 로 그대로(210) 보낸다. 토스가 미국 클래스주를 점 표기(BRK.B)로 받으면 BUY/시세 모두 stock-not-found(4xx)→REJECTED 또는 no-price 예외. 오늘 밤 KMI 는 점 없어 무해하나, sp100(BRK-B 등) 운영 전환 시 BRK-B 후보가 선정되면 매수 전량 실패·시세 예외→가드 에러카운트 누적→정지. 또한 managed.place_order(189-194)는 _norm 으로 가드 비교는 정확하나 가드 통과 후 원본 req(비정규화)를 self._broker.place_order(req)(200)로 넘겨, 관리종목 매도 시 'googl' 같은 비정규화 심볼이 토스로 전송될 수 있다(매도 사이징 정합 흔들림). 인바운드(토스→내부)만 _norm 흡수, 아웃바운드는 미처리. (Critical 아웃바운드 + Minor managed 원본전달 병합)

- 근거: toss.py:210 params={'symbols':symbol},247 body['symbol']=req.symbol(정규화 호출 없음); managed.py:33-35 _norm 내부용·189 검사하나 200 원본 req 전달; universe.py:22-24 하이픈 BRK-B; tests_toss.py AAPL 만 검증
- 권고: TossBroker.place_order/get_quote/cancel_order/get_order 진입부에 토스 기대 표기로 역정규화하는 단일 함수(_to_toss_symbol)를 두거나, get_positions 노출 canonical 과 place_order 전송 표기를 단일 규칙으로 고정하라. sp100 운영 전 BRK-B 한 종목으로 get_quote 실거래 확인. 토스 US 심볼 표기를 toss_check.py 로 실증.

### [Critical] broker/toss.py:place_order(247) clientOrderId — uuid 매 호출 새 생성
**멱등키가 호출마다 새로 생성 → 논리적 재시도(재플랜·운영자 재실행) 중복접수 못 막음**

clientOrderId 가 매 place_order 마다 uuid.uuid4().hex[:32] 로 새로 생성된다(247). 주석은 '멱등성 키(재요청 안전,10분)' 라 주장하나, 동일 논리적 주문을 상위에서 재시도(위 Critical 의 재플랜·미체결 미취소, 또는 운영자 수동 재실행)하면 새 uuid 가 부여돼 토스가 별개 주문으로 접수한다. 즉 이 키는 같은 _request 콜의 HTTP 재전송에만 멱등이고 비즈니스 레벨 중복(런마다 키 달라짐)은 못 막는다. 게다가 _request 는 method 무관 429/5xx/RequestException 을 max_retries 까지 재시도(133-139)하므로 place_order POST 도 재시도 대상 — 토스 clientOrderId 서버측 멱등 보장이 입력(코드/주석)으로 확인 안 되며, 보장이 없으면 네트워크 타임아웃 1회가 곧 이중체결(MARKET 슬리피지 누적). (uuid 멱등 + POST 재시도 2개 worker finding 병합 — 같은 멱등성 뿌리)

- 근거: broker/toss.py:247 uuid.uuid4().hex[:32](키 영속/재사용 로직 없음); 120-124·133-139 method 분기 없는 재시도; 259 place_order 가 _request('POST') 호출
- 권고: clientOrderId 를 주문의 결정론적 함수(hash(session_date|symbol|side|qty|sequence))로 생성해 같은 세션 같은 의도 주문이 같은 키가 되게 하라. 토스 clientOrderId 서버측 멱등 계약을 문서로 확정 전까지 place_order POST 자동 재시도 0(또는 재시도 前 get_order 로 직전 제출 성공 확인 후 재전송).

### [Major] run_live.py:104-122(sleeve_protected drop, toss 분기) + live_engine._run_once_locked(169-173) 보호가드 부재
**보호종목 후보-제외가 run_live 단일 호출 지점에만 존재 — 엔진 계층엔 강제 안 됨**

보호종목을 후보 유니버스에서 제외(방어3)하는 코드는 run_live.run() 의 broker_kind=='toss' 분기로만 sleeve_protected 를 채우고(104-113) drop(119-122)도 그때만 발생. live_engine.run_once/_run_once_locked 자체는 prices.columns 를 그대로 sel_fn 에 흘리며(171-173) 보호종목 제외를 전혀 안 한다. 향후 다른 진입점(테스트훅·수동호출·신규 스크립트)이 ManagedBroker+prices 로 run_once 를 직접 부르며 사전-drop 을 빠뜨리면, AMAT(보호종목이면서 sp100 멤버, universe.py:23) 이 모멘텀 게이트를 통과해 weights 에 실릴 수 있다. 최종 backstop 인 ManagedBroker.place_order BUY 가드(192-194)가 막으나, 그 경우 Executor.plan 이 보호종목에 예산 배정(62-68) 후 거부되어 managed 매수 예산이 잠식·축소.

- 근거: run_live.py:104 toss 분기 안에서만 sleeve_protected 채움; live_engine.py:171-173 sel_fn(prices) 그대로; universe.py:23 sp100 에 AMAT
- 권고: 보호종목 제외를 엔진 경계로 끌어올려라. ManagedBroker.protected 를 run_once 초입에서 사용해 select 직전 prices/weights 에서 _norm 매칭 drop 하거나, Executor.plan 의 buy_cands 생성 시 broker 가 protected 면 해당 심볼 skip. 단일 호출 지점(run_live)에만 의존하지 말 것.

### [Major] broker/executor.py:26-30(getattr buy_mult)·38-41,62-68(last 기반 사이징) + managed.py/toss.py(_commission·_spread·_slippage 부재)
**토스 라이브에서 buy_mult=1.0 + MARKET 을 last 평탄시세로 사이징 — 수수료/슬리피지 버퍼 0, 체결가>last 시 cash_cap 초과**

Executor.plan 은 buy_mult 을 getattr(self.broker,'_commission'/'_spread'/'_slippage',0.0)(26-28)로 읽는데, 라이브 체인 GuardedBroker→ManagedBroker→TossBroker 어디에도 이 속성이 없다(grep 확인: paper.py 에만 존재). ManagedBroker 에 __getattr__ 도 없어 default 0.0 → buy_mult=1.0. 또 사이징/affordability 를 모두 get_quote(sym).last 로 하는데(38,64) TossBroker.get_quote 는 bid=ask=last 평탄근사(215, 라이브 MARKET 호가미사용). 실제 토스 MARKET 체결은 ask·시장충격으로 last 보다 비싸므로, cash_cap=$100, KMI≈$32 → afford=int(100/32)=3주, 사이징비용 3*$32=$96 으로 계산돼도 실체결이 불리하게(예 $33.40) 들어오면 3*$33.40=$100.20>캡. EXEC-1 docstring(20-22) 의 버퍼는 paper 에서만 유효, toss 경로 미작동. paper 검증은 통과하나 toss 실거래서 캡 초과 지출 경로가 열림. (executor buy_mult 2건 + last 사이징 1건 = 3 worker finding 병합, 같은 cap-overshoot 뿌리)

- 근거: executor.py:26-29 getattr default 0.0,38(price=last),64(unit=price*buy_mult); paper.py:24-26 만 속성 정의; managed.py/toss.py 미존재(grep); toss.py:215 bid=ask=last
- 권고: 브로커 비용을 BaseBroker 공개 인터페이스(estimated_costs() 또는 공개속성)로 올려 TossBroker 에 보수적 토스 수수료·환전스프레드 추정치 제공(buy_mult>1). 또는 사이징/affordability 를 ask(혹은 last*(1+안전마진))로 하고 plan 마지막에 sum(buy notional)<=cash_cap 사후검증 추가.

### [Major] broker/guardrail.py:RunLock(83-160)·LOCK_FILE(22) + run_exit.py:55·live_engine.py:126 공유 락 + pid 재사용 회수(104-124)
**run_live·run_exit 가 단일 run.lock 공유 — 크래시 락 잔존/pid 재사용이 청산을 최대 1~6h 무경보 차단**

run_live(데일리 진입)와 run_exit(15분 청산)이 동일 default LOCK_FILE(run.lock, guardrail.py:22)을 쓴다. (a) run_live 가 크래시로 락 잔존하면 age<=_LOCK_STALE_SEC(1h) 동안 회수 안 됨 → 그동안 run_exit 모든 호출이 status='locked' 즉시 반환, run_exit.py:149 에서 locked 는 benign(exit0)이라 알림도 없음 → 손절/200MA 청산이 최대 1h 무경보 미작동. (b) Windows 에서 크래시 후 pid 가 재사용되면 1h~6h 구간서 _pid_alive(holder)=True 오판(다른 무관 프로세스 점유)으로 회수 안 함(112,55-60) → 최대 6h locked. 자금손실 직결 아니나 청산 가용성 저하. (Major 공유락 + Info pid재사용 병합 — 같은 락·escalation 뿌리)

- 근거: guardrail.py:22(단일 LOCK_FILE),112(stale/hard 조건),55-60(pid 판정불가 True); run_exit.py:55,57-58(locked 반환),149(locked benign exit0); live_engine.py:126
- 권고: run_live 와 run_exit 에 별도 락(run.lock vs exit.lock) 부여 — 진입·청산은 임계자원이 달라 상호배제 불필요. locked 가 반복되면 알림 escalation 도입(무경보 차단 방지).

### [Major] broker/toss.py:get_account(183-192)·_num(63-70) + guardrail.py:272·280-281·302-303·328-330
**토스 API 가 equity=0.0 으로 강제 coerce 되면 첫 실행 양 손실가드 fail-open + 비례 명목캡 비활성**

TossBroker.get_account 은 _num() 으로 None/누락/비숫자 응답을 0.0 강제(63-70,187-191). buying-power·holdings 응답이 손상·빈 envelope 면 cash=0,pos_val=0 → equity=0.0(finite). _require_finite_equity 는 isfinite 만 보므로 0.0 통과(272). 첫 실행(state 신규)에서 check_daily_loss 는 base None→통과(280-281), check_total_drawdown 은 hwm=max(None→0,0)=0,'if hwm<=0:return'(302-303) 통과 → 양 손실가드 무력화·거래 진행. 추가로 run_equity=0.0 이라 비례 명목캡도 'if run_equity>0' False(328-330)로 꺼지고 절대 $1M 캡만 남음.

- 근거: toss.py:63-70(None→0.0),187-192(equity=cash+pos_val); guardrail.py:272(isfinite 만),280-281(base None 통과),302-303(hwm<=0 통과),328-330(run_equity 0 시 비례캡 비활성)
- 권고: _require_finite_equity 에 equity<=0 도 fail-closed 추가(check_daily_loss base<=0 처리와 대칭). 또는 TossBroker.get_account 에서 cashBuyingPower/marketValue 키 자체가 응답에 없으면 0 coerce 대신 TossAPIError raise 해 데이터 결함을 상위 에러가드로.

### [Major] broker/toss.py:_ensure_connected(153-156)·_request 4xx(137-150)·place_order(261)
**토큰 조기 만료(401)를 비즈니스거부로 오인 → 유효주문 REJECTED, 재인증 안 함**

_ensure_connected 는 로컬 monotonic 시계와 _token_expiry(169) 비교로만 갱신 판단. 서버측 토큰 조기 무효화(키 회전·서버 재시작·시계 스큐)로 만료 전 무효가 되면 토스가 401 반환. 401 은 4xx 라 _request 재시도 대상(429/5xx) 아님 → 즉시 raise(149-150). place_order 에서 is_business_error()==True(60: 400≤status<500) → REJECTED Order 반환(261-262). 즉 '재인증하면 성공할 일시 상태'를 영구 거부로 처리해 정상 주문이 미체결로 남고, get_account/get_positions/get_quote 도 401 시 TossAPIError raise → run_once 가 error/partial 로 끝나며 재인증 시도 없음. 토큰 유효한데 거래 못 하는 false-negative.

- 근거: broker/toss.py:59-60,153-156,162-169,133-150,258-263
- 권고: _request 에서 401(가능하면 토스 토큰만료 code) 감지 시 self._token=None 후 connect() 재호출·1회 재시도(주문은 멱등 clientOrderId 라 안전). place_order REJECTED 매핑에서 401 제외해 transient 분류.

### [Major] broker/toss.py:_STATUS_MAP(34-45)·get_order(279) + live_engine._await_fills(63-85)
**상태매핑 누락값(접수대기/EXPIRED/오타스펠)→SUBMITTED 폴백으로 미체결 오인·무한폴링·다음 런 중복**

_STATUS_MAP 은 PENDING/PARTIAL_FILLED/FILLED/CANCELED/REJECTED 등만 매핑, get_order(279)는 미지값을 SUBMITTED 폴백. 토스 v1.1.1 실제 응답이 NEW/ACCEPTED/RECEIVED/PENDING_NEW 또는 EXPIRED/DONE_FOR_DAY 또는 PARTIALLY_FILLED(map 은 'PARTIAL_FILLED' — 스펠 상이 가능) 같은 값을 쓰면, 종결 상태인데 SUBMITTED 로 남아 _await_fills 가 30s 무의미 폴링 後 비종결 간주 → partial 처리(231-239)·record_error 누적으로 가드 정지 위험. 특히 EXPIRED(주문 만료=실질 취소)/DONE_FOR_DAY 가 CANCELLED 로 매핑 안 되면 봇이 살아있는 주문으로 착각 → 다음 런 중복주문. tests_toss.py 는 map 에 있는 값만 검증, 누락값 미검증.

- 근거: broker/toss.py:34-45,275-294; live_engine.py:63-85,230-239; tests_toss.py:239-248
- 권고: 토스 Open API v1.1.1 주문상태 enum 전체를 문서 확정해 _STATUS_MAP 완성(NEW/ACCEPTED/PENDING_NEW→SUBMITTED, EXPIRED/DONE_FOR_DAY→CANCELLED, PARTIALLY_FILLED 스펠 확인). 미지 상태 폴백을 SUBMITTED 가 아니라 경고+드리프트 보고로. tests_toss 에 미지/접수대기/EXPIRED 케이스 추가.

### [Major] data.py:90-111 load_panel + live_engine.py:151-157(staleness 게이트) + live_select_canslim.py:53-62 / live_risk.py:23-32
**패널 staleness 게이트가 최신 티커 하나로 우회 + SPY ffill stale → 종목별/레짐 거짓 신호**

load_panel 은 pd.DataFrame(closes).dropna(how='all')(110)로 union 인덱스를 만들어, 한 종목만 최신 봉이어도 panel.index[-1] 가 그 최신 날짜가 된다. live_engine H1 게이트(154 session_gap(prices.index[-1],today))는 이 패널 max 인덱스로만 판정 → 대다수 stale 이어도 단 한 종목 신선하면 통과. 이어 select(canslim:54-62)는 종목별 s=prices[t].dropna() 의 자기 iloc[-1]/SMA200/high52/12-1mom 을 써 stale 종목은 오래된 마지막 봉으로 거짓 매수/게이트 통과(stale-back 신호). 또 live_risk.apply_overlay(25)는 SPY 를 .reindex(prices.index).ffill() 해, yfinance 가 SPY 를 1세션 짧게 주면 패널 최신 날짜에 NaN→ffill 이 직전 stale SPY 종가를 채움 → px=spy.iloc[-1](27) 가 stale, isfinite 체크(28)는 ffill 후라 통과 → 레짐이 1세션 늦은 SPY 로 결정(경계서 ON↔OFF 뒤집히면 전량 현금화/잘못된 풀투자). (패널 staleness 우회 + SPY ffill 병합 — 같은 종목별/벤치 staleness 은폐 뿌리)

- 근거: data.py:110 dropna(how='all') union; live_engine.py:152-154 last_bar=prices.index[-1]; live_select_canslim.py:54-62 종목별 dropna 후 iloc[-1]; live_risk.py:25 reindex.ffill,27 iloc[-1],28 isfinite 만,32 risk_on=px>mav
- 권고: select 진입 시 종목별 s.index[-1] 의 session_gap 을 검사해 stale 종목 후보 제외하거나 load_panel 에서 종목별 마지막봉 분산 점검해 stale 컬럼 드롭/경고. apply_overlay 는 ffill 直前 SPY 원본 마지막 유효 인덱스를 패널 마지막과 session_gap 비교해 1세션 초과 stale 이면 ValueError 로 거래 보류.

### [Major] run_exit.py:86-99 — 청산 경로 데이터 신선도 게이트 부재
**청산(run_exit)엔 stale 데이터 차단 게이트가 없음 — 진입(run_live)과 비대칭**

live_engine.run_once 는 max_staleness_sessions 로 session_gap>한도면 거래 보류(150-157). 그러나 run_exit._run_locked 엔 동일 게이트가 전혀 없다. data.load(s,'2022-01-01',end_excl) 일봉이 주말/휴장/피드지연으로 여러 세션 stale 해도 len(s)>=200 만 만족하면 check_exits 가 SMA200/50·RSI 를 산출, 실시간가(0 또는 지연가)와 비교해 청산 트리거. 200MA 는 1세션 stale 로 거의 안 움직이나 RSI 과열 트림·50MA 이탈·손절은 stale 일봉/지연 실시간가 조합으로 거짓 신호 가능. 진입은 보호, 청산은 무방비.

- 근거: run_exit.py:86-99 session_gap 호출 없음; 대조 live_engine.py:150-157 H1 게이트; calendar_util.py:55-64 session_gap 존재
- 권고: run_exit 에서도 closes[s] 마지막 봉에 session_gap(s.index[-1],today) staleness 검사 추가, 한도 초과 시 자동청산서 제외(수동확인 알림). live_engine 의 cfg.max_staleness_sessions 와 동일 기준 권장.

### [Minor] toss_setup.py:52-61 + broker/toss.py:194-206(marketCountry=='US' 필터)
**protected 스냅샷이 US-only 보유에서만 생성 — 비US/미분류 보유분 보호 누락 가능**

toss_setup 은 protected 를 b.get_positions() 결과에서만 만든다(52-61). TossBroker.get_positions 는 marketCountry=='US' 항목만 반환(201). 보호 종목이 모두 미국이면 정상이나, (1)토스가 marketCountry 누락/오분류하거나 (2)펀딩 직후 holdings 가 일시적으로 비어/부분이면 그 종목이 protected 에서 빠진다. sp100 에 든 AMAT 류가 marketCountry 누락으로 protected 에 안 들어가면 봇이 보호 의도와 달리 매수·매도 후보로 취급(protected 부재→후보-제외도 안 됨). 스냅샷 시점 holdings 신뢰도에 보호 경계가 통째로 의존.

- 근거: toss_setup.py:52 for p in b.get_positions(),:61 protected=set(holdings)-...; toss.py:201 marketCountry!=self._market_country: continue
- 권고: toss_setup 에서 holdings 가 비었거나 기대 종목 수보다 적으면 저장 거부·경고. 보호 대상 심볼을 사용자가 명시 입력/확정(보유 종목 하드 화이트리스트 대조)하는 검증 단계 추가.

### [Minor] broker/executor.py:25,60(investable=equity*alloc vs budget=cash+proceeds) + managed.py:175-184(get_account)
**cash_cap 은 현금배포 캡일 뿐 — managed 평가액 상승분이 노출을 캡 초과로 키움**

ManagedBroker.get_account 는 cash=min(real.cash,cap) 클램프하나 equity=cash+managed_val 이고 managed_val 은 봇 보유분 현재가 평가액(177-184). Executor.plan 의 investable=acct.equity*alloc(25)은 이 equity 를 쓴다. 매수 예산은 budget=acct.cash+proceeds(=캡된 cash+매도대금,60)로 묶여 '신규 현금 지출'은 캡 내 유지되나, 봇 보유 KMI 가 가격상승하면 managed_val↑→equity↑→investable↑ 하여 보유 평가차익이 재투자 여력으로 환산돼 관리 총 포지션 가치가 $100 를 점진 초과 가능(KMI 2배→managed_val=$192,equity=$292,investable≈$277). 사용자가 cash_cap 을 '봇 최대 노출'로 기대했다면 실제 노출은 더 큼. 첫 1주엔 영향 없으나 장기 누적 시 캡 의미 어긋남.

- 근거: managed.py:184(equity=cash+managed_val, cash 만 cap); executor.py:25(investable=equity*alloc),60(budget=cash+proceeds)
- 권고: cash_cap 을 '누적 현금배포 한도'로 명확히 문서화하거나, 별도 position-value 캡(managed_val 상한)을 두어 평가액 상승이 재투자 여력으로 환산되지 않게 하라.

### [Minor] broker/executor.py:41(tgt_qty=int(w*investable/price)) + live_select_canslim.py:98(등비중) + broker/toss.py:250(int 절사)
**KMI 첫 거래가 선정 종목수에 따라 0주로 floor + 분수 basis 청산 시 0주 매도 가능**

tgt_qty=int(w*investable/price)(41). cap=$100,alloc=0.95→investable=$95,KMI≈$32. 등비중 w=1.0/len(final)(canslim:98). top_n=3·3종목 선정 시 w=0.333→int(0.333*95/32)=int(0.989)=0주 → KMI 주문이 안 나간다(첫 검증 실패). 1주는 final 이 정확히 2개(w=0.5→int(1.48)=1)일 때만, KMI 단독(w=1.0)이면 int(2.96)=2주. '오늘 밤 KMI 1주' 결과가 사이징 아니라 그날 선정 종목수에 의존하는 off-by-one 경계. 또 청산 SELL 은 toss.place_order 가 quantity=str(int(req.qty))(250)로 정수 절사 — record_fills/reconcile 가 분수 basis(basis=0.x)를 만들면 int(0.x)=0 으로 0주 매도가 제출돼 청산이 무시되고 추세붕괴 종목이 보유로 남음(손실확대). (사이징 floor + 청산 int절사 병합)

- 근거: executor.py:41 int floor; live_select_canslim.py:98 weights=1.0/len(final); toss.py:250 quantity=str(int(req.qty)); managed.py:161 qty=min(p.qty,basis) 분수 가능
- 권고: 첫 검증은 cash_cap·top_n·예상 선정수를 함께 고정하거나 top_n=1/KMI 단독 유니버스로 1주 결정론화. 청산 SELL 수량은 floor 아니라 보유 정수주로 명시 계산하고 int 절사로 0 되면 경고/수동확인. basis 를 항상 정수로 유지하는 불변식 강제 검토.

### [Minor] broker/toss.py:cancel_order(267-273)
**취소 transient 오류(5xx/네트워크/타임아웃)를 False(취소실패)로 뭉개 상태 오인**

cancel_order 는 모든 TossAPIError 를 except 로 잡아 False 반환(272-273). 4xx(이미체결 409·잘못된 id)와 5xx/네트워크 타임아웃을 구분 안 하므로, 실제 토스가 취소를 접수·처리했으나 응답 유실(타임아웃)된 경우에도 False(미취소) 보고. 호출측이 이를 '주문 살아있다'로 해석하면 취소된 주문을 다시 다루거나, '취소 성공' 전제 재주문 로직이 막혀 잔존+신규 공존 가능. 현재 cancel_order 직접 호출 경로는 좁으나 place_order 는 transient/business 구분, cancel 은 미구분이라 계약 일관성이 깨져 향후 청산·정정 확장 시 desync 위험.

- 근거: broker/toss.py:59-60,258-263(place_order 대비),267-273
- 권고: cancel_order 도 place_order 처럼 e.is_business_error() 분기 — 4xx(이미체결/없음)는 False(정상적 취소불가), 5xx/네트워크는 raise 하여 상위가 '취소 상태 불명'으로 get_order 재확인.

### [Minor] broker/toss.py:market_open(217-235) + run_exit.py:70-71 / calendar_util.py 폴백(50-52)
**장 캘린더 tz 가정 불명확 → 항상 closed 오판으로 청산 영구 skip + 청산 today 폴백 세션 불일치**

market_open 은 rm['startTime']/['endTime'] 를 fromisoformat 파싱(230-231)하고 now=datetime.now().astimezone()(tz-aware,232) 와 비교. 토스가 윈도를 tz 오프셋 없는 naive 로 주면 aware↔naive 비교 TypeError→except(234)로 False(휴장) → 정규장 중인데 항상 '장 닫힘' 판정 → run_exit 가 영구 skip(closed)(run_exit.py:70-71) 되어 손절·200MA 청산 미작동. 안전측(fail→closed)이라 손실 직결은 아니나 청산 가드 silent 무력화. 또 run_exit.run(52-53)은 today=(session or datetime.now().date()) 로, last_completed_session 이 None 이면 KST now.date 로 폴백 → US 세션과 어긋나 KillSwitch baseline·day 키가 잘못된 날짜로. run_live 는 session None 을 명시 error 차단(96-98)하는데 run_exit 는 조용히 폴백(비대칭). (market_open tz + today 폴백 병합 — 둘 다 청산 게이트 silent 오작동)

- 근거: broker/toss.py:217-235; run_exit.py:52-53,70-71; 대조 run_live.py:96-98; calendar_util.py:50-52 None 반환
- 권고: 토스 market-calendar 응답 시각 tz 를 문서 고정, naive 면 명시 tz(KST/ET) 부여 후 비교. 파싱/비교 실패 시 False 대신 '판정불가' 구분해 운영자 알림. run_exit 도 session is None 이면 명시 error 반환(거래 보류)으로 run_live 와 일관화.

### [Minor] broker/managed.py:place_order(195-200) pending 영속 위치 vs RunLock 임계구역
**pending 영속 후 주문 직전 크래시 시 pending 과대(유령은 실보유 cap 으로 차단)**

BUY 의 pending 영속(_save,199)이 self._pending 인메모리 갱신 후 디스크 원자적 저장(save_sleeve tempfile+os.replace 양호). place_order 와 실제 broker.place_order(200) 사이 크래시하면 pending 은 +qty 됐는데 주문 미제출. 다음 reconcile_basis 가 min(실보유,basis+pending)(250)로 실보유 cap 하므로 유령포지션은 안 생기나(tests_managed 확인), pending 과대계상 채 남아 그 종목 reconcile 까지 1런 지연 가능. 또 같은 세션 BUY 여러 건이면 매 place_order 마다 전체 슬리브 재기록(N회 fsync).

- 근거: broker/managed.py:196-200(제출 前 pending _save),250(min real cap); tests_managed.py
- 권고: 현 설계의 실보유 cap 이 유령·자금손실을 막으므로 긴급 아님. pending 영속 시점에 order_id 미발급이라 추적 불가한 점은 위 Critical 의 breadcrumb 도입 시 함께 해소.

### [Info] broker/managed.py:154-184(get_positions/get_account)·203-257(record_fills/reconcile_basis) + guardrail.py:184(buying_power=cash)·328-334(비례 명목캡)
**보호 격리·co-mingle 흡수 차단·buying_power 캡·sleeve 기준 명목캡 — 공격벡터 b/c/d 부재 확인(안전)**

적대 검증 종합(결함 아님): (1) get_account managed_val 은 self.get_positions()(basis>0 managed 만, 보호는 disjoint 제거)만 순회(179)해 보호 평가액이 equity·buying_power 에 안 섞임. (2) record_fills 는 s in protected 면 skip(215), SELL 은 max(0,basis-fq)(227)로 managed 만 감소, reconcile_basis 는 min(실보유,basis+pending)(250)으로 co-mingle 추가분 미흡수. (3) buying_power=cash(=캡된 현금)로 하드코딩(managed.py:184·toss.py:192), Executor 는 buying_power 미참조 → 마진/2x 노출 없음. (4) check_order_notional 비례캡은 run_equity(=sleeve equity, protected 제외)(328-330) 기준이라 protected 포함으로 느슨해지는 벡터 d 부재 — KMI~$32<min($1M,0.40*$100*1.5=$60) 통과. (5) 일일손실 baseline 은 day-over-day(roll_day 246-249)라 매 런 리셋 안 됨(벡터 c 부재).

- 근거: managed.py:179·159·215·227·250·184; toss.py:192; guardrail.py:328-334; live_engine.py:202-203; guardrail.py:246-249
- 권고: 변경 불필요. 회귀 방지로 'get_positions 가 보호종목 절대 반환 안 함'·'buying_power<=cash_cap' 불변식을 tests_managed 에 고정 권장. 단 cash_cap 미지정 운영 시 sleeve equity 가 managed+full-cash 로 부풀어 비례캡이 느슨해질 수 있으니 toss 운영에 cash_cap(TOSS_MANAGED_CASH) 필수화 검토.

### [Info] live_select_canslim.py:26-31(_A_DIR sibling import)·34-36(_mom_12_1)·55(길이게이트) + live_engine.py:19-23,169-170 + run_live.py:103-123(protected drop 순서)
**canslim 형제프로젝트 import 단일실패점 + 인덱싱 룩어헤드 없음 + 보호 drop 순서 정합(확인, 일부 통합위험)**

(1) 기본 canslim 전략이 형제 디렉토리 텔레그램_시그널_알리미 의 engine.funda import 에 하드 의존(_A_DIR=parent/'텔레그램_시그널_알리미', append). 부재면 _select_canslim=None→canslim 요청 명시 error(fail-closed, 안전)이나, 오늘 밤 KMI end-to-end 가 이 형제 import 성공에 100% 묶여 단일 실패점. import 는 모듈 로드 시 1회 try/except 라 부분설치/버전불일치면 점수 0(모멘텀만)으로 깔리고 screen_degraded 로만 알림. (2) _mom_12_1 은 iloc[-21]/iloc[-252](36)로 최근 21봉 스킵, 길이게이트 len>=252(55)가 IndexError 방지 → 일별 진입 룩어헤드 없음. (3) protected drop 순서(load 113→_norm drop 119-122→make_broker 123→run_once)는 weights 생성 前이고 _norm 비교로 표기차 우회도 막힘 — 보호 자체는 안 뚫림(가드 _norm 기준).

- 근거: live_select_canslim.py:26-31,36,55; live_engine.py:19-23,169-170; run_live.py:119-122; data.py:55,87
- 권고: go-live 전 `python -c "import live_select_canslim"` 사전 점검을 체크리스트에 추가. 가능하면 engine.funda 필요부 벤더링 또는 형제경로를 환경변수/명시 설정으로 고정. 인덱싱·drop 순서는 조치 불요(단 위 Major 패널 staleness 와 함께 보면 정상 인덱싱이라도 stale 종목엔 stale 신호 계산됨 유의).
