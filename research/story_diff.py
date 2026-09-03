"""10-K 스토리 diff — 연도별 사업보고서 문장·톤 변화 탐지 (Notion "스토리 리더" 이식).

"요즘 이 회사 어때?" — 최근 두 10-K 를 문장 단위로 대조해 ① 새로 등장/사라진 문장,
② 헤징 톤 변화(will→may 류: 확신어 ↓ · 유보어 ↑ = 경영진 자신감 약화)를 계량한다.
사람이 수백 페이지짜리 문서 둘을 나란히 못 읽는 걸 대신한다.

⚠️ 프로토타입 — SEC EDGAR 무키. **섹션 추출(Item 1A)은 10-K HTML 포맷 편차가 커서 강건하지
않다.** 추출 실패 시 전문(full) 폴백. 톤 계량은 어휘 빈도 델타(문장별 정밀 매핑 아님).
자문·리서치 전용 — 트레이딩 루프·저널 무기록. stdlib 만(VM 무의존).

사용:
    python research/story_diff.py AAPL
    python research/story_diff.py MSFT --section full --ua "yourname you@example.com"

SEC 는 식별 User-Agent 를 요구한다(과도요청 차단용). --ua 로 실연락처 권장.
"""
from __future__ import annotations

import argparse
import difflib
import html as _html
import json
import re
import sys
import time
import urllib.request

_UA_DEFAULT = "ustrade-research story_diff (contact: set --ua)"
_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# SEC WAF 실측(2026-08-14): data.sec.gov 는 브라우저 UA+Accept+Accept-Language 3종이면 통과.
# www.sec.gov(Archives 본문·company_tickers)는 TLS 지문까지 봐서 urllib 로는 차단 — curl_cffi
# 임퍼소네이션이 있으면 그걸 우선 쓴다(insane-search 와 동일 처방). 미설치면 urllib 폴백(US IP·VM 통과).
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_HDRS = {"Accept": "application/json,text/html,*/*", "Accept-Language": "en-US,en;q=0.9"}

try:
    from curl_cffi import requests as _ccf   # TLS 임퍼소네이션 — www.sec.gov WAF 우회
except ImportError:
    _ccf = None


def _get(url: str, ua: str) -> bytes:
    hdrs = {"User-Agent": ua or _BROWSER_UA, **_HDRS}
    if _ccf is not None:
        r = _ccf.get(url, headers=hdrs, impersonate="chrome", timeout=30)
        r.raise_for_status()
        return r.content
    # 폴백 — SEC 는 봇틱 UA 를 403 하므로 실UA 도 브라우저형으로. Host 는 urlopen 자동설정.
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, "Accept-Encoding": "gzip", **_HDRS})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
    if data[:2] == b"\x1f\x8b":            # gzip
        import gzip
        data = gzip.decompress(data)
    return data


def resolve_cik(ticker: str, ua: str) -> tuple[str, str]:
    """티커 → (10자리 zero-pad CIK, 회사명). 대소문자 무시."""
    raw = json.loads(_get(_TICKERS_URL, ua))
    tk = ticker.upper()
    for row in raw.values():
        if row.get("ticker", "").upper() == tk:
            return f"{int(row['cik_str']):010d}", row.get("title", tk)
    sys.exit(f"[story_diff] 티커 {ticker!r} 를 SEC company_tickers 에서 못 찾음.")


def latest_10ks(cik: str, ua: str, n: int = 2) -> list[dict]:
    """최근 10-K n개 메타 (accession·primaryDocument·filingDate). 최신이 앞."""
    sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json", ua))
    rec = sub["filings"]["recent"]
    out = []
    for form, acc, doc, dt in zip(rec["form"], rec["accessionNumber"],
                                  rec["primaryDocument"], rec["filingDate"]):
        if form == "10-K":
            out.append({"accession": acc.replace("-", ""), "doc": doc, "date": dt})
        if len(out) >= n:
            break
    if len(out) < n:
        sys.exit(f"[story_diff] 10-K {n}개 필요한데 {len(out)}개만 있음 (CIK {cik}).")
    return out


def fetch_filing_text(cik: str, meta: dict, ua: str) -> str:
    cik_int = int(cik)
    url = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{meta['accession']}/{meta['doc']}"
    return html_to_text(_get(url, ua).decode("utf-8", errors="ignore"))


def html_to_text(h: str) -> str:
    """조악한 HTML→텍스트 — script/style 제거, 태그 제거, 엔티티 언이스케이프, 공백 정규화."""
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</tr>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = _html.unescape(h)
    h = re.sub(r"[ \t ]+", " ", h)
    h = re.sub(r"\n\s*\n+", "\n", h)
    return h.strip()


