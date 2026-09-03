#!/usr/bin/env python3
"""chartist(SR Flip) 진입 게이트 실증 리플레이 — 실제 1분봉으로 게이트별 통과 횟수를 센다.

왜: 2026-08 관측에서 chartist 진입이 0건이었는데, 저널로는 원인을 못 본다 — 무장(`armed`)·
되돌림 근접은 `ctx['state']` 인메모리에만 있고 디스크에 안 남기 때문이다. 그래서 실제 1분봉을
룰에 다시 먹여 어느 게이트에서 죽는지 센다.

방법:
  · 원본 `intraday_rules.chartist_rule` 을 그대로 호출 = 진입 판정 ground truth
  · 그와 line-for-line 동일한 계측 사본으로 게이트별 카운터 집계
  · 두 판정이 갈리면 `계측사본 불일치` 로 표면화 (0 이어야 계측이 유효)
  · 포지션은 항상 flat — 보유로 막히는 기회를 제외한 *진입 기회 상한* 측정

사용:
  python research/chartist_gate_replay.py --days 7 --set chartist
  python research/chartist_gate_replay.py --days 7 --set ctl --bar-min 5
  python research/chartist_gate_replay.py --selftest        # 네트워크 없이 계측사본 동형성 검사

⚠️ 관측 전용 — 매매 경로와 무관하고 어떤 상태도 쓰지 않는다.
⚠️ yfinance 1분봉은 최근 ~30일만 제공하고 1회 요청 상한이 8일이다(--days 기본 7).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 룰 임포트가 paths.py 를 끌어오므로 실 상태 디렉터리를 건드리지 않게 스크래치 home 으로 격리.
os.environ.setdefault("USTRADE_HOME",
                      str(Path(os.environ.get("TEMP", "/tmp")) / "chartist_gate_replay"))

from run_intraday import Bar                    # noqa: E402
import intraday_rules as R                      # noqa: E402
from personas import PERSONAS                   # noqa: E402

CFG = PERSONAS["chartist"]["intraday_cfg"]
SETS = {"chartist": PERSONAS["chartist"]["watchlist"],
        "ctl": PERSONAS["chartist_ctl"]["watchlist"]}
SETS["both"] = sorted(set(SETS["chartist"]) | set(SETS["ctl"]))

GATES = ["bars_eligible", "g1_uptrend", "g2_breakout", "g3_thrust_ARMED",
         "armed_bars", "disarm_expire", "disarm_breakdown",
         "g4_low_touch", "g5_near", "g6_bull_reversal",
         "x_rsi_block", "g7_rsi_ok", "x_min_risk_block", "g8_ENTRY"]


def instrumented(bars, cnt, st, cfg=CFG, regime_on=True, cash=1e9, equity=100000.0):
    """chartist_rule 진입부 미러 — 게이트 카운터만 추가. 반환 True = BUY 판정.

    ⚠️ 원본이 바뀌면 여기도 같이 바꿔야 한다. 어긋나면 리포트의 `계측사본 불일치` 가 0 이 아니게 된다.
    """
    look, ma_bars = cfg["sr_bars"], cfg["ma_bars"]
    rsi_bars, rsi_max = cfg["rsi_bars"], cfg["rsi_max"]
    buf, tol, max_wait = cfg["breakout_buf"], cfg["retest_tol"], cfg["retest_max_bars"]
    stop_buf, swing_bars = cfg["stop_buf"], cfg["swing_bars"]
    min_risk_frac, max_chase = cfg["min_risk_frac"], cfg["max_chase"]
    retest_low_tol, rr = cfg["retest_low_tol"], cfg["rr"]
    tmin = R._thrust_min_eff(bars, cfg, 0.0015)
    entry_amt = cfg["entry_frac"] * equity
    if not bars:
        return False
    closes = [b.close for b in bars]
    c = closes[-1]
    ma = R._ma(closes, ma_bars)
    st.pop("stop", None)
    st.pop("target", None)
    if len(bars) < look + 2:
        return False
    cnt["bars_eligible"] += 1
    armed = st.get("armed")
    if armed is None:
        real = [b for b in bars[-(look + 1):-1] if b.n > 0]
        if not real:
            return False
        resistance = max(b.high for b in real)
        uptrend = ma is not None and c > ma
        if uptrend:
            cnt["g1_uptrend"] += 1
            if c > resistance * (1 + buf):
                cnt["g2_breakout"] += 1
                if regime_on and R._thrust(bars, 3) > tmin:
                    cnt["g3_thrust_ARMED"] += 1
                    st["armed"] = {"level": resistance, "age": 0}
        return False
    armed["age"] += 1
    level = armed["level"]
    if armed["age"] > max_wait or c < level * (1 - tol * 2):
        cnt["disarm_expire" if armed["age"] > max_wait else "disarm_breakdown"] += 1
        st.pop("armed", None)
        return False
    cnt["armed_bars"] += 1
    low_ok = level * (1 - retest_low_tol) <= bars[-1].low <= level
    close_ok = level * (1 - tol) <= c <= level * (1 + max_chase)
    if low_ok:
        cnt["g4_low_touch"] += 1
    if not (regime_on and low_ok and close_ok):
        return False
    cnt["g5_near"] += 1
    if not R._bull_reversal(bars):
        return False
    cnt["g6_bull_reversal"] += 1
    rsi = R._rsi(closes, rsi_bars)
    if rsi is not None and rsi >= rsi_max:
        cnt["x_rsi_block"] += 1
        return False
    cnt["g7_rsi_ok"] += 1
    swing_low = max(min(b.low for b in bars[-swing_bars:]), level * (1 - retest_low_tol))
    stop = min(swing_low, level) * (1 - stop_buf)
    risk = c - stop
    cnt["risk_frac_sum"] += risk / c
    cnt["risk_frac_max"] = max(cnt["risk_frac_max"], risk / c)
    if risk <= 0:
        return False
    rpt = cfg.get("risk_per_trade", 0.02)
    cap = cfg.get("max_position_weight", 0.40)
    amount = min(equity * rpt * (c / risk), equity * cap * 0.95)
    amount = min(amount, cash)
    if amount < cfg.get("min_order_usd", 5.0):
        cnt["x_min_risk_block"] += 1        # 이제 현금소진/미소명목만(구 min_risk_frac 거부 폐기)
        return False
    cnt["g8_ENTRY"] += 1
    st.clear()
    st["stop"] = stop
    st["target"] = c + rr * risk
    return True


def to_bars(df, step_min=1):
    """세션 1일치 OHLC 프레임 → Bar 리스트. 결측 버킷은 n=0 평탄봉으로 충전(BarAggregator 동형)."""
    out, prev = [], None
    for ts, row in df.iterrows():
        b = int(ts.timestamp() // 60) // step_min
        if prev is not None and b > prev + 1:
            close = out[-1].close
            for k in range(prev + 1, min(b, prev + 400)):
                out.append(Bar(k * 60 * step_min, close, close, close, close, 0))
        out.append(Bar(b * 60 * step_min, float(row["Open"]), float(row["High"]),
                       float(row["Low"]), float(row["Close"]), 1))
        prev = b
    return out


def replay(symbols, days, bar_min, regime_on=True):
    import yfinance as yf
    data = yf.download(symbols, period=f"{days}d", interval="1m", group_by="ticker",
                       auto_adjust=False, progress=False, threads=True)
    cnt = Counter()
    cnt["risk_frac_max"] = 0.0
    entries = mismatches = sessions = 0
    for s in symbols:
        try:
            df = (data[s] if len(symbols) > 1 else data).dropna()
        except KeyError:
            print(f"  ! {s}: 데이터 없음")
            continue
        if df.empty:
            print(f"  ! {s}: 빈 프레임")
            continue
        if df.index.tz is not None:
            df = df.tz_convert("America/New_York")
        for _day, g in df.groupby(df.index.date):
            g = g.between_time("09:30", "16:00")
            if bar_min > 1:
                g = g.resample(f"{bar_min}min").agg(
                    {"Open": "first", "High": "max", "Low": "min", "Close": "last"}).dropna()
            if len(g) < CFG["sr_bars"] + 10:
                continue
            sessions += 1
            bars_all = to_bars(g, bar_min)
            st_i, st_r = {}, {}
            ctx = {"cfg": CFG, "state": st_r, "sym": s,
                   "equity": 100000.0, "regime_on": regime_on}
            for i in range(1, len(bars_all) + 1):
                w = bars_all[:i]
                buy_i = instrumented(w, cnt, st_i, regime_on=regime_on)
                buy_r = bool(R.chartist_rule(w, None, 1e9, ctx))   # pos=None → 항상 flat
                entries += buy_r
                mismatches += (buy_i != buy_r)
    return cnt, entries, mismatches, sessions


def report(cnt, entries, mismatches, sessions):
    print(f"\n세션수(종목·일): {sessions}   원본룰 BUY: {entries}   계측사본 불일치: {mismatches}")
    print("\n게이트별 통과 카운트")
    for k in GATES:
        print(f"  {k:22s} {cnt[k]}")
    n = cnt["g7_rsi_ok"]
    if n:
        print(f"\n  최종 risk 후보 {n}건 · 평균 risk/c = {cnt['risk_frac_sum'] / n * 100:.3f}%"
              f" · 최대 {cnt['risk_frac_max'] * 100:.3f}%"
              f"   (요구 {CFG['min_risk_frac'] * 100:.1f}%)")
    if mismatches:
        print("\n  ⚠️ 계측사본이 원본룰과 어긋났다 — instrumented() 를 chartist_rule 에 재정합할 것.")


def selftest():
    """네트워크 없이 계측사본 ↔ 원본룰 동형성 검사. 합성 랜덤워크 봉으로 전 경로를 밟는다."""
    import random
    random.seed(7)
    cnt = Counter()
    cnt["risk_frac_max"] = 0.0
    mismatch = 0
    for _trial in range(40):
        px, bars = 100.0, []
        st_i, st_r = {}, {}
        ctx = {"cfg": CFG, "state": st_r, "sym": "T", "equity": 100000.0, "regime_on": True}
        for _ in range(200):
            px *= 1 + random.gauss(0, 0.0025)
            hi = px * (1 + abs(random.gauss(0, 0.002)))
            lo = px * (1 - abs(random.gauss(0, 0.002)))
            bars.append(Bar(len(bars) * 60, px, hi, lo, px, 1))
            a = instrumented(bars, cnt, st_i)
            b = bool(R.chartist_rule(bars, None, 1e9, ctx))
            mismatch += (a != b)
    touched = cnt["g3_thrust_ARMED"] + cnt["g5_near"]
    assert cnt["bars_eligible"] > 0, "웜업조차 못 넘음 — 합성 봉 수 부족"
    assert touched > 0, "무장/근접 경로를 한 번도 안 밟음 — selftest 가 게이트를 검증 못 함"
    assert mismatch == 0, f"계측사본 불일치 {mismatch}건 — instrumented() 재정합 필요"
    print(f"selftest OK — 평가봉 {cnt['bars_eligible']}, 무장 {cnt['g3_thrust_ARMED']}, "
          f"근접 {cnt['g5_near']}, 불일치 0")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=7, help="yfinance 1분봉 기간(상한 8)")
    ap.add_argument("--set", default="chartist", choices=list(SETS), help="watchlist 선택")
    ap.add_argument("--bar-min", type=int, default=1, help="봉 길이(분) — 1분봉을 리샘플")
    ap.add_argument("--regime-off", action="store_true", help="레짐 OFF 로 평가(신규진입 차단 확인용)")
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 계측사본 동형성만 검사")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    syms = SETS[a.set]
    print(f"set={a.set}  bar={a.bar_min}m  days={a.days}  "
          f"symbols({len(syms)}): {','.join(syms)}")
    report(*replay(syms, a.days, a.bar_min, regime_on=not a.regime_off))


if __name__ == "__main__":
    main()
