"""역DCF 밸류에이션 — "지금 이 주가가 성립하려면 회사가 얼마나 성장해야 하는가"를 역산.

일반 DCF 는 성장률을 넣어 적정가를 구하지만, 역DCF 는 거꾸로: 현재 시총을 입력으로 받아
그것을 정당화하는 **시장 내재 성장률**을 푼다. 그 값을 회사의 **과거 실제 성장률**과 나란히
놓으면 "가격이 요구하는 성장 vs 실제 달성한 성장"의 갭이 나온다(양수 클수록 완벽하게 가격됨).

순수 함수 — 데이터 페치·FMP 콜 없음. 입력(market_cap, fcf0, wacc, historical_g)은 호출측이
`fmp_factors.snapshot()`(market_cap·fcf_yield) + `engine.funda.canslim_tag()`(rev_g) 로 조달한다.
stdlib 만 사용 — VM 무의존 실행(desk 툴과 동일 규율).

부호 규약: 이 모듈은 "값이 클수록 매수"가 아니다. valuation_gap 이 **작을수록(음수)** 저평가=매수.
선정 배선측이 부호를 뒤집어 tilt/gate 로 쓴다.
"""
from __future__ import annotations

# 2단계 DCF 기본값 — 하우스 캘리브레이션 knob. 물리 상수 아님, 튜닝 대상.
DEFAULT_YEARS = 10           # 고성장 국면 길이(년)
DEFAULT_TERMINAL_G = 0.025   # 영구성장률 ≈ 장기 명목 GDP
DEFAULT_WACC = 0.09          # 하우스 기본 할인율(종목별 beta 미조달 — 상수 + 민감도밴드로 보완)
_G_LO, _G_HI = -0.50, 1.00   # 내재성장률 탐색 범위(연율 -50%~+100%)


def _pv(g: float, market_cap: float, fcf0: float, wacc: float,
        years: int, terminal_growth: float) -> float:
    """성장률 g 가정 시 2단계 DCF 현재가치. market_cap 인자는 미사용(시그니처 대칭용 아님) —
    호출측이 pv(g) - market_cap 의 부호를 보고 이분법하므로 여기선 순수 PV 만 반환한다."""
    # 고성장 구간: Σ_{t=1..N} fcf0(1+g)^t / (1+w)^t
    pv = 0.0
    cf = fcf0
    disc = 1.0
    for _ in range(years):
        cf *= (1.0 + g)          # fcf0(1+g)^t
        disc *= (1.0 + wacc)     # (1+w)^t
        pv += cf / disc
    # 터미널: fcf_{N+1}/(w - tg) 를 N 시점으로 할인. cf 는 현재 fcf0(1+g)^N.
    terminal_cf = cf * (1.0 + terminal_growth)
    pv += (terminal_cf / (wacc - terminal_growth)) / disc
    return pv


def implied_growth(market_cap: float, fcf0: float, wacc: float = DEFAULT_WACC,
                   years: int = DEFAULT_YEARS, terminal_growth: float = DEFAULT_TERMINAL_G,
                   tol: float = 1e-5, max_iter: int = 200):
    """현재 시총을 정당화하는 내재 연성장률 g 를 이분법으로 역산.

    반환: (g, flag). flag ∈ {"ok","exceeds_model","below_model"}.
      - exceeds_model: 탐색 상한(+100%)에서도 PV < 시총 → 모델이 설명 못 할 만큼 비쌈(g=상한 클램프)
      - below_model : 탐색 하한(-50%)에서도 PV > 시총 → 모델 대비 쌈(g=하한 클램프)
    입력 부적격이면 (None, 사유). PV 는 g 에 단조증가라 이분법 수렴 보장.
    """
    if not (market_cap and market_cap > 0):
        return None, "market_cap<=0"
    if not (fcf0 and fcf0 > 0):
        # FCF 음수/영: 역DCF 정의 불가(음수 현금흐름을 성장시켜 양의 가치를 만들 수 없음).
        # 적자·현금소각 기업은 이 축으로 평가 안 함 — 호출측이 중립(결측) 처리.
        return None, "fcf0<=0"
    if wacc <= terminal_growth:
        # 터미널 분모 (w-tg)<=0 → 발산/음수. 할인율이 영구성장보다 커야 유의미.
        return None, "wacc<=terminal_growth"

    lo, hi = _G_LO, _G_HI
    pv_lo = _pv(lo, market_cap, fcf0, wacc, years, terminal_growth)
    pv_hi = _pv(hi, market_cap, fcf0, wacc, years, terminal_growth)
    if pv_hi < market_cap:
        return hi, "exceeds_model"      # 최대 성장으로도 못 미침 = priced beyond model
    if pv_lo > market_cap:
        return lo, "below_model"        # 최소 성장에서도 초과 = cheap

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        pv_mid = _pv(mid, market_cap, fcf0, wacc, years, terminal_growth)
        if abs(pv_mid - market_cap) < tol * market_cap:
            return mid, "ok"
        if pv_mid < market_cap:         # PV 부족 → 더 높은 성장 필요
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi), "ok"        # max_iter 소진 — 구간 중점(tol 근방)


