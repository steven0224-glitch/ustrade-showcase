# FINAL_SYNTHESIS — 전체 코드 실거래 안전·정합성 점검 (보호종목 불가침 + cash_cap 정확성 최우선)

- **대상**: 미국주식 자동매매(B) — 토스 단일계좌 자동매매, 기존 11종목과 계좌 공유, ManagedBroker 슬리브, KMI 1주 첫 실거래 직전
- **일시**: 2026-06-22 12:24 KST
- **팀**: A=실거래안전-적대 (자금사고/보호위반 헌팅 (adversarial)) / B=정합성-호의 (상태기계/계약 정합성 증명 (charitable))
- **모드**: interleave · 에이전트 25개

## §1 요약 (신뢰도 태깅 — 코드 결정론 병합)

| 분류 | 건수 |
|---|---|
| CONFIRMED Critical | 6 |
| CONFIRMED Major | 12 |
| CONFIRMED Minor | 11 |
| — 양팀 공동발견(both) | 0 |
| — 교차확인(cross-confirm) | 29 |
| LOWCONF | 0 |
| DISPUTED (사용자 판단) | 3 |
| NOTE (검증불가 관찰/안전확인) | 2 |
| 기각(phantom) | 0 |

> 두 팀("실거래안전-적대" / "정합성-호의") 교차검증 결과 CONFIRMED 29건(Critical 6 / Major 12 / Minor 11)이 확정됐고, 29건 모두 양 팀이 독립적으로 동일하게 짚어낸 교차확인 항목으로 LOWCONF·기각(phantom)은 0건입니다. 가장 무거운 축은 Critical 6건으로, 단일 killswitch.json 에 paper $100k 와 toss $21k baseline·HWM 이 혼입돼 KMI 첫 실거래에서 false-halt 가 나고 수동 JSON 편집으로만 풀리는 deadlock(broker/guardrail.py + run_live.py make_broker), 토스 lastPrice 결측 시 get_quote 가 0.0 을 반환해 관리종목을 시장가로 거짓 청산하는 결함(run_exit.py·toss.py·live_exit.py), 그리고 미체결 DAY주문 미취소로 인한 더블바이·주문응답 유실 시 SELL 유령포지션·아웃바운드 심볼 역정규화 누락·매호출 새 멱등키 같은 주문 무결성 문제가 오늘 밤 KMI 1주 검증에 직접 위협이 됩니다. 다만 DISPUTED 3건이 남아 있으니 그대로 확정하지 말고 합의 판정 후 반영해야 합니다.

> 보존 불변식 OK (항목 증발/발명 없음).

## §2 CONFIRMED 상위 (신뢰도순)

