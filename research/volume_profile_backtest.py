"""오프라인 연구 백테스트 — 데이터 게이트로 미실증이던 볼륨 기법 3종(매물대·VWAP·OBV매집).

배경: chartist 이식(316461a) 때 매물대(HVN/POC)·VWAP 은 "Toss 거래량 미제공"으로 백테스트
      없이 스킵됐다(성적 기각이 아니라 데이터 게이트). KIS 피드(f803b9c)로 라이브 실현이
      가능해졌으므로, 이식 여부를 *일봉 합산 거래량*(yfinance = 전체 테이프)으로 먼저 실증.
      chartist_volume_backtest(볼륨확인 기각)·divergence_backtest(6변형 기각)와 동일 방법론.

변형(베이스라인 = chartist S/R 되돌림, 가격전용):
  A   베이스라인      — divergence_backtest A 와 동일
  P1  HVN 돌파검증    — 무장 시 돌파레벨이 매물대 상위 bin(HVN)에 속할 때만 (5편: 위치 강화.
                        저매물(LVN) 돌파는 지지력 없는 레벨 = 되돌림 신뢰 낮다는 가설)
  V1  VWAP 위 진입    — 되돌림 확인 시 종가 > 롤링 VWAP(20d, OHLC4 가중 — data.vwap 컨벤션).
                        기관 평단 위 = 수요 우위 필터
  V2  VWAP 합류       — 되돌림 레벨과 VWAP 가 1% 이내 합류(confluence)일 때만 진입
  O1  OBV 매집 사전   — 무장 시 직전 M일 가격 횡보(레인지<10%) AND OBV 상승(강의: OBV↑+가격
                        횡보=매집) — 매집 후 돌파만 신뢰

매물대 근사: 일봉 [low,high] 에 그날 거래량 균등 분배 → lookback 60d 히스토그램(40 bins).
      HVN = 볼륨 상위 1/3 bin. 일중 분포를 모르는 근사지만 다일 매물대(존 식별)로는 표준 기법.
판정: A 대비 기대값·PF 동시 개선 + n 급감 없음 + 파라미터 plateau + 2×슬리피지 생존.
"""
import sys, os
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data  # noqa: E402
from chartist_volume_backtest import sma, rsi, bull_reversal, metrics, per_year  # noqa: E402
from divergence_backtest import obv_series, WATCHLIST, fmt  # noqa: E402

START, END = "2016-01-01", date.today().isoformat()

P = dict(sr_lookback=30, ma_bars=20, rsi_bars=14, rsi_max=72.0,
         breakout_buf=0.002, retest_tol=0.005, retest_max=10,
         stop_buf=0.005, rr=2.0, max_hold=40,
         vp_lookback=60, vp_bins=40, hvn_frac=1 / 3,       # 매물대(P1)
         vwap_bars=20, confluence_tol=0.01,                # VWAP(V1·V2)
         accum_days=15, flat_pct=0.10,                     # OBV 매집(O1)
         slippage_bps=5.0, risk_per_trade=0.01)


# ───────── 볼륨 파생 시리즈/헬퍼 (전부 i 이전 데이터만 — 무-lookahead) ─────────
def rolling_vwap(o, h, l, c, v, i, n):
    """롤링 n일 VWAP — 전형가 (O+H+L+C)/4 가중(data.vwap 컨벤션). i 포함(신호=당일 종가 시점)."""
    if i - n + 1 < 0:
        return None
    num = den = 0.0
    for j in range(i - n + 1, i + 1):
        tp = (o[j] + h[j] + l[j] + c[j]) / 4.0
        num += tp * v[j]
        den += v[j]
    return num / den if den > 0 else None


def volume_profile(h, l, v, i, lookback, nbins):
    """[i-lookback, i) 일봉 volume-at-price — 일중 균등분배 근사. (하한, bin폭, bins) 또는 None."""
    lo = min(l[i - lookback:i])
    hi = max(h[i - lookback:i])
    if hi <= lo:
        return None
    w = (hi - lo) / nbins
    bins = [0.0] * nbins
    for j in range(i - lookback, i):
        b0 = max(0, min(nbins - 1, int((l[j] - lo) / w)))
        b1 = max(0, min(nbins - 1, int((h[j] - lo) / w)))
        share = v[j] / (b1 - b0 + 1)
        for b in range(b0, b1 + 1):
            bins[b] += share
    return lo, w, bins


def in_hvn(level, prof, hvn_frac):
    """level 가격이 볼륨 상위 hvn_frac bin(고매물대)에 속하나."""
    lo, w, bins = prof
    idx = max(0, min(len(bins) - 1, int((level - lo) / w)))
    k = max(1, int(len(bins) * hvn_frac))
    thresh = sorted(bins, reverse=True)[k - 1]
    return bins[idx] >= thresh


