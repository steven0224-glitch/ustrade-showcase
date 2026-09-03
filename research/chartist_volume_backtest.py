"""오프라인 연구 백테스트 — chartist S/R 되돌림 전략: 거래량 확인 有 vs 無.

목적: Toss 는 거래량 미제공 → 실거래는 가격전용(A)뿐. "만약 거래량을 봤다면(B) 성과가
      유의하게 나았을까?"를 일봉 데이터(yfinance)로 오프라인 비교. *실거래/모의 경로엔 안 섞음.*

방법론(backtest-expert): 무-lookahead(신호=당일 종가, 진입=익일 시가), 마찰(슬리피지) 부과,
      리스크기반 사이징, 다레짐 5년+, rr·vol_mult plateau 스윕, per-year, 2× 슬리피지 스트레스.

두 변형은 *돌파 무장 조건만* 다름:
  A(price-only) : 추세(C>MA) + 저항 종가돌파 + 당일 상승봉(모멘텀 프록시=thrust 대체)
  B(volume-conf): A + 돌파일 거래량 >= vol_mult × 최근 vol_lookback 평균  (방식 4편 1.5~3× 룰)
공통: 무장 후 되돌림(레벨 근접) + 상승 반전캔들 + RSI<rsi_max → 익일 시가 진입.
      청산: 되돌림 스윙로우 아래 레벨손절 / R:R 익절 / 시간초과 종가청산 (전부 익일 이후 봉으로 판정).
"""
import sys, os, math
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트(이식성)
import data  # yfinance 로더 (Open/High/Low/Close/Volume)

WATCHLIST = ["NVDA", "AMD", "TSLA", "AAPL", "MSFT", "META", "AMZN", "PLTR", "AVGO", "GOOGL"]
START, END = "2016-01-01", date.today().isoformat()

P = dict(sr_lookback=30, ma_bars=20, rsi_bars=14, rsi_max=72.0,
         breakout_buf=0.002, retest_tol=0.005, retest_max=10,
         stop_buf=0.005, rr=2.0, max_hold=40,
         vol_mult=1.5, vol_lookback=20,
         slippage_bps=5.0, risk_per_trade=0.01)


# ───────── 지표 (numpy 없이 순수, 결정론) ─────────
def sma(vals, i, n):
    if i - n + 1 < 0:
        return None
    return sum(vals[i - n + 1:i + 1]) / n


def rsi(closes, i, n=14):
    if i - n < 0:
        return None
    gain = loss = 0.0
    for k in range(i - n + 1, i + 1):
        d = closes[k] - closes[k - 1]
        if d >= 0:
            gain += d
        else:
            loss -= d
    if loss == 0:
        return 100.0
    rs = (gain / n) / (loss / n)
    return 100.0 - 100.0 / (1.0 + rs)


def bull_reversal(o, h, l, c, i):
    """일봉 해머(긴 아래꼬리) 또는 상승장악형. intraday_rules._bull_reversal 과 동일 로직."""
    if h[i] <= l[i]:
        return False
    body = abs(c[i] - o[i])
    lower = min(o[i], c[i]) - l[i]
    upper = h[i] - max(o[i], c[i])
    if lower > 0 and lower >= 2 * body and upper <= body:              # 해머
        return True
    if i >= 1 and c[i - 1] < o[i - 1] and c[i] > o[i] \
            and c[i] >= o[i - 1] and o[i] <= c[i - 1]:                 # 상승장악형
        return True
    return False


