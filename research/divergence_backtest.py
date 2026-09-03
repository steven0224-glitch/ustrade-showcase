"""오프라인 연구 백테스트 — 다이버전스(5주차 강의) 게이트가 chartist S/R 되돌림에 +인가.

목적: 강의(다이버전스, 진짜 추세를 알다)의 채택 후보를 이식 前 실증. chartist_volume_backtest
      (볼륨확인 -0.19R 기각 전례)와 동일 방법론·동일 베이스라인. *실거래/모의 경로엔 안 섞음.*

방법론: 무-lookahead(피벗은 우측 k봉 확인 후에만 성립, 신호=당일 종가, 진입=익일 시가),
      슬리피지 부과, 리스크기반 사이징, 다레짐 9년, 파라미터 스윕, per-year, 2× 슬리피지 스트레스.
      ⚠️ 일봉 프록시 — 실전은 1분봉. 방향 판정용(전례: 볼륨확인 기각도 일봉으로 충분했음).

변형(진입 게이트, 되돌림 확인 시점에 평가):
  A  베이스라인    — 가격전용 SR 되돌림 (chartist_volume_backtest A 와 동일 로직)
  B  히든RSI       — A + 히든 강세 RSI 다이버전스(가격 저점↑·RSI 저점↓, 눌림목 지속 신호) 요구
  C  약세거부      — A + 일반 약세 다이버전스(가격 고점↑·RSI 고점↓) 활성 시 진입 거부
  D  OBV돌파확인   — A + 무장(돌파) 시 OBV 도 N봉 신고(가격 신고에 거래량 흐름 동반 — 가짜돌파 필터)
  E  2중합의       — A + 히든 강세가 RSI 와 MACD *둘 다*에서 성립(강의: 동시 발생 = 최고 신뢰)
  F  약세청산      — A + 보유 중 일반 약세 다이버전스 확인 시 익일 시가 조기청산(러너 경고등)

다이버전스 판정(결정론·무-lookahead):
  피벗 저점 j = low[j] 가 좌우 k봉보다 낮음 — *우측 k봉이 지나야 확인*(신호는 j+k 이후에만 가용).
  히든 강세 = 최근 두 확인 피벗저점 (p1<p2)에서 low[p2] > low[p1] AND ind[p2] < ind[p1],
             p2 확인시점이 최근 div_recent 봉 이내. 일반 약세 = 피벗고점 대칭(가격↑·지표↓).
"""
import sys, os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo 루트
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                    # research/
import data  # noqa: E402
from chartist_volume_backtest import sma, rsi, bull_reversal, metrics, per_year  # noqa: E402

# livermore/chartist 계열 실제 watchlist 합집합(대형 기술주 16) — 실전 유니버스 정합
WATCHLIST = ["NVDA", "TSLA", "AMD", "META", "AMZN", "AAPL", "MSFT", "GOOGL", "NFLX", "AVGO",
             "PLTR", "SMCI", "COIN", "MU", "MRVL", "ORCL"]
START, END = "2016-01-01", date.today().isoformat()

P = dict(sr_lookback=30, ma_bars=20, rsi_bars=14, rsi_max=72.0,
         breakout_buf=0.002, retest_tol=0.005, retest_max=10,
         stop_buf=0.005, rr=2.0, max_hold=40,
         pivot_k=2, div_recent=10, min_pivot_sep=3,
         slippage_bps=5.0, risk_per_trade=0.01)


# ───────── 지표 시리즈 (사전계산, i 시점 값만 참조 = 무-lookahead) ─────────
def rsi_series(closes, n=14):
    return [rsi(closes, i, n) for i in range(len(closes))]


def macd_series(closes, fast=12, slow=26):
    """MACD 라인(EMA12-EMA26). EMA 는 재귀라 시계열 전체 사전계산이 자연·결정론."""
    def ema(vals, n):
        k = 2.0 / (n + 1)
        out, e = [], None
        for v in vals:
            e = v if e is None else v * k + e * (1 - k)
            out.append(e)
        return out
    ef, es = ema(closes, fast), ema(closes, slow)
    return [f - s for f, s in zip(ef, es)]


def obv_series(closes, vols):
    out, acc = [], 0.0
    for i in range(len(closes)):
        if i > 0:
            if closes[i] > closes[i - 1]:
                acc += vols[i]
            elif closes[i] < closes[i - 1]:
                acc -= vols[i]
        out.append(acc)
    return out


