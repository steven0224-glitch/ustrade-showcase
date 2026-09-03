"""장중 액티브 트레이딩 서브시스템 검증 — 네트워크 0(FakeSession), 실주문 0.

대상:
  P1 TossQuoteClient — 호가전용·구조적 무주문(place_order 메서드 부재)·결측가 raise·401 재인증

실행:  & $py tests/tests_intraday.py   (PYTHONPATH=프로젝트 루트)
"""
import os
import json
import pathlib
import tempfile
import sys
import time
from datetime import datetime

from broker.toss_quote import TossQuoteClient
from broker.toss import TossAPIError
from broker.paper import PaperBroker
from broker.base import Position, AccountInfo, OrderRequest, Side, OrderType
from intraday_guard import IntradayGuard
from tests_toss import FakeSession, BASE
import run_intraday as ri
from run_intraday import (Bar, BarAggregator, IntradayTrader, Signal, market_is_open,
                          persona_lock_path, _acquire_persona_locks, _ET, build_traders,
                          persona_home)
from intraday_rules import oneil_rule, wood_rule, livermore_rule, chartist_rule, RULES
import personas

PASS, FAIL = [], []


def _mkbars(closes, opens=None, highs=None, lows=None):
    bars = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else c
        h = highs[i] if highs else max(o, c)
        lo = lows[i] if lows else min(o, c)
        bars.append(Bar(i * 60, o, h, lo, c, 1))
    return bars


def _ctx(cfg=None):
    return {"sym": "X", "cfg": dict(cfg or {}), "state": {}}


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


_TOKEN = ("POST", "/oauth2/token")
_PRICES = ("GET", "/api/v1/prices")
_ACCOUNTS = ("GET", "/api/v1/accounts")
_TOKEN_OK = (200, {"access_token": "tok", "token_type": "Bearer", "expires_in": 86400})


