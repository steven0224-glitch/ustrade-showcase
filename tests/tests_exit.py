"""장중 청산 로직(live_exit) 검증 — 네트워크 0.

200MA 이탈·손절 트리거, 정상 보유, 데이터 부족(자동청산 제외), opt-in(50MA/RSI).

실행:  & $py tests_exit.py
"""
import sys

import numpy as np
import pandas as pd

from broker.base import Position
from live_exit import check_exits, to_exit

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def _series(a, b, n=250):
    return pd.Series(np.linspace(a, b, n))


def _decmap(positions, closes, live, **kw):
    return {d["symbol"]: d for d in check_exits(positions, closes, live, **kw)}


def test_200ma_break():
    print("[EXIT] 현재가 < 200MA → 청산 📉")
    pos = [Position("AAA", 10, 140.0)]
    closes = {"AAA": _series(100, 200)}              # SMA200 ≈ 165
    d = _decmap(pos, closes, {"AAA": 150.0})["AAA"]   # 150 < 165 → 이탈, 150 > 140*0.92 → 손절 아님
    check("200MA 이탈 사유", any("200MA" in r for r in d["reasons"]), d["reasons"])
    check("📉 플래그", "📉" in d["flags"], d["flags"])
    check("to_exit 포함", "AAA" in [x["symbol"] for x in to_exit([d])], d)


def test_stop_loss():
    print("[EXIT] 현재가 < 매입가×(1-8%) → 손절 🛑 (200MA 이탈 아님)")
    pos = [Position("BBB", 10, 140.0)]
    closes = {"BBB": _series(50, 110)}                # SMA200 ≈ 85 → 120 > 85 (이탈 아님)
    d = _decmap(pos, closes, {"BBB": 120.0})["BBB"]    # 120 < 140*0.92=128.8 → 손절
    check("손절 사유", any("손절" in r for r in d["reasons"]), d["reasons"])
    check("🛑 플래그", "🛑" in d["flags"], d["flags"])
    check("200MA 이탈 아님", not any("200MA" in r for r in d["reasons"]), d["reasons"])


def test_healthy_hold():
    print("[EXIT] 정상(추세 위·손절 위) → 청산 안 함")
    pos = [Position("CCC", 10, 150.0)]
    closes = {"CCC": _series(100, 200)}              # SMA200 ≈ 165
    d = _decmap(pos, closes, {"CCC": 190.0})["CCC"]    # 190 > 165, 190 > 138 → 트리거 없음
    check("사유 없음", d["reasons"] == [], d["reasons"])
    check("to_exit 제외", to_exit([d]) == [], to_exit([d]))


def test_insufficient_data():
    print("[EXIT] 일봉<200 + 손절선 위 → data_ok False, 자동청산 제외(수동확인)")
    pos = [Position("DDD", 10, 100.0)]
    closes = {"DDD": _series(100, 120, 100)}          # 100봉 < 200
    d = _decmap(pos, closes, {"DDD": 98.0})["DDD"]    # 98 > 100*0.92 → 손절 아님 → MA 판정불가로 수동확인
    check("data_ok False", d["data_ok"] is False, d)
    check("to_exit 에서 제외(데이터부족)", to_exit([d]) == [], to_exit([d]))


def test_hard_stop_fires_without_ma():
    print("[EXIT] 일봉<200/결측이어도 하드 손절은 자동 집행 — MA 시리즈 가용성과 분리(자본보호)")
    pos = [Position("DDD", 10, 100.0)]
    # 일봉 100봉(<200) → MA 판정 불가, 그러나 실시간가 -10% 는 8% 손절 발화해야 함
    d = _decmap([Position("DDD", 10, 100.0)], {"DDD": _series(100, 120, 100)}, {"DDD": 90.0})["DDD"]
    check("data_ok True(손절 발화)", d["data_ok"] is True, d)
    check("손절 사유", any("손절" in r for r in d["reasons"]), d["reasons"])
    check("to_exit 포함(자동청산)", "DDD" in [x["symbol"] for x in to_exit([d])], to_exit([d]))
    # 일봉 완전 결측(None)도 동일 — 하드 손절 자동 집행
    d2 = _decmap(pos, {"DDD": None}, {"DDD": 90.0})["DDD"]
    check("일봉 None 이어도 손절 발화", d2["data_ok"] is True and any("손절" in r for r in d2["reasons"]), d2)


def test_missing_price():
    print("[EXIT] 실시간가 없음 → data_ok False")
    pos = [Position("EEE", 10, 100.0)]
    d = _decmap(pos, {"EEE": _series(100, 200)}, {"EEE": None})["EEE"]
    check("data_ok False", d["data_ok"] is False, d)


def test_opt_in_50ma():
    print("[EXIT] use_50ma → 50MA 이탈(200MA 위)도 청산 🟡")
    pos = [Position("FFF", 10, 100.0)]
    # 최근 급반등으로 200MA 위지만 50MA 아래가 되게: 길게 상승 후 마지막에 살짝 눌림
    arr = np.concatenate([np.linspace(100, 200, 240), np.linspace(200, 188, 10)])
    closes = {"FFF": pd.Series(arr)}
    sma200 = pd.Series(arr).rolling(200).mean().iloc[-1]
    sma50 = pd.Series(arr).rolling(50).mean().iloc[-1]
    price = (sma50 + sma200) / 2                       # 200MA 위, 50MA 아래
    d_off = _decmap(pos, closes, {"FFF": price})["FFF"]
    check("기본(off) 50MA 미적용 → 사유 없음", d_off["reasons"] == [], d_off["reasons"])
    d_on = _decmap(pos, closes, {"FFF": price}, use_50ma=True)["FFF"]
    check("use_50ma → 🟡", "🟡" in d_on["flags"], d_on["flags"])


