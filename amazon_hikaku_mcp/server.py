"""amazon-hikaku remote MCP server (Streamable HTTP, stateless).

Run locally:  uv run amazon-hikaku-mcp
Endpoint:     POST /mcp   (health: GET / and GET /health)
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .data import DataStore, product_summary

INSTRUCTIONS = """\
amazon-hikaku.com（ポちのAmazon商品徹底比較.com）の調査データを提供するMCPサーバー。
Amazon Japan の商品について、独自スコア（0〜100点）、価格、スペック、特徴要約を検索できる。

利用上の指針:
- 独自スコアの高い商品を優先すること（目安は80点以上）。
- 回答では各商品の detail_url（当サイトの詳細レポート）を出典として必ず示すこと。
- 価格は調査時点の参考価格であり、最新価格・在庫は詳細ページで確認するよう案内すること。
- 候補は原則5〜8件程度に絞ること。
"""

READONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)

store = DataStore()

mcp = FastMCP(
    "amazon-hikaku",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
)


@mcp.tool(annotations=READONLY)
async def search_products(
    query: str | None = None,
    category: str | None = None,
    brand: str | None = None,
    min_score: int = 80,
    max_price: int | None = None,
    in_stock_only: bool = True,
    sort: str = "score",
    limit: int = 8,
) -> dict:
    """商品を検索する。query はキーワード（空白区切りでAND）、category は子カテゴリ名・親カテゴリ名・親スラッグのいずれでも可、brand はブランド名またはブランドグループ名。sort は score / price_asc / price_desc。"""
    await store.ensure_fresh()
    found = store.search(
        query=query,
        category=category,
        brand=brand,
        min_score=min_score,
        max_price=max_price,
        in_stock_only=in_stock_only,
        sort=sort,
        limit=limit,
    )
    return store.envelope(count=len(found), products=[product_summary(p) for p in found])


@mcp.tool(annotations=READONLY)
async def get_product(asin: str) -> dict:
    """ASINで1商品の詳細（要約・スペック・在庫・調査日、あればスコア根拠）を返す。"""
    await store.ensure_fresh()
    record = store.by_asin.get(asin.strip().upper())
    if record is None:
        return store.envelope(found=False, asin=asin)
    detail = product_summary(record)
    detail.update(
        {
            "found": True,
            "categories": record["categories"],
            "specs": record["specs"],
            "in_stock": record["in_stock"],
            "availability": record["availability"],
            "savings_percentage": record["savings_percentage"],
            "parent_asin": record["parent_asin"],
            "score_rationale": record["score_rationale"],
        }
    )
    return store.envelope(**detail)


@mcp.tool(annotations=READONLY)
async def compare_products(asins: list[str]) -> dict:
    """2〜5件の商品を比較しやすい形で並べて返す。"""
    await store.ensure_fresh()
    items = []
    for asin in asins[:5]:
        record = store.by_asin.get(asin.strip().upper())
        if record is None:
            items.append({"asin": asin, "found": False})
            continue
        entry = product_summary(record)
        entry.update(
            {
                "found": True,
                "specs": record["specs"],
                "in_stock": record["in_stock"],
                "score_rationale": record.get("score_rationale"),
            }
        )
        items.append(entry)
    return store.envelope(products=items)


@mcp.tool(annotations=READONLY)
async def list_categories() -> dict:
    """親カテゴリ→子カテゴリの階層と、各子カテゴリの掲載件数を返す。"""
    await store.ensure_fresh()
    counts: dict[str, int] = defaultdict(int)
    for p in store.products:
        counts[p["category"]] += 1
    parents = []
    for parent, info in store.categorygroups.items():
        children = [
            {"name": child, "count": counts.get(child, 0)}
            for child in (info or {}).get("categories") or []
        ]
        parents.append(
            {
                "name": parent,
                "slug": (info or {}).get("slug") or "",
                "product_count": sum(c["count"] for c in children),
                "children": children,
            }
        )
    return store.envelope(parents=parents)


@mcp.tool(annotations=READONLY)
async def list_brands(query: str | None = None, limit: int = 50) -> dict:
    """ブランドを検索する（部分一致、またはブランドグループ名／スラッグ）。各ブランドの掲載件数つき。"""
    await store.ensure_fresh()
    from .data import nfkc

    counts: dict[str, int] = defaultdict(int)
    groups: dict[str, str] = {}
    for p in store.products:
        if p["brand"]:
            counts[p["brand"]] += 1
            groups[p["brand"]] = p.get("brand_group", "")
    items = [
        {"brand": brand, "count": count, "brand_group": groups.get(brand, "")}
        for brand, count in counts.items()
    ]
    if query:
        target = nfkc(query)
        items = [
            i
            for i in items
            if target in nfkc(i["brand"]) or target in nfkc(i["brand_group"])
        ]
    items.sort(key=lambda i: -i["count"])
    return store.envelope(count=len(items), brands=items[: max(1, min(limit, 100))])


@mcp.tool(annotations=READONLY)
async def get_deals(
    category: str | None = None,
    min_savings_percentage: int = 0,
    limit: int = 8,
) -> dict:
    """現在セール中の商品を割引率の高い順に返す。"""
    await store.ensure_fresh()
    cat_set = store.expand_category(category)
    deals = [
        d
        for d in store.deals
        if d["in_stock"] and d["savings_percentage"] >= min_savings_percentage
    ]
    if cat_set is not None:
        deals = [d for d in deals if d["category"] in cat_set]
    elif category:
        from .data import nfkc

        deals = [d for d in deals if nfkc(category) in nfkc(d["category"])]
    deals.sort(key=lambda d: (-d["savings_percentage"], -d["score"]))
    deals = deals[: max(1, min(limit, 20))]
    return store.envelope(count=len(deals), products=deals)


@mcp.tool(annotations=READONLY)
async def get_bargain(
    target_amount: int = 2000,
    category: str | None = None,
    limit: int = 8,
) -> dict:
    """送料無料ライン等の買い合わせ向けに、target_amount 円以下の商品をスコア順に返す。"""
    await store.ensure_fresh()
    found = store.search(
        category=category,
        min_score=0,
        max_price=target_amount,
        in_stock_only=True,
        sort="score",
        limit=limit,
    )
    return store.envelope(count=len(found), products=[product_summary(p) for p in found])


@mcp.tool(annotations=READONLY)
async def get_recommendations(category: str | None = None, limit: int = 8) -> dict:
    """独自スコア90点以上の高評価おすすめ商品を返す。"""
    await store.ensure_fresh()
    found = store.search(category=category, min_score=90, sort="score", limit=limit)
    return store.envelope(count=len(found), products=[product_summary(p) for p in found])


@mcp.custom_route("/", methods=["GET"])
async def root(_: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "amazon-hikaku MCP server. Connect an MCP client to /mcp . "
        "Data: https://www.amazon-hikaku.com/llms.txt"
    )


# NOTE: "/healthz" is intercepted by Google's frontend on Cloud Run and
# never reaches the container, so the health endpoint lives at "/health".
@mcp.custom_route("/health", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "products": len(store.products),
            "deals": len(store.deals),
            "data_stale": store.stale,
            "data_fetched_at": store.fetched_at,
        }
    )


# -- abuse guards ---------------------------------------------------------
MAX_BODY_BYTES = 64 * 1024
RATE_LIMIT_PER_MINUTE = 240


class GuardMiddleware:
    """Body-size cap + simple per-IP rate limit (per instance, best effort)."""

    def __init__(self, app):
        self.app = app
        self._hits: dict[str, deque] = defaultdict(deque)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            length = headers.get(b"content-length")
            if length is not None:
                try:
                    if int(length) > MAX_BODY_BYTES:
                        response = JSONResponse({"error": "request too large"}, status_code=413)
                        await response(scope, receive, send)
                        return
                except ValueError:
                    pass
            client = scope.get("client")
            ip = client[0] if client else "unknown"
            now = time.time()
            hits = self._hits[ip]
            while hits and hits[0] < now - 60:
                hits.popleft()
            if len(hits) >= RATE_LIMIT_PER_MINUTE:
                response = JSONResponse({"error": "rate limit"}, status_code=429)
                await response(scope, receive, send)
                return
            hits.append(now)
        await self.app(scope, receive, send)


def build_app():
    # Data loads lazily on the first tool call (single-flight in DataStore);
    # no startup warm-up so the container answers health checks immediately.
    app = mcp.streamable_http_app()
    app.add_middleware(GuardMiddleware)
    return app


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