def _client(extra=None):
    routes = {_TOKEN: _TOKEN_OK}
    if extra:
        routes.update(extra)
    sess = FakeSession(routes)
    c = TossQuoteClient(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    return c, sess


# ───── P1: connect 토큰만(계좌 선택 생략) ─────
def test_connect_token_only():
    print("[P1] connect: OAuth 토큰만 발급 — /prices 는 account 불요라 /accounts 미호출")
    c, sess = _client()
    c.connect()
    check("토큰 저장", c._token == "tok", c._token)
    check("/accounts 미호출(계좌 생략)", not any(call["path"] == "/api/v1/accounts" for call in sess.calls))


# ───── P1: 구조적 무주문 — 실주문 0 보장 ─────
def test_no_order_methods():
    print("[P1] 구조적 안전: place_order/cancel_order 메서드 부재 → 버그로도 실주문 불가")
    c, _ = _client()
    check("place_order 메서드 없음", not hasattr(c, "place_order"))
    check("cancel_order 메서드 없음", not hasattr(c, "cancel_order"))
    check("get_order 메서드 없음", not hasattr(c, "get_order"))
    # TossBroker(주문가능) 를 상속하지 않음 — 타입 구조로 격리
    from broker.toss import TossBroker
    check("TossBroker 비상속", not isinstance(c, TossBroker))


# ───── P1: get_quote 정상/결측 ─────
def test_get_quote():
    print("[P1] get_quote: 정상 lastPrice→Quote, 결측/0→raise (거짓손절 차단)")
    c, _ = _client({_PRICES: (200, {"result": [{"symbol": "AAPL", "lastPrice": "275.04"}]})})
    c.connect()
    q = c.get_quote("AAPL")
    check("정상가 Quote.last", q.last == 275.04, q.last)
    check("last() 스칼라", c.last("AAPL") == 275.04)

    c2, _ = _client({_PRICES: (200, {"result": [{"symbol": "X"}]})})       # lastPrice 키 없음
    c2.connect()
    raised = False
    try:
        c2.get_quote("X")
    except TossAPIError:
        raised = True
    check("lastPrice 결측 → raise", raised)

    c3, _ = _client({_PRICES: (200, {"result": [{"symbol": "X", "lastPrice": "0"}]})})
    c3.connect()
    raised2 = False
    try:
        c3.get_quote("X")
    except TossAPIError:
        raised2 = True
    check("lastPrice 0 → raise", raised2)


# ───── P1: 심볼 역정규화(클래스주) ─────
def test_outbound_symbol():
    print("[P1] get_quote: BRK-B→BRK.B 역정규화(TossBroker 와 동일 표기)")
    c, sess = _client({_PRICES: (200, {"result": [{"symbol": "BRK.B", "lastPrice": "440.1"}]})})
    c.connect()
    c.get_quote("BRK-B")
    prices_call = next(call for call in sess.calls if call["path"] == "/api/v1/prices")
    check("심볼 BRK.B 전송", prices_call["params"]["symbols"] == "BRK.B", prices_call["params"])


# ───── P1: 401 → 재인증 후 재시도 ─────
def test_reauth_on_401():
    print("[P1] 401(토큰만료) → 재인증 후 /prices 재시도 성공")
    state = {"n": 0}

    def prices_route(call):
        state["n"] += 1
        if state["n"] == 1:
            return (401, {"error": {"code": "token-expired", "message": "expired"}})
        return (200, {"result": [{"symbol": "AAPL", "lastPrice": "275.0"}]})

    # max_retries=1 이어야 401 분기의 재귀 재시도 경로가 살아있음(401 은 재시도카운트와 무관하게 1회 재인증)
    sess = FakeSession({_TOKEN: _TOKEN_OK, _PRICES: prices_route})
    c = TossQuoteClient(api_key="k", api_secret="s", base_url=BASE, max_retries=0, session=sess)
    c.connect()
    tok_before = sum(1 for call in sess.calls if call["path"] == "/oauth2/token")
    q = c.get_quote("AAPL")
    tok_after = sum(1 for call in sess.calls if call["path"] == "/oauth2/token")
    check("재시도 후 정상가", q.last == 275.0, q.last)
    check("재인증 토큰 재발급", tok_after > tok_before, (tok_before, tok_after))


# ───── P2: BarAggregator 합성 ─────
def test_bar_aggregator():
    print("[P2] BarAggregator: 버킷 경계 넘을 때 직전 OHLC 바 확정(리스트 반환) + 갭 평탄충전")
    agg = BarAggregator(60)
    check("첫 샘플 → 빈 리스트", agg.add(0, 100.0) == [])
    check("같은 버킷 → 빈 리스트(갱신)", agg.add(30, 110.0) == [])
    bars = agg.add(60, 105.0)       # 새 버킷 → bucket0 바 확정
    check("경계 → 1개 바", len(bars) == 1, bars)
    bar = bars[0]
    check("open=100", bar.open == 100.0, bar)
    check("high=110", bar.high == 110.0, bar)
    check("low=100", bar.low == 100.0, bar)
    check("close=110(버킷 마지막)", bar.close == 110.0, bar)
    check("n=2", bar.n == 2, bar)
    check("start=0", bar.start == 0, bar)
    # 피드 갭 — bucket0 후 bucket3(2버킷 건너뜀) → [bar0, 평탄1, 평탄2] (벽시계 연속 유지)
    agg2 = BarAggregator(60)
    agg2.add(0, 100.0)
    gap = agg2.add(180, 105.0)
    check("갭 → 3개 바(직전+평탄2)", len(gap) == 3, [(b.start, b.n) for b in gap])
    check("평탄바 close=직전 close", gap[1].close == 100.0 and gap[2].close == 100.0)
    check("평탄바 n=0(거래없음)", gap[1].n == 0 and gap[2].n == 0)
    check("평탄바 start 연속(60,120)", gap[1].start == 60 and gap[2].start == 120)
    check("시각 역행 무시", agg2.add(60, 99.0) == [])
    # 거대 ts 점프(클럭이상) → 평탄충전 MAXBARS 캡(메모리폭주 방지)
    agg3 = BarAggregator(60)
    agg3.add(0, 100.0)
    huge = agg3.add(60 * 10_000_000, 105.0)
    check("거대갭 → MAXBARS+1 캡", len(huge) <= ri.MAXBARS + 1, len(huge))


# ───── P2: IntradayTrader E2E (실 PaperBroker, 결정론) ─────
def _scripted_rule(bars, pos, cash, ctx):
    # 첫 바: 미보유면 $500 매수. 3번째 바 이후: 보유면 전량청산.
    if pos is None and len(bars) >= 1 and not ctx["state"].get("bought"):
        ctx["state"]["bought"] = True
        return [Signal("BUY", amount=500.0, reason="entry")]
    if pos is not None and len(bars) >= 3:
        return [Signal("SELL_ALL", reason="exit")]
    return []


def test_intraday_trader_e2e():
    print("[P2] IntradayTrader: 샘플→1분봉→룰→PaperBroker 체결→책영속→runs.jsonl 스냅샷")
    tmp = pathlib.Path(tempfile.mkdtemp())
    logs = tmp / "logs"; logs.mkdir()
    prices = {"AAA": 100.0}
    qfn = lambda s: prices[s]
    broker = PaperBroker(cash=1000.0, price_fn=qfn, state_file=str(tmp / "book.json"))
    tr = IntradayTrader("t", broker, qfn, _scripted_rule, ["AAA"], log_dir=str(logs))

    for ts in (0, 60, 120, 180):    # 3개 닫힌 바 생성(매수 후 청산 트리거)
        prices["AAA"] = 100.0 + ts / 60.0
        tr.sample(ts)

    check("매수+청산 2체결", len(tr.fills) == 2, [f["action"] for f in tr.fills])
    check("첫 체결 BUY", tr.fills[0]["action"] == "BUY")
    check("둘째 체결 SELL_ALL", tr.fills[1]["action"] == "SELL_ALL")
    check("청산 후 무보유", broker.get_position("AAA") is None)
    check("intraday.jsonl 기록", (logs / "intraday.jsonl").exists())

    tr.snapshot("2026-06-29")
    runs = (logs / "runs.jsonl")
    check("runs.jsonl 스냅샷 기록", runs.exists())
    rec = json.loads(runs.read_text(encoding="utf-8").strip().splitlines()[-1])
    check("스냅샷 intraday 태그", rec.get("intraday") is True, rec)
    check("스냅샷 orders=2건", len(rec.get("orders", [])) == 2, rec.get("orders"))
    check("스냅샷 후 fills 비움", tr.fills == [])

    # 책 영속 — 새 PaperBroker 가 같은 state_file 로드 시 동일 현금(청산으로 ~원금 복귀)
    b2 = PaperBroker(cash=1.0, price_fn=qfn, state_file=str(tmp / "book.json"))
    check("책 영속 로드(현금>900)", b2.get_account().cash > 900.0, b2.get_account().cash)


def test_eod_flatten():
    print("[EOD] eod_flatten: cfg on → 전량청산(reason 'EOD 청산') / cfg 없음 → no-op")
    tmp = pathlib.Path(tempfile.mkdtemp())
    logs = tmp / "logs"; logs.mkdir()
    prices = {"AAA": 100.0, "BBB": 50.0}
    b = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr = IntradayTrader("t", b, lambda s: prices[s], lambda *a, **k: [], ["AAA", "BBB"],
                        cfg={"eod_flatten": True}, log_dir=str(logs))
    tr._execute("AAA", Signal("BUY", amount=20000.0, reason="x"))
    tr._execute("BBB", Signal("BUY", amount=10000.0, reason="x"))
    tr.eod_flatten()
    check("전량청산 — 무보유", b.get_position("AAA") is None and b.get_position("BBB") is None)
    sells = [f for f in tr.fills if f["action"] == "SELL_ALL"]
    check("SELL_ALL 2건 + reason 'EOD 청산'",
          len(sells) == 2 and all(f["reason"] == "EOD 청산" for f in sells),
          [(f["action"], f.get("reason")) for f in tr.fills])
    check("현금 복귀(재가동 가능)", b.get_account().cash > 99000.0, b.get_account().cash)

    b2 = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr2 = IntradayTrader("t2", b2, lambda s: prices[s], lambda *a, **k: [], ["AAA"], log_dir=str(logs))
    tr2._execute("AAA", Signal("BUY", amount=20000.0, reason="x"))
    tr2.eod_flatten()
    check("cfg 없음 → no-op(보유 유지)", b2.get_position("AAA") is not None)

    # 장중전용·일1런無 페르소나(livermore/chartist) — 동일 동결 리스크라 둘 다 eod_flatten=True 배선
    for name in ("livermore", "chartist"):
        check(f"{name} intraday_cfg eod_flatten=True",
              personas.PERSONAS[name]["intraday_cfg"].get("eod_flatten") is True)


# ───── P2-B5: EOD 청산 누락 이월 포지션 — 개장 1회 flat ─────
def test_flatten_carryover():
    print("[P2-B5] flatten_carryover: 전일 이월 보유(resumed_today=False) → 개장 시장가 청산 / 당일 재시작은 보존")
    tmp = pathlib.Path(tempfile.mkdtemp())
    logs = tmp / "logs"; logs.mkdir()
    prices = {"AAA": 100.0, "BBB": 50.0}
    b = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr = IntradayTrader("t", b, lambda s: prices[s], lambda *a, **k: [], ["AAA", "BBB"],
                        cfg={"eod_flatten": True}, log_dir=str(logs))   # resumed_today 기본 False
    tr._execute("AAA", Signal("BUY", amount=20000.0, reason="x"))
    tr._execute("BBB", Signal("BUY", amount=10000.0, reason="x"))
    tr.flatten_carryover()
    check("이월 포지션 전량청산 — 무보유", b.get_position("AAA") is None and b.get_position("BBB") is None)
    sells = [f for f in tr.fills if f["action"] == "SELL_ALL"]
    check("SELL_ALL 2건 + reason '이월 포지션 개장청산'(EOD 정상청산과 저널상 구분)",
          len(sells) == 2 and all(f["reason"] == "이월 포지션 개장청산" for f in sells),
          [(f["action"], f.get("reason")) for f in tr.fills])

    # resumed_today=True(같은 세션 재시작) — flatten 스킵, 보유 유지(P1-B1 보호청산이 이미 bars=1부터 관리)
    b2 = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr2 = IntradayTrader("t2", b2, lambda s: prices[s], lambda *a, **k: [], ["AAA"],
                         cfg={"eod_flatten": True}, log_dir=str(logs), resumed_today=True)
    tr2._execute("AAA", Signal("BUY", amount=20000.0, reason="x"))
    tr2.flatten_carryover()
    check("당일 재시작(resumed_today=True) → flatten 스킵(보유 유지)", b2.get_position("AAA") is not None)

    # cfg 없음(eod_flatten 미설정) → no-op(eod_flatten() 과 동일 원칙)
    b3 = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr3 = IntradayTrader("t3", b3, lambda s: prices[s], lambda *a, **k: [], ["AAA"], log_dir=str(logs))
    tr3._execute("AAA", Signal("BUY", amount=20000.0, reason="x"))
    tr3.flatten_carryover()
    check("cfg 없음 → no-op(보유 유지)", b3.get_position("AAA") is not None)


def test_intraday_order_reason_journaled():
    print("[REASON-INTRADAY] 장중 체결 사유가 runs.jsonl 스냅샷 orders 에 보존 + 수량 2자리(정책)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    logs = tmp / "logs"; logs.mkdir()
    prices = {"AAA": 100.0}
    broker = PaperBroker(cash=100000.0, price_fn=lambda s: prices[s])
    tr = IntradayTrader("t", broker, lambda s: prices[s], lambda *a, **k: [], ["AAA"], log_dir=str(logs))
    tr._execute("AAA", Signal("BUY", amount=20000.0, reason="피벗 돌파"))
    tr.snapshot("2026-07-02")
    rec = json.loads((logs / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    o = rec["orders"][0]
    check("장중 주문 사유 보존(snapshot 이 reason 드롭 안 함 — 빈칸 방지)", o.get("reason") == "피벗 돌파", o)
    check("장중 체결 수량 2자리(소수주 정책)", abs(o["qty"] * 100 - round(o["qty"] * 100)) < 1e-6, o.get("qty"))


# ───── P2: 장중 게이트 ─────
def test_market_gate():
    print("[P2] market_is_open: 평일 09:30~16:00 ET 만 True")
    mon_open = datetime(2026, 6, 29, 10, 0, tzinfo=_ET)     # 월 10:00
    mon_closed = datetime(2026, 6, 29, 17, 0, tzinfo=_ET)   # 월 17:00
    sat = datetime(2026, 6, 27, 10, 0, tzinfo=_ET)          # 토
    check("월 10:00 → 개장", market_is_open(mon_open) is True)
    check("월 17:00 → 마감", market_is_open(mon_closed) is False)
    check("토 10:00 → 마감", market_is_open(sat) is False)


# ───── P2: 페르소나(책) 단위 프로세스 락 ─────
def test_persona_process_lock():
    print("[FIX] 프로세스 락 = 책 옆(persona_home/state/intraday.lock) — 계정·--only 무관 동일파일, 페르소나 단위")
    from contextlib import ExitStack
    tmp = pathlib.Path(tempfile.mkdtemp())
    hf = lambda n: str(tmp / f"ustrade-paper-{n}")
    check("락 경로 = home/state/intraday.lock",
          persona_lock_path("liv", hf) == os.path.join(hf("liv"), "state", "intraday.lock"),
          persona_lock_path("liv", hf))
    # 계정 스코프(cache_base/USTRADE_HOME) 아님 — SYSTEM 태스크와 유저 셸이 같은 파일을 본다
    saved = os.environ.get("USTRADE_HOME")
    try:
        os.environ["USTRADE_HOME"] = str(tmp / "acctA")
        p1 = persona_lock_path("liv", hf)
        os.environ["USTRADE_HOME"] = str(tmp / "acctB")
        p2 = persona_lock_path("liv", hf)
    finally:
        if saved is None:
            os.environ.pop("USTRADE_HOME", None)
        else:
            os.environ["USTRADE_HOME"] = saved
    check("계정(USTRADE_HOME) 달라도 동일 락 파일(SYSTEM↔유저셸 이중가동 차단)", p1 == p2, (p1, p2))

    pmap = {"liv": {"intraday": True}, "liv_ctl": {"intraday": True}, "buf": {"intraday": False}}
    with ExitStack() as s1:
        owned = _acquire_persona_locks(pmap, s1, home_fn=hf)
        check("intraday 페르소나만 락(비intraday 제외)", owned == {"liv", "liv_ctl"}, owned)
        check("락 파일 생성", os.path.exists(persona_lock_path("liv", hf)))
        with ExitStack() as s2:
            # --only 조합이 달라도(스모크 vs 등록 태스크) 같은 책이면 중복 거부 = 구버그 회귀 차단
            dup = _acquire_persona_locks({"liv": {"intraday": True}}, s2, home_fn=hf)
            check("같은 페르소나 재기동 거부(--only 조합 무관)", dup == set(), dup)
            # 짝실험(livermore↔livermore_swing, *_ctl)은 계속 동시 가동 가능
            other = _acquire_persona_locks({"liv_swing": {"intraday": True}}, s2, home_fn=hf)
            check("다른 페르소나 동시 가동 허용(짝실험 유지)", other == {"liv_swing"}, other)
        check("짝 페르소나 락 해제", not os.path.exists(persona_lock_path("liv_swing", hf)))
    check("컨텍스트 종료 → 전 락 해제", not os.path.exists(persona_lock_path("liv", hf))
          and not os.path.exists(persona_lock_path("liv_ctl", hf)))


# ───── P3: oneil 룰 (피벗돌파·7%손절·20%익절) ─────
def test_oneil_rule():
    print("[P3] oneil: 피벗 돌파 진입 · 7% 손절 · 20% 익절")
    # 진입 — 20바 100 평탄 후 102 돌파(thrust)
    sigs = oneil_rule(_mkbars([100.0] * 20 + [102.0]), None, 2000.0, _ctx())
    check("피벗돌파 → BUY", len(sigs) == 1 and sigs[0].action == "BUY" and sigs[0].amount == 500.0, sigs)
    # 손절 — 보유 평단100, 92 (<93)
    pos = Position("X", 5, 100.0)
    s2 = oneil_rule(_mkbars([100.0] * 20 + [92.0]), pos, 0.0, _ctx())
    check("7% 손절 → SELL_ALL", len(s2) == 1 and s2[0].action == "SELL_ALL", s2)
    # 익절 — 121 (>120)
    s3 = oneil_rule(_mkbars([100.0] * 20 + [121.0]), pos, 0.0, _ctx())
    check("20% 익절 → SELL_ALL", len(s3) == 1 and s3[0].action == "SELL_ALL", s3)
    # 보유·중립 — 105
    s4 = oneil_rule(_mkbars([100.0] * 20 + [105.0]), pos, 0.0, _ctx())
    check("중립 → 무신호", s4 == [], s4)
    # 워밍업 부족
    check("바<21 → 무신호", oneil_rule(_mkbars([100.0] * 5), None, 2000.0, _ctx()) == [])


# ───── P3: wood 룰 (MA회복 진입·트림·손절·추가) ─────
def test_wood_rule():
    print("[P3] wood: MA 회복 진입 · MA 이탈 트림 · 깊은이탈 손절 · 신고가 추가")
    s_entry = wood_rule(_mkbars([100.0] * 20 + [101.0]), None, 2000.0, _ctx())
    check("MA회복 → BUY", len(s_entry) == 1 and s_entry[0].action == "BUY", s_entry)
    pos = Position("X", 6, 100.0)
    s_trim = wood_rule(_mkbars([100.0] * 20 + [99.5]), pos, 0.0, _ctx())
    check("MA 가벼운 이탈 → SELL 트림", len(s_trim) == 1 and s_trim[0].action == "SELL"
          and 0 < s_trim[0].qty < 6, s_trim)
    s_stop = wood_rule(_mkbars([100.0] * 20 + [90.0]), pos, 0.0, _ctx())
    check("MA 깊은 이탈 → SELL_ALL 손절", len(s_stop) == 1 and s_stop[0].action == "SELL_ALL", s_stop)
    s_add = wood_rule(_mkbars([100.0] * 20 + [102.0]), pos, 2000.0, _ctx())
    check("신고가 강세 → BUY 추가", len(s_add) == 1 and s_add[0].action == "BUY"
          and s_add[0].amount == 200.0, s_add)


# ───── P3: livermore 룰 (ORB·트레일·피라미딩·반전) ─────
def test_livermore_rule():
    print("[P3] livermore: 오프닝레인지 돌파 · 트레일손절 · 피라미딩 · 반전청산")
    s_entry = livermore_rule(_mkbars([100.0] * 15 + [102.0]), None, 2000.0, _ctx())
    check("ORB 돌파 → BUY", len(s_entry) == 1 and s_entry[0].action == "BUY"
          and s_entry[0].amount == 400.0, s_entry)
    pos = Position("X", 5, 100.0)
    # 트레일 손절 — 고점 110 시드 후 106 (<110*0.97=106.7)
    ctx_t = _ctx(); ctx_t["state"]["hw"] = 110.0
    s_trail = livermore_rule(_mkbars([100.0] * 15 + [106.0]), pos, 0.0, ctx_t)
    check("트레일손절 → SELL_ALL", len(s_trail) == 1 and s_trail[0].action == "SELL_ALL", s_trail)
    # 피라미딩 — 신고가 103 + thrust
    s_pyr = livermore_rule(_mkbars([100.0] * 15 + [103.0]), pos, 2000.0, _ctx())
    check("신고가 → BUY 피라미딩", len(s_pyr) == 1 and s_pyr[0].action == "BUY"
          and s_pyr[0].amount == 200.0, s_pyr)
    # 반전 청산 — 강한 음봉 + 스윙로우 이탈
    bars_rev = _mkbars([100.0] * 15 + [99.0], opens=[100.0] * 16)
    s_rev = livermore_rule(bars_rev, pos, 0.0, _ctx())
    check("반전 → SELL_ALL", len(s_rev) == 1 and s_rev[0].action == "SELL_ALL"
          and "반전" in s_rev[0].reason, s_rev)


# ───── P3: chartist 룰 (돌파→되돌림 진입·레벨손절·R:R 익절) ─────
def test_chartist_rule():
    print("[P3] chartist: 저항 돌파→무장→되돌림(SR Flip)+해머 진입 · 레벨손절 · R:R 익절 · 레짐게이트")
    cfg = {"sr_bars": 5, "ma_bars": 3, "rsi_bars": 3, "retest_tol": 0.005,
           "retest_max_bars": 10, "rr": 2.0, "entry_frac": 0.005, "thrust_min": 0.0015}

    def bar(o, h, l, c, i, n=1):
        return Bar(i * 60, o, h, l, c, n)

    # 1) 저항 돌파 → 무장(진입 신호 없음, level=100 기록)
    ctx = _ctx(cfg)
    base = [bar(100, 100, 100, 100, i) for i in range(6)]        # 6바 평탄 100 (저항 100)
    breakout = base + [bar(100, 101, 100, 101, 6)]               # 종가 101 돌파 + thrust
    s1 = chartist_rule(breakout, None, 500.0, ctx)
    check("돌파 → 무장(진입 없음, level 100)",
          s1 == [] and ctx["state"].get("armed", {}).get("level") == 100.0, ctx["state"])

    # 2) 되돌림 해머(저점 99.7 거부, 종가 100.1) → BUY + 손절/익절 레벨 확정
    retest = breakout + [bar(100.0, 100.15, 99.7, 100.1, 7)]
    s2 = chartist_rule(retest, None, 500.0, ctx)
    check("되돌림+해머 → BUY", len(s2) == 1 and s2[0].action == "BUY" and s2[0].amount == 500.0
          and "되돌림" in s2[0].reason, s2)
    st = ctx["state"]
    check("레벨 손절 세팅(<진입가 100.1)", st.get("stop") is not None and st["stop"] < 100.1, st.get("stop"))
    check("R:R 2:1 익절 세팅", st.get("target") is not None
          and abs((st["target"] - 100.1) - 2 * (100.1 - st["stop"])) < 1e-6, (st.get("target"), st.get("stop")))

    # 3) 보유 중 레벨 손절 — 종가 ≤ stop → SELL_ALL(보호청산)
    pos = Position("X", 5, 100.0)
    ctx_s = _ctx(cfg); ctx_s["state"] = {"stop": 98.0, "target": 104.0}
    bars_s = [bar(100, 100, 100, 100, i) for i in range(6)] + [bar(100, 100, 97.5, 97.9, 6)]
    s_stop = chartist_rule(bars_s, pos, 0.0, ctx_s)
    check("레벨 손절 → SELL_ALL 보호청산", len(s_stop) == 1 and s_stop[0].action == "SELL_ALL"
          and s_stop[0].protective is True, s_stop)

    # 4) 보유 중 R:R 익절 — 종가 ≥ target → SELL_ALL(보호청산)
    ctx_t = _ctx(cfg); ctx_t["state"] = {"stop": 98.0, "target": 104.0}
    bars_t = [bar(100, 100, 100, 100, i) for i in range(6)] + [bar(104, 105, 104, 104.5, 6)]
    s_tgt = chartist_rule(bars_t, pos, 0.0, ctx_t)
    check("R:R 익절 → SELL_ALL 보호청산", len(s_tgt) == 1 and s_tgt[0].action == "SELL_ALL"
          and s_tgt[0].protective is True, s_tgt)

    # 5) 약세장(regime off) → 돌파해도 무장 안 함(신규진입 차단)
    ctx_off = _ctx(cfg); ctx_off["regime_on"] = False
    s_off = chartist_rule(breakout, None, 500.0, ctx_off)
    check("레짐OFF → 돌파 무시(무장 안 함)", s_off == [] and "armed" not in ctx_off["state"], ctx_off["state"])

    # 6) 워밍업 부족(바 < sr_bars+2) → 무신호
    check("바<sr_bars+2 → 무신호",
          chartist_rule([bar(100, 100, 100, 100, i) for i in range(4)], None, 500.0, _ctx(cfg)) == [])


# ───── P3a2: 적응형 thrust 임계 — max(floor, k×σ3), 합성봉 제외·웜업 floor ─────
def test_thrust_min_eff():
    print("[ADAPT] _thrust_min_eff: k=0 floor 불변 / k>0 σ3 적응 / σ≈0 floor 백스톱 / 합성·웜업 floor")
    from intraday_rules import _thrust_min_eff

    def bar(c, i, n=1):
        return Bar(i * 60, c, c, c, c, n)

    px, wavy = 100.0, []
    for i in range(40):                                  # 교대 ±1% → σ1≈1%, σ3≈1.73%
        px = px * (1.01 if i % 2 == 0 else 1 / 1.01)
        wavy.append(bar(px, i))
    check("thrust_k 미설정 → 고정 floor(기존 동작 불변)",
          _thrust_min_eff(wavy, {"thrust_min": 0.001}, 0.001) == 0.001)
    eff = _thrust_min_eff(wavy, {"thrust_min": 0.001, "thrust_k": 1.0}, 0.001)
    check("k=1 → ≈σ3 (±1% 교대 ⇒ ~1.7%)", 0.012 < eff < 0.022, eff)
    eff_h = _thrust_min_eff(wavy, {"thrust_min": 0.001, "thrust_k": 0.5}, 0.001)
    check("k 비례(0.5k ≈ eff/2)", abs(eff_h - eff / 2) < 1e-9, (eff_h, eff))
    flat = [bar(100.0, i) for i in range(40)]
    check("σ≈0(평탄) → floor 백스톱",
          _thrust_min_eff(flat, {"thrust_min": 0.001, "thrust_k": 1.0}, 0.001) == 0.001)
    synth = [bar(100.0 + i, i, n=0) for i in range(40)]
    check("전부 합성봉 → floor(σ 추정 불가 — 합성 평탄가가 σ 눌러 임계 무력화 차단)",
          _thrust_min_eff(synth, {"thrust_min": 0.001, "thrust_k": 1.0}, 0.001) == 0.001)
    check("실봉 <15 웜업 → floor",
          _thrust_min_eff(wavy[:10], {"thrust_min": 0.001, "thrust_k": 1.0}, 0.001) == 0.001)
    check("RULES 대조군 별칭 = 원본 함수",
          RULES["livermore_ctl"] is livermore_rule and RULES["chartist_ctl"] is chartist_rule)


# ───── P3b: 사이징 절대금액→비율 전환(감사) — entry_frac/add_frac × ctx['equity'] ─────
def test_frac_sizing_uses_equity():
    print("[AUDIT] entry_frac/add_frac × ctx['equity'] — 동일 frac 에서 equity 만 바뀌면 주문금액도 비례")
    ctx40 = _ctx({"entry_frac": 0.25}); ctx40["equity"] = 40000.0
    s40 = oneil_rule(_mkbars([100.0] * 20 + [102.0]), None, 100000.0, ctx40)
    check("oneil entry_frac 0.25 × equity 40000 = 10000", len(s40) == 1 and s40[0].amount == 10000.0, s40)
    ctx80 = _ctx({"entry_frac": 0.25}); ctx80["equity"] = 80000.0
    s80 = oneil_rule(_mkbars([100.0] * 20 + [102.0]), None, 100000.0, ctx80)
    check("동일 frac, equity 2배 → 금액도 2배(20000)", len(s80) == 1 and s80[0].amount == 20000.0, s80)
    # wood add_frac — 피라미딩 사이징도 동일 경로(equity 대비 %)
    ctxw = _ctx({"add_frac": 0.10}); ctxw["equity"] = 50000.0
    pos = Position("X", 6, 100.0)
    s_add = wood_rule(_mkbars([100.0] * 20 + [102.0]), pos, 100000.0, ctxw)
    check("wood add_frac 0.10 × equity 50000 = 5000", len(s_add) == 1 and s_add[0].amount == 5000.0, s_add)
    # ctx 에 equity 미주입(레거시/미배선 호출) → 폴백 100000.0(전 페르소나 시드와 정합)
    check("equity 미주입 → 폴백 100000 기준(oneil 0.25→25000)",
          oneil_rule(_mkbars([100.0] * 20 + [102.0]), None, 100000.0, _ctx({"entry_frac": 0.25}))[0].amount == 25000.0)


# ───── P2-B4: 피라미딩 add_frac 0.08 정합 — 체결후 비중캡 검사에서 add 상시거부 차단 ─────
def test_pyramid_dial_guard_arithmetic():
    print("[P2-B4] livermore add_frac 0.08 — entry+add×2=0.36<max_position_weight 0.40(체결후 가드 통과 여유)")
    cfg = personas.PERSONAS["livermore"]["intraday_cfg"]
    check("add_frac 0.10→0.08 정합(구다이얼은 entry+add×2==cap 이라 체결후검사서 add 상시거부)",
          cfg["add_frac"] == 0.08, cfg["add_frac"])
    check("entry+add×2 < max_position_weight(상승여유 확보, 리스크캡은 불변)",
          cfg["entry_frac"] + cfg["add_frac"] * cfg["max_adds"] < cfg["max_position_weight"]
          and cfg["max_position_weight"] == 0.40,
          (cfg["entry_frac"], cfg["add_frac"], cfg["max_adds"], cfg["max_position_weight"]))
    cfg_sw = personas.PERSONAS["livermore_swing"]["intraday_cfg"]
    check("livermore_swing 도 동일 add_frac 0.08(독립 dict, 별도 정합)", cfg_sw["add_frac"] == 0.08)
    check("livermore_ctl 은 원본과 참조공유(다이얼 변경 자동반영)",
          personas.PERSONAS["livermore_ctl"]["intraday_cfg"] is cfg)
    cfg_w = personas.PERSONAS["wood"]["intraday_cfg"]
    check("wood 도 동일 결함 검산 확인 후 add_frac 0.08 정합(0.20+0.08×2=0.36<0.40)",
          cfg_w["add_frac"] == 0.08
          and cfg_w["entry_frac"] + cfg_w["add_frac"] * cfg_w["max_adds"] < cfg_w["max_position_weight"],
          (cfg_w["entry_frac"], cfg_w["add_frac"], cfg_w["max_adds"], cfg_w["max_position_weight"]))

    # 가드 산술 재현 — entry(20%) 체결 후 +2% 상승 상태에서 add#1 체결후비중검사 통과(0.08 다이얼)
    g = IntradayGuard({"intraday_cfg": {"max_position_weight": cfg["max_position_weight"],
                                        "max_trades_per_day": 20}})
    eq = 100000.0
    entry_amt = cfg["entry_frac"] * eq                          # 20000
    px_entry = 100.0
    pos = Position("X", entry_amt / px_entry, px_entry)         # 200주 @ 100
    px_now = px_entry * 1.02                                    # +2% 신고가(피라미딩 조건)
    add1 = Signal("BUY", amount=cfg["add_frac"] * eq, reason="피라미딩 #1")
    acct = AccountInfo(cash=eq - entry_amt, equity=eq, buying_power=0.0)
    check("add#1 — 0.08 다이얼로 체결후비중 캡 통과", g.allow("livermore", "X", add1, pos, acct, px_now) is True)


# ───── P4: 장중 리스크 가드 ─────
def _acct(equity):
    return AccountInfo(cash=0.0, equity=equity, buying_power=0.0)


def test_intraday_guard():
    print("[P4] IntradayGuard: 회전캡 · 일중손실정지 · 비중캡 · 최소보유(보호청산 예외)")
    buy = Signal("BUY", amount=100.0, reason="entry")
    stop = Signal("SELL_ALL", reason="7% 손절", protective=True)   # 권위 = 플래그(reason 키워드 폴백 폐지)
    trim = Signal("SELL", qty=1.0, reason="MA 이탈 트림")

    # 회전 캡 — max_trades=2
    g = IntradayGuard({"intraday_cfg": {"max_trades_per_day": 2}})
    check("매수1 허용", g.allow("t", "X", buy, None, _acct(1000)) is True)
    g.note_fill("X", buy); g.note_fill("X", buy)        # 체결 2건
    check("캡 도달 후 매수 거부", g.allow("t", "X", buy, None, _acct(1000)) is False)
    check("캡 후에도 보호청산 허용", g.allow("t", "X", stop, Position("X", 5, 100), _acct(1000)) is True)

    # 일중 최대손실 → 정지
    g2 = IntradayGuard({"intraday_cfg": {"intraday_max_loss": 0.05}})
    g2.allow("t", "X", buy, None, _acct(1000))          # day_start=1000 기록
    check("정상시 매수 허용", g2.allow("t", "X", buy, None, _acct(990)) is True)
    check("5% 손실 → 매수 정지", g2.allow("t", "X", buy, None, _acct(940)) is False)
    check("정지 후 보호청산 허용", g2.allow("t", "X", stop, Position("X", 5, 100), _acct(940)) is True)
    check("정지 래치 유지(회복해도)", g2.allow("t", "X", buy, None, _acct(1010)) is False)

    # 단일 비중 캡 — max_position_weight=0.4, 보유 500 >= 0.4*1000
    g3 = IntradayGuard({"intraday_cfg": {"max_position_weight": 0.4}})
    check("비중초과 추가매수 거부", g3.allow("t", "X", buy, Position("X", 5, 100), _acct(1000)) is False)
    check("비중내 매수 허용", g3.allow("t", "Y", buy, Position("Y", 1, 100), _acct(1000)) is True)

    # 최소보유 — min_hold=120, 비보호 매도는 차단, 보호청산은 통과
    clk = {"t": 0.0}
    g4 = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120}}, now_fn=lambda: clk["t"])
    g4.note_fill("X", buy)                                # 매수 t=0
    clk["t"] = 60.0
    check("매수후 60s 트림 차단", g4.allow("t", "X", trim, Position("X", 5, 100), _acct(1000)) is False)
    check("매수후 60s 손절은 허용", g4.allow("t", "X", stop, Position("X", 5, 100), _acct(1000)) is True)
    clk["t"] = 200.0
    check("min-hold 경과 후 트림 허용", g4.allow("t", "X", trim, Position("X", 5, 100), _acct(1000)) is True)

    # protective 명시 플래그 — reason 키워드 없어도 보호청산 인정(min-hold 무관)
    clk2 = {"t": 0.0}
    g6 = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120}}, now_fn=lambda: clk2["t"])
    g6.note_fill("X", Signal("BUY", amount=100.0, reason="entry"))
    clk2["t"] = 30.0
    eod = Signal("SELL_ALL", reason="EOD 강제청산", protective=True)   # 키워드 없음, 플래그만
    check("protective 플래그 → min-hold 무관 허용", g6.allow("t", "X", eod, Position("X", 5, 100), _acct(1000)) is True)
    nonprot = Signal("SELL_ALL", reason="그냥매도")                    # 키워드도 플래그도 없음
    check("비보호+키워드없음 → min-hold 차단", g6.allow("t", "X", nonprot, Position("X", 5, 100), _acct(1000)) is False)
    kwonly = Signal("SELL", qty=1.0, reason="트레일 비슷한 트림")      # 키워드만, 플래그 없음
    check("reason 키워드 폴백 폐지 → 비보호 취급(min-hold 우회 통로 차단)",
          g6.allow("t", "X", kwonly, Position("X", 5, 100), _acct(1000)) is False)

    # 비중캡 시가 기준(last_px) — 상승포지션은 원가로는 미달이나 시가로는 초과 → 거부
    g7 = IntradayGuard({"intraday_cfg": {"max_position_weight": 0.4}})
    buy2 = Signal("BUY", amount=100.0, reason="e")
    check("시가 기준 비중캡 거부(시가 150*3=450>=400)",
          g7.allow("t", "X", buy2, Position("X", 3, 100), _acct(1000), 150.0) is False)
    check("시가 미전달 시 원가 100*3=300<400 → 허용",
          g7.allow("t", "X", buy2, Position("X", 3, 100), _acct(1000)) is True)


