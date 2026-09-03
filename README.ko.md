> English: [README.md](README.md)

# 미국주식 자동매매

**전략 개발·검증(백테스트) + 라이브 체결(토스 Open API)** 통합 프레임워크.
신호로직 한 번 정의 → **simple / backtrader / vectorbt 세 엔진 공유**(백테스트). 라이브 신호는
`canslim`(텔레그램 시그널 코어 이식), 체결은 `broker/toss.py`(토스증권 Open API, US 종목).
실거래 전 PaperBroker·`toss_check.py`·소액 검증 필수 — 아래 "토스 실거래" 절 참조.

> ## ⚠️ 성과 수치 — 기대치 낮춰라 (개인 자동매매용)
>
> - 이 문서의 성과 수치(Sharpe·MDD·수익률)는 **가설적 in-sample 백테스트**다. 실거래 트랙레코드
>   아님. 미래수익 보장 아님.
> - **상방 편향**됨: 생존편향(현재 구성종목 정적 유니버스), 슬리피지/스프레드 미반영,
>   세금(미국 양도세 22%·배당원천 15%)·환전스프레드 미반영. **실제 net 수익은 표기값보다 낮다** —
>   이 숫자를 기대수익으로 잡지 말 것.
> - 실거래 전 **모의(paper)·소액 검증 필수**. 무인 자동매매는 버그=실손실 (가드레일이 마지막 방어선).
> - (개인 사용 기준이라 데이터/OSS 라이선스·자본시장법은 해당 없음 — 상업 배포 시에만 검토 필요.)

## 설계 핵심

```
data.py            yfinance 다운로드 + CSV 캐시 (한글경로 SSL 문제 자동 패치)
                   load()=단일종목 OHLCV, load_panel()=다종목 종가패널
universe.py        종목 바스켓 (megacap/tech/diversified/sp100) + CSV/콤마 지원
strategies/
  [단일종목]  generate_signals(df) → entry/exit          ← 단일 소스
    ma_cross.py        이동평균 교차 (baseline)
    momentum.py        시계열 모멘텀
    vcp.py             VCP/브레이크아웃 (단순화, 튜닝 전제)
  [포트폴리오] generate_weights(panel) → 리밸런스일 목표비중
    cross_momentum.py  상대강도 모멘텀 (top-N, 주기 리밸런스)
  [팩터]      factors.py — 횡단면 팩터 라이브러리 (momentum/reversal/low-vol/52w/composite)
engines/
  simple_runner.py    단일종목 경량 벡터 (numba 불필요)
  bt_runner.py        단일종목 backtrader (이벤트 기반, 라이브 최유사)
  vbt_runner.py       단일종목 vectorbt (초고속, 파라미터 스윕)
  portfolio_runner.py 다종목 — drift·turnover·fee 정확처리 + 벤치마크
  risk_runner.py      리스크 오버레이 — 레짐필터·변동성타겟·종목손절 (일별)
  metrics.py          성과지표 공통
broker/                라이브 골격 (vnpy 게이트웨이 패턴 발췌)
  base.py              BaseBroker ABC + 데이터모델(Order/Position/Quote/enum)
  paper.py             PaperBroker — 모의체결 (라이브 경로 검증용, 지금 동작)
  toss.py              TossBroker — 토스 Open API(openapi.tossinvest.com) 구현 (7메서드, US-only)
  managed.py           ManagedBroker — 관리 슬리브 (자기 매수분만 거래, 기존 보유분 불가침)
  toss_check.py        토스 연결 점검 — 읽기전용(주문 X). 실거래 전 키·계좌·시세 확인
  toss_setup.py        관리 슬리브 설정 — 현재 보유종목을 protected 로 스냅샷
  executor.py          목표비중 → 주문 변환 (매도→매수, diff 기반)
  guardrail.py         KillSwitch — 무인 안전 가드레일 (영속 상태)
backtest.py            단일종목 CLI
backtest_portfolio.py  포트폴리오 CLI
sweep.py               파라미터 격자탐색 + 과적합 진단
walkforward.py         워크포워드 (롤링 재최적화 OOS)
backtest_risk.py       리스크 레이어 비교 + ablation
eval_factor.py         팩터 IC 검증 (Alphalens 발췌) — 거래 전 알파 게이트
fmp_client.py          FMP 무료티어 클라이언트 (키로더+캐시+백오프)
fmp_factors.py         어닝서프라이즈(premium차단) + 현재 펀더멘털 스냅샷/스크린
live_select.py         라이브 선택 — 모멘텀 랭킹 + 펀더멘털 스크린 (결측=플래그)
live_select_canslim.py 라이브 선택 v2 — 텔레그램 시그널 코어 이식 (12-1 모멘텀+💎CANSLIM/📋애널 교차검증)
live_risk.py           라이브 리스크 오버레이 — 레짐(SPY 200MA)+vol타겟
live_engine.py         run_once() — 선택→리스크→가드→체결 (DRY 단일소스)
live_rebalance.py      CLI 데모 (run_once를 PaperBroker로)
run_live.py            운영 원샷 진입점(데일리 진입) — cron 호출, 저널+알림
live_exit.py           장중 청산 로직 (A 매도룰 코어: 200MA 이탈 + 손절, 결정적)
run_exit.py            장중 청산 원샷 — cron N분(미 정규장), 보유분 청산룰 점검→매도
notify.py              알림 스텁 (텔레그램/슬랙, 미설정시 로그)
(데모·대안 스크립트 live_demo/live_filter_demo/scheduler 는 archive/ 로 이동 — 운영 미사용)
```

