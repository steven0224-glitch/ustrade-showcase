"""FMP 무료티어 팩터 — 어닝 서프라이즈(백테스트가능) + 현재 펀더멘털 스냅샷(라이브).

- earnings_surprise_panel: epsActual vs epsEstimated → PEAD 팩터 (시점데이터, IC 백테스트 가능)
- snapshot: 현재 ratios-ttm/key-metrics-ttm → 라이브 품질/가치 필터용 (과거 없음 = 백테스트 불가)
"""
import re

import numpy as np
import pandas as pd

from fmp_client import FMP
from logsetup import get_logger

_log = get_logger("fmp")


def _safe_err(e) -> str:
    """예외 메시지에서 apikey 노출 제거 (requests 에러는 URL 통째라 키가 로그에 샘)."""
    return re.sub(r"(apikey|apiKey)=[^&\s]+", r"\1=***", str(e))


def earnings_surprise_panel(tickers, index, fmp=None, drift_days=63):
    """어닝 서프라이즈 팩터 패널 (index×tickers).

    surprise = (epsActual - epsEstimated) / |epsEstimated|, 발표일부터 drift_days 지속(ffill).
    높을수록 매수 (PEAD: 어닝 서프라이즈 후 주가 표류).
    """
    fmp = fmp or FMP()
    cols = {}
    for t in tickers:
        try:
            rows = fmp.earnings(t, limit=40)
        except Exception as e:
            _log.warning("스킵 %s: %s", t, _safe_err(e))
            continue
        if not isinstance(rows, list):     # 비리스트 응답(None·dict 스키마변동)은 한 종목만 skip(전체 abort 방지)
            _log.warning("스킵 %s: earnings 비리스트 응답 %s", t, type(rows).__name__)
            continue
        recs = []
        for r in rows:
            a, e, d = r.get("epsActual"), r.get("epsEstimated"), r.get("date")
            if a is None or e is None or e == 0 or d is None:
                continue
            try:
                ts = pd.Timestamp(d)
            except Exception:
                continue                          # 결측/이상 날짜 레코드 skip(date 만 [] 인덱싱하던 비대칭 제거)
            surp = (a - e) / abs(e)
            recs.append((ts, float(np.clip(surp, -1.0, 1.0))))
        if not recs:
            continue
        s = pd.Series(dict(recs)).sort_index()
        # 거래일 인덱스로 정렬 후 발표일부터 drift_days 지속
        s = s.reindex(index.union(s.index)).ffill(limit=drift_days).reindex(index)
        cols[t] = s
    if not cols:
        raise ValueError("어닝 데이터 0 — 키/티커 확인")
    return pd.DataFrame(cols).reindex(columns=list(tickers))


# 라이브 필터용 현재 펀더멘털 (백테스트 불가 — 스냅샷)
SNAPSHOT_FIELDS = {
    "pe": ("ratios", "priceToEarningsRatioTTM"),
    "pb": ("ratios", "priceToBookRatioTTM"),
    "ps": ("ratios", "priceToSalesRatioTTM"),
    "debt_equity": ("ratios", "debtToEquityRatioTTM"),
    "net_margin": ("ratios", "netProfitMarginTTM"),
    "div_yield": ("ratios", "dividendYieldTTM"),
    "earnings_yield": ("metrics", "earningsYieldTTM"),
    "fcf_yield": ("metrics", "freeCashFlowYieldTTM"),
    "market_cap": ("metrics", "marketCap"),
    # 품질 대리변수 — ROE/ROIC 는 ratios-ttm 이 아니라 key-metrics-ttm 소스(fmp_cache 실측).
    # 이미 호출하는 엔드포인트라 추가 API 콜 0. 구캐시엔 없을 수 있으나 결측=NaN=중립(기존 정책).
    "roe": ("metrics", "returnOnEquityTTM"),
    "roic": ("metrics", "returnOnInvestedCapitalTTM"),   # 점수 미반영(관측용) — 품질 재설계 시 ROE 대체 후보
}


def snapshot(tickers, fmp=None):
    """현재 펀더멘털 스냅샷 DataFrame (라이브 품질/가치 필터용)."""
    fmp = fmp or FMP()
    rows = {}
    for t in tickers:
        try:
            rt, km = fmp.ratios_ttm(t), fmp.key_metrics_ttm(t)
        except Exception as e:
            _log.warning("스킵 %s: %s", t, _safe_err(e))
            continue
        rows[t] = {name: (rt if src == "ratios" else km).get(field)
                   for name, (src, field) in SNAPSHOT_FIELDS.items()}
    return pd.DataFrame(rows).T


