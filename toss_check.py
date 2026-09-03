"""토스 연결 점검 — 읽기 전용(주문 일절 없음). 실거래 전 키·계좌·시세 확인용.

connect → 계좌(accountSeq) → 매수가능금액/평가액 → 보유종목(US) → 샘플 시세 만 호출한다.
place_order/cancel 은 호출하지 않으므로 안전하다.

설정:  setx TOSS_API_KEY "..."   /   setx TOSS_API_SECRET "..."   (새 셸에서 적용)
실행:  & $py toss_check.py [SYMBOL]      # SYMBOL 기본 AAPL
"""
import sys

from broker.toss import TossBroker, TossAPIError


def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    b = TossBroker(paper=False)
    if not (b.api_key and b.api_secret):
        print("✗ TOSS_API_KEY / TOSS_API_SECRET 환경변수 미설정 — 설정 후 새 셸에서 재실행")
        return 2
    try:
        b.connect()
        _acct_masked = ("****" + str(b._account_no)[-4:]) if b._account_no else "(미상)"
        print(f"✓ 연결 성공 — 계좌 {_acct_masked} (accountSeq={b._account_seq})")

        acct = b.get_account()
        print(f"  계좌 ({b.currency}): cash {acct.cash:,.2f} | equity {acct.equity:,.2f} | "
              f"buying_power {acct.buying_power:,.2f}")

        pos = b.get_positions()
        print(f"  보유 US 종목 {len(pos)}개:")
        for p in pos:
            print(f"    {p.symbol}: {p.qty} @ {p.avg_price}")

        q = b.get_quote(symbol)
        print(f"  시세 {symbol}: last {q.last}")

        print("\n읽기 전용 점검 완료 — 주문은 일절 보내지 않았습니다.")
        return 0
    except TossAPIError as e:
        print(f"✗ 토스 API 오류: {e}")
        return 1
    except Exception as e:
        print(f"✗ 예외: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
