"""상시 스케줄 루프 (대안). ⚠️ 운영 권장은 cron/Task Scheduler + run_live.py 원샷.

원샷이 더 견고 (크래시해도 OS가 다음 시각 재실행, 메모리 누수 없음).
이 루프는 개발/단순배포용. 설치: pip install schedule

매일 RUN_TIME(호스트 로컬)에 깨어나 — 그 시점 ET 기준 NYSE 세션일이면 run_live.run() 1회.
거래 대상 세션·DST·공휴일은 run_live(=last_completed_session) 가 처리하므로, RUN_TIME 은
"미장 마감 이후"이기만 하면 됨. 중복 실행은 멱등성 락(C2)이 차단.

  python scheduler.py
"""
import os
import time

import run_live
from calendar_util import now_et, is_session

RUN_TIME = os.environ.get("RUN_TIME", "06:10")   # 호스트 로컬, 미장 마감(미 동부 16:00) 후


def job():
    et = now_et()
    if not is_session(et.date()):    # 주말·공휴일 NYSE 휴장 → 스킵
        print(f"[{et:%Y-%m-%d %H:%M} ET] 휴장 — 스킵")
        return
    print(f"[{et:%Y-%m-%d %H:%M} ET] 스케줄 실행")
    run_live.run()


def main():
    try:
        import schedule
    except ImportError:
        print("schedule 미설치 → pip install schedule")
        return
    schedule.every().day.at(RUN_TIME).do(job)
    print(f"스케줄러 시작 — 매일 {RUN_TIME}(로컬) 기상, NYSE 세션일만 실행. Ctrl+C 종료.")
    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