def implied_growth_band(market_cap: float, fcf0: float, wacc: float = DEFAULT_WACC,
                        wacc_delta: float = 0.015, **kw):
    """WACC ±delta 에서 내재성장률 3점 (저WACC, 중심, 고WACC). 하나의 숫자에 의존하지 않기 위함.

    WACC 가 높을수록 같은 주가를 정당화하는 데 더 높은 성장이 필요 → g 는 WACC 에 증가.
    따라서 밴드 = (g@wacc-delta, g@wacc, g@wacc+delta) 오름차순. 밴드가 넓으면 WACC 민감.
    성분 중 하나라도 None(부적격)이면 (None, None, None) — 호출측이 결측 처리.
    """
    g_lo, _ = implied_growth(market_cap, fcf0, wacc - wacc_delta, **kw)
    g_mid, flag = implied_growth(market_cap, fcf0, wacc, **kw)
    g_hi, _ = implied_growth(market_cap, fcf0, wacc + wacc_delta, **kw)
    if None in (g_lo, g_mid, g_hi):
        return None, None, None
    return g_lo, g_mid, g_hi


def valuation_gap(implied_g, historical_g):
    """내재성장률 - 과거실제성장률. 양수 클수록 완벽하게 가격됨(고평가), 음수는 저평가.

    "시장이 요구하는 성장률을 회사가 실제로 낸 적 있는가"의 정량화.
    둘 중 하나라도 None 이면 None(비교 불가 — 결측).
    """
    if implied_g is None or historical_g is None:
        return None
    return implied_g - historical_g


def _selfcheck() -> None:
    """assert 자체검증 — stdlib 만. `python strategies/reverse_dcf.py` 로 실행."""
    # ① PV 는 g 에 단조증가
    mc, fcf = 1_000_000.0, 50_000.0   # fcf_yield 5%
    pvs = [_pv(g, mc, fcf, 0.09, 10, 0.025) for g in (-0.2, 0.0, 0.1, 0.3, 0.6)]
    assert all(a < b for a, b in zip(pvs, pvs[1:])), f"PV 비단조: {pvs}"

    # ② 라운드트립 — g* 로 시총을 만들면 implied_growth 가 g* 를 복원
    for g_star in (0.05, 0.12, 0.25):
        mc_star = _pv(g_star, 1.0, 0.04, 0.09, 10, 0.025)   # fcf0=0.04(4% 수익률), mcap=PV
        g_rec, flag = implied_growth(mc_star, 0.04, 0.09)
        assert flag == "ok", f"g*={g_star} flag={flag}"
        assert abs(g_rec - g_star) < 1e-3, f"라운드트립 실패 g*={g_star} 복원={g_rec:.4f}"

    # ③ WACC 밴드 오름차순 (g 는 WACC 에 증가)
    g_lo, g_mid, g_hi = implied_growth_band(1_000_000.0, 50_000.0, 0.09, 0.015)
    assert g_lo <= g_mid <= g_hi, f"밴드 비오름차순: {g_lo},{g_mid},{g_hi}"

    # ④ 엣지 — 적자 FCF·WACC<=터미널·시총<=0 은 None + 사유
    assert implied_growth(1e6, -100.0, 0.09) == (None, "fcf0<=0")
    assert implied_growth(1e6, 5e4, 0.02) == (None, "wacc<=terminal_growth")
    assert implied_growth(0.0, 5e4, 0.09) == (None, "market_cap<=0")

    # ⑤ 클램프 플래그 — +100% 성장 상한이 넓어 웬만한 저수익률은 ok 로 풀린다. 극단만 클램프:
    #   fcf0=mcap×1e-6(사실상 무FCF megacap): 최대 성장으로도 못 미침 → exceeds_model(g=상한)
    g_x, flag_x = implied_growth(1_000_000.0, 1.0, 0.09)
    assert flag_x == "exceeds_model" and g_x == _G_HI, f"exceeds flag={flag_x} g={g_x}"
    #   fcf0=mcap×2(수익률 200%, 비현실적 딥밸류): 최소 성장에서도 초과 → below_model(g=하한)
    g_c, flag_c = implied_growth(1_000_000.0, 2_000_000.0, 0.09)
    assert flag_c == "below_model" and g_c == _G_LO, f"below flag={flag_c} g={g_c}"

    # ⑥ valuation_gap 부호 + None 전파
    assert abs(valuation_gap(0.20, 0.08) - 0.12) < 1e-9          # 내재 20% > 실제 8% = 고평가
    assert valuation_gap(None, 0.08) is None

    # ⑦ 실사례 감각 — fcf_yield 3%, wacc 9% 면 내재성장이 양수(가격에 성장이 반영됨)
    g_real, flag_real = implied_growth(1_000_000.0, 30_000.0, 0.09)
    assert flag_real == "ok" and g_real > 0.0, f"실사례 벗어남 g={g_real:.3f} flag={flag_real}"

    print("PASS — reverse_dcf 자체검증 7항목 (단조성·라운드트립·밴드·엣지·극단·갭·실사례)")
    print(f"  예시: fcf_yield 3%, WACC 9% → 내재성장 {g_real:.1%} (10년 고성장 가정)")
    print(f"  밴드: WACC 7.5/9/10.5% → g {g_lo:.1%}/{g_mid:.1%}/{g_hi:.1%}")


if __name__ == "__main__":
    _selfcheck()
