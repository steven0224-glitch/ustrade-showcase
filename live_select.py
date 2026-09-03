"""라이브 종목선택 — 모멘텀 랭킹 + FMP 펀더멘털 필터.

⚠️ 라이브 전용. 펀더멘털이 현재 스냅샷뿐(무료티어) → 과거 backtest엔 못 씀.
backtest 는 strategies/cross_momentum (필터 없음), 라이브 선택만 이걸로 보강.

흐름: 모멘텀 상위 pool → 펀더멘털 스크린(적자·고PE 제거) → 최종 top_n 등비중.
데이터 결측(레이트/402) 종목 = 제외 아니라 플래그 (데이터 갭으로 알파 버리지 않음).
"""
import pandas as pd

from strategies import factors as F
import fmp_factors as ff
from logsetup import get_logger

_log = get_logger("live_select")


def select(prices, lookback=126, top_n=3, pool=8,
           min_margin=0.0, max_pe=80.0, max_debt_equity=None,
           min_market_cap=None, max_market_cap=None,
           use_pead=False, pead_weight=0.3, fmp=None,
           use_alpha=False, alpha_weight=0.3, alpha_name="alpha101_032", ohlcv_fn=None):
    """최신 시점 모멘텀+펀더멘털 선택 → (target_weights dict, info dict).

    lookback = 모멘텀 룩백(거래일, skip=21 고정). 기본 126 = 6-1 모멘텀(확정구성).
    min/max_market_cap = 시총 경계(USD, None=무동작) — screen() 으로 전달.
    use_pead = 어닝 서프라이즈(PEAD) 틸트(기본 off). on 이면 후보 모멘텀에 PEAD z-score 를
      pead_weight 만큼 가중해 재랭킹. ⚠️ IC 검증(eval_factor earnings_surprise) 통과 후만 켤 것.
      FMP 어닝 미가용 시 모멘텀-only 로 자동 폴백(거래 중단 없음).
    use_alpha = Alpha Zoo 팩터(alpha_name, 기본 alpha101_032) 틸트(기본 off). on 이면 후보풀 모멘텀에
      alpha z-score 를 alpha_weight 만큼 가중해 재랭킹. use_pead 와 독립(동시=압축, 권장 하나씩).
      ohlcv_fn = 테스트 주입 훅(기본 data.load_ohlcv_panel). 실패 시 모멘텀 순서 유지.
    """
    mom = F.momentum(prices, lookback=lookback, skip=21).iloc[-1].dropna().sort_values(ascending=False)
    candidates = list(mom.head(pool).index)

    alpha_used = False
    if use_alpha and candidates:
        # Alpha Zoo 틸트 — 후보풀 한정 재랭킹. 캐시된 OHLCV 로 팩터 최신값 z-결합. 실패=모멘텀 순서 유지.
        # ⚠️ eval_factor --factor zoo 의 random-control confirmed_alive 통과분만 (alpha101_032=sp100/diversified
        # 양쪽 confirmed·모멘텀과 직교 +0.24·분위단조). load_ohlcv_panel 은 load_panel 과 동일 종목캐시 재사용(추가다운로드 0).
        try:
            from strategies import alpha_zoo as Z
            import data as _data
            start = str(prices.index[0].date())
            end = str((prices.index[-1] + pd.Timedelta(days=1)).date())   # exclusive → 마지막 봉 포함
            panel = (ohlcv_fn or _data.load_ohlcv_panel)(list(prices.columns), start, end)
            ac = Z.compute(alpha_name, panel).iloc[-1].reindex(candidates)
            if ac.notna().any():
                combined = (ff._z(mom.loc[candidates]) * (1.0 - alpha_weight)
                            + ff._z(ac).fillna(0.0) * alpha_weight)
                ranked = list(combined.dropna().sort_values(ascending=False).index)
                if ranked:
                    candidates = ranked
                    alpha_used = True
        except Exception as e:
            _log.warning("alpha 틸트 스킵(모멘텀 폴백): %s", e)

    pead_used = False
    if use_pead and candidates:
        # PEAD 틸트 — 후보풀 한정으로 어닝 패널 조회(호출 bound). 실패/무데이터면 모멘텀 순서 유지.
        try:
            from fmp_factors import earnings_surprise_panel
            pead_latest = earnings_surprise_panel(candidates, prices.index, fmp).iloc[-1]
            if pead_latest.notna().any():
                mom_c = mom.loc[candidates]
                combined = (ff._z(mom_c) * (1.0 - pead_weight)
                            + ff._z(pead_latest.reindex(candidates)).fillna(0.0) * pead_weight)
                ranked = list(combined.dropna().sort_values(ascending=False).index)
                if ranked:
                    candidates = ranked
                    pead_used = True
        except Exception as e:
            _log.warning("PEAD 틸트 스킵(모멘텀 폴백): %s", e)

    # 스냅샷+결측정리(core dropna)+스크린+screen_degraded — 공유 함수(fmp_factors, buffett/wood 와 대칭).
    # U1 — 펀더 데이터 대량 결측(레이트429·키문제)이면 스크린이 사실상 무력화돼 모멘텀만 거래됨.
    # 제외하진 않되(개인용 폴백 허용) 무력화 상태를 플래그 → run_live 가 경보.
    snap, passed, fails, missing, screen_degraded = ff.snapshot_and_screen(
        candidates, fmp, screen_kwargs=dict(min_net_margin=min_margin, max_pe=max_pe,
                                            max_debt_equity=max_debt_equity,
                                            min_market_cap=min_market_cap, max_market_cap=max_market_cap))
    passed_set = set(passed)

    eligible = [t for t in candidates if t in passed_set or t in missing]
    final = eligible[:top_n]
    weights = ff.equal_weight(final)

    return weights, {
        "candidates": candidates,
        "fails": fails,
        "missing": missing,
        "final_missing": [t for t in final if t in missing],   # 최종 선정분 중 결측 교집합(관측용, 로직 무영향)
        "screen_degraded": screen_degraded,
        "pead_used": pead_used,
        "alpha_used": alpha_used,
        "final": final,
        "momentum_only": list(mom.head(top_n).index),   # 순수 모멘텀 카운터팩추얼(PEAD 재랭킹 무관)
    }