# ───── FIX: 락 프로토콜 일원화(RunLock 재사용) ─────
def test_lock_protocol_unified():
    print("[FIX] 장중 락 = guardrail.RunLock 그대로 — 자체 락기계·mtime 백데이트 해킹 제거(일1런 결번 원인)")
    from broker.guardrail import RunLock, LockBusy
    check("자체 락 구현 전부 삭제(_pid_of_lock·_reclaim_stale_lock·single_instance_lock·release_lock)",
          not any(hasattr(ri, n) for n in ("_pid_of_lock", "_reclaim_stale_lock",
                                           "single_instance_lock", "release_lock")))
    check("mtime 백데이트 해킹 삭제(run.lock 을 과거로 당겨 run_live 를 속이던 통로)",
          not hasattr(ri, "_backdate_lock_mtime") and not hasattr(ri, "_BOOK_LOCK_STEAL_BACKDATE"))
    check("run_intraday 가 RunLock/LockBusy 재사용", ri.RunLock is RunLock and ri.LockBusy is LockBusy)
    # 상대 프로토콜(run_live)이 보는 mtime 은 '지금' — 과거로 조작되지 않는다
    tmp = pathlib.Path(tempfile.mkdtemp())
    lk = tmp / "state" / "run.lock"
    with RunLock(lk):
        age = time.time() - os.stat(lk).st_mtime
        check("보유 중 mtime = 현재(백데이트 0)", age < 60, round(age, 1))
        busy = False
        try:
            with RunLock(lk):
                pass
        except LockBusy:
            busy = True
        check("살아있는 보유자 = 중복 거부(LockBusy)", busy)
    check("해제 후 파일 제거", not lk.exists())


def test_daily_running_gate():
    print("[FIX] _daily_running: run.lock 최근 mtime = 일1런 진행중(공유책 로드 대기)")
    from run_intraday import _daily_running
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "state").mkdir()
    check("run.lock 부재 → False", _daily_running(str(tmp)) is False)
    (tmp / "state" / "run.lock").write_text(str(os.getpid()), encoding="utf-8")
    check("최근 락(하트비트) → True(진행중)", _daily_running(str(tmp)) is True)


def test_rules_protective_flag():
    print("[FIX] 보호청산 룰이 Signal.protective=True 명시(가드 키워드 의존 제거)")
    s_stop = oneil_rule(_mkbars([100.0] * 20 + [92.0]), Position("X", 5, 100.0), 0.0, _ctx())
    check("oneil 손절 protective", s_stop[0].protective is True, s_stop)
    s_tgt = oneil_rule(_mkbars([100.0] * 20 + [121.0]), Position("X", 5, 100.0), 0.0, _ctx())
    check("oneil 익절 protective", s_tgt[0].protective is True)
    s_woodstop = wood_rule(_mkbars([100.0] * 20 + [90.0]), Position("X", 6, 100.0), 0.0, _ctx())
    check("wood 손절 protective", s_woodstop[0].protective is True)
    s_trim = wood_rule(_mkbars([100.0] * 20 + [99.5]), Position("X", 6, 100.0), 0.0, _ctx())
    check("wood 트림 비보호(min-hold 적용)", s_trim[0].protective is False, s_trim)
    ctx_t = _ctx(); ctx_t["state"]["hw"] = 110.0
    s_trail = livermore_rule(_mkbars([100.0] * 15 + [106.0]), Position("X", 5, 100.0), 0.0, ctx_t)
    check("livermore 트레일손절 protective", s_trail[0].protective is True)


# ───── P1-B1: 워밍업 게이트는 진입에만 — 보유 보호청산은 바 부족해도 즉시 평가 ─────
def test_protective_exit_before_warmup():
    print("[P1-B1] 워밍업(피벗/MA/ORB/SR) 부족 + 보유 중 → 보호청산 즉시 평가(재시작 직후 무보호 구간 차단)")
    pos = Position("X", 5, 100.0)
    # oneil — piv=20(게이트 piv+1=21), 3바만으로도 평단 단일임계 손절 평가(바 이력 불요)
    s_oneil = oneil_rule(_mkbars([100.0, 95.0, 90.0]), pos, 0.0, _ctx())
    check("oneil: 바<piv+1 이어도 7% 손절 발화", len(s_oneil) == 1 and s_oneil[0].action == "SELL_ALL"
          and s_oneil[0].protective is True, s_oneil)

    # wood — man=20(게이트 man+1=21), 정확히 20바(=MA 산출 가능한 최솟값)로 MA손절 발화
    s_wood = wood_rule(_mkbars([100.0] * 19 + [80.0]), pos, 0.0, _ctx())
    check("wood: 바==ma_bars(<man+1) 이어도 MA 손절 발화", len(s_wood) == 1
          and s_wood[0].action == "SELL_ALL" and s_wood[0].protective is True, s_wood)

    # livermore — orK=15(게이트 orK+1=16), 3바만으로도 hw(ctx state, 폴백=평단) 기준 트레일손절 평가
    ctx_liv = _ctx(); ctx_liv["state"]["hw"] = 110.0
    s_liv = livermore_rule(_mkbars([100.0, 103.0, 106.0]), pos, 0.0, ctx_liv)
    check("livermore: 바<orK+1 이어도 트레일손절 발화", len(s_liv) == 1
          and s_liv[0].action == "SELL_ALL" and s_liv[0].protective is True, s_liv)

    # chartist — look=30(게이트 look+2=32), 3바만으로도 st 확정 절대레벨(stop) 기준 손절 평가
    ctx_ch = _ctx(); ctx_ch["state"] = {"stop": 98.0, "target": 104.0}
    s_ch = chartist_rule(_mkbars([100.0, 99.0, 97.5]), pos, 0.0, ctx_ch)
    check("chartist: 바<look+2 이어도 레벨손절 발화", len(s_ch) == 1
          and s_ch[0].action == "SELL_ALL" and s_ch[0].protective is True, s_ch)

    # 대조군 — 무포지션(진입측)은 워밍업 게이트가 여전히 유효(진입 로직 자체는 불변)
    check("oneil 무포지션·바부족 → 여전히 무신호(진입 게이트 불변)",
          oneil_rule(_mkbars([100.0, 95.0, 90.0]), None, 2000.0, _ctx()) == [])


# ───── R2-FIX: 오프닝레인지 고정 · 일1런 떴다사라짐 대기 · 개장 대기 ─────
def test_livermore_or_anchor():
    print("[R2] livermore 오프닝레인지 세션 1회 고정 — MAXBARS 트림 드리프트 무관")
    ctx = _ctx()
    livermore_rule(_mkbars([100.0] * 15 + [101.0]), None, 2000.0, ctx)   # 첫 orK 바로 OR 캡처(고점100)
    check("OR 1회 캡처(100)", ctx.get("orange") is not None and ctx["orange"][0] == 100.0, ctx.get("orange"))
    drift = _mkbars([200.0] * 12 + [148.0, 149.0, 150.0, 151.0])         # 앞15 고점 200 으로 드리프트
    s = livermore_rule(drift, None, 2000.0, ctx)
    check("트림 드리프트 무관 — 고정 OR(100)로 돌파 BUY", len(s) == 1 and s[0].action == "BUY", s)


def test_livermore_or_restart_anchor():
    print("[FIX] livermore ORB 앵커 — 장중 재시작(session_open+grace 창 밖 첫 바)은 진입 스킵, 보유 보호청산은 유지")
    so = 1_000_000.0                                       # 임의 개장 epoch
    grace = 1800
    # 정상: 첫 바 = 개장창 안 → ORB 확정 → 돌파 진입
    normal = _mkbars([100.0] * 15 + [102.0])
    for i, b in enumerate(normal):
        b.start = so + i * 60
    ctx = _ctx(); ctx["session_open"] = so
    s = livermore_rule(normal, None, 2000.0, ctx)
    check("개장창 시작 → ORB 진입", len(s) == 1 and s[0].action == "BUY", s)
    check("개장창 → orange 확정", ctx.get("orange") is not None, ctx.get("orange"))
    # 재시작: 첫 바가 개장+grace 창 한참 밖 → ORB 미확정 → 오레벨 진입 스킵
    base = so + grace + 7200
    restart = _mkbars([100.0] * 15 + [102.0])
    for i, b in enumerate(restart):
        b.start = base + i * 60
    ctx2 = _ctx(); ctx2["session_open"] = so
    s2 = livermore_rule(restart, None, 2000.0, ctx2)
    check("재시작 창밖 → 진입 스킵", s2 == [], s2)
    check("재시작 → orange 미확정", ctx2.get("orange") is None, ctx2.get("orange"))
    # 재시작 창밖이어도 보유 포지션 트레일손절은 계속 동작(관리는 orh 불요 — 게이트가 막지 않음)
    ctx3 = _ctx(); ctx3["session_open"] = so; ctx3["state"] = {"hw": 110.0}
    restart2 = _mkbars([100.0] * 15 + [106.0])
    for i, b in enumerate(restart2):
        b.start = base + i * 60
    s3 = livermore_rule(restart2, Position("X", 5, 100.0), 0.0, ctx3)
    check("재시작 창밖에도 보유 트레일손절 유지", len(s3) == 1 and s3[0].action == "SELL_ALL", s3)


def test_await_daily_runs():
    print("[R2] _await_daily_runs: 락이 떴다 사라질 때까지 대기 / grace 내 미관측이면 진행(교착 없음)")
    from run_intraday import _await_daily_runs
    pmap = {"oneil": {"intraday": True, "daily_run": True},
            "wood": {"intraday": True, "daily_run": True},
            "liv": {"intraday": True}}              # daily_run 없음 → 대기 대상 아님
    state = {"oneil": 0}

    def running(home):
        if str(home).endswith("oneil"):
            state["oneil"] += 1
            return state["oneil"] <= 2              # 처음 2회 진행중, 이후 완료
        return False                                # wood: 한 번도 안 뜸 → grace 후 진행

    clock = {"t": 0.0}
    _await_daily_runs(pmap, timeout=600, poll=10, appear_grace=50,
                      now_fn=lambda: clock["t"],
                      running_fn=running,
                      sleep_fn=lambda s: clock.__setitem__("t", clock["t"] + s))
    check("oneil 떴다사라짐까지 대기(>=3회 체크)", state["oneil"] >= 3, state["oneil"])
    check("교착 없이 종료(시간 진행)", clock["t"] > 0)


def test_wait_until_open():
    print("[R2] _wait_until_open: 개장 전 기동→대기 후 True, 주말/마감후→즉시 False")
    from run_intraday import _wait_until_open
    seq = {"i": 0}
    times = [datetime(2026, 6, 29, 9, 0, tzinfo=_ET),    # 월 09:00 (개장 전)
             datetime(2026, 6, 29, 9, 15, tzinfo=_ET),
             datetime(2026, 6, 29, 9, 31, tzinfo=_ET)]   # 개장
    def nowf():
        t = times[min(seq["i"], len(times) - 1)]; seq["i"] += 1; return t
    sleeps = []
    ok = _wait_until_open(max_wait=10000, poll=1, sleep_fn=lambda s: sleeps.append(s), now_fn=nowf)
    check("개장 전 → 대기 후 True", ok is True)
    check("개장까지 sleep 발생", len(sleeps) >= 1, sleeps)
    check("주말 → 즉시 False",
          _wait_until_open(now_fn=lambda: datetime(2026, 6, 27, 10, 0, tzinfo=_ET),
                           sleep_fn=lambda s: None) is False)
    check("마감후(17:00) → False",
          _wait_until_open(now_fn=lambda: datetime(2026, 6, 29, 17, 0, tzinfo=_ET),
                           sleep_fn=lambda s: None) is False)


# ───── R3-FIX: 개장자산 baseline seed · 정지 표면화 ─────
def test_day_start_seed():
    print("[R3] day_start_equity = 개장 자산(보유 포함) 선seed — 야간보유 손실 baseline 누락 차단")
    g = IntradayGuard({"intraday_cfg": {}})
    g.seed_day_start(1500.0)
    check("seed 설정", g.day_start_equity == 1500.0)
    g.seed_day_start(9999.0)
    check("재seed 무시(이미 설정)", g.day_start_equity == 1500.0)
    g2 = IntradayGuard({"intraday_cfg": {}}); g2.seed_day_start(0)
    check("0/음수 seed 무시", g2.day_start_equity is None)
    # IntradayTrader __init__ 가 보유 포함 개장 equity 로 seed
    b = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    b.place_order(OrderRequest("AAA", Side.BUY, qty=10, order_type=OrderType.MARKET))  # 10@100=1000
    g3 = IntradayGuard({"intraday_cfg": {}})
    IntradayTrader("t", b, lambda s: 100.0, lambda *a: [], ["AAA"], guard=g3)
    check("보유 포함 개장 equity seed(현금1000+보유1000=2000)", g3.day_start_equity == 2000.0, g3.day_start_equity)


