"""토스 관리 슬리브 설정 — 현재 보유종목을 '보호(protected)'로 스냅샷한다.

단일 토스 계좌를 기존 포트폴리오와 자동매매가 공유할 때, 자동매매가 *자기가 산 종목만*
다루고 기존 보유분은 절대 건드리지 않도록 경계를 만든다. 이 스크립트가 그 경계(슬리브)를 만든다.

동작(계좌는 읽기 전용 — 주문 없음):
  1. 토스 connect → 현재 US 보유종목 조회
  2. protected = 현재 보유종목 − managed(자동매매가 이미 산 것은 보호하지 않음, 계속 관리)
  3. state/toss_sleeve.json 저장 (managed 는 보존; --reset 시 비움)

사용 순서:
  - 자동매매에 자금을 떼어주기로 한 만큼 기존 종목을 일부 매도해 현금을 만든 뒤,
  - 이 스크립트를 실행해 '남은 보유종목 전부'를 protected 로 고정한다.
  - 이후 run_live --broker toss 는 protected 를 절대 매매하지 않고 새 현금으로만 거래한다.

실행:  & $py toss_setup.py            # 보유 스냅샷 → protected (managed 보존)
       & $py toss_setup.py --reset    # managed 도 비움(완전 초기화)
"""
import argparse
import datetime
import sys

from broker.toss import TossBroker, TossAPIError
from broker.managed import load_sleeve, save_sleeve, _norm
from paths import STATE_DIR

SLEEVE_PATH = STATE_DIR / "toss_sleeve.json"


def main():
    ap = argparse.ArgumentParser(description="토스 관리 슬리브 설정(보유 스냅샷)")
    ap.add_argument("--reset", action="store_true", help="managed 도 비움(완전 초기화)")
    a = ap.parse_args()

    b = TossBroker(paper=False)
    if not (b.api_key and b.api_secret):
        print("✗ TOSS_API_KEY / TOSS_API_SECRET 미설정")
        return 2
    try:
        b.connect()
    except TossAPIError as e:
        print(f"✗ 토스 연결 실패: {e}")
        return 1

    # 기존 슬리브의 managed(basis)·pending(미확정 매수) 보존(없거나 --reset 이면 빈 dict)
    existing_managed, existing_pending = {}, {}
    if SLEEVE_PATH.exists() and not a.reset:
        s = load_sleeve(SLEEVE_PATH)
        existing_managed, existing_pending = s["managed"], s["pending"]

    holdings = {}                                          # 정규화 심볼 → 현재 보유수량
    for p in b.get_positions():                            # US 보유종목 (TossBroker 가 US-only)
        holdings[_norm(p.symbol)] = holdings.get(_norm(p.symbol), 0.0) + p.qty

    # managed: 여전히 보유 중인 자동매매 분만, basis 를 실제 보유수량으로 클램프(스테일 제거)
    managed = {s: min(basis, holdings[s]) for s, basis in existing_managed.items()
               if s in holdings and basis > 0}
    # pending(미확정 매수)도 봇 소유로 간주 → protected 제외(고아 매수분 자본 동결 방지). 보존.
    pending = {s: q for s, q in existing_pending.items() if q > 0}
    # protected: 봇 소유(managed ∪ pending) 제외한 모든 현재 보유종목 → 재매수분도 항상 보호됨
    protected = set(holdings) - set(managed) - set(pending)

    save_sleeve(str(SLEEVE_PATH), protected, managed, pending)

    print(f"✓ 슬리브 저장: {SLEEVE_PATH}")
    print(f"  스냅샷 시각: {datetime.datetime.now().isoformat(timespec='seconds')}")
    print(f"  보호(protected) {len(protected)}종목 — 자동매매가 절대 매매 안 함:")
    print("    " + (", ".join(sorted(protected)) if protected else "(없음)"))
    print(f"  관리(managed) {len(managed)}종목 — 자동매매가 관리(심볼:수량):")
    print("    " + (", ".join(f"{s}:{q:g}" for s, q in sorted(managed.items()))
                    if managed else "(없음 — 첫 거래부터 채워짐)"))
    if pending:
        print(f"  미확정 매수(pending) {len(pending)}종목 — 다음 실행서 실보유 대조 후 흡수:")
        print("    " + ", ".join(f"{s}:{q:g}" for s, q in sorted(pending.items())))
    print("\n이제 run_live --broker toss 는 protected 를 건드리지 않고 새 현금으로만 거래한다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
