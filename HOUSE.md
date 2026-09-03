# HOUSE.md — 하우스 운용 규정

**트레이딩 작업을 시작할 때 가장 먼저 읽는 파일.** 유니버스·한도·킬스위치·스케줄을
코드에서 매번 다시 추론하지 않도록 한 번 컴파일해 둔 것이다.

컴파일 일자: **2026-08-09** · 대상 커밋 기준: 메인 트리 (worktrees 사본 제외)

> ⚠️ **이 문서는 코드의 사본이지 진실이 아니다.** 값이 충돌하면 `파일:라인` 쪽이 옳다.
> 아래 §9 갱신 규약을 지킬 것. stale 한 HOUSE.md 는 없느니만 못하다.

---

## 1. 현재 운용 단계 — L0 Paper 무인

**T0 = 2026-08-09 재설정 (§B v2.2).** 실행이 **PC → VM** 으로 이전됐다.
첫 fresh 런 = **2026-08-09 06:10 UTC · VM `ustrade-entry`** (거래대상 세션 2026-08-07),
fresh $100k. 12주 창 = 2026-08-09 ~ **2026-11-01**.

> **왜 옮겼나** — §B 권위였던 PC 태스크 `UsPaperLive` 는 06:10 에 기계가 깨어 있어야 했고,
> 실측 결번률이 **~17%**(6세션 중 세션 08-03 결번 + 2회 5.5h·17h 지연)로 게이트 A(<5%)를
> 위반했다. VM 은 PC 전원과 무관하게 도는 용도로 세운 머신이다(사용자 결정 2026-08-09).
> 동시에 VM `ustrade-entry` 는 `--top-n` 누락으로 **top_n 3** 으로 돌고 있었다 — 즉 그 시점까지
> §B 정합 데이터는 어느 머신에도 없었다. PC 5런은 폐기(최소표본 45런 대비 ~1주분),
> 구 상태는 `C:\ustrade-data\_pre_t0_20260809\` 로 보존. 상세 = `docs/paper-trading-dod.md` §B v2.2.

**구 이력**: T0 = 2026-08-01(A1~A11 충족일), 첫 fresh 런 08-03 06:10 `UsPaperLive`(PC).
A4 완료 2026-08-01 12:45~13:05 PASS. PC 북(`%LOCALAPPDATA%\ustrade`)은 동결 상태로 남아 있다.

**A4 완료 (2026-08-01 12:45~13:05 PASS)** — 07-23 사용자 결정대로 대낮 통제 RTC 웨이크
테스트(데스크탑 DESKTOP-TSEG3ND, AC) 1회 성공으로 종결, A1~A11 전원 충족
(`docs/paper-trading-dod.md:67-72`).

**T0 개시 절차 실행 완료 (2026-08-01 13:25:25, `ClaudeT0Reset` 경유)**: paper 상태 3파일 삭제 →
fresh $100k (`killswitch.paper.json` · `paper_book.json` · `dividends_last.paper.txt`,
`docs/paper-trading-dod.md:114-120`). 실행 상세·백업 위치는 `docs/paper-trading-dod.md:122-131`.

**실거래 봇은 정지 상태 유지** (`tools/setup_paper_tasks.ps1:4,121`). 장중 루프는 paper 전용 —
호가만 TossQuoteClient 이고 주문 메서드가 없어 **실주문 0** (`tools/setup_intraday.ps1:13`).

### §B 실험 — v2.2 (2026-08-09 재등록, 실행 머신 이전) · 동결됨

paper 계좌 · **canslim + sp100 · top_n 5 등비중** · 매일 06:10 무인 1런 · **개입 없음**.
선정 로직 튜닝은 개입이며 실험 리셋 사유다.
**실행 권위 = VM `ustrade-entry` 인자** `run_live.py --strategy canslim --universe sp100 --top-n 5`
(`USTRADE_HOME=C:\ustrade-data`, 06:10 **UTC**, SYSTEM). PC `UsPaperLive` 는 **Disabled**.
✅ **v2.2 집행 완료 (2026-08-09 데스크탑 SSH)** — VM 인자에 `--top-n 5` 추가 · 알림 env 블랭킹
제거(§B 알림이 VM 에서 나감) · `exit $LASTEXITCODE` 추가(종전 rc 항상 1) · paper 상태 4파일을
`C:\ustrade-data\_pre_t0_20260809\` 로 이관해 fresh $100k · **`ustrade-watch` 신설**(매시 deadman —
종전 VM 일1런은 결번 감시가 없었다) · PC 태스크 2종 Disable. 첫 런 검증 = `runs.jsonl` 의
`selection.final` 길이 **5** + 시작자산 $100,000. 상세는 `docs/paper-trading-dod.md` §B-1.
판정 권위 = `docs/paper-trading-dod.md` §B — 아래는 요약이다. 충돌 시 dod.md 가 옳다.

**v1(07-11) → v2(08-01) → v2.1(08-01) → v2.2(08-09).** v2.1 까지는 첫 런 前이라 §B 창 관측 0회.
**v2.2 는 PC 5런을 폐기하고 T0 를 2026-08-09 로 재설정** — 판정 규칙·전략 파라미터는 전부 불변,
바뀐 것은 실행 머신과 시계뿐이다(사유 = PC 결번률 17% · VM top_n 3 불일치, dod.md §B v2.2).
- **v2 사유** = 검정력 결함: top_n=3 의 12주 초과수익 σ=7.5%p(실측) 앞에서 v1 판정선 "초과 ≥0"은
  동전던지기라 **알파 0 전략이 60% 확률로 "계속"** 판정을 받았다 → 판정규칙 재설계.
- **v2.1 사유** = 사용자 결정으로 **`top_n` 3→5 채택**(v2 가 미채택 옵션 C 로 병기했던 안).
  검정력을 올리는 유일한 가용 레버이고 첫 런 前이 마지막 기회였다. σ 7.5→5.8%p · TE 15.6→12.1%p ·
  t ×1.29(상한 — `vol_target` 오버레이 감안 실현 1.16~1.29). **바뀐 것은 `top_n` 하나뿐**:
  유니버스·엔진·pool(12)·vol_target·정수주·스케줄 전부 불변. 판정 구조도 v2 그대로 승계.
v1 원문은 dod.md 에 이력 보존 — 삭제 금지.

⚠️ **`--top-n` 은 v2.1 용 신설 CLI 인자** — 엔진 기본값은 3 그대로(`live_engine.py:39`), §B 만
5 로 돈다. 명시된 CLI 값이 대시보드 `control_settings.json` 보다 **우선**한다
(`run_live.py:189-193`) — 대시보드 "보유 종목수" 편집이 §B 를 조용히 뒤집던 경로를 막은 것.
대시보드 UI 는 여전히 기본값 3 을 표시한다(표시상 불일치, 실동작 5).

- 최소 표본: T0 + **12주 AND 창 내 ok-런 ≥45** (v1 "리밸런스 ≥8회"는 `reselect_days=0`
  때문에 8거래일에 충족돼 무의미했으므로 폐기, `live_engine.py:56`)
- **성과 판정과 운영 게이트를 분리한다.**
  - **게이트 A(운영·차단)**: 결번률 <5% ∧ 2주 연속 주2회+ 결번 없음 ∧ 킬스위치 오트립 0 ∧
    error 런 <2% ∧ 저널 무결 — 검정력과 무관하게 유효, 위반 시 성과 불문 중단
  - **게이트 B(위험·차단)**: MDD ≤ **20%** (킬스위치 `max_total_drawdown` 정합). 성과 비교가
    아니라 위험 한도다
  - **성과(3단)**: 일별 초과수익 d 의 t-통계로 **`t ≥ +1.30` 계속 / `t ≤ −1.30` 중단 /
    그 사이 = 판정 불가(표본 부족) → +4주씩 최대 2회 연장(12→16→20주)**.
    **20주에도 판정 불가면 게이트 A·B 통과 전제로 "조건부 계속 = L1 진입"** — 성과 유의성은
    원리적으로 도달 불가(IR 0.5 를 80% 검정력으로 잡는 데 ~25년)이므로 그것을 진입 조건에
    두면 사다리가 영구히 멈춘다. 위험 통제는 유의성이 아니라 자본 상한이 한다
    (L1 진입은 준비 단·자본 0, 실 상한은 L2 $200)
- 오판율(알파 0): **"계속" 60% → 16%**, 좋은 전략 오살 26% → 10% (dod.md §B-7).
  **top_n 3→5 는 이 수치를 바꾸지 않는다** — t 가 실현 σ 로 자기정규화되므로 v2 판정선의 오판율은
  σ 에 불변이고, 바뀌는 것은 같은 확률로 잡히는 **알파 수준**이다(IR ±0.5 = 연 ±7.8%p → **±6.0%p**)
- ⚠️ **§B 는 알파를 검정하지 못한다** — 어떤 판정도 "엣지 있음/없음"의 근거로 인용 금지.
  top_n=5 로도 그렇다(연 +8%p 알파 80% 검정력 소요 23.5년 → 14.1년)
- **이 v2.1 등록 이후 §B 수정 = 실험 무효 + 재등록** (`docs/paper-trading-dod.md` §B-9)
- §B 집계 명령: `python tools/paper_nav.py --since 2026-07-31`(첫 fresh 런(08-03)이 처리하는
  세션 = 07-31; pre-T0 레코드는 session ≤07-30 이라 미포함. `--since` 는 런 레코드의
  `session`(거래 세션 날짜) 기준 — `ts` 실행시각이 아니다. `docs/paper-trading-dod.md:132-134`).
  t-통계 산출은 paper_nav 확장 필요 — 스펙은 dod.md §B 부록, 확장 전 중간점검은 누적 초과
  고정밴드 ±1.30σ. **v2.1(top5) σ=5.8%p 기준: 12주 ±7.6%p · 16주 ±8.7%p · 20주 ±9.8%p**

신규 아이디어는 `docs/queue-post-freeze.md` 에 큐잉만 한다.

---

## 2. 유니버스

바스켓 6종이 `universe.py:9-108` 에 정적 정의. 지정 방식은 바스켓명 | CSV/TXT 경로 |
콤마구분 문자열 (`universe.py:111-120`).

| 바스켓 | 종목수 | 쓰는 곳 |
|---|---|---|
| `megacap` | 15 | — |
| `tech` | 15 | — |
| `sp100` | ~101 | **§B 실험(UsPaperLive)** |
| `diversified` | 28 | `RunConfig` 기본값 (`live_engine.py:36`) |
| `sp500` | ~500 | buffett, oneil 페르소나 |
| `growth` | ~45 | wood 페르소나 |

장중 전용 페르소나(livermore/chartist/*_ctl)는 유니버스가 아니라 **고정 watchlist 16종**
(`personas.py:129-152`). 대조군 `_ADV16` = sp500 20거래일 평균 달러거래대금 상위 16, 기계적 산출.

### 생존편향 — 코드가 스스로 경고한다

`universe.py:3-5, 20-21, 47-48` 에 명시: 상장폐지 종목 누락, sp100 = "오늘의 승자 →
미래 셀렉션 편향", sp500 은 수시 변경되어 주기적 수동 갱신 필요.
**백테스트 결과를 해석할 때 이 편향을 반드시 언급할 것.**

### 전수 생존 점검 이력

정적 목록이므로 티커 개명·폐지가 조용히 누적된다. 증상은 로그의
`possibly delisted; no price data` — 거래는 안 죽지만(스킵 처리) 그 종목이 후보에서 사라진다.

| 일자 | 대상 | 결과 |
|---|---|---|
| 2026-07-26 | sp100 (101종목) | DEAD 1 / STALE 0 / ALIVE 100. **`BK`→`BNY` 교체** — 상장폐지가 아니라 티커 개명(2026-05-21, 상장·CUSIP 불변)인데 2개월간 방치돼 후보 이탈 상태였음. 게이트 ALL 11 SUITES PASS 확인. sp500·growth·diversified 는 **미점검** |

> ⚠️ **sp100 은 §B 실험 유니버스다 — T0 이후 변경 = 실험 무효 + 재등록**(§1). 위 교체는
> T0 前(pre-T0)이라 무해. 다음 점검은 T0 개시 前에 몰아서 하거나, T0 後면 §B 판정까지 보류할 것.

### 데이터 규율

- yfinance 일봉 + CSV 캐시, `auto_adjust=True`, 3회 재시도 (`data.py:76-86`)
- **패널 실패율 20% 초과 → raise** (`data.py:110-127`) — 피드 이상으로 유니버스가
  조용히 축소되는 것을 차단
- **결측 = 제외가 아니라 플래그** (`live_select.py:91,97`). 결측 30% 초과 시
  `screen_degraded` 경보
- 종목별 stale 컬럼 제외, 전 종목 stale 이면 거래 보류 (`live_engine.py:311-319`)

---

## 3. 리스크 한도 — `GuardConfig` (`broker/guardrail.py:65-73`)

무인 거래의 최종 방어선. **이 값들은 risk 데스크만 변경을 제안할 수 있고, 변경은 사람이 승인한다.**

| 항목 | 값 | 라인 |
|---|---|---|
| 일일손실 한도 | **5%** (당일 baseline 대비) | `:66` |
| 누적 드로다운 한도 | **20%** (HWM 대비) | `:67` |
| 단일종목 최대 비중 | **40%** | `:68` |
| 총노출 상한 `max_gross` | **105%** | `:69` |
| 연속 에러 정지 | 롤링 6회 중 **3회** | `:70-71` |
| 단일주문 명목 절대상한 | **$1,000,000** | `:72` |
| 주문 명목 버퍼 | 1.5 (=40%×자산×1.5) | `:73` |

- **레버리지 없음** — `apply_overlay(max_leverage=1.0)`, 디레버리지만 (`live_risk.py:5,48-49,96`)
- **현금 하한 5%** — `alloc=0.95` (`live_engine.py:60`). 소수주 모드는 `fee_reserve=0.005` 추가 haircut
- **현금 상한 없음** — 포트폴리오 레벨 현금 상한 규정이 코드에 존재하지 않는다.
  유일한 총투입 캡은 장중 페르소나용 `max_deploy` (기본 1.0=비활성,
  livermore_swing 만 0.70) (`intraday_guard.py:27-29`)
- **레짐 필터**: SPY < 200MA → 목표비중 전부 0 = 전량 현금 (`live_risk.py:52-78`)
- **변동성 타겟**: 기본 0.20 (`live_engine.py:58`) / buffett 0.12 · wood 0.30 · oneil 0.20
  (`personas.py:86,96,113`) / 백테스트 기본 0.15

### ⚠️ "포트폴리오 heat" 는 이 하우스에 없다

단일 heat 지표가 코드에 존재하지 않는다. 기능적 대응물이 셋으로 분산돼 있다:
`max_gross`(105%) · `vol_target` 스케일 · `max_total_drawdown`(20%).
**heat 라는 단어로 소통하지 말 것** — 어느 것을 말하는지 불명확해진다.

**섹터·산업별 노출 한도도 없다.** 분산 강제는 단일종목 40% 캡뿐이다.
같은 테마 중복 베팅은 코드가 막아주지 않으므로 risk 데스크가 사람 눈으로 봐야 한다.

### 손절 — 경로마다 다르다

| 경로 | 규칙 |
|---|---|
| 일1런 (`run_live`) | 종목 손절 **없음**. 청산은 별도 경로 |
| `run_exit.py` (**토스 실거래 전용**) | 200MA 이탈 → 전량청산 · 평단 대비 **−8%** · opt-in 50MA·RSI. 하드 손절은 일봉 결측과 무관하게 항상 평가 (`live_exit.py:37-85`) |
| 백테스트 | `--stop_loss` 기본 0.15 |
| 장중 paper | oneil 7%/익절 20% · wood 5% · livermore 3% · livermore_swing hw트레일 8% · chartist 레벨손절 + R:R 2.0 |

앵커 구분은 risk 데스크 메모리 2026-07-07 항목 참조 (진입 제안선 vs 보유 추적선).

---

## 4. 포지션 사이징 — 3단계

1. **등비중** `w = 1/len(final)` — 선정 엔진 4종 공통
2. **변동성 스케일** (`live_risk.py:80-100`)
   ```
   realized = std(port_ret) × √252        # 최근 20봉 가중포트 수익률
   scale    = clip(vol_target / realized, 0.0, 1.0)
   final_w  = w × scale
   ```
   완전행 2개 미만이면 **fail-closed**(거래 보류) — scale=1.0 폴백 금지
3. **주문 변환** (`broker/executor.py`)
   - 정수주: `investable = equity × 0.95`, `tgt_qty = int(w × investable / price)`
   - 비용배수 `buy_mult = (1+spread/2)(1+slippage)(1+commission)(1+cost_buffer)`
   - 소수주: `min_order_usd = 5.0` 무거래 밴드, 센트 내림
   - **매도 먼저 → 실현현금 확인 → 매수 재사이징** (EXEC-2, `live_engine.py:390-402`)
   - 비중 큰 주문부터 예산 배정

**구조적 모순은 실행 전에 차단**: `1/top_n > max_position_weight` 면 error 반환
(`live_engine.py:289-293`). 과소선택 시 캡으로 클램프.

---

## 5. 킬스위치

상태 스키마 권위 = `KillSwitch._default_state()` (`broker/guardrail.py:181-185`).
경로 권위 = `paths.py:16-27` — `STATE_DIR = $USTRADE_HOME/state`, 미설정 시
`%LOCALAPPDATA%\ustrade\state\`. paper 페르소나는 `C:\ustrade-paper-<persona>\state\`
(`tools/setup_paper_tasks.ps1:91`). **세션에서 직접 읽지 말 것**(MSIX 오버레이).

> ⚠️ **프로젝트 폴더의 `state/`·`logs/`·`data_cache/`·`fmp_cache/`·`results/` 는 런타임이 아니다.**
> `.gitignore:2-6` 으로 추적 제외된 로컬 잔재이며, `USTRADE_HOME` 을 프로젝트 루트로
> 지정하는 스크립트는 없다 — 즉 어떤 코드도 이 경로를 읽지 않는다.
> 구스키마 `state/killswitch.json`(2025-01-02, halt_kind·hwm·recent 없음)은 **2026-07-20 삭제**
> (2026-06-24 dual-team 리뷰 권고 `archive/reviews/20260624_113942_dualteam/BACKLOG.md:289`).

**namespace 격리**: `killswitch.<ns>.json`, ns = broker종류 또는 `paper_<persona>`.
paper $100k 와 toss $100 이 baseline 을 공유해 −99.97% 오트립 나던 것을 차단한 조치.

| halt_kind | 조건 | 자동해제 | 청산 허용 |
|---|---|---|---|
| (수동) HALT 파일 | `STATE_DIR/HALT` 존재 | ✗ | ✗ |
| `daily_loss` | 당일 −5% | **새 거래일** | ✓ |
| `total_drawdown` | HWM −20% | ✗ (수동) | ✓ + 잔여 롱 보호 전량청산 |
| `error` | 최근 6회 중 3회 | ✗ | ✓ |
| `bad_equity` | 자산 NaN/inf/≤0 | ✗ | ✗ |
| `bad_baseline` | baseline ≤0 | ✗ | ✗ |
| `position_bound` | 비중>40% ∨ 총노출>105% ∨ NaN | ✗ | ✗ |
| `order_notional` | 주문 > min($1M, 40%×자산×1.5) | ✗ | ✗ |

**리셋은 사람만 한다.** 리허설 후에는 즉시 `--reset-halt`(already_ran 때문에 트립 상태
방치 금지).

### 정지 영속화 — fail-closed (수리 2026-07-31)

`trip()` 의 상태파일 저장(`_save`)이 실패해도 정지가 무음으로 유실되지 않는다. `_save`
실패 시 사이드카 마커 `killswitch.<ns>.halt` 를 기록하고 예외를 올려 이번 런을 error 로
종결시킨다(`broker/guardrail.py:306-327`, 마커 경로 정의 `:213-220`). 다음 런의 `_load`
는 이 마커 존재만으로 `halted=True` 를 승계한다 — 상태파일이 옛 값(=거래 허용)이어도
정지가 이어진다(`:222-236`). `_save` 가 이후 한 번이라도 성공하면(=상태파일이 진실을
담으면) 마커를 자동 삭제하므로 자동해제·reset 경로에 별도 처리가 필요 없다(`:253-278`,
마커 unlink `:274-277`).

### 스케일급변 재seed — HWM 교차확인 (수리 2026-07-31)

`roll_day`/`check_daily_loss` 는 직전 실행 대비 자산이 >5배 뛰거나 <1/5 로 급락하면
(브로커 오독·cash_cap 변경 의심) baseline 을 현재 자산으로 재seed 한다. 종전엔 무조건
재seed — 손실이 클수록(예: −85%) baseline·HWM 양쪽에서 지워져 `daily_loss`·
`total_drawdown` 를 모두 통과시키는 비단조 구멍이었다(−20% 는 트립, −85% 는 무트립).
수리 후: down-jump(≥80% 급락) 재seed 는 `_breaches_drawdown()`(HWM 대비 누적DD 한도
초과 여부, `broker/guardrail.py:349-356`)로 교차확인 — 한도를 이미 넘는 급락이면
재seed 하지 않고 `total_drawdown` 트립을 그대로 통과시킨다(`roll_day` 재seed 분기
`:370-391`, `check_daily_loss` 동일 판별 `:438-450`).
**부작용**: 정당한 80%+ 출금이나 `cash_cap` 대폭 축소도 이 조건에 걸리면 트립한다 —
사람이 `--reset-halt` 해야 복구된다.

### 정지가 막지 **않는** 것 — 배당 (명문화 2026-07-26)

**배당 입금은 킬스위치 게이트 앞에서 돈다.** `run_once` 가 RunLock 을 잡은 직후,
`_run_once_locked`(= `is_halted`/`already_ran` 판정) **前**에 `process_dividends` 를 호출한다
(`live_engine.py:217-229`). 설계 의도이며 코드 주석에 명시돼 있다 — **배당은 거래가 아니라
회계 이벤트이고, 실계좌에서도 정지 여부와 무관하게 입금된다.** 권리 수량 = 리밸런스 前 보유분
= ex-date 보유분이라 이 위치여야 정확하다.

따라서 halted 런에서도 이런 일이 일어난다:
- `dividends_last.<ns>.txt` 마커가 전진한다
- 해당 창에 배당이 있었으면 **현금이 실제로 입금된다** (`paper_book.json` 변동)

`halted` 상태에서 북이 움직였다고 곧바로 결함으로 판정하지 말 것 — 배당인지 먼저 보라.
스코프는 `run_live` 의 기본 paper 북뿐(페르소나·toss 는 marker 미전달, `dividends.py` 설계 계약).
실측: 2026-07-26 A3 리허설의 halted 런에서 마커 07-23→07-24 전진, 그날 배당 이벤트 0이라
북은 무변동(`docs/paper-trading-dod.md:56-58`).

### 거래 보류·중단 게이트 (`run_once` status)

`stale`(3세션 초과) · `locked`(RunLock O_EXCL, 좀비락 회수) · `already_ran`(당일 1회 멱등) ·
`skip`(선택 공집합 → **포지션 유지**, 전량 현금화 안 함) · `hold`(reselect_days 비도래일)

- 상태파일 손상/비-dict → **fail-closed halt**
- SPY 1세션 초과 stale → ValueError, 거래 보류
- **실거래 명시확인 게이트**: `--confirm-live` 또는 `USTRADE_LIVE_CONFIRM=1` 없으면 거부
- **알림채널 필수**: 실거래인데 TELEGRAM/SLACK 미설정이면 거래 거부
- **토스 슬리브 필수**: `toss_sleeve.json` 없으면 거부
- `GuardedBroker` — 모든 place_order 가 킬스위치를 통과해야 하며 우회 불가
- 종료코드: benign(ok/already_ran/locked/skip/hold)=0, soft(stale/partial)=1, 나머지=2

### 장중 루프 프로세스 락 (수리 2026-07-31)

`run_intraday.py` 의 페르소나별 프로세스 락이 계정 스코프 `cache_base()`(=`USTRADE_HOME`,
미설정 시 `%LOCALAPPDATA%\ustrade`) + `--only` 문자열 조합 키에서 **책 옆 경로**
`persona_home/state/intraday.lock` 로 바뀌었다(`persona_lock_path()`,
`run_intraday.py:878-890`). 종전 방식은 SYSTEM 태스크와 유저 셸이 서로 다른 락 파일을
보고, `--only` 조합만 달라도 다른 락을 잡아 **같은 책**을 두 프로세스가 last-writer-wins
로 덮어쓸 수 있었다. 락 기계 자체도 자체 O_EXCL + mtime 백데이트 대신 `run_live`(일1런)
와 **동일한 `RunLock` 구현**(`broker/guardrail.py`)을 쓰도록 통일했다 — 두 서브시스템이
서로 다른 steal/heartbeat 규칙을 적용하던 것이 락 버그의 근원이었다(`run_intraday.py:28-30`,
획득 지점 `:893-907`). 일1런의 `STATE_DIR/run.lock`(본 절 상단)과는 별개 파일 — 보호
대상이 다르다(일1런 리밸런스 vs 장중 루프 인스턴스).

**수리 2 (2026-08-01)**: 위 통일이 죽은 pid 락 회수를 즉시→30분(`(dead AND age>1800) OR
age>21600`)으로 후퇴시켜, `run_live` 가 락 안에서 도는 일1런 전체(`live_engine.py:230-236`)가
크래시하면 장중 보호청산(`_on_bar`)이 최대 30분 스킵될 수 있었다(Med). `RunLock` 에
`steal_dead_after` 생성자 인자를 추가(기본 1800=기존 동작 유지, `broker/guardrail.py:91-119`)하고
intraday 페르소나·책 락 획득부(`run_intraday.py:306,427,485,904`, 관찰전용 volume_shadow 락
`:960`도 통일)는 `steal_dead_after=120`으로 죽은 락을 2분 내 회수 — Windows 는 보유자가 fd 를
연 채면 rename steal 이 sharing violation 으로 실패해 *살아있는* 락은 값과 무관히 오탈취 불가하므로
짧은 값의 위험이 구조적으로 차단된다.

---

## 6. 스케줄

**일1런 진입**: `run_live.py`, 거래 대상 = ET 기준 직전 종료 NYSE 세션.
README 표준 **평일 06:10** (`README.md:320-323`).
⚠️ 머신 env 변경 후 **재부팅 또는 Schedule 서비스 재시작 필수**.

| 태스크 | 내용 | 출처 |
|---|---|---|
| **`ustrade-entry`** (VM) | **매일 06:10 UTC, `--strategy canslim --universe sp100 --top-n 5` = §B v2.2 권위** | `docs/paper-trading-dod.md` §B-1 |
| **`ustrade-watch`** (VM) | **매시 결번 감지(deadman), `USTRADE_HOME=C:\ustrade-data`** — 2026-08-09 신설 | 같은 절 |
| ~~`UsPaperLive`~~ (PC) | **Disabled 2026-08-09** — 06:10 웨이크 실패로 결번률 17%. 북은 동결 보존 | 아래 §1 |
| ~~`UsPaperWatch`~~ (PC) | **Disabled 2026-08-09** — 동결된 PC 북을 감시하면 오경보만 남는다 | 아래 §1 |
| `ustrade-paper-{buffett,wood,oneil,buffett_v2,canslim_rdcf}` | 0/8/16/24/32분 스태거(FMP 버스트 회피). **canslim_rdcf 는 2026-08-14 신설**(oneil A/B, 일1런 전용·장중 없음) | `setup_paper_tasks.ps1:51-56,101` |
| `ustrade-intraday` | entry + ~10분 지연 | `tools/setup_intraday.ps1:69-75` |
| `ustrade-intraday-open` | **13:30 UTC = 09:30 EDT 개장**(ORB 앵커) | `:77-85` |

> ⚠️ **실측(2026-08-09, 운용 VM `Get-ScheduledTask`)**: VM(<vm-host>)에 `ustrade-*` **10종** —
> autopull·dashboard·entry·**watch(신설)**·intraday·intraday-open·paper-{buffett,buffett_v2,oneil,wood}.
> `ustrade-exit` 는 태스크 자체 **부재**(라이브 미사용과 일치). `UsPaper*` 는 VM 매칭 **0**(PC 전용,
> 이제 둘 다 Disabled). 표의 태스크명은 머신별 배치가 다름을 전제로 읽을 것.
>
> ⚠️ VM 페르소나 태스크(`ustrade-paper-*`)는 인자에서 `TELEGRAM_*`/`SLACK_*` 를 `''` 로 덮어
> **알림을 내지 않는다** — 의도된 침묵(9종 × 알림 홍수 차단). `ustrade-entry`(§B)만 2026-08-09
> 부터 알림을 낸다. 페르소나 이상은 알림이 아니라 대시보드·저널로 봐야 한다.
>
> ⚠️ **실측(2026-08-28, DESKTOP-88ESLUA=노트북)**: 이 노트북에 pre-T0 페르소나 태스크
> `ustrade-paper-{buffett,oneil,wood}`(SYSTEM)가 T0 마이그레이션 때 **안 꺼진 채** 07-24~08-28 VM 과
> **병행 실행**(로컬 북 22~26런 누적). 08-09 엔 `UsPaperLive`/`UsPaperWatch` 만 Disable 했고 페르소나
> 태스크는 누락한 것 — FMP 이중소비·VM 권위 북과 분기의 원인. **2026-08-28 3종 Disable + 북
> `C:\ustrade-paper-_archive_20260828` 아카이브.** §B(`ustrade-data`)는 이 머신 미실행이라 오염 0.
> ⚠️ 비승격 `Get-ScheduledTask` 는 SYSTEM 태스크를 **안 보여준다** — 승격 창에서 확인
> (`lessons/2026-08-28-system-scheduled-tasks-hidden-from-nonelevated-enum.md`).

**deadman 판정식**: 직전 종료세션 마감 후 첫 평일 06:10 + **유예 50분** 경과 & 기록 없음 →
경보. 주말·공휴일 무경보, 세션당 1회 dedup. 감시 대상 홈 = 그 태스크의 `USTRADE_HOME`
(VM `ustrade-watch` → `C:\ustrade-data` = §B 북).

**전원 전략**: VM 은 상시가동이므로 §B·페르소나는 PC 전원과 **무관**하다(2026-08-09 이전 완료).
PC 잔여 태스크에만 해당: WakeToRun + **S3 절전**. 완전 종료·빠른시작 종료는 RTC 웨이크 불발이고
RTC 웨이크는 AC 연결 시에만 허용된다 — **PC 에 무인 스케줄을 새로 얹지 말 것**(결번률 17% 전례).

**토스 실거래 스케줄(확정, 미집행)**: 진입 KST 23:35 평일 1회 (`--cash-cap 500`),
청산 `*/15 22-23,0-6` KST (DST 양 체제 커버).

**주기 제어**: `reselect_days` — buffett 만 7(주1회, 일일 churn 연 4~7% 누수 절감),
나머지 0(매일).

---

## 7. 전략 엔진

### 일1런 선정 엔진 5종 (`live_engine.py:16-31, 331-345`)

| 엔진 | 정의 |
|---|---|
| `momentum` | 모멘텀(126일, skip 21) 상위 pool 8 → FMP 하드스크린(적자·고PE 제거) → top_n 등비중 |
| `canslim` | 가격 하드게이트(close>200MA ∧ 52주 근접도≥0.85 ∧ 12-1 모멘텀>0) → CANSLIM(EPS YoY≥25% ∧ 매출≥10%) + 애널 점수 틸트. **§B 실험 엔진** |
| `buffett` | 저변동(252일) 상위 pool → FMP(max_pe 25, 순이익률≥8%) → quality_value_score 상위 |
| `buffett_v2` | 저변동(252일) 상위 pool → 적자·PE>60 만 컷(그 외 하드컷 없음) → `quality_value_score_v2`: 가치 FCF·이익수익률 z + 품질(ROIC 1.0·ROE 0.5·마진 0.5) − 구 하드컷(PE25·마진8%)의 최대 1z 연속 페널티, 섹터 틸트는 표본수축 부분풀링(min_n=4)으로 제거. **buffett A/B 실험군(12주, 아래)** |
| `wood` | 단기 모멘텀(63일, skip 10) → `z(P/S) − z(배당수익률) + z(모멘텀)`. **가치 스크린 없음**(적자 혁신주 허용) |
| `canslim_rdcf` | canslim 과 동일 가격스크린·펀더점수 위에 **역DCF 소프트 틸트**(완벽하게 가격된 종목 감점). **canslim A/B 실험군**(아래) |

canslim·canslim_rdcf 는 형제 디렉토리 `Projects/텔레그램_시그널_알리미` 에 의존하며 부재 시 명시적 error.

### buffett A/B 실험군 — `buffett_v2` (2026-08-01 등록)

`buffett_v2`(`live_select_buffett_v2.py`)는 `buffett` 의 A/B 대조 실험군이다. 다이얼은 buffett 과
동일(top_n·pool·lookback·vol_target·reselect_days·universe·cash) — 다른 것은 선정 로직뿐(위
엔진표). 페르소나 등록 = `personas.py:104`, 태스크 = `ustrade-paper-buffett_v2`.

- **기간**: 12주 병행, **2026-08-04 ~ 10-27**.
- **판정**: NAV·MDD·회전율·선정품질 4지표 중 **3지표 이상 우세** 쪽을 승자로 채택하고 패자는
  삭제(A/B 종료). 동률이면 `buffett`(v1) 존치.
- ⚠️ **§B 실험과 무관** — 판정 축·표본·시계가 다르다. 이 A/B 의 페르소나 성과를 §B(top_n 5
  canslim 실험)의 증거로 인용 금지.

### canslim A/B 실험군 — `canslim_rdcf` (2026-08-14 등록)

`canslim_rdcf`(`live_select_canslim_rdcf.py`)는 `oneil`(canslim)의 A/B 대조 실험군이다. 다이얼은
oneil 과 동일(top_n 5·pool 25·vol_target 0.20·regime_ma 200·fractional·value_trap_gate·universe
sp500·cash) — 다른 것은 **역DCF 밸류 오버레이 하나뿐**. 페르소나 = `personas.py`, 태스크(예정) =
`ustrade-paper-canslim_rdcf`. 단 **장중 루프는 안 붙인다**(oneil 과 달리 `intraday` 미설정) —
밸류틸트 효과를 일1런 선정에서만 계측.

- **틸트**: `gap = 내재성장(역DCF) − 실제성장(rev_g)`. gap>+10%p 부터 감점, +30%p 에 최대 1점
  (`live_select_canslim_rdcf.GAP_FLOOR/GAP_SCALE/PENALTY_CAP`). 배제 아닌 순위 하향. FMP 결측·적자
  FCF 는 무감점 폴백(canslim 순위 보존). 데이터: rev_g=canslim_tag(추가비용 0), market_cap·FCF=
  `fmp.key_metrics_ttm` 1콜/종목(pool 12~25, 캐시 상각).
- **기간·판정**: 12주 병행, 2026-08-14 개시. 판정은 buffett A/B 와 동일 틀 — NAV·MDD·회전율·선정품질
  4지표 중 **3+ 우세** 쪽 채택, 동률이면 `oneil`(v1) 존치.
- ⚠️ **§B 실험과 무관** — §B(top_n 5 canslim sp100 일1런)는 손대지 않았다. 이 A/B 는 sp500 페르소나
  트랙이고 판정 축·표본·시계가 §B 와 다르다. 이 A/B 성과를 §B 증거로 인용 금지.
- ⚠️ **판정 전까지 `canslim_rdcf` 와 `oneil` dict 를 함께 바꾸지 말 것**(한쪽만 바뀌면 12주 무효).

### ⚠️ §B 실험 ≠ `oneil` 페르소나 — 같은 canslim 엔진, 다른 실험

둘 다 canslim 이지만 설정이 갈린다. **성과를 섞어 읽지 말 것** — oneil 결과는 §B 증거가 아니다.

| | **§B 실험** (06:10 일1런) | **`oneil` 페르소나** |
|---|---|---|
| 진입 | `run_live.py --universe sp100 --top-n 5`, 비-persona 분기 (`run_live.py:370-383`) | `--persona oneil` 분기 (`:362-369`) |
| 유니버스 | **sp100** | **sp500** (`personas.py:111`) |
| `top_n` | **5** — CLI `--top-n` 로 주입(v2.1). 엔진 기본값은 3 그대로(`live_engine.py:39`) | **5** (`personas.py:113`, persona override) |
| `pool` | 8 → **12** 자동보정 (`run_live.py:151-152`) | **25** (`personas.py:113`) |
| `value_trap_gate` | **off** (기본 False, `live_engine.py:54`) | **on** (`personas.py:114`) |
| 체결 단위 | 정수주 (`--fractional` 미지정, `live_engine.py:62`) | **소수주** (`personas.py:113`) |
| 장중 루프 | **없음** | 병행 (`intraday`·`daily_run` True, `personas.py:115-116`) |
| 상태 격리 | `paper` ns | `paper_oneil` ns, 별 home |

### 장중 액티브 룰 5종 (paper 전용)

oneil(피벗 돌파) · wood(MA 회복) · livermore(오프닝레인지 돌파+피라미딩) ·
livermore_swing(20세션 고점 돌파, **오버나이트 보유**) · chartist(SR Flip)

**피라미딩 사이징** — livermore · livermore_swing · wood 3종이 `entry_frac` 진입 후 **2회 추가**
(`max_adds: 2`). **add 8% × 2회 → 총 20+8+8 = 36% ≤ 단일종목 캡 40%**(캡의 90%,
`personas.py:40,64,109`). 3종 전부 옛 다이얼(add 10% × 2회 = 20+10+10 = 40% = 캡과 정확히 같음 →
체결후 비중검사에서 add 상시거부, B4)이 동일하게 성립함을 검산 확인 후 add_frac 0.10→0.08 로
정합 완료(2026-08 감사) — 실측 반영됨. oneil 은 `add_frac` 자체가 없어 피라미딩 없음.

**chartist 사이징(2026-08-28 수리)** — 피라미딩 없음, 리스크기반 단발 진입: `amount = min(equity ×
risk_per_trade(2%) × (진입가/주당risk), 캡 40% × 0.95)`. 종전 고정 `entry_frac 20%` + `min_risk_frac`
거부게이트는 SR Flip 의 구조적 소형 risk(레벨 ±1%)를 '진입 금지'로 오처리 → **112세션 진입 0**
(실측 `research/chartist_gate_replay.py`). 수리 후 동일 7일 진입 **21**. `chartist_ctl` 은 같은 cfg
공유라 동일 적용(짝실험 보존). 상세 `desks/risk/memory.md` 2026-08-28. 캡 바인딩 시 실손실 ≈ 0.2~0.4%
equity/거래로 rpt 2% 보다 작다(보수적).

**페르소나 10종**, 전원 시드 $100,000. 대조군 2종(`*_ctl`)은 비큐레이션 ADV16. `buffett_v2` 는
`buffett` 의 A/B 실험군, `canslim_rdcf` 는 `oneil`(canslim)의 A/B 실험군(위 §7 각 절).
단 `canslim_rdcf` 는 일1런 전용(장중 루프 없음)이라 장중 9종에는 미포함.

### 하드스크린 임계 (`fmp_factors.py:111-139`)

`min_net_margin`(0.0) · `max_pe`(80.0) · `max_debt_equity`(None) · `min/max_market_cap`(None).
숫자 강제변환 후 비교 — NaN 무탈락 누수 차단.

---

## 8. 배포 게이트 — 11 스위트

```bash
python tools/run_tests.py     # → "ALL 11 SUITES PASS"
```

managed · toss · exit · hardening · panic · review · personas · intraday ·
selection_review · canslim · dividends (`tools/run_tests.py:28-38`)

- stage1~8(백테스트/리서치)은 **의도적 제외** — vectorbt 등 VM 부재 의존성
- pytest 불필요, `importlib` 직접 호출
- `USTRADE_CI=1` 이면 canslim 제외 → 10스위트
- 전체 회귀는 별개: `test_suites.py` = 23스위트
- **최근 실측: ALL 11 SUITES PASS (07-31 노트북 4회 · 08-01 데스크탑 병합트리)**

---

## 9. 이 문서의 갱신 규약

**갱신해야 하는 때**:
- `GuardConfig` 기본값 변경
- 유니버스 바스켓 구성 변경
- 스케줄 태스크 추가·삭제·시각 변경
- 선정 엔진 추가·제거
- 운용 단계 전이 (T0 개시, L1→L2 승격 등)
- **§B 판정규칙 재등록** (§1 요약 + `docs/paper-trading-dod.md` §B 를 같이 — 구판은 이력 보존)
- 배포 게이트 스위트 수 변경

**갱신 방법**: 값을 고칠 때 `파일:라인` 근거를 같이 갱신한다. 근거 없는 값은 쓰지 않는다.
확인 못 한 항목은 지우지 말고 "미확인"으로 남긴다 — 공백은 "없음"이 아니라 "안 봤음"이다.

---

## 10. 데스크 라우팅

데스크별 라우팅은 같은 폴더 `CLAUDE.md` §2 가 단일 권위다.