def _z(s):
    s = pd.to_numeric(s, errors="coerce")
    sd = s.std()
    return (s - s.mean()) / sd if sd and sd > 0 else s * 0.0


_QV_COLS = ("pe", "pb", "earnings_yield", "net_margin", "debt_equity", "roe")


def quality_value_score(snap: pd.DataFrame) -> pd.Series:
    """가치(저PE·저PB·고earnings_yield) + 퀄리티(고마진·저부채·고ROE) z-score 합. 높을수록 우량.

    ⚠️ 현재 스냅샷 기반 = 라이브 틸트용. 과거 데이터 없어 historical 백테스트 불가 (무료티어 한계).
    """
    if snap.empty or "pe" not in snap.columns:   # 전 티커 실패 → snapshot 빈 DF(컬럼 없음) → snap["pe"] KeyError 방지
        return pd.Series(dtype=float)
    # reindex — 구캐시/합성 스냅에 신규 컬럼(roe)이 없어도 NaN 컬럼으로 채워 KeyError 대신 중립 처리.
    # to_numeric — FMP 스키마 드리프트로 문자열이 한 칸 섞이면 컬럼이 object dtype 이 되고 아래
    # .clip 이 TypeError 로 죽는다(기존 pe.clip 도 같은 노출이었음).
    s = snap.reindex(columns=list(_QV_COLS)).apply(pd.to_numeric, errors="coerce")
    # 부호역전 방어(P1-A3) — pe·pb·D/E 는 분모(순이익·자기자본)가 음수면 부호가 뒤집혀 '싸고 무차입'
    # 으로 위장한다: pb<0 → -pb 최대 → value 1위, D/E<0 → -D/E 최대 → quality 1위를 동시 달성.
    # 비양수는 '최악'이 아니라 '측정불가'로 처리한다 — 자사주매입 누적(MCD·HD류)도 자기자본이 음수라
    # 최악 처리는 오판이 된다. NaN → 아래 하위팩터별 fillna(0) 로 중립 기여(기존 결측 정책과 동일).
    pe = s["pe"].where(s["pe"] > 0)
    pb = s["pb"].where(s["pb"] > 0)
    de = s["debt_equity"].where(s["debt_equity"] > 0)
    # 하위팩터별 개별 fillna(0) — 한 팩터 결측이 NaN 전파로 value/quality 컴포넌트 전체를 0 소거하지
    # 않게(부분결측 흔함). 컴포넌트 통째 fillna 면 저PE/PB 강점이 earnings_yield 하나 결측에 죽음.
    value = (_z(-pe.clip(upper=100)).fillna(0) + _z(-pb).fillna(0)
             + _z(s["earnings_yield"]).fillna(0))
    # ROE(P1-A2) — margin 만으로는 못 잡는 자본효율. clip(-1,1) 은 자본잠식 아티팩트 절단용:
    # BA 는 순이익률 +2.5% 인데 ROE 가 -87 로 찍혀(fmp_cache 실측) 무클립이면 z 분포를 통째 뭉갠다.
    # 우량 대역(0.1~0.5) 해상도는 그대로 두고 양끝만 자른다.
    quality = (_z(s["net_margin"]).fillna(0) + _z(-de.clip(upper=5)).fillna(0)
               + _z(s["roe"].clip(-1.0, 1.0)).fillna(0))
    return (value + quality).sort_values(ascending=False)


# ── buffett_v2 전용 (A/B 12주 병행) — v1 경로는 아래 어느 것도 호출하지 않는다 ──────────────
# v1(quality_value_score)·canslim 접점(equal_weight·screen_degraded_flag)은 동결. 순수 추가.

def sectors(tickers, fmp=None) -> pd.Series:
    """티커→섹터 (profile 엔드포인트). **v2 전용** — snapshot() 은 호출하지 않으므로
    v1/momentum/wood 의 FMP 콜 수는 불변. 종목당 +1 콜, 섹터는 준정적이라 캐시 TTL 로 상각된다.
    조회 실패분은 None → _z_sector 가 전역 z 로 폴백(섹터 갭이 선정을 죽이지 않음)."""
    fmp = fmp or FMP()
    out = {}
    for t in tickers:
        try:
            out[t] = (fmp.profile(t) or {}).get("sector")
        except Exception as e:
            _log.warning("섹터 스킵 %s: %s", t, _safe_err(e))
            out[t] = None
    return pd.Series(out, dtype=object)


