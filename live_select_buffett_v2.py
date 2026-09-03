"""버핏형 v2 (paper 페르소나 전용) — ROIC 중심 품질 · 현금흐름 가치 · 섹터중립 랭킹.

⚠️ buffett(v1) 과 **12주 A/B 병행** 실험군. v1 은 대조군이라 동결한다 — 그래서 이 모듈은
v1 을 재사용·파라미터화하지 않고 흐름을 복제한다(공유하면 v2 튜닝이 대조군을 오염시킨다).
  ponytail: 의도적 중복. 실험 종료(승자 채택) 시 진 쪽을 삭제하고 하나로 합칠 것.

v1 대비 선정 로직만 다르다(A/B 격리 — universe·top_n·vol_target·reselect·시드는 v1 과 동일):
  ① 하드컷 → 랭킹 페널티. 컷은 적자(마진<0)·극단 PE(>60) 배제만 남기고, v1 의 PE≤25·마진≥8%
     는 quality_value_score_v2 의 연속 감점으로 이전. v1 은 두 컷의 곱이 저마진 고ROE 업종과
     무형자산 복리우량주를 통째로 탈락시켰다.
  ② 품질축 = ROIC 중심(ROE·마진 보조) — ROE 는 레버리지로 부풀어 금융주에서 괴리가 크다.
  ③ 가치축 = FCF·이익 수익률(PE·PB 제외) — 부호역전(자본잠식) 함정이 구조적으로 사라진다.
  ④ 섹터중립 z — 섹터 표본이 얇으면 수축(fmp_factors._z_sector). 섹터 조회는 profile
     엔드포인트라 종목당 +1 콜(스크린 통과분에만, 준정적이라 캐시로 상각).
"""
import fmp_factors as ff
from logsetup import get_logger

_log = get_logger("live_select_buffett_v2")


def _load_piotroski():
    """A엔진 Piotroski 로더 — v1 과 동일(대조군과 같은 veto 를 써야 A/B 차이가 선정축에만 남는다)."""
    try:
        from live_select_canslim import piotroski, _is_value_trap
        return piotroski, _is_value_trap
    except Exception:
        return None, None


def select(prices, lookback=252, top_n=5, pool=15,
           min_margin=0.0, max_pe=60.0, max_debt_equity=None,
           min_market_cap=None, max_market_cap=None, value_trap_gate=False, fmp=None, **_):
    # 후보 생성은 v1 과 완전 동일(저변동 + 구조적 하락 배제) — A/B 차이를 채점 단계에만 남기려면
    # 풀 생성이 변인이 되면 안 된다. fill_method=None: 결측 ffill 이 변동성을 과소평가(v1 과 동일).
    rets = prices.pct_change(fill_method=None)
    vol = rets.tail(lookback).std().dropna()
    n = len(prices)
    base = prices.iloc[max(0, n - lookback)]
    trend = prices.iloc[-1] / base - 1.0
    cand_pool = [t for t in vol.sort_values().index if not (trend.get(t, 0.0) <= -0.20)][:pool]

    # 하드컷은 '적자·극단 PE' 만 — 나머지 판단은 랭킹으로 넘긴다. require_fields 는 v1 과 동일하게
    # 유지(마진·PE 결측이 NaN 비교로 조용히 통과해 '검증됨' 으로 둔갑하는 누수 차단은 전략축이
    # 아니라 정합성 수정이라 양 arm 공통이어야 한다).
    snap, passed, fails, missing, screen_degraded = ff.snapshot_and_screen(
        cand_pool, fmp, screen_kwargs=dict(min_net_margin=min_margin, max_pe=max_pe,
                                           max_debt_equity=max_debt_equity,
                                           min_market_cap=min_market_cap, max_market_cap=max_market_cap,
                                           require_fields=("net_margin", "pe")))

    passed_in_snap = [t for t in passed if t in snap.index]
    # 섹터는 스크린 통과분에만 조회(탈락분까지 부르면 콜 낭비). 실패=None → 전역 z 폴백.
    sector = ff.sectors(passed_in_snap, fmp) if passed_in_snap else None
    qv = ff.quality_value_score_v2(snap.loc[passed_in_snap], sector) if passed_in_snap else None
    ranked = list(qv.index) if qv is not None else list(passed)
    unscored = [] if qv is not None else list(passed)
    eligible = ranked + missing

    # ── Piotroski value trap veto (opt-in) — v1 과 동일 로직 ──────
    vetoed, pio, veto_unavailable = [], {}, False
    if value_trap_gate:
        piotroski, is_trap = _load_piotroski()
        if piotroski is None:
            veto_unavailable = True
        else:
            kept = []
            for t in eligible:                               # lazy — top_n 채우면 중단
                if len(kept) >= top_n:
                    break
                try:
                    f = piotroski(t)
                except Exception as e:
                    _log.warning("piotroski 스킵 %s: %s", t, e)
                    f = {}
                pio[t] = (f or {}).get("score")
                if is_trap(f):
                    vetoed.append(t)
                    continue
                kept.append(t)
            eligible = kept

    final = eligible[:top_n]
    weights = ff.equal_weight(final)
    scores = {t: round(float(qv.get(t, 0.0)), 2) for t in final if qv is not None and t in qv.index}

    reasons = ff.degraded_reasons(cand_pool, missing, final, unscored)
    # 섹터 전량 결측 = 섹터중립이 조용히 무효화된 상태(전역 z 로 동작) — 관측 가능하게 표면화.
    sector_missing = [t for t in passed_in_snap if sector is None or not sector.get(t)]
    if passed_in_snap and len(sector_missing) == len(passed_in_snap):
        reasons = reasons + ["섹터 전량 결측 — 섹터중립 미적용(전역 z 폴백)"]
    if reasons:
        _log.warning("buffett_v2 선정 degrade: %s", " | ".join(reasons))

    return weights, {
        "strategy": "buffett_v2", "candidates": cand_pool, "fails": fails, "missing": missing,
        "final_missing": [t for t in final if t in missing],
        "screen_degraded": bool(screen_degraded or reasons), "degraded_reasons": reasons,
        "final": final, "scores": scores,
        "excluded_value_trap": vetoed, "piotroski": pio, "veto_unavailable": veto_unavailable,
        "sector_missing": sector_missing,
        "sectors": {t: sector.get(t) for t in passed_in_snap} if sector is not None else {},
        "momentum_only": [],       # 모멘텀 전략 아님 — v1 과 동일(selection_review 차원 일관)
    }
