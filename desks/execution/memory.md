# execution — 축적 메모리

> append-only. 손편집 금지 — `python desks/desk_memory.py append execution ...` 를 쓸 것.
> (2026-08-28 압축: corrected-쌍 병합 · PC-웨이크 3→1 · VM-복구 2→1 + 장문 항목 산문 압축. 19→15항목, 사실·규칙·근거 무손실.)

### 2026-07-11 [hazard]

MSIX 오버레이 때문에 세션에서 `%LOCALAPPDATA%\ustrade` 를 직접 읽거나 쓰면 안 된다. 검사조차
외부 태스크 경유 — 원샷 schtasks 로 덤프 스크립트 실행 → Temp 스크래치패드 복사 → 세션에서 읽기.

_근거: lessons/2026-07-11-claude-msix-overlay.md_

**규칙: 런타임 상태 확인은 항상 외부 태스크 덤프 경유. 세션에서 직접 경로 접근 금지.**

### 2026-07-12 [hazard]

**단일 런의 성공은 상태 이월의 증거가 아니다.** paper 북이 영속되지 않아 매 런 fresh $100k 로 시작
→ "누적 NAV" 측정이 원리적 불가. 비자명했던 이유: 모든 단일 런 관찰이 정상(체결·저널 equity 99,911·
텔레그램·rc 0)이라, 런 하나 안엔 없고 **런 사이(day N→N+1)에만** 있는 결함이 어떤 로그·알림·종료코드
로도 안 보였다. 문서(DoD·§B·T0 리셋)는 영속 북 전제, 코드는 미구현.

_근거: lessons/2026-07-12-crossrun-state-defect-invisible-in-single-run.md_

**규칙: 상태 이월을 주장하려면 연속 2런 저널을 대조해 이전 런 포지션이 다음 런 시작 상태에 나타나는지 확인한다. 단일 런 성공으로 갈음하지 않는다.**

### 2026-07-21 [confirmed]

PC 무인 스케줄은 결번이 구조적 — 2026-07 실측 종합(운영상 2026-08-09 VM 이전으로 폐기, 기술사실 보존):
① 07-14~16 3연속 결번 원인은 태스크가 아니라 **호스트 미가동**(StartWhenAvailable 캐치업은 07-13·17 부팅
후 2회 실증). ② **S3 절전 복귀는 캐치업 미발화 — 부팅만**(07-20 46h S3 중 06:10 결번, 수동복귀 후 캐치업 0).
③ RTC 웨이크(WakeToRun+S3, AC·정책 충족)도 46h 실전 불발 — 기존 '지원 확인'은 powercfg 능력조회였지 실측
웨이크가 아니었다. ④ deadman 은 부팅 직후 1건만, 호스트 꺼진 동안 침묵(동일 호스트 감시자 한계).

_근거: Task Scheduler LastRunTime + powercfg /a·/q + Power-Troubleshooter/Kernel-General 이벤트(07-17~21 실측)_

**규칙: 결번은 태스크 설정 前 호스트 가동이력부터 확인. 웨이크 경로는 능력조회가 아니라 실제 성공 1회로만 검증. 무인 스케줄은 상시가동 머신(VM)에만(→ 08-09 §B 항목).**

### 2026-07-23 [corrected]

VM 시계 +5:40 skew(07-23 00:47 KST 관측)는 VM 문제가 아니라 측정 오류로 판정. 증거: ① 부팅(07-21) 이래
Amazon Time Sync 연속 valid, w32tm 오류 0 ② Kernel-General Id=1 시계 스텝 이벤트 전무(±5:40 스텝이면 반드시
기록) ③ intraday_open.log 마지막 07-22 20:00:18 = 16:00 ET 장마감 정합 ④ 07-23 12:13Z 재실측 VM-PC 차 ≤1초.
00:47+5:40=06:27 KST=21:27Z → 측정 세션이 대화 시작 시각을 '현재'로 앵커한 stale-clock 오류 유력. 따라서 07-22
intraday 데이터 시계발 오염 없음.

_근거: 2026-07-23 세션 실측, VM <vm-host>(<vm-tailscale-ip>)_

**규칙: 원격 시계 skew 는 단일 왕복 명령 내 양단 epoch 동시측정 + Kernel-General 스텝 이벤트 교차확인 후에만 확정하고, 확정 前 스케줄 태스크를 재배선하지 않는다.**

