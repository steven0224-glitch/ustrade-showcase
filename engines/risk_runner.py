"""리스크 오버레이 시뮬레이터 — 일별 레짐필터 / 변동성타겟 / 종목손절.

종목선택 전략과 독립. target_weights(리밸런스 비중) 위에 리스크 규칙을 일별 적용.
하루 처리 순서:
  1) 전일 비중에 당일 수익률 적용(이미 발생) → 자산·비중 표류
  2) 리스크 체크 (당일 종가 기준, 효과는 익일):
       - 종목 손절: 진입가 대비 -stop_loss 이하면 그 종목만 현금화
       - 레짐 OFF: 전량 현금화
  3) 리밸런스일이면 목표비중 설정 (레짐 OFF면 진입 안 함, vol_target 으로 총노출 스케일)
강제 청산·리밸런스 모두 turnover×fee 차감.
"""
import numpy as np
import pandas as pd

from . import metrics


def simulate(prices, target_weights, cash=10000.0, fee=0.0005,
             regime=None, vol_target=None, vol_lookback=20,
             max_leverage=1.0, stop_loss=None, ref_ret=None,
             spread=0.0, stop_gap=0.0):
    """리스크 오버레이 시뮬. spread/stop_gap 은 실현 슬리피지 모델(기본 0=기존 동작 불변).

    spread   : 호가 스프레드 — 회전(turnover) 1단위당 spread/2 비용(편도, executor 관례와 동일).
               리밸런스·강제청산 모두 부과.
    stop_gap : 갭 슬리피지 — 강제청산(손절·레짐OFF)에만 추가로 부과. 갭다운이 nominal
               stop 보다 불리하게 체결되는 것을 모델(종가 백테스트라 통계적 근사).
    """
    rets = prices.pct_change().fillna(0.0).values
    pxv = prices.values
    dates = prices.index
    n = prices.shape[1]

    rebal = {d: target_weights.loc[d].reindex(prices.columns).fillna(0.0).values
             for d in target_weights.index}
    reg = None if regime is None else regime.reindex(dates).fillna(False).values.astype(bool)
    # 변동성 추정용 레퍼런스 수익률 (현금 피드백 분리). 없으면 실현수익 사용.
    ref = None if ref_ret is None else ref_ret.reindex(dates).fillna(0.0).values

    w = np.zeros(n)
    entry_px = np.full(n, np.nan)
    eq = cash
    equity, port_rets, gross = [], [], []
    hist = []  # 변동성 추정용 port 수익률

    for i, d in enumerate(dates):
        r = rets[i]
        if i == 0:
            port_r = 0.0
        else:
            port_r = float((w * r).sum())
            new_w = w * (1.0 + r)
            total = new_w.sum() + (1.0 - w.sum())
            w = new_w / total if total > 0 else new_w
        eq *= (1.0 + port_r)
        hist.append(port_r)

        # 강제청산 비용률 = fee + 편도 스프레드 + 갭 슬리피지 (갭다운 불리체결 근사)
        exit_cost = fee + spread / 2.0 + stop_gap

        # --- 2) 리스크 체크 (효과는 익일) ---
        if stop_loss is not None:
            held = w > 1e-9
            hit = held & (~np.isnan(entry_px)) & ((pxv[i] / entry_px - 1.0) <= -stop_loss)
            if hit.any():
                eq *= (1.0 - w[hit].sum() * exit_cost)   # 청산 비용(+스프레드+갭)
                w[hit] = 0.0
                entry_px[hit] = np.nan

        if reg is not None and not reg[i]:
            if w.sum() > 1e-9:
                eq *= (1.0 - w.sum() * exit_cost)        # 전량 청산 비용(+스프레드+갭)
            w = np.zeros(n)
            entry_px[:] = np.nan

        # --- 3) 리밸런스 ---
        if d in rebal:
            tw = rebal[d].copy()
            if reg is not None and not reg[i]:
                tw = np.zeros(n)                   # 리스크오프 → 진입 보류
            if vol_target is not None:
                src = ref if ref is not None else np.asarray(hist)
                lo = max(0, i - vol_lookback + 1)
                window = src[lo:i + 1]
                realized = np.std(window) * np.sqrt(252) if len(window) >= vol_lookback else 0.0
                # 추정 불가(초기)면 풀투자(1.0), 가능하면 목표/실현 비율로 스케일
                scale = 1.0 if realized == 0 else float(np.clip(vol_target / realized, 0.0, max_leverage))
                tw = tw * scale
            eq *= (1.0 - np.abs(tw - w).sum() * (fee + spread / 2.0))   # 리밸런스 회전 비용(+스프레드)
            w = tw
            entry_px = np.where(w > 1e-9, pxv[i], np.nan)

        equity.append(eq)
        port_rets.append(port_r)
        gross.append(w.sum())

    idx = dates
    eq_s = pd.Series(equity, index=idx)
    pr_s = pd.Series(port_rets, index=idx)
    m = metrics.compute(eq_s, pr_s, len(target_weights))
    m["avg_gross"] = float(np.mean(gross))   # 평균 총노출 (1=풀투자, <1=현금보유)
    return eq_s, pr_s, m


def underwater(equity):
    return equity / equity.cummax() - 1.0
