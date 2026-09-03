"""런타임 데이터 경로 — 상태·로그·캐시를 동기화 폴더(OneDrive 등) 밖에 둔다.

OneDrive 동기화 경로에 킬스위치 상태/로그를 두면 충돌본·롤백으로 멱등락·손실 baseline 이
손상돼 더블트레이드/정지실패 위험이 있고, 로그(=계좌 PII)가 클라우드로 동기화된다.
→ 기본을 로컬 비동기 경로로, 환경변수 USTRADE_HOME 으로 재정의 가능.

  USTRADE_HOME 미설정 시 기본:
    Windows : %LOCALAPPDATA%\\ustrade
    기타     : ~/.local/state/ustrade
  기존 캐시를 유지하려면 USTRADE_HOME 을 프로젝트 폴더로 지정.
"""
import os
from pathlib import Path


def base_dir() -> Path:
    env = os.environ.get("USTRADE_HOME")
    if env:
        return Path(env)
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return Path(root) / "ustrade"
    return Path(os.path.expanduser("~")) / ".local" / "state" / "ustrade"


BASE = base_dir()
STATE_DIR = BASE / "state"        # killswitch.json, HALT, run.lock
LOG_DIR = BASE / "logs"           # runs.jsonl, alerts.log


def cache_base() -> Path:
    """캐시(가격·FMP) 루트 — USTRADE_CACHE_HOME 으로 state/logs 와 분리 가능.
    여러 페르소나(별 USTRADE_HOME)가 한 캐시를 공유 → 동일 유니버스 중복 다운로드·FMP 레이트 절감."""
    env = os.environ.get("USTRADE_CACHE_HOME")
    return Path(env) if env else BASE


CACHE_BASE = cache_base()
DATA_CACHE = CACHE_BASE / "data_cache"  # yfinance CSV 캐시 (USTRADE_CACHE_HOME 로 공유 가능)
FMP_CACHE = CACHE_BASE / "fmp_cache"    # FMP 응답 캐시 (USTRADE_CACHE_HOME 로 공유 가능)


def persona_homes() -> list:
    """모의매매 페르소나 격리 home 목록 — USTRADE_PERSONA_HOMES(';' 구분 절대경로,
    setup_paper_tasks/setup_intraday 가 머신 env 로 설정)의 정규 파서.
    heartbeat/review/대시보드/mcp 등이 각자 파싱하던 것을 단일화(오타·공백 처리 일관)."""
    return [Path(h.strip()) for h in os.environ.get("USTRADE_PERSONA_HOMES", "").split(";")
            if h.strip()]


def atomic_replace(tmp, dst, retries: int = 5) -> bool:
    """tmp → dst 원자 교체. Windows 동시접근(공유 캐시: peer 가 dst 를 read 로 열고있음) PermissionError 재시도.
    최종 실패 시 tmp 정리하고 False(호출측은 이번 캐시 미기록 — 다음 실행이 재생성). 성공 True. per-pid tmp 와 짝."""
    import time
    for i in range(retries):
        try:
            os.replace(tmp, dst)
            return True
        except PermissionError:
            if i < retries - 1:
                time.sleep(0.05 * (i + 1))
        except OSError:
            break
    try:
        os.remove(tmp)   # 교체 실패 → orphan tmp 정리
    except OSError:
        pass
    return False


def append_jsonl_rotating(path, rec: dict, max_bytes: int = 5_000_000, retries: int = 5,
                          backups: int = 1) -> None:
    """JSONL 한 줄 append + max_bytes 초과 시 .1 로 회전(runs/exits/panics/volume_shadow 공통 패턴,
    기존 4벌 복붙을 단일화). Windows 동시접근 PermissionError 재시도는 atomic_replace 와 동일한
    짧은 backoff 를 회전·append 양쪽에 적용. append 재시도 소진 시 마지막 예외를 그대로 던진다 —
    호출측이 저널 실패를 감지해 notify 등으로 표면화할 수 있게(무성 실패 방지).

    backups=N (기본 1) : .1 만 두던 것을 .1..N 다중 백업으로 확장. 회전 시 .k→.(k+1) 로 밀어
    가장 오래된 .N 을 버린다 → 최대 (N+1)×max_bytes 보존. volume_shadow(관찰 데이터) 처럼 히스토리를
    누적해야 하는 저널이 단일 백업 회전으로 조용히 소실되는 것을 막는다. backups=1 이면 기존 동작 불변."""
    import json
    import time
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        for k in range(backups - 1, 0, -1):          # .k → .(k+1), 오래된 것부터(backups>1 일 때만 실행)
            src = path.with_name(f"{path.name}.{k}")
            if src.exists():
                try:
                    src.replace(path.with_name(f"{path.name}.{k + 1}"))
                except OSError:
                    pass
        bak = path.with_name(path.name + ".1")
        for i in range(retries):
            try:
                path.replace(bak)
                break
            except PermissionError:
                if i < retries - 1:
                    time.sleep(0.05 * (i + 1))
            except OSError:
                break
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    for i in range(retries):
        try:
            with path.open("a", encoding="utf-8") as fp:
                fp.write(line)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(0.05 * (i + 1))