엔진별 결과 차이는 정상: simple↔vectorbt 거의 일치(분수 주식·벡터화),
backtrader는 정수 주식수·익일체결로 약간 보수적 → **라이브 환경에 가장 근접.**

## 실행

```powershell
$py = "C:\Users\<you>\.venvs\ustrade\Scripts\python.exe"
cd "C:\Users\<you>\OneDrive\문서\Claude\Projects\미국주식 자동매매"

# 단일 엔진
& $py backtest.py --ticker AAPL --strategy ma_cross --engine simple

# 세 엔진 동시 비교
& $py backtest.py --ticker NVDA --strategy momentum --engine all --start 2018-01-01

# 전략 파라미터 자유 전달
& $py backtest.py --ticker MSFT --strategy ma_cross --engine backtrader --fast 10 --slow 30
& $py backtest.py --ticker TSLA --strategy momentum --lookback 60 --threshold 0.05
& $py backtest.py --ticker AMD  --strategy vcp --vol_mult 2.0
```

옵션: `--start --end --cash --fee --force(캐시무시)`
결과 차트 → `results/`, 데이터 캐시 → `data_cache/`

### 포트폴리오 (다종목 / 상대강도 모멘텀)

```powershell
# 섹터분산 28종목, 모멘텀 상위 5종목 월리밸런스, SPY 벤치마크
& $py backtest_portfolio.py --universe diversified --strategy rs_momentum --top_n 5

# 빅테크 유니버스, 상위 3종목, 6개월 모멘텀
& $py backtest_portfolio.py --universe tech --top_n 3 --lookback 126

# 직접 지정 유니버스
& $py backtest_portfolio.py --universe AAPL,MSFT,NVDA,AMD,GOOGL --top_n 2 --freq M
```

파라미터: `--lookback`(모멘텀 기간) `--skip`(최근 제외일, 12-1 모멘텀) `--top_n`(보유종목수)
`--freq M|W|Q`(리밸런스 주기) `--benchmark SPY|none`
벤치마크 = 유니버스 동일비중 + 지정 ETF 매수후보유 자동 비교.

유니버스: `megacap`(15) `tech`(15) `diversified`(28) `sp100`(101) | CSV경로 | 콤마구분.

---

## 검증툴 3종 (전략 신뢰도 — 절대수익보다 중요)

### 1. 파라미터 스윕 + 과적합 진단 — `sweep.py`

lookback×top_n 격자를 train/test 분할로 평가. 3개 진단:
- In/Out-of-sample 성과 감쇠 (train 최적이 test서도 먹히나)
- train↔test Spearman 순위상관 (+0.5↑ 전이양호 / 0·음수 = 과적합)
- plateau (고립 스파이크 vs 넓은 고원 — 고원 중심이 robust)

```powershell
& $py sweep.py --universe diversified --split 2021-01-01
& $py sweep.py --universe sp100 --lookbacks 63,126,252 --top_ns 3,5,10
```
→ 히트맵(train/test/전체) + CSV. **실측 결과: Spearman -0.09 → 파라미터 재최적화는 과적합.**

### 2. 워크포워드 — `walkforward.py`

train_years 로 best 파라미터 선택 → 다음 test_years 적용 → 전진. 연속 OOS 곡선.
핵심 비교: **WFO(재최적화) vs 고정 robust 파라미터.**

```powershell
& $py walkforward.py --universe diversified --train_years 3 --test_years 1 --fixed_lookback 126 --fixed_top_n 3
```
→ **실측 결과: 고정(126,3) Sharpe 1.57 vs WFO 1.14. 재최적화가 노이즈 추종해 완패.**
WFO가 매 구간 train 1등 (63,2)=과적합코너 골라 다음 해 붕괴 반복. **결론: 파라미터 고정.**

### 3. 리스크 레이어 — `backtest_risk.py`

고정 전략에 오버레이 적용, 베이스라인 대비 MDD 축소 비교. 자산곡선+언더워터 차트.
- 레짐필터: SPY 200MA 아래면 전량 현금 (리밸런스 때만 재진입)
- 변동성타겟: 실현변동성으로 총노출 스케일 (max_leverage=1.0, 디레버리지만)
- 종목손절: 진입가 대비 -X%면 그 종목만 현금화

```powershell
& $py backtest_risk.py --universe diversified --vol_target 0.20 --stop_loss 0    # 권장
& $py backtest_risk.py --universe diversified --no-regime --stop_loss 0.10       # ablation
```
**실측 ablation (효율 = Sharpe 유지하며 MDD 축소):**

| 오버레이 | Sharpe | MDD | 판정 |
|---|---|---|---|
| 베이스라인 | 1.53 | -41% | — |
| 레짐+vol0.20 | **1.48** | **-21%** | ⭐ 최고효율 |
| 레짐만 | 1.37 | -31% | 무난 |
| 종목손절 0.15 | 1.43 | -38% | ❌ 모멘텀 상극 (효과↓ Sharpe↓) |

**결론: 변동성타겟이 MDD를 가장 싸게 깎음. 종목 손절은 모멘텀에 역효과 — 쓰지 말 것.**

### 4. 팩터 IC 검증 — `eval_factor.py` (ml-for-trading 발췌)

sweep/walkforward 가 "파라미터" 과적합을 잡는다면, 이건 **팩터 자체에 알파가 있나**를 검증.
시점별 팩터값↔미래수익 횡단면 Spearman IC + 분위수 수익 + decay.

```powershell
& $py eval_factor.py --universe diversified --factor all                    # 전 팩터 IC 비교
& $py eval_factor.py --universe diversified --factor momentum_6_1            # 단일 상세(decay+분위수)
& $py eval_factor.py --universe diversified --factor composite --combine momentum_6_1,momentum_12_1
```
판정 기준: |mean IC|≥0.02 & IC-IR≥0.3 & |t|≥2 → 유의미 / 분위수 단조+양스프레드 → 거래가능.

**실측 (diversified 28종목):**

| 팩터 | mean IC | IC-IR | t | 판정 |
|---|---|---|---|---|
| **momentum_6_1** | **0.061** | 0.19 | 2.13 | 최강이나 노이즈 |
| momentum_12_1 | 0.040 | 0.13 | 1.40 | 약 |
| low_volatility | **-0.066** | -0.22 | -2.43 | ⚠️ 역부호 (성장株 강세장) |
| reversal_1m / high_52w | ~0 | ~0 | ~0 | 무신호 |
| composite | 0.020 | 0.06 | — | 희석 (가격팩터 상관 높음) |

**결론**: 6개월 모멘텀 = 약한 real alpha (top분위 +2.55%/월, IC decay 장기↑).
IC-IR 0.19 = 노이즈 → 파라미터 재최적화 위험의 정량 근원. **가격 다팩터는 상관 높아 무익 →
진짜 멀티팩터는 직교 펀더멘털(가치·퀄리티·실적, FMP 필요).**

---

## ✅ 확정 구성 (실측 검증 통과)

```
유니버스   : diversified (28종목)
전략       : rs_momentum, lookback=126(6개월), skip=21, top_n=3, freq=M(월)
리스크     : 레짐(SPY 200MA) + vol_target=0.20, 종목손절 OFF
성과        : Sharpe ~1.5, MDD ~-21%, 노출 ~60%
             ※ 가설적 in-sample 백테스트값 (생존편향·비용·세금 미반영, 상방편향). 상단 면책 참조.
             ※ 비용·세금·생존편향 보정 시 현실적 Sharpe 는 이보다 현저히 낮음. 라이브 트랙레코드 없음.
```
```powershell
& $py backtest_risk.py --universe diversified --lookback 126 --top_n 3 --vol_target 0.20 --stop_loss 0
```
파라미터 재최적화 금지(과적합). 이 구성을 고정해 라이브 이식 예정.

---

## 🔔 신호 전략 `canslim` — 텔레그램 시그널 알리미 코어 이식

별도 프로젝트 `텔레그램_시그널_알리미`(공유방 신호 → 교차검증 → 봇 DM)에서 **검증된 코어 매수신호**만
이 자동매매의 선택 단계로 이식. `live_select_canslim.py` 가 `live_select.select` 과 동일 계약
`select(prices) → (weights, info)` 을 만족해 체결경로(오버레이·가드레일·Executor) **무수정** 연결.
펀더 함수는 A `engine/funda.py`(yfinance) 를 직접 import — 단일소스 재사용(복제 X, 전사오류 0).

**신호 파이프라인** (종가패널 → 목표비중):
1. **가격 스크린(하드게이트)** — `close>200MA` AND 52주 고가근접 `prox≥0.85` AND **12-1 모멘텀>0**
   (A 검증: 12-1 이 6-1 보다 OOS 우월 1.27). 모멘텀 내림차순 상위 `pool`(12)을 펀더 검증 풀로.
2. **교차검증 스코어** — 💎CANSLIM(분기 EPS YoY≥25% & 매출≥10%) + 📋애널 strong_buy → `score 0~2`.
   모멘텀이 게이트, 펀더는 랭킹 틸트(A와 동일 — 필수 아님). Piotroski F 점수는 `info` 에 기록.
3. **선정** — `(score↓, 모멘텀↓)` 정렬 상위 `top_n` → 동일비중. 이후 `apply_overlay`(레짐 SPY200MA +
   vol타겟 0.20)가 노출 조절(레짐 OFF=현금, 고변동=디레버리지).

**보수성 다이얼**(opt-in, 기본은 A에 충실):
- `min_score`(기본 0) — `score<N` 제외 (1 = 💎/📋 확인 없는 순수 모멘텀 차단)
- `value_trap_gate`(기본 off) — Piotroski reliable & F<5 제외 (A는 GARP 에만 적용)

**실행**:
```powershell
& $py run_live.py --strategy canslim --universe sp100 --broker paper --force   # 권장(후보풀 넓음)
& $py run_live.py --strategy momentum                                          # 기존 모멘텀+FMP 경로
```
`python run_live.py` 무인자 = `canslim` + `paper` 기본. `RunConfig.strategy` dataclass 기본값은
`momentum`(기존 회귀 테스트의 `live_engine.select` monkeypatch 훅 보존) — **운영 진입점(run_live)만
canslim 기본**. 실측(megacap, paper): 모멘텀풀 8 → 💎/📋 교차검증 → score2 GOOGL·NVDA·AMZN 선정 →
vol타겟이 1/3 비중을 0.238 로 디레버리지 → 3건 체결, reconcile ok.

**범위**: **코어 매수신호만 자동체결.** A 의 스윙·GARP·PEAD·방언급은 약한 엣지(본인 백테스트:
스윙 OOS 0.57, GARP 0.49)라 이식 안 함 — 텔레그램 알림용으로 A 에서 유지.

---

## ⚠️ 편향 경고 (반드시 인지)

1. **생존편향** — 모든 유니버스가 *현재* 구성종목 정적 목록. 상장폐지 종목 누락 → 절대성과 낙관.
2. **셀렉션 편향 (sp100 특히 심함)** — S&P100 = 오늘의 승자 집합. 과거 모멘텀 백테스트에 미래정보 침투.
   집중(top3)할수록 증폭 → sp100 top3 +8769% 같은 **비현실적 수익**. 라이브 재현 불가.
3. **유니버스 확대 ≠ 개선** — sp100(101종목) 최고 Sharpe 1.38 < diversified(28) 1.53.
   종목 늘려도 위험조정수익 안 오름. sp100은 메커니즘 검증용, **성과 주장엔 부적합.**
4. 진짜 해법 = **point-in-time 구성종목** 데이터 (Norgate/CRSP 등, 유료). 정적 리스트론 못 고침.

→ 이 프레임워크의 가치는 절대수익이 아니라 **방법론적 결론** (과적합·손절상극·vol타겟효율).

## 전략 추가

- 단일종목: `strategies/` 에 `Strategy` 상속 (`name`+`generate_signals`), `REGISTRY` 에 추가 → 3엔진 자동
- 포트폴리오: `PortfolioStrategy` 상속 (`name`+`generate_weights`), `PORTFOLIO_REGISTRY` 에 추가

## 환경

- venv: `C:\Users\<you>\.venvs\ustrade` (OneDrive 밖 — 동기화 회피)
- 의존성: `requirements.txt`
- 재설치: `& $py -m pip install -r requirements.txt`

## FMP 펀더멘털 (무료티어) — 라이브 필터 전용

키: 환경변수 `FMP_API_KEY` 또는 `~/.config/fmp_api.key` (OneDrive 밖, 하드코딩 금지).

**⚠️ 무료티어 현실 (실측)**:
| 데이터 | 무료 | 비고 |
|---|---|---|
| quote, profile, ratios-ttm, key-metrics-ttm | ✓ | **현재 스냅샷만** |
| ratios/key-metrics/income (period=quarter) | 💰 402 | 과거 시점 펀더멘털 = premium |
| earnings | ✓ but **limit 4 캡** | 5개 이상 = 402, 사실상 현재만 |

→ **결론: 무료론 펀더멘털 historical 백테스트 불가.** 현재 스냅샷 = **라이브 품질/가치 필터**로만.
(과거 펀더멘털 IC 검증 원하면 FMP 유료 / SimFin·SEC EDGAR 무료 historical 별도 필요.)

**통합 라이브 경로** (선택→리스크→체결):
```powershell
& $py live_rebalance.py --universe diversified --top_n 3 --pool 8 --vol_target 0.20
```
데이터 → `live_select`(모멘텀 top8 → 펀더멘털 스크린 → top3) → `live_risk`(레짐+vol) → `Executor` → 브로커.
**실측**: 모멘텀 top3 TSLA·WMT·HD → TSLA(P/E 364) 제거 → 레짐 ON·vol 100% → 체결.
리스크 오버레이: SPY<200MA면 전량현금, 실현변동성>목표면 노출 축소 (vt=0.10 → 70% 투자 확인).
주의: 스냅샷 1일1회+캐시 (무료 250req/day, 버스트 429). 백테스트엔 펀더멘털 필터 미적용(과거 데이터 없음).

## 라이브 골격 (broker/) — 토스 전 미리 구축

vnpy 게이트웨이 패턴 발췌. 전략/체결 분리 → **PaperBroker 로 라이브 경로 전체를 토스 없이 검증 완료.**

```powershell
& $py archive\live_demo.py --universe diversified --lookback 126 --top_n 3
```
→ rs_momentum 목표비중 → Executor diff 계산 → PaperBroker 체결 → 포지션/계좌 갱신.
연속 리밸런스로 turnover(부분매도·매수 diff) 동작 확인됨.

**토스 구현 완료** = `broker/toss.py` 7메서드를 토스 Open API(v1.1.1)에 매핑. 나머지(Executor·
전략·리스크·가드레일) 무수정. 상세·실거래 체크리스트는 아래 "토스 실거래" 절 참조.

### 킬스위치 (broker/guardrail.py) — 무인 안전 가드레일

주문 제출 **전** 체크, 상태 `state/killswitch.json` 영속 (스케줄 실행 간 유지).
- **수동 HALT**: `state/HALT` 파일 생성 시 즉시 전면 정지 (operator kill)
- **일일손실 한도**: 당일 시작자산 대비 -5% 초과 → 정지 (다음날 자동 리셋)
- **포지션 바운드**: 단일종목 >40% or 총노출 >105% → 차단
- **연속 API 에러** 3회 → 정지 / **주문 명목 상한** (fat-finger)

트립 시 **수동 reset 전까지 거래 거부** (`--reset-halt`). 무인은 버그=손실 → 마지막 방어선.
실측: top1(100%>40%) 트립→재실행도 정지 유지→reset 후 재개 확인.

## 배포 스캐폴드

**원샷 실행** (cron/Task Scheduler가 호출 — 크래시해도 OS가 재실행, 권장):
```powershell
& $py run_live.py                # paper + canslim 1회 (저널 logs/runs.jsonl + 알림)
& $py run_live.py --broker toss  # 토스 실거래 (TOSS_API_KEY/SECRET 필요 — 아래 절 먼저)
& $py run_live.py --force        # 당일 중복실행 락 무시 (수동 재실행)
```
거래 대상 세션은 ET 기준 직전 종료 NYSE 세션으로 자동 판정(DST·공휴일 처리). 당일 1회만
거래(중복매매 방지 락), 데이터가 stale 하면 거래 보류, 일부 체결 실패 시 다음 실행이 재조정.

**스케줄 (미장 마감 후 = 한국 새벽, 평일)**:
- Linux VM (권장): `crontab -e` →
  `10 6 * * 1-5 cd /path/proj && /path/.venvs/ustrade/bin/python run_live.py >> logs/cron.log 2>&1`
- Windows: 작업 스케줄러 → 매일 06:10 트리거 → `python run_live.py`
- 상시 루프 대안: `python archive/scheduler.py` (pip install schedule — 운영은 Task Scheduler 사용)
- ⚠️ **머신 env 변경 후 재부팅(또는 Schedule 서비스 재시작) 필수** — Task Scheduler 서비스가
  부팅 시점 env 를 캐시하므로, TELEGRAM_*/FMP_API_KEY 등을 새로 설정해도 태스크에는 안 보임.
  (2026-07-01~03 실사례: FMP 키가 태스크에 안 보여 "펀더 스크린 무력화" + 그 경보가
  "채널 미설정 — 로그만"으로 3일간 무성. 7/3 재부팅으로 자가치유.)

**토스 실거래 스케줄 (진입 데일리 + 청산 장중)** — MARKET 주문은 미 정규장(EDT 22:30~05:00 /
EST 23:30~06:00 KST)에만 체결되므로 둘 다 그 시간대에. 개장 전·마감 후 틱은 토스 `market_open`
게이트가 흡수(닫히면 자동 skip) — 단 **cron 윈도 자체는 DST 양 체제를 다 덮어야** 한다:
```
# 진입: 평일 1회 (개장 직후 예시 KST 23:35 — 둘 다 개장 후)
35 23 * * 1-5  cd /path/proj && /path/.venvs/ustrade/bin/python run_live.py --broker toss --universe sp100 --cash-cap 500 >> logs/cron.log 2>&1
# 청산: 정규장 중 15분마다 (DST 양 체제 커버 22~06시 KST; 기존 23,0-4 는 겨울 EST 마지막 1h 미커버)
*/15 22-23,0-6 * * 1-5  cd /path/proj && /path/.venvs/ustrade/bin/python run_exit.py >> logs/exit.log 2>&1
```
구름 VM 의 cron 이 UTC 면 시각을 환산할 것. 청산은 `--use-50ma`/`--ob-rsi 80`/`--stop-pct` 로 조절.
- DST·공휴일은 NYSE 캘린더(`calendar_util`)가 처리 — RUN_TIME 은 "미장 마감 이후" 아무 시각이면 됨
  (실제 거래 세션은 ET 기준 자동 판정, 휴장일엔 자동 스킵). 계절별 수동 조정 불필요.

