"""heartbeat.py — dead-man's switch. 예정된 실행이 누락됐는지 감지.

live 모드(기본): run_live 가 매 실행마다 logs/runs.jsonl 에 session(거래세션 날짜)을
기록한다. 이 스크립트를 더 잦은 주기(예: 매시간)로 cron 에 걸면 — 직전 종료 NYSE 세션에
대한 실행 기록이 없고 마감 후 유예시간이 지났을 때 알림을 보낸다. cron 정지·DST 어긋남·
리부트로 트레이더가 조용히 멈춘 상황을 감지하는 용도 (정상 동작은 run_live 자체가 알림).

paper 모드: live 태스크가 꺼진 paper 체제 전용 감시. live 체크(일일진입·청산 cron)는
매시간 오경보만 내므로 끄고, 대신 USTRADE_PERSONA_HOMES 의 각 페르소나 home 을 감시 —
(a) 일1런 신선도: 저널 마지막 session 과 직전종료 세션의 거래일 갭 ≥ 2 면 태스크 사망.
    페르소나 일1런은 '다음 거래일 아침'에 직전 세션을 기록하므로 마감+유예 방식은 매일 밤·
    주말이 전부 오경보 — 세션 갭 방식이 아침실행·주말·공휴일을 전부 무경보로 통과시킨다.
(b) 장중 루프 생존: 장중(개장+N분 후)인데 마지막 intraday 스냅샷이 N분 넘게 stale.
(c) 대시보드 생존: USTRADE_DASH_URL(기본 http://127.0.0.1:8765/api/health) 200 응답.
notify 채널사(死) flag 감시는 두 모드 공통.

  python heartbeat.py                # live 모드, 기본 유예 6시간
  python heartbeat.py --mode paper   # paper 체제 감시 (VM ustrade-heartbeat 태스크용)
exit code: 0 = 정상, 1 = 이상 감지(알림 발송)
"""
import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import pandas as pd

from paths import LOG_DIR, STATE_DIR, persona_homes as _persona_homes
from calendar_util import (last_completed_session, now_et, ET, _NYSE, minutes_since_open,
                           session_gap)
from notify import notify

# SSH/스케줄드 컨텍스트의 stdout 이 cp1252 면 한글·— print 가 UnicodeEncodeError 로 크래시
# → check() 래퍼가 가짜 '자체 오류' 경보를 쏨(정상인데 exit 1). run_tests.py 와 동일 보호.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _read_jsonl_gens(path: Path) -> list:
    """path 와 path+'.1'(회전 세대) 를 합쳐 JSON 레코드 리스트로 — review.load_journals 와 동일 관례.
    .1 이 항상 더 오래된 세대라 .1→본체 순으로 이어붙이면 append 순서가 보존된다. 회전 직후(본체가
    막 비었는데 .1 만 과거기록을 쥔 순간)에도 마지막 기록을 못 읽어 dead-man 이 오탐하는 것 방지."""
    out = []
    for p in (Path(str(path) + ".1"), path):
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def _traded_sessions() -> set:
    out = set()
    for rec in _read_jsonl_gens(LOG_DIR / "runs.jsonl"):
        if rec.get("session") and not rec.get("intraday"):   # 장중 snapshot(intraday=True, session=당일 ET)은 제외 —
            out.add(rec["session"])                          # 일1런(직전종료세션) dead-man 게이트를 마스킹하지 않게
    return out


def _exit_run_age_min():
    """logs/exits.jsonl 마지막 기록(run_exit ts, 호스트 로컬)으로부터 경과 분. 없으면 None.

    ts·now 둘 다 호스트 로컬(naive)이라 TZ 무관하게 일관 — 청산 cron 생존 감시용.
    """
    last = None
    for rec in _read_jsonl_gens(LOG_DIR / "exits.jsonl"):
        if rec.get("ts"):
            last = rec["ts"]
    if not last:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 60.0
    except Exception:
        return None


def _scan_runs(home: Path):
    """home/logs/runs.jsonl(+.1) 훑어 (마지막 일1런 session, 마지막 intraday ts, 저널존재) 반환.

    파일이 append-only 라 마지막에 본 값이 최신. intraday=True 스냅샷과 일1런 레코드가
    같은 파일을 공유(oneil/wood)하거나 한쪽만 있는 home(buffett=일1런만, livermore=장중만)
    모두 이 한 함수로 커버 — 페르소나 이름 하드코딩 없이 데이터로 분류.
    """
    f = home / "logs" / "runs.jsonl"
    if not f.exists() and not Path(str(f) + ".1").exists():
        return None, None, False
    daily_sess = intra_ts = None
    for rec in _read_jsonl_gens(f):
        if rec.get("intraday"):
            intra_ts = rec.get("ts") or intra_ts
        elif rec.get("session"):
            daily_sess = rec["session"]
    return daily_sess, intra_ts, True


