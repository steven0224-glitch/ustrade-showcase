"""버핏형 가치·우량 선택 (paper 페르소나 전용) — 저PE/PB·고마진·저부채 안정 대형주 집중.

⚠️ 라이브 전용(FMP 현재 스냅샷, 과거 없어 backtest 불가). 모멘텀이 아니라
'저변동(안정) 사전선별 → 가치·퀄리티 점수' 순. 실거래 경로(canslim/momentum)와 독립 — 모의매매 비교용.

흐름: 유니버스에서 저변동(안정 우량) 상위 pool → FMP 스냅샷 → 하드스크린(저PE·흑자) →
quality_value_score(저PE·저PB·고earnings_yield + 고마진·저부채·고ROE) 상위 top_n 등비중.
가치투자는 모멘텀 무관 → 저변동으로 pool 선별(FMP 호출 bound + '안정 비즈니스' 틸트).
value_trap_gate(opt-in): Piotroski reliable&F<5 veto — ai-berkshire '레드라인'(희석·부채급증·
발생액)의 정량 등가(9신호에 포함). A엔진(canslim 경유) 부재 시 no-op + veto_unavailable 표면화.
"""
import fmp_factors as ff
from logsetup import get_logger

_log = get_logger("live_select_buffett")


def _load_piotroski():
    """A엔진 Piotroski 로더 — canslim 모듈 경유 재사용(경로주입 포함). 부재 시 (None, None).

    모듈 레벨이 아닌 지연 로드 — 'buffett 등록에 A엔진 불요' 불변식(live_engine try/except
    등록) 보존 + 테스트가 이 함수를 monkeypatch 해 A 없이(CI 리눅스) 검증 가능.
    """
    try:
        from live_select_canslim import piotroski, _is_value_trap
        return piotroski, _is_value_trap
    except Exception:
        return None, None