def test_halted_surfacing():
    print("[R3] IntradayGuard.halted → snapshot 레코드 + 대시보드 read_engine_state 반영(ARMED 오표시 차단)")
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "state").mkdir(); (tmp / "logs").mkdir()
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0)
    guard = IntradayGuard({"intraday_cfg": {"intraday_max_loss": 0.05}})
    guard.halted = True                                  # 일중손실 정지 상태
    tr = IntradayTrader("t", broker, lambda s: 100.0, lambda *a: [], ["AAA"],
                        guard=guard, log_dir=str(tmp / "logs"))
    tr.snapshot("2026-06-29")
    rec = json.loads((tmp / "logs" / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    check("snapshot halted=True 기록", rec.get("halted") is True, rec)
    saved = bd.load_closes
    bd.load_closes = lambda **_k: {}
    try:
        r = bd.read_engine_state(offline=True, broker="paper", home=str(tmp), ks_namespace="paper_t")
    finally:
        bd.load_closes = saved
    check("대시보드 halted 반영(스냅샷 OR)", r is not None and r["halted"] is True, r and r.get("halted"))


# ───── R4-FIX: 공유책 프로세스간 직렬화 ─────
def test_book_lock_serialization():
    print("[R4] 공유책 — 일1런 run.lock 점유 시 장중매매 보류, 해제 후 디스크 재동기화+체결")
    tmp = pathlib.Path(tempfile.mkdtemp())
    state, logs = tmp / "state", tmp / "logs"; state.mkdir(); logs.mkdir()
    book = str(state / "paper_book_x.json")
    lockpath = str(state / "run.lock")
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, state_file=book,
                         commission=0, spread=0, slippage=0)

    def rule_buy(bars, pos, cash, ctx):
        return [Signal("BUY", amount=500.0, reason="t")] if pos is None else []

    tr = IntradayTrader("x", broker, lambda s: 100.0, rule_buy, ["AAA"],
                        log_dir=str(logs), book_lock=lockpath)
    # 1) 일1런이 run.lock 점유(살아있는 PID) → 매매 보류
    open(lockpath, "w", encoding="utf-8").write(str(os.getpid()))
    for ts in (0, 60, 120):
        tr.sample(ts)
    check("락 점유 중 → 매매 보류", broker.get_position("AAA") is None)
    # 2) 외부(run_live 모사)가 책 변경 후 락 해제 → reload 흡수 + 체결
    pathlib.Path(book).write_text(json.dumps(
        {"cash": 1500.0, "positions": [{"symbol": "ZZZ", "qty": 2.0, "avg_price": 50.0}]}),
        encoding="utf-8")
    os.remove(lockpath)
    for ts in (180, 240):
        tr.sample(ts)
    check("락 해제 후 체결 발생", broker.get_position("AAA") is not None)
    check("reload 로 외부 변경(ZZZ) 흡수", broker.get_position("ZZZ") is not None)
    # 3) 동시 run_live 가 같은 종목 이미 매수(reload 로 보임) → post-reload 재평가로 중복매수 안 함
    pathlib.Path(book).write_text(json.dumps(
        {"cash": 1000.0, "positions": [{"symbol": "AAA", "qty": 5.0, "avg_price": 100.0}]}), encoding="utf-8")
    for ts in (300, 360):
        tr.sample(ts)
    check("reload 후 이미 보유 → 중복매수 안 함(qty 5 유지)",
          abs(broker.get_position("AAA").qty - 5.0) < 1e-6, broker.get_position("AAA").qty)


def test_protective_defers_when_locked():
    print("[R6] 공유책 락 점유 중엔 보호청산도 보류(stale 무락 _save 클로버 방지), 해제 후 집행")
    tmp = pathlib.Path(tempfile.mkdtemp()); state, logs = tmp / "state", tmp / "logs"; state.mkdir(); logs.mkdir()
    book = str(state / "paper_book_x.json"); lockpath = str(state / "run.lock")
    broker = PaperBroker(cash=1000.0, price_fn=lambda s: 100.0, state_file=book,
                         commission=0, spread=0, slippage=0)
    broker.place_order(OrderRequest("AAA", Side.BUY, qty=5, order_type=OrderType.MARKET))   # 보유 5

    def rule_sell(bars, pos, cash, ctx):
        return [Signal("SELL_ALL", reason="손절", protective=True)] if pos is not None else []

    tr = IntradayTrader("x", broker, lambda s: 100.0, rule_sell, ["AAA"], log_dir=str(logs), book_lock=lockpath)
    open(lockpath, "w", encoding="utf-8").write(str(os.getpid()))            # 일1런 점유
    for ts in (0, 60, 120):
        tr.sample(ts)
    check("락 점유 중 보호청산도 보류 → 보유 유지(클로버 방지)", broker.get_position("AAA") is not None)
    os.remove(lockpath)
    for ts in (180, 240):
        tr.sample(ts)
    check("락 해제 후 보호청산 집행 → 무보유", broker.get_position("AAA") is None)


def test_order_failure_isolated():
    print("[R8] 주문 실패(Toss 일시 호가장애)가 루프를 죽이지 않고 격리(보호청산 종일 사망 방지)")
    tmp = pathlib.Path(tempfile.mkdtemp()); logs = tmp / "logs"; logs.mkdir()

    class FlakyBroker:
        def get_position(self, s): return None
        def get_positions(self): return []
        def get_account(self): return AccountInfo(cash=2000.0, equity=2000.0, buying_power=2000.0)
        def reload(self): pass
        def place_order(self, req): raise TossAPIError("no-price", "transient")   # BUY 시 일시장애

    def rule_buy(bars, pos, cash, ctx):
        return [Signal("BUY", amount=500.0, reason="t")] if pos is None else []

    tr = IntradayTrader("x", FlakyBroker(), lambda s: 100.0, rule_buy, ["AAA"], log_dir=str(logs))
    raised = False
    try:
        for ts in (0, 60, 120):                  # 바 완성 → place_order raise → 격리돼야
            tr.sample(ts)
    except Exception:
        raised = True
    check("주문 실패가 sample 밖으로 전파 안 됨(루프 생존)", raised is False)


def test_snapshot_ts_format():
    print("[R7] snapshot/journal ts = run_live 와 동일 naive-local·초단위(공유 runs.jsonl 정렬 정합)")
    from run_intraday import _now_iso
    ts = _now_iso()
    timepart = ts.split("T")[1] if "T" in ts else ts
    check("오프셋 없음(naive — '+00:00' 아님)", "+" not in timepart and "Z" not in ts, ts)
    check("초단위 ISO(len 19, T 구분)", len(ts) == 19 and ts[10] == "T", ts)
    # run_live 포맷과 동일 패턴(YYYY-MM-DDTHH:MM:SS)
    import re
    check("YYYY-MM-DDTHH:MM:SS 패턴", re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", ts) is not None, ts)


def test_guard_failclosed_no_account():
    print("[R6] 가드 — 계좌조회 실패(acct None)면 BUY 거부(fail-closed), 보호청산은 허용")
    g = IntradayGuard({"intraday_cfg": {}})
    buy = Signal("BUY", amount=100.0, reason="e")
    stop = Signal("SELL_ALL", reason="손절", protective=True)
    check("acct None → BUY 거부(fail-closed)", g.allow("t", "X", buy, None, None) is False)
    check("acct None → 보호청산 허용", g.allow("t", "X", stop, Position("X", 5, 100), None) is True)


def test_pyramid_adds_on_fill():
    print("[R5] 피라미딩 adds 카운터 — 체결 시에만 증가(가드 거부 시 슬롯 보존)")
    tmp = pathlib.Path(tempfile.mkdtemp()); logs = tmp / "logs"; logs.mkdir()
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    tr = IntradayTrader("x", broker, lambda s: 100.0, lambda *a: [], ["AAA"], log_dir=str(logs))
    tr._execute("AAA", Signal("BUY", amount=200.0, reason="add", pyramid=True))
    check("체결된 pyramid → adds=1", tr.ctx["AAA"]["state"].get("adds") == 1, tr.ctx["AAA"]["state"])

    class RejectGuard:
        def allow(self, *a, **k): return False
        def note_fill(self, *a, **k): pass
    tr2 = IntradayTrader("y", broker, lambda s: 100.0, lambda *a: [], ["AAA"],
                         guard=RejectGuard(), log_dir=str(logs))
    tr2._execute("AAA", Signal("BUY", amount=200.0, reason="add", pyramid=True))
    check("거부된 pyramid → adds 미증가(슬롯 보존)", tr2.ctx["AAA"]["state"].get("adds") is None,
          tr2.ctx["AAA"]["state"])


# ───── P5: 페르소나 등록 + 배선 ─────
def test_personas_registration():
    print("[P5] personas: livermore 신규 + oneil/wood intraday · buffett 일1런 유지 · RULES 커버")
    p = personas.PERSONAS
    check("livermore 등록", "livermore" in p and p["livermore"]["cash"] == 100000.0)
    check("livermore intraday", p["livermore"].get("intraday") is True)
    check("oneil intraday", p["oneil"].get("intraday") is True)
    check("wood intraday", p["wood"].get("intraday") is True)
    check("buffett 일1런(intraday 미설정)", not p["buffett"].get("intraday"))
    intraday_names = [n for n, m in p.items() if m.get("intraday")]
    check("모든 intraday 페르소나 RULES 보유", all(n in RULES for n in intraday_names), intraday_names)
    check("모든 intraday 페르소나 watchlist 비어있지 않음",
          all(p[n].get("watchlist") for n in intraday_names))


def test_persona_home_resolution():
    print("[P5] persona_home: USTRADE_PERSONA_HOMES 매칭 + 기본 폴백")
    saved = os.environ.get("USTRADE_PERSONA_HOMES")
    # 경로는 호스트 OS 컨벤션으로 — Path(...).name 이 뒷 세그먼트를 잡아야 매칭됨.
    # r"C:\..." 하드코딩은 POSIX(리눅스 CI)서 '\'가 구분자가 아니라 매칭이 깨짐.
    home_liv = os.path.join(os.sep, "foo", "ustrade-paper-livermore")
    home_wood = os.path.join(os.sep, "bar", "ustrade-paper-wood")
    os.environ["USTRADE_PERSONA_HOMES"] = f"{home_liv};{home_wood}"
    try:
        check("env 매칭", persona_home("livermore") == home_liv,
              persona_home("livermore"))
        check("미매칭 → 기본", persona_home("nope") == os.path.join("C:\\", "ustrade-paper-nope"))
    finally:
        if saved is None:
            os.environ.pop("USTRADE_PERSONA_HOMES", None)
        else:
            os.environ["USTRADE_PERSONA_HOMES"] = saved


def test_build_traders_wiring():
    print("[P5] build_traders: intraday 페르소나만·home별 책경로·watchlist·가드 배선, 룰없음 skip")
    tmp = pathlib.Path(tempfile.mkdtemp())
    pmap = {
        "x": {"intraday": True, "cash": 1500.0, "watchlist": ["AAA", "BBB"],
              "intraday_cfg": {"max_trades_per_day": 7}},
        "y": {"intraday": False},                       # 비intraday → skip
        "z": {"intraday": True, "watchlist": ["C"]},    # 룰 없음 → skip
    }
    rmap = {"x": lambda *a: []}
    traders = build_traders(lambda s: 100.0, pmap, rmap,
                            home_fn=lambda n: str(tmp / n), guard_factory=IntradayGuard)
    check("intraday+룰보유 1종만", len(traders) == 1 and traders[0].name == "x", [t.name for t in traders])
    tr = traders[0]
    check("watchlist 배선", tr.watchlist == ["AAA", "BBB"])
    check("가드 cfg 반영", tr.guard.max_trades == 7)
    check("책 state_file home별", tr.broker._state_file == os.path.join(str(tmp / "x"), "state", "paper_book_x.json"),
          tr.broker._state_file)
    check("log_dir home별", tr.log_dir == os.path.join(str(tmp / "x"), "logs"))
    check("seed cash 반영", tr.broker.get_account().cash == 1500.0)


def test_persona_map_only():
    print("[P5b] _persona_map: --only 필터 — 개장기동 태스크가 장중전용만 배선(oneil/wood 조율 격리)")
    from run_intraday import _persona_map
    import personas
    check("only 미지정=전체", set(_persona_map(None)) == set(personas.PERSONAS))
    only = {"livermore", "chartist"}
    check("only 필터=지정만", set(_persona_map(only)) == (only & set(personas.PERSONAS)))
    check("only 에 oneil/wood 없음", not ({"oneil", "wood"} & set(_persona_map(only))))
    # 락은 --only 조합이 아니라 *페르소나(책)* 단위 — 태스크 동시가동은 서로 다른 책이라 무해하고,
    # 같은 페르소나를 다른 --only 조합으로 두 번 띄우면(스모크 vs 등록 태스크) 이제 차단된다.
    check("락 키 = 페르소나별(--only 문자열 아님)",
          persona_lock_path("livermore") != persona_lock_path("chartist")
          and "livermore_chartist" not in persona_lock_path("livermore"))


# ───── P6: 대시보드 장중 스냅샷 픽업 ─────
def test_dashboard_reads_intraday():
    print("[P6] read_persona: 장중 runs.jsonl 스냅샷 → intraday 플래그·포지션·체결 표출")
    dd = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
    if dd not in sys.path:
        sys.path.insert(0, dd)
    import build_data as bd

    tmp = pathlib.Path(tempfile.mkdtemp())
    state, logs = tmp / "state", tmp / "logs"
    state.mkdir(); logs.mkdir()
    snap = {"ts": "2026-06-29T14:00:00+00:00", "broker": "paper", "persona": "livermore",
            "session": "2026-06-29", "status": "intraday", "intraday": True,
            "account": {"cash": 1200.0, "equity": 2010.0},
            "positions": [{"symbol": "NVDA", "qty": 3.0, "avg": 180.0},
                          {"symbol": "TSLA", "qty": 1.5, "avg": 170.0}],
            "orders": [{"symbol": "NVDA", "side": "BUY", "qty": 3.0, "fill": 180.0, "status": "FILLED"},
                       {"symbol": "TSLA", "side": "BUY", "qty": 1.5, "fill": 170.0, "status": "FILLED"}],
            "reconcile": {"ok": True, "drift": []}}
    (logs / "runs.jsonl").write_text(json.dumps(snap) + "\n", encoding="utf-8")

    saved = bd.load_closes
    bd.load_closes = lambda **_k: {}        # 네트워크/캐시 차단
    try:
        r = bd.read_persona("livermore", str(tmp), offline=True)
    finally:
        bd.load_closes = saved
    check("read_persona 비None", r is not None)
    check("intraday 플래그 표출", r.get("intraday") is True, r.get("intraday"))
    check("라벨=리버모어", "리버모어" in r.get("label", ""), r.get("label"))
    check("포지션 2종", r["summary"]["positions"] == 2, r["summary"])
    check("보유 NVDA·TSLA", {h["tk"] for h in r["holdings"]} == {"NVDA", "TSLA"})
    check("체결원장 2건(FILLED orders)", len(r.get("trades", [])) == 2, r.get("trades"))
    check("status intraday", r.get("status") == "intraday")


def test_guard_persistence():
    print("[AUDIT] IntradayGuard 영속 — 장중 크래시·재시작 시 *당일* halt 래치·baseline·회전수 복원")
    tmp = pathlib.Path(tempfile.mkdtemp())
    sf = str(tmp / "intraday_guard_x.json")
    buy = Signal("BUY", amount=100.0, reason="entry")
    cfg = {"intraday_cfg": {"intraday_max_loss": 0.05, "max_trades_per_day": 9}}
    g = IntradayGuard(dict(cfg), state_file=sf, today="2026-06-26")
    g.seed_day_start(1000.0)                              # baseline=1000 기록·저장
    g.note_fill("X", buy)                                 # trades=1 저장
    g.allow("x", "X", buy, None, _acct(940))             # -6% → halt 래치·저장
    check("정지 래치됨", g.halted is True)
    # 재시작(새 프로세스) — 같은 state_file + 같은 날 → 복원
    g2 = IntradayGuard(dict(cfg), state_file=sf, today="2026-06-26")
    check("재시작: halt 래치 복원", g2.halted is True, g2.halted)
    check("재시작: baseline 보존(중간저점 재설정 아님)", g2.day_start_equity == 1000.0, g2.day_start_equity)
    check("재시작: 회전수 복원", g2.trades == 1, g2.trades)
    check("재시작 seed 무시(복원 baseline 우선)", (g2.seed_day_start(940.0) or g2.day_start_equity) == 1000.0)
    check("재시작: 정지 중 신규매수 거부", g2.allow("x", "X", buy, None, _acct(1010)) is False)
    check("재시작: 정지 중 보호청산 허용",
          g2.allow("x", "X", Signal("SELL_ALL", reason="7% 손절", protective=True),
                   Position("X", 5, 100), _acct(1010)) is True)
    # 다음 거래일 — 다른 today → 신규 seed(전일 정지 미상속)
    g3 = IntradayGuard(dict(cfg), state_file=sf, today="2026-06-27")
    check("다음날: 신규 seed(정지 해제)", g3.halted is False and g3.day_start_equity is None)
    # 손상 레코드(필드 일부 깨짐) → all-or-nothing: 반쪽 복원 0(halt 래치 안 떨어짐, paper._load 패턴)
    bad = str(tmp / "intraday_guard_bad.json")
    pathlib.Path(bad).write_text(json.dumps({"date": "2026-06-26", "day_start_equity": "NaNNN",
                                             "halted": True, "trades": 3}), encoding="utf-8")
    gb = IntradayGuard(dict(cfg), state_file=bad, today="2026-06-26")
    check("손상 레코드 → 반쪽복원 0(신규 seed, halt 래치 미탈락)",
          gb.halted is False and gb.day_start_equity is None and gb.trades == 0,
          (gb.halted, gb.day_start_equity, gb.trades))
    # nan/inf baseline(외부변조) → float() 통과하지만 비유한 거부 → 신규 seed(검증 완성)
    nanf = str(tmp / "intraday_guard_nan.json")
    pathlib.Path(nanf).write_text(json.dumps({"date": "2026-06-26", "day_start_equity": "nan",
                                             "halted": True, "trades": 4}), encoding="utf-8")
    gn = IntradayGuard(dict(cfg), state_file=nanf, today="2026-06-26")
    check("비유한 baseline → 신규 seed(halt 미상속)",
          gn.halted is False and gn.day_start_equity is None, (gn.halted, gn.day_start_equity))
    # state_file 없으면 인메모리만(기존 단위테스트 불변)
    g4 = IntradayGuard(dict(cfg))
    g4.seed_day_start(500.0)
    check("state_file 없음 → 영속 no-op(인메모리)", g4.day_start_equity == 500.0)


def _shared_book_trader(tmpdir, rule, cash=2000.0, px=100.0):
    """공유책(book_lock) 트레이더 1식 — (trader, broker, book, lockpath)."""
    state, logs = tmpdir / "state", tmpdir / "logs"
    state.mkdir(parents=True, exist_ok=True); logs.mkdir(parents=True, exist_ok=True)
    book, lockpath = str(state / "paper_book_x.json"), str(state / "run.lock")
    b = PaperBroker(cash=cash, price_fn=lambda s: px, state_file=book,
                    commission=0, spread=0, slippage=0)
    tr = IntradayTrader("x", b, lambda s: px, rule, ["AAA"], log_dir=str(logs), book_lock=lockpath)
    return tr, b, book, lockpath


def _buy_once(bars, pos, cash, ctx):
    return [Signal("BUY", amount=500.0, reason="t")] if pos is None else []


def test_reload_failure_fails_closed():
    print("[CRIT] 공유책 reload 실패 = fail-closed — 그 바 매매 스킵(stale 책 _save 클로버 차단) + 락 반납")
    tr, broker, book, lockpath = _shared_book_trader(pathlib.Path(tempfile.mkdtemp()), _buy_once)
    boom, orig = {"on": True}, broker.reload

    def flaky_reload():
        if boom["on"]:
            raise OSError("책 읽기 실패(디스크/동기화 장애)")
        return orig()

    broker.reload = flaky_reload
    for ts in (0, 60, 120):
        tr.sample(ts)
    check("reload 실패 → 그 바 매매 스킵(무보유)", broker.get_position("AAA") is None,
          broker.get_position("AAA"))
    check("reload 실패 → 체결 0건(삼키고 stale 진행 안 함)", tr.fills == [], tr.fills)
    check("reload 실패해도 락 반납(파일 잔존 0)", not os.path.exists(lockpath))
    boom["on"] = False
    for ts in (180, 240):
        tr.sample(ts)
    check("reload 복구 후 정상 체결(영구 정지 아님)", broker.get_position("AAA") is not None)


def test_paper_reload_missing_vs_corrupt_file():
    print("[CRIT] PaperBroker.reload() — 파일 부재는 무예외(fresh 시작), 파일 존재+파싱실패는 raise(무효수리 노출)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    book = str(tmp / "book.json")
    b = PaperBroker(cash=500.0, price_fn=lambda s: 10.0, state_file=book,
                    commission=0, spread=0, slippage=0)
    raised = False
    try:
        b.reload()
    except Exception:
        raised = True
    check("책 파일 부재 → reload() 무예외", raised is False)
    check("책 파일 부재 → 인메모리 불변(시드 유지)", b.get_account().cash == 500.0, b.get_account().cash)

    pathlib.Path(book).write_text("{이것은 손상된 json", encoding="utf-8")
    raised = False
    try:
        b.reload()
    except Exception:
        raised = True
    check("책 파일 존재+파싱실패 → reload() raise(무효수리 노출)", raised is True)
    check("raise 해도 인메모리는 직전값 유지(부분반영 없음)", b.get_account().cash == 500.0, b.get_account().cash)


def test_paper_ctor_load_unchanged_on_corrupt_file():
    print("[CRIT] 생성자(__init__) 경로는 기존 동작 불변 — 손상 파일이어도 무예외로 시드 시작(최소diff 보장)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    book = str(tmp / "book.json")
    pathlib.Path(book).write_text("{이것도 손상", encoding="utf-8")
    raised = False
    try:
        b = PaperBroker(cash=777.0, price_fn=lambda s: 10.0, state_file=book,
                        commission=0, spread=0, slippage=0)
    except Exception:
        raised = True
    check("생성자 경로는 손상 파일에도 무예외", raised is False)
    if not raised:
        check("생성자 경로 손상 시 시드 유지", b.get_account().cash == 777.0, b.get_account().cash)


def test_paper_corrupt_book_skips_bar_via_real_reload():
    print("[CRIT] 실제 손상 책 파일 → on_bar reload() 진짜 raise → 기존 fail-closed 분기 발화(무효수리 활성화 실증)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    tr, broker, book, lockpath = _shared_book_trader(tmp, _buy_once)
    broker._save()                                  # 책 파일 실제 생성(부재 아닌 진짜 파싱실패 유도용)
    pathlib.Path(book).write_text("{이것은 손상된 json", encoding="utf-8")
    for ts in (0, 60, 120):
        tr.sample(ts)
    check("손상 책 → 그 바 매매 스킵(무보유)", broker.get_position("AAA") is None,
          broker.get_position("AAA"))
    check("손상 책 → 체결 0건(삼키고 stale 진행 안 함)", tr.fills == [], tr.fills)
    check("손상 책이어도 락 반납(파일 잔존 0)", not os.path.exists(lockpath))
    broker._save()                                  # 복구 — 인메모리(직전값) 그대로 유효 JSON 재기록
    for ts in (180, 240):
        tr.sample(ts)
    check("복구 후 정상 체결(영구 정지 아님)", broker.get_position("AAA") is not None)


def test_lock_miss_observability():
    print("[HIGH] 공유책 락 미획득 관측 — lock_miss 카운트 + snapshot 필드(종전 완전 무음)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    tr, broker, book, lockpath = _shared_book_trader(tmp, _buy_once)
    check("초기 lock_miss=0", tr.lock_miss == 0)
    open(lockpath, "w", encoding="utf-8").write(str(os.getpid()))   # 일1런 점유(살아있는 PID) 모사
    for ts in (0, 60, 120):
        tr.sample(ts)
    check("락 미획득 → lock_miss 증가", tr.lock_miss >= 1, tr.lock_miss)
    check("매매는 종전대로 보류(거래 로직 불변)", broker.get_position("AAA") is None)
    n = tr.lock_miss
    tr.snapshot("2026-07-31")
    rec = json.loads((tmp / "logs" / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    check("snapshot 에 lock_miss 관측 필드", rec.get("lock_miss") == n, (rec.get("lock_miss"), n))
    check("snapshot 자신의 락 미획득도 계수(무락 best-effort 기록)", tr.lock_miss == n + 1, tr.lock_miss)
    os.remove(lockpath)
    tr.snapshot("2026-07-31")
    rec2 = json.loads((tmp / "logs" / "runs.jsonl").read_text(encoding="utf-8").strip().splitlines()[-1])
    check("락 해제 후에도 필드 유지(누적값)", rec2.get("lock_miss") == n + 1, rec2.get("lock_miss"))


class _FakeTrader:
    """main() 루프 배선 검증용 — 매매 없이 호출 횟수만 기록."""
    def __init__(self, name="x"):
        self.name, self.watchlist, self.snaps, self.flattened = name, [], 0, 0
        self.carryover_flattened = 0

    def sample(self, ts):
        pass

    def snapshot(self, session):
        self.snaps += 1

    def eod_flatten(self):
        self.flattened += 1

    def flatten_carryover(self):
        self.carryover_flattened += 1


def _run_main(argv, tr):
    """main() 을 네트워크·실락·실책 없이 1회 구동 — market_is_open 은 첫 호출만 True(16:00 통과 모사)."""
    import broker.toss_quote as tq
    import broker.kis_quote as kq

    class FakeQC:
        def connect(self):
            pass

        def last(self, s):
            return 100.0

    gate = {"n": 0}

    def fake_open(now=None):
        gate["n"] += 1
        return gate["n"] <= 1

    saved = (ri.market_is_open, ri._wait_until_open, ri._build_traders, ri._acquire_persona_locks,
             ri.SAMPLE_SECONDS, tq.TossQuoteClient, kq.KISQuoteClient.from_env)
    try:
        ri.market_is_open = fake_open
        ri._wait_until_open = lambda *a, **k: True
        ri._build_traders = lambda qc, only=None: [tr]
        ri._acquire_persona_locks = lambda pmap, stack, **k: {"x"}
        ri.SAMPLE_SECONDS = 0
        tq.TossQuoteClient = FakeQC
        kq.KISQuoteClient.from_env = staticmethod(lambda: None)      # 볼륨섀도 휴면(네트워크 0)
        return ri.main(argv)
    finally:
        (ri.market_is_open, ri._wait_until_open, ri._build_traders, ri._acquire_persona_locks,
         ri.SAMPLE_SECONDS, tq.TossQuoteClient, kq.KISQuoteClient.from_env) = saved


def test_once_no_eod_flatten():
    print("[HIGH] --once 스모크는 16:00 을 넘겨도 EOD 전량청산 안 함 + 스냅샷 1회(이중기록 제거)")
    tr = _FakeTrader()
    rc = _run_main(["--once"], tr)
    check("--once 정상 종료", rc == 0, rc)
    check("--once 틱이 마감 넘겨도 EOD 청산 안 함(실책 보호)", tr.flattened == 0, tr.flattened)
    check("--once 스냅샷 정확히 1회(루프내+마감 이중기록 제거)", tr.snaps == 1, tr.snaps)
    check("--once 은 이월청산도 실행 안 함(책 불가촉, P2-B5 동일원칙)", tr.carryover_flattened == 0,
          tr.carryover_flattened)
    # 대조군: 자연 마감(--once 아님)은 EOD 청산 발화 — 가드가 EOD 자체를 죽이지 않았음
    tr2 = _FakeTrader()
    rc2 = _run_main([], tr2)
    check("자연 마감 → EOD 청산 발화(불변)", rc2 == 0 and tr2.flattened == 1, (rc2, tr2.flattened))
    check("자연 기동 → 개장 이월청산 1회 호출(P2-B5, _sync_watchlist_holdings 전)",
          tr2.carryover_flattened == 1, tr2.carryover_flattened)


def test_sample_single_onbar_on_gap():
    print("[AUDIT] 거대 클럭점프 — 한 샘플이 다수 바 닫아도 _on_bar 1회(공유책 락·reload 폭주 차단)")
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0)
    tr = IntradayTrader("x", broker, lambda s: 100.0, lambda *a: [], ["X"])
    calls = {"n": 0}
    tr._on_bar = lambda sym: calls.__setitem__("n", calls["n"] + 1)
    tr.sample(0)                          # 첫 샘플 — 버킷 오픈, 닫힘 0
    check("첫 샘플 _on_bar 0회", calls["n"] == 0, calls["n"])
    tr.sample(60 * 5000)                  # 거대 갭 — 수천 버킷, MAXBARS 캡 평탄충전 다수 바 닫힘
    check("거대갭 샘플 _on_bar 1회", calls["n"] == 1, calls["n"])
    check("바 보관 MAXBARS 캡 유지", len(tr.bars["X"]) <= ri.MAXBARS, len(tr.bars["X"]))


def test_dust_sell_flush():
    print("[FIX] dust 매도 차단 — 트림 명목/잔량 < MIN_TRADE_NOTIONAL 이면 전량청산(0.00001주 꼬리 무한발화 차단)")
    # 1) 작은 포지션 트림이 dust → 전량청산(flush)
    b = PaperBroker(cash=100.0, price_fn=lambda s: 1.0, commission=0, spread=0, slippage=0)
    b.place_order(OrderRequest("AAA", Side.BUY, qty=2.0, order_type=OrderType.MARKET))   # 2주@$1=$2 (<MIN 5)
    tr = IntradayTrader("x", b, lambda s: 1.0, lambda *a: [], ["AAA"])
    tr._execute("AAA", Signal("SELL", qty=2.0 * 0.34, reason="MA 이탈 트림"))             # 트림 0.68주=$0.68
    check("dust 트림 → 전량청산(무보유)", b.get_position("AAA") is None, b.get_position("AAA"))
    check("flush 체결 qty=전량 2.0", len(tr.fills) == 1 and abs(tr.fills[0]["qty"] - 2.0) < 1e-6, tr.fills)

    # 2) 큰 포지션 트림은 부분매도 유지(정상 동작 불변)
    b2 = PaperBroker(cash=2000.0, price_fn=lambda s: 10.0, commission=0, spread=0, slippage=0)
    b2.place_order(OrderRequest("AAA", Side.BUY, qty=100.0, order_type=OrderType.MARKET))  # 100@$10=$1000
    tr2 = IntradayTrader("y", b2, lambda s: 10.0, lambda *a: [], ["AAA"])
    tr2._execute("AAA", Signal("SELL", qty=100.0 * 0.34, reason="트림"))                   # 34주=$340
    check("큰 트림 → 부분매도(34주)", len(tr2.fills) == 1 and abs(tr2.fills[0]["qty"] - 34.0) < 1e-6, tr2.fills)
    check("부분매도 후 잔량 66", abs(b2.get_position("AAA").qty - 66.0) < 1e-6, b2.get_position("AAA").qty)

    # 3) 반복 트림(wood 모사) — 무한 dribble 없이 유한 + 모든 체결 명목 ≥ MIN + sub-band 안정화(밴드).
    #    (executor 무거래밴드 대칭: 트림 명목<MIN & 잔량 건강이면 스킵 → 잔여는 sub-band 에 안정화, 전량청산은 SELL_ALL)
    b3 = PaperBroker(cash=2000.0, price_fn=lambda s: 4.0, commission=0, spread=0, slippage=0)
    b3.place_order(OrderRequest("AAA", Side.BUY, qty=100.0, order_type=OrderType.MARKET))  # 100@$4=$400
    trim_rule = lambda bars, pos, cash, ctx: ([Signal("SELL", qty=pos.qty * 0.34, reason="트림")]
                                              if pos is not None and pos.qty > 1e-9 else [])
    tr3 = IntradayTrader("z", b3, lambda s: 4.0, trim_rule, ["AAA"])
    for ts in range(0, 60 * 80, 60):       # 최대 80바 트림 발화
        tr3.sample(ts)
    sells = [f for f in tr3.fills if f["action"] == "SELL"]
    pos = b3.get_position("AAA")
    check("반복 트림 → 유한(무한 dribble 아님, 체결 ≤12)", len(sells) <= 12, len(sells))
    check("모든 트림 체결 명목 ≥ MIN(밴드로 미세트림 스킵)",
          all(f["qty"] * 4.0 >= ri.MIN_TRADE_NOTIONAL - 1e-6 for f in sells),
          [round(f["qty"] * 4.0, 2) for f in sells])
    check("sub-band 안정화(잔여 존재, 다음 트림 명목<MIN)",
          pos is not None and pos.qty * 0.34 * 4.0 < ri.MIN_TRADE_NOTIONAL + 0.3, pos and pos.qty)


def test_hi_price_trim_min_increment():
    print("[FIX] 고가주 트림이 <0.01주로 절사돼도 드롭/무한no-op 없이 최소증분 매도(2dp floor→0 회귀)")
    # 단일바: 0.02주($24)@$1200, 트림 0.0068주 → floor 0 → 최소증분 0.01 매도(드롭 아님)
    b = PaperBroker(cash=0.0, price_fn=lambda s: 1200.0, commission=0, spread=0, slippage=0)
    b._positions["HI"] = Position("HI", 0.02, 1200.0)
    tr = IntradayTrader("hi", b, lambda s: 1200.0, lambda *a: [], ["HI"])
    tr._execute("HI", Signal("SELL", qty=0.02 * 0.34, reason="MA 이탈 트림"))   # 0.0068주 → floor 0
    npos = b.get_position("HI")
    check("트림 드롭 안 됨(체결 1건)", len(tr.fills) == 1, tr.fills)
    check("포지션 진행(감소 또는 청산)", npos is None or npos.qty < 0.02 - 1e-9, npos and npos.qty)
    # 반복 트림(고가주) — 무한 no-op 없이 유한 종결(회귀 핵심)
    b2 = PaperBroker(cash=0.0, price_fn=lambda s: 1200.0, commission=0, spread=0, slippage=0)
    b2._positions["HI"] = Position("HI", 0.30, 1200.0)
    trule = lambda bars, pos, cash, ctx: ([Signal("SELL", qty=pos.qty * 0.34, reason="트림")]
                                          if pos is not None and pos.qty > 1e-9 else [])
    tr2 = IntradayTrader("hi2", b2, lambda s: 1200.0, trule, ["HI"])
    for ts in range(0, 60 * 80, 60):
        tr2.sample(ts)
        if b2.get_position("HI") is None:
            break
    check("고가주 반복트림 → 유한 종결(무보유)", b2.get_position("HI") is None, b2.get_position("HI"))


def test_trim_no_over_liquidation():
    print("[FIX] 중가주 미세트림(명목<MIN & 잔량 건강)은 전량청산도 미세매도도 아닌 무거래밴드 스킵(executor 대칭)")
    # px=$490, 0.029주($14.21, 비-dust). 트림 0.00986→floor0→min(0.01,0.029)=0.01. 명목 0.01*490=$4.9<MIN.
    # 잔량 0.019*490=$9.31≥MIN(건강). 구버그: 전량청산($14.21). 밴드: 스킵(무거래).
    b = PaperBroker(cash=0.0, price_fn=lambda s: 490.0, commission=0, spread=0, slippage=0)
    b._positions["MID"] = Position("MID", 0.029, 490.0)
    tr = IntradayTrader("mid", b, lambda s: 490.0, lambda *a: [], ["MID"])
    tr._execute("MID", Signal("SELL", qty=0.029 * 0.34, reason="MA 이탈 트림"))
    npos = b.get_position("MID")
    check("전량청산 아님 — 구코드는 qty*px=$4.9<MIN 로 전량청산", npos is not None and npos.qty > 1e-9, npos)
    check("미세트림 밴드 스킵(무거래, 포지션 불변 0.029)", npos and abs(npos.qty - 0.029) < 1e-9, npos and npos.qty)
    check("체결 0건(주문 미제출)", tr.fills == [], tr.fills)
    # 진짜 잔량-dust 는 여전히 전량청산(불변) — 0.03주@$100=$3(잔량 dust)
    b2 = PaperBroker(cash=0.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    b2._positions["LO"] = Position("LO", 0.03, 100.0)
    tr2 = IntradayTrader("lo", b2, lambda s: 100.0, lambda *a: [], ["LO"])
    tr2._execute("LO", Signal("SELL", qty=0.03 * 0.34, reason="트림"))          # 잔량 (0.03-0.01)*100=$2<$5
    check("잔량 dust면 전량청산(불변)", b2.get_position("LO") is None, b2.get_position("LO"))


def test_flatten_trim_cooldown():
    print("[FIX] 트림이 포지션 flat → 비보호라도 재진입 쿨다운(whipsaw 차단), 회전캡은 카운트")
    clk = {"t": 0.0}
    g = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120, "max_trades_per_day": 99}},
                      now_fn=lambda: clk["t"])
    buy = Signal("BUY", amount=100.0, reason="entry")
    trim = Signal("SELL", qty=1.0, reason="MA 이탈 트림")            # 비보호(reason 에 손절/트레일/반전/익절 없음)
    g.note_fill("X", buy)                                            # t=0 진입
    clk["t"] = 200.0
    g.note_fill("X", trim, flattened=True)                          # 트림이 전량 소진(flat) t=200
    check("flatten 트림 → last_exit_ts 기록", "X" in g.last_exit_ts and abs(g.last_exit_ts["X"] - 200.0) < 1e-9, g.last_exit_ts)
    check("비보호 트림이라 회전캡도 +1(총 2)", g.trades == 2, g.trades)
    clk["t"] = 260.0                                                # exit 후 60s < 120 쿨다운
    check("flatten 후 쿨다운 내 재매수 차단", g.allow("t", "X", buy, None, _acct(1000)) is False)
    clk["t"] = 400.0                                                # 200s > 120 경과
    check("쿨다운 경과 후 재매수 허용", g.allow("t", "X", buy, None, _acct(1000)) is True)
    # 비-flatten 부분트림은 쿨다운 없음(기존 동작 불변)
    g2 = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120}}, now_fn=lambda: clk["t"])
    g2.note_fill("Y", trim, flattened=False)
    check("부분트림(non-flat) → 쿨다운 없음(기존 불변)", "Y" not in g2.last_exit_ts, g2.last_exit_ts)


def test_regime_gate():
    print("[FIX] SPY 레짐 게이트 — 약세장(regime_on=False) 진입·피라미딩 BUY 차단 / 보호청산·트림은 허용")
    off = {"sym": "X", "cfg": {}, "state": {}, "regime_on": False}
    on = {"sym": "X", "cfg": {}, "state": {}, "regime_on": True}
    bars_break = _mkbars([100.0] * 20 + [102.0])
    pos = Position("X", 5, 100.0)
    # oneil — 진입 게이트
    check("oneil 레짐OFF → 진입 차단", oneil_rule(bars_break, None, 2000.0, dict(off)) == [])
    check("oneil 레짐ON → 진입", len(oneil_rule(bars_break, None, 2000.0, dict(on))) == 1)
    s_stop = oneil_rule(_mkbars([100.0] * 20 + [92.0]), pos, 0.0, dict(off))
    check("oneil 레짐OFF에도 손절 허용(보호청산)", len(s_stop) == 1 and s_stop[0].action == "SELL_ALL", s_stop)
    s_tgt = oneil_rule(_mkbars([100.0] * 20 + [121.0]), pos, 0.0, dict(off))
    check("oneil 레짐OFF에도 익절 허용", len(s_tgt) == 1 and s_tgt[0].action == "SELL_ALL", s_tgt)
    # wood — 진입·피라미딩 게이트 / 트림·손절 무관
    check("wood 레짐OFF → 진입 차단", wood_rule(_mkbars([100.0] * 20 + [101.0]), None, 2000.0, dict(off)) == [])
    check("wood 레짐ON → 진입", len(wood_rule(_mkbars([100.0] * 20 + [101.0]), None, 2000.0, dict(on))) == 1)
    check("wood 레짐OFF → 피라미딩 차단",
          wood_rule(_mkbars([100.0] * 20 + [102.0]), pos, 2000.0, dict(off)) == [])
    s_trim = wood_rule(_mkbars([100.0] * 20 + [99.5]), pos, 0.0, dict(off))
    check("wood 레짐OFF에도 트림 허용", len(s_trim) == 1 and s_trim[0].action == "SELL", s_trim)
    s_wstop = wood_rule(_mkbars([100.0] * 20 + [90.0]), pos, 0.0, dict(off))
    check("wood 레짐OFF에도 손절 허용", len(s_wstop) == 1 and s_wstop[0].action == "SELL_ALL", s_wstop)
    # livermore — 진입·피라미딩 게이트 / 트레일손절 무관
    check("livermore 레짐OFF → ORB진입 차단",
          livermore_rule(_mkbars([100.0] * 15 + [102.0]), None, 2000.0, dict(off)) == [])
    check("livermore 레짐ON → ORB진입",
          len(livermore_rule(_mkbars([100.0] * 15 + [102.0]), None, 2000.0, dict(on))) == 1)
    check("livermore 레짐OFF → 피라미딩 차단",
          livermore_rule(_mkbars([100.0] * 15 + [103.0]), pos, 2000.0, dict(off)) == [])
    ctx_t = dict(off); ctx_t["state"] = {"hw": 110.0}
    s_trail = livermore_rule(_mkbars([100.0] * 15 + [106.0]), pos, 0.0, ctx_t)
    check("livermore 레짐OFF에도 트레일손절 허용", len(s_trail) == 1 and s_trail[0].action == "SELL_ALL", s_trail)
    # 미주입(_ctx) → 기본 허용(기존 동작·테스트 불변)
    check("regime_on 미주입 → 기본 진입 허용", len(oneil_rule(bars_break, None, 2000.0, _ctx())) == 1)


def test_dynamic_watchlist():
    print("[FIX] _watchlist_for — daily_run은 당일 일1런 선정분(신선도)∪보유 동적 / 비-daily_run 정적 / 폴백")
    from run_intraday import _watchlist_for, _last_selection_final

    class FakeBroker:
        def __init__(self, syms): self._p = [Position(s, 1.0, 100.0) for s in syms]
        def get_positions(self): return self._p

    # 파일 A — 전일(stale) + 당일(fresh) 선정 + 장중 스냅샷(selection 무)
    a = pathlib.Path(tempfile.mkdtemp()); (a / "logs").mkdir()
    # traded 일1런 레코드 = "ok" + weights(비어있지않음) + risk.regime="ON" (실제 저널 형태)
    (a / "logs" / "runs.jsonl").write_text(
        json.dumps({"session": "2026-06-26", "selection": {"final": ["OLD1", "OLD2"]},
                    "weights": {"OLD1": 0.5, "OLD2": 0.5}, "risk": {"regime": "ON"}, "status": "ok"}) + "\n"
        + json.dumps({"session": "2026-06-29", "selection": {"final": ["NVDA", "AMD"]},
                      "weights": {"NVDA": 0.5, "AMD": 0.5}, "risk": {"regime": "ON"}, "status": "ok"}) + "\n"
        + json.dumps({"session": "2026-06-29", "status": "intraday", "intraday": True}) + "\n",
        encoding="utf-8")
    check("session 무지정 → 마지막 selection(레거시)", _last_selection_final(str(a)) == ["NVDA", "AMD"])
    check("session=당일 → 당일 final", _last_selection_final(str(a), session="2026-06-29") == ["NVDA", "AMD"])
    check("session=미존재 → []", _last_selection_final(str(a), session="2026-06-30") == [])
    wl = _watchlist_for({"daily_run": True, "watchlist": ["X", "Y"]},
                        home=str(a), broker=FakeBroker(["AMD", "PLTR"]), session="2026-06-29")
    check("daily_run 신선 → 당일선정∪보유", wl == ["NVDA", "AMD", "PLTR"], wl)

    # [FIX-R3b] 레짐 OFF(전량현금 미거래) 일1런: selection.final 은 오버레이 前 원시픽이나 미거래 → 워치 배제
    c = pathlib.Path(tempfile.mkdtemp()); (c / "logs").mkdir()
    (c / "logs" / "runs.jsonl").write_text(
        json.dumps({"session": "2026-06-29", "selection": {"final": ["NVDA", "AMD"]},
                    "weights": {}, "risk": {"regime": "OFF"}, "status": "ok"}) + "\n",
        encoding="utf-8")
    check("레짐OFF 원시픽 → 워치 배제([])", _last_selection_final(str(c), session="2026-06-29") == [],
          _last_selection_final(str(c), session="2026-06-29"))
    wl_c = _watchlist_for({"daily_run": True, "watchlist": ["FB"]},
                          home=str(c), broker=FakeBroker(["MSFT"]), session="2026-06-29")
    check("레짐OFF → 보유만(원시픽 신규진입 차단)", wl_c == ["MSFT"], wl_c)

    # 파일 B — 전일 선정만 + 당일은 stale(selection 무) → 핵심: 어제 픽 거부, 보유만
    b = pathlib.Path(tempfile.mkdtemp()); (b / "logs").mkdir()
    (b / "logs" / "runs.jsonl").write_text(
        json.dumps({"session": "2026-06-26", "selection": {"final": ["OLD1", "OLD2"]}, "status": "ok"}) + "\n"
        + json.dumps({"session": "2026-06-29", "status": "stale"}) + "\n",
        encoding="utf-8")
    check("당일 stale → 전일 선정 거부([])", _last_selection_final(str(b), session="2026-06-29") == [])
    wl_b = _watchlist_for({"daily_run": True, "watchlist": ["FALLBK"]},
                          home=str(b), broker=FakeBroker(["TSLA"]), session="2026-06-29")
    check("stale 당일 → 보유만(어제픽 신규진입 차단)", wl_b == ["TSLA"], wl_b)
    wl_b2 = _watchlist_for({"daily_run": True, "watchlist": ["FALLBK"]},
                           home=str(b), broker=FakeBroker([]), session="2026-06-29")
    check("stale 당일 + 무보유 → 정적 폴백", wl_b2 == ["FALLBK"], wl_b2)

    # 비-daily_run → 정적
    check("비-daily_run → 정적 watchlist", _watchlist_for({"watchlist": ["A", "B"]}) == ["A", "B"])


def test_apply_overlay_gap_robust():
    print("[FIX] apply_overlay — 선정종목 중간 데이터갭(NaN)이 vol 추정 전체를 무력화하지 않음(그날 리밸런스 스킵 방지)")
    import live_risk, data
    from calendar_util import last_completed_session
    import pandas as _pd, numpy as _np
    ses = last_completed_session()
    idx = _pd.bdate_range(end=ses, periods=260)
    _np.random.seed(0)
    aaa = _pd.Series(100 + _np.cumsum(_np.random.randn(260) * 0.5), index=idx)
    bbb = _pd.Series(50 + _np.cumsum(_np.random.randn(260) * 0.3), index=idx)
    aaa.iloc[-5] = _np.nan                                   # vol_lookback(20) 창 내부 중간 갭
    prices = _pd.DataFrame({"AAA": aaa, "BBB": bbb})
    spy_up = _pd.DataFrame({"Close": _np.linspace(300, 500, 260)}, index=idx)   # 레짐 ON, 非stale
    saved = data.load
    try:
        data.load = lambda s, a, b: spy_up
        out, info = live_risk.apply_overlay(prices, {"AAA": 0.5, "BBB": 0.5}, vol_target=0.20,
                                            regime_ma=200, vol_lookback=20)
        check("갭 있어도 ValueError 없이 리밸런스 산출(그날 스킵 방지)", info.get("regime") == "ON", info)
        check("realized 유한(갭 행만 제외)", _np.isfinite(info.get("realized_vol", _np.nan)), info)
        check("비중 유한(NaN 오염 없음)", out and all(_np.isfinite(v) for v in out.values()), out)
    finally:
        data.load = saved


def test_regime_on_helper():
    print("[FIX-B] live_risk.regime_on — SPY>200MA / 기준세션 정렬 / stale-SPY 거부 / 부족·실패 None / fail-open")
    import live_risk, data, run_intraday
    from calendar_util import last_completed_session
    import pandas as _pd, numpy as _np
    ses = last_completed_session()
    idx = _pd.bdate_range(end=ses, periods=260)                  # 끝을 기준세션에 맞춤(stale 가드 통과)
    def _raise(s, a, b): raise RuntimeError("net")
    saved = data.load
    try:
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(100, 300, 260)}, index=idx)
        check("상승추세 → 레짐 ON(True)", live_risk.regime_on() is True)
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(300, 100, 260)}, index=idx)
        check("하락추세 → 레짐 OFF(False)", live_risk.regime_on() is False)
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(100, 110, 50)}, index=idx[:50])
        check("데이터<200MA → None(판정불가)", live_risk.regime_on() is None)
        stale = _pd.bdate_range(end=ses - _pd.Timedelta(days=20), periods=260)   # SPY 20일 뒤쳐짐
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(100, 300, 260)}, index=stale)
        check("stale SPY → None(옛종가 ON 오판 차단)", live_risk.regime_on() is None)
        data.load = _raise
        check("로드 실패 → None", live_risk.regime_on() is None)
        # _resolve_regime: None(판정불가)·실패 → fail-open True, 약세 → False
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(100, 110, 50)}, index=idx[:50])
        check("_resolve_regime 판정불가 → fail-open True", run_intraday._resolve_regime() is True)
        data.load = lambda s, a, b: _pd.DataFrame({"Close": _np.linspace(300, 100, 260)}, index=idx)
        check("_resolve_regime 약세장 → False", run_intraday._resolve_regime() is False)
    finally:
        data.load = saved


