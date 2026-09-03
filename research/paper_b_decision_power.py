"""§B 판정규칙 v1 vs v2 오판율 — 몬테카를로 (docs/paper-trading-dod.md §B-7 의 재현 스크립트).

왜 있나: §B 는 **사전등록** 문서다. 거기 실린 오판율 표가 재현 불가능한 주장이면
사전등록의 의미가 없다. 이 파일이 그 표의 유일한 근거다.

모형
  일별 초과수익 d_t ~ N(mu, sd) iid.
  sd 는 "12주(60세션) 누적 초과수익 sigma = 7.5%p"(2026-08-01 실측, 캐시 패널·무작위 3종목)에 맞춤.
  mu 는 연 정보비율 IR 로 파라미터화: mu_daily = IR * sd_daily * sqrt(252) / 252.
  look = 12/16/20주 = 60/80/100 세션. 첫 교차가 판정을 확정(순차 검정, 다중 look 보정 없음 — 사전등록).

  v1: 누적초과 >= 0 계속 / < -5%p 중단 / 그 사이 연장
  v2: t >= +1.30 계속 / t <= -1.30 중단 / 그 사이 판정불가 -> 연장, 최종 look 무교차는 그대로 남김
      (문서상 "조건부 계속"으로 귀결하지만, 판정 라벨 자체는 inconclusive 로 집계한다)

  python research/paper_b_decision_power.py            # 표 출력 (N=200k)
  python research/paper_b_decision_power.py --selftest # 산식 자가검증 (N=40k, 고정시드)

ponytail: iid 정규 가정. 실제 d 는 레짐 필터로 분산이 시변하지만, 사전등록 시점엔 실현 데이터가
0개라 실측 sigma 하나 말고는 넣을 정보가 없다. 실현 t 는 자기교정되므로 이 가정은 판정선이
아니라 "오판율 공시값"에만 영향한다. 실데이터 60런 쌓이면 부트스트랩으로 갱신할 것.
"""
import argparse

import numpy as np

SIG12_TOP3 = 0.075                # 12주 누적 초과수익 sigma — top_n=3 실측(2026-08-01)
SIG12_TOP5 = SIG12_TOP3 * np.sqrt(3 / 5)   # top_n=5 (v2.1 채택). sigma ∝ 1/sqrt(k), 잔차 지배 가정
SESS12 = 60                       # 12주 ~ 60 세션
LOOKS = (60, 80, 100)             # 12 / 16 / 20 주
TCRIT = 1.30                      # v2 경계 = 단측 10% (n=60 에서 1.296, n=100 에서 1.290)
V1_STOP = -0.05                   # v1 중단선 = 누적초과 -5%p (절대값 → sigma 에 의존)


def sd_daily(sig12):
    return sig12 / np.sqrt(SESS12)


def paths(ir_ann, n_paths, rng, sig12):
    """연 정보비율 ir_ann 에 대응하는 일별 초과수익 경로 (n_paths x max(LOOKS))."""
    sd = sd_daily(sig12)
    mu = ir_ann * sd * np.sqrt(252) / 252.0
    return rng.normal(mu, sd, size=(n_paths, LOOKS[-1]))


def _sequential(d, go_fn, stop_fn):
    """look 을 순서대로 보며 첫 교차로 확정. 반환 = (계속률, 중단률, 미교차율)."""
    n_paths = d.shape[0]
    done = np.zeros(n_paths, bool)
    go = np.zeros(n_paths, bool)
    stop = np.zeros(n_paths, bool)
    for n in LOOKS:
        x = d[:, :n]
        g = (~done) & go_fn(x, n)
        s = (~done) & stop_fn(x, n) & ~g          # 동시 성립 불가지만 방어
        go |= g
        stop |= s
        done |= (g | s)
    return go.mean(), stop.mean(), (~done).mean()


def _t(x, n):
    return x.mean(1) / (x.std(1, ddof=1) / np.sqrt(n))


def v2(d):
    return _sequential(d, lambda x, n: _t(x, n) >= TCRIT, lambda x, n: _t(x, n) <= -TCRIT)


def v1(d):
    return _sequential(d, lambda x, n: x.sum(1) >= 0.0, lambda x, n: x.sum(1) < V1_STOP)


def single_look(d, n=SESS12):
    """12주 1회 판정 — (v1 계속, v1 중단, v2 계속, v2 중단)."""
    x = d[:, :n]
    e = x.sum(1)
    t = _t(x, n)
    return (e >= 0).mean(), (e < V1_STOP).mean(), (t >= TCRIT).mean(), (t <= -TCRIT).mean()


