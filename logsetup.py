"""구조화 로깅 — 회전 파일 핸들러 + 콘솔. 무인 운영 로그가 무한 증가하지 않게.

USTRADE_LOG_LEVEL(기본 INFO)로 레벨 조정. 파일은 LOG_DIR/ustrade.log (2MB×5 회전).
get_logger("name") → 'ustrade.name' 자식 로거.
"""
import logging
import os
from logging.handlers import RotatingFileHandler

from paths import LOG_DIR

_ROOT = "ustrade"
_configured = False


def _configure():
    global _configured
    if _configured:
        return
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        root = logging.getLogger(_ROOT)
        root.setLevel(os.environ.get("USTRADE_LOG_LEVEL", "INFO").upper())
        fh = RotatingFileHandler(LOG_DIR / "ustrade.log", maxBytes=2_000_000,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s"))
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(fh)
        root.addHandler(ch)
        root.propagate = False
    except Exception:
        pass   # 로깅 설정 실패가 거래를 막지 않게
    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    _configure()
    return logging.getLogger(f"{_ROOT}.{name}" if name else _ROOT)