def extract_section(text: str, which: str) -> tuple[str, bool]:
    """Item 1A(Risk Factors) 또는 Item 7(MD&A) 추출. 실패 시 (전문, False).

    ⚠️ 10-K 목차에도 'Item 1A' 가 나와 첫 매치가 목차일 수 있다 → 마지막 매치부터 다음 Item 까지.
    포맷 편차로 자주 실패한다(그래서 폴백). full 은 항상 전문.
    """
    if which == "full":
        return text, True
    anchors = {"risk": (r"item\s*1a\.?\s*risk\s+factors", r"item\s*1b\b|item\s*2\b"),
               "mda": (r"item\s*7\.?\s*management", r"item\s*7a\b|item\s*8\b")}
    start_pat, end_pat = anchors[which]
    starts = list(re.finditer(start_pat, text, re.I))
    if not starts:
        return text, False
    s = starts[-1].start()               # 목차 매치 회피 — 실제 섹션은 뒤쪽
    end_m = re.search(end_pat, text[s + 50:], re.I)
    e = (s + 50 + end_m.start()) if end_m else len(text)
    section = text[s:e].strip()
    if len(section) < 500:               # 너무 짧으면 추출 실패로 간주
        return text, False
    return section, True


# 톤 어휘 — 확신 vs 유보. 델타로 경영진 자신감 방향을 근사한다(문장별 정밀 매핑 아님).
_HEDGE = ["may", "might", "could", "potential", "potentially", "uncertain", "uncertainty",
          "possible", "possibly", "risk", "risks", "adverse", "adversely", "challenging",
          "difficult", "decline", "declined", "weaken", "weakened", "cautious", "volatile",
          "volatility", "if we", "no assurance", "cannot assure"]
_CONFIDENT = ["will", "strong", "strongly", "robust", "confident", "confidence", "record",
              "growth", "grow", "increase", "increased", "improve", "improved", "solid",
              "momentum", "expand", "expanded", "leading", "leadership", "outperform"]


def _rate(text: str, lexicon: list[str]) -> tuple[int, float]:
    """어휘 총출현수 + 1000단어당 비율."""
    low = text.lower()
    n = sum(len(re.findall(r"\b" + re.escape(w) + r"\b", low)) for w in lexicon)
    words = max(len(re.findall(r"\w+", low)), 1)
    return n, 1000.0 * n / words


def tone_delta(old: str, new: str) -> dict:
    ho, hor = _rate(old, _HEDGE)
    hn, hnr = _rate(new, _HEDGE)
    co, cor = _rate(old, _CONFIDENT)
    cn, cnr = _rate(new, _CONFIDENT)
    return {"hedge_old": hor, "hedge_new": hnr, "conf_old": cor, "conf_new": cnr,
            "hedge_shift": hnr - hor, "conf_shift": cnr - cor}


def _sentences(text: str) -> list[str]:
    # 마침표/개행 기준 조악 분할 — 문장부호 편차 큰 filing 에 완벽하진 않음.
    parts = re.split(r"(?<=[.;])\s+|\n", text)
    return [p.strip() for p in parts if len(p.strip()) > 40]