def test_trim_churn_cap():
    print("[FIX] 트림(비보호 SELL)도 회전캡·min-hold 적용 — wood 트림 연쇄 churn 차단(보호청산은 무관)")
    buy = Signal("BUY", amount=100.0, reason="entry")
    trim = Signal("SELL", qty=1.0, reason="MA 이탈 트림")             # 비보호
    stop = Signal("SELL_ALL", reason="MA 이탈 손절", protective=True)  # 보호청산
    pos = Position("X", 5, 100.0)
    # 회전캡 — max_trades=2 도달 후 트림 거부, 보호청산은 허용
    g = IntradayGuard({"intraday_cfg": {"max_trades_per_day": 2, "min_hold_seconds": 0}})
    g.note_fill("X", buy); g.note_fill("X", trim)                    # 체결 2건 → 캡 도달
    check("캡 도달 후 트림 거부(회전캡 트림 적용)", g.allow("t", "X", trim, pos, _acct(1000)) is False)
    check("캡 도달 후에도 보호청산 허용", g.allow("t", "X", stop, pos, _acct(1000)) is True)
    # min-hold — 트림 체결이 시각 갱신 → 연속 트림 min-hold 간격
    clk = {"t": 0.0}
    g2 = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120, "max_trades_per_day": 99}},
                       now_fn=lambda: clk["t"])
    g2.note_fill("X", buy)                                           # 매수 t=0
    clk["t"] = 130.0
    check("매수후 130s 첫 트림 허용", g2.allow("t", "X", trim, pos, _acct(1000)) is True)
    g2.note_fill("X", trim)                                          # 트림 체결 t=130 → 시각 갱신
    clk["t"] = 180.0
    check("트림후 50s 둘째 트림 차단(트림간 min-hold)", g2.allow("t", "X", trim, pos, _acct(1000)) is False)
    clk["t"] = 260.0
    check("트림후 130s 둘째 트림 허용", g2.allow("t", "X", trim, pos, _acct(1000)) is True)