### 2026-07-23 [hazard]

KIS vol-shadow 07-22 개장 65분 24종 전원 EGW00123 결손 — 근본원인 **KIS 토큰 동일반환(alias) 함정**: 유효기간
내 재발급에 기존 토큰 반환하면서 `expires_in`은 전체값(86400). Admin ssh 스모크와 SYSTEM 태스크가 **계정별 state
분리**(`%LOCALAPPDATA%\ustrade` vs `systemprofile\...\ustrade`)로 다른 만료 기록 → SYSTEM 캐시가 만료를 +87분
과대평가 → T1 서버측 사망(07-22 13:17:48Z) 후 74분 재사용. 재발급 그물이 401/403에만 배선(KIS는 500+EGW00123로
만료 반환)이라 자가회복 실패, margin(만료-10분) 도달에야 강제 재발급으로 소생. 실패 301건·EGW 904건. 수정 =
`4bf268f`(실만료 `access_token_token_expired` 클램프 + 상태코드 무관 토큰오류 재발급 + 하드만료 재발급). 검증
(07-23 당일 런): 13:31:20Z부터 76틱 무결손, 하드만료(14:34:56Z) 통과 시 14:35:50Z 재발급, EGW 0·실패 0.

_근거: SYSTEM kis_token.json mtime/expiry(14:34:56.36Z 정합), volume_shadow.jsonl 07-22 첫 레코드 14:34:56, intraday_open.log(UTF-16) EGW 히스토그램 실측_

**규칙: ① KIS 토큰 만료는 응답 실만료 문자열만 믿는다(expires_in은 alias에서 거짓). ② VM 수동 스모크는 태스크와 같은 계정 컨텍스트(state 경로)로 실행하거나, 실행 후 토큰 캐시 이원화를 의심한다. ③ PS `*>` 리다이렉트 로그는 UTF-16LE — 파싱 前 BOM 확인.**

### 2026-07-26 [hazard]

킬스위치 halted 는 배당 처리를 막지 않는다 — `process_dividends` 가 `_run_once_locked` 前(live_engine.py:217-229)
이라 halted 런에서도 `dividends_last.<ns>.txt` 마커가 전진하고, 배당 있으면 현금 입금(paper_book.json 변동)된다.
이는 **의도**(주석 명시: 실계좌 동일, 권리수량=ex-date 보유분)이고 HOUSE §5 로 명문화됨. (당초 '문서에 없음'
으로 적었다가 같은 날 정정 — 이 리포는 의도를 주석에 적으니 게이트 밖 실행은 호출부 주석부터 읽을 것.) A3
리허설 실측: 마커 07-23→07-24 전진, 그날 배당 0이라 무변동(무해).

_근거: 2026-07-26 A3 킬스위치 리허설 + 후속 조사_

**규칙: halted 런에서 북이 움직였으면 결함 판정 前 배당인지 먼저 확인한다. 게이트 밖 실행을 발견하면 사각으로 단정하기 前 호출부 주석을 읽는다.**

### 2026-07-27 [postmortem]

VM 인바운드 전멸(07-17~21): 아웃바운드 정상·게스트 방화벽 디스크 각인. 스냅샷 복원 2회 무효(시점이 사고 이후
+ Sysprep 없어 자가치유 미실행). 리부트+45분 무반응 → 클린 재건축(반나절, git bundle 이식으로 자격증명 회피).
부검 포기로 근본원인 영구 미확정. 막은 것은 기술이 아니라 비용(스냅샷 60GB×2 ≈ 월 6USD). **현재 VM
(<vm-host>) 스냅샷 0건**(구 스냅샷 2개 삭제) → 인스턴스 손실 시 복구는 재건축 유일(리포=GitHub, 시크릿=
VM env+PC 원본, 절차=lessons/2026-07-27-vm-inbound-loss-rebuild-beats-revive.md).

_근거: 2026-07-17~27 · wip/vm-selfheal-recovery.md · get-instance-snapshots length=0(07-27 14:23 UTC)_

**규칙: 인바운드 전멸 + 스냅샷 시점이 사고 이후면 소생 시도 금지 — 리부트 1회 후 30~45분 무반응이면 클린 재건축. VM 손실 복구는 재건축 전제(복원 경로 없음). 부검·백업 스냅샷은 보관비용과 종료기한을 함께 적는다.**

