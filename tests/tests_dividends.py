"""dividends.py 배당 입금(§B 총수익 회계) 검증 — 네트워크 0 (fetch 주입).

핵심 단언: (1) 첫 가동 = 마커 초기화만·소급 입금 0, (2) (마커, 세션] 창·금액·이벤트 필드,
(3) 멱등(마커 전진 후 재호출 무입금), (4) fail-open(fetch/credit 예외에도 raise 없음,
실패는 항상 미입금=과소계상 방향), (5) PaperBroker.credit_cash 입금·영속·비정상값 거부.

실행:  python tests/tests_dividends.py
"""
import json
import sys
import tempfile
from collections import namedtuple
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dividends import process_dividends
from broker import PaperBroker

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


_Pos = namedtuple("Pos", "symbol qty")


class _FakeBroker:
    def __init__(self, positions, raise_amounts=()):
        self._pos = [_Pos(s, q) for s, q in positions]
        self.credits = []
        self._raise = set(raise_amounts)   # 이 금액의 입금은 예외(부분 실패 시나리오)

    def get_positions(self):
        return list(self._pos)

    def credit_cash(self, amount):
        if round(float(amount), 2) in self._raise:
            raise RuntimeError("boom")
        self.credits.append(round(float(amount), 2))


def test_first_run_init():
    print("[DIV] 첫 가동 — 마커 초기화만, 소급 입금 0")
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "marker.txt"
        b = _FakeBroker([("AAA", 10.0)])
        ev = process_dividends(b, "2026-07-10", m,
                               fetch=lambda s, a, z: {date(2026, 7, 3): 1.0})
        check("이벤트 0", ev == [], ev)
        check("입금 0", b.credits == [], b.credits)
        check("마커 = 세션", m.read_text(encoding="utf-8").strip() == "2026-07-10")


def test_window_and_credit():
    print("[DIV] (마커, 세션] 창 — 인자·금액·이벤트 필드·멱등")
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "marker.txt"
        m.write_text("2026-07-01", encoding="utf-8")
        calls = []

        def fetch(sym, start_excl, end_incl):
            calls.append((sym, start_excl, end_incl))
            return {date(2026, 7, 3): 0.5} if sym == "AAA" else {}

        b = _FakeBroker([("AAA", 10.0), ("BBB", 3.0)])
        ev = process_dividends(b, "2026-07-10", m, fetch=fetch)
        check("AAA $5.00 입금", b.credits == [5.0], b.credits)
        check("이벤트 필드", len(ev) == 1 and ev[0]["symbol"] == "AAA"
              and ev[0]["ex_date"] == "2026-07-03" and ev[0]["amount"] == 5.0
              and ev[0]["qty"] == 10.0, ev)
        check("fetch 창 = (마커, 세션]", calls and calls[0][1] == date(2026, 7, 1)
              and calls[0][2] == date(2026, 7, 10), calls)
        check("마커 전진", m.read_text(encoding="utf-8").strip() == "2026-07-10")
        ev2 = process_dividends(b, "2026-07-10", m, fetch=fetch)
        check("멱등(재호출 무입금)", ev2 == [] and b.credits == [5.0], (ev2, b.credits))


def test_fail_open_fetch():
    print("[DIV] fail-open — fetch 예외에도 raise 없음, 마커 미전진(다음 런 재시도)")
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "marker.txt"
        m.write_text("2026-07-01", encoding="utf-8")

        def boom(sym, a, z):
            raise RuntimeError("api down")

        b = _FakeBroker([("AAA", 10.0)])
        ev = process_dividends(b, "2026-07-10", m, fetch=boom)   # raise 시 테스트 자체가 죽음
        check("이벤트 0", ev == [], ev)
        check("입금 0", b.credits == [], b.credits)
        check("마커 미전진(재시도 방향)", m.read_text(encoding="utf-8").strip() == "2026-07-01")


def test_fail_open_credit():
    print("[DIV] credit 예외 — 해당 건만 스킵(과소계상)·나머지 진행·raise 없음")
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "marker.txt"
        m.write_text("2026-07-01", encoding="utf-8")

        def fetch(sym, a, z):
            return {date(2026, 7, 3): 1.0}   # AAA→$10, BBB→$3

        b = _FakeBroker([("AAA", 10.0), ("BBB", 3.0)], raise_amounts=(10.0,))
        ev = process_dividends(b, "2026-07-10", m, fetch=fetch)
        check("실패 건 제외 입금", b.credits == [3.0], b.credits)
        check("반환 = 입금 성공분만", len(ev) == 1 and ev[0]["symbol"] == "BBB", ev)
        check("마커는 전진(재입금 차단)", m.read_text(encoding="utf-8").strip() == "2026-07-10")


def test_corrupt_marker():
    print("[DIV] 손상 마커 — 재초기화(입금 0, 과소계상 방향)")
    with tempfile.TemporaryDirectory() as td:
        m = Path(td) / "marker.txt"
        m.write_text("not-a-date!!", encoding="utf-8")
        b = _FakeBroker([("AAA", 10.0)])
        ev = process_dividends(b, "2026-07-10", m,
                               fetch=lambda s, a, z: {date(2026, 7, 3): 1.0})
        check("이벤트 0·입금 0", ev == [] and b.credits == [], (ev, b.credits))
        check("마커 재초기화", m.read_text(encoding="utf-8").strip() == "2026-07-10")


def test_paperbroker_credit_cash():
    print("[DIV] PaperBroker.credit_cash — 입금·책 영속·비정상값 거부")
    with tempfile.TemporaryDirectory() as td:
        sf = Path(td) / "book.json"
        b = PaperBroker(cash=100.0, price_fn=lambda s: 10.0, state_file=str(sf))
        b.credit_cash(5.25)
        check("현금 반영", abs(b.get_account().cash - 105.25) < 1e-9, b.get_account().cash)
        saved = json.loads(sf.read_text(encoding="utf-8"))
        check("책 영속", abs(saved["cash"] - 105.25) < 1e-9, saved)
        for bad in (0, -1.0, float("nan"), float("inf")):
            try:
                b.credit_cash(bad)
                check(f"거부 {bad!r}", False)
            except ValueError:
                check(f"거부 {bad!r}", True)
        check("거부 후 현금 불변", abs(b.get_account().cash - 105.25) < 1e-9)


def main():
    print("=" * 60)
    print("tests_dividends — 배당 입금(§B 총수익 회계) 검증")
    print("=" * 60)
    for fn in (test_first_run_init, test_window_and_credit, test_fail_open_fetch,
               test_fail_open_credit, test_corrupt_marker, test_paperbroker_credit_cash):
        fn()
    print("-" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("실패:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