def report(n_paths=200_000, seed=20260801):
    rng = np.random.default_rng(seed)
    for label, sig12 in (("top_n=3 (v1 등록시 · v2 오판율 산정 기준)", SIG12_TOP3),
                         ("top_n=5 (v2.1 채택 2026-08-01)", SIG12_TOP5)):
        te = sig12 / np.sqrt(12 / 52)
        print()
        print("=" * 76)
        print(label)
        print(f"  12주 sigma {sig12 * 100:.2f}%p · 일별 sd {sd_daily(sig12) * 100:.3f}%p"
              f" · TE연 {te * 100:.1f}%p · 12주 80%검정력 최소탐지 알파 연 {2.486 * sig12 / (12 / 52) * 100:.0f}%p"
              f" · N={n_paths:,}")
        for ir in (0.0, 0.5, -0.5):
            d = paths(ir, n_paths, rng, sig12)
            g1, s1, g2, s2 = single_look(d)
            print(f"  IR_ann {ir:+.1f} (연 알파 {ir * te * 100:+.1f}%p)")
            print(f"    12주 1회   v1 계속 {g1:5.1%} 중단 {s1:5.1%} | v2 계속 {g2:5.1%} 중단 {s2:5.1%}")
            for name, fn in (("v1", v1), ("v2", v2)):
                g, st, u = fn(d)
                print(f"    20주 누적  {name} 계속 {g:5.1%} 중단 {st:5.1%} 판정불가 {u:5.1%}")
    print()
    print("=" * 76)
    print(f"채택 효과(top3 -> top5): 12주 sigma {SIG12_TOP3 * 100:.1f} -> {SIG12_TOP5 * 100:.1f}%p"
          f" · t 배율 {np.sqrt(5 / 3):.2f} · 같은 검정력 도달기간 1/{5 / 3:.2f}배")
    print("  v2 의 알파0 오판율은 sigma 에 불변(t 가 자기정규화) — 바뀌는 것은 "
          "'주어진 검정력을 얻는 알파 수준'이다. v1 은 -5%p 절대선이라 sigma 에 따라 흔들린다.")


def selftest():
    rng = np.random.default_rng(7)
    n = 40_000
    assert abs(sd_daily(SIG12_TOP3) * np.sqrt(SESS12) - SIG12_TOP3) < 1e-12       # sigma 보정 정합
    assert abs(SIG12_TOP5 - 0.0581) < 1e-4, SIG12_TOP5                            # top5 sigma = 7.5*sqrt(3/5)

    d0 = paths(0.0, n, rng, SIG12_TOP3)
    g1, s1, g2, s2 = single_look(d0)
    assert abs(g1 - 0.50) < 0.02, g1        # v1 "초과>=0" = 동전던지기
    assert abs(s1 - 0.253) < 0.02, s1       # v1 중단 = Phi(-5/7.5) = 25.2%
    assert abs(g2 - 0.10) < 0.015, g2       # v2 계속 = 단측 10% 명목
    assert abs(s2 - 0.10) < 0.015, s2
    assert g2 < g1 / 3, (g2, g1)            # 재설계의 목적: 알파 0 오통과 급감

    G1, S1, U1 = v1(d0)
    G2, S2, U2 = v2(d0)
    assert abs(G1 - 0.60) < 0.02, G1        # v1 ladder 누적 계속 60%
    assert abs(G2 - 0.16) < 0.02, G2        # v2 ladder 누적 계속 16%
    assert U1 < 0.10 and U2 > 0.60, (U1, U2)
    assert abs(G1 + S1 + U1 - 1.0) < 1e-9 and abs(G2 + S2 + U2 - 1.0) < 1e-9

    dg = paths(0.5, n, rng, SIG12_TOP3)     # 좋은 전략 오살률: v2 가 v1 의 절반 미만
    assert v2(dg)[1] < v1(dg)[1] / 2, (v2(dg)[1], v1(dg)[1])

    db = paths(-0.5, n, rng, SIG12_TOP3)    # 판별력(나쁜전략 중단률 - 알파0 중단률)은 두 규칙이 비슷
    disc1, disc2 = v1(db)[1] - S1, v2(db)[1] - S2
    assert 0.05 < disc2 < disc1 < 0.16, (disc1, disc2)

    # v2.1 핵심: v2 판정선의 알파0 오판율은 sigma 에 불변(t 자기정규화). top5 로 바뀌어도 동일.
    d5 = paths(0.0, n, rng, SIG12_TOP5)
    G5, S5, U5 = v2(d5)
    assert abs(G5 - G2) < 0.015 and abs(S5 - S2) < 0.015, (G5, S5, G2, S2)
    # 반면 v1 의 -5%p 절대선은 sigma 축소로 중단률이 눈에 띄게 내려간다(규칙이 sigma 에 의존).
    assert v1(d5)[1] < v1(d0)[1] - 0.03, (v1(d5)[1], v1(d0)[1])   # 35.3% -> 30.4% 실측
    # 검정력 이득: 같은 절대알파(연 +8%p)에서 top5 의 12주 E[t] 가 top3 보다 크다
    te3, te5 = SIG12_TOP3 / np.sqrt(12 / 52), SIG12_TOP5 / np.sqrt(12 / 52)
    t3 = np.mean([_t(paths(0.08 / te3, 4_000, rng, SIG12_TOP3)[:, :60], 60).mean()])
    t5 = np.mean([_t(paths(0.08 / te5, 4_000, rng, SIG12_TOP5)[:, :60], 60).mean()])
    assert t5 > t3 * 1.15, (t3, t5)
    print("selftest OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="§B v1 vs v2 판정규칙 오판율 (dod.md §B-7 재현)")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--paths", type=int, default=200_000)
    a = ap.parse_args()
    selftest() if a.selftest else report(a.paths)