### 2026-07-27 [confirmed]

재건축 배포 체인(PC push→GitHub→autopull 10분→서명검증→run_tests→대시보드 재기동)이 2026-07-27 데스크탑 SSH 실측으로 무결 확인. VM HEAD=4533b82(PC 일치), autopull.last=fetch=ok head=4533b82, autopull.log에 ALL 11 SUITES PASS + dashboard restarted, autopull Ready/dashboard Running.

_근거: 2026-07-27 23:0x KST · ssh <vm-host> · logs/autopull.last 14:05:17 UTC_

**규칙: VM 배포 확인은 HEAD 일치 하나로 끝내지 말고 autopull.last의 head + autopull.log의 SUITES PASS 라인까지 볼 것. SSH는 System32 OpenSSH 명시, 원격 PS 인용부호는 -EncodedCommand로 회피.**

### 2026-07-31 [confirmed]

복붙 분기가 안전버그의 근본 원인 패턴 — 저널 회전 4벌 중 1벌 누락, 원자저장 3벌 중 1벌 재시도 누락, select 4벌 중 wood 만 가드 누락, 락 기계 2벌 중 백데이트 해킹. 같은 로직 N벌이 보이면 공유 헬퍼가 수리다

_근거: 2026-07-31 전면감사 종합_

**규칙: 동일 로직 3벌째 복붙 시점에 공유 함수로 추출한다**

### 2026-08-01 [hazard]

07-31 노트북 전면감사 커밋(aa2f619)이 미서명으로 push돼 VM 서명게이트가 pull을 무음 차단 — CRIT 4건 안전수정이 런타임 미반영인 채 스케줄·대시보드는 정상 가동이라 배포 완료로 오인 가능한 상태였음. autopull.log는 fetch 정상·FAIL 0이라 로그만으론 안 보임(마지막 pull 흔적 'dashboard restarted' 07-27 13:45 UTC가 유일 단서).

_근거: 2026-08-01 데스크탑 세션 실측 — vm_autopull.ps1:86 (verify-commit origin/main, 팁 1개만 검증)_

**규칙: 배포 커밋은 allowed_signers 등록 키가 있는 머신(현 데스크탑)에서 만들 것. 다른 머신에서 커밋했으면 서명 커밋 1개를 위에 얹어 push(팁만 검증되므로 역사 재작성 불요). push 후 VM HEAD 전진 확인까지가 배포 완료.**

### 2026-08-03 [rejected]

대시보드 개요에 토스 실계좌 표시하는 기능: 사용자 결정으로 보류. 대시보드 프로세스에 매매 자격증명을 넣지 않는다는 문서화된 격리 결정(broker/toss_quote.py:11-12, tools/setup_intraday.ps1:14)과 충돌 — 직접 조회안은 설계 반전이라 기각, 스냅샷 파일 경유안(헤드리스 태스크가 STATE_DIR/toss_account.json 기록, 대시보드는 파일만 읽기)이 재개 시 권장 경로. 표시 범위는 US 종목·USD 기준으로 합의됨(marketCountry 필터 유지).

_근거: 2026-08-03 사용자 선택(AskUserQuestion) + 정찰 보고_

**규칙: 대시보드(청취 프로세스)에 브로커 자격증명 주입 금지 — 실계좌 데이터가 필요하면 자격증명 가진 헤드리스 쪽이 파일로 밀어내고 대시보드는 읽기만 한다**

### 2026-08-09 [hazard]

배포 게이트(tools/run_tests.py)가 무인 VM에서 돌면 테스트 픽스처가 실제 텔레그램 알림을 쐈다 — 2026-08-08 12:35 UTC autopull 게이트가 존재하지 않는 페르소나 't' 로 '장중 루프 진입 정지: 일일손실 정지 / 킬스위치 상태파일 손상(JSONDecodeError)' 2건 발송. 토요일이라 장중 루프 자체가 안 도는 시각이었고 VM 킬스위치 5개 전부 halted=False·JSON 정상이었다. 스위트 출력을 autopull.log 에 append 하던 것도 200KB 트림을 밀어 pull/게이트 이력을 소각.

