"""S&P 500 구성종목 갱신 헬퍼 — 분기 1회 개발PC에서 수동 실행.

Wikipedia 에서 현재 구성종목을 받아 yfinance dash-form(BRK.B->BRK-B)으로 정규화하고
universe.py 의 UNIVERSES["sp500"] 과 diff 를 출력한다. VM 런타임/파이프라인과 무관 —
사람이 결과를 검토 후 paste 블록을 universe.py 에 직접 붙여넣는다.

주의: 출력되는 새 리스트는 알파벳 정렬이라, 기존 universe.py 의 (대략 인덱스 순)
순서와는 다르다. 다음 diff 를 깔끔하게 만들기 위한 선택이며 동작에는 영향 없음.

growth(캐시우드형 파괴성장 ~45종목)는 큐레이션 리스트라 자동 소스가 없음 — 별도 수동 검토 필요.
"""
import io
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_root))

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
# Wikipedia는 기본 urllib User-Agent를 403으로 거부 → 브라우저 UA로 위장.
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def normalize(symbol: str) -> str:
    return symbol.strip().replace(".", "-").upper()


def format_paste_block(tickers: list) -> str:
    lines = ["    # --- paste into universe.py UNIVERSES[\"sp500\"] ---"]
    for i in range(0, len(tickers), 10):
        chunk = tickers[i:i + 10]
        lines.append("        " + ", ".join(f'"{t}"' for t in chunk) + ",")
    return "\n".join(lines)


def fetch_current_sp500() -> list:
    try:
        import pandas as pd
    except ImportError:
        print("pip install pandas")
        sys.exit(2)
    import urllib.request

    try:
        req = urllib.request.Request(WIKI_URL, headers=_HEADERS)
        with urllib.request.urlopen(req) as resp:
            html = resp.read().decode("utf-8")
        tables = pd.read_html(io.StringIO(html))
    except ImportError:
        print("pip install lxml")
        sys.exit(2)
    symbols = tables[0]["Symbol"].tolist()
    candidates = sorted(dict.fromkeys(normalize(s) for s in symbols))
    return _validate_with_yfinance(candidates)


def _validate_with_yfinance(candidates: list) -> list:
    """yfinance 최근 5일 종가로 실제 거래 가능한 티커만 통과시킨다.
    (합병전 스핀오프, 위키 오기 심볼 등을 걸러냄)"""
    import yfinance as yf

    data = yf.download(candidates, period="5d", group_by="ticker",
                        auto_adjust=True, progress=False, threads=True)

    valid, dropped = [], []
    for tk in candidates:
        try:
            ok = data[tk]["Close"].notna().any()
        except Exception:
            ok = False
        (valid if ok else dropped).append(tk)

    if not valid:
        print("경고: yfinance 검증 결과 전종목 실패 — 네트워크 확인 필요. 중단.")
        sys.exit(2)

    print(f"DROPPED ({len(dropped)}): {dropped}")
    return sorted(valid)


def main():
    from universe import UNIVERSES

    current = set(UNIVERSES["sp500"])
    wiki = fetch_current_sp500()
    wiki_set = set(wiki)

    added = sorted(wiki_set - current)
    removed = sorted(current - wiki_set)

    print(f"현재 universe.py sp500: {len(current)}종목")
    print(f"Wikipedia 현재 구성종목: {len(wiki)}종목")
    print(f"ADDED ({len(added)}): {added}")
    print(f"REMOVED ({len(removed)}): {removed}")
    print()
    print(format_paste_block(wiki))
    print()
    print("주의: growth(캐시우드형 파괴성장) 는 자동 소스 없음 — 별도 수동 검토.")


def selftest():
    assert normalize("BRK.B") == "BRK-B"
    assert normalize("BF.B") == "BF-B"
    assert normalize("AAPL") == "AAPL"

    tickers = [f"T{i}" for i in range(23)]
    block = format_paste_block(tickers)
    body_lines = [l for l in block.splitlines() if l.strip().startswith('"')]
    assert len(body_lines) == 3, f"expected 3 lines, got {len(body_lines)}"

    print("selftest ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["selftest"]:
        selftest()
    else:
        main()