def _z_tol(s):
    """_z 의 허용오차판 — 상수 컬럼에서 std 가 정확히 0 이 아니라 1e-17 로 나오는 경우를 막는다.

    [0.2, 0.2, 0.2] 의 mean 은 0.20000000000000004(1 ULP 오차)라 편차가 ~3e-17, std 도 3e-17 이
    되고 `sd > 0` 가드를 통과한다. 그 뒤 편차/std 로 나누면 **의미 없는 반올림 노이즈가 O(1)
    z-score 로 증폭**된다(상수 열 하나가 ±0.8 씩 기여, 순위까지 뒤집을 수 있음).
    → std 를 값의 스케일과 비교해 '사실상 상수' 면 0 기여로 떨군다.

    ⚠️ 공유 _z 는 고치지 않는다 — v1(buffett 대조군)·wood 가 쓰고 있어 12주 A/B 중 대조군
    거동이 바뀌면 실험이 무효가 된다. 동일 결함이 _z 에도 있다는 사실은 별도 보고 대상.
    """
    s = pd.to_numeric(s, errors="coerce")
    sd, scale = s.std(), s.abs().max()
    if not (sd and sd > 1e-12 * (scale if scale and scale > 0 else 1.0)):
        return s * 0.0
    return (s - s.mean()) / sd


def _z_sector(s, sector=None, min_n: int = 4):
    """섹터 틸트만 제거하고 전역 z — 섹터 표본이 얇으면 제거량을 비례 축소(shrinkage).

    pool 20종에 GICS 11섹터면 섹터당 표본이 1~4다. 순수 섹터내 z 는 n=1 에서 0, n=2 에서 부호만
    남은 ±0.707 고정이라 pool 절반이 랭킹에서 증발한다 → 부분 풀링(partial pooling)으로 간다.
    가중 w=(n-1)/(min_n-1): n=1 은 보정 0(전역 z 그대로), n>=min_n 은 섹터 틸트 완전 제거.
    n=1 에서 섹터효과를 추정할 표본이 없다는 사실을 그대로 반영한 것.

    ⚠️ 빼는 것은 섹터평균 자체가 아니라 **섹터평균의 전체평균 대비 편차**(James-Stein 형)다.
    섹터평균을 통째로 빼면 w=1 그룹은 수준(level)이 0 으로 붕괴하는데 w=0 싱글턴은 원값을
    유지해, 한 시리즈 안에서 척도가 다른 값이 섞인다. 실측 반례(FCF yield):
      Staples 4종 [6,7,8,9%] + Utilities 싱글턴 [2%] → 싱글턴이 z 1.12 로 **1위**,
      9% 우량주는 0.77 (2% 가 9% 를 이김). 편차만 빼면 수준이 보존돼 이 역전이 사라진다.
    sector 미제공/전량결측이면 전역 z 로 폴백 = v1 과 동일 거동.
    """
    s = pd.to_numeric(s, errors="coerce")
    if sector is None:
        return _z_tol(s)
    grp = s.groupby(pd.Series(sector).reindex(s.index))   # 섹터 NaN 행은 그룹 제외 → transform NaN → 보정 0
    w = ((grp.transform("count") - 1.0) / max(min_n - 1, 1)).clip(0.0, 1.0)
    tilt = (w * (grp.transform("mean") - s.mean())).fillna(0.0)
    return _z_tol(s - tilt)


_QV2_COLS = ("pe", "net_margin", "roic", "roe", "fcf_yield", "earnings_yield")