_근거: 2026-08-08 12:35:42 autopull.log '배포 테스트 통과' + tests_intraday.py:2149 IntradayTrader('t') 픽스처 + VM state 실측_

**규칙: 운영 알림 채널을 쓰는 코드는 테스트에서 반드시 차단할 것(notify.py USTRADE_NOTIFY_OFF, 러너가 세팅). 반대로 배포 알림(halt/에러)이 오면 먼저 발신 시각을 배포 시각과 대조 — 게이트 소음일 수 있다. 로그 파일 하나에 원장(pull/게이트)과 대량 출력(테스트)을 섞지 말 것: 트림이 원장을 먼저 지운다.**

### 2026-08-09 [hazard]

§B 권위 태스크가 PC(UsPaperLive)에 있어 06:10 웨이크에 의존했고 T0 이후 6세션 중 세션 2026-08-03 결번 + 08-04 5.5h 지연 + 08-06 17h 지연 = 결번률 약 17%. 한 세션을 놓치면 catch-up 이 그 세션을 복구하지 않는다(run_live 는 last_completed_session 만 처리) — 결번은 영구 손실이다. 같은 시각 VM ustrade-entry 는 --top-n 누락으로 top_n 3 으로 돌아 §B 정합 데이터가 어느 머신에도 없었다. 2026-08-09 VM 이전 집행(인자 정합·알림 언블랭킹·exit LASTEXITCODE·ustrade-watch deadman 신설·PC 태스크 Disable).

_근거: 2026-08-09 PC/VM schtasks + runs.jsonl 양쪽 실측, HOUSE.md §1 · docs/paper-trading-dod.md §B v2.2_

**규칙: 무인 스케줄은 상시가동 머신(VM)에만 얹는다 — PC 는 절전/종료로 결번이 구조적이고 놓친 세션은 복구 불가. 새 태스크 등록 시 (1) 인자가 실험 스펙과 글자 단위로 일치하는지 (2) 그 런을 감시하는 deadman 이 같은 머신에 있는지 (3) rc 가 python 종료코드를 그대로 반영하는지(exit \) 세 개를 반드시 확인.**

### 2026-08-14 [rejected]

PaperBroker.get_quote 가 NaN 시세를 그대로 통과시켜 get_account 의 equity 가 NaN → 킬스위치 bad_equity fail-closed 트립(자동해제 없음). 2026-08-12 06:18 wood 페르소나 일1런에서 발생, 08-13 재계산은 정상(=일시적 yfinance 미완성 행). 진입이 2일 정지됐고 사람이 --reset-halt 해야 복구.

_근거: VM C:\ustrade-paper-wood\logs\runs.jsonl 2026-08-12 status=tripped equity=NaN · broker/paper.py:54-59,65-79_

**규칙: 브로커 시세 진입점은 NaN/inf/<=0 을 '조회 실패'로 승격해 raise할 것 — 호출부 폴백이 이미 있는데 조용한 NaN 은 가드를 트립시켜 운영을 멈춘다. 데이터 결함을 상태 결함으로 오분류하지 말 것.**

### 2026-08-28 [hazard]

DESKTOP-88ESLUA(노트북)에 pre-T0 페르소나 태스크 ustrade-paper-{buffett,oneil,wood}(SYSTEM principal)가 T0 마이그레이션(2026-08-09 PC→VM) 때 안 꺼진 채 2026-07-24~08-28 VM과 병행 실행 — 로컬 북 22~26런 누적. 당시 UsPaperLive/UsPaperWatch(§B PC 태스크)만 Disable했고 페르소나 태스크는 누락. 결과: FMP 무료티어 이중소비 + VM 권위 페르소나 북과 분기(대시보드 오독 위험). §B(ustrade-data)는 이 머신 미실행이라 오염 0. 2026-08-28 3종 Disable + 북 C:\ustrade-paper-_archive_20260828 아카이브.

_근거: 2026-08-28 실측 DESKTOP-88ESLUA — C:\ustrade-paper-*\logs\runs.jsonl(oneil 마지막 08-28 06:26) + 승격 Get-ScheduledTask_

**규칙: 머신 마이그레이션 시 §B 태스크뿐 아니라 ustrade-paper-* 페르소나 태스크도 구머신에서 Disable 확인. 구머신 C:\ustrade-paper-*\logs\runs.jsonl mtime로 병행실행 잔재 주기 점검.**