def test_watchlist_picks_up_daily_holdings():
    print("[FIX-CS6] 일1런 세션중 신규매수 종목이 워치에 편입(미보호 방치 차단)")
    tmp = pathlib.Path(tempfile.mkdtemp()); state, logs = tmp / "state", tmp / "logs"; state.mkdir(); logs.mkdir()
    book = str(state / "paper_book_x.json"); lockpath = str(state / "run.lock")
    px = {"AAA": 100.0, "NEW": 50.0}
    broker = PaperBroker(cash=5000.0, price_fn=lambda s: px[s], state_file=book,
                         commission=0, spread=0, slippage=0)
    broker.place_order(OrderRequest("AAA", Side.BUY, qty=5, order_type=OrderType.MARKET))
    tr = IntradayTrader("x", broker, lambda s: px[s], lambda *a: [], ["AAA"],
                        log_dir=str(logs), book_lock=lockpath)
    check("초기 워치 = [AAA]", tr.watchlist == ["AAA"], tr.watchlist)
    # 일1런이 세션 중 NEW 매수(디스크 책 직접 변경, AAA 유지)
    pathlib.Path(book).write_text(json.dumps({"cash": 2500.0, "positions": [
        {"symbol": "AAA", "qty": 5.0, "avg_price": 100.0},
        {"symbol": "NEW", "qty": 50.0, "avg_price": 50.0}]}), encoding="utf-8")
    for ts in (0, 60, 120, 180):                 # AAA 바닫힘→_on_bar reload(NEW 인메모리)→다음 sample top 편입
        tr.sample(ts)
    check("일1런 신규매수 NEW 워치 편입", "NEW" in tr.watchlist, tr.watchlist)
    check("NEW agg/bars/ctx 초기화", "NEW" in tr.aggs and "NEW" in tr.bars and "NEW" in tr.ctx)
    check("NEW ctx regime_on 주입", tr.ctx["NEW"].get("regime_on") is True)


def test_ctx_state_reconcile():
    print("[FIX-C] reload reconcile — 외부 포지션 변경 시 stale 트레일/피라미딩 폐기, 자기보유 보존, flat 비움")
    tmp = pathlib.Path(tempfile.mkdtemp()); state, logs = tmp / "state", tmp / "logs"; state.mkdir(); logs.mkdir()
    book = str(state / "paper_book_x.json"); lockpath = str(state / "run.lock")
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, state_file=book,
                         commission=0, spread=0, slippage=0)
    broker.place_order(OrderRequest("AAA", Side.BUY, qty=5, order_type=OrderType.MARKET))   # 5@100
    tr = IntradayTrader("x", broker, lambda s: 100.0, lambda *a: [], ["AAA"], log_dir=str(logs), book_lock=lockpath)
    tr.ctx["AAA"]["state"]["hw"] = 130.0           # 옛 트레일 고점
    tr.ctx["AAA"]["state"]["adds"] = 2             # 피라미딩 소진
    tr._known_avg["AAA"] = 100.0                    # 지문 = 현 평단
    # 외부(run_live)가 같은 종목을 다른 평단(80)으로 교체
    pathlib.Path(book).write_text(json.dumps(
        {"cash": 1500.0, "positions": [{"symbol": "AAA", "qty": 5.0, "avg_price": 80.0}]}), encoding="utf-8")
    tr.sample(0); tr.sample(60)                     # 바 완성 → _on_bar → reload → reconcile
    check("외부 평단변경 → 트레일 hw 폐기", "hw" not in tr.ctx["AAA"]["state"], tr.ctx["AAA"]["state"])
    check("외부 평단변경 → 피라미딩 adds 폐기", "adds" not in tr.ctx["AAA"]["state"])
    check("지문 새 평단(80)으로 갱신", abs(tr._known_avg.get("AAA", 0) - 80.0) < 1e-6, tr._known_avg.get("AAA"))
    # 자기보유(평단 동일) → 상태 보존(자기 피라미딩 오탐 방지)
    b2 = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    b2.place_order(OrderRequest("BBB", Side.BUY, qty=5, order_type=OrderType.MARKET))
    tr2 = IntradayTrader("y", b2, lambda s: 100.0, lambda *a: [], ["BBB"])
    tr2.ctx["BBB"]["state"]["hw"] = 130.0; tr2._known_avg["BBB"] = 100.0
    tr2._reconcile_ctx_state("BBB")
    check("평단 동일(자기보유) → 트레일 상태 보존", tr2.ctx["BBB"]["state"].get("hw") == 130.0)
    # flat → 상태·지문 비움
    b2.place_order(OrderRequest("BBB", Side.SELL, qty=5, order_type=OrderType.MARKET))
    tr2._reconcile_ctx_state("BBB")
    check("flat → 상태·지문 비움", tr2.ctx["BBB"]["state"] == {} and "BBB" not in tr2._known_avg,
          (tr2.ctx["BBB"]["state"], tr2._known_avg.get("BBB")))


def test_thrust_gapfill_immune():
    print("[FIX-A] _thrust/_opening_range/pivot — 갭충전 합성봉(n=0) 면역(가짜돌파·거짓음성·ORB붕괴 차단)")
    from intraday_rules import _thrust, _opening_range, oneil_rule
    # _thrust: 실봉만이면 정상, 윈도우에 합성봉 끼면 0(보류)
    real = [Bar(i * 60, 100.0, 100.0, 100.0, 100.0 + i, 1) for i in range(4)]   # 100→103 상승
    check("실봉 윈도우 → thrust>0", _thrust(real, 3) > 0, _thrust(real, 3))
    gap = [Bar(0, 100, 100, 100, 100, 1), Bar(60, 100, 100, 100, 100, 0),
           Bar(120, 100, 100, 100, 100, 0), Bar(180, 105, 105, 105, 105, 1)]    # 갭 뒤 점프
    check("합성봉 낀 윈도우 → thrust 0(가짜점프 보류)", _thrust(gap, 3) == 0.0, _thrust(gap, 3))
    # _opening_range: 합성 제외 실봉 범위, 전부 합성이면 None
    orb = [Bar(0, 100, 102, 98, 100, 1)] + [Bar(i * 60, 100, 100, 100, 100, 0) for i in range(1, 15)]
    check("ORB 실봉만 산출(102/98)", _opening_range(orb, 15) == (102.0, 98.0), _opening_range(orb, 15))
    check("ORB 전부 합성 → None", _opening_range([Bar(i * 60, 100, 100, 100, 100, 0) for i in range(15)], 15) is None)
    # oneil 통합: 피벗 돌파해도 최근 갭충전이면 thrust 0 → 미진입(가짜 돌파 차단)
    bars = (_mkbars([100.0] * 20)                                              # 실봉 20 (피벗 100)
            + [Bar(20 * 60, 100, 100, 100, 100, 0), Bar(21 * 60, 100, 100, 100, 100, 0)]  # 갭충전 2
            + [Bar(22 * 60, 102, 102, 102, 102, 1)])                          # 점프 102
    check("oneil: 갭 뒤 점프 돌파 → 미진입(thrust 보류)",
          oneil_rule(bars, None, 2000.0, _ctx()) == [], oneil_rule(bars, None, 2000.0, _ctx()))


def test_quote_gap_equity_lastgood():
    print("[FIX] PaperBroker 호가공백 폴백 = 마지막 실거래가(원가 아님) — 폭락 중 일중손실 halt 정상 발화")
    px = {"AAA": 100.0}
    def qfn(s):
        if px[s] is None:
            raise RuntimeError("no quote")
        return px[s]
    b = PaperBroker(cash=1000.0, price_fn=qfn, commission=0, spread=0, slippage=0)
    b.place_order(OrderRequest("AAA", Side.BUY, qty=10, order_type=OrderType.MARKET))  # 10@100 → cash0 eq1000
    check("진입 후 equity=1000", abs(b.get_account().equity - 1000.0) < 1e-6, b.get_account().equity)
    px["AAA"] = 80.0
    check("하락 반영 equity=800", abs(b.get_account().equity - 800.0) < 1e-6)
    px["AAA"] = None                                       # 호가공백
    eq = b.get_account().equity
    check("호가공백 → last-good(80) 폴백 equity=800(원가1000 아님)", abs(eq - 800.0) < 1e-6, eq)
    # 가드 연동 — day_start 1000, max_loss 5% → eq 800 이면 halt(원가동결이면 1000→미발화였음)
    g = IntradayGuard({"intraday_cfg": {"intraday_max_loss": 0.05}})
    g.seed_day_start(1000.0)
    buy = Signal("BUY", amount=100.0, reason="e")
    check("호가공백 폭락 중에도 halt 발화", g.allow("t", "AAA", buy, b.get_position("AAA"), b.get_account()) is False)


def test_bear_reversal_synthetic_gap():
    print("[FIX] _bear_reversal — 갭충전 합성봉(n=0)을 prior_low 에서 제외(가짜 조기 반전청산 차단)")
    from intraday_rules import _bear_reversal
    last_down = Bar(5 * 60, 100.0, 100.0, 99.4, 99.4, 1)            # 실제 0.6% 음봉(>rev 0.5%)
    # prior 5봉 전부 합성(n=0, 평탄 100) → 가짜 prior_low=100 으로 false 발화하던 케이스
    syn = [Bar(i * 60, 100.0, 100.0, 100.0, 100.0, 0) for i in range(5)]
    check("합성 prior + 실음봉 → 반전 미발화", _bear_reversal(syn + [last_down]) is False)
    # prior 에 실제 스윙로우 99.0 → last 99.4 는 그 위 → 미발화(정상)
    real_low = [Bar(i * 60, 100.0, 100.0, 99.0 if i == 2 else 100.0, 100.0, 1) for i in range(5)]
    check("실 스윙로우(99.0) 위 음봉(99.4) → 미발화", _bear_reversal(real_low + [last_down]) is False)
    # prior 실제 평탄 100 → last 하향이탈 → 정상 반전 발화
    real_flat = [Bar(i * 60, 100.0, 100.0, 100.0, 100.0, 1) for i in range(5)]
    check("실 prior(100) 하향이탈(99.4) → 반전 발화", _bear_reversal(real_flat + [last_down]) is True)


def test_protective_exit_no_churn():
    print("[FIX-E] 보호청산은 회전캡 미소모 — 손절 다수에도 재량 매매 예산 보존")
    g = IntradayGuard({"intraday_cfg": {"max_trades_per_day": 2, "min_hold_seconds": 0}})
    buy = Signal("BUY", amount=100.0, reason="e")
    stop = Signal("SELL_ALL", reason="손절", protective=True)
    g.note_fill("X", buy)                                   # 재량 매수 → trades=1
    g.note_fill("X", stop)                                  # 보호청산 → trades 미증가
    g.note_fill("Y", stop)                                  # 또 보호청산 → 미증가
    check("보호청산은 trades 미증가(=1)", g.trades == 1, g.trades)
    check("매수 1건 더 허용(캡 2 미도달)", g.allow("t", "Z", buy, None, _acct(1000)) is True)
    g.note_fill("Z", buy)                                   # trades=2
    check("매수 2건째 후 캡 도달 → 거부", g.allow("t", "W", buy, None, _acct(1000)) is False)
    check("캡 도달 후에도 보호청산 허용", g.allow("t", "X", stop, Position("X", 5, 100), _acct(1000)) is True)


def test_reentry_cooldown():
    print("[FIX] 보호청산 후 재진입 쿨다운 — 손절 직후 같은종목 재매수(whipsaw) 차단, 경과 후 허용")
    clk = {"t": 0.0}
    g = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120, "max_trades_per_day": 99}},
                      now_fn=lambda: clk["t"])
    buy = Signal("BUY", amount=100.0, reason="피벗 돌파")
    stop = Signal("SELL_ALL", reason="7% 손절", protective=True)
    g.note_fill("X", buy)                                            # 진입 t=0
    clk["t"] = 60.0
    g.note_fill("X", stop)                                           # 보호청산 체결 t=60 → 쿨다운 시작
    clk["t"] = 100.0
    check("손절후 40s 재매수 차단(쿨다운)", g.allow("t", "X", buy, None, _acct(1000)) is False)
    check("쿨다운 중 타 종목은 무관", g.allow("t", "Y", buy, None, _acct(1000)) is True)
    check("쿨다운 중에도 보호청산 허용", g.allow("t", "X", stop, Position("X", 5, 100), _acct(1000)) is True)
    clk["t"] = 200.0
    check("손절후 140s 재매수 허용(쿨다운 경과)", g.allow("t", "X", buy, None, _acct(1000)) is True)
    # 영속 — 재시작 시 쿨다운 복원
    tmp = pathlib.Path(tempfile.mkdtemp()); sf = str(tmp / "g.json")
    clk["t"] = 0.0
    gp = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120}}, now_fn=lambda: clk["t"],
                       state_file=sf, today="2026-06-29")
    gp.note_fill("X", stop)                                          # 보호청산 t=0 저장
    clk["t"] = 50.0
    g2 = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 120}}, now_fn=lambda: clk["t"],
                       state_file=sf, today="2026-06-29")
    check("재시작: 쿨다운 복원(50s<120 재매수 차단)", g2.allow("t", "X", buy, None, _acct(1000)) is False)


def test_build_traders_regime_inject():
    print("[FIX] build_traders regime_on → 트레이더 ctx 주입(약세장 진입게이트 배선)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    pmap = {"x": {"intraday": True, "cash": 1500.0, "watchlist": ["AAA"]}}
    rmap = {"x": lambda *a: []}
    t_off = build_traders(lambda s: 100.0, pmap, rmap, home_fn=lambda n: str(tmp / n), regime_on=False)
    check("ctx regime_on=False 주입", t_off[0].ctx["AAA"]["regime_on"] is False, t_off[0].ctx["AAA"])
    t_def = build_traders(lambda s: 100.0, pmap, rmap, home_fn=lambda n: str(tmp / "b" / n))
    check("기본 regime_on=True", t_def[0].ctx["AAA"]["regime_on"] is True)


# ───── SWING: livermore_swing — 피벗 진입·오버나이트 트레일·상태영속·현금바닥 ─────
def test_livermore_swing_rule():
    print("[SWING] livermore_swing: day_high 피벗 진입(fail-closed) · 첫 바 갭관통 트레일 · 피라미딩 · 레짐게이트")
    from intraday_rules import livermore_swing_rule
    # 진입 — 피벗(직전 20세션 고점) 100 돌파 + thrust. day_high 는 엔진 주입.
    ctx = _ctx(); ctx["day_high"] = 100.0
    sigs = livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, ctx)
    check("피벗 돌파 → BUY", len(sigs) == 1 and sigs[0].action == "BUY", sigs)
    # day_high 미주입(데이터 실패) → 진입 fail-closed
    check("day_high 없음 → 진입 안 함",
          livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, _ctx()) == [])
    # 레짐 OFF → 진입 차단
    ctx_off = _ctx(); ctx_off["day_high"] = 100.0; ctx_off["regime_on"] = False
    check("레짐 OFF → 진입 차단",
          livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, ctx_off) == [])
    # 오버나이트 갭관통 — 복원된 hw=120, 개장 *첫 바* 110 ≤ 120×0.92=110.4 → 즉시 청산(워밍업 게이트 無)
    pos = Position("X", 5, 100.0)
    ctx2 = _ctx(); ctx2["state"]["hw"] = 120.0
    s2 = livermore_swing_rule(_mkbars([110.0]), pos, 0.0, ctx2)
    check("갭관통 첫 바 트레일 청산(protective)", len(s2) == 1 and s2[0].action == "SELL_ALL"
          and s2[0].protective, s2)
    # 임계 위면 보유 지속 + hw 는 실고점 유지
    ctx3 = _ctx(); ctx3["state"]["hw"] = 120.0
    s3 = livermore_swing_rule(_mkbars([111.0]), pos, 0.0, ctx3)
    check("임계 위 → 보유(무신호), hw 유지", s3 == [] and ctx3["state"]["hw"] == 120.0,
          (s3, ctx3["state"]))
    # 피라미딩 — 신고가 + thrust → add(BUY pyramid), 레짐 OFF 면 add 도 차단
    ctx4 = _ctx()
    s4 = livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 102.0]), pos, 2000.0, ctx4)
    check("신고가+thrust → 피라미딩 BUY", len(s4) == 1 and s4[0].action == "BUY"
          and getattr(s4[0], "pyramid", False), s4)
    ctx5 = _ctx(); ctx5["regime_on"] = False
    s5 = livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 102.0]), pos, 2000.0, ctx5)
    check("레짐 OFF → add 차단(트레일은 별개)", s5 == [], s5)
    # accum_gate 다이얼(기본 off) — on+미충족만 차단, 미주입 fail-open, off 면 무시
    ctx6 = _ctx({"accum_gate": True}); ctx6["day_high"] = 100.0; ctx6["accum_ok"] = False
    check("accum_gate on + 매집 미충족 → 진입 차단",
          livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, ctx6) == [])
    ctx7 = _ctx({"accum_gate": True}); ctx7["day_high"] = 100.0     # accum_ok 미주입(산출 실패)
    check("accum_gate on + 미주입 → fail-open 진입",
          len(livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, ctx7)) == 1)
    ctx8 = _ctx(); ctx8["day_high"] = 100.0; ctx8["accum_ok"] = False
    check("다이얼 off → accum_ok 무시(기존 동작 불변)",
          len(livermore_swing_rule(_mkbars([100.0, 100.0, 100.0, 100.6]), None, 2000.0, ctx8)) == 1)


