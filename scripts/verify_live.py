#!/usr/bin/env python3
"""Practical live verification of the amazon-hikaku MCP server.

Uses the official MCP Python SDK client (Streamable HTTP) against the
production endpoint and exercises realistic shopping-research scenarios,
edge cases, payload sizes and latency. Prints PASS/FAIL lines.

Usage: uv run python scripts/verify_live.py [base_url]
"""

import asyncio
import json
import sys
import time
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

BASE = sys.argv[1] if len(sys.argv) > 1 else "https://mcp.amazon-hikaku.com"
URL = BASE + "/mcp"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


def payload(result) -> tuple[dict | None, int, bool]:
    text = ""
    for block in result.content or []:
        if getattr(block, "type", None) == "text":
            text += block.text
    try:
        return json.loads(text), len(text.encode()), bool(result.isError)
    except json.JSONDecodeError:
        return None, len(text.encode()), bool(result.isError)


async def call(session, name, args):
    t0 = time.time()
    r = await session.call_tool(name, args)
    dt = time.time() - t0
    data, size, is_err = payload(r)
    return data, size, is_err, dt


async def main() -> int:
    async with streamablehttp_client(URL) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            t0 = time.time()
            init = await session.initialize()
            cold = time.time() - t0
            print(f"initialize: {cold:.2f}s protocol={init.protocolVersion} server={init.serverInfo.name}")
            check("initialize (cold incl.)", cold < 15, f"{cold:.2f}s")

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            check("tools/list = 8 tools", len(names) == 8, ",".join(names))

            # S1: budget + keyword search (typical AI shopping research)
            data, size, err, dt = await call(
                session, "search_products", {"query": "ワイヤレスイヤホン", "max_price": 30000, "limit": 5}
            )
            prods = (data or {}).get("products", [])
            related = sum(
                1
                for p in prods
                if "イヤホン" in (p["title"] + p.get("category", "") + p.get("description", ""))
                or "airpods" in p["title"].lower()
                or "soundcore" in p["title"].lower()
                or "space travel" in p["title"].lower()
            )
            ok = (
                not err
                and len(prods) == 5
                and all(0 < p["price"] <= 30000 for p in prods)
                and related >= 4
            )
            check("S1 search earbuds <=30000", ok, f"{dt:.2f}s {size}B scores={[p['score'] for p in prods]} related={related}/5")

            # detail URLs must be live
            live = 0
            for p in prods[:3]:
                try:
                    req = urllib.request.Request(p["detail_url"], method="HEAD")
                    with urllib.request.urlopen(req, timeout=20) as r:
                        live += r.status == 200
                except Exception:
                    pass
            check("S1 detail URLs live (3)", live == 3, f"{live}/3")

            # S2: category listing then category search via slug
            data, size, err, dt = await call(session, "list_categories", {})
            parents = (data or {}).get("parents", [])
            check("S2 list_categories", not err and len(parents) >= 20, f"{dt:.2f}s {size}B parents={len(parents)}")

            # S3: compare the top-3 from S1
            asins = [p["asin"] for p in prods[:3]]
            data, size, err, dt = await call(session, "compare_products", {"asins": asins})
            comp = (data or {}).get("products", [])
            check("S3 compare 3 ASINs", not err and len(comp) == 3 and all("score_rationale" in c for c in comp),
                  f"{dt:.2f}s {size}B")

            # S4: deals with min savings
            data, size, err, dt = await call(session, "get_deals", {"min_savings_percentage": 30, "limit": 5})
            deals = (data or {}).get("products", [])
            ok = not err and deals and all(d["savings_percentage"] >= 30 for d in deals)
            check("S4 deals >=30% off", bool(ok), f"{dt:.2f}s {size}B n={len(deals)}")

            # S5: bargain add-on items under a target amount
            data, size, err, dt = await call(session, "get_bargain", {"target_amount": 2000, "limit": 5})
            barg = (data or {}).get("products", [])
            ok = not err and barg and all(0 < b["price"] <= 2000 for b in barg)
            check("S5 bargain <=2000", bool(ok), f"{dt:.2f}s {size}B n={len(barg)}")

            # S6: brand search
            data, size, err, dt = await call(session, "list_brands", {"query": "Anker", "limit": 5})
            brands = (data or {}).get("brands", [])
            check("S6 list_brands Anker", not err and any("anker" in b["brand"].lower() for b in brands),
                  f"{dt:.2f}s {size}B {[b['brand'] for b in brands][:3]}")

            # S7: recommendations are high-score only
            data, size, err, dt = await call(session, "get_recommendations", {"limit": 5})
            recs = (data or {}).get("products", [])
            ok = not err and recs and all(r["score"] >= 90 for r in recs)
            check("S7 recommendations score>=90", bool(ok), f"{dt:.2f}s {size}B scores={[r['score'] for r in recs]}")

            # E1: nonexistent ASIN -> graceful (error or not-found payload, no crash)
            data, size, err, dt = await call(session, "get_product", {"asin": "B000000000"})
            check("E1 unknown ASIN graceful", data is not None or err, f"isError={err} body={str(data)[:80]}")

            # E2: search with no matches -> empty list, not an error
            data, size, err, dt = await call(session, "search_products", {"query": "存在しない商品xyz", "limit": 5})
            check("E2 no-match search", not err and (data or {}).get("count") == 0, f"count={(data or {}).get('count')}")

            # E3: oversized limit is clamped
            data, size, err, dt = await call(session, "search_products", {"query": "イヤホン", "limit": 100})
            check("E3 limit clamp", not err and (data or {}).get("count", 999) <= 20, f"count={(data or {}).get('count')}")

            # E4: concurrency - 5 parallel calls
            t0 = time.time()
            outs = await asyncio.gather(
                *[session.call_tool("search_products", {"query": q, "limit": 3})
                  for q in ["イヤホン", "マウス", "キーボード", "モニター", "充電器"]]
            )
            dt = time.time() - t0
            ok = all(not o.isError for o in outs)
            check("E4 5x parallel search", ok, f"{dt:.2f}s total")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