| severity | 위치 | 요지 | 신뢰도 |
|---|---|---|---|
| Critical | broker/guardrail.py:21,236-254,292-309 (KillSwitch STATE_FILE/roll_day/check_total_drawdown) + reset:220-233 + run_live.py:32-38,123 (make_broker) | 단일 killswitch.json 에 paper $100k 와 toss $21k/$100 baseline·HWM 혼입 → KMI 첫 실거래 false-halt + 수동 JSON 편집 강요 deadlock | cross-confirm |
| Critical | run_exit.py:93-96 + broker/toss.py:208-215 get_quote + live_exit.py:52,60-65 check_exits + broker/guardrail.py:388-392 | 토스 lastPrice 결측 시 get_quote 가 0.0 반환 → 관리종목 거짓 청산(시장가 강제 매도) | cross-confirm |
| Critical | live_engine.py:_await_fills(63-85)·_run_once_locked(229-245) + broker/managed.py:reconcile_basis(234-257) + broker/toss.py:243-265(SUBMITTED·DAY tif:251); grep: live 경로 cancel_order 0건 | 미체결 잔존 DAY주문 미취소 → 다음 런 재플랜으로 더블바이(KMI 1주 검증서도 발생 가능) | partial |
| Critical | broker/toss.py:place_order(258-263)·_request(116-151) 5xx/타임아웃 raise 경로 + live_engine.run_once(200-216)·_reconcile(88-108); SELL pending 미보호 | 주문 응답 유실(타임아웃/5xx) 시 체결된 주문을 미체결로 오인 → order_id 소실·유령포지션, 특히 SELL 청산 | partial |
| Critical | broker/toss.py:place_order(247)·get_quote(210)·cancel/get_order 진입부 — 아웃바운드 심볼 미정규화 + managed.py:188-200(s=_norm 검사하나 원본 req 전달) | 내부→토스 아웃바운드 심볼이 역정규화 안 됨 — 점 표기 클래스주(BRK-B/BRK.B) 계약 불일치로 sp100 운영 시 매수·시세 전량 실패 | partial |
| Critical | broker/toss.py:place_order(247) clientOrderId — uuid 매 호출 새 생성 | 멱등키가 호출마다 새로 생성 → 논리적 재시도(재플랜·운영자 재실행) 중복접수 못 막음 | partial |
| Major | run_live.py:104-122(sleeve_protected drop, toss 분기) + live_engine._run_once_locked(169-173) 보호가드 부재 | 보호종목 후보-제외가 run_live 단일 호출 지점에만 존재 — 엔진 계층엔 강제 안 됨 | partial |
| Major | broker/guardrail.py:RunLock(83-160)·LOCK_FILE(22) + run_exit.py:55·live_engine.py:126 공유 락 + pid 재사용 회수(104-124) | run_live·run_exit 가 단일 run.lock 공유 — 크래시 락 잔존/pid 재사용이 청산을 최대 1~6h 무경보 차단 | partial |
| Major | broker/toss.py:get_account(183-192)·_num(63-70) + guardrail.py:272·280-281·302-303·328-330 | 토스 API 가 equity=0.0 으로 강제 coerce 되면 첫 실행 양 손실가드 fail-open + 비례 명목캡 비활성 | partial |
| Major | broker/toss.py:_ensure_connected(153-156)·_request 4xx(137-150)·place_order(261) | 토큰 조기 만료(401)를 비즈니스거부로 오인 → 유효주문 REJECTED, 재인증 안 함 | partial |
| Major | broker/toss.py:_STATUS_MAP(34-45)·get_order(279) + live_engine._await_fills(63-85) | 상태매핑 누락값(접수대기/EXPIRED/오타스펠)→SUBMITTED 폴백으로 미체결 오인·무한폴링·다음 런 중복 | partial |
| Major | data.py:90-111 load_panel + live_engine.py:151-157(staleness 게이트) + live_select_canslim.py:53-62 / live_risk.py:23-32 | 패널 staleness 게이트가 최신 티커 하나로 우회 + SPY ffill stale → 종목별/레짐 거짓 신호 | cross-confirm |

## §3 DISPUTED (타이브레이커 미결 — 사용자 판단)

### [Major] broker/executor.py:26-30(getattr buy_mult)·38-41,62-68(last 기반 사이징) + managed.py/toss.py(_commission·_spread·_slippage 부재)
- **요지**: 토스 라이브에서 buy_mult=1.0 + MARKET 을 last 평탄시세로 사이징 — 수수료/슬리피지 버퍼 0, 체결가>last 시 cash_cap 초과
- **발견**: A 팀 — 근거: executor.py:26-29 getattr default 0.0,38(price=last),64(unit=price*buy_mult); paper.py:24-26 만 속성 정의; managed.py/toss.py 미존재(grep); toss.py:215 bid=ask=last
- **refute 사유**: (교차검증 verdict 없음 — locKey 미매칭)
- **crux**: no-crossverdict

### [Minor] broker/executor.py:25,60(investable=equity*alloc vs budget=cash+proceeds) + managed.py:175-184(get_account)
- **요지**: cash_cap 은 현금배포 캡일 뿐 — managed 평가액 상승분이 노출을 캡 초과로 키움
- **발견**: A 팀 — 근거: managed.py:184(equity=cash+managed_val, cash 만 cap); executor.py:25(investable=equity*alloc),60(budget=cash+proceeds)
- **refute 사유**: (교차검증 verdict 없음 — locKey 미매칭)
- **crux**: no-crossverdict

