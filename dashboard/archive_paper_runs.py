"""runs.jsonl 에서 paper(테스트·시뮬) 레코드를 runs.archive.jsonl 로 이관 — 실거래(권위 브로커)만 남김.

권위 브로커 = runs.jsonl 마지막 account.equity 보유 레코드의 broker (대시보드 포트폴리오와 동일 기준).
그 브로커 레코드만 runs.jsonl 에 유지, 나머지(=테스트)는 archive 로 이동. 대시보드 test 섹션이
archive 도 읽으므로 이관해도 테스트 거래는 계속 보임. 실행 전 runs.jsonl.bak 백업. 멱등.

용법:
  python dashboard/archive_paper_runs.py --dry     # 미리보기(파일 변경 없음)
  python dashboard/archive_paper_runs.py           # 실제 이관
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))   # 프로젝트 모듈
sys.path.insert(0, HERE)                      # build_data

import build_data                              # noqa: E402
from paths import LOG_DIR                      # noqa: E402


def main():
    dry = "--dry" in sys.argv
    runs = os.path.join(str(LOG_DIR), "runs.jsonl")
    arch = os.path.join(str(LOG_DIR), "runs.archive.jsonl")
    if not os.path.exists(runs):
        print("runs.jsonl 없음 — 할 것 없음")
        return
    # 장중에는 변경 거부 — 분당 intraday snapshot 의 무락 runs.jsonl append 와 RMW 가 lost-update 경합.
    # (--dry 미리보기는 무변경이라 허용.) 장마감 후 수동 실행이 운영 기본.
    from run_intraday import market_is_open
    if not dry and market_is_open():
        print("미 정규장 중 — archive 보류(장중 snapshot append 경합 방지). 장마감 후 재실행.")
        return
    # runs.jsonl read-modify-write 전체를 RunLock(전역 STATE_DIR/run.lock)으로 직렬화 — run_live._journal
    # 의 동시 append 가 read→replace 창에서 lost-update 로 소실되던 것 차단(같은 락 도메인).
    from paths import atomic_replace
    from broker.guardrail import RunLock, LockBusy
    try:
        lock_cm = RunLock()
        lock_cm.__enter__()
    except LockBusy as e:
        print(f"다른 실행이 runs.jsonl 락 보유 중 — archive 보류(재실행): {e}")
        return
    try:
        # 권위브로커 산정도 락 안에서 — 락 밖이면 그 사이 append 로 권위 레코드가 바뀌어 오분류 이관(TOCTOU).
        bk = build_data._authoritative_broker()
        if bk is None:
            print("권위 브로커 판정 불가(account 보유 레코드 없음) — 중단(안전)")
            return
        keep, move = [], []
        for ln in open(runs, encoding="utf-8").read().splitlines():
            s = ln.strip()
            if not s:
                continue
            try:
                r = json.loads(s)
            except Exception:
                keep.append(s)        # 파싱불가 → 보존(데이터 유실 방지)
                continue
            (keep if r.get("broker") == bk else move).append(s)
        print(f"권위 브로커={bk} · 유지 {len(keep)} · 이관(테스트) {len(move)}")
        if not move:
            print("이관할 테스트 레코드 없음 — 이미 깨끗")
            return
        if dry:
            print("--dry — 파일 변경 안 함")
            return
        shutil.copy2(runs, runs + ".bak")
        # 멱등·원자 — 비원자 append→truncate 는 크래시 시 archive 중복(이중집계)·runs truncate 손상.
        # (1) 기존 archive 와 합쳐 정확일치 중복 제거 후 tmp→atomic_replace, (2) runs(keep)도 tmp→atomic_replace.
        existing = []
        if os.path.exists(arch):
            existing = [l for l in open(arch, encoding="utf-8").read().splitlines() if l.strip()]
        seen = set(existing)
        merged = existing + [l for l in move if l not in seen]   # 이미 아카이브된 라인 재append 방지(crash 후 재실행 멱등)
        arch_tmp = f"{arch}.{os.getpid()}.tmp"
        with open(arch_tmp, "w", encoding="utf-8") as f:
            for ln in merged:
                f.write(ln + "\n")
        if not atomic_replace(arch_tmp, arch):
            print(f"아카이브 교체 실패 — 중단(runs 미변경, 재실행 가능): {arch}")
            return
        runs_tmp = f"{runs}.{os.getpid()}.tmp"
        with open(runs_tmp, "w", encoding="utf-8") as f:
            for ln in keep:
                f.write(ln + "\n")
        if not atomic_replace(runs_tmp, runs):
            print(f"⚠ runs 교체 실패 — archive 는 갱신됨(dedup 으로 재실행 안전), runs 미정리. 재실행 권장: {runs}")
            return
    finally:
        lock_cm.__exit__(None, None, None)
    print(f"완료 — 백업: {runs}.bak · 아카이브 += {len(merged) - len(existing)}건 → {arch}")


if __name__ == "__main__":
    main()