# ───────── 백테스트 (한 종목) ─────────
def backtest_ticker(df, use_volume, p):
    o = df["Open"].tolist(); h = df["High"].tolist()
    l = df["Low"].tolist(); c = df["Close"].tolist(); v = df["Volume"].tolist()
    dates = [d.date().isoformat() for d in df.index]
    n = len(c)
    trades = []
    warm = max(p["sr_lookback"], p["ma_bars"], p["rsi_bars"] + 1, p["vol_lookback"]) + 1
    slip = p["slippage_bps"] / 1e4

    i = warm
    armed = None      # {"level", "age"}
    while i < n - 1:  # 진입은 익일(i+1) 시가라 마지막 봉 전까지
        ma = sma(c, i, p["ma_bars"])
        if armed is None:
            resistance = max(h[i - p["sr_lookback"]:i])               # 직전 N봉(현재 제외) 고점
            uptrend = ma is not None and c[i] > ma
            up_day = c[i] > c[i - 1]                                   # thrust 프록시(당일 상승봉)
            broke = c[i] > resistance * (1 + p["breakout_buf"])
            vol_ok = True
            if use_volume:
                vavg = sma(v, i - 1, p["vol_lookback"])               # 돌파일 제외 최근평균과 비교
                vol_ok = vavg is not None and vavg > 0 and v[i] >= p["vol_mult"] * vavg
            if uptrend and up_day and broke and vol_ok:
                armed = {"level": resistance, "age": 0}
            i += 1
            continue

        # armed — 되돌림 대기
        armed["age"] += 1
        level = armed["level"]
        if armed["age"] > p["retest_max"] or c[i] < level * (1 - p["retest_tol"] * 2):
            armed = None
            i += 1
            continue
        near = l[i] <= level * (1 + p["retest_tol"]) and c[i] >= level * (1 - p["retest_tol"])
        rv = rsi(c, i, p["rsi_bars"])
        if not (near and bull_reversal(o, h, l, c, i) and (rv is None or rv < p["rsi_max"])):
            i += 1
            continue

        # 진입 = 익일(j=i+1) 시가 + 슬리피지
        j = i + 1
        entry = o[j] * (1 + slip)
        swing_low = min(l[i - 2:i + 1])
        stop = min(swing_low, level) * (1 - p["stop_buf"])
        risk = entry - stop
        if risk <= 0:
            armed = None
            i += 1
            continue
        target = entry + p["rr"] * risk

        # 보유 — j 부터 청산 스캔 (당일 봉 High/Low 로 터치 판정, 갭은 시가 체결)
        exit_px = exit_dt = exit_reason = None
        k = j
        held = 0
        while k < n:
            held += 1
            # 보수적: 같은 봉서 손절·익절 동시 터치면 손절 우선
            if l[k] <= stop:
                exit_px = min(o[k], stop) * (1 - slip)               # 갭다운이면 시가 체결(더 나쁨)
                exit_dt, exit_reason = dates[k], "stop"
                break
            if h[k] >= target:
                exit_px = target * (1 - slip)                        # 보수적: 갭업 상단 미반영, 목표가만 체결(승자 과대 방지)
                exit_dt, exit_reason = dates[k], "target"
                break
            if held >= p["max_hold"]:
                exit_px = c[k] * (1 - slip)
                exit_dt, exit_reason = dates[k], "time"
                break
            k += 1
        if exit_px is None:                                          # 데이터 끝 — 미청산 트레이드 제외(생존편향 방지)
            break
        R = (exit_px - entry) / risk
        trades.append(dict(sym=df.attrs.get("sym", "?"), entry_dt=dates[j], entry=entry,
                           exit_dt=exit_dt, exit=exit_px, R=R,
                           ret=(exit_px - entry) / entry, reason=exit_reason, year=dates[j][:4]))
        armed = None
        i = k + 1                                                    # 청산 후부터 재스캔(중복 보유 없음)
    return trades


# ───────── 지표 집계 ─────────
def metrics(trades):
    if not trades:
        return dict(n=0)
    wins = [t for t in trades if t["R"] > 0]
    losses = [t for t in trades if t["R"] <= 0]
    gross_win = sum(t["R"] for t in wins)
    gross_loss = -sum(t["R"] for t in losses)
    # 리스크기반 사이징 equity 커브(트레이드 시간순, 각 진입 risk_per_trade)
    eq = 1.0; peak = 1.0; maxdd = 0.0
    for t in sorted(trades, key=lambda x: x["entry_dt"]):
        eq *= (1 + P["risk_per_trade"] * t["R"])
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    return dict(
        n=len(trades),
        win_rate=len(wins) / len(trades),
        avg_R=sum(t["R"] for t in trades) / len(trades),
        avg_win_R=(gross_win / len(wins)) if wins else 0.0,
        avg_loss_R=(-gross_loss / len(losses)) if losses else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        expectancy_R=sum(t["R"] for t in trades) / len(trades),
        equity_mult=eq,                                             # 1% 리스크 *복리*(재투자) — 참고용
        equity_linear=1 + P["risk_per_trade"] * sum(t["R"] for t in trades),  # 고정사이징(비복리)
        max_dd=maxdd,
        tgt_hits=sum(1 for t in trades if t["reason"] == "target"),
        stop_hits=sum(1 for t in trades if t["reason"] == "stop"),
        time_hits=sum(1 for t in trades if t["reason"] == "time"),
    )


def per_year(trades):
    ys = {}
    for t in trades:
        ys.setdefault(t["year"], []).append(t["R"])
    return {y: (len(rs), sum(rs) / len(rs)) for y, rs in sorted(ys.items())}


