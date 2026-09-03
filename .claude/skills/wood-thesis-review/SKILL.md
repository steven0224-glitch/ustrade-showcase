---
name: wood-thesis-review
description: wood(캐시우드형) 페르소나의 현재 픽을 성장 렌즈로 오프라인 검증 — 티커별 파괴적 혁신 thesis 유효성(intact/weakened/broken), 밸류체인 병목 노출, 적자기업 cash runway·희석 이력을 WebSearch로 조사해 verdict 테이블 출력. 자문 전용(매매루프·저널 무기록). 트리거 — "wood 픽 검증", "우드 thesis 점검", "/wood-thesis-review"
---

# wood-thesis-review — wood 픽 성장렌즈 오프라인 검증

wood 퀀트 선정의 구조적 공백을 사람 눈으로 메우는 자문 스킬. wood 의 성장은 프록시
(고P/S+저배당+고모멘텀 — 무료 FMP 에 매출성장 필드 없음, live_select_wood.py 참조)라
"왜 오르는가(thesis)"를 검증하지 못한다. ai-berkshire(xbtlin/ai-berkshire)의
bottleneck-hunter·thesis-tracker **구조만** 차용하고 가치 렌즈는 제거했다.

## 불변식 (위반 금지)

1. **자문 전용** — 매매루프·저널·state 에 아무것도 쓰지 않는다. 출력은 대화뿐.
   매도/매수 지시 금지 — verdict 는 관찰이며 wood 는 다음 일1런에서 스스로 재선정한다.
2. **가치 렌즈 금지** — PE/PB/DCF 저평가 판정 금지. wood 철학 = 적자 혁신 허용.
   버핏 잣대(저PE·흑자·안전마진)로 성장주를 재단하면 전 종목 기각되는 범주 오류.
3. **결정론 루프 불가침** — 이 스킬의 어떤 산출물도 자동매매 입력이 되지 않는다.

## 절차

### 1. 픽 획득

VM에서 wood 최신 선정을 읽는다 (SSH 는 PowerShell 툴로 직접 — 사용자에게 명령 넘기지 말 것):

```powershell
# VM의 wood 페르소나 저널 마지막 selection 레코드 (5MB 로테이션 → .1 폴백)
ssh <vm> "powershell -Command \"Get-Content C:\ustrade-paper-wood\logs\runs.jsonl -Tail 50\""
```

- JSONL 레코드 중 `"selection"` 키 있는 마지막 것 → `selection.final` = 픽 리스트,
  `selection.scores` = 성장점수 (참고 표기용).
- `runs.jsonl` 부재/빈 파일 → `runs.jsonl.1` 시도. 그래도 실패 → 사용자에게 티커 목록 요청.
- 픽이 비었으면(레짐 off 등) 그대로 보고하고 종료.

### 2. 티커당 3축 리서치 (WebSearch, 티커당 축별 1~2회)

| 축 | 질문 | 판정 |
|---|---|---|
| ① thesis | 이 종목의 파괴적 혁신 서사는 무엇인가? 최근 분기 실적·가이던스·핵심 KPI(성장률 추세)가 그 서사를 지지하는가? | **intact** / **weakened** / **broken** |
| ② bottleneck | 밸류체인 어디에 있나 — 병목 소유자(수혜)인가 병목 의존자(노출)인가? 공급 제약·경쟁 대체재 리스크는? | 수혜 / 중립 / 노출 |
| ③ runway | (적자·FCF음수 기업만) 현금 소진까지 몇 분기? 최근 2년 희석(증자·전환사채) 이력은? | 여유(2년+) / 주의 / 위험(4분기−) |

- 근거는 최근 자료 우선(어닝콜·10-Q 보도·주요 뉴스). 축당 출처 1개 이상 명시.
- 흑자·FCF양수 기업은 ③ 생략("해당 없음").
- 판정 불가(정보 부족)면 솔직히 "판정 불가"로 — 억지 판정 금지.

### 3. 출력 — verdict 테이블

```
| 티커 | 성장점수 | ① thesis | ② bottleneck | ③ runway | 핵심 근거(출처) |
```

테이블 뒤 요약 3줄 이내: broken/위험 판정 종목 지목 + "자문 전용, 매매루프 무연결" 명시.
weakened/broken 이 있어도 행동 지시는 하지 않는다 — 사용자가 판단.