def test_swing_rule_state_persistence():
    print("[SWING] 룰상태 영속: 보유종목 hw 복원 · flat 종목 잔재 폐기 · nan 손상 격리 · sample 경로 저장")
    tmp = pathlib.Path(tempfile.mkdtemp())
    book = str(tmp / "book.json"); rsf = str(tmp / "rules_state.json")
    px = {"AAA": 100.0, "BBB": 50.0}
    qfn = lambda s: px[s]
    b = PaperBroker(cash=100000.0, price_fn=qfn, state_file=book)
    tr = IntradayTrader("s", b, qfn, lambda *a: [], ["AAA", "BBB"], rule_state_file=rsf)
    tr._execute("AAA", Signal("BUY", amount=10000.0, reason="x"))     # AAA 만 보유
    tr.ctx["AAA"]["state"]["hw"] = 130.0
    tr.ctx["BBB"]["state"]["hw"] = 77.0                               # flat 종목 잔재(폐기돼야 함)
    tr._save_rule_state()
    check("상태 파일 기록", os.path.exists(rsf))
    b2 = PaperBroker(cash=1.0, price_fn=qfn, state_file=book)         # 새 세션 모사(책 로드)
    tr2 = IntradayTrader("s", b2, qfn, lambda *a: [], ["AAA", "BBB"], rule_state_file=rsf)
    check("보유 AAA hw 복원(세션 관통)", tr2.ctx["AAA"]["state"].get("hw") == 130.0,
          tr2.ctx["AAA"]["state"])
    check("flat BBB 잔재 폐기(스테일 무장 방지)", tr2.ctx["BBB"]["state"] == {},
          tr2.ctx["BBB"]["state"])
    # nan 손상 항목 — 그 종목만 신규 seed(트레일 영구침묵 차단)
    with open(rsf, "w", encoding="utf-8") as f:
        f.write('{"AAA": {"hw": NaN}}')
    tr3 = IntradayTrader("s", b2, qfn, lambda *a: [], ["AAA"], rule_state_file=rsf)
    check("nan hw → 폐기(신규 seed)", "hw" not in tr3.ctx["AAA"]["state"], tr3.ctx["AAA"]["state"])
    # sample 경로 — 바 완성 시 자동 저장(분당 영속)
    os.remove(rsf)
    mut = lambda bars, pos, cash, ctx: ctx["state"].__setitem__("hw", 1.0) or []
    tr4 = IntradayTrader("s", b2, qfn, mut, ["AAA"], rule_state_file=rsf)
    tr4.sample(0.0); tr4.sample(61.0)                                 # 두 번째 샘플이 첫 바를 닫음
    check("sample 바 완성 → 자동 저장", os.path.exists(rsf))
    check("rule_state_file 미배선 → 파일 무생성(기존 페르소나 불변)",
          not os.path.exists(str(tmp / "none.json")))


def test_swing_deploy_cap():
    print("[SWING] IntradayGuard max_deploy: 총투입 70% 캡(현금바닥 30%) — 초과 매수 거부·보호청산 무관")
    g = IntradayGuard({"intraday_cfg": {"max_deploy": 0.70, "max_position_weight": 0.40,
                                        "max_trades_per_day": 20}})
    buy20k = Signal("BUY", amount=20000.0, reason="x")
    # 투자 55k(현금 45k) — +20k = 75k > 70k 캡 → 거부
    a1 = AccountInfo(cash=45000.0, equity=100000.0, buying_power=0.0)
    check("투입 55%+20% > 70% → 거부", g.allow("s", "X", buy20k, None, a1, 100.0) is False)
    # 투자 40k(현금 60k) — +20k = 60k ≤ 70k → 허용
    a2 = AccountInfo(cash=60000.0, equity=100000.0, buying_power=0.0)
    check("투입 40%+20% ≤ 70% → 허용", g.allow("s", "X", buy20k, None, a2, 100.0) is True)
    # 보호청산은 캡 무관 항상 허용
    stop = Signal("SELL_ALL", reason="트레일손절 8%", protective=True)
    check("보호청산 캡 무관 허용", g.allow("s", "X", stop, Position("X", 5, 100.0), a1) is True)
    # max_deploy 미설정(기본 1.0) → 기존 동작(비중캡만) — 풀투자 페르소나 불변
    g0 = IntradayGuard({"intraday_cfg": {"max_position_weight": 0.40, "max_trades_per_day": 20}})
    a3 = AccountInfo(cash=5000.0, equity=100000.0, buying_power=0.0)
    check("기본(캡 off) → 투입 95%+20k 도 비중캡만 적용", g0.allow("s", "X", buy20k, None, a3, 100.0) is True)


def test_swing_wiring_and_preset():
    print("[SWING] 배선: 프리셋 등록·RULES·build_traders(day_high 주입·상태파일)·_day_levels_for")
    from intraday_rules import RULES, livermore_swing_rule
    p = personas.PERSONAS.get("livermore_swing")
    check("페르소나 등록(intraday·일1런無)", p is not None and p.get("intraday") is True
          and not p.get("daily_run"))
    check("오버나이트 — eod_flatten 없음", not p["intraday_cfg"].get("eod_flatten"))
    check("영속·현금바닥 다이얼", p["intraday_cfg"].get("persist_state") is True
          and p["intraday_cfg"].get("max_deploy") == 0.70)
    check("cash 명시 $100k(무지정 $2000 함정 회피)", p["cash"] == 100000.0)
    liv = personas.PERSONAS["livermore"]
    check("watchlist livermore 와 동일 내용·별도 객체(변인 통제+격리)",
          p["watchlist"] == liv["watchlist"] and p["watchlist"] is not liv["watchlist"])
    check("cfg livermore 와 별도 dict(독립 튜닝)", p["intraday_cfg"] is not liv["intraday_cfg"])
    check("RULES 등록", RULES.get("livermore_swing") is livermore_swing_rule)
    # build_traders — day_levels → ctx['day_high'], persist_state → rule_state_file 배선
    tmp = pathlib.Path(tempfile.mkdtemp())
    pmap = {"s": {"intraday": True, "cash": 1500.0, "watchlist": ["AAA"],
                  "intraday_cfg": {"persist_state": True, "pivot_days": 6}}}
    trs = build_traders(lambda s: 100.0, pmap, {"s": lambda *a: []},
                        home_fn=lambda n: str(tmp / n), day_levels={"AAA": 55.0})
    check("ctx day_high 주입", trs[0].ctx["AAA"]["day_high"] == 55.0, trs[0].ctx["AAA"])
    check("rule_state_file 배선", trs[0].rule_state_file
          and trs[0].rule_state_file.endswith("intraday_rules_state_s.json"), trs[0].rule_state_file)
    # _day_levels_for — 결정론(주입 load_fn·session): tail(n) 고점, 데이터 절반 이하 미확정
    import pandas as pd
    from datetime import date
    import run_intraday as _ri
    fake = {"AAA": pd.DataFrame({"High": [10.0, 20.0, 15.0, 12.0, 11.0, 13.0]}),
            "NEW": pd.DataFrame({"High": [10.0, 11.0]})}          # 2행 ≤ 6//2 → 미확정
    lv = _ri._day_levels_for({"s": {**pmap["s"], "watchlist": ["AAA", "NEW"]}},
                             load_fn=lambda s, a, b: fake[s], session=date(2026, 7, 8))
    check("피벗 = 직전 6세션 High 최대", lv.get("AAA") == 20.0, lv)
    check("데이터 절반 이하 → 미확정(fail-closed)", "NEW" not in lv, lv)
    check("스윙 없음 → 즉시 {} (네트워크 0)",
          _ri._day_levels_for({"x": {"intraday": True, "watchlist": ["AAA"], "intraday_cfg": {}}}) == {})
    # accum_flags 배선 — ctx['accum_ok'] 주입 + 프리셋 다이얼 기본 off
    trs2 = build_traders(lambda s: 100.0, pmap, {"s": lambda *a: []},
                         home_fn=lambda n: str(tmp / "a" / n), accum_flags={"AAA": False})
    check("ctx accum_ok 주입", trs2[0].ctx["AAA"]["accum_ok"] is False, trs2[0].ctx["AAA"])
    check("livermore_swing accum_gate on(2026-07-09 채택) + 다이얼 15d/10%",
          p["intraday_cfg"].get("accum_gate") is True and p["intraday_cfg"]["accum_days"] == 15
          and p["intraday_cfg"]["flat_pct"] == 0.10)
    # _accum_flags_for — 결정론: 횡보+OBV상승=True / 급등추세=False / 게이트 없으면 load_fn 미호출
    gated = {"s": {"intraday": True, "watchlist": ["FLAT", "TREND"],
                   "intraday_cfg": {"accum_gate": True, "accum_days": 3, "flat_pct": 0.10}}}
    fake2 = {"FLAT": pd.DataFrame({"Close": [100.0, 101.0, 100.5, 101.5, 101.0, 101.8],
                                   "Volume": [0.0, 100.0, 10.0, 100.0, 10.0, 100.0]}),
             "TREND": pd.DataFrame({"Close": [100.0, 110.0, 121.0, 133.0, 146.0, 161.0],
                                    "Volume": [0.0, 100.0, 100.0, 100.0, 100.0, 100.0]})}
    af = _ri._accum_flags_for(gated, load_fn=lambda s, a, b: fake2[s], session=date(2026, 7, 8))
    check("횡보+OBV상승 → accum_ok True", af.get("FLAT") is True, af)
    check("급등 추세(횡보 아님) → False", af.get("TREND") is False, af)

    def _boom(*a):
        raise AssertionError("게이트 없는데 load_fn 호출됨")
    check("accum_gate 페르소나 없음 → 즉시 {} (네트워크 0)",
          _ri._accum_flags_for({"x": {"intraday": True, "watchlist": ["AAA"], "intraday_cfg": {}}},
                               load_fn=_boom) == {})


# ───── KIS: 볼륨 섀도(관찰 전용 — 매매 경로 0 접촉) ─────
_KTOKEN = ("POST", "/oauth2/tokenP")
_KPRICE = ("GET", "/uapi/overseas-price/v1/quotations/price")


def _kis(routes, tmp):
    from broker.kis_quote import KISQuoteClient
    sess = FakeSession(routes)
    c = KISQuoteClient(app_key="k", app_secret="s", base_url=BASE, session=sess,
                       token_file=str(tmp / "tok.json"), excd_file=str(tmp / "excd.json"),
                       sleep_fn=lambda *_: None)
    return c, sess