def run(use_volume, p, dfs):
    all_t = []
    for sym, df in dfs.items():
        df.attrs["sym"] = sym
        all_t += backtest_ticker(df, use_volume, p)
    return all_t


def fmt(m):
    if m.get("n", 0) == 0:
        return "  (트레이드 0)"
    return (f"  트레이드={m['n']}  승률={m['win_rate']:.1%}  기대값={m['expectancy_R']:+.3f}R  "
            f"PF={m['profit_factor']:.2f}  평균승={m['avg_win_R']:+.2f}R 평균패={m['avg_loss_R']:+.2f}R\n"
            f"  총수익={m['equity_linear'] - 1:+.1%}(고정1%)  복리×={m['equity_mult']:.3f}  MaxDD={m['max_dd']:.1%}  "
            f"익절/손절/시간={m['tgt_hits']}/{m['stop_hits']}/{m['time_hits']}")


if __name__ == "__main__":
    print(f"데이터 로드 {START}~{END} …")
    dfs = {}
    for sym in WATCHLIST:
        try:
            df = data.load(sym, START, END)
            if len(df) > 100:
                dfs[sym] = df
        except Exception as e:
            print(f"  ⚠ {sym} 로드 실패: {e!r}")
    print(f"로드 성공: {sorted(dfs)} ({len(dfs)}/{len(WATCHLIST)})\n")
    if not dfs:
        print("데이터 0 — 네트워크/캐시 확인 필요")
        sys.exit(1)

    tot_days = sum(len(d) for d in dfs.values())
    print(f"총 일봉 {tot_days} (종목평균 {tot_days // len(dfs)}일 ≈ {tot_days // len(dfs) // 252}년)\n")

    print("=" * 62)
    print("baseline (rr=2.0, vol_mult=1.5, slip=5bp, risk=1%/trade)")
    print("=" * 62)
    A = run(False, P, dfs)
    B = run(True, P, dfs)
    print("[A] 가격전용 (Toss 실행가능):"); print(fmt(metrics(A)))
    print("[B] 거래량확인 (오프라인 전용):"); print(fmt(metrics(B)))

    print("\n─ per-year 기대값(R) [트레이드수] ─")
    ya, yb = per_year(A), per_year(B)
    for y in sorted(set(ya) | set(yb)):
        na, ea = ya.get(y, (0, 0)); nb, eb = yb.get(y, (0, 0))
        print(f"  {y}:  A {ea:+.2f}R[{na:>2}]   B {eb:+.2f}R[{nb:>2}]")

    print("\n─ rr 스윕 (plateau 확인, vol_mult=1.5 고정) ─")
    for rr in (1.5, 2.0, 2.5, 3.0):
        pp = dict(P, rr=rr)
        ma, mb = metrics(run(False, pp, dfs)), metrics(run(True, pp, dfs))
        print(f"  rr={rr}:  A 기대{ma.get('expectancy_R',0):+.3f}R PF{ma.get('profit_factor',0):.2f} n{ma.get('n',0)}"
              f"   |  B 기대{mb.get('expectancy_R',0):+.3f}R PF{mb.get('profit_factor',0):.2f} n{mb.get('n',0)}")

    print("\n─ vol_mult 스윕 (B 만, rr=2.0) ─")
    for vm in (1.3, 1.5, 2.0, 3.0):
        mb = metrics(run(True, dict(P, vol_mult=vm), dfs))
        print(f"  vol_mult={vm}:  B 기대{mb.get('expectancy_R',0):+.3f}R PF{mb.get('profit_factor',0):.2f} 승률{mb.get('win_rate',0):.1%} n{mb.get('n',0)}")

    print("\n─ 2× 슬리피지 스트레스 (slip=10bp) ─")
    pp = dict(P, slippage_bps=10.0)
    ma, mb = metrics(run(False, pp, dfs)), metrics(run(True, pp, dfs))
    print("[A] "); print(fmt(ma)); print("[B] "); print(fmt(mb))

    print("\n─ 슬리피지 0 (마찰 무영향 확인) ─")
    pp = dict(P, slippage_bps=0.0)
    ma, mb = metrics(run(False, pp, dfs)), metrics(run(True, pp, dfs))
    print(f"  A 기대{ma['expectancy_R']:+.3f}R PF{ma['profit_factor']:.2f}  |  B 기대{mb['expectancy_R']:+.3f}R PF{mb['profit_factor']:.2f}")
