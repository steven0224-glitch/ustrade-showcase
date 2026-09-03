"""우드형 파괴적 성장 선택 (paper 페르소나 전용) — 고모멘텀·성장프리미엄(고P/S·저배당) 혁신주.

⚠️ 라이브 전용. 무료 FMP 에 매출성장 필드 없음 → 성장을 '고P/S(매출 프리미엄)' 로 프록시하고
'고모멘텀·저배당(성장재투자)' 은 점수항이 아니라 **편입 게이트**로 건다(정통 매출성장 스크린은
유료티어 필요 — 한계 명시). 가치(저PE)는 일부러 안 거름 — 적자 혁신주 허용.
실거래 경로와 독립 — 모의매매 비교용.

흐름: 단기 모멘텀 상위 pool(모멘텀=게이트) → FMP 스냅샷 → 배당 게이트 → 성장점수(고P/S) 상위 top_n 등비중.
"""
import pandas as pd

from strategies import factors as F
import fmp_factors as ff
from logsetup import get_logger

_log = get_logger("live_select_wood")

# 배당 게이트 임계(2026-08-01) — 배당은 z 연속 페널티가 아니라 이진 게이트다. 유니버스의 30여 종이
# 정확히 0 에 몰려 있어 z 가 스파이크가 되고(실측 배당 z폭 4.25 > P/S z폭 3.65), 0.13% 짜리
# 토큰 배당(MRVL)까지 성장주를 랭킹에서 밀어내던 구조적 왜곡을 제거한다.
# 임계 1.5% 근거(growth45 43회 리밸런스 실측): 진짜 인컴주(QCOM 2.49%)는 배제하고 토큰 배당
# 성장주(MU 0.06·MRVL 0.13·GOOGL 0.25·AAPL 0.35·META 0.38·NVDA 0.50·AVGO 0.67·CRM 0.96)는
# 전부 통과 + 게이트 상태전환 0회(1.0% 는 ORCL 이 3회 깜빡여 이력현상이 필요해진다 → 단순 임계 유지).
DIV_GATE = 0.015


def _winsor_top(s):
    """최댓값 1종을 2위값까지 눌러 z 분포 장악을 막는다(P2-A6). 결측/단일값이면 무동작.

    P/S 는 우편향이라 1종이 sd 를 통째로 좌우한다. growth45 pool18 실측(2026-07)에서
    ARM(P/S 49.6)만으로 sd 가 1.29x 팽창해 나머지 17종의 z 폭이 3.39→2.63 으로 눌렸다.
    P/S 500 짜리 1종을 주입한 스트레스에서는 나머지 17종 z 폭이 **0.46 까지 붕괴** —
    P/S 서열이 사실상 소멸하고 배당항이 순위를 가져간다(실측 배당 z폭 4.25 > P/S z폭 3.65).

    경계 선택(43회 리밸런스 실측): 고정상한 clip@25 는 상위 3~6종을 정확히 동점으로 만들어
    (동점 51건) 선정 경계를 입력순으로 무너뜨리고, q95 윈저는 소표본 보간 탓에 최댓값을
    2위와 1위 사이 65% 지점까지만 내려 스트레스에서 z폭 1.84 로 약하다. 2위값 윈저는
    스트레스 z폭 3.10 회복 + 정상구간 서열보존 spearman 0.996 + 무처리대비 멤버십 변경
    2종목/43회. 발생 동점은 1~2위 한 쌍뿐 — 등비중이라 둘 다 편입되므로 무해하다
    (동점이 해로운 지점은 7/8 경계인데 상단 윈저는 거기에 동점을 못 만든다).
    """
    # to_numeric 먼저 — FMP 스키마 드리프트로 문자열이 한 칸 섞이면 컬럼이 object dtype 이 되고
    # nlargest 가 TypeError 로 죽는다(_z 는 내부에서 이미 coerce 하므로 종전엔 노출 안 되던 경로).
    s = pd.to_numeric(s, errors="coerce")
    top2 = s.nlargest(2)
    return s if top2.empty else s.clip(upper=top2.iloc[-1])


