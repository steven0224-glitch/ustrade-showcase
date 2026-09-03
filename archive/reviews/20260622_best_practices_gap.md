# 자동매매 베스트프랙티스 웹 수집 → 내 시스템 갭분석 (2026-06-22, 최종·재검증 완료)

검토용 정리. **아직 아무것도 적용 안 함.** 오늘 밤 KMI 1주 검증과 무관하게 진행 가능.

## 방법·집계
6개 소스 병렬 웹 수집(워크플로 116 에이전트):
GitHub OSS(freqtrade/jesse/nautilus_trader/lumibot/QuantConnect Lean/hummingbot) · X/Threads/Reddit r/algotrading blowup · 뉴스/HN/GeekNews(Knight Capital 등) · 퀀트 리스크 문서(FIA/SEC 15c3-5/Alpaca/QC) · 브로커 API 엔지니어링 · 한국 리테일(토스/KIS/키움).

**121 원시 → 64 구별 → 20 이미커버·NA / 34 확정갭 / 10 오탐기각.**
(1차 40갭·4오탐 → 적대적 재검증 후 6건이 "이미 커버"로 추가 기각 → 34갭·10오탐. 전 항목 검증 완료.)

## 총평 (합성 에이전트)
> 자본보존 코어(리컨실·드로다운 서킷·슬리브 캡)는 이미 견고. 진짜 빈 곳은 **단 하나의 P1 — 'HALT가 청산까지 얼려버리는' 데드락**을 깨는 panic_exit 원샷이고, 나머지는 KMI 1주 검증 동안 데이터를 쌓으며 P2/P3로 천천히 적용해도 된다.

---

## 🔴 P1 — panic_exit.py 원샷 (HALT-청산 데드락 해소)
**무엇**: 지금 `state/HALT`를 켜면 GuardedBroker가 SELL도 막고 `run_exit`도 `is_halted`에서 조기반환(run_exit.py:73)해 청산 루프 자체가 안 돈다. '폭주매수 끊기'와 '기존 포지션 비우기'가 상호배타 → 무인 상황에서 손실을 끊는 단일 청산 명령이 없다.
**왜 P1**: 인벤토리상 **유일하게 '안전 의도가 정반대로 동작'**하는 항목. 다른 갭은 방어 한 겹이 빈 수준이지만 이건 비상 탈출구가 잠긴 구조.
**보수적 권고**: GuardedBroker는 절대 손대지 말고 별도 `panic_exit.py`만 추가.
1. run_exit와 동일 RunLock 임계구역(동시매도 레이스 차단)
2. `ManagedBroker.get_positions()`의 **봇 관리분만** `OrderRequest(SELL,qty,MARKET)` → protected 11종목 불가침 자동보장
3. `_await_fills` 후 잔존 미체결 취소(run_exit.py:124-131 패턴 재사용)
4. 완료 후 `KillSwitch.trip(reason='panic flatten', kind='manual')`
5. 정규장 외엔 '취소+HALT trip'만, 청산은 다음 개장 첫 틱. is_halted 우회는 청산(위험축소) 방향에 한정, BUY 절대 불가.

---

## 🟠 P2 — 중기 (전부 toss_check.py 실증 선행 권장)

