"""JSON-RPC smoke test against a running server.

Usage: python scripts/smoke.py [base_url]   (default http://127.0.0.1:8080)
Exercises initialize, tools/list, and one call per tool; prints timings.
"""

import json
import sys
import time
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


def rpc(method, params=None, rid=1):
    body = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        BASE + "/mcp", data=json.dumps(body).encode(), headers=HEADERS, method="POST"
    )
    start = time.time()
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode()
    elapsed = time.time() - start
    # json_response mode returns plain JSON; tolerate SSE framing too.
    if raw.lstrip().startswith("event:") or "\ndata:" in raw:
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw), elapsed


def call_tool(name, arguments, rid):
    return rpc(
        "tools/call", {"name": name, "arguments": arguments}, rid=rid
    )


def main():
    result, t = rpc(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "smoke", "version": "0"},
        },
    )
    print(f"initialize: {t:.2f}s -> {json.dumps(result.get('result', result), ensure_ascii=False)[:200]}")

    result, t = rpc("tools/list", {}, rid=2)
    tools = [tool["name"] for tool in result["result"]["tools"]]
    print(f"tools/list: {t:.2f}s -> {tools}")
    ann = result["result"]["tools"][0].get("annotations")
    print(f"first tool annotations: {ann}")

    cases = [
        ("search_products", {"query": "ワイヤレスイヤホン", "limit": 3}),
        ("search_products", {"category": "pc-workstation", "limit": 3}),
        ("get_product", {"asin": "B07DY4XYND"}),
        ("compare_products", {"asins": ["B07DY4XYND", "B00PY3EYQO"]}),
        ("list_categories", {}),
        ("list_brands", {"query": "Anker", "limit": 5}),
        ("get_deals", {"limit": 3}),
        ("get_bargain", {"target_amount": 2000, "limit": 3}),
        ("get_recommendations", {"limit": 3}),
    ]
    for i, (name, args) in enumerate(cases, start=10):
        result, t = call_tool(name, args, rid=i)
        payload = result.get("result", result)
        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text = block["text"]
                break
        print(f"{name} {args}: {t:.2f}s isError={payload.get('isError')} -> {text[:220]}")


if __name__ == "__main__":
    main()