def _check_paper(intraday_stale_min: float = 15.0, daily_run_open_grace: float = 90.0) -> list:
    """paper 체제 감시 — 페르소나 home 별 (a) 일1런 신선도 (b) 장중 루프 생존 + (c) 대시보드.

    daily_run 페르소나(oneil/wood)의 장중 루프는 일1런 완료 후에야 기동(개장+~1h)하므로, (b) 무장을
    intraday_stale_min(개장+15분)이 아니라 daily_run_open_grace(개장+90분)로 늦춘다 — 개장 직후
    스냅샷 부재는 '아직 미기동'이지 사망이 아니라, 안 그러면 매 거래일 10:00 ET 틱이 오발화한다
    (heartbeat 매시 틱이 9:45~10:45 미기동 구간에 걸림, 2026-07 실증)."""
    alerts = []
    homes = _persona_homes()
    if not homes:
        alerts.append("paper 모드인데 USTRADE_PERSONA_HOMES 미설정 — 감시 대상 0 (머신 env 확인)")
    try:
        import personas                     # home.name → daily_run 여부(무장 유예 분기). import 실패는 무해(전원 15분).
        pmap = personas.PERSONAS
    except Exception:
        pmap = {}
    last_done = last_completed_session()
    since_open = minutes_since_open()
    for home in homes:
        name = home.name
        daily_sess, intra_ts, has_journal = _scan_runs(home)
        if not has_journal:
            alerts.append(f"[{name}] 저널 없음({home / 'logs' / 'runs.jsonl'}) — 태스크 미가동 의심")
            continue
        if daily_sess is None and intra_ts is None:
            alerts.append(f"[{name}] 저널이 비어있음 — 태스크 미가동 의심")
            continue
        # (a) 일1런은 '다음 거래일 아침'에 직전 세션을 기록 → 갭 1 = 정상 대기, 갭 2+ = 사망.
        if daily_sess is not None and last_done is not None:
            gap = session_gap(daily_sess, last_done)
            if gap >= 2:
                alerts.append(f"[{name}] 일1런 미실행 의심 — 마지막 기록 세션 {daily_sess}, "
                              f"직전종료 {last_done} (거래일 {gap}회 공백, 태스크 정지 의심)")
        # (b) 장중 루프 — 개장+유예 지나서도 스냅샷 stale 이면 루프 사망. daily_run 페르소나(oneil/wood)는
        #     일1런 후 기동(개장+~1h)이라 무장을 daily_run_open_grace 로 늦춰 미기동 구간 오발화 차단.
        meta = pmap.get(name.replace("ustrade-paper-", ""), {})
        open_grace = daily_run_open_grace if meta.get("daily_run") else intraday_stale_min
        if intra_ts is not None and since_open is not None and since_open >= open_grace:
            try:
                age = (datetime.now() - datetime.fromisoformat(intra_ts)).total_seconds() / 60.0
            except Exception:
                age = None
            if age is None or age > intraday_stale_min:
                shown = "파싱불가" if age is None else f"{age:.0f}분 전"
                alerts.append(f"[{name}] 장중 루프 정지 의심 — 마지막 스냅샷 {shown} "
                              f"(>{intraday_stale_min:.0f}분)")
    # (c) 대시보드 생존 — 빈 값으로 끄기 가능(대시보드 없는 호스트에서 수동 실행 시).
    url = os.environ.get("USTRADE_DASH_URL", "http://127.0.0.1:8765/api/health")
    if url:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status != 200:
                    alerts.append(f"대시보드 응답 이상 — {url} HTTP {r.status}")
        except Exception as e:
            alerts.append(f"대시보드 무응답 — {url} ({e.__class__.__name__})")
    return alerts


