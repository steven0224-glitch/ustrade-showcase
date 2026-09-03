#!/usr/bin/env python3
"""데스크 메모리 write-back 헬퍼.

검수에서 기각당한 사유, 포스트모템 교훈, 확인된 패턴을 데스크 memory.md 에 append 한다.
손편집 대신 이 스크립트를 쓰는 이유는 형식 강제와 압축 시점 감지 때문이다.

사용:
    python desks/desk_memory.py append risk --kind rejected \
        --text "레짐 전환 직후 ATR 사이징 과대" --source "2026-07-20 백테스트 검수" --regime OFF
    python desks/desk_memory.py read risk
    python desks/desk_memory.py read risk --kind rejected --limit 5
    python desks/desk_memory.py compact risk
    python desks/desk_memory.py status

stdlib 만 사용한다 (cron/스케줄 태스크에서 의존성 없이 돌아야 함).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import sys
from pathlib import Path

DESKS = ("research", "strategy", "risk", "execution", "performance")

KINDS = {
    "rejected": "기각",       # 검수에서 반려된 사유 — 가장 중요
    "confirmed": "확인",      # 반복 관찰로 확인된 패턴
    "corrected": "정정",      # 과거 기록이 틀렸음이 밝혀짐
    "postmortem": "포스트모템",  # 청산 후 분석 교훈
    "hazard": "함정",         # 데이터·도구의 알려진 함정
}

# 압축 권고 임계 — A4 한 페이지 ≈ 이 정도. 넘으면 읽히지 않기 시작한다.
COMPACT_LINES = 120
COMPACT_ENTRIES = 40

ENTRY_RE = re.compile(r"^### (\d{4}-\d{2}-\d{2}) \[(\w+)\]")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def desk_dir(desk: str) -> Path:
    if desk not in DESKS:
        sys.exit(f"[desk_memory] 알 수 없는 데스크: {desk!r}. 가능: {', '.join(DESKS)}")
    return repo_root() / "desks" / desk


def memory_path(desk: str) -> Path:
    return desk_dir(desk) / "memory.md"


def today() -> str:
    return _dt.date.today().isoformat()


def cmd_append(args: argparse.Namespace) -> int:
    path = memory_path(args.desk)
    if not path.exists():
        sys.exit(f"[desk_memory] {path} 없음. desks/README.md 규약에 따라 먼저 생성할 것.")

    text = args.text.strip()
    if not text:
        sys.exit("[desk_memory] --text 가 비어 있다.")
    if len(text) < 15:
        sys.exit("[desk_memory] --text 가 너무 짧다. 나중의 자신이 읽고 행동할 수 있게 쓸 것.")

    entry = [f"\n### {today()} [{args.kind}]", "", text]
    if args.regime:
        # 레짐 태그 — 기각을 "어느 레짐에서" 내렸는지 남긴다. 레짐이 뒤집히면(ON↔OFF)
        # 과거 기각을 재시도할 가치가 있는지 사람이 read 로 훑어 판단하는 근거.
        # 본문 라인으로 저장 → ENTRY_RE(헤더) 와 (date,kind,text) 튜플이 불변, 기존 항목 무손상.
        entry.append(f"\n_레짐: {args.regime.strip()}_")
    if args.source:
        entry.append(f"\n_근거: {args.source}_")
    if args.rule:
        entry.append(f"\n**규칙: {args.rule}**")
    entry.append("")

    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n".join(entry))

    print(f"[desk_memory] {args.desk} <- [{args.kind}] {text[:60]}{'...' if len(text) > 60 else ''}")

    body = path.read_text(encoding="utf-8")
    n_lines = len(body.splitlines())
    n_entries = len(_parse_entries(body))
    if n_lines > COMPACT_LINES or n_entries > COMPACT_ENTRIES:
        print(
            f"[desk_memory] ⚠ 압축 권고: {n_entries}개 항목 / {n_lines}줄 "
            f"(임계 {COMPACT_ENTRIES}개 / {COMPACT_LINES}줄). "
            f"`python desks/desk_memory.py compact {args.desk}` 로 확인."
        )
    return 0


def _parse_entries(body: str) -> list[tuple[str, str, str]]:
    """(date, kind, text) 목록. 최신이 뒤."""
    out: list[tuple[str, str, str]] = []
    cur_date = cur_kind = None
    buf: list[str] = []
    for line in body.splitlines():
        m = ENTRY_RE.match(line)
        if m:
            if cur_date:
                out.append((cur_date, cur_kind, "\n".join(buf).strip()))
            cur_date, cur_kind = m.group(1), m.group(2)
            buf = []
        elif cur_date:
            buf.append(line)
    if cur_date:
        out.append((cur_date, cur_kind, "\n".join(buf).strip()))
    return out


def cmd_read(args: argparse.Namespace) -> int:
    path = memory_path(args.desk)
    if not path.exists():
        sys.exit(f"[desk_memory] {path} 없음.")
    entries = _parse_entries(path.read_text(encoding="utf-8"))
    if args.kind:
        entries = [e for e in entries if e[1] == args.kind]
    if args.limit:
        entries = entries[-args.limit:]
    if not entries:
        print(f"[desk_memory] {args.desk}: 해당 항목 없음.")
        return 0
    for date, kind, text in entries:
        print(f"### {date} [{kind}]\n{text}\n")
    return 0


def cmd_compact(args: argparse.Namespace) -> int:
    path = memory_path(args.desk)
    if not path.exists():
        sys.exit(f"[desk_memory] {path} 없음.")
    body = path.read_text(encoding="utf-8")
    entries = _parse_entries(body)
    n_lines = len(body.splitlines())

    print(f"[desk_memory] {args.desk}: {len(entries)}개 항목 / {n_lines}줄")
    if len(entries) <= COMPACT_ENTRIES and n_lines <= COMPACT_LINES:
        print("[desk_memory] 압축 불필요.")
        return 0

    by_kind: dict[str, int] = {}
    for _, kind, _ in entries:
        by_kind[kind] = by_kind.get(kind, 0) + 1
    print("\n종류별:")
    for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:12s} {n:3d}  ({KINDS.get(kind, '?')})")

    cutoff = (_dt.date.today() - _dt.timedelta(days=180)).isoformat()
    old = [e for e in entries if e[0] < cutoff]
    print(f"\n180일 경과 항목: {len(old)}개")
    print(
        "\n압축 지침:\n"
        "  1. corrected 로 반박된 원본 항목 → 둘을 한 줄로 병합\n"
        "  2. 같은 규칙을 반복 진술한 항목 → 가장 명료한 하나만 남김\n"
        "  3. rejected 중 이미 코드/한도로 강제된 것 → HOUSE.md 로 승격 후 제거\n"
        "  4. 180일 경과 + 이후 재발 없음 → 삭제 후보\n"
        "\n압축은 사람이 검토 후 직접 수행한다. 이 스크립트는 파일을 고치지 않는다."
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    print(f"{'데스크':<14} {'항목':>5} {'줄':>6}  최근 기록")
    print("-" * 62)
    for desk in DESKS:
        path = memory_path(desk)
        if not path.exists():
            print(f"{desk:<14} {'-':>5} {'-':>6}  (memory.md 없음)")
            continue
        body = path.read_text(encoding="utf-8")
        entries = _parse_entries(body)
        last = entries[-1][0] if entries else "없음"
        flag = ""
        if len(entries) > COMPACT_ENTRIES or len(body.splitlines()) > COMPACT_LINES:
            flag = "  ⚠압축권고"
        print(f"{desk:<14} {len(entries):>5} {len(body.splitlines()):>6}  {last}{flag}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="데스크 메모리 write-back 헬퍼")
    sub = p.add_subparsers(dest="cmd", required=True)

    ap = sub.add_parser("append", help="교훈 1건 추가")
    ap.add_argument("desk", choices=DESKS)
    ap.add_argument("--kind", required=True, choices=sorted(KINDS))
    ap.add_argument("--text", required=True, help="무엇을 배웠는가 (행동 가능하게)")
    ap.add_argument("--source", help="근거 — 날짜, 트레이드 ID, 백테스트 런")
    ap.add_argument("--rule", help="이 교훈에서 도출된 검증 가능한 규칙 한 줄")
    ap.add_argument("--regime", help="관측 시점 레짐 — 예: 'ON'/'OFF'(SPY vs 200MA) 또는 macro-regime-detector 상태. "
                                     "레짐 전환 시 과거 기각 재검토 근거")
    ap.set_defaults(func=cmd_append)

    rp = sub.add_parser("read", help="메모리 읽기")
    rp.add_argument("desk", choices=DESKS)
    rp.add_argument("--kind", choices=sorted(KINDS))
    rp.add_argument("--limit", type=int)
    rp.set_defaults(func=cmd_read)

    cp = sub.add_parser("compact", help="압축 필요 여부 진단")
    cp.add_argument("desk", choices=DESKS)
    cp.set_defaults(func=cmd_compact)

    sp = sub.add_parser("status", help="전 데스크 현황")
    sp.set_defaults(func=cmd_status)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
