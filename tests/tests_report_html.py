"""report_html 렌더 검증 — 네트워크 0 (합성 dims). 관측·리포트 전용 산출물.

실행:  python tests_report_html.py
"""
import report_html as R

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


_DIMS = {
    "persona": {"oneil": {"n": 5, "avg": 0.08, "median": 0.06, "hit": 0.6},
                "wood": {"n": 4, "avg": -0.03, "median": -0.02, "hit": 0.25}},
    "canslim": {"in": {"n": 6, "avg": 0.05, "median": 0.04, "hit": 0.66},
                "out": {"n": 3, "avg": -0.01, "median": 0.0, "hit": 0.33}},
    "_overall": {"n": 9, "avg": 0.03, "median": 0.02, "hit": 0.55},
}


def test_bar_chart():
    print("[CHART] _bar_chart base64 / 빈 데이터 None")
    b = R._bar_chart("t", ["a", "b"], [0.05, -0.02])
    check("base64 문자열 반환", isinstance(b, str) and len(b) > 100)
    check("빈 라벨 → None", R._bar_chart("t", [], []) is None)


def test_dim_table():
    print("[TABLE] _dim_table — 버킷·부호클래스")
    html = R._dim_table(_DIMS, "persona", "페르소나")
    check("oneil 버킷 포함", "oneil" in html)
    check("양수 pos 클래스", "class='pos'>+8.00%" in html)
    check("음수 neg 클래스", "class='neg'>-3.00%" in html)
    check("빈 차원 → 빈 문자열", R._dim_table(_DIMS, "nonexistent", "x") == "")


def test_realized_section_guarded():
    print("[REAL] _realized_section — 실패/무데이터에도 문자열(무해)")
    s = R._realized_section()
    check("문자열 반환(throw 안 함)", isinstance(s, str))


def test_build_html_integration():
    print("[BUILD] build_html — 유효 HTML(빈 저널이어도)")
    html = R.build_html(horizon=20)
    check("doctype 시작", html.strip().startswith("<!doctype"))
    check("배너(자동변경 안 함) 포함", "자동변경되지 않음" in html)
    check("</html> 닫힘", html.strip().endswith("</html>"))


def main():
    print("=" * 60)
    print("report_html 렌더")
    print("=" * 60)
    test_bar_chart()
    test_dim_table()
    test_realized_section_guarded()
    test_build_html_integration()
    print("-" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("실패:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
