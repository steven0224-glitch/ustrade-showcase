"""읽기전용 MCP 서버 — 자동매매 상태/저널을 Claude Desktop 등에 노출 (stdio JSON-RPC 2.0).

HKUDS/Vibe-Trading 의 mcp_server 아이디어 이식: 매매 루프는 결정론 유지하고, **분석·회고에만**
LLM 을 붙이는 안전한 접점. Vibe 와 동일 원칙 — **주문/제어 툴은 절대 노출 안 함**(읽기 전용).

의존성 0 (mcp/fastmcp 미설치): stdio 위 newline-delimited JSON-RPC 를 직접 구현. Claude Desktop
config 예:
  { "mcpServers": { "ustrade": { "command": "python", "args": ["<repo>/mcp_server.py"] } } }

노출 툴(전부 read-only):
  health              킬스위치/HALT 상태 (+ 페르소나 namespace)
  holdings            최신 계좌 스냅샷(현금·평가액·보유종목)
  recent_runs         최근 N 런 요약(세션·브로커·페르소나·상태·레짐·final)
  selection_reasons   최신 선택의 종목별 근거(scores·canslim·piotroski·analyst·momentum)
  signal_performance  신호 차원별 사후수익 집계(selection_review)
  realized_roundtrips 실거래 실현 라운드트립 요약(review FIFO)
"""
import glob
import json
import os
import sys

SERVER_NAME = "ustrade-readonly"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL = "2024-11-05"


def _err(msg):
    print(f"[mcp_server] {msg}", file=sys.stderr, flush=True)


# ── 읽기 헬퍼 (전부 guarded, throw 안 함) ─────────────────────────────
def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def tool_health(_args):
    from broker.guardrail import KILL_FILE, STATE_FILE
    from paths import STATE_DIR
    out = {"halt_file": os.path.exists(KILL_FILE), "killswitch": _read_json(STATE_FILE)}
    ns = {}
    for p in glob.glob(str(STATE_DIR / "killswitch.*.json")):
        name = os.path.basename(p)[len("killswitch."):-len(".json")]
        ns[name] = _read_json(p)
    if ns:
        out["namespaces"] = ns
    return out


def _recent_recs(limit):
    import review
    recs = review.load_journals(real_only=False)
    return recs[-limit:] if limit and len(recs) > limit else recs


def tool_holdings(_args):
    recs = _recent_recs(0)
    for r in reversed(recs):
        if "account" in r or "positions" in r:
            return {"session": r.get("session"), "broker": r.get("broker"),
                    "persona": r.get("persona", "real"),
                    "account": r.get("account"), "positions": r.get("positions", [])}
    return {"note": "저널에 계좌 스냅샷 없음"}


def tool_recent_runs(args):
    limit = int(args.get("limit", 10) or 10)
    out = []
    for r in _recent_recs(limit):
        sel = r.get("selection") or {}
        out.append({
            "session": r.get("session"), "broker": r.get("broker"),
            "persona": r.get("persona", "real"), "status": r.get("status"),
            "reason": r.get("reason"),
            "regime": (r.get("risk") or {}).get("regime"),
            "final": sel.get("final") or [],
            "equity": (r.get("account") or {}).get("equity"),
        })
    return {"count": len(out), "runs": out}


def tool_selection_reasons(_args):
    for r in reversed(_recent_recs(0)):
        sel = r.get("selection") or {}
        if sel.get("final"):
            final = sel["final"]
            scores = sel.get("scores") or {}
            pio = sel.get("piotroski") or {}
            canslim = set(sel.get("canslim") or [])
            analyst = set(sel.get("analyst") or [])
            mom = set(sel.get("momentum_only") or [])
            reasons = {t: {"score": scores.get(t), "piotroski": pio.get(t),
                           "canslim": t in canslim, "analyst": t in analyst,
                           "momentum_only": t in mom} for t in final}
            return {"session": r.get("session"), "persona": r.get("persona", "real"),
                    "final": final, "reasons": reasons}
    return {"note": "final 선택이 있는 런 없음"}


