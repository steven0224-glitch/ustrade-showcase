"""배당 현금 입금 — 기본 paper 북(§B 실험 경로) 전용 총수익 회계.

§B 벤치마크는 SPY *총수익*(배당 포함)인데 전략 NAV 는 배당 미계상이었다 — 12주에
~0.3~0.4%p 를 전략만 손해 보는 비대칭(2026-07-12 수리, pre-T0 유일 창). ex-date 기준
입금(총수익 회계 표준): 권리 수량 = 당일 리밸런스 *전* 보유분. 포지션은 런에서만 바뀌므로
결번이 있었어도 "현재(리밸런스 전) 보유수량 = 그 구간 전체의 보유수량"이 성립 —
(마커, 세션] 창 처리로 결번에도 정확.

설계 계약:
- fail-open: 어떤 실패도 raise 하지 않는다 — 배당 처리가 매매 런을 죽이면 안 됨(A1 결번 금지).
- 멱등: 마커 파일(마지막 처리 세션). 마커 쓰기는 입금 *전* — 실패 조합이 항상
  미입금(과소계상) 방향으로 떨어지게(이중입금보다 안전한 쪽).
- 첫 가동/손상 마커: 현재 세션으로 초기화만, 과거 소급 입금 0 (shakedown 오염 방지).
- 스코프: run_live 가 기본 paper 북에만 marker 를 전달 — 페르소나·toss 미적용
  (페르소나 함대는 진행 중 비교실험이라 회계 변경 = 오염; ETF 페르소나 도입 때 확장).
"""
import sys
from datetime import date
from pathlib import Path


def fetch_dividends(symbol: str, start_excl: date, end_incl: date) -> dict:
    """(start, end] 창의 ex-date 배당 {date: per_share(USD)}. 실패 시 빈 dict(fail-open) —
    해당 심볼의 이번 창 배당은 미입금(과소계상)으로 떨어지고 stderr 에 경고."""
    try:
        import yfinance as yf
        s = yf.Ticker(symbol).dividends
        if s is None or len(s) == 0:
            return {}
        out = {}
        for ts, amt in s.items():
            d = ts.date() if hasattr(ts, "date") else ts
            a = float(amt)
            if start_excl < d <= end_incl and a > 0:
                out[d] = out.get(d, 0.0) + a
        return out
    except Exception as e:
        print(f"[dividends] {symbol} 배당 조회 실패(이번 창 미입금): {e!r}", file=sys.stderr)
        return {}


def _write_marker(p: Path, d: date) -> bool:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(d.isoformat(), encoding="utf-8")
        return True
    except Exception as e:
        print(f"[dividends] 마커 저장 실패(이번 창 미입금·다음 런 재시도): {e!r}", file=sys.stderr)
        return False


def process_dividends(broker, session_iso: str, marker_file, fetch=fetch_dividends) -> list:
    """(마커, 세션] 창의 ex-date 배당을 현재(리밸런스 전) 보유수량으로 현금 입금.

    반환: 입금 성공 이벤트 리스트(저널용) — 절대 raise 하지 않음. fetch 는 창이 이미
    걸러진 {date: per_share} 를 반환하는 계약(fetch_dividends 참조, 테스트 주입점)."""
    try:
        end = date.fromisoformat(str(session_iso))
        p = Path(marker_file)
        last = None
        if p.exists():
            try:
                last = date.fromisoformat(p.read_text(encoding="utf-8").strip())
            except Exception:
                last = None            # 손상 마커 → 재초기화(이번 창 미입금 = 과소계상 방향)
        if last is None:
            _write_marker(p, end)      # 첫 가동 — 소급 입금 0
            return []
        if last >= end:
            return []                  # 이미 처리(멱등)
        events = []
        for pos in broker.get_positions():
            sym, qty = pos.symbol, float(pos.qty)
            if qty <= 0:
                continue
            for d, per_share in sorted(fetch(sym, last, end).items()):
                amt = round(qty * float(per_share), 2)
                if amt > 0:
                    events.append({"symbol": sym, "ex_date": d.isoformat(),
                                   "per_share": float(per_share), "qty": qty, "amount": amt})
        if not _write_marker(p, end):  # 마커 먼저 — 마커실패+입금성공 조합(이중입금) 차단
            return []
        credited = []
        for e in events:
            try:
                broker.credit_cash(e["amount"])
                credited.append(e)
            except Exception as ex:
                print(f"[dividends] 입금 실패(과소계상) {e['symbol']} {e['amount']}: {ex!r}",
                      file=sys.stderr)
        return credited
    except Exception as e:
        print(f"[dividends] 처리 실패(매매 무영향·마커 미전진=다음 런 재시도): {e!r}", file=sys.stderr)
        return []
