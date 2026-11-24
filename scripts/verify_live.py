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
            check("tools/list = 10 tools", len(names) == 10 and "find_similar_products" in names and "list_spec_keys" in names, ",".join(names))

            # P1: prompts are exposed and render usable guidance
            prompts = await session.list_prompts()
            pnames = [p.name for p in prompts.prompts]
            got = await session.get_prompt("shopping_consultation", {"topic": "ワイヤレスイヤホン"})
            gtext = got.messages[0].content.text if got.messages else ""
            check(
                "P1 prompts list/get",
                "shopping_consultation" in pnames and "bargain_hunting" in pnames and "facets" in gtext,
                ",".join(pnames),
            )

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
            facets = (data or {}).get("facets") or {}
            ok = (
                not err
                and len(prods) == 5
                and all(0 < p["price"] <= 30000 for p in prods)
                and all("in_stock" in p for p in prods)
                and all(p.get("citation", "").endswith(f"({p['detail_url']})") for p in prods)
                and all(p.get("score_label") == f"独自スコア {p['score']}点" for p in prods)
                and all("円" in (p.get("price_label") or "") for p in prods)
                and "citation" in ((data or {}).get("citation_note") or "")
                and "score_label" in ((data or {}).get("display_note") or "")
                and related >= 4
                and (data or {}).get("total_count", 0) >= 5
                and len(facets.get("brands", [])) >= 1
                and facets.get("price_max") is not None
            )
            check("S1 search earbuds <=30000", ok, f"{dt:.2f}s {size}B scores={[p['score'] for p in prods]} related={related}/5 total={(data or {}).get('total_count')} brands={[b['brand'] for b in facets.get('brands', [])][:3]}")

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

            # S4: deals with min savings; default score floor (70) applies
            data, size, err, dt = await call(session, "get_deals", {"min_savings_percentage": 30, "limit": 5})
            deals = (data or {}).get("products", [])
            ok = (
                not err
                and deals
                and all(d["savings_percentage"] >= 30 for d in deals)
                and all(d["score"] >= 70 for d in deals)
            )
            check("S4 deals >=30% off & score>=70", bool(ok), f"{dt:.2f}s {size}B n={len(deals)} scores={[d['score'] for d in deals]}")

            # S4b: min_score=0 lifts the floor (override works; savings sort
            # surfaces the high-discount low-score items the floor was hiding)
            data0, _s0, err0, _dt0 = await call(
                session, "get_deals", {"min_savings_percentage": 30, "min_score": 0, "limit": 20, "sort": "savings"}
            )
            deals0 = (data0 or {}).get("products", [])
            ok = not err0 and any(d["score"] < 70 for d in deals0)
            check("S4b deals min_score=0 override", bool(ok), f"n={len(deals0)} min_score_seen={min((d['score'] for d in deals0), default=None)}")

            # S8: sale-type filtering via deal badges + census of live sale types
            data, size, err, dt = await call(session, "get_deals", {"sale_type": "タイムセール", "limit": 5})
            ts = (data or {}).get("products", [])
            badges = (data or {}).get("available_badges", [])
            ok = not err and ts and all("タイムセール" in d.get("deal_badge", "") for d in ts) and badges
            check("S8 deals sale_type=タイムセール", bool(ok), f"n={len(ts)} census={[(b['badge'], b['count']) for b in badges]}")
            data, size, err, dt = await call(session, "get_deals", {"sale_type": "存在しないセール種別", "limit": 5})
            none_found = (data or {}).get("products", [])
            ok = not err and none_found == [] and len((data or {}).get("available_badges", [])) >= 1
            check("S8b unknown sale_type -> 0 + census", bool(ok), f"census_n={len((data or {}).get('available_badges', []))}")

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

            # E2: search with no matches -> empty list + suggestions, not an error
            data, size, err, dt = await call(session, "search_products", {"query": "存在しない商品xyz", "limit": 5})
            check("E2 no-match search", not err and (data or {}).get("count") == 0 and "suggestions" in (data or {}), f"count={(data or {}).get('count')}")

            # S9: katakana brand resolves to the English brand (was 0 hits)
            data, size, err, dt = await call(
                session, "search_products",
                {"brand": "ソニー", "category": "ワイヤレスイヤホン", "min_score": 0, "limit": 5},
            )
            check("S9 brand=ソニー finds Sony", not err and (data or {}).get("total_count", 0) >= 1,
                  f"total={(data or {}).get('total_count')}")

            # S10: abbreviation expansion (ノイキャン was 1 hit)
            data, size, err, dt = await call(
                session, "search_products", {"query": "ノイキャン イヤホン", "min_score": 0, "limit": 20}
            )
            check("S10 ノイキャン expansion >=10", not err and (data or {}).get("total_count", 0) >= 10,
                  f"total={(data or {}).get('total_count')}")

            # S11: deals carry citation / score_label / price_label (P0 fix)
            data, size, err, dt = await call(session, "get_deals", {"limit": 3})
            deals = (data or {}).get("products", [])
            ok = (
                not err
                and deals
                and all(d.get("citation", "").endswith(f"({d['detail_url']})") for d in deals)
                and all(d.get("score_label") == f"独自スコア {d['score']}点" for d in deals)
                and all("円" in (d.get("price_label") or "") for d in deals)
                and (data or {}).get("total_count", 0) >= len(deals)
            )
            check("S11 deals labels present", bool(ok), f"n={len(deals)} total={(data or {}).get('total_count')}")

            # S12: pagination via offset (no overlap, has_more)
            d1, _s, _e, _t = await call(session, "search_products", {"query": "イヤホン", "min_score": 0, "limit": 5, "offset": 0})
            d2, _s, _e, _t = await call(session, "search_products", {"query": "イヤホン", "min_score": 0, "limit": 5, "offset": 5})
            a1 = {p["asin"] for p in (d1 or {}).get("products", [])}
            a2 = {p["asin"] for p in (d2 or {}).get("products", [])}
            check("S12 offset pagination", bool(a1) and bool(a2) and a1.isdisjoint(a2) and (d1 or {}).get("has_more"),
                  f"page1={len(a1)} page2={len(a2)}")

            # S13: compare validation + structured summary + pros/cons
            data, size, err, dt = await call(session, "compare_products", {"asins": asins[:1]})
            invalid_ok = "error" in (data or {})
            data, size, err, dt = await call(session, "compare_products", {"asins": asins})
            comp = (data or {}).get("products", [])
            summ = (data or {}).get("comparison_summary") or {}
            ok = (
                invalid_ok
                and not err
                and len(comp) == 3
                and "cheapest_asin" in summ
                and "price_spread" in summ
                and all("pros" in c and "cons" in c for c in comp)
            )
            check("S13 compare summary/pros/cons", bool(ok), f"summary_keys={sorted(summ.keys())}")

            # S14: bargain with a price floor (min_amount)
            data, size, err, dt = await call(
                session, "get_bargain", {"target_amount": 2000, "min_amount": 1000, "limit": 5}
            )
            barg = (data or {}).get("products", [])
            ok = not err and barg and all(1000 <= b["price"] <= 2000 for b in barg)
            check("S14 bargain 1000..2000", bool(ok), f"n={len(barg)} prices={[b['price'] for b in barg]}")

            # S15: similar products (same category, excludes target)
            data, size, err, dt = await call(session, "find_similar_products", {"asin": asins[0], "limit": 5})
            sims = (data or {}).get("products", [])
            target = (data or {}).get("target") or {}
            ok = (
                not err
                and (data or {}).get("found")
                and sims
                and all(s["asin"] != asins[0] for s in sims)
                and all(s.get("category") == target.get("category") for s in sims)
            )
            check("S15 find_similar_products", bool(ok), f"n={len(sims)} category={target.get('category')}")

            # S16 (F-1): available_badges must not contradict a 0-result
            data, size, err, dt = await call(
                session, "get_deals",
                {"category": "ワイヤレスイヤホン", "min_score": 85, "sale_type": "プライム"},
            )
            badge_names = [b["badge"] for b in (data or {}).get("available_badges", [])]
            ok = not err and (
                (data or {}).get("total_count", 0) > 0 or "プライム会員限定セール" not in badge_names
            )
            check("S16 badges respect min_score floor", bool(ok),
                  f"total={(data or {}).get('total_count')} badges={badge_names}")

            # S17 (F-2): default score sort keeps title/category matches ahead
            # of description-only matches (モババ query)
            data, size, err, dt = await call(session, "search_products", {"query": "モババ", "limit": 5})
            prods = (data or {}).get("products", [])
            flags = [
                ("モバイルバッテリー" in (p["title"] + p.get("category", "")))
                or ("モバイルバッテリ" in (p["title"] + p.get("category", "")))
                for p in prods
            ]
            ok = not err and flags == sorted(flags, reverse=True)
            check("S17 score sort buckets title matches first", bool(ok),
                  f"flags={flags} titles={[p['title'][:20] for p in prods]}")

            # S18 (F-3): similar products prefer the price neighborhood and
            # report total_count
            data, size, err, dt = await call(session, "find_similar_products", {"asin": "B0FSZH9TG9", "limit": 5})
            sims = (data or {}).get("products", [])
            near_flags = [0 if 2800 <= s["price"] <= 8400 else 1 for s in sims]
            ok = (
                not err
                and isinstance((data or {}).get("total_count"), int)
                and near_flags == sorted(near_flags)
            )
            check("S18 similar price neighborhood + total_count", bool(ok),
                  f"total_count={(data or {}).get('total_count')} prices={[s['price'] for s in sims]}")

            # S19 (F-4): pros/cons are not wrapped in outer parentheses
            data, size, err, dt = await call(
                session, "compare_products",
                {"asins": ["B0FSZH9TG9", "B0G19RWV21", "B0FNRS5WB2"]},
            )
            texts = [
                t
                for c in (data or {}).get("products", [])
                for t in (c.get("pros", []) + c.get("cons", []))
            ]
            wrapped = [
                t for t in texts
                if (t.startswith("(") and t.endswith(")")) or (t.startswith("（") and t.endswith("）"))
            ]
            check("S19 pros/cons not paren-wrapped", not err and texts and not wrapped,
                  f"texts={len(texts)} wrapped={len(wrapped)}")

            # S20 (F-5): get_product exposes deal_badge for on-sale items
            d1, _s, _e, _t = await call(session, "get_deals", {"limit": 1})
            deal0 = ((d1 or {}).get("products") or [{}])[0]
            data, size, err, dt = await call(session, "get_product", {"asin": deal0.get("asin", "")})
            ok = (
                not err
                and deal0
                and (data or {}).get("deal_badge") == deal0.get("deal_badge")
            )
            check("S20 get_product deal_badge", bool(ok),
                  f"asin={deal0.get('asin')} badge={(data or {}).get('deal_badge')}")

            # S21 (T2): few-hit searches (1-3) also carry relaxation suggestions
            data, size, err, dt = await call(
                session, "search_products", {"query": "浴室 水垢 洗剤", "min_score": 0}
            )
            total = (data or {}).get("total_count", -1)
            ok = not err and (total == 0 or total > 3 or "suggestions" in (data or {}))
            check("S21 few-hit suggestions", bool(ok), f"total={total} suggestions={'suggestions' in (data or {})}")

            # S22 (T9): freshness fields distinguish investigation vs cache fetch
            prods = (data or {}).get("products", [])
            ok = bool((data or {}).get("data_fetched_at")) and (
                not prods or bool(prods[0].get("last_investigated"))
            )
            check("S22 freshness fields present", ok,
                  f"fetched_at={bool((data or {}).get('data_fetched_at'))} investigated={bool(prods and prods[0].get('last_investigated'))}")

            # S23: exclude_brand removes the facet's top brand entirely
            d1, _s, _e, _t = await call(
                session, "search_products", {"query": "ワイヤレスイヤホン", "min_score": 0, "limit": 5}
            )
            top_brand = ((d1 or {}).get("facets", {}).get("brands") or [{}])[0].get("brand")
            d2, _s, err, _t = await call(
                session, "search_products",
                {"query": "ワイヤレスイヤホン", "min_score": 0, "limit": 20, "exclude_brand": top_brand},
            )
            ok = (
                not err
                and top_brand
                and all(p["brand"] != top_brand for p in (d2 or {}).get("products", []))
                and (d2 or {}).get("total_count", 10**9) < (d1 or {}).get("total_count", 0)
            )
            check("S23 exclude_brand", bool(ok),
                  f"brand={top_brand} total {(d1 or {}).get('total_count')}->{(d2 or {}).get('total_count')}")

            # S24: find_similar_products honors min_score
            data, size, err, dt = await call(
                session, "find_similar_products",
                {"asin": "B0FSZH9TG9", "min_score": 95, "limit": 5},
            )
            sims = (data or {}).get("products", [])
            ok = not err and (data or {}).get("found") and all(s["score"] >= 95 for s in sims)
            check("S24 similar min_score", bool(ok),
                  f"n={len(sims)} scores={[s['score'] for s in sims]}")

            # S25: facets expose parent-category mix (pet vs household)
            data, size, err, dt = await call(
                session, "search_products", {"query": "トイレ", "min_score": 0, "limit": 3}
            )
            parents = {
                f["parent"]: f["count"]
                for f in (data or {}).get("facets", {}).get("category_parents", [])
            }
            ok = not err and parents.get("ペット用品", 0) >= 10
            check("S25 facets category_parents", bool(ok), f"parents={parents}")

            # S26: exclude_category removes the whole pet subtree
            pet_children = {
                "トイレシート・猫砂・衛生", "ドッグフード・犬おやつ", "キャットフード・猫おやつ",
                "その他ペット用品", "自動給水・給餌器・ケージ", "ハーネス・おもちゃ・ケア",
            }
            d1, _s, _e, _t = await call(
                session, "search_products",
                {"query": "トイレ 除菌 消臭", "min_score": 0, "limit": 20},
            )
            d2, _s, err, _t = await call(
                session, "search_products",
                {"query": "トイレ 除菌 消臭", "min_score": 0, "limit": 20,
                 "exclude_category": "ペット用品"},
            )
            ok = (
                not err
                and (d2 or {}).get("total_count", 10**9) < (d1 or {}).get("total_count", 0)
                and all(
                    p["category"] not in pet_children
                    for p in (d2 or {}).get("products", [])
                )
            )
            check("S26 exclude_category pet subtree", bool(ok),
                  f"total {(d1 or {}).get('total_count')}->{(d2 or {}).get('total_count')}")

            # P2: gift_consultation prompt is exposed and renders guidance
            got = await session.get_prompt(
                "gift_consultation",
                {"recipient": "彼女", "occasion": "クリスマス", "budget": "1万円"},
            )
            gtext = got.messages[0].content.text if got.messages else ""
            check(
                "P2 gift_consultation prompt",
                "gift_consultation" in pnames and "recipient" in gtext and "彼女" in gtext,
                ",".join(pnames),
            )

            # S27: occasion filter returns only christmas-tagged gift items
            data, _s, err, _t = await call(
                session, "search_products",
                {"occasion": "クリスマス", "min_score": 0, "limit": 10},
            )
            prods = (data or {}).get("products", [])
            ok = (
                not err
                and (data or {}).get("total_count", 0) >= 1
                and all("クリスマス" in p.get("gift_occasions", []) for p in prods)
                and all(p.get("gift_label") for p in prods)
            )
            check("S27 occasion=christmas gift filter", bool(ok),
                  f"total={(data or {}).get('total_count')}")

            # S28: recipient + occasion combined (mothers_day implies parents)
            data, _s, err, _t = await call(
                session, "search_products",
                {"recipient": "両親", "occasion": "母の日", "min_score": 0, "limit": 10},
            )
            prods = (data or {}).get("products", [])
            ok = (
                not err
                and (data or {}).get("total_count", 0) >= 1
                and all("母の日" in p.get("gift_occasions", []) for p in prods)
                and all("両親" in p.get("gift_recipients", []) for p in prods)
            )
            check("S28 recipient+occasion gift filter", bool(ok),
                  f"total={(data or {}).get('total_count')}")

            # S29: recommendations honor the occasion filter
            data, _s, err, _t = await call(
                session, "get_recommendations", {"occasion": "クリスマス", "limit": 5},
            )
            prods = (data or {}).get("products", [])
            ok = not err and all(
                "クリスマス" in p.get("gift_occasions", []) for p in prods
            )
            check("S29 recommendations occasion filter", bool(ok),
                  f"total={(data or {}).get('total_count')}")

            # S30: spec key discovery lists ストレージ with real coverage
            data, _s, err, _t = await call(session, "list_spec_keys", {})
            keys = {k["key"]: k for k in (data or {}).get("spec_keys", [])}
            ok = not err and keys.get("ストレージ", {}).get("products", 0) >= 300
            check("S30 list_spec_keys discovery", bool(ok),
                  f"storage products={keys.get('ストレージ', {}).get('products')}")

            # S31: spec_filters numeric comparison (smartphone storage >= 256GB)
            import re as _re

            def storage_gb(v: str) -> float:
                m = _re.search(r"(\d+(?:\.\d+)?)\s*(TB|GB)", v or "", _re.I)
                if not m:
                    return 0.0
                return float(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)

            data, _s, err, _t = await call(
                session, "search_products",
                {"query": "スマートフォン", "min_score": 0, "limit": 3,
                 "spec_filters": {"ストレージ": ">=256GB"}},
            )
            prods = (data or {}).get("products", [])
            ok = not err and (data or {}).get("total_count", 0) >= 1 and bool(prods)
            for p in prods:
                detail, _s2, err2, _t2 = await call(
                    session, "get_product", {"asin": p["asin"]}
                )
                if err2 or storage_gb((detail or {}).get("specs", {}).get("ストレージ", "")) < 256:
                    ok = False
            check("S31 spec_filters storage>=256GB", bool(ok),
                  f"total={(data or {}).get('total_count')}")

            # S32: spec fallback into その他 labels (RDT-93T carries its
            # 9.0kg capacity only as "乾燥容量: 9.0kg" inside その他)
            data, _s, err, _t = await call(
                session, "search_products",
                {"query": "衣類乾燥機", "min_score": 0, "limit": 10,
                 "spec_filters": {"容量": ">=9kg"}},
            )
            asins = [p["asin"] for p in (data or {}).get("products", [])]
            ok = not err and "B0CJJ1SPCK" in asins
            check("S32 spec_filters capacity>=9kg via sonota fallback", bool(ok),
                  f"total={(data or {}).get('total_count')} asins={asins}")

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