1. **단계적 정지(reduce-only)** `GAP` — HALT 중에도 화이트리스트(daily_loss/total_drawdown/error)면 청산 SELL만 통과, bad_equity/수동HALT/상태손상 kind는 SELL도 차단(오판매도 방지). enum 신설 없이 `is_halted`가 halt_kind 노출. **회귀테스트 필수**: '드로다운 트립 시 청산 체결+진입 거부' & 'bad_equity/수동HALT 시 SELL도 거부'.
2. **거래소측 손절(STOP 주문)** `GAP` — 현 손절은 cron·PC 가용성에 100% 종속. 선결: toss_check로 `/orders`가 STOP+stopPrice+GTC 수용하는지 실증(toss.py:290이 'GTC 미지원' 가정 박아둠 → 부분 NA 가능). 미지원 시 heartbeat.py를 exits.jsonl도 감시하게 보강(현 runs.jsonl만).
3. **슬리피지 칼라 + marketable LIMIT** `PARTIAL` — 전 주문 MARKET 고정, 얇은호가/재개경매 갭 방어 0개. (A) 비파괴 사후탐지 먼저: check_order_notional 옆에 'last 대비 호가 이탈>1%면 해당 주문 skip→partial' (ref는 라이브시세와 독립=avg_price/캐시종가). (B) 청산 SELL부터 marketable LIMIT(last*(1-collar)). 매수 LIMIT은 갭업 체결률 저하라 페이퍼 선검증.
4. **멱등 강화** `PARTIAL` — 토스 서버측 clientOrderId 멱등이 문서 미확정인데 `_request`가 5xx 후 POST를 max_retries=2 자동 재전송 → 응답누락 시 더블체결 경로. (1) 주문 POST만 max_retries=0(조회·취소는 유지), 미확정분은 reconcile_basis 위임. (2) **코드보다 우선** toss_check로 동일 clientOrderId 재전송 422/거부 1회 실측.
5. **관찰성·새너티 묶음(effort low)** `PARTIAL` — ① In-flight no-ACK 능동조회 ② econ_monitor(runs/exits.jsonl 되읽어 N세션 무체결·reject반복 탐지, 읽기전용 신설) ③ 시세 frozen-tick 신선도(base.py Quote에 ts 옵셔널 → run_exit 임계 60~90s 초과면 live[s]=None) ④ 체결보고 무신뢰 검증(record_fills에 fq=min(fq,req.qty) 클램프, get_order 심볼불일치→SUBMITTED) + spike sanity + 코드버전 스탬핑.

기타 P2: 백테-라이브 패리티, 응답값 무신뢰 검증, LULD 인지, 상태파일 원자적 쓰기, 배포 일관성, 스테이지드 롤아웃, 백테스트 슬리피지 모델, 외부입력 자동행동 주의, 비공식 API 방어적 파싱.

---

## 🟡 P3 — 선택(낮은 우선, 무인 운영 길어질 때)
- **연속 손절 가드** — killswitch.json에 stop_events:[] 추가, 우선 notify-only 후 trip 승격
- **재진입 쿨다운 / 저수익 격리** — 당일은 mark_traded가 이미 차단, 실노출은 익일 1틱뿐. opt-in 기본 비활성
- **주문율 스로틀 / 레이트리밋 페이싱** — max_orders_per_run(기본12) + toss `_request` min_interval(0.25s, FMP 패턴)
- **미체결 TTL 안전스윕 / 콜드스타트 정리** — toss list_open_orders 실증 후 '관리슬리브+나이>1세션'만 cancel. 전역 cancel_all 금지(수동주문 보호)
- **백테스트 슬리피지 반영** — 리서치 엔진 turnover*fee를 (fee+slippage)로 분리, paper.py와 일관(0.0005~0.001)
- **스키마 드리프트 가시화 + 코드버전 스탬핑** — get_order 미지 status에 warning 1줄(동작불변), OneDrive '충돌된 사본' .py 존재 시 거래 거부(fail-closed)
- **상태파일 fail-loud / 감사로그 / 반복체결 한도** — load_sleeve 손상JSON try/except raise, trip()에 gate_events.jsonl append, mark_traded에 buy_streak WARN
- 지수백오프+지터, 유니버스 안전필터, tick/lot 라운딩, 이상치 스파이크, 시도(attempt)기준 기록, 미체결 시 신규금지 게이트, 라이브러리 필드명 어서션

---

