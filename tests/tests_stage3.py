"""Stage 3 (MEDIUM 수정) 검증 — 네트워크 불필요.

M1 일일손실 fail-open / M3 에러 윈도우(flapping) / M4 day-over-day baseline /
M5 슬리피지·스프레드 / M6 원자적 상태쓰기·fail-closed.
(M2 루프중 HALT 재체크 = Stage1 C4 GuardedBroker 에서 검증됨.)
실행:  & $py tests_stage3.py
"""
import json
import sys

from broker import guardrail
from broker import GuardConfig, KillSwitch, HaltError, PaperBroker
from broker.base import OrderRequest, Side, OrderType
from tests_stage1 import _use_temp_state

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


# ───────────────────────── M1 — 일일손실 fail-open 제거 ─────────────────────────
def test_m1_daily_loss():
    print("[M1] baseline None=통과, 0/음수=fail-closed, 손실초과=trip")
    _use_temp_state()
    ks = KillSwitch(today="2026-06-01")

    ks.state["day_start_equity"] = None
    check("baseline None → 0 반환(통과)", ks.check_daily_loss(100.0) == 0.0)

    ks.state["day_start_equity"] = 0.0
    raised = False
    try:
        ks.check_daily_loss(100.0)
    except HaltError:
        raised = True
    check("baseline 0 → fail-closed(HaltError)", raised)

    ks2 = KillSwitch(today="2026-06-02")
    ks2.reset()
    ks2.state["day_start_equity"] = 100000.0
    check("손실 -3% (한도 내) → dd 반환", abs(ks2.check_daily_loss(97000.0) - (-0.03)) < 1e-9)
    tripped = False
    try:
        ks2.check_daily_loss(90000.0)   # -10% < -5%
    except HaltError:
        tripped = True
    check("손실 -10% > 한도 → trip", tripped)


# ───────────────────────── M3 — 에러 윈도우 (flapping 차단) ─────────────────────────
def test_m3_error_window():
    print("[M3] 성공/실패 교차도 윈도우 누적 → 트립 (연속 아님)")
    _use_temp_state()
    cfg = GuardConfig(max_consecutive_errors=3, error_window=6)
    ks = KillSwitch(config=cfg, today="2026-06-01")

    # flapping: err, ok, err, ok, err → 윈도우 [1,0,1,0,1] 합=3 → 3번째 err 에서 트립
    seq = ["e", "s", "e", "s"]
    for o in seq:
        if o == "e":
            try:
                ks.record_error("flap")
            except HaltError:
                pass
        else:
            ks.record_success()
    check("교차 4회 후 아직 미트립", not ks.state["halted"], ks.state)
    tripped = False
    try:
        ks.record_error("flap-final")   # 3번째 에러
    except HaltError:
        tripped = True
    check("flapping 3번째 에러 → 트립", tripped and ks.state["halted"])

    # 연속 3회도 트립
    _use_temp_state()
    ks2 = KillSwitch(config=GuardConfig(max_consecutive_errors=3, error_window=6), today="2026-06-01")
    cnt = 0
    for _ in range(3):
        try:
            ks2.record_error("x")
        except HaltError:
            cnt += 1
    check("연속 3회 → 트립", ks2.state["halted"] and cnt == 1)


# ───────────────────────── M4 — day-over-day baseline ─────────────────────────
def test_m4_baseline_carry():
    print("[M4] 일일손실 baseline = 직전일 마지막 자산 (first-touch 아님)")
    _use_temp_state()                       # 한 temp 디렉토리 공유 (영속 확인)
    ks1 = KillSwitch(today="2026-06-01")
    ks1.roll_day(100000.0)                  # day1: prior 없음 → base 100k, last_equity 100k
    check("day1 baseline = 현재(최초)", ks1.state["day_start_equity"] == 100000.0)

    ks2 = KillSwitch(today="2026-06-02")    # 영속 state 로드 (last_equity=100k)
    ks2.roll_day(90000.0)                   # day2: base = 직전 last_equity 100k (90k 아님)
    check("day2 baseline = 직전일 자산(100k)", ks2.state["day_start_equity"] == 100000.0,
          ks2.state["day_start_equity"])
    tripped = False
    try:
        ks2.check_daily_loss(90000.0)       # 100k 대비 -10% → 트립 (first-touch면 미트립)
    except HaltError:
        tripped = True
    check("day-over-day -10% → 트립", tripped)


# ───────────────────────── M5 — 슬리피지/스프레드 ─────────────────────────
def test_m5_slippage():
    print("[M5] 시장가 체결 = 낙관적 mid 아님 (매수 불리, 매도 불리)")
    b = PaperBroker(cash=1_000_000.0, price_fn=lambda s: 100.0,
                    commission=0.0, spread=0.001, slippage=0.001)
    q = b.get_quote("AAA")
    check("ask > mid > bid", q.bid < 100.0 < q.ask, (q.bid, q.ask))

    buy = b.place_order(OrderRequest("AAA", Side.BUY, 10, OrderType.MARKET))
    check("매수 체결가 > mid(100)", buy.avg_fill_price > 100.0, buy.avg_fill_price)
    sell = b.place_order(OrderRequest("AAA", Side.SELL, 5, OrderType.MARKET))
    check("매도 체결가 < mid(100)", sell.avg_fill_price < 100.0, sell.avg_fill_price)
    check("매수가 > 매도가 (스프레드 비용)", buy.avg_fill_price > sell.avg_fill_price)


# ───────────────────────── M6 — 원자적 쓰기 + fail-closed ─────────────────────────
def test_m6_atomic_failclosed():
    print("[M6] 손상 상태파일 → fail-closed(halt), 저장은 원자적")
    d = _use_temp_state()

    # 손상 파일 → 로드 시 halted=True
    guardrail.STATE_FILE.write_text("{이건 깨진 JSON…", encoding="utf-8")
    ks = KillSwitch(today="2026-06-01")
    halted, reason = ks.is_halted()
    check("손상 파일 → fail-closed(halt)", halted and "손상" in reason, reason)

    # reset 후 정상 저장 → 유효 JSON, tmp 잔존 없음
    ks.reset()
    ks.record_success()
    data = json.loads(guardrail.STATE_FILE.read_text(encoding="utf-8"))
    check("저장 후 유효 JSON", isinstance(data, dict) and "recent" in data)
    check("tmp 파일 잔존 없음", not list(d.glob("killswitch.*.tmp")))


def main():
    print("=" * 70)
    print(" Stage 3 (MEDIUM) 검증 — 네트워크 없음")
    print("=" * 70)
    for t in (test_m1_daily_loss, test_m3_error_window, test_m4_baseline_carry,
              test_m5_slippage, test_m6_atomic_failclosed):
        print()
        t()
    print("\n" + "=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
