"""장중 청산 로직 — 텔레그램 시그널(A) engine/sell.py 매도룰의 코어를 이식(결정적, 네트워크 0).

진입은 데일리(run_live)가 담당. 이 모듈은 *보유분 빠른 청산*만 — 일봉 MA 레벨 + 실시간가로
장중에 추세 붕괴/손절을 즉시 잡는다. run_exit.py 가 N분마다 호출.

기본(보수) 트리거:
  - 현재가 < 200일선 → 추세 붕괴, 전량 청산 📉
  - 현재가 < 평균매입가 × (1 − stop_pct) → 손절 🛑
opt-in:
  - 현재가 < 50일선 → 단기 약화 (use_50ma)
  - RSI ≥ ob_rsi 이고 상승추세 → 과열 청산 (ob_rsi 지정 시; 전량 청산)

MA/RSI 레벨은 일봉(느림)이지만 현재가가 실시간 → 가격이 장중 MA 를 깨는 순간 트리거된다.
"""
import pandas as pd


def _sma(closes: pd.Series, n: int):
    if closes is None or len(closes) < n:
        return None
    return float(closes.rolling(n).mean().iloc[-1])


def _rsi(closes: pd.Series, n: int = 14):
    if closes is None or len(closes) < n + 1:
        return None
    d = closes.diff()
    up = d.clip(lower=0).rolling(n).mean()
    dn = (-d.clip(upper=0)).rolling(n).mean()
    rs = up / dn.replace(0, 1e-9)
    out = 100 - 100 / (1 + rs)
    out = out.mask((up == 0) & (dn == 0), 50.0)            # 무변동 = 중립 50 (A indicators 와 동일)
    v = out.iloc[-1]
    return None if pd.isna(v) else float(v)


def check_exits(positions, daily_closes, live_prices, *, regime_ma: int = 200,
                stop_pct: float = 0.08, use_50ma: bool = False, ob_rsi: float = None):
    """보유 포지션별 청산 판정. 트리거된 것만 reasons 가 채워진다.

    positions     : list[Position] (symbol, qty, avg_price) — ManagedBroker 가 준 슬리브 보유분
    daily_closes  : {symbol: pd.Series(일봉 종가)}
    live_prices   : {symbol: float} 실시간가
    반환: [{symbol, qty, price, reasons:[...], flags:[...], data_ok:bool}, ...]
    """
    out = []
    for p in positions:
        s = daily_closes.get(p.symbol)
        price = live_prices.get(p.symbol)
        rec = {"symbol": p.symbol, "qty": p.qty, "price": price,
               "reasons": [], "flags": [], "data_ok": True}
        if price is None or not (price > 0):
            rec["data_ok"] = False                                  # 실시간가 결측/0/음수/NaN — price>0 가 NaN·0 차단
            rec["reasons"].append("데이터 부족 — 수동 확인 필요")   # 판정 불가(자동청산 X)
            out.append(rec)
            continue

        # 하드 손절 — 실시간가·평단만 필요(일봉 MA 시리즈 불필요). 일봉 결측/노후와 무관하게 항상 평가.
        # MA 시리즈 가용성에 손절을 결합하면 무관한 일봉 데이터 문제로 -stop_pct 보호 손절이 자동
        # 집행되지 않는다(자본 보호 갭) → 분리해 항상 집행.
        if p.avg_price and p.avg_price > 0 and price < p.avg_price * (1 - stop_pct):
            rec["reasons"].append(f"{stop_pct:.0%} 손절 (${price:.2f} < 매입 ${p.avg_price:.2f})")
            rec["flags"].append("🛑")

        if s is None or len(s) < regime_ma:                         # 일봉 MA/RSI 시리즈 결측·노후 → MA기반 트리거 판정불가
            if not rec["reasons"]:                                  # 손절도 안 걸렸으면 수동확인(자동청산 X)
                rec["data_ok"] = False
                rec["reasons"].append("데이터 부족 — 수동 확인 필요")
            out.append(rec)                                         # 손절이 걸렸으면 data_ok=True → 자동청산(손절은 MA 무관 유효)
            continue

        sma200 = _sma(s, regime_ma)
        sma50 = _sma(s, 50)
        if sma200 is not None and price < sma200:
            rec["reasons"].append(f"200MA 이탈 (${price:.2f} < ${sma200:.2f})")
            rec["flags"].append("📉")
        if use_50ma and sma50 is not None and price < sma50 and (sma200 is None or price >= sma200):
            rec["reasons"].append(f"50MA 이탈 (${price:.2f} < ${sma50:.2f})")
            rec["flags"].append("🟡")
        if ob_rsi is not None:
            rsi = _rsi(s)
            if rsi is not None and rsi >= ob_rsi and sma200 is not None and price > sma200:
                rec["reasons"].append(f"RSI {rsi:.0f} 과열 청산")   # to_exit→run_exit 가 d['qty'] 전량 매도 → 라벨도 '청산'(트림 아님, 실행과 일치)
                rec["flags"].append("🔺")
        out.append(rec)
    return out


def to_exit(decisions):
    """자동 청산 대상만 — data_ok 이고 reasons 있는 것(데이터부족 수동확인은 제외)."""
    return [d for d in decisions if d["data_ok"] and d["reasons"]]