def confirmed_pivots(vals, i, k, is_low, span=60):
    """i 시점에 *확인된* 피벗(저점/고점) 인덱스 리스트 — 피벗 j 는 j+k <= i 여야 확인(무-lookahead).
    최근 span 봉만 스캔(성능·관련성)."""
    out = []
    lo = max(k, i - span)
    for j in range(lo, i - k + 1):
        w0, w1 = vals[j - k:j], vals[j + 1:j + k + 1]
        if is_low:
            if all(vals[j] < x for x in w0) and all(vals[j] <= x for x in w1):
                out.append(j)
        else:
            if all(vals[j] > x for x in w0) and all(vals[j] >= x for x in w1):
                out.append(j)
    return out


def _last_two(pivots, min_sep):
    if len(pivots) < 2:
        return None
    p2 = pivots[-1]
    for p1 in reversed(pivots[:-1]):
        if p2 - p1 >= min_sep:
            return p1, p2
    return None


def hidden_bullish(price_lows, ind, i, p):
    """히든 강세 — 가격 저점↑ AND 지표 저점↓, 최근 피벗이 div_recent 이내 확인분."""
    piv = confirmed_pivots(price_lows, i, p["pivot_k"], is_low=True)
    pair = _last_two(piv, p["min_pivot_sep"])
    if pair is None:
        return False
    p1, p2 = pair
    if i - (p2 + p["pivot_k"]) > p["div_recent"]:          # 확인 시점 기준 신선도
        return False
    if ind[p1] is None or ind[p2] is None:
        return False
    return price_lows[p2] > price_lows[p1] and ind[p2] < ind[p1]


def bearish_regular(price_highs, ind, i, p):
    """일반 약세 — 가격 고점↑ AND 지표 고점↓ (상승동력 소진 경고)."""
    piv = confirmed_pivots(price_highs, i, p["pivot_k"], is_low=False)
    pair = _last_two(piv, p["min_pivot_sep"])
    if pair is None:
        return False
    p1, p2 = pair
    if i - (p2 + p["pivot_k"]) > p["div_recent"]:
        return False
    if ind[p1] is None or ind[p2] is None:
        return False
    return price_highs[p2] > price_highs[p1] and ind[p2] < ind[p1]


# ───────── 백테스트 (한 종목, variant 훅) ─────────
def backtest_ticker(df, variant, p):
    o = df["Open"].tolist(); h = df["High"].tolist()
    l = df["Low"].tolist(); c = df["Close"].tolist(); v = df["Volume"].tolist()
    dates = [d.date().isoformat() for d in df.index]
    n = len(c)
    R = rsi_series(c, p["rsi_bars"])
    M = macd_series(c)
    O = obv_series(c, v)
    trades = []
    warm = max(p["sr_lookback"], p["ma_bars"], p["rsi_bars"] + 1) + p["pivot_k"] + 1
    slip = p["slippage_bps"] / 1e4

    i = warm
    armed = None
    while i < n - 1:
        ma = sma(c, i, p["ma_bars"])
        if armed is None:
            resistance = max(h[i - p["sr_lookback"]:i])
            uptrend = ma is not None and c[i] > ma
            up_day = c[i] > c[i - 1]
            broke = c[i] > resistance * (1 + p["breakout_buf"])
            gate = True
            if variant == "D" and broke:                     # OBV 돌파확인 — 가격 신고에 OBV 신고 동반
                gate = O[i] >= max(O[i - p["sr_lookback"]:i])
            if uptrend and up_day and broke and gate:
                armed = {"level": resistance, "age": 0}
            i += 1
            continue

        armed["age"] += 1
        level = armed["level"]
        if armed["age"] > p["retest_max"] or c[i] < level * (1 - p["retest_tol"] * 2):
            armed = None
            i += 1
            continue
        near = l[i] <= level * (1 + p["retest_tol"]) and c[i] >= level * (1 - p["retest_tol"])
        rv = R[i]
        if not (near and bull_reversal(o, h, l, c, i) and (rv is None or rv < p["rsi_max"])):
            i += 1
            continue
        # ── 되돌림 확인 시점의 다이버전스 게이트 ──
        if variant == "B" and not hidden_bullish(l, R, i, p):
            i += 1
            continue
        if variant == "C" and bearish_regular(h, R, i, p):
            i += 1
            continue
        if variant == "E" and not (hidden_bullish(l, R, i, p) and hidden_bullish(l, M, i, p)):
            i += 1
            continue

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

        exit_px = exit_dt = exit_reason = None
        k = j
        held = 0
        while k < n:
            held += 1
            if l[k] <= stop:
                exit_px = min(o[k], stop) * (1 - slip)
                exit_dt, exit_reason = dates[k], "stop"
                break
            if h[k] >= target:
                exit_px = target * (1 - slip)
                exit_dt, exit_reason = dates[k], "target"
                break
            # F: 보유 중 일반 약세 다이버전스 확인(당일 종가 시점) → 익일 시가 조기청산
            if variant == "F" and k + 1 < n and bearish_regular(h, R, k, p):
                exit_px = o[k + 1] * (1 - slip)
                exit_dt, exit_reason = dates[k + 1], "bear_div"
                k += 1
                break
            if held >= p["max_hold"]:
                exit_px = c[k] * (1 - slip)
                exit_dt, exit_reason = dates[k], "time"
                break
            k += 1
        if exit_px is None:
            break
        trades.append(dict(sym=df.attrs.get("sym", "?"), entry_dt=dates[j], entry=entry,
                           exit_dt=exit_dt, exit=exit_px, R=(exit_px - entry) / risk,
                           ret=(exit_px - entry) / entry, reason=exit_reason, year=dates[j][:4]))
        armed = None
        i = k + 1
    return trades