def quality_value_score_v2(snap: pd.DataFrame, sector=None) -> pd.Series:
    """buffett_v2 점수 = 가치(FCF·이익수익률) + 품질(ROIC 중심) − 소프트 페널티, 섹터중립 z.

    v1 대비 4가지가 다르다:
      ① PE≤25·마진≥8% 하드컷 → 위반 정도에 비례하는 연속 페널티(컷은 적자·PE>60 만 남음)
      ② 품질축 margin → ROIC 중심(ROE 는 레버리지로 부풀어 보조 가중: BAC ROE .105/ROIC .046)
      ③ 가치축 PE·PB → FCF·이익수익률(무형자산 상각으로 PE 가 왜곡되는 복리우량주 구제)
      ④ 섹터 demean(표본 얇으면 수축) — 저마진 업종이 마진 하나로 통째 탈락하지 않게
    v1 은 대조군이라 동결 — 공유 대신 별도 함수로 둔다(12주 A/B 중 한쪽만 바뀌면 실험이 깨짐).
    """
    if snap.empty or "pe" not in snap.columns:
        return pd.Series(dtype=float)
    s = snap.reindex(columns=list(_QV2_COLS)).apply(pd.to_numeric, errors="coerce")

    def z(col, lo, hi):
        """클립 후 섹터중립 z. 결측은 0 기여(v1 의 하위팩터별 fillna 정책 승계)."""
        return _z_sector(s[col].clip(lo, hi), sector).fillna(0.0)

    # 수익률 클립 ±0.5 — 50% 넘는 FCF/이익수익률은 사실상 데이터 아티팩트(분모 붕괴).
    value = z("fcf_yield", -0.5, 0.5) + z("earnings_yield", -0.5, 0.5)
    # ROIC 1.0 / ROE 0.5 / 마진 0.5 — 자본효율을 주축, 나머지는 보조. clip(-1,1) 은 v1 과 동일한
    # 자본잠식 아티팩트 절단(BA: 마진 +2.5% 인데 ROE -87).
    quality = (z("roic", -1.0, 1.0) + 0.5 * z("roe", -1.0, 1.0)
               + 0.5 * z("net_margin", -1.0, 1.0))
    # 소프트 페널티 — v1 하드컷의 연속판. 위반 정도에 비례하되 최대 1z 로 유계(극단값이 랭킹을
    # 지배하지 않게). PE 25→0점, 50 이상→1점 감점 / 마진 8%→0점, 0%→1점 감점.
    # 결측은 0(감점 없음) — 데이터 갭으로 종목을 죽이지 않는 기존 정책과 일관.
    pe_pen = ((s["pe"] - 25.0) / 25.0).clip(0.0, 1.0).fillna(0.0)
    mg_pen = ((0.08 - s["net_margin"]) / 0.08).clip(0.0, 1.0).fillna(0.0)
    return (value + quality - pe_pen - mg_pen).sort_values(ascending=False)


def screen(snap: pd.DataFrame, min_net_margin: float = 0.0, max_pe: float = 80.0,
           max_debt_equity: float = None, min_market_cap: float = None,
           max_market_cap: float = None, require_fields=()):
    """하드 스크린 — 통과 티커 + 탈락사유. (은행 등은 부채비율 캡 제외 권장)

    값을 숫자로 강제 변환(to_numeric) 후 비교 — None/NaN/문자열이 `pe>max_pe` 같은 비교를
    조용히 통과(NaN 비교는 항상 False)하던 누수 차단(STRAT-1). 전 필드 결측 행은 호출측
    (live_select)이 snap 에서 제외해 missing(데이터 갭)으로 분류.

    min/max_market_cap: 시총 경계(USD, 예 10e9=$10B). None=무동작. marketCap 결측은
    통과(NaN 비교 False) — 시총 데이터 갭으로 종목을 죽이지 않음(net_margin/pe 와 동일 정책).

    require_fields(P2-A15①): 이 필드가 결측이면 '무데이터 통과' 대신 탈락. 기본 ()= 기존 동작
    불변(momentum 경로는 관대 정책 유지) — buffett 만 ("net_margin", "pe") 를 넘겨 보수화한다.
    버핏 하드컷의 의미는 '흑자·고마진 확인'인데, 마진 결측이 NaN 비교로 조용히 통과하면
    '검증됨' 으로 둔갑해 quality_value_score 랭킹까지 올라간다(missing 강등도 안 됨 —
    snapshot_and_screen 의 core dropna 는 pe·net_margin 이 *둘 다* 결측일 때만 발동).
    """
    fails = {}
    for t, row in snap.iterrows():
        nm = pd.to_numeric(row.get("net_margin"), errors="coerce")
        pe = pd.to_numeric(row.get("pe"), errors="coerce")
        de = pd.to_numeric(row.get("debt_equity"), errors="coerce")
        mc = pd.to_numeric(row.get("market_cap"), errors="coerce")
        need = [f for f in require_fields
                if pd.isna(pd.to_numeric(row.get(f), errors="coerce"))]
        if need:
            fails[t] = "필수 펀더 결측 " + "·".join(need)
        elif pd.notna(nm) and nm < min_net_margin:
            fails[t] = f"순이익률 {nm:.1%} < {min_net_margin:.0%}"
        elif pd.notna(pe) and pe > max_pe:
            fails[t] = f"P/E {pe:.0f} > {max_pe:.0f}"
        elif max_debt_equity is not None and pd.notna(de) and de > max_debt_equity:
            fails[t] = f"부채비율 {de:.1f} > {max_debt_equity}"
        elif min_market_cap is not None and pd.notna(mc) and mc < min_market_cap:
            fails[t] = f"시총 ${mc/1e9:.1f}B < ${min_market_cap/1e9:.0f}B"
        elif max_market_cap is not None and pd.notna(mc) and mc > max_market_cap:
            fails[t] = f"시총 ${mc/1e9:.1f}B > ${max_market_cap/1e9:.0f}B"
    passed = [t for t in snap.index if t not in fails]
    return passed, fails


