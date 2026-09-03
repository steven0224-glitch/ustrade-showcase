"""알림 스텁 — 텔레그램/슬랙. 미설정 시 콘솔+로그만. 절대 예외 안 던짐(알림이 거래 못 막게).

설정 (환경변수):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  SLACK_WEBHOOK_URL
"""
import os
import sys

from logsetup import get_logger
from paths import STATE_DIR

_log = get_logger("notify")

ICONS = {"info": "ℹ️", "ok": "✅", "warn": "⚠️", "halt": "🛑", "error": "❌"}

# 채널 설정됐는데 전송 0건(토큰만료·웹훅폐기·네트워크) = 무성 채널사망. 회전로그 너머의
# notify-독립 신호: 영속 flag 파일(heartbeat·대시보드가 능동 점검) + stderr(cron MAILTO).
_NOTIFY_FAIL_FLAG = STATE_DIR / "notify_fail.flag"


def _mark_channel_fail(line: str):
    """채널 설정됨+전송0건 → 독립 신호 2개 남김(둘 다 예외 삼킴 — 알림이 거래 못 막게).
    (1) stderr: 알림채널과 독립된 경로(cron MAILTO 가 캡처). (2) 영속 flag: heartbeat 가 매 틱
    점검해 '채널 사망'을 dead-man 으로 능동 표면화 — 백스톱이 같은 죽은 notify() 를 공유하는 SPOF 보강."""
    try:
        sys.stderr.write("NOTIFY-FAIL 알림 채널 미전달(토큰/네트워크 확인): " + line + "\n")
    except Exception:
        pass
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _NOTIFY_FAIL_FLAG.write_text(line, encoding="utf-8")
    except Exception:
        pass


def _clear_channel_fail():
    """전송 성공 or 채널 미설정 → 채널사망 상태 아님. flag 자가치유 제거(영구 stale 경보 방지)."""
    try:
        if _NOTIFY_FAIL_FLAG.exists():
            _NOTIFY_FAIL_FLAG.unlink()
    except Exception:
        pass


def has_channel() -> bool:
    """실거래용 알림 채널(텔레그램 or 슬랙)이 하나라도 설정됐는지. 무인 실거래 전제조건."""
    tg = bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))
    return tg or bool(os.environ.get("SLACK_WEBHOOK_URL"))


def _telegram(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        import requests
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text}, timeout=10)
        return bool(r.ok)   # HTTP 2xx 만 성공 — 401(토큰만료)·400(chat 오류)을 성공으로 오인하지 않음
    except Exception:
        return False


def _slack(text: str) -> bool:
    url = os.environ.get("SLACK_WEBHOOK_URL")
    if not url:
        return False
    try:
        import requests
        r = requests.post(url, json={"text": text}, timeout=10)
        return bool(r.ok)   # HTTP 2xx 만 성공 — 전송 실패를 성공으로 오인하지 않음
    except Exception:
        return False


def notify(message: str, level: str = "info", stamp: str = ""):
    """알림 발송 (설정된 채널) + 항상 로그. 예외 삼킴."""
    line = f"{ICONS.get(level, '')} [{stamp or 'now'}] {message}"
    # 채널 킬스위치 — 테스트·리허설용. 전송만 끊고 로그는 남긴다.
    # 왜: 테스트 스위트엔 킬스위치 트립·패닉청산 픽스처가 있고 notify 는 모듈 전역 env 를 본다.
    # TELEGRAM_* 가 설정된 무인 VM 에서 배포 게이트(tools/run_tests.py)가 돌면 그 픽스처가 *진짜*
    # 🛑 halt 알림을 쐈다 — 2026-08-08 12:35 UTC autopull 게이트가 존재하지 않는 페르소나 "t" 로
    # "장중 루프 진입 정지" 2건 발송, 운영자가 실제 정지와 구분 불가. 러너가 이 플래그를 세운다.
    # 채널사망 flag 로직도 건너뛴다 — 알림 끈 런이 실제 채널사망 신호를 지워선 안 된다.
    if os.environ.get("USTRADE_NOTIFY_OFF") == "1":
        try:
            _log.info(line + " (NOTIFY_OFF — 채널 미전송, 로그만)")
        except Exception:
            pass
        return
    sent = []
    if _telegram(line):
        sent.append("tg")
    if _slack(line):
        sent.append("slack")
    if sent:
        chan = f" → {','.join(sent)}"
        _clear_channel_fail()                  # 채널 회복 → 채널사망 flag 자가치유
    elif has_channel():
        # 채널은 설정됐는데 전송 0건 = 미전달(토큰만료·네트워크). '미설정'과 구분해 로그에 경고로 남기고,
        # notify-독립 신호(flag + stderr)도 남김 — heartbeat 가 이를 능동 점검해 SPOF(채널死 시 백스톱까지
        # 같은 죽은 notify() 로 무성화)를 보강한다.
        chan = " ⚠️ 알림 전송 실패(채널 설정됨, 미전달 — 토큰/네트워크 확인)"
        _mark_channel_fail(line)
    else:
        chan = " (채널 미설정 — 로그만)"
        _clear_channel_fail()                  # 미설정 = 실패상태 아님 → stale flag 제거
    # 회전 로그(ustrade.log) + 콘솔. 레벨 매핑. 예외 삼킴(알림이 거래 못 막게).
    try:
        lvl = {"error": 40, "halt": 40, "warn": 30}.get(level, 20)
        _log.log(lvl, line + chan)
    except Exception:
        pass