## ✅ 이미 잘 갖춤 (안심, covered/NA 20건)
1. 기동/매 실행 시 3계층 리컨실리에이션 — reconcile_basis(브로커=진실원천, min(실보유,basis+pending), ghost position 차단) + _reconcile 드리프트 알림 + pending write-ahead intent log
2. 자동 드로다운 서킷브레이커 — 일손실 5%·누적DD 20%(영속 HWM)·HALT 파일·에러윈도우(3/6), GuardedBroker 주문경계서 우회불가 강제
3. 잔고/익스포저 캡 — cash_cap 물리적 상한 + 슬리브 protected/managed 분리로 레버리지ETF 11종목 불가침 구조보장
4. 포지션 사이징 — 종목 40%·gross 105%·fat-finger 명목캡(buffer 1.5)·NaN fail-closed, 주문별 강제
5. 결정론 멱등키 — sha1(day|symbol|side|qty) + RunLock O_EXCL + 당일1회 mark_traded → 더블트레이드 3중 차단
6. 주문 상태머신 + 전량순회 — _STATUS_MAP 종단매핑, _await_fills break 없이 전 미체결 순회(버스트 동시체결 버그 구조차단), PARTIAL basis 흡수
7. 버잉파워 사전조회 + 현금 어포더빌리티 사이징 — alloc 0.95 + cost_buffer 0.5%, insufficient-buying-power graceful REJECTED
8. 토큰 수명관리 — 만료 60초 전 선제 reconnect(monotonic, NTP 면역), 401 1회 재인증+재귀가드
9. 최악가정 자본고갈 구조적 불가 — 리밸런서라 하락마다 매수 적층 안 함, SPY<200MA면 전량현금화
10. NA-by-design 정확 식별 — PDT·IBKR reqIds·웹소켓 구독한도·Kafka/SQS·KIS 도메인분리 등 구조에 없는 위협을 과설계 없이 배제

## 🚫 오탐 기각 (웹이 권했으나 이미 동등·우월 커버, 10건)
TIF IOC/FOK(토스 미지원+MARKET-only, 잔존취소 sweep이 커버) · 자기체결/워시트레이드(executor 종목당 한방향+RunLock 단일실행→양방 resting 불가) · 주문 전 사전조건 점검(4축 이미 구현·테스트) · **최대 장중 누적포지션 한도**(일1회+diff plan이 누적 봉쇄) · **Cancel-On-Disconnect**(원샷 프로세스+잔존취소 sweep) · **손절 우선 실행**(check_exits가 이미 가격기반 우선청산) · **미국장 시간 게이트**(toss.market_open이 이미 함) · **메인→서브 페일오버**(토스 단일계좌라 대체 브로커 없음=NA) · **환전·USD 검증**(USD뷰 정상 확인됨) · **구조화된 에러 분류**(4xx REJECTED / 5xx raise / 401 재인증 이미 분기).

---

## 오늘 밤 KMI 1주 — 합성 에이전트 권고
검증 자체는 **안전하게 진행 가능**(코어 자본보존 견고, PC 동석=사람이 곧 킬스위치). 단 두 가지:
1. **검증 전 코드 변경은 P1 panic_exit '신규 파일 추가'까지만.** GuardedBroker·executor·toss 본체를 건드리는 P2(멱등 max_retries=0, 슬리피지 칼라, reduce-only)는 KMI 1주 동안 **실제 토스 응답 데이터를 모으고 toss_check로 멱등계약·STOP·타임스탬프·orderbook 스펙을 실증한 뒤** 적용 → 추측구현 회피.
2. **가장 위험한 시나리오 = panic_exit 부재 상태에서 폭주매수 버그**. 검증 중 그게 오면 **토스 앱에서 직접 수동 매도**할 준비를 해두고, 검증 직후 P1을 최우선 적용.
3. 검증 1주의 부산물로 실증 로그(멱등 거부코드·STOP 수용여부·/prices 타임스탬프·중복 status값)를 의식적으로 수집하면 이후 P2가 추측 없이 풀린다.
