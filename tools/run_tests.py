"""배포 게이트용 테스트 러너 — pytest 불필요.

test_suites.SUITES 의 모든 스위트 main() 을 실행. 하나라도 0 아니면 비0 반환.
pytest 가 없는 환경(VM·기본 인터프리터)에서도 동일하게 동작하도록 직접 호출.
"""
import importlib
import os
import sys
from pathlib import Path

# 헤드리스(SSH/스케줄드) 컨텍스트의 stdout 은 cp1252 로 잡혀 스위트가 찍는 한글·✓ 출력이
# UnicodeEncodeError 로 크래시 → 배포 게이트 오탐(실패 아닌데 exit 1). 러너 진입점에서
# UTF-8 강제 — 모든 스위트·호출자(vm_autopull 게이트·deploy_push·수동) 공통 보호.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 배포 게이트는 TELEGRAM_*/SLACK_* 가 설정된 무인 VM(autopull 테스트 게이트·deploy_push)에서 돈다.
# 스위트의 킬스위치 트립·패닉청산 픽스처가 그대로 실제 알림을 쏴서 운영자 채널을 오염시켰다
# (2026-08-08 12:35 UTC: 페르소나 "t" 로 🛑 "장중 루프 진입 정지" 2건 — 실제 정지와 구분 불가).
# 러너에서 채널을 끈다(notify.py 의 USTRADE_NOTIFY_OFF 가드). 명시 설정이 있어도 덮어쓴다 —
# 게이트는 어떤 경우에도 사람을 호출하지 않는다.
os.environ["USTRADE_NOTIFY_OFF"] = "1"

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))
sys.path.insert(0, str(_root / "tests"))   # 스위트는 tests/ 로 이동 — bare 모듈명 import 해결

# 배포 게이트 = 실거래로 나가는 코드의 안전 스위트만. stage1~8 은 백테스트/리서치
# 엔진(vectorbt 등 VM 에 없는 무거운 의존성) 테스트라 라이브 배포와 무관 → 제외.
# 실거래 경로(브로커·가드레일·청산·패닉·재귀검증·시그널) 만 게이트.
SUITES = [
    "tests_managed",    # 보호종목 슬리브 (기존 11종목 불가침)
    "tests_toss",       # 토스 브로커
    "tests_exit",       # 장중 청산
    "tests_hardening",  # 킬스위치/런락/가드레일
    "tests_panic",      # 비상 청산
    "tests_review",     # 재귀검증·자동튜닝
    "tests_personas",   # 모의매매 페르소나(버핏/우드 스크린·책 영속·디스패치)
    "tests_intraday",   # 장중 액티브 트레이딩(호가전용클라·바합성·룰·가드·배선·대시보드)
    "tests_selection_review",  # 페르소나 비교 selection_review(관찰 리포트)
    "tests_canslim",    # canslim 시그널 (A엔진 의존)
    "tests_dividends",  # 배당 입금(§B 총수익 회계) — 기본 paper 북 fail-open/멱등
]

# CI(GitHub Actions Linux)엔 A엔진(C:\텔레그램_시그널_알리미)이 없어 canslim import 불가
# → USTRADE_CI=1 이면 제외. 로컬/VM 게이트는 전체(canslim 포함) 유지.
if os.environ.get("USTRADE_CI"):
    SUITES = [s for s in SUITES if s != "tests_canslim"]


def main():
    failed = []
    for name in SUITES:
        mod = importlib.import_module(name)
        if mod.main() != 0:
            failed.append(name)
    print("=" * 60)
    if failed:
        print("FAIL:", ", ".join(failed))
        return 1
    print(f"ALL {len(SUITES)} SUITES PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
