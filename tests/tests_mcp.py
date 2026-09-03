"""읽기전용 MCP 서버 검증 — 네트워크 0 (in-process JSON-RPC). stdio 안 띄움.

핵심 단언: (1) JSON-RPC handshake/list/call 정상, (2) 알림엔 무응답, (3) 미지원 메서드/툴 에러,
(4) **주문/제어 툴이 하나도 없음**(read-only 보안 불변식 — Vibe 원칙 이식).

실행:  python tests_mcp.py
"""
import io
import json

import mcp_server as M

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  — {detail}" if detail and not cond else ""))


def test_initialize():
    print("[RPC] initialize handshake")
    r = M._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18"}})
    check("result 존재", "result" in r)
    check("protocolVersion 에코", r["result"]["protocolVersion"] == "2025-06-18")
    check("serverInfo.name", r["result"]["serverInfo"]["name"] == M.SERVER_NAME)
    check("tools capability", "tools" in r["result"]["capabilities"])


def test_notification_no_response():
    print("[RPC] 알림(notifications/initialized) → 무응답")
    r = M._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    check("None 반환(응답 없음)", r is None)


def test_tools_list():
    print("[RPC] tools/list")
    r = M._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = r["result"]["tools"]
    names = {t["name"] for t in tools}
    check("6개 툴", len(tools) == 6, len(tools))
    check("모두 inputSchema 보유", all("inputSchema" in t and "description" in t for t in tools))
    check("health 포함", "health" in names)


def test_readonly_invariant():
    print("[SEC] 주문/제어 툴 부재 (read-only 불변식)")
    names = set(M.TOOLS)
    expected = {"health", "holdings", "recent_runs", "selection_reasons",
                "signal_performance", "realized_roundtrips"}
    check("툴 집합 == 읽기전용 화이트리스트", names == expected, names ^ expected)
    banned = ("order", "buy", "sell", "trade", "halt", "resume", "run_live", "execute", "cancel")
    leaks = [n for n in names if any(b in n.lower() for b in banned)]
    check("금지 동사(order/buy/sell/halt/run…) 툴명 없음", not leaks, leaks)


def test_tools_call_health():
    print("[RPC] tools/call health (네트워크 0)")
    r = M._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "health", "arguments": {}}})
    check("isError False", r["result"]["isError"] is False)
    payload = json.loads(r["result"]["content"][0]["text"])
    check("halt_file 키 존재", "halt_file" in payload)


def test_unknown_method_and_tool():
    print("[RPC] 미지원 메서드/툴 에러코드")
    r1 = M._handle({"jsonrpc": "2.0", "id": 4, "method": "does/not/exist"})
    check("미지원 메서드 -32601", r1["error"]["code"] == -32601)
    r2 = M._handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "delete_everything", "arguments": {}}})
    check("미지원 툴 -32602", r2["error"]["code"] == -32602)


def test_serve_stdio_roundtrip():
    print("[IO] serve() stdio 루프 — 2요청 2응답, 알림 무응답")
    stdin = io.StringIO(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
    )
    stdout = io.StringIO()
    M.serve(stdin=stdin, stdout=stdout)
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    check("응답 2개(알림 제외)", len(lines) == 2, len(lines))
    ids = [json.loads(ln).get("id") for ln in lines]
    check("id 1,2 순서", ids == [1, 2], ids)


def main():
    print("=" * 60)
    print("읽기전용 MCP 서버")
    print("=" * 60)
    test_initialize()
    test_notification_no_response()
    test_tools_list()
    test_readonly_invariant()
    test_tools_call_health()
    test_unknown_method_and_tool()
    test_serve_stdio_roundtrip()
    print("-" * 60)
    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("실패:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