def tool_signal_performance(args):
    import selection_review as sr
    horizon = int(args.get("horizon", 20) or 20)
    from paths import LOG_DIR, persona_homes
    log_dirs = [LOG_DIR] + [h / "logs" for h in persona_homes()]
    _md, dims, meta = sr.run(horizon=horizon, log_dirs=log_dirs)
    return {"meta": meta, "overall": dims.get("_overall"),
            "persona": dims.get("persona"), "canslim": dims.get("canslim"),
            "regime": dims.get("regime")}


def tool_realized_roundtrips(_args):
    import review
    recs = review.load_journals(real_only=True)
    fills = review.extract_fills(recs)
    rt = review.round_trips(fills)
    trips = rt.get("trips", [])
    if not trips:
        return {"trips": 0, "note": "실현 라운드트립 없음"}
    total = sum(t["pnl"] for t in trips)
    wins = sum(1 for t in trips if t["pnl"] > 0)
    return {"trips": len(trips), "realized_pnl": round(total, 2),
            "win_rate": round(wins / len(trips), 3), "open_positions": rt.get("open", {})}


TOOLS = {
    "health": (tool_health, "킬스위치/HALT 상태 (+페르소나 namespace)", {"type": "object", "properties": {}}),
    "holdings": (tool_holdings, "최신 계좌 스냅샷(현금·평가액·보유종목)", {"type": "object", "properties": {}}),
    "recent_runs": (tool_recent_runs, "최근 N 런 요약",
                    {"type": "object", "properties": {"limit": {"type": "integer", "default": 10}}}),
    "selection_reasons": (tool_selection_reasons, "최신 선택의 종목별 근거", {"type": "object", "properties": {}}),
    "signal_performance": (tool_signal_performance, "신호 차원별 사후수익 집계(H거래일)",
                           {"type": "object", "properties": {"horizon": {"type": "integer", "default": 20}}}),
    "realized_roundtrips": (tool_realized_roundtrips, "실거래 실현 라운드트립 요약(FIFO)",
                            {"type": "object", "properties": {}}),
}


# ── JSON-RPC 처리 ────────────────────────────────────────────────────
def _handle(req):
    """요청 dict → 응답 dict(또는 None=알림, 응답 없음)."""
    method = req.get("method")
    rid = req.get("id")
    is_notification = "id" not in req

    if method == "initialize":
        client_proto = (req.get("params") or {}).get("protocolVersion") or DEFAULT_PROTOCOL
        return _ok(rid, {"protocolVersion": client_proto,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})

    if method in ("notifications/initialized", "initialized"):
        return None   # 알림 — 응답 없음

    if method == "ping":
        return _ok(rid, {})

    if method == "tools/list":
        tools = [{"name": n, "description": d, "inputSchema": s} for n, (_f, d, s) in TOOLS.items()]
        return _ok(rid, {"tools": tools})

    if method == "tools/call":
        params = req.get("params") or {}
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            return _rpc_err(rid, -32602, f"unknown tool: {name}")
        try:
            result = TOOLS[name][0](args)
            text = json.dumps(result, ensure_ascii=False, indent=2, default=str)
            return _ok(rid, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as e:
            _err(f"tool {name} 실패: {e!r}")
            return _ok(rid, {"content": [{"type": "text", "text": f"error: {e!r}"}], "isError": True})

    if is_notification:
        return None
    return _rpc_err(rid, -32601, f"method not found: {method}")


def _ok(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_err(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def serve(stdin=None, stdout=None):
    """stdio JSON-RPC 루프 — newline-delimited. stdout 은 전송채널(로그는 stderr)."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            _err(f"JSON 파싱 실패: {line[:120]!r}")
            continue
        resp = _handle(req)
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            stdout.flush()


if __name__ == "__main__":
    _err(f"{SERVER_NAME} v{SERVER_VERSION} 시작 (읽기 전용, stdio)")
    serve()