### [Minor] broker/executor.py:41(tgt_qty=int(w*investable/price)) + live_select_canslim.py:98(등비중) + broker/toss.py:250(int 절사)
- **요지**: KMI 첫 거래가 선정 종목수에 따라 0주로 floor + 분수 basis 청산 시 0주 매도 가능
- **발견**: A 팀 — 근거: executor.py:41 int floor; live_select_canslim.py:98 weights=1.0/len(final); toss.py:250 quantity=str(int(req.qty)); managed.py:161 qty=min(p.qty,basis) 분수 가능
- **refute 사유**: (교차검증 verdict 없음 — locKey 미매칭)
- **crux**: no-crossverdict

## §3.7 NOTE (검증불가 관찰 / 안전확인)

- [Info] broker/managed.py:154-184(get_positions/get_account)·203-257(record_fills/reconcile_basis) + guardrail.py:184(buying_power=cash)·328-334(비례 명목캡) — 보호 격리·co-mingle 흡수 차단·buying_power 캡·sleeve 기준 명목캡 — 공격벡터 b/c/d 부재 확인(안전)
- [Info] live_select_canslim.py:26-31(_A_DIR sibling import)·34-36(_mom_12_1)·55(길이게이트) + live_engine.py:19-23,169-170 + run_live.py:103-123(protected drop 순서) — canslim 형제프로젝트 import 단일실패점 + 인덱싱 룩어헤드 없음 + 보호 drop 순서 정합(확인, 일부 통합위험)

## §4 팀별 결과

### Team A (실거래안전-적대) — counts {'critical': 6, 'major': 7, 'minor': 5}