def _check_impl(grace_hours: float = 6.0, exit_stale_min: float = 25.0, mode: str = "live",
                intraday_stale_min: float = 15.0) -> int:
    """dead-man 점검. live: (1) 일일 진입 누락 + (2) 청산 cron 사망. paper: 페르소나/대시보드
    감시로 대체 — live 체크는 live 태스크가 꺼진 체제에선 매시간 오경보라 모드로 분리."""
    now = pd.Timestamp(now_et())
    alerts = []

    if mode == "paper":
        alerts += _check_paper(intraday_stale_min)
    else:
        # (1) 일일 진입 누락 — 직전 종료 세션 마감 + 유예 지났는데 실행 기록 없음.
        session = last_completed_session()
        if session is not None:
            sched = _NYSE.schedule(start_date=session.isoformat(), end_date=session.isoformat())
            close = sched["market_close"].iloc[-1]            # tz-aware (UTC)
            if now >= close + pd.Timedelta(hours=grace_hours) and session.isoformat() not in _traded_sessions():
                alerts.append(f"미실행 감지 — {session} 세션 리밸런스 기록 없음 (cron 정지·스케줄 이상 의심)")

        # (2) 장중 청산 cron 사망 — 장중 + 개장 후 충분히 지났는데 최근 청산 실행 기록이 stale.
        #     개장 직후(첫 cron 틱 전)·휴장은 minutes_since_open 가드로 오경보 제외.
        since_open = minutes_since_open()
        if since_open is not None and since_open >= exit_stale_min:
            age = _exit_run_age_min()
            if age is None or age > exit_stale_min:
                shown = "기록 없음" if age is None else f"{age:.0f}분 전"
                alerts.append(f"장중 청산 cron 미동작 의심 — 마지막 청산 실행 {shown} "
                              f"(>{exit_stale_min:.0f}분, cron 정지·DST 어긋남 의심)")

    # (3) 알림 채널 사망 의심 — notify 가 '채널 설정됨+전송0건' 시 남긴 영속 flag. 백스톱(heartbeat)도
    #     같은 notify() 를 쓰므로 채널이 죽으면 dead-man 경보까지 무성화된다(SPOF). flag 를 능동 점검해
    #     exit code·stdout·stderr 등 notify-독립 경로로 채널死를 표면화. 성공/미설정 notify 가 자가치유 제거.
    fail_flag = STATE_DIR / "notify_fail.flag"
    if fail_flag.exists():
        try:
            detail = fail_flag.read_text(encoding="utf-8").strip().splitlines()[-1][:160]
        except Exception:
            detail = ""
        alerts.append("알림 채널 미전달 의심 — notify 전송실패 flag 존재 "
                      f"(토큰만료·웹훅폐기 의심, 채널 점검 필요): {detail}")

    # 마지막 점검 결과를 상태파일로 — 대시보드 감시 배지가 '언제 점검했고 이상 몇 건'을 읽음.
    # best-effort: 상태파일 실패가 dead-man 본체를 못 막게.
    try:
        (STATE_DIR / "heartbeat_status.json").write_text(
            json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                        "mode": mode, "alerts": len(alerts)}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass
    for m in alerts:
        notify("⚠️ " + m, "error", now.isoformat())
        print("ALERT — " + m)
    if not alerts:
        print("OK — 진입·청산 모니터 정상")
    return 1 if alerts else 0


def check(grace_hours: float = 6.0, exit_stale_min: float = 25.0, mode: str = "live",
          intraday_stale_min: float = 15.0) -> int:
    """_check_impl 래퍼 — 데드맨 자기死 방지. check 자체가 캘린더/pandas 등에서 raise 하면
    stderr·exit code 로만 드러나 무성화될 수 있으므로, notify 로 표면화 후 실패코드(1) 반환한다."""
    try:
        return _check_impl(grace_hours, exit_stale_min, mode, intraday_stale_min)
    except Exception as e:
        try:
            notify(f"⚠️ heartbeat 자체 오류 — dead-man 점검 실패(캘린더/pandas 등): {e!r}",
                   "error", now_et().isoformat())
        except Exception:
            pass
        print(f"ERROR — heartbeat check 크래시: {e!r}", file=sys.stderr)
        return 1


def main():
    ap = argparse.ArgumentParser(description="dead-man 하트비트 — 실행 누락 감지")
    ap.add_argument("--mode", choices=("live", "paper"), default="live",
                    help="live=일일진입·청산 cron 감시(기본) / paper=페르소나 home·장중 루프·대시보드 감시")
    ap.add_argument("--grace-hours", type=float, default=6.0,
                    help="[live] 마감 후 이 시간까지는 미실행이어도 알림 안 함 (데이터 발행지연·실행여유)")
    ap.add_argument("--exit-stale-min", dest="exit_stale_min", type=float, default=25.0,
                    help="[live] 장중 청산 cron 마지막 실행이 이 분 넘게 stale 이면 cron 사망 의심 알림 (cron 15분 주기 가정)")
    ap.add_argument("--intraday-stale-min", dest="intraday_stale_min", type=float, default=15.0,
                    help="[paper] 장중인데 페르소나 스냅샷이 이 분 넘게 stale 이면 루프 사망 의심 (스냅샷 분당 가정)")
    a = ap.parse_args()
    return check(a.grace_hours, a.exit_stale_min, a.mode, a.intraday_stale_min)


if __name__ == "__main__":
    sys.exit(main())