def select(prices, lookback=63, top_n=7, pool=14,
           min_margin=0.0, max_pe=80.0, max_debt_equity=None, fmp=None, **_):
    """우드형 성장 선택 → (target_weights dict, info dict).

    min_margin/max_pe/max_debt_equity 는 시그니처만 유지 — live_engine 이 전 전략 공통 kwargs 로
    무조건 전달하므로 받되, 의도적으로 미사용(wood 설계 = 가치 스크린 없음, 적자 혁신주 허용 —
    모듈 docstring 참고). 배선하려면 설계 변경 승인 먼저.
    """
    mom = F.momentum(prices, lookback=lookback, skip=10).iloc[-1].dropna().sort_values(ascending=False)
    cand_pool = list(mom.head(pool).index)

    # 스냅샷+결측정리(core dropna) — 공유 함수(fmp_factors, momentum/buffett 와 대칭). screen_kwargs
    # 미전달 = 하드스크린 생략(가치 스크린 없음이 설계). core dropna 는 여전히 적용 — ratios_ttm 만
    # 실패해도 ps/div_yield(같은 엔드포인트 소스)가 NaN 인데, 그대로면 아래 fillna(mom_z) 가 결측을
    # 모멘텀값으로 위장시켜 성장점수인 척 편입되던 것 차단(buffett 대칭, 선정결과 변경 가능한 결함수정).
    snap, _passed, _fails, missing, screen_degraded = ff.snapshot_and_screen(cand_pool, fmp)

    # 배당 게이트 — '성장 재투자' 조건을 점수 감점이 아니라 편입 자격으로 건다(DIV_GATE 주석 참고).
    # 스냅샷 *뒤*에 놓는 건 취향이 아니라 강제다: div_yield 는 snapshot(cand_pool) 산출물이라
    # 절단 전에 걸려면 유니버스 45종 스냅샷(FMP 콜 2.5배)이 필요하다 — A7 (b)안을 기각한 그 비용.
    # 결측/구캐시(컬럼 부재)는 통과 — 데이터갭으로 종목을 죽이지 않는 이 repo 정책과 일관하고,
    # 종전 div_yield.fillna(0.0)(=무배당 취급)과도 같은 방향이다. to_numeric 은 스키마 드리프트 방어.
    in_snap = [t for t in cand_pool if t in snap.index]
    dv = (pd.to_numeric(snap["div_yield"].reindex(in_snap), errors="coerce")
          if in_snap and "div_yield" in snap.columns else None)
    div_gated = [] if dv is None else [t for t in in_snap if pd.notna(dv[t]) and dv[t] > DIV_GATE]
    if div_gated:
        in_snap = [t for t in in_snap if t not in set(div_gated)]

    # 성장 점수 — 고P/S(매출 프리미엄) 단일항. 가치 스크린 없음(적자 혁신 허용).
    # 모멘텀은 위 cand_pool 절단(45→18)에서 이미 게이트로 쓰였고, 점수식에는 안 들어간다(P2-A7).
    # 이유: 모멘텀 상위 18 로 자른 뒤 z(mom) 을 다시 더하면 같은 신호를 두 번 센다. 실측
    # (growth45, 2025-09~2026-07, 시변 P/S) 회전 대조 — 현행 주1회 64·월1회 29 / z(mom)제거
    # 61·27 / 전유니버스 스코어링 61·31. 회전 최소는 z(mom) 제거이고 FMP 스냅샷도 18 유지
    # (전유니버스안은 45 = 무료티어 쿼터 2.5배 → A4/A5 실패모드를 스스로 부른다).
    # 정체성 유지: 모멘텀 top18 게이트는 그대로라 '파괴적 성장 모멘텀' 은 여전히 편입 조건.
    scores, ranked, unscored = {}, [], []
    if in_snap:
        sub = snap.loc[in_snap]
        mom_z = ff._z(mom.reindex(in_snap))       # 점수항 아님 — 아래 결측 폴백 전용
        gs = ff._z(_winsor_top(sub["ps"]))                                        # P2-A6
        unscored = list(gs.index[gs.isna()])       # 성장점수 결측 → 모멘텀 폴백(조용한 degrade, A4 로 표면화)
        gs = gs.fillna(mom_z).sort_values(ascending=False)
        ranked = list(gs.index)
        scores = {t: round(float(gs.get(t, 0.0)), 2) for t in ranked}
    eligible = ranked + missing                              # 점수분 우선, 데이터갭(모멘텀 순) 뒤
    final = eligible[:top_n]
    weights = ff.equal_weight(final)
    scores = {t: scores[t] for t in final if t in scores}

    # P2-A4 — degrade 사유 명시. 결측률이 30% 임계 미만이어도 실매수분이 미검증/미채점이면
    # 플래그를 켜 run_live 의 기존 screen_degraded 알림이 반드시 발화하게 한다(거래 정책 불변).
    reasons = ff.degraded_reasons(cand_pool, missing, final, unscored)
    if reasons:
        _log.warning("wood 선정 degrade: %s", " | ".join(reasons))

    return weights, {
        "strategy": "wood", "candidates": cand_pool, "missing": missing,
        "final_missing": [t for t in final if t in missing],   # 최종 선정분 중 결측 교집합(관측용, 로직 무영향)
        "screen_degraded": bool(screen_degraded or reasons), "degraded_reasons": reasons,
        "div_gated": div_gated,                                # 배당 게이트 배제분(관측용)
        "final": final, "scores": scores,
        "momentum_only": cand_pool[:top_n],
    }
