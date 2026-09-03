"""pytest 엔트리 — 각 스테이지 회귀 스위트를 실행하고 실패 0 을 단언.

스위트 파일은 'tests_stage*.py'(복수형 prefix)라 pytest 기본 수집(test_*.py)에 안 걸린다.
이 파일(test_suites.py)만 수집돼 각 스위트의 main()을 호출, 반환코드 0(전부 PASS)을 확인.

  pytest -q          # 전체
  pytest -q -k stage4
"""
import importlib
import os
import sys

# 스위트는 tests/ 로 이동(루트 정리) + bare 모듈명 cross-import(from tests_stage1 import…) 유지
# → 루트와 tests/ 를 path 에 올려 importlib.import_module(bare) 와 cross-import 둘 다 해결.
_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 스위트 픽스처가 실제 텔레그램/슬랙 알림을 쏘지 않게(tools/run_tests.py 와 동일 정책)
os.environ["USTRADE_NOTIFY_OFF"] = "1"

import pytest

SUITES = [f"tests_stage{i}" for i in range(1, 9)] + ["tests_canslim", "tests_toss",
                                                     "tests_managed", "tests_exit", "tests_hardening",
                                                     "tests_panic", "tests_review",
                                                     "tests_personas", "tests_intraday",
                                                     "tests_selection_review", "tests_alpha_zoo",
                                                     "tests_slippage", "tests_report_html",
                                                     "tests_mcp", "tests_dividends"]


@pytest.mark.parametrize("mod_name", SUITES)
def test_stage_suite(mod_name):
    mod = importlib.import_module(mod_name)
    rc = mod.main()
    assert rc == 0, f"{mod_name}: 실패 {getattr(mod, 'FAIL', '?')}"