def select(prices, lookback=252, top_n=5, pool=15,
           min_margin=0.08, max_pe=25.0, max_debt_equity=None,
           min_market_cap=None, max_market_cap=None, value_trap_gate=False, fmp=None, **_):
    # ⚠️ 하드컷 곱(margin≥8% ∧ PE≤25)의 섹터편향 — 이번 패스 임계 불변(재설계는 별도 결정).
    # 두 컷의 교집합이 저마진 고ROE 업종(유통·보험·에너지)을 마진에서, 무형자산 복리 우량주를
    # PE 에서 각각 쳐내 버핏 실보유 스타일이 통째로 탈락한다. 품질은 ROE 항(quality_value_score)이
    # 랭킹에서 보완할 뿐, 컷 자체를 통과시키진 않는다.
    # fill_method=None 명시(P2-A15②) — pandas 는 미지정 시 결측을 ffill 한 뒤 수익률을 내
    # 거래정지·상장전 구간이 0% 수익률로 채워져 변동성이 과소평가된다(= 저변동 pool 상위로
    # 오르는 방향의 편향). live_risk.py:83 과 동일 패턴.
    rets = prices.pct_change(fill_method=None)
    vol = rets.tail(lookback).std().dropna()                 # 장기 변동성 (낮을수록 안정)
    n = len(prices)
    base = prices.iloc[max(0, n - lookback)]
    trend = prices.iloc[-1] / base - 1.0                     # 장기 추세 (구조적 하락 가치트랩 1차 배제)
    # NaN trend(신규상장=base 시점 미거래)는 배제 안 함 — 펀더 단계서 거름. -20% 이하만 명시 탈락.
    # (trend.get 기본값 0.0 은 '값 존재+NaN' 엔 미적용 → `not(<=-0.20)` 로 NaN·상승을 통과시킴)
    cand_pool = [t for t in vol.sort_values().index if not (trend.get(t, 0.0) <= -0.20)][:pool]

    # 스냅샷+결측정리(core dropna)+스크린+screen_degraded — 공유 함수(fmp_factors, momentum/wood 와 대칭).
    # require_fields(P2-A15①, 잔건②) — 마진·PE 결측은 '무데이터 통과' 대신 탈락. buffett 하드컷
    # 한정(screen 기본 ()= momentum/wood 경로 동작 불변). pe 결측도 net_margin 과 동일 누수(자매
    # 누수) — max_pe 컷이 NaN 비교로 조용히 통과시켜 미검증 종목이 '고밸류 배제 통과'로 둔갑한다.
    snap, passed, fails, missing, screen_degraded = ff.snapshot_and_screen(
        cand_pool, fmp, screen_kwargs=dict(min_net_margin=min_margin, max_pe=max_pe,
                                           max_debt_equity=max_debt_equity,
                                           min_market_cap=min_market_cap, max_market_cap=max_market_cap,
                                           require_fields=("net_margin", "pe")))

    passed_in_snap = [t for t in passed if t in snap.index]
    qv = ff.quality_value_score(snap.loc[passed_in_snap]) if passed_in_snap else None
    ranked = list(qv.index) if qv is not None else list(passed)
    unscored = [] if qv is not None else list(passed)   # qv 부재 = 채점 없이 스크린 순서로 나감(A4 관측)
    eligible = ranked + missing                              # 점수 가능분 우선, 데이터갭은 뒤(폴백)

    # ── Piotroski value trap veto (opt-in) — reliable&F<5 탈락, 다음 순위로 백필 ──────
    vetoed, pio, veto_unavailable = [], {}, False
    if value_trap_gate:
        piotroski, is_trap = _load_piotroski()
        if piotroski is None:
            veto_unavailable = True                          # A엔진 부재 — 조용한 no-op 방지(표면화)
        else:
            kept = []
            for t in eligible:                               # lazy — top_n 채우면 중단(yfinance 호출 최소)
                if len(kept) >= top_n:
                    break
                try:                                          # 종목별 격리 — A엔진 hiccup 하나가 veto 전체를 안 죽임(FMP snapshot 경로와 대칭)
                    f = piotroski(t)
                except Exception as e:
                    _log.warning("piotroski 스킵 %s: %s", t, e)
                    f = {}                                    # 빈dict = reliable=False 취급 → is_trap 안 죽임(데이터갭 정책과 일관)
                pio[t] = (f or {}).get("score")              # canslim info 형상 미러 → selection_review 호환
                if is_trap(f):                               # 데이터갭(reliable=False)은 안 죽임 — FMP missing 정책과 일관
                    vetoed.append(t)
                    continue
                kept.append(t)
            eligible = kept                                  # 미조회 잔여는 top_n 밖 — 배제 무해

    final = eligible[:top_n]
    weights = ff.equal_weight(final)
    scores = {t: round(float(qv.get(t, 0.0)), 2) for t in final if qv is not None and t in qv.index}

    # P2-A4 — degrade 사유를 저널·알림에 명시(거래 정책 불변). screen_degraded 는 풀 결측률
    # 30% 임계라 '결측 4/15(27%) 인데 그 중 2개를 실제로 샀다' 를 못 잡는다. 실매수분이 미검증
    # 이면 여기서 플래그를 켜 run_live 의 기존 알림(screen_degraded)이 반드시 발화하게 한다.
    reasons = ff.degraded_reasons(cand_pool, missing, final, unscored)
    if reasons:
        _log.warning("buffett 선정 degrade: %s", " | ".join(reasons))

    return weights, {
        "strategy": "buffett", "candidates": cand_pool, "fails": fails, "missing": missing,
        "final_missing": [t for t in final if t in missing],   # 최종 선정분 중 결측 교집합(관측용, 로직 무영향)
        "screen_degraded": bool(screen_degraded or reasons), "degraded_reasons": reasons,
        "final": final, "scores": scores,
        "excluded_value_trap": vetoed, "piotroski": pio, "veto_unavailable": veto_unavailable,
        "momentum_only": [],       # 버핏은 모멘텀 전략 아님 → 빈값(selection_review momentum 차원 의미 일관)
    }
