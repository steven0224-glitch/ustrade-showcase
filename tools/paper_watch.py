"""paper_watch.py — paper 데일리 진입(run_live, 평일 06:10 KST) 결번 감시 (DoD A2).

heartbeat.py live 모드의 '마감+유예' 방식은 주말이 통째로 오경보다(금 세션 기록은 월 06:10에야
생김). paper 모드는 페르소나 함대·대시보드 전제라 단일 홈 데일리 실험에 안 맞는다. 이 러너는
그 중간 — heartbeat 의 검증된 부품(_traded_sessions·notify)을 재사용하되 판정을
"직전 종료 세션의 **첫 실행 기회(마감 후 첫 평일 06:10 KST)+유예 50분**이 지났는데 기록 없음"
으로 바꾼다. 아침실행·주말·공휴일 전부 무경보 통과, 결번은 당일 07:00 KST에 잡힌다.

경보 채널·저널·상태 경로는 전부 본체와 동일(paths/notify). 세션당 경보 1회(마커 dedup).
스케줄: UsPaperWatch — 매시간 (pythonw, 게이트가 조용히 통과시키므로 24/7 무해).

수동 테스트: python tools/paper_watch.py --force   (게이트·dedup 무시, 마커 안 씀)
exit code: 0 = 정상, 1 = 이상 감지(알림 발송)
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 프로젝트 루트

from paths import STATE_DIR
from notify import notify

_ENTRY_HOUR, _ENTRY_MIN, _GRACE_MIN = 6, 10, 50   # 평일 06:10 KST 진입 + 유예
_MARKER = STATE_DIR / "paper_watch_alerted.txt"        # 세션당 경보 1회
_FLAG_MARKER = STATE_DIR / "paper_watch_flagday.txt"   # 채널死 경보 일1회


def _entry_deadline(session) -> datetime:
    """세션 S 의 실행 기회 마감 = S 마감(KST 익일 새벽) 후 첫 평일 06:10 + 유예.

    ET 16:00 마감 = KST 익일 05:00(EDT)/06:00(EST) → KST 달력일 S+1 부터 탐색.
    금요 세션 → 월 07:00, 평일 세션 → 익일 07:00. now 는 호스트 로컬(KST) naive.
    """
    d = session + timedelta(days=1)
    while d.weekday() >= 5:          # 토(5)·일(6) 건너뜀
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, _ENTRY_HOUR, _ENTRY_MIN) + timedelta(minutes=_GRACE_MIN)


def _check_impl(force: bool = False) -> int:
    from heartbeat import _traded_sessions          # 지연 import — 캘린더/pandas 체인
    from calendar_util import last_completed_session

    STATE_DIR.mkdir(parents=True, exist_ok=True)   # 처녀 홈(첫 가동)서 마커/status 쓰기 무음실패 방지
    # pending: (메시지, 마커파일 또는 None, 마커에 쓸 값) — 마커는 notify 발송 *후* 에 쓴다.
    # 먼저 쓰면 notify 도달 전에 프로세스가 끊길 때(크래시·강제종료) 경보가 영구 억제된다.
    pending = []
    session = last_completed_session()
    if session is not None:
        recorded = session.isoformat() in _traded_sessions()
        due = datetime.now() >= _entry_deadline(session)
        already = (not force) and _MARKER.exists() and _MARKER.read_text(encoding="utf-8").strip() == session.isoformat()
        if not recorded and (due or force) and not already:
            msg = (f"paper 미실행 감지 — {session.isoformat()} 세션 기록 없음 "
                   f"(마감 후 첫 평일 06:10+50분 경과 — UsPaperLive 태스크 정지·절전 의심)")
            pending.append((msg, None if force else _MARKER, session.isoformat()))

    # 채널死 표면화 (heartbeat 와 동일 flag, 일 1회 dedup) — 채널이 죽으면 이 경보도 무성이지만
    # status 파일·exit code 로는 남는다(대시보드/수동 확인 경로).
    fail_flag = STATE_DIR / "notify_fail.flag"
    if fail_flag.exists():
        today = datetime.now().date().isoformat()
        if force or not (_FLAG_MARKER.exists() and _FLAG_MARKER.read_text(encoding="utf-8").strip() == today):
            try:
                detail = fail_flag.read_text(encoding="utf-8").strip().splitlines()[-1][:160]
            except Exception:
                detail = ""
            msg = f"알림 채널 미전달 의심 — notify 전송실패 flag: {detail}"
            pending.append((msg, None if force else _FLAG_MARKER, today))

    try:   # 상태 파일 — best-effort (heartbeat_status.json 과 동일 계약)
        (STATE_DIR / "paper_watch_status.json").write_text(
            json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                        "session": session.isoformat() if session is not None else None,
                        "alerts": len(pending)}, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    for m, marker, value in pending:
        notify("⚠️ " + m, "error", datetime.now().isoformat(timespec="seconds"))
        print("ALERT — " + m)
        if marker is not None:
            marker.write_text(value, encoding="utf-8")   # notify 후 기록 — dedup 억제 창 최소화
    if not pending:
        print("OK — paper 데일리 진입 정상 (또는 판정 유예 내)")
    return 1 if pending else 0


def main(force: bool = False) -> int:
    """자기死 방어 — 점검 자체가 죽으면 notify 로 표면화 (heartbeat.check 와 동일 계약)."""
    try:
        return _check_impl(force)
    except Exception as e:
        try:
            notify(f"⚠️ paper_watch 자체 오류 — 결번 점검 실패: {e!r}", "error",
                   datetime.now().isoformat(timespec="seconds"))
        except Exception:
            pass
        print(f"ERROR — paper_watch 크래시: {e!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(force="--force" in sys.argv))