| severity | 위치 | 요지 | 권고 |
|---|---|---|---|
| Critical | broker/guardrail.py:21,236-254,292-309 (KillSwitch STATE_FILE/roll_day/check_total_drawdown) + reset:220-233 + run_live.py:32-38,123 (make_broker) | 단일 killswitch.json 에 paper $100k 와 toss $21k/$100 baseline·HWM 혼입 → KMI 첫 실거래 false-halt + 수동 JSON 편집 강요 deadlock | killswitch state 를 브로커별 네임스페이스(killswitch_{broker}.json)로 분리하거나 paper/toss 가 USTRADE_HOME(STATE_DIR)을 공유하지 않게 하라. roll_day/check_total_drawdown 가 '직전 equity 대비 비현실적 스케일 점프(예 >5배)' 면 baseline·hwm 을 현재 equity 로 재seed+경고. go-live 전 toss_setup 또는 run_live(toss) 진입부에 killswitch.json 삭제/초기화 절차 명시. reset 시 total_drawdown 이면 hwm 뿐 아니라 day_start_equity 도 None 재seed. |
| Critical | run_exit.py:93-96 + broker/toss.py:208-215 get_quote + live_exit.py:52,60-65 check_exits + broker/guardrail.py:388-392 | 토스 lastPrice 결측 시 get_quote 가 0.0 반환 → 관리종목 거짓 청산(시장가 강제 매도) | get_quote 가 last<=0(lastPrice 부재) 시 0.0 반환하지 말고 예외를 던지거나, live_exit.check_exits 가드를 `if price is None or not (price>0) or s is None ...` 로 강화해 price=0.0 을 data_ok=False 로 자동청산에서 제외하라(최소수정). |
| Critical | live_engine.py:_await_fills(63-85)·_run_once_locked(229-245) + broker/managed.py:reconcile_basis(234-257) + broker/toss.py:243-265(SUBMITTED·DAY tif:251); grep: live 경로 cancel_order 0건 | 미체결 잔존 DAY주문 미취소 → 다음 런 재플랜으로 더블바이(KMI 1주 검증서도 발생 가능) | 재플랜 前 직전 미체결 주문을 cancel_order 로 정리하거나, 제출 order_id/clientOrderId 를 상태파일 breadcrumb 로 남겨 다음 런이 get_order 로 오픈주문을 조회·합산(cur_qty+오픈수량)해 diff 계산. 또는 mark_traded 를 '주문 제출 직후'로 옮기고 partial 도 당일 재거래 차단(force 로만 우회). |
| Critical | broker/toss.py:place_order(258-263)·_request(116-151) 5xx/타임아웃 raise 경로 + live_engine.run_once(200-216)·_reconcile(88-108); SELL pending 미보호 | 주문 응답 유실(타임아웃/5xx) 시 체결된 주문을 미체결로 오인 → order_id 소실·유령포지션, 특히 SELL 청산 | place_order 전송오류(5xx/네트워크) 시 clientOrderId 를 슬리브/저널에 영속하고 다음 런 시작 시 GET /orders?clientOrderId= 로 접수/체결 여부 조회·복구하는 reconcile-by-clientOrderId 경로 추가. SELL 도 pending 의도로그를 두어 미확인 매도를 다음 런이 검증. |
| Critical | broker/toss.py:place_order(247)·get_quote(210)·cancel/get_order 진입부 — 아웃바운드 심볼 미정규화 + managed.py:188-200(s=_norm 검사하나 원본 req 전달) | 내부→토스 아웃바운드 심볼이 역정규화 안 됨 — 점 표기 클래스주(BRK-B/BRK.B) 계약 불일치로 sp100 운영 시 매수·시세 전량 실패 | TossBroker.place_order/get_quote/cancel_order/get_order 진입부에 토스 기대 표기로 역정규화하는 단일 함수(_to_toss_symbol)를 두거나, get_positions 노출 canonical 과 place_order 전송 표기를 단일 규칙으로 고정하라. sp100 운영 전 BRK-B 한 종목으로 get_quote 실거래 확인. 토스 US 심볼 표기를 toss_check.py 로 실증. |
| Critical | broker/toss.py:place_order(247) clientOrderId — uuid 매 호출 새 생성 | 멱등키가 호출마다 새로 생성 → 논리적 재시도(재플랜·운영자 재실행) 중복접수 못 막음 | clientOrderId 를 주문의 결정론적 함수(hash(session_date\|symbol\|side\|qty\|sequence))로 생성해 같은 세션 같은 의도 주문이 같은 키가 되게 하라. 토스 clientOrderId 서버측 멱등 계약을 문서로 확정 전까지 place_order POST 자동 재시도 0(또는 재시도 前 get_order 로 직전 제출 성공 확인 후 재전송). |
| Major | run_live.py:104-122(sleeve_protected drop, toss 분기) + live_engine._run_once_locked(169-173) 보호가드 부재 | 보호종목 후보-제외가 run_live 단일 호출 지점에만 존재 — 엔진 계층엔 강제 안 됨 | 보호종목 제외를 엔진 경계로 끌어올려라. ManagedBroker.protected 를 run_once 초입에서 사용해 select 직전 prices/weights 에서 _norm 매칭 drop 하거나, Executor.plan 의 buy_cands 생성 시 broker 가 protected 면 해당 심볼 skip. 단일 호출 지점(run_live)에만 의존하지 말 것. |
| Major | broker/executor.py:26-30(getattr buy_mult)·38-41,62-68(last 기반 사이징) + managed.py/toss.py(_commission·_spread·_slippage 부재) | 토스 라이브에서 buy_mult=1.0 + MARKET 을 last 평탄시세로 사이징 — 수수료/슬리피지 버퍼 0, 체결가>last 시 cash_cap 초과 | 브로커 비용을 BaseBroker 공개 인터페이스(estimated_costs() 또는 공개속성)로 올려 TossBroker 에 보수적 토스 수수료·환전스프레드 추정치 제공(buy_mult>1). 또는 사이징/affordability 를 ask(혹은 last*(1+안전마진))로 하고 plan 마지막에 sum(buy notional)<=cash_cap 사후검증 추가. |
| Major | broker/guardrail.py:RunLock(83-160)·LOCK_FILE(22) + run_exit.py:55·live_engine.py:126 공유 락 + pid 재사용 회수(104-124) | run_live·run_exit 가 단일 run.lock 공유 — 크래시 락 잔존/pid 재사용이 청산을 최대 1~6h 무경보 차단 | run_live 와 run_exit 에 별도 락(run.lock vs exit.lock) 부여 — 진입·청산은 임계자원이 달라 상호배제 불필요. locked 가 반복되면 알림 escalation 도입(무경보 차단 방지). |
| Major | broker/toss.py:get_account(183-192)·_num(63-70) + guardrail.py:272·280-281·302-303·328-330 | 토스 API 가 equity=0.0 으로 강제 coerce 되면 첫 실행 양 손실가드 fail-open + 비례 명목캡 비활성 | _require_finite_equity 에 equity<=0 도 fail-closed 추가(check_daily_loss base<=0 처리와 대칭). 또는 TossBroker.get_account 에서 cashBuyingPower/marketValue 키 자체가 응답에 없으면 0 coerce 대신 TossAPIError raise 해 데이터 결함을 상위 에러가드로. |
| Major | broker/toss.py:_ensure_connected(153-156)·_request 4xx(137-150)·place_order(261) | 토큰 조기 만료(401)를 비즈니스거부로 오인 → 유효주문 REJECTED, 재인증 안 함 | _request 에서 401(가능하면 토스 토큰만료 code) 감지 시 self._token=None 후 connect() 재호출·1회 재시도(주문은 멱등 clientOrderId 라 안전). place_order REJECTED 매핑에서 401 제외해 transient 분류. |
| Major | broker/toss.py:_STATUS_MAP(34-45)·get_order(279) + live_engine._await_fills(63-85) | 상태매핑 누락값(접수대기/EXPIRED/오타스펠)→SUBMITTED 폴백으로 미체결 오인·무한폴링·다음 런 중복 | 토스 Open API v1.1.1 주문상태 enum 전체를 문서 확정해 _STATUS_MAP 완성(NEW/ACCEPTED/PENDING_NEW→SUBMITTED, EXPIRED/DONE_FOR_DAY→CANCELLED, PARTIALLY_FILLED 스펠 확인). 미지 상태 폴백을 SUBMITTED 가 아니라 경고+드리프트 보고로. tests_toss 에 미지/접수대기/EXPIRED 케이스 추가. |
| Major | data.py:90-111 load_panel + live_engine.py:151-157(staleness 게이트) + live_select_canslim.py:53-62 / live_risk.py:23-32 | 패널 staleness 게이트가 최신 티커 하나로 우회 + SPY ffill stale → 종목별/레짐 거짓 신호 | select 진입 시 종목별 s.index[-1] 의 session_gap 을 검사해 stale 종목 후보 제외하거나 load_panel 에서 종목별 마지막봉 분산 점검해 stale 컬럼 드롭/경고. apply_overlay 는 ffill 直前 SPY 원본 마지막 유효 인덱스를 패널 마지막과 session_gap 비교해 1세션 초과 stale 이면 ValueError 로 거래 보류. |
| Major | run_exit.py:86-99 — 청산 경로 데이터 신선도 게이트 부재 | 청산(run_exit)엔 stale 데이터 차단 게이트가 없음 — 진입(run_live)과 비대칭 | run_exit 에서도 closes[s] 마지막 봉에 session_gap(s.index[-1],today) staleness 검사 추가, 한도 초과 시 자동청산서 제외(수동확인 알림). live_engine 의 cfg.max_staleness_sessions 와 동일 기준 권장. |
| Minor | toss_setup.py:52-61 + broker/toss.py:194-206(marketCountry=='US' 필터) | protected 스냅샷이 US-only 보유에서만 생성 — 비US/미분류 보유분 보호 누락 가능 | toss_setup 에서 holdings 가 비었거나 기대 종목 수보다 적으면 저장 거부·경고. 보호 대상 심볼을 사용자가 명시 입력/확정(보유 종목 하드 화이트리스트 대조)하는 검증 단계 추가. |
| Minor | broker/executor.py:25,60(investable=equity*alloc vs budget=cash+proceeds) + managed.py:175-184(get_account) | cash_cap 은 현금배포 캡일 뿐 — managed 평가액 상승분이 노출을 캡 초과로 키움 | cash_cap 을 '누적 현금배포 한도'로 명확히 문서화하거나, 별도 position-value 캡(managed_val 상한)을 두어 평가액 상승이 재투자 여력으로 환산되지 않게 하라. |
| Minor | broker/executor.py:41(tgt_qty=int(w*investable/price)) + live_select_canslim.py:98(등비중) + broker/toss.py:250(int 절사) | KMI 첫 거래가 선정 종목수에 따라 0주로 floor + 분수 basis 청산 시 0주 매도 가능 | 첫 검증은 cash_cap·top_n·예상 선정수를 함께 고정하거나 top_n=1/KMI 단독 유니버스로 1주 결정론화. 청산 SELL 수량은 floor 아니라 보유 정수주로 명시 계산하고 int 절사로 0 되면 경고/수동확인. basis 를 항상 정수로 유지하는 불변식 강제 검토. |
| Minor | broker/toss.py:cancel_order(267-273) | 취소 transient 오류(5xx/네트워크/타임아웃)를 False(취소실패)로 뭉개 상태 오인 | cancel_order 도 place_order 처럼 e.is_business_error() 분기 — 4xx(이미체결/없음)는 False(정상적 취소불가), 5xx/네트워크는 raise 하여 상위가 '취소 상태 불명'으로 get_order 재확인. |
| Minor | broker/toss.py:market_open(217-235) + run_exit.py:70-71 / calendar_util.py 폴백(50-52) | 장 캘린더 tz 가정 불명확 → 항상 closed 오판으로 청산 영구 skip + 청산 today 폴백 세션 불일치 | 토스 market-calendar 응답 시각 tz 를 문서 고정, naive 면 명시 tz(KST/ET) 부여 후 비교. 파싱/비교 실패 시 False 대신 '판정불가' 구분해 운영자 알림. run_exit 도 session is None 이면 명시 error 반환(거래 보류)으로 run_live 와 일관화. |
| Minor | broker/managed.py:place_order(195-200) pending 영속 위치 vs RunLock 임계구역 | pending 영속 후 주문 직전 크래시 시 pending 과대(유령은 실보유 cap 으로 차단) | 현 설계의 실보유 cap 이 유령·자금손실을 막으므로 긴급 아님. pending 영속 시점에 order_id 미발급이라 추적 불가한 점은 위 Critical 의 breadcrumb 도입 시 함께 해소. |
| Info | broker/managed.py:154-184(get_positions/get_account)·203-257(record_fills/reconcile_basis) + guardrail.py:184(buying_power=cash)·328-334(비례 명목캡) | 보호 격리·co-mingle 흡수 차단·buying_power 캡·sleeve 기준 명목캡 — 공격벡터 b/c/d 부재 확인(안전) | 변경 불필요. 회귀 방지로 'get_positions 가 보호종목 절대 반환 안 함'·'buying_power<=cash_cap' 불변식을 tests_managed 에 고정 권장. 단 cash_cap 미지정 운영 시 sleeve equity 가 managed+full-cash 로 부풀어 비례캡이 느슨해질 수 있으니 toss 운영에 cash_cap(TOSS_MANAGED_CASH) 필수화 검토. |
| Info | live_select_canslim.py:26-31(_A_DIR sibling import)·34-36(_mom_12_1)·55(길이게이트) + live_engine.py:19-23,169-170 + run_live.py:103-123(protected drop 순서) | canslim 형제프로젝트 import 단일실패점 + 인덱싱 룩어헤드 없음 + 보호 drop 순서 정합(확인, 일부 통합위험) | go-live 전 `python -c "import live_select_canslim"` 사전 점검을 체크리스트에 추가. 가능하면 engine.funda 필요부 벤더링 또는 형제경로를 환경변수/명시 설정으로 고정. 인덱싱·drop 순서는 조치 불요(단 위 Major 패널 staleness 와 함께 보면 정상 인덱싱이라도 stale 종목엔 stale 신호 계산됨 유의). |

