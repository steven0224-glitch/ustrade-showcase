"""startup.py — 기동 지터(thundering-herd 분산).

setup_paper_tasks.ps1 이 페르소나 3종(buffett/wood/oneil)을 동일 Task Scheduler 트리거
(ustrade-entry)로 등록 → 매 세션 run_live.py 3개가 같은 순간 발사된다. 별 home·별 락이라
파일 경합은 이미 없지만, cold cache 면 셋 다 같은 찰나에 yfinance·FMP·공유캐시(data_cache/
fmp_cache 의 per-pid tmp atomic-replace)를 강타한다 — 제공자 레이트(402)·디스크 버스트.

첫 데이터 fetch 전 0~N초 균등 랜덤 슬립으로 셋을 시간축에 흩뿌린다. run_intraday 는 단일인스턴스
락 + 스케줄러 RandomDelay(setup_intraday.ps1)로 이미 분산돼 있어 불필요 → run_live.main() 에서만
호출한다(dashboard server.api_run 의 run() 직접호출 경로는 main 을 안 거쳐 무영향).

knobs:
  env USTRADE_STARTUP_JITTER_SEC — 최대 지터 초(기본 30, 0=비활성).
  대화형 실행(stdin 이 tty: 개발자가 터미널서 수동 run)은 자동 skip — 스케줄러(NonInteractive,
  tty 없음)에서만 지터. 수동 디버깅이 매번 0~30초 기다리는 footgun 제거.
"""
import os
import random
import sys
import time


def startup_jitter(max_seconds: float = None, *, interactive: bool = None,
                   sleep_fn=time.sleep, rand_fn=random.random) -> float:
    """0~max 균등 랜덤 슬립. 반환=실제 슬립 초(0=슬립 안 함).

    대화형(stdin tty)·max<=0 이면 0. max_seconds None → env USTRADE_STARTUP_JITTER_SEC(기본 30).
    interactive·sleep_fn·rand_fn 은 결정론 테스트용 주입구."""
    if interactive is None:
        try:
            interactive = sys.stdin.isatty()
        except Exception:
            interactive = False
    if interactive:
        return 0.0
    if max_seconds is None:
        try:
            max_seconds = float(os.environ.get("USTRADE_STARTUP_JITTER_SEC", "30"))
        except ValueError:
            max_seconds = 30.0
    if max_seconds <= 0:
        return 0.0
    delay = rand_fn() * max_seconds
    sleep_fn(delay)
    return delay
