# CLAUDE.md — 미국주식 자동매매

이 리포에서 작업할 때의 규칙. 사용자 글로벌 `~/.claude/CLAUDE.md` 와 함께 적용된다.

---

## 1. 시작 규약 — 예외 없음

트레이딩 관련 작업을 시작하기 전에 **순서대로** 읽는다:

1. **`HOUSE.md`** — 유니버스·한도·킬스위치·스케줄·현재 운용 단계
2. **담당 데스크의 `desks/<desk>/soul.md` + `memory.md`**
3. 그 다음에 스킬을 호출한다

`HOUSE.md` 를 건너뛰고 코드에서 한도를 다시 추론하지 말 것. 그러라고 컴파일해 둔 것이다.
값이 코드와 충돌하면 코드가 옳고, 그때는 `HOUSE.md` 를 갱신한다 (§9 갱신 규약).

## 2. 데스크 라우팅

| 하려는 일 | 데스크 |
|---|---|
| 후보 발굴, 스크리너, 시나리오, 섹터 | `research` |
| 가설 정식화, 백테스트, edge 파이프라인 | `strategy` |
| 사이징, 노출, 레짐, 손절, 킬스위치 기준 | `risk` |
| 주문, 스케줄, 저널, 장애 대응 | `execution` |
| 포스트모템, 성과 귀인, 테제 추적 | `performance` |

구조는 `desks/README.md`. 이번 분기 목표는 `desks/GOALS.md`.

## 3. 두 가지 철칙

검수는 별도 컨텍스트(작성자≠검수자, 글로벌 §8-2) · 기각 사유는 즉시 memory 로(글로벌 §8-3):

```bash
python desks/desk_memory.py append <desk> --kind rejected \
  --text "무엇이 왜 틀렸는가" --source "근거" --rule "도출된 검증 가능한 규칙"
```

## 4. 사람에게 반드시 올릴 것

각 데스크 `soul.md` 의 escalate 절이 권위 있는 목록이다. 공통으로:

- **paper → live 전환** — 자동 승격 절대 금지
- **런 결번** — 원인이 PC 미가동이어도 보고
- **§B 실험 파라미터 변경** — T0 이후 변경은 실험 무효

## 5. 환경 함정

- **MSIX 오버레이**: `%LOCALAPPDATA%\ustrade` 를 세션에서 직접 읽거나 쓰지 말 것.
  검사도 외부 태스크 경유 (원샷 schtasks → Temp 스크래치패드 복사 → 세션에서 읽기).
  `~/.claude/lessons/2026-07-11-claude-msix-overlay.md`
- **프로젝트 폴더의 `state/`·`logs/`·`data_cache/`·`fmp_cache/`·`results/` 는 런타임이 아니다.**
  `.gitignore:2-6` 잔재이고 어떤 코드도 읽지 않는다 — 경로 권위는 `paths.py:16-27`.
  (구스키마 `state/killswitch.json` 은 2026-07-20 삭제. 자세히는 `HOUSE.md` §5)
- 배포 게이트는 pytest 가 아니라 `python tools/run_tests.py` (11스위트).
  `~/.claude/lessons/2026-07-08-ustrade-gate-not-pytest.md`
- `.claude/worktrees/` 에 리포 사본이 있다. 조사·수정은 **메인 트리**에서 한다.

## 6. §A 동결 중

신규 기능·전략 엔진 추가는 `docs/queue-post-freeze.md` 에 큐잉만 한다.
T0 실험이 끝나기 전의 선정 로직 변경은 개입이며 실험 리셋 사유다.
