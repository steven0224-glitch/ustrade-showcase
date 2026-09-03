# chartist SR-Flip 진입 게이트 리플레이 — 사이징 수리 전/후

- 도구: `research/chartist_gate_replay.py` (실제 yfinance 1분봉, 관찰 전용, 매매경로 무접촉)
- 데이터: 최근 7거래일 · chartist watchlist 16종 · **112 종목·세션**
- 방법: 원본 `intraday_rules.chartist_rule` 호출 = ground truth + line-for-line 계측 사본(불일치 0 자체검증), 포지션 항상 flat = 진입 기회 상한

## 판정 — 수리 전후

| | 수리 전 (`min_risk_frac` 거부게이트) | 수리 후 (리스크기반 사이징) |
|---|---|---|
| **원본룰 진입(BUY)** | **0** | **21** |
| 최종 risk 후보(g7_rsi_ok) | 35 | 21 |
| `x_min_risk_block` | 35 (전멸) | 0 |
| 계측사본 불일치 | 0 | 0 |
| risk/c 평균·최대 | 0.483% · 0.708% | 0.507% · 0.708% |

수리 전 최종 후보 35건의 risk 상한(0.708%)이 요구치 `min_risk_frac` 1.2%의 59%에 불과 → 전건 거부 → 진입 0. `retest_low_tol 0.008` 이 stop 바닥을 `level×0.992` 로 클램프해 risk 상한이 구조적으로 ~1.1%인데, -EV 감사(`a36d685`)가 하한을 1.2%로 올려 상한<하한 산술 모순이 됨(감사 2건이 5일 간격 각각 옳은 수정 → 교집합 공집합).

## 수리

`min_risk_frac` 거부게이트 폐기 → 리스크기반 사이징(9편·7주차 2% 룰):
`amount = min(equity × risk_per_trade(0.02) × (진입가/주당risk), max_position_weight × 0.95)` + 현금·min_order 상한. 작은 risk 를 *거부*가 아니라 *증량*으로 처리(정통 2% 룰). 캡 바인딩 시 실손실 ≈ 캡38% × 손절거리 → rpt 2%보다 작다(보수적).

## 검증

- 배포게이트 11스위트 PASS (exit 0), `test_chartist_rule` 포함
- 계측사본 selftest 통과(불일치 0)
- 단위 delta: 구코드 거부자리(risk/c 0.698% < 1.2%)가 신코드에선 리스크기반 BUY(캡 38000)
- 독립 verifier(fresh context) 7/7 VERIFIED — `git diff` 로 변경 범위 chartist 함수/설정 국한(회귀 0)

## 배포

커밋 `088cfa0`(서명, 2026-08-28) → GitHub push → VM autopull 반영. paper 전용(실주문 0), §B 실험 무관. `chartist_ctl` 은 cfg 공유라 동일 적용(짝실험 보존). 다음 미국장 세션부터 실제 진입 발생 → 대시보드 표면화.

⚠️ 대조군 `chartist_ctl` 은 동일 112세션에서 수리 전 1건 → 수리 후 재측정 필요(별건).