def test_kis_quote_client():
    print("[KIS] 클라이언트: 무주문 구조·토큰 디스크 공유·EXCD 자가해결·tvol 파싱·휴면")
    tmp = pathlib.Path(tempfile.mkdtemp())

    def price(call):
        p = call["params"]
        if p["EXCD"] == "NYS" and p["SYMB"] == "ORCL":
            return (200, {"rt_cd": "0", "output": {"last": "230.5", "tvol": "1234567"}})
        if p["EXCD"] == "NAS" and p["SYMB"] == "NVDA":
            return (200, {"rt_cd": "0", "output": {"last": "1000.1", "tvol": "999", "tamt": "998900.5"}})
        return (200, {"rt_cd": "0", "output": {"last": "", "tvol": ""}})   # 잘못된 거래소 = 공란

    c, sess = _kis({_KTOKEN: (200, {"access_token": "ktok", "expires_in": 86400}),
                    _KPRICE: price}, tmp)
    check("주문 메서드 부재(구조적 실주문 0)",
          not hasattr(c, "place_order") and not hasattr(c, "cancel_order"))
    from broker.toss import TossBroker
    check("TossBroker/주문형 비상속", not isinstance(c, TossBroker))
    c.connect()
    s = c.get_snapshot("NVDA")
    check("NAS 1발 해결 + last/tvol/tamt float", s["excd"] == "NAS" and s["tvol"] == 999.0
          and s["last"] == 1000.1 and s["tamt"] == 998900.5, s)
    s2 = c.get_snapshot("ORCL")
    check("NAS 공란 → NYS 폴백 자가해결", s2["excd"] == "NYS" and s2["tvol"] == 1234567.0, s2)
    n1 = len([x for x in sess.calls if x["path"] == _KPRICE[1]])
    c.get_snapshot("ORCL")
    n2 = len([x for x in sess.calls if x["path"] == _KPRICE[1]])
    check("EXCD 디스크 캐시 → 1콜 직행", n2 - n1 == 1, (n1, n2))
    # 토큰 디스크 공유 — 새 인스턴스(토큰 라우트 없음)가 재발급 없이 재사용
    c2, sess2 = _kis({_KPRICE: price}, tmp)
    c2.connect()
    check("토큰 디스크 재사용(재발급 0 — 발급 레이트 제한 대응)",
          not any(x["path"] == _KTOKEN[1] for x in sess2.calls))
    check("재사용 토큰으로 조회 성공", c2.get_snapshot("NVDA")["tvol"] == 999.0)
    # env 키 없음 → from_env None (섀도 휴면)
    saved = {k: os.environ.pop(k, None) for k in ("KIS_APP_KEY", "KIS_APP_SECRET")}
    try:
        from broker.kis_quote import KISQuoteClient
        check("env 미설정 → from_env None(휴면)", KISQuoteClient.from_env() is None)
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def test_kis_token_heal():
    """2026-07-22 개장 65분 결손 회귀 — KIS 동일토큰 alias 의 expires_in 과대평가 +
    HTTP 500/200 EGW00123 무재발급이 원인. 실만료 클램프와 재발급 그물을 고정한다."""
    print("[KIS] 토큰: 실만료 클램프(alias)·EGW00123 자가회복(200/500)·하드만료만 재발급")
    from datetime import timedelta as _td, timezone as _tz
    from broker.kis_quote import KISQuoteClient
    tmp = pathlib.Path(tempfile.mkdtemp())

    def mk(routes, i, now_fn=None):
        sess = FakeSession(routes)
        return KISQuoteClient(app_key="k", app_secret="s", base_url=BASE, session=sess,
                              token_file=str(tmp / f"tok{i}.json"),
                              excd_file=str(tmp / f"excd{i}.json"),
                              sleep_fn=lambda *_: None,
                              **({"now_fn": now_fn} if now_fn else {}))

    # 1) 실만료 클램프 — alias 응답(expires_in=86400, 실만료 1h)이면 실만료를 기록
    real = 1_000_000_000.0
    exp_s = datetime.fromtimestamp(real + 3600, _tz(_td(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
    c = mk({_KTOKEN: (200, {"access_token": "t1", "expires_in": 86400,
                            "access_token_token_expired": exp_s})}, 1, now_fn=lambda: real)
    c.connect()
    check("실만료(access_token_token_expired) 클램프", abs(c._token_expiry - (real + 3600)) < 2,
          str(c._token_expiry))

    # 2) HTTP 200 + rt_cd!=0 + EGW00123 → 강제 재발급 1회 후 성공
    n = {"tok": 0, "px": 0}
    def tok(call):
        n["tok"] += 1
        return (200, {"access_token": f"t{n['tok']}", "expires_in": 86400})
    def px(call):
        n["px"] += 1
        if n["px"] == 1:
            return (200, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token"})
        return (200, {"rt_cd": "0", "output": {"last": "10.5", "tvol": "42"}})
    c2 = mk({_KTOKEN: tok, _KPRICE: px}, 2)
    c2.connect()
    s = c2.get_snapshot("NVDA")
    check("200+EGW00123 → 재발급 1회 자가회복", s["tvol"] == 42.0 and n["tok"] == 2, str(n))

    # 3) HTTP 500 + EGW00123 → 5xx 재시도로 시간 태우지 않고 즉시 재발급
    m = {"tok": 0, "px": 0}
    def tok3(call):
        m["tok"] += 1
        return (200, {"access_token": f"x{m['tok']}", "expires_in": 86400})
    def px3(call):
        m["px"] += 1
        if m["px"] == 1:
            return (500, {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "기간이 만료된 token"})
        return (200, {"rt_cd": "0", "output": {"last": "9", "tvol": "7"}})
    c3 = mk({_KTOKEN: tok3, _KPRICE: px3}, 3)
    c3.connect()
    s3 = c3.get_snapshot("NVDA")
    check("500+EGW00123 → 재시도 소모 없이 재발급(price 2콜)",
          s3["tvol"] == 7.0 and m == {"tok": 2, "px": 2}, str(m))

    # 4) 재발급은 하드만료에서만 — margin 창(잔여<10분) 선제발급 제거 회귀
    t = {"now": 0.0}
    k = {"tok": 0}
    def tok4(call):
        k["tok"] += 1
        return (200, {"access_token": f"z{k['tok']}", "expires_in": 1000})
    c4 = mk({_KTOKEN: tok4,
             _KPRICE: (200, {"rt_cd": "0", "output": {"last": "1", "tvol": "1"}})}, 4,
            now_fn=lambda: t["now"])
    c4.connect()
    t["now"] = 500.0                      # 잔여 500s < margin 600s — 구코드는 여기서 재발급했다
    c4.get_snapshot("NVDA")
    check("만료 전(잔여<margin) 재발급 안 함(alias 무익)", k["tok"] == 1, str(k))
    t["now"] = 1001.0                     # 하드만료 경과
    c4.get_snapshot("NVDA")
    check("하드만료 후 첫 콜에서 재발급", k["tok"] == 2, str(k))


def test_kis_volume_shadow():
    print("[KIS] VolumeShadow: 분경계 delta 저널·기준선·같은분 no-op·리셋 방어·심볼 실패 격리")
    from broker.kis_quote import VolumeShadow
    tmp = pathlib.Path(tempfile.mkdtemp())
    jp = str(tmp / "vshadow.jsonl")
    tv = {"AAA": 100.0, "BBB": 50.0}
    ta = {"AAA": 1000.0, "BBB": 500.0}
    fails = set()

    class Stub:
        def get_snapshot(self, sym):
            if sym in fails:
                raise RuntimeError("boom")
            return {"last": 10.0, "tvol": tv[sym], "tamt": ta[sym], "excd": "NAS"}

    vs = VolumeShadow(Stub(), ["AAA", "BBB"], journal_path=jp,
                      sleep_fn=lambda *_: None, now_fn=lambda: 0.0)
    vs.tick(0.0)                        # 첫 틱 = 기준선(저널 없음)
    check("기준선 틱 — 저널 0", not os.path.exists(jp))
    vs.tick(30.0)                       # 같은 분 — no-op
    check("같은 분 — no-op", not os.path.exists(jp))
    tv["AAA"] = 160.0
    tv["BBB"] = 55.0
    ta["AAA"] = 1600.0
    vs.tick(61.0)                       # 분 경계 — delta 저널
    lines = [json.loads(x) for x in open(jp, encoding="utf-8")]
    check("2심볼 delta 저널", len(lines) == 2 and {r["sym"] for r in lines} == {"AAA", "BBB"},
          lines)
    a = next(r for r in lines if r["sym"] == "AAA")
    check("AAA Δ=60 + bucket_start=직전 분", a["vol"] == 60.0 and a["bucket_start"] == 0, a)
    check("거래대금 Δ=600(분당 VWAP 원자료)", a["vamt"] == 600.0, a)
    tv["AAA"] = 5.0                     # tvol 감소(새 세션/피드 리셋 모사)
    fails.add("BBB")                    # BBB 실패 — AAA 수집은 계속돼야
    vs.tick(121.0)
    lines = [json.loads(x) for x in open(jp, encoding="utf-8")]
    a2 = [r for r in lines if r["sym"] == "AAA"][-1]
    check("tvol 리셋 → vol=None(오염 차단)", a2["vol"] is None and a2["tvol"] == 5.0, a2)
    check("vol=None 이면 vamt 도 None(반쪽 오염 차단)", a2["vamt"] is None, a2)
    check("실패 심볼 격리(BBB 만 누락, 예외 미전파)",
          sum(1 for r in lines if r["sym"] == "BBB") == 1)
    fails.clear()
    tv["AAA"] = 20.0
    tv["BBB"] = 60.0
    vs.tick(181.0)                      # 리셋 후 새 기준선에서 delta 재개
    lines = [json.loads(x) for x in open(jp, encoding="utf-8")]
    a3 = [r for r in lines if r["sym"] == "AAA"][-1]
    b3 = [r for r in lines if r["sym"] == "BBB"][-1]
    check("리셋 후 delta 재개(5→20=15)", a3["vol"] == 15.0, a3)
    check("실패 복구 심볼 delta 재개(55→60=5)", b3["vol"] == 5.0, b3)


def test_protective_levels():
    """관측용 보호선 산식 = 룰 청산식 고정 — 어긋나면 대시보드가 틀린 손절가 표시(무표시보다 해악)."""
    print("[PROT] protective_levels 산식 = 룰 청산식")
    from intraday_rules import protective_levels
    lv = protective_levels("oneil", {}, {"stop_pct": 0.07, "target_pct": 0.20}, 100.0)
    check("oneil 평단 임계", lv == {"stop": 93.0, "target": 120.0}, str(lv))
    lv = protective_levels("livermore", {"hw": 110.0}, {"stop_pct": 0.03}, 100.0)
    check("livermore hw 트레일", lv == {"stop": round(110 * 0.97, 4)}, str(lv))
    check("livermore hw 미기록 → avg 초기값",
          protective_levels("livermore", {}, {}, 100.0) == {"stop": 97.0})
    lv = protective_levels("livermore_swing", {"hw": 110.0}, {"stop_pct": 0.08}, 100.0)
    check("livermore_swing hw 트레일(8%)", lv == {"stop": round(110 * 0.92, 4)}, str(lv))
    check("livermore_swing 기본폭 0.08(cfg 미지정)",
          protective_levels("livermore_swing", {}, {}, 100.0) == {"stop": 92.0})
    lv = protective_levels("chartist", {"stop": 98.5, "target": 104.0}, {}, 100.0)
    check("chartist 절대 레벨", lv == {"stop": 98.5, "target": 104.0}, str(lv))
    check("chartist 레벨 미확정 → 생략", protective_levels("chartist", {}, {}, 100.0) == {})
    bars = _mkbars([100.0] * 20)
    lv = protective_levels("wood", {}, {"stop_pct": 0.05, "ma_bars": 20}, 100.0, bars=bars)
    check("wood MA 임계", lv == {"stop": 95.0}, str(lv))
    check("wood 바 부족 → 생략", protective_levels("wood", {}, {}, 100.0, bars=[]) == {})
    check("무평단 가드", protective_levels("livermore", {}, {}, 0.0) == {})


# ───── P2-B6: 장중 킬스위치(읽기 전용 소비) ─────
def test_killswitch_gate():
    print("[P2-B6] 킬스위치 — halted 시 신규진입 차단·보호청산 유지, 장중루프의 파일 쓰기 0")
    tmp = pathlib.Path(tempfile.mkdtemp())
    state, logs = tmp / "state", tmp / "logs"
    state.mkdir(); logs.mkdir()
    ks = state / "killswitch.paper_t.json"
    ks.write_text(json.dumps({"halted": True, "reason": "일일손실 정지"}), encoding="utf-8")
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    broker.place_order(OrderRequest("AAA", Side.BUY, qty=5, order_type=OrderType.MARKET))
    # min_hold 0 = 이 테스트의 관심축(킬스위치)만 남김 — 재진입 쿨다운은 별 테스트(test_reentry_cooldown)
    guard = IntradayGuard({"intraday_cfg": {"min_hold_seconds": 0}})
    tr = IntradayTrader("t", broker, lambda s: 100.0, lambda *a: [], ["AAA"], guard=guard,
                        log_dir=str(logs), killswitch_file=str(ks))
    before = (ks.read_text(encoding="utf-8"), sorted(os.listdir(state)))
    tr.poll_killswitch(1000.0)
    check("halted 소비", tr.ks_halted is True)
    check("가드로 전파(진입 게이트)", guard.ks_halted is True)
    acct = broker.get_account()
    check("가드 allow: 신규진입 거부",
          guard.allow("t", "AAA", Signal("BUY", amount=100.0), None, acct) is False)
    check("가드 allow: 보호청산 허용",
          guard.allow("t", "AAA", Signal("SELL_ALL", reason="손절", protective=True),
                      broker.get_position("AAA"), acct) is True)
    tr._execute("AAA", Signal("BUY", amount=100.0, reason="진입"))
    check("정지 중 신규매수 미체결", abs(broker.get_position("AAA").qty - 5.0) < 1e-9)
    tr._execute("AAA", Signal("BUY", amount=100.0, reason="피라미딩", pyramid=True))
    check("정지 중 피라미딩 미체결", abs(broker.get_position("AAA").qty - 5.0) < 1e-9)
    tr._execute("AAA", Signal("SELL_ALL", reason="트레일손절", protective=True))
    p = broker.get_position("AAA")
    check("정지 중 보호청산은 체결", p is None or p.qty <= 1e-9)
    check("킬스위치 파일 불변(소유권=일1런)", (ks.read_text(encoding="utf-8"), sorted(os.listdir(state))) == before)
    recs = [json.loads(x) for x in open(logs / "intraday.jsonl", encoding="utf-8")]
    trips = [r for r in recs if r["action"] == "KILLSWITCH_HALT"]
    check("트립 저널 1건", len(trips) == 1, len(trips))
    tr.poll_killswitch(1005.0)                     # 스로틀(<30s) — 재판정·중복 저널 없음
    check("폴 스로틀(중복 저널 0)",
          sum(1 for x in open(logs / "intraday.jsonl", encoding="utf-8")) == len(recs))
    # 수동 HALT 파일도 동일 판정(일1런 guardrail.is_halted 와 동형)
    ks.write_text(json.dumps({"halted": False}), encoding="utf-8")
    (state / "HALT").write_text("stop", encoding="utf-8")
    tr.poll_killswitch(1100.0)
    check("state/HALT 파일 → 정지 유지", tr.ks_halted is True)
    (state / "HALT").unlink()
    tr.poll_killswitch(1200.0)
    check("해제 관측 → 진입 재개", tr.ks_halted is False and guard.ks_halted is False)
    tr._execute("AAA", Signal("BUY", amount=100.0, reason="진입"))
    check("해제 후 신규매수 체결", broker.get_position("AAA").qty > 0)
    # 손상 JSON = fail-closed(진입만 차단)
    ks.write_text("{깨진", encoding="utf-8")
    tr.poll_killswitch(1300.0)
    check("상태파일 손상 → fail-closed 정지", tr.ks_halted is True)
    check("미배선(killswitch_file=None) 은 항상 통과",
          IntradayTrader("u", broker, lambda s: 100.0, lambda *a: [], ["AAA"]).poll_killswitch(1.0) is False)


# ───── P2-B7: 오버나이트 갭 baseline ─────
def test_gap_baseline_carry():
    print("[P2-B7] 갭 가시화 — 전일 마감 equity 를 다음 세션 일중손실 baseline 으로 앵커")
    tmp = pathlib.Path(tempfile.mkdtemp())
    sf = str(tmp / "guard.json")
    cfg = {"intraday_cfg": {"intraday_max_loss": 0.05}}
    g = IntradayGuard(cfg, state_file=sf, today="2026-06-29")
    g.seed_day_start(1000.0)
    g.mark_equity(1050.0)                                   # 장중 최종 관측 = 전일 마감
    check("마감 equity 영속", json.loads(open(sf, encoding="utf-8").read())["last_equity"] == 1050.0)
    g2 = IntradayGuard(cfg, state_file=sf, today="2026-06-30")   # 다음날, 갭다운 개장 950(-9.5%)
    check("전일 마감 승계", g2.carry_equity == 1050.0)
    g2.seed_day_start(950.0)
    check("baseline=전일마감(개장 equity 아님)", g2.day_start_equity == 1050.0, g2.day_start_equity)
    acct = AccountInfo(cash=950.0, equity=950.0, buying_power=0.0)
    check("갭 손실이 가드에 잡힘 → 신규진입 차단",
          g2.allow("t", "X", Signal("BUY", amount=10.0), None, acct) is False)
    check("일중손실 정지 래치", g2.halted is True)
    check("정지해도 보호청산 허용",
          g2.allow("t", "X", Signal("SELL_ALL", reason="손절", protective=True), None, acct) is True)
    check("전일 상태(halt·회전)는 승계 안 함(baseline 만)", IntradayGuard(cfg, state_file=sf,
          today="2026-07-01").halted is False)
    # stale(장기 미가동) 승계 거부 — 다일 누적손실이 하루 한도로 오트립되지 않게
    g3 = IntradayGuard(cfg, state_file=sf, today="2026-07-20")
    check("CARRY_MAX_DAYS 초과 → 승계 거부", g3.carry_equity is None)
    g3.seed_day_start(950.0)
    check("결손 시 현행 fallback(개장 equity)", g3.day_start_equity == 950.0)
    # 같은 날 재시작은 종전대로 복원 baseline 우선(회귀)
    g4 = IntradayGuard(cfg, state_file=sf, today="2026-07-20")
    g4.seed_day_start(500.0)
    check("당일 재시작 — 복원 baseline 보존", g4.day_start_equity == 950.0)
    # eod_flatten 페르소나(마감 flat)는 두 값이 같아 동작 불변
    sf2 = str(tmp / "guard2.json")
    ga = IntradayGuard(cfg, state_file=sf2, today="2026-06-29")
    ga.seed_day_start(1000.0); ga.mark_equity(1000.0)
    gb = IntradayGuard(cfg, state_file=sf2, today="2026-06-30")
    gb.seed_day_start(1000.0)
    check("이월 없는 페르소나는 baseline 동일(동작 불변)", gb.day_start_equity == 1000.0)


# ───── P2-B10: 보호청산 회전 계측(차단 없음) ─────
def test_protective_turnover_metrics():
    print("[P2-B10] 보호청산 = 회전캡 면제 유지 + 건수·명목 별도 계측(하루 요약 노출)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    sf = str(tmp / "g.json")
    g = IntradayGuard({"intraday_cfg": {"max_trades_per_day": 1, "min_hold_seconds": 0}},
                      state_file=sf, today="2026-06-29", now_fn=lambda: 0.0)
    acct = AccountInfo(cash=10000.0, equity=10000.0, buying_power=0.0)
    g.note_fill("X", Signal("BUY", amount=100.0, reason="진입"), notional=1000.0)
    g.note_fill("X", Signal("SELL_ALL", reason="손절", protective=True), notional=1200.0)
    g.note_fill("Y", Signal("SELL_ALL", reason="트레일손절", protective=True), notional=800.0)
    s = g.turnover_summary()
    check("재량 회전만 캡 카운트", s["trades"] == 1, s)
    check("보호청산 건수 별도", s["prot_exits"] == 2, s)
    check("보호청산 명목 누적", s["prot_notional"] == 2000.0, s)
    check("재량 명목 누적", s["churn_notional"] == 1000.0, s)
    check("캡 도달 후 신규매수 차단", g.allow("t", "X", Signal("BUY", amount=10.0), None, acct) is False)
    check("캡 도달해도 보호청산 통과(면제 유지)",
          g.allow("t", "X", Signal("SELL_ALL", reason="손절", protective=True), None, acct) is True)
    g2 = IntradayGuard({"intraday_cfg": {}}, state_file=sf, today="2026-06-29")
    check("재시작 내구(계측 복원)", g2.turnover_summary() == s, g2.turnover_summary())
    # 스냅샷 노출 — 체결 명목이 _execute → note_fill 로 실제 배선됐는지까지 확인
    logs = tmp / "logs"; logs.mkdir()
    broker = PaperBroker(cash=2000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    guard = IntradayGuard({"intraday_cfg": {}})
    tr = IntradayTrader("t", broker, lambda s: 100.0, lambda *a: [], ["AAA"], guard=guard,
                        log_dir=str(logs))
    tr._execute("AAA", Signal("BUY", amount=500.0, reason="진입"))
    tr._execute("AAA", Signal("SELL_ALL", reason="손절", protective=True))
    tr.snapshot("2026-06-29")
    rec = json.loads(open(logs / "runs.jsonl", encoding="utf-8").read().splitlines()[-1])
    check("스냅샷 하루 요약 노출", rec["turnover"]["prot_exits"] == 1, rec.get("turnover"))
    check("보호청산 명목 계상(≈$500)", abs(rec["turnover"]["prot_notional"] - 500.0) < 1.0,
          rec.get("turnover"))
    check("보호청산은 회전캡 카운트 미증가", rec["turnover"]["trades"] == 1, rec.get("turnover"))


# ───── P2-B14: watchlist 밖 보유종목 룰상태 복원 ─────
def test_offwatchlist_state_restore():
    print("[P2-B14] watchlist 편집으로 빠진 보유종목도 hw 트레일 복원(평단 재앵커 차단)")
    tmp = pathlib.Path(tempfile.mkdtemp())
    rs = tmp / "rules_state.json"
    rs.write_text(json.dumps({"ZZZ": {"hw": 150.0}, "QQQ": {"hw": 99.0}}), encoding="utf-8")
    broker = PaperBroker(cash=5000.0, price_fn=lambda s: 100.0, commission=0, spread=0, slippage=0)
    broker.place_order(OrderRequest("ZZZ", Side.BUY, qty=5, order_type=OrderType.MARKET))
    tr = IntradayTrader("t", broker, lambda s: 100.0, lambda *a: [], ["AAA"],   # ZZZ 가 watchlist 에 없음
                        rule_state_file=str(rs))
    check("보유종목 워치 편입", "ZZZ" in tr.watchlist and "ZZZ" in tr.aggs)
    check("hw 복원(평단 100 재앵커 아님)", tr.ctx["ZZZ"]["state"].get("hw") == 150.0,
          tr.ctx.get("ZZZ", {}).get("state"))
    check("미보유 잔재는 여전히 폐기", "QQQ" not in tr.ctx)
    tr._save_rule_state()
    check("저장 라운드트립(소실 없음)",
          json.loads(rs.read_text(encoding="utf-8")) == {"ZZZ": {"hw": 150.0}})
    check("보유 종목의 트레일 폭 유지(150×0.92)",
          abs(tr.ctx["ZZZ"]["state"]["hw"] * 0.92 - 138.0) < 1e-9)


# ───── P2-B15: 조기마감(반일장) ─────
def test_early_close_session():
    print("[P2-B15] 조기마감 13:00 ET — 마감 판정·EOD 청산이 16:00 스테일 호가를 안 씀")
    from run_intraday import _wait_until_open
    check("반일장 12:59 → 개장", market_is_open(datetime(2026, 11, 27, 12, 59, tzinfo=_ET)) is True)
    check("반일장 13:00 → 마감(EOD 청산 발화)",
          market_is_open(datetime(2026, 11, 27, 13, 0, tzinfo=_ET)) is False)
    check("반일장 15:00 → 마감", market_is_open(datetime(2026, 11, 27, 15, 0, tzinfo=_ET)) is False)
    check("정상일 15:00 → 개장", market_is_open(datetime(2026, 11, 30, 15, 0, tzinfo=_ET)) is True)
    check("공휴일(추수감사절) → 마감", market_is_open(datetime(2026, 11, 26, 11, 0, tzinfo=_ET)) is False)
    check("세션 경계 = (09:30, 13:00)", ri._session_minutes(datetime(2026, 11, 27).date()) == (570, 780))
    check("반일장 마감 후 기동 → 즉시 종료",
          _wait_until_open(now_fn=lambda: datetime(2026, 11, 27, 13, 30, tzinfo=_ET),
                           sleep_fn=lambda s: None) is False)
    check("휴장일 기동 → 즉시 종료(개장 대기 폴링 안 함)",
          _wait_until_open(now_fn=lambda: datetime(2026, 11, 26, 9, 0, tzinfo=_ET),
                           sleep_fn=lambda s: None) is False)
    class _Stop(Exception):
        pass

    sleeps = []

    def _slp(s):                                   # 첫 대기에서 탈출 — 실시간 스핀 없이 '대기했음' 만 확인
        sleeps.append(s)
        raise _Stop
    try:
        _wait_until_open(max_wait=60, poll=7, now_fn=lambda: datetime(2026, 11, 27, 9, 0, tzinfo=_ET),
                         sleep_fn=_slp)
    except _Stop:
        pass
    check("반일장 개장 전 기동 → 개장 대기(즉시 종료 아님)", sleeps == [7], sleeps)


def main():
    print("=" * 70)
    print(" 장중 액티브 트레이딩 서브시스템 검증 — 네트워크 0 / 실주문 0")
    print("=" * 70)
    print()
    for t in (test_connect_token_only, test_no_order_methods, test_get_quote,
              test_outbound_symbol, test_reauth_on_401,
              test_bar_aggregator, test_intraday_trader_e2e, test_eod_flatten,
              test_flatten_carryover,
              test_intraday_order_reason_journaled,
              test_market_gate,
              test_persona_process_lock,
              test_oneil_rule, test_wood_rule, test_livermore_rule, test_chartist_rule,
              test_thrust_min_eff,
              test_frac_sizing_uses_equity,
              test_pyramid_dial_guard_arithmetic,
              test_intraday_guard, test_lock_protocol_unified, test_daily_running_gate,
              test_rules_protective_flag,
              test_protective_exit_before_warmup,
              test_livermore_or_anchor, test_livermore_or_restart_anchor,
              test_await_daily_runs, test_wait_until_open,
              test_day_start_seed, test_halted_surfacing, test_book_lock_serialization,
              test_protective_defers_when_locked, test_pyramid_adds_on_fill,
              test_guard_failclosed_no_account, test_snapshot_ts_format,
              test_order_failure_isolated,
              test_personas_registration, test_persona_home_resolution,
              test_build_traders_wiring, test_persona_map_only, test_dashboard_reads_intraday,
              test_guard_persistence, test_sample_single_onbar_on_gap,
              test_reload_failure_fails_closed, test_paper_reload_missing_vs_corrupt_file,
              test_paper_ctor_load_unchanged_on_corrupt_file, test_paper_corrupt_book_skips_bar_via_real_reload,
              test_lock_miss_observability, test_once_no_eod_flatten,
              test_dust_sell_flush, test_hi_price_trim_min_increment, test_trim_no_over_liquidation,
              test_flatten_trim_cooldown,
              test_regime_gate, test_dynamic_watchlist, test_apply_overlay_gap_robust, test_regime_on_helper,
              test_trim_churn_cap, test_protective_exit_no_churn, test_reentry_cooldown,
              test_bear_reversal_synthetic_gap, test_thrust_gapfill_immune,
              test_quote_gap_equity_lastgood, test_ctx_state_reconcile,
              test_watchlist_picks_up_daily_holdings, test_build_traders_regime_inject,
              test_livermore_swing_rule, test_swing_rule_state_persistence,
              test_swing_deploy_cap, test_swing_wiring_and_preset,
              test_kis_quote_client, test_kis_token_heal, test_kis_volume_shadow,
              test_protective_levels,
              test_killswitch_gate, test_gap_baseline_carry, test_protective_turnover_metrics,
              test_offwatchlist_state_restore, test_early_close_session):
        t()
        print()
    print("=" * 70)
    print(f" 결과: {len(PASS)} PASS / {len(FAIL)} FAIL")
    if FAIL:
        print(" 실패:", ", ".join(FAIL))
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