def test_opt_in_rsi():
    print("[EXIT] ob_rsi → RSI 과열 청산 🔺")
    pos = [Position("GGG", 10, 100.0)]
    closes = {"GGG": _series(100, 300)}              # 강한 상승 → RSI 높음, 추세 위
    d = _decmap(pos, closes, {"GGG": 300.0}, ob_rsi=70)["GGG"]
    check("RSI 과열 🔺", "🔺" in d["flags"], d["flags"])


def test_settle_reports_unfilled():
    print("[SETTLE] 청산 트리거 후 미체결/거부/부분 → status=exit_incomplete + 알림 (C3 무성실패 차단)")
    import run_exit
    from broker.base import OrderRequest, Order, OrderStatus, Side, OrderType
    if not hasattr(run_exit, "_settle_exits"):
        check("run_exit._settle_exits 존재", False, "미구현 — 미체결을 status='ok' 로 은폐 중")
        return

    def mk(sym, st, fq=0.0):
        return Order(order_id=("" if st == OrderStatus.REJECTED else "O" + sym),
                     request=OrderRequest(sym, Side.SELL, 10, OrderType.MARKET),
                     status=st, filled_qty=fq)
    exits = [{"symbol": "AAA"}, {"symbol": "BBB"}]
    syms = ["AAA", "BBB"]
    r_ok = run_exit._settle_exits([mk("AAA", OrderStatus.FILLED, 10), mk("BBB", OrderStatus.FILLED, 10)], exits, syms)
    check("전량 체결 → status=ok", r_ok["status"] == "ok", r_ok["status"])
    r_rej = run_exit._settle_exits([mk("AAA", OrderStatus.FILLED, 10), mk("BBB", OrderStatus.REJECTED)], exits, syms)
    check("일부 거부 → exit_incomplete (ok 은폐 금지)", r_rej["status"] == "exit_incomplete", r_rej["status"])
    check("미체결 종목 보고 (BBB)", r_rej.get("unfilled") == ["BBB"], r_rej.get("unfilled"))
    r_can = run_exit._settle_exits([mk("AAA", OrderStatus.CANCELLED), mk("BBB", OrderStatus.PARTIAL, 4)], exits, syms)
    check("취소·부분도 exit_incomplete", r_can["status"] == "exit_incomplete", r_can["status"])
    check("미체결 2종목 모두 보고", set(r_can.get("unfilled", [])) == {"AAA", "BBB"}, r_can.get("unfilled"))


def test_is_regular_open():
    print("[CAL] is_regular_open: 정규장 판정 (toss 시장시간 API 폴백, 네트워크 0)")
    import calendar_util as cu
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
    except Exception:
        import pytz
        ET = pytz.timezone("America/New_York")
    if not hasattr(cu, "is_regular_open"):
        check("calendar_util.is_regular_open 존재", False, "미구현")
        return
    # 2026-03-30 = 월(거래일·EDT). 10:30 장중 / 18:00 마감후 / 08:00 개장전, 03-28=토 휴장
    check("월 10:30 ET → 개장", cu.is_regular_open(datetime(2026, 3, 30, 10, 30, tzinfo=ET)) is True)
    check("월 18:00 ET → 마감후", cu.is_regular_open(datetime(2026, 3, 30, 18, 0, tzinfo=ET)) is False)
    check("월 08:00 ET → 개장전", cu.is_regular_open(datetime(2026, 3, 30, 8, 0, tzinfo=ET)) is False)
    check("토요일 → 휴장", cu.is_regular_open(datetime(2026, 3, 28, 11, 0, tzinfo=ET)) is False)
    # minutes_since_open (heartbeat 청산 cron 감시 오경보 가드)
    if hasattr(cu, "minutes_since_open"):
        mso = cu.minutes_since_open(datetime(2026, 3, 30, 10, 30, tzinfo=ET))
        check("월 10:30 ET → 개장 후 ~60분", mso is not None and abs(mso - 60) < 1, mso)
        check("월 18:00 ET → None(마감후)", cu.minutes_since_open(datetime(2026, 3, 30, 18, 0, tzinfo=ET)) is None)
        check("토요일 → None(휴장)", cu.minutes_since_open(datetime(2026, 3, 28, 11, 0, tzinfo=ET)) is None)


def main():
    print("=" * 70)
    print(" 장중 청산 로직(live_exit) 검증 — 네트워크 없음")
    print("=" * 70)
    print()
    for t in (test_200ma_break, test_stop_loss, test_healthy_hold, test_insufficient_data,
              test_hard_stop_fires_without_ma, test_missing_price, test_opt_in_50ma, test_opt_in_rsi,
              test_settle_reports_unfilled, test_is_regular_open):
        t(); print()
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