def backtest_ticker(df, variant, p):
    o = df["Open"].tolist(); h = df["High"].tolist()
    l = df["Low"].tolist(); c = df["Close"].tolist(); v = df["Volume"].tolist()
    dates = [d.date().isoformat() for d in df.index]
    n = len(c)
    O = obv_series(c, v)
    trades = []
    warm = max(p["sr_lookback"], p["ma_bars"], p["rsi_bars"] + 1,
               p["vp_lookback"], p["vwap_bars"], p["accum_days"] + 1) + 1
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
            if broke and variant == "P1":                     # 돌파레벨이 고매물대(HVN)일 때만
                prof = volume_profile(h, l, v, i, p["vp_lookback"], p["vp_bins"])
                gate = prof is not None and in_hvn(resistance, prof, p["hvn_frac"])
            if broke and variant == "O1":                     # 매집(가격 횡보 + OBV 상승) 후 돌파만
                m = p["accum_days"]
                rng = (max(c[i - m:i]) - min(c[i - m:i])) / c[i]
                gate = rng < p["flat_pct"] and O[i - 1] > O[i - 1 - m]
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
        rv = rsi(c, i, p["rsi_bars"])
        if not (near and bull_reversal(o, h, l, c, i) and (rv is None or rv < p["rsi_max"])):
            i += 1
            continue
        # ── 되돌림 확인 시점의 VWAP 게이트 ──
        if variant in ("V1", "V2"):
            vw = rolling_vwap(o, h, l, c, v, i, p["vwap_bars"])
            if vw is None:
                i += 1
                continue
            if variant == "V1" and not (c[i] > vw):           # 기관 평단(VWAP) 위 수요 우위만
                i += 1
                continue
            if variant == "V2" and not (abs(level - vw) / level < p["confluence_tol"]):
                i += 1
                continue                                      # 레벨·VWAP 합류만

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


# ───────── 스윙 섹션 — O1 의 의미상 정합 적용처는 livermore_swing(20세션 피벗) ─────────
# 위 A~O1 은 되돌림 진입(chartist형) 위에서의 검증. O1(매집 사전조건)은 *일봉 스케일 돌파*를
# 게이트하는 가설이라, 실전 대응물은 chartist 의 30분 마이크로 돌파가 아니라 livermore_swing 의
# 20세션 피벗 돌파다. 스윙 엔진(피벗 돌파 직진입 + hw 트레일 8%, 피라미딩 생략)으로 직접 A/B.
def backtest_swing(df, use_accum, p):
    o = df["Open"].tolist(); h = df["High"].tolist()
    l = df["Low"].tolist(); c = df["Close"].tolist(); v = df["Volume"].tolist()
    dates = [d.date().isoformat() for d in df.index]
    n = len(c)
    O = obv_series(c, v)
    trades = []
    piv, ma_n = p.get("swing_pivot", 20), p["ma_bars"]
    trail = p.get("swing_trail", 0.08)
    warm = max(piv, ma_n, p["accum_days"] + 1) + 1
    slip = p["slippage_bps"] / 1e4

    i = warm
    while i < n - 1:
        ma = sma(c, i, ma_n)
        pivot = max(h[i - piv:i])
        uptrend = ma is not None and c[i] > ma
        up_day = c[i] > c[i - 1]                          # thrust 프록시(룰과 동일 강등)
        broke = c[i] > pivot * (1 + p["breakout_buf"])
        gate = True
        if use_accum and broke:
            m = p["accum_days"]
            rng = (max(c[i - m:i]) - min(c[i - m:i])) / c[i]
            gate = rng < p["flat_pct"] and O[i - 1] > O[i - 1 - m]
        if not (uptrend and up_day and broke and gate):
            i += 1
            continue
        j = i + 1
        entry = o[j] * (1 + slip)
        risk = entry * trail                              # R 정규화 = 초기 트레일 폭(스윙 룰과 동일)
        hw = entry
        exit_px = exit_dt = None
        k = j
        while k < n:
            hw = max(hw, c[k])
            if c[k] <= hw * (1 - trail):                  # 트레일 발화(종가 판정) → 익일 시가 청산
                if k + 1 < n:
                    exit_px, exit_dt = o[k + 1] * (1 - slip), dates[k + 1]
                    k += 1
                break
            k += 1
        if exit_px is None:
            break                                         # 데이터 끝 미청산 — 제외(생존편향 방지)
        trades.append(dict(sym=df.attrs.get("sym", "?"), entry_dt=dates[j], entry=entry,
                           exit_dt=exit_dt, exit=exit_px, R=(exit_px - entry) / risk,
                           ret=(exit_px - entry) / entry, reason="trail", year=dates[j][:4]))
        i = k + 1
    return trades


def run_swing(use_accum, p, dfs):
    all_t = []
    for sym, df in dfs.items():
        df.attrs["sym"] = sym
        all_t += backtest_swing(df, use_accum, p)
    return all_t