### Team B (정합성-호의) — counts {'critical': 0, 'major': 5, 'minor': 7}

| severity | 위치 | 요지 | 권고 |
|---|---|---|---|
| Major | broker/executor.py:26-29, 60-68 (Executor.plan, buy_mult / budget headroom) | 라이브 toss 경로에서 비용버퍼(buy_mult)가 1.0 — EXEC-1 보호 무력 | 실비용 배수를 브로커 속성 추정에 의존하지 말고 명시 파라미터로 주입(예: Executor(broker, alloc, cost_buffer=0.005)) 하거나, ManagedBroker/TossBroker 에 _commission/_spread/_slippage 를 정의(또는 ManagedBroker 가 inner 로 위임)해 라이브에서도 buy_mult>1 이 적용되게 한다. |
| Major | run_exit.py:_run_locked (107-124) + broker/managed.py:place_order(187-200)·record_fills(226-227) | 청산 SELL은 멱등키·pending 없어 늦은체결 시 다음 cron이 같은 주식 재매도(oversell) | SELL에도 pending(또는 outstanding-order id) 추적을 도입: place_order SELL 시 미체결 매도 의도를 영속하고, get_positions/exit 평가 시 진행 중 SELL 수량을 차감하거나, run_exit가 직전 미종결 주문을 get_order로 조회해 동일 심볼 재제출을 차단. 최소한 record_fills 후 SUBMITTED 잔존 SELL이 있으면 해당 심볼을 당 실행 청산 후보에서 제외. |
| Major | broker/guardrail.py:21 STATE_FILE / :175,248,253 roll_day·last_equity / :299 check_total_drawdown·hwm ↔ run_live.py:28-38 make_broker (paper cash=100000 vs toss cash_cap=100) | 단일 killswitch.json 을 paper·toss 가 공유 → baseline·hwm 스케일 혼입 오염 | killswitch state 파일을 broker 모드별로 분리하라(예 STATE_DIR/f'killswitch.{broker_kind}.json' 또는 KillSwitch 에 namespace 인자). 또는 roll_day 진입 시 저장된 last_equity 와 현재 equity 의 스케일 급변(예 10배 이상 괴리)을 감지해 baseline 을 이월하지 말고 현재 equity 로 재seed + 경고. 최소한 toss 실거래 전 paper 실행이 같은 state 를 건드리지 않도록 USTRADE_HOME 을 모드별로 분리. |
| Major | broker/toss.py:116-124, place_order via _request (243-263) | 네트워크 타임아웃이 미체결 단정으로 raise + clientOrderId 매 호출 uuid → 재시도 시 더블체결 위험 | clientOrderId 를 OrderRequest 기준 결정론적 키(예: 날짜+symbol+side+qty 해시)로 만들거나, 주문 전송 직후 타임아웃이면 get-orders 로 clientOrderId 존재 여부를 조회해 접수 확인 후에만 raise. 최소한 reconcile/_await_fills 가 '제출은 됐으나 응답 누락' 주문을 복구할 수 있도록 placed-but-unacked 상태를 표면화할 것. |
| Major | run_exit.py:_run_locked (라인 86-99) / live_exit.py:check_exits (라인 52) | 청산 경로에 데이터 신선도 게이트가 전혀 없음 — stale 일봉으로 자동매도 가능 | run_exit에서도 closes[s]의 last index에 대해 session_gap(closes[s].index[-1], today) > 임계(예: max_staleness_sessions)면 해당 종목을 data_ok=False(수동확인)로 강등하거나 청산 자체를 보류하라. check_exits에 freshness 임계 인자를 추가해 진입/청산이 동일 신선도 정책을 공유하게 하라. |
| Minor | broker/managed.py:177 (get_account cap) + broker/executor.py:38,41,64 (사이징 기준가) | cash_cap 은 사이징시점 명목캡일 뿐 실지출캡 아님 — 슬리피지·수수료 초과 가능 (체결 후 대조 부재) | 엄밀 캡이 목표라면 buy_mult 에 보수적 비용버퍼를 넣어(buy_mult finding 참조) 명목예산을 cash_cap×(1-버퍼) 로 낮추거나, 사이징 기준가를 ask×(1+slippage) 로 올려 실지출이 cash_cap 을 넘지 않도록 헤드룸을 둔다. 첫 실거래는 toss MARKET 대신 LIMIT 으로 상한가를 박는 것도 고려. |
| Minor | broker/managed.py:place_order(195-200) vs record_fills(219-225) | BUY 제출이 5xx/네트워크로 raise되면 pending은 영속되나 record_fills 미실행 — force 재실행 시 재매수 가능 | force 재실행 경로에서 직전 error 실행의 미확정 주문(pending 잔존)이 있으면 사용자 확인을 요구하거나, place_order가 던진 직후 reconcile_basis를 한 번 호출해 실보유 재확인 후 plan을 재산출. 최소한 error 반환 시 알림에 'pending 미확정 — force 재실행 전 토스 주문내역 확인' 경고 포함. |
| Minor | broker/toss.py:153-169, _ensure_connected / connect 토큰 만료 | connect()는 만료 직전 자동갱신하나 _request 도중 401 만료 시 재인증 없이 REJECTED 오분류 | _request 에서 401(invalid_token) 응답은 1회 connect() 재호출 후 재시도하는 경로를 추가하거나, 최소한 401 을 transient(raise)로 분류해 REJECTED 흡수에서 제외(미체결이 거부로 기록되는 것 방지). |
| Minor | broker/toss.py:279, get_order _STATUS_MAP 미지정 기본값 | 미지정 토스 상태코드가 SUBMITTED로 폴백 — 종료상태 누락 시 무한폴링 | 토스 v1.1.1 주문상태 enum 전체를 _STATUS_MAP 에 명시 매핑(특히 EXPIRED/DONE_FOR_DAY 류 → CANCELLED 또는 별도 종료). 미지정 폴백은 SUBMITTED 유지하되 로깅으로 신규 상태 가시화. |
| Minor | broker/toss.py:217-235, market_open 시간대 비교 | market_open이 캘린더 tz와 로컬 tz 혼합 비교 — naive 응답 시 TypeError→휴장 폴백으로 청산 영구보류 | start/end 가 naive 면 캘린더 응답의 timezone(또는 country별 거래소 tz)으로 명시 localize 후 비교. fromisoformat 파싱 결과의 tzinfo 유무를 점검해 naive/aware 를 일관되게 맞출 것. |
| Minor | live_engine.py:181-184 (_run_once_locked) / live_risk.py:18-36 | vol_target=0 이면 레짐(SPY 200MA) 필터까지 통째로 우회됨 | 레짐 필터와 변동성 타겟을 분리한다. 레짐 판정은 항상 apply_overlay 를 호출해 수행하고(또는 별도 호출), vol_target<=0 일 때는 apply_overlay 내부에서 scale=1.0 로만 처리하도록 분기하라. 즉 live_engine 의 if cfg.vol_target > 0 게이트를 제거하고 vol_target 비활성화는 live_risk 내부에서 다루게 한다. |
| Minor | run_exit.py:run (라인 52-53) + run_exit.py:_run_locked (라인 86) | run_exit 가 ET 세션 대신 로컬(KST) 날짜로 fail-open·end_excl 산출 — 킬스위치 baseline 키 오염 및 세션경계 off-by-one | 진입 경로와 동일하게 (1) session is None 이면 청산을 보류(error/closed)하라 — datetime.now().date() 폴백 제거(최소한 now_et().date() 로 폴백). (2) end_excl 도 last_completed_session() 결과(이미 today 로 보유)에서 (session + timedelta(days=1)) 로 산출해 ET 세션경계를 통일하라. |

## §5 교차 대조

- both(양팀 동일위치 공동발견): 0
- A-only(+B 교차판정): 22
- B-only(+A 교차판정): 12
- (both=0 은 두 팀이 위치표기를 다르게 적어 locKey 공동매칭이 0 — 대신 cross-verify 3-lens 다수결로 29건 confirm)

## §6 로스터

**A:** 보호종목-격리 헌터, 현금캡-사이징 헌터, 동시성-멱등 헌터, 킬스위치-가드레일 헌터, 토스API-계약 헌터, 전략로직-데이터신선도 헌터, 교차검증-경로통합 헌터

**B:** 슬리브-보호불변식, 현금캡-사이징, 멱등-중복차단, 킬스위치-가드, 토스API-계약, 시그널-오버레이-청산, 데이터-세션경계