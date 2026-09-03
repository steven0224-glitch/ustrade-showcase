# desks/ — 하우스 데스크 구조

이 리포는 스킬 모음이 아니라 **하나의 운용사(house)** 로 조직된다. 다섯 개 데스크가 각각
자기 영역을 소유하고, 자기 메모리를 축적하며, 서로의 산출물을 검수한다.

출처: 2026-07-20 세션에서 외부 글 3편(Karpathy LLM Wiki / 42-skill org chart / 5-pillar agent team)을
검토해 도출한 3가지 갭을 적용한 구조. 배경은 `~/.claude/lessons/2026-07-20-desk-architecture.md`
(리포 안이 아니라 전역 lessons 디렉토리에 있다).

---

## 1. 왜 데스크인가

기존 구조의 문제는 스킬이 **무상태**라는 것이었다. vcp-screener를 100번 돌려도
101번째 호출은 100번의 경험을 모른다. 교훈은 세션이 끝나면 대화 로그로만 남는다.

데스크는 이 문제를 세 개의 파일로 해결한다:

| 파일 | 역할 | 누가 쓰나 |
|---|---|---|
| `soul.md` | 계약 — 무엇을 소유하고(own), 무엇이 완료이며(done), 무엇은 사람에게 올리는가(escalate) | 사람이 정함. 데스크는 못 바꿈 |
| `memory.md` | 축적 — 무엇이 통했고 무엇이 틀렸는가 | 데스크가 씀 (append-only) |
| `goals.md` | 현재 분기의 목표와 KPI | 사람이 정함, 분기마다 갱신 |

**핵심은 memory.md가 데스크마다 갈라진다는 점.** 리스크 데스크의 "이 레짐에서 사이징
실수한 패턴"과 리서치 데스크의 "이 스크리너는 이 구간에서 노이즈가 많다"는 서로 다른
전문성이다. 전역 `lessons/`에 섞으면 회수율이 떨어진다.

## 2. 다섯 데스크

| 데스크 | 소유 영역 | 주요 스킬 |
|---|---|---|
| `research` | 아이디어 발굴 — 유니버스 스캔, 후보 생성, 시나리오 | vcp-screener, canslim-screener, pead-screener, kanchi-*, value-dividend-screener, scenario-analyzer, sector-analyst |
| `strategy` | 가설 → 검증된 전략 | edge-* 파이프라인, backtest-expert, trade-hypothesis-ideator, strategy-pivot-designer |
| `risk` | 크기와 노출 — 얼마나, 언제 멈출까 | position-sizer, exposure-coach, macro-regime-detector, us-market-bubble-detector, market-top-detector |
| `execution` | 주문과 운영 — paper/live 엔진, 스케줄, 장애 | 리포 본체 (live_engine.py 등), breakout-trade-planner, parabolic-short-trade-planner |
| `performance` | 결과 해석과 되먹임 — **write-back의 발원지** | signal-postmortem, trade-performance-coach, trader-memory-core |

## 3. 두 가지 철칙

### 철칙 1 — 어떤 데스크도 자기 일을 스스로 채점하지 않는다

산출물은 반드시 다른 데스크가 검수한다. 검수자는 **"이 결과는 틀렸다고 가정하고
어디가 틀렸는지 찾아라"** 라는 자세로 본다.

| 작성자 | 검수자 | 검수 관점 |
|---|---|---|
| research | strategy | 이 후보가 검증 가능한 가설이 되는가, 아니면 그냥 눈에 띄는 종목인가 |
| strategy | risk | 백테스트가 살아남을 크기인가, 과최적화·생존편향은 없는가 |
| risk | performance | 과거 실제 결과가 이 한도를 정당화하는가 |
| execution | risk | 이 주문이 한도를 넘지 않는가 |
| performance | research | 이 포스트모템이 원래 가설을 공정하게 평가하는가 |

검수는 별도 세션·별도 컨텍스트에서 하는 것이 원칙이다. 같은 대화에서 이어서 하면
작성 맥락에 오염된다. `verifier` 서브에이전트가 이 용도다.

### 철칙 2 — 기각 사유는 반드시 memory.md로 돌아간다

검수에서 기각당했으면 그 이유를 작성자 데스크의 `memory.md`에 기록한다.
기록하지 않은 기각은 반복된다. 이것이 이 구조 전체의 존재 이유다.

```bash
python desks/desk_memory.py append risk \
  --kind rejected \
  --text "레짐 전환 직후 ATR 사이징이 과대. 20일 ATR은 갭 이후 3일간 신뢰 못 함" \
  --source "2026-07-20 백테스트 검수"
```

## 4. 세션 시작 규약

트레이딩 작업을 시작할 때 순서:

1. `HOUSE.md` 를 읽는다 — 하우스 전체의 고정 규정 (유니버스, 한도, 현재 단계)
2. 담당 데스크의 `soul.md` + `memory.md` 를 읽는다
3. 그 다음에 스킬을 호출한다

`HOUSE.md`는 Karpathy LLM Wiki의 컴파일 캐시와 같은 역할이다. 스킬마다 코드를 다시
읽어 한도를 추론하는 대신, 한 번 컴파일된 사실을 읽는다.

## 5. memory.md 관리

- **append-only.** 과거 기록을 지우지 않는다. 틀린 것으로 판명되면 지우지 말고
  새 항목으로 반박을 추가한다 (`--kind corrected`).
- **한 페이지를 넘으면 압축한다.** 영원히 자라는 메모리는 읽히지 않는다.
  `desk_memory.py compact <desk>` 가 압축 대상을 표시해준다. 압축은 사람이 검토 후 수행.
- 항목 형식은 `desk_memory.py` 가 강제한다. 손으로 편집하지 말 것.

## 6. 경계 — 다른 메모리 시스템과 구분

| 저장소 | 담는 것 |
|---|---|
| `desks/*/memory.md` | **도메인 판단 교훈** — 이 시장·이 전략·이 종목군에서 배운 것 |
| `~/.claude/lessons/` | **도구·환경 교훈** — 삽질 원인, 환경 함정 |
| auto memory | 사람의 선호·정책 |
| memory-bank | 대화 히스토리 회상 |
| `wip/` | 지금 열린 작업의 다음 행동 |

트레이딩 판단에 관한 교훈이면 데스크 메모리, Claude Code 환경에 관한 것이면 lessons.