VARIANTS = {"A": "베이스라인(가격전용)", "P1": "HVN 돌파검증(매물대)", "V1": "VWAP 위 진입",
            "V2": "레벨·VWAP 합류", "O1": "OBV 매집 사전조건"}

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
    print(f"변형별 성적 (vp={P['vp_lookback']}d/{P['vp_bins']}bins/top{P['hvn_frac']:.2f}, "
          f"vwap={P['vwap_bars']}d, accum={P['accum_days']}d, slip=5bp)")
    print("=" * 66)
    results = {}
    for vk, label in VARIANTS.items():
        results[vk] = run(vk, P, dfs)
        print(f"[{vk}] {label}:")
        print(fmt(metrics(results[vk])))

    print("\n─ per-year 기대값(R) [트레이드수] ─")
    pys = {vk: per_year(t) for vk, t in results.items()}
    years = sorted(set().union(*[set(py) for py in pys.values()]))
    for y in years:
        row = f"  {y}: "
        for vk in VARIANTS:
            cnt, e = pys[vk].get(y, (0, 0.0))
            row += f" {vk} {e:+.2f}[{cnt:>2}]"
        print(row)

    print("\n─ P1 스윕 (hvn_frac × vp_lookback, plateau 확인) ─")
    for hf in (0.25, 1 / 3, 0.5):
        for vl in (40, 60, 90):
            m = metrics(run("P1", dict(P, hvn_frac=hf, vp_lookback=vl), dfs))
            print(f"  hvn={hf:.2f} vp={vl}d:  기대{m.get('expectancy_R', 0):+.3f}R "
                  f"PF{m.get('profit_factor', 0):.2f} n{m.get('n', 0)}")

    print("\n─ V1/V2 스윕 (vwap_bars) ─")
    for vb in (10, 20, 30):
        m1 = metrics(run("V1", dict(P, vwap_bars=vb), dfs))
        m2 = metrics(run("V2", dict(P, vwap_bars=vb), dfs))
        print(f"  vwap={vb}d:  V1 {m1.get('expectancy_R', 0):+.3f}R n{m1.get('n', 0)}"
              f"   |  V2 {m2.get('expectancy_R', 0):+.3f}R n{m2.get('n', 0)}")

    print("\n─ O1 스윕 (accum_days × flat_pct) ─")
    for ad in (10, 15, 20):
        for fp in (0.08, 0.10, 0.15):
            m = metrics(run("O1", dict(P, accum_days=ad, flat_pct=fp), dfs))
            print(f"  accum={ad}d flat<{fp:.0%}:  기대{m.get('expectancy_R', 0):+.3f}R n{m.get('n', 0)}")

    print("\n─ 2× 슬리피지 스트레스 (slip=10bp) ─")
    pp = dict(P, slippage_bps=10.0)
    for vk in VARIANTS:
        m = metrics(run(vk, pp, dfs))
        print(f"  [{vk}] 기대{m.get('expectancy_R', 0):+.3f}R PF{m.get('profit_factor', 0):.2f} n{m.get('n', 0)}")

    print("\n" + "=" * 66)
    print("스윙 섹션 — livermore_swing(20세션 피벗 돌파+트레일 8%)에 O1 적용")
    print("=" * 66)
    SA = run_swing(False, P, dfs)
    SO = run_swing(True, P, dfs)
    print("[S-A] 스윙 베이스라인:"); print(fmt(metrics(SA)))
    print("[S-O1] + OBV 매집 사전조건:"); print(fmt(metrics(SO)))
    print("\n─ S per-year ─")
    pa, po = per_year(SA), per_year(SO)
    for y in sorted(set(pa) | set(po)):
        na, ea = pa.get(y, (0, 0.0)); no_, eo = po.get(y, (0, 0.0))
        print(f"  {y}:  S-A {ea:+.2f}R[{na:>2}]   S-O1 {eo:+.2f}R[{no_:>2}]")
    print("\n─ S-O1 스윕 (accum × flat) ─")
    for ad in (10, 15, 20):
        for fp in (0.08, 0.10, 0.15):
            m = metrics(run_swing(True, dict(P, accum_days=ad, flat_pct=fp), dfs))
            print(f"  accum={ad}d flat<{fp:.0%}:  기대{m.get('expectancy_R', 0):+.3f}R n{m.get('n', 0)}")
    print("\n─ S 2× 슬리피지 ─")
    pp = dict(P, slippage_bps=10.0)
    ma_, mo_ = metrics(run_swing(False, pp, dfs)), metrics(run_swing(True, pp, dfs))
    print(f"  S-A 기대{ma_.get('expectancy_R', 0):+.3f}R n{ma_.get('n', 0)}  |  "
          f"S-O1 기대{mo_.get('expectancy_R', 0):+.3f}R n{mo_.get('n', 0)}")

    print("\n판정 가이드: 채택 = 베이스라인 대비 기대값·PF 동시 개선 + n 급감 없음 + plateau + 스트레스 생존."
          "\n           통과 변형도 룰 배선은 flag(기본 off) — 장중 재검증 후 활성.")