**알림 설정** (환경변수, 미설정 시 logs/alerts.log만):
```
TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   또는   SLACK_WEBHOOK_URL
```
체결/정지/트립/에러 시 발송. 알림 실패가 거래를 막지 않음 (예외 삼킴).

**산출**: `logs/runs.jsonl`(매 실행 저널), `logs/alerts.log`, `state/killswitch.json`(가드 상태).

## 토스 실거래 (`broker/toss.py`)

토스증권 Open API(`openapi.tossinvest.com`, v1.1.1) 어댑터. `BaseBroker` 7메서드를 매핑해
canslim 신호 → 오버레이 → 가드레일 → Executor → **실체결**까지 무수정 연결된다.

| 메서드 | 토스 엔드포인트 |
|---|---|
| connect | `POST /oauth2/token`(client_credentials) → 토큰 + `GET /api/v1/accounts` → accountSeq |
| get_account | `GET /api/v1/buying-power`(USD) + `/api/v1/holdings` → cash·equity |
| get_positions | `GET /api/v1/holdings` → **marketCountry=="US" 만** |
| get_quote | `GET /api/v1/prices` → lastPrice |
| place_order | `POST /api/v1/orders` (수량기반, clientOrderId 멱등키) |
| cancel_order / get_order | `POST /api/v1/orders/{id}/cancel` / `GET /api/v1/orders/{id}` |

### 관리 슬리브 (`broker/managed.py`) — 단일 계좌 공유 안전장치

토스는 계좌 1개만 개설 가능 → 기존 포트폴리오(레버리지 ETF 등)와 자동매매가 **한 계좌를 공유**한다.
리밸런서는 '목표에 없는 보유종목 = 청산'으로 보므로 가만 두면 네 기존 종목을 전량 매도한다.
`ManagedBroker` 가 이를 원천 차단 — **자동매매는 자기가 체결로 보유한 수량(basis)만 거래하고,
설정 시점 보유분(protected)은 매도도 매수도 안 한다.** 방어:
1. `get_positions` → managed 종목을 **min(실수량, basis)** 로 노출. 보호분 숨김 + 사용자가 같은
   종목을 추가 매수(co-mingle)해도 자동매매는 자기 basis 만큼만 봄 → **사용자분 불가침**
2. `place_order` → **SELL=managed&¬protected, BUY=¬protected** (위반 시 REJECTED)
3. 후보 유니버스에서 protected 제외 → 전략이 기존종목을 타겟으로 안 함
4. basis 는 **체결 후에만** 갱신 → 거부·미체결 매수가 슬리브를 오염시키지 않음
- 심볼은 전 경계에서 정규화(`BRK.B`↔`BRK-B`·대소문자·공백) → 표기 불일치로 보호가 뚫리지 않음

사이징도 **슬리브 기준**(equity = 현금 + managed(cap) 평가액)이라, 기존 종목을 팔아 자금 마련 불가.

```powershell
& $py toss_setup.py                    # 현재 보유종목 → protected 스냅샷 (state/toss_sleeve.json)
```
자동매매에 줄 현금을 만들려고 기존 종목 일부를 **먼저 판 뒤** 실행하면, 남은 보유분 전부가
protected 로 고정된다. 슬리브 미설정 시 `run_live --broker toss` 는 거부된다.
현금 한도: `--cash-cap 500` 또는 env `TOSS_MANAGED_CASH=500` (미지정 시 계좌 가용현금 전부).

**설정 + 읽기전용 점검** (실거래 전 필수):
```powershell
setx TOSS_API_KEY "c_..." ;  setx TOSS_API_SECRET "s_..."   # 새 셸에서 적용
& $py toss_check.py                # 연결·계좌·보유·시세만 확인 (주문 X)
```

