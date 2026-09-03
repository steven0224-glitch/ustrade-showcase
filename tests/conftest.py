"""pytest 경로 부트스트랩 — 프로젝트 루트와 tests/ 를 sys.path 에 올린다.

스위트는 루트 정리로 tests/ 로 이동했지만 bare 모듈명 cross-import(from tests_stage1
import …)와 프로젝트 모듈 import(import broker …)를 그대로 쓴다. 두 경로를 path 에
올려 pytest·직접실행 어느 쪽이든 import 가 해결되게 한다. (CI 게이트는 tools/run_tests.py
가 동일하게 처리.)
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for _p in (_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 스위트 픽스처가 실제 텔레그램/슬랙 알림을 쏘지 않게(tools/run_tests.py 와 동일 정책)
os.environ["USTRADE_NOTIFY_OFF"] = "1"