def sentence_diff(old: str, new: str, sample: int = 8) -> dict:
    so, sn = _sentences(old), _sentences(new)
    sm = difflib.SequenceMatcher(a=so, b=sn, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("insert", "replace"):
            added.extend(sn[j1:j2])
        if tag in ("delete", "replace"):
            removed.extend(so[i1:i2])
    return {"n_old": len(so), "n_new": len(sn), "n_added": len(added),
            "n_removed": len(removed), "added": added[:sample], "removed": removed[:sample]}


def report(ticker: str, which: str, ua: str) -> str:
    cik, name = resolve_cik(ticker, ua)
    time.sleep(0.2)
    filings = latest_10ks(cik, ua, 2)
    time.sleep(0.2)
    new_txt = fetch_filing_text(cik, filings[0], ua)
    time.sleep(0.2)
    old_txt = fetch_filing_text(cik, filings[1], ua)

    new_sec, ok_new = extract_section(new_txt, which)
    old_sec, ok_old = extract_section(old_txt, which)
    used = which if (ok_new and ok_old) else "full"
    if used == "full":
        new_sec, old_sec = new_txt, old_txt

    tone = tone_delta(old_sec, new_sec)
    sdiff = sentence_diff(old_sec, new_sec)

    L = [f"# 10-K 스토리 diff — {name} ({ticker.upper()})", ""]
    L.append(f"- 최신: {filings[0]['date']} · 직전: {filings[1]['date']}")
    L.append(f"- 대상 섹션: **{used}**" + ("" if used == which else f" (요청 {which} 추출 실패 → 전문 폴백)"))
    L.append("")
    L.append("## 톤 변화 (1000단어당 빈도)")
    arrow = lambda d: "▲" if d > 0.05 else ("▼" if d < -0.05 else "≈")
    L.append(f"- 유보어(hedge): {tone['hedge_old']:.2f} → {tone['hedge_new']:.2f}  "
             f"{arrow(tone['hedge_shift'])} {tone['hedge_shift']:+.2f}")
    L.append(f"- 확신어(confident): {tone['conf_old']:.2f} → {tone['conf_new']:.2f}  "
             f"{arrow(tone['conf_shift'])} {tone['conf_shift']:+.2f}")
    verdict = ("톤 약화(유보↑·확신↓) — 경영진 자신감 저하 신호"
               if tone["hedge_shift"] > 0.1 and tone["conf_shift"] < 0 else
               "톤 강화(확신↑·유보↓)" if tone["conf_shift"] > 0.1 and tone["hedge_shift"] < 0 else
               "혼조/큰 변화 없음")
    L.append(f"- **판정: {verdict}**")
    L.append("")
    L.append(f"## 문장 변화 (문장 {sdiff['n_old']}→{sdiff['n_new']}, "
             f"+{sdiff['n_added']} / -{sdiff['n_removed']})")
    if sdiff["added"]:
        L.append("\n**새로 등장:**")
        L.extend(f"- + {s[:220]}" for s in sdiff["added"])
    if sdiff["removed"]:
        L.append("\n**사라짐:**")
        L.extend(f"- − {s[:220]}" for s in sdiff["removed"])
    L.append("\n⚠️ 프로토타입 — 톤은 어휘빈도 근사, 섹션추출은 포맷편차로 불완전. 원문 대조 필수.")
    return "\n".join(L)


def _selfcheck() -> None:
    """네트워크 없이 파싱 로직만 — 섹션추출(목차 회피)·톤방향·문장diff. 실페치는 VM(US IP)."""
    txt = html_to_text("<style>x{}</style><p>Revenue &amp; growth were <b>strong</b>.</p>"
                       "<script>evil()</script><div>Risks may increase.</div>")
    assert "evil" not in txt and "x{}" not in txt and "Revenue & growth were strong" in txt

    doc = ("CONTENTS Item 1A. Risk Factors 12 Item 7. Management 40 " + "X" * 100 +
           " Item 1A. Risk Factors " + "사업은 위험에 노출된다. " * 40 + " Item 1B. none.")
    sec, ok = extract_section(doc, "risk")
    assert ok and "CONTENTS" not in sec and sec.startswith("Item 1A"), "목차 회피 실패"
    assert extract_section("no items, just prose", "risk") == ("no items, just prose", False)

    td = tone_delta("We will deliver strong growth and record momentum here.",
                    "Results may decline; no assurance, uncertain and adverse and volatile.")
    assert td["hedge_shift"] > 0 and td["conf_shift"] < 0, f"톤 방향 오류: {td}"

    shared = "This shared sentence stays present across both filing years unchanged."
    d = sentence_diff("Our first-year risk sentence was long and detailed. " + shared,
                      shared + " A brand new second-year sentence appears here now.")
    assert d["n_added"] >= 1 and d["n_removed"] >= 1, f"diff 카운트: {d}"
    print("PASS — story_diff 파싱 5항목 (html→text·섹션추출·목차회피·톤방향·문장diff)")


def main() -> int:
    ap = argparse.ArgumentParser(description="10-K 연도별 스토리 diff (프로토타입)")
    ap.add_argument("ticker", nargs="?", help="티커 (--selftest 시 생략)")
    ap.add_argument("--section", choices=["risk", "mda", "full"], default="risk",
                    help="대조 섹션 (기본 risk=Item 1A). 추출 실패 시 전문 폴백")
    ap.add_argument("--ua", default=_UA_DEFAULT, help="SEC User-Agent (실연락처 권장)")
    ap.add_argument("--selftest", action="store_true", help="네트워크 없이 파싱 로직 검증")
    args = ap.parse_args()
    if args.selftest:
        _selfcheck()
        return 0
    if not args.ticker:
        ap.error("ticker 필요 (또는 --selftest)")
    print(report(args.ticker, args.section, args.ua))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