**⚠️ 실거래 전 반드시 인지 (안전 설계 + 미해결 운영결정)**:
1. **샌드박스 없음** — 토스 Open API엔 paper 엔드포인트가 없다. `TossBroker`는 항상 실거래.
   모의는 `--broker paper`(PaperBroker)로만.
2. **기존 보유분 불가침** — `ManagedBroker`(관리 슬리브, 위)가 자동매매를 *자기 매수분*에
   가둔다. 설정 시점 보유종목(레버리지 ETF·KR 주식 등)은 매도·매수 모두 차단된다.
   `toss_setup.py` 로 스냅샷 안 하면 실거래 거부.
3. **계좌뷰 = USD 기준** — cash=USD 매수가능금액, equity=cash+USD평가액. 계좌가 **KRW 자동환전**
   방식이면 USD 매수가능금액이 0/불안정 → 가드레일(일손실·드로다운)이 오작동할 수 있다.
   `toss_check.py`로 cash/equity가 합리적인지 먼저 확인할 것.
4. **주문 타이밍 ↔ 주문유형 (미해결 결정)** — Executor 기본은 **MARKET** 주문. MARKET은 미국 정규장
   (KST 22:30~05:00)에만 체결된다. 현 스케줄(미장 마감 후 한국 새벽 실행)로 MARKET을 내면
   `order-hours-closed`로 거부될 수 있다. 선택지: (a) 실행 시각을 미 정규장 중으로, 또는
   (b) 지정가/LOC(`timeInForce=CLS`) 도입. **이 결정 전엔 토스 실거래 금지** — paper로만.
5. **에러 처리** — 4xx(잔고부족 등)=주문 REJECTED 반환(다음 실행이 재조정), 5xx/네트워크=raise→
   연속에러 한도 시 가드레일 자동정지. 실거래는 알림 채널(TELEGRAM_*/SLACK) 미설정 시 거부된다.

**go-live 체크리스트**:
1. `toss_check.py` 정상 (연결·cash/equity 합리성)
2. 자동매매 줄 현금 만들려 기존종목 일부 매도 → `toss_setup.py` (남은 보유분 protected 스냅샷)
3. 알림 채널(TELEGRAM_*/SLACK) 설정
4. 주문 타이밍 결정 (위 4번)
5. `--cash-cap` 설정 → **소액 1주 수동 검증** → 가드레일 한도 점검 → 무인 스케줄 가동

### 장중 청산 오버레이 (`run_exit.py`)

진입은 데일리(`run_live`)가, **빠른 청산은 장중 루프**가 담당. 모멘텀 엣지는 저빈도가 정답이라
진입을 늘리지 않되, 리스크는 장중에 즉시 끊는다. `live_exit.py`가 A 매도룰 코어를 이식:
- 현재가 < **200일선** → 추세 붕괴 전량 청산 📉
- 현재가 < 평균매입가 × (1−**stop_pct**, 기본 8%) → 손절 🛑
- opt-in: `--use-50ma`(50MA 이탈 🟡), `--ob-rsi 80`(RSI 과열 청산 🔺)

MA 레벨은 일봉이지만 **현재가가 실시간** → 장중에 MA 깨는 즉시 트리거. ManagedBroker 경유라
**봇 매수분만** 청산(기존 보유분 불가침). 미 정규장 닫혀있으면 자동 skip. 저널 `logs/exits.jsonl`.
```powershell
& $py run_exit.py                  # 1회 점검(장중이면 청산)
& $py run_exit.py --stop-pct 0.10 --use-50ma
```

## 다음 단계

1. **주문 타이밍/유형 결정** (위 4번) — 토스 실거래의 유일한 블로커.
2. 24/7 클라우드 VM 배포 (미장 = 한국 새벽). DST·공휴일은 `calendar_util` 처리.
3. 매매일지 로깅 (trading-skills `trader-memory-core` 연동 가능).

⚠️ 모든 전략은 백테스트 baseline. 실거래 전 충분한 검증·모의투자 필수.
과거성과 ≠ 미래수익.