def screen_degraded_flag(total: int, missing_count: int) -> bool:
    """결측 30%+ = 스크린 신뢰 저하(과반 대기 않음). momentum/buffett/wood/canslim 4개 선택모듈 공용 임계.

    ⚠️ 시그니처·반환 고정(§B canslim 이 직접 import). 확장은 아래 degraded_reasons 로.
    """
    return bool(total) and missing_count / total > 0.3


def degraded_reasons(candidates, missing, final, unscored=()) -> list:
    """선정 신뢰 저하 사유(관측 전용 — 거래 정책 무영향). 빈 리스트 = 전원 펀더 검증 후 채점됨.

    P2-A4. screen_degraded_flag 는 '풀 전체 결측률 30%' 만 본다 — 결측 5/18(28%)처럼 임계
    미만이면 False 인데, 그 5개는 eligible=ranked+missing 폴백으로 실제 매수분에 들어갈 수
    있다(펀더 미검증 편입인데 알림·저널 어디에도 안 남음). 여기가 그 구간을 잇는다.

    unscored: snap 에는 있으나 점수 산출이 결측이라 모멘텀 폴백으로 대체된 티커(wood
    gs.fillna(mom_z) / buffett qv 부재) — missing 이 아니라 더 조용하다.
    """
    fin, miss = list(final or []), set(missing or [])
    out = []
    if candidates and screen_degraded_flag(len(candidates), len(miss)):
        out.append(f"펀더 결측 {len(miss)}/{len(candidates)} — 스크린 신뢰 저하")
    unver = [t for t in fin if t in miss]
    if unver:
        out.append("펀더 미검증 편입 " + ", ".join(unver))
    uns = [t for t in fin if t in set(unscored or ()) and t not in miss]
    if uns:
        out.append("점수 결측→모멘텀 폴백 " + ", ".join(uns))
    return out


def snapshot_and_screen(tickers, fmp=None, core_fields=("pe", "net_margin"), screen_kwargs=None):
    """스냅샷 조회 → 결측행 정리 → (옵션) 하드스크린 → screen_degraded 판정. 선택모듈 3종(momentum/
    buffett/wood) 공용 — canslim 은 FMP 미사용이라 screen_degraded_flag 만 별도 재사용.

    정리 2단계: 전 필드 결측 행(데이터 갭) dropna → core_fields 전부결측 행 dropna. 후자는
    ratios_ttm 엔드포인트만 실패(key_metrics_ttm 은 성공)해도 ratios 소스 필드(pe/ps/net_margin/
    div_yield 등)가 함께 NaN 이 되는데 dropna(how="all")은 안 잡음 — pe/net_margin(둘 다 ratios
    소스) 이 전부 NaN 이면 그 행 전체를 결측 취급해 missing 강등(스크린 무탈락 통과·하위 fillna
    로 다른 값이 위장하는 것 차단).

    screen_kwargs=None(기본) → 하드스크린 생략(wood — 가치 스크린 없음, 적자 혁신주 허용).
    반환: (snap, passed, fails, missing, screen_degraded). screen_kwargs 없으면 passed=None, fails={}.
    """
    snap = snapshot(tickers, fmp)
    if not snap.empty:
        snap = snap.dropna(how="all")
        core = [c for c in core_fields if c in snap.columns]
        if core:
            snap = snap.dropna(subset=core, how="all")
    missing = [t for t in tickers if t not in snap.index]
    screen_degraded = screen_degraded_flag(len(tickers), len(missing))

    passed, fails = None, {}
    if screen_kwargs is not None:
        passed, fails = screen(snap, **screen_kwargs)
    return snap, passed, fails, missing, screen_degraded


def equal_weight(tickers) -> dict:
    """등비중 배분 — 빈 리스트/None 은 빈 dict. 4개 선택모듈(momentum/buffett/wood/canslim) 공용."""
    return {t: 1.0 / len(tickers) for t in tickers} if tickers else {}