def run(variant, p, dfs):
    all_t = []
    for sym, df in dfs.items():
        df.attrs["sym"] = sym
        all_t += backtest_ticker(df, variant, p)
    return all_t


VARIANTS = {"A": "베이스라인(가격전용)", "B": "히든RSI 요구", "C": "약세 다이버전스 거부",
            "D": "OBV 돌파확인", "E": "히든 RSI∧MACD 합의", "F": "약세 다이버전스 조기청산"}


def fmt(m):
    if m.get("n", 0) == 0:
        return "  (트레이드 0)"
    return (f"  n={m['n']:>3}  승률={m['win_rate']:.1%}  기대={m['expectancy_R']:+.3f}R  "
            f"PF={m['profit_factor']:.2f}  총수익={m['equity_linear'] - 1:+.1%}(고정1%)  "
            f"MaxDD={m['max_dd']:.1%}  익절/손절/시간={m['tgt_hits']}/{m['stop_hits']}/{m['time_hits']}")


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
        sys.exit(1)

    print("=" * 66)
    print(f"변형별 성적 (rr={P['rr']}, pivot_k={P['pivot_k']}, div_recent={P['div_recent']}, slip=5bp)")
    print("=" * 66)
    results = {}
    for vkey, label in VARIANTS.items():
        results[vkey] = run(vkey, P, dfs)
        print(f"[{vkey}] {label}:")
        print(fmt(metrics(results[vkey])))

    print("\n─ per-year 기대값(R) [트레이드수] ─")
    pys = {vk: per_year(t) for vk, t in results.items()}
    years = sorted(set().union(*[set(py) for py in pys.values()]))
    for y in years:
        row = f"  {y}: "
        for vk in VARIANTS:
            cnt, e = pys[vk].get(y, (0, 0.0))
            row += f" {vk} {e:+.2f}[{cnt:>2}]"
        print(row)

    print("\n─ div_recent 스윕 (B·C·F, plateau 확인) ─")
    for dr in (5, 10, 15, 20):
        pp = dict(P, div_recent=dr)
        parts = []
        for vk in ("B", "C", "F"):
            m = metrics(run(vk, pp, dfs))
            parts.append(f"{vk} {m.get('expectancy_R', 0):+.3f}R n{m.get('n', 0)}")
        print(f"  div_recent={dr:>2}:  " + "  |  ".join(parts))

    print("\n─ pivot_k 스윕 (B, 피벗 민감도) ─")
    for pk in (2, 3, 4):
        m = metrics(run("B", dict(P, pivot_k=pk), dfs))
        print(f"  pivot_k={pk}:  B 기대{m.get('expectancy_R', 0):+.3f}R PF{m.get('profit_factor', 0):.2f} n{m.get('n', 0)}")

    print("\n─ 2× 슬리피지 스트레스 (slip=10bp) ─")
    pp = dict(P, slippage_bps=10.0)
    for vk in VARIANTS:
        m = metrics(run(vk, pp, dfs))
        print(f"  [{vk}] 기대{m.get('expectancy_R', 0):+.3f}R PF{m.get('profit_factor', 0):.2f} n{m.get('n', 0)}")

    print("\n판정 가이드: 채택 = A 대비 기대값·PF 동시 개선 + n 급감 없음(표본 붕괴=과적합 신호)"
          "\n           + div_recent/pivot_k plateau(단일 지점 승리는 기각) + 스트레스 생존.")
