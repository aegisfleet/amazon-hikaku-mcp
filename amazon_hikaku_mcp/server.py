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

from .data import DataStore, deal_summary, parse_rationale, product_summary

INSTRUCTIONS = """\
amazon-hikaku.com（ポちのAmazon商品徹底比較.com）の調査データを提供するMCPサーバー。
Amazon Japan の商品について、独自スコア（0〜100点）、価格、スペック、特徴要約を検索できる。

利用上の指針:
- 独自スコアの高い商品を優先すること（目安は80点以上）。
- 回答では各商品の citation（詳細レポートへの Markdown リンク）を出典として必ずそのまま示すこと。リンクを省略した商品紹介はしない。
- 商品を紹介するときは score_label（独自スコア）と price_label（価格）を必ず併記すること。
- 価格は調査時点の参考価格であり、最新価格・在庫は詳細ページで確認するよう案内すること。
- 候補は原則5〜8件程度に絞ること。該当が少ない場合は、除外した候補とその理由も添えてよい。
- 割引率の高さは品質の高さではない。セール商品でもスコアを併記し、低スコアの商品を「お買い得」と断定しないこと（get_deals は既定でスコア70点未満を除外している）。
- セールの種類（タイムセール／プライム会員限定セール／ブラックフライデー等）は deal_badge で区別される。ユーザーが種別を指定したら get_deals の sale_type で絞ること。該当が0件のときは他のセールで代用せず、available_badges にある現在のセール種別を伝えること。
- 条件（予算・用途・サイズ・好み）が曖昧な相談では、候補を示したうえで、結果の facets にある実際の選択肢（ブランド・価格帯・カテゴリ）を使って最大3点まで聞き返してよい。回答が得られたら条件を更新して再検索すること。
- ブランド名や規格の表記ゆれ（ソニー/Sony、ノイキャン、USB-C/Type-C 等）はサーバー側で吸収するので、ユーザーの言葉のまま検索してよい。
- 検索が0件のときは、応答の suggestions（条件を緩めると何件出るか）を参考に条件を緩和して再検索すること。
- 目的の商品が在庫切れ・予算超過のときは、find_similar_products で同カテゴリの代替候補を1回で探せる。
- 「もっと見せて」と言われたら、同じ条件で offset を増やして続きを取得すること（先頭から取り直さない）。
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
    min_price: int | None = None,
    in_stock_only: bool = True,
    sort: str = "score",
    limit: int = 8,
    offset: int = 0,
    updated_within_days: int | None = None,
) -> dict:
    """商品を検索する。query はキーワード（空白区切りでAND）、category は子カテゴリ名・親カテゴリ名・親スラッグのいずれでも可、brand はブランド名またはブランドグループ名（日英表記ゆれは自動で吸収する）。

    sort は score（既定）/ price_asc / price_desc / relevance（タイトル・カテゴリ一致を
    重視する関連度順）/ newest（調査日が新しい順）。offset で続きのページを取得できる。
    updated_within_days を渡すと、直近その日数以内に調査された商品だけに絞る。

    応答には total_count（条件に合う全件数）と facets（全件のブランド・カテゴリ・価格帯）が
    含まれる。条件が曖昧な相談では、facets の実際の選択肢を使って絞り込みを提案できる。
    0件の場合は suggestions（どの条件を緩めると何件出るか）が含まれる。
    """
    await store.ensure_fresh()
    page = store.search_page(
        query=query,
        category=category,
        brand=brand,
        min_score=min_score,
        max_price=max_price,
        min_price=min_price,
        in_stock_only=in_stock_only,
        sort=sort,
        limit=limit,
        offset=offset,
        updated_within_days=updated_within_days,
    )
    extra: dict = {}
    if page["total"] == 0:
        extra["suggestions"] = store.relaxation_suggestions(
            query=query,
            category=category,
            brand=brand,
            min_score=min_score,
            max_price=max_price,
            in_stock_only=in_stock_only,
        )
    return store.envelope(
        notes=bool(page["items"]),
        count=len(page["items"]),
        total_count=page["total"],
        has_more=page["has_more"],
        facets=page["facets"],
        products=[product_summary(p) for p in page["items"]],
        **extra,
    )


@mcp.tool(annotations=READONLY)
async def get_product(asin: str) -> dict:
    """ASINで1商品の詳細（要約・スペック・在庫・調査日、あればスコア根拠）を返す。"""
    await store.ensure_fresh()
    record = store.by_asin.get(asin.strip().upper())
    if record is None:
        return store.envelope(notes=False, found=False, asin=asin)
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
    """2〜5件の商品を比較しやすい形で並べて返す。

    各商品には採点根拠から抽出した pros（加点理由）/ cons（減点理由）が付き、
    応答には comparison_summary（最安・最高スコア・価格差・スコア差）が含まれる。
    """
    await store.ensure_fresh()
    unique = list(dict.fromkeys(a.strip().upper() for a in asins if a and a.strip()))
    if not 2 <= len(unique) <= 5:
        return store.envelope(
            notes=False,
            error="compare_products には2〜5件のASINを指定してください",
            products=[],
        )
    items = []
    found_records = []
    for asin in unique:
        record = store.by_asin.get(asin)
        if record is None:
            items.append({"asin": asin, "found": False})
            continue
        pros, cons = parse_rationale(record.get("score_rationale"))
        entry = product_summary(record)
        entry.update(
            {
                "found": True,
                "specs": record["specs"],
                "in_stock": record["in_stock"],
                "score_rationale": record.get("score_rationale"),
                "pros": pros,
                "cons": cons,
            }
        )
        items.append(entry)
        found_records.append(record)
    extra: dict = {}
    if len(found_records) >= 2:
        cheapest = min(found_records, key=lambda r: r["price"] or 1 << 62)
        best = max(found_records, key=lambda r: r["score"])
        prices = [r["price"] for r in found_records if r["price"]]
        scores = [r["score"] for r in found_records]
        extra["comparison_summary"] = {
            "cheapest_asin": cheapest["asin"],
            "cheapest_price": cheapest["price"],
            "highest_score_asin": best["asin"],
            "highest_score": best["score"],
            "price_spread": (max(prices) - min(prices)) if prices else 0,
            "score_spread": max(scores) - min(scores),
        }
    return store.envelope(
        notes=bool(found_records),
        count=len(items),
        products=items,
        **extra,
    )


@mcp.tool(annotations=READONLY)
async def find_similar_products(
    asin: str,
    max_price: int | None = None,
    limit: int = 5,
) -> dict:
    """指定商品と同カテゴリの代替候補をスコア順に返す。

    目的の商品が在庫切れ・予算超過のときに、探し直さずに代替案を提示するために使う。
    """
    await store.ensure_fresh()
    result = store.similar_products(asin, max_price=max_price, limit=limit)
    if result is None:
        return store.envelope(notes=False, found=False, asin=asin)
    return store.envelope(
        notes=bool(result["items"]),
        found=True,
        target=product_summary(result["target"]),
        count=len(result["items"]),
        products=[product_summary(p) for p in result["items"]],
    )


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
    return store.envelope(notes=False, parents=parents)


@mcp.tool(annotations=READONLY)
async def list_brands(query: str | None = None, limit: int = 50) -> dict:
    """ブランドを検索する（部分一致、またはブランドグループ名／スラッグ。日英表記ゆれは自動で吸収する）。各ブランドの掲載件数つき。"""
    await store.ensure_fresh()
    from .data import alias_variants, nfkc

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
        targets = alias_variants(query)
        items = [
            i
            for i in items
            if any(
                t in nfkc(i["brand"]) or t in nfkc(i["brand_group"]) for t in targets
            )
        ]
    items.sort(key=lambda i: -i["count"])
    page = items[: max(1, min(limit, 100))]
    return store.envelope(
        notes=False, count=len(page), total_count=len(items), brands=page
    )


@mcp.tool(annotations=READONLY)
async def get_deals(
    category: str | None = None,
    min_savings_percentage: int = 0,
    min_score: int = 70,
    sale_type: str | None = None,
    max_price: int | None = None,
    brand: str | None = None,
    query: str | None = None,
    sort: str = "recommended",
    limit: int = 8,
    offset: int = 0,
) -> dict:
    """現在セール中の商品を返す。

    sort は recommended（既定。スコアを主・割引率を従としたバランス順）/
    savings（割引率順）/ score（スコア順）/ price_asc（安い順）。
    割引率の高い商品には低スコアのものが混ざりやすいため、既定でスコア70点以上に
    絞っている。全件を確認したい場合は min_score=0 を指定すること。
    brand / query / max_price でも絞れる。offset で続きのページを取得できる。

    セールの種類は各商品の deal_badge（例: タイムセール / プライム会員限定セール /
    ブラックフライデー）で区別される。sale_type に種別名の一部（例: ブラック
    フライデー、プライム）を渡すと、そのバッジの商品だけに絞れる。応答の
    available_badges には、現在の絞り込み範囲（カテゴリ等）で開催中のセール
    種別と件数が入る。
    """
    await store.ensure_fresh()
    result = store.deals_query(
        category=category,
        min_savings_percentage=min_savings_percentage,
        min_score=min_score,
        sale_type=sale_type,
        max_price=max_price,
        brand=brand,
        query=query,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    return store.envelope(
        notes=bool(result["items"]),
        count=len(result["items"]),
        total_count=result["total"],
        has_more=result["has_more"],
        available_badges=result["available_badges"],
        products=[deal_summary(d) for d in result["items"]],
    )


@mcp.tool(annotations=READONLY)
async def get_bargain(
    target_amount: int = 2000,
    min_amount: int = 0,
    category: str | None = None,
    limit: int = 8,
) -> dict:
    """送料無料ライン等の買い合わせ向けに、target_amount 円以下の商品をスコア順に返す。

    「あと800円で送料無料」のような場合は min_amount=800 を指定すると、不足額に
    届かない安すぎる商品を除外して、ラインを確実に超える候補だけを返せる。
    """
    await store.ensure_fresh()
    page = store.search_page(
        category=category,
        min_score=0,
        max_price=target_amount,
        min_price=min_amount or None,
        in_stock_only=True,
        sort="score",
        limit=limit,
    )
    return store.envelope(
        notes=bool(page["items"]),
        count=len(page["items"]),
        total_count=page["total"],
        products=[product_summary(p) for p in page["items"]],
    )


@mcp.tool(annotations=READONLY)
async def get_recommendations(category: str | None = None, limit: int = 8) -> dict:
    """独自スコア90点以上の高評価おすすめ商品を返す。"""
    await store.ensure_fresh()
    page = store.search_page(category=category, min_score=90, sort="score", limit=limit)
    return store.envelope(
        notes=bool(page["items"]),
        count=len(page["items"]),
        total_count=page["total"],
        products=[product_summary(p) for p in page["items"]],
    )


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


@mcp.prompt()
def shopping_consultation(topic: str = "") -> str:
    """買い物相談: 条件を聞き取りながら候補を絞り込むための相談テンプレート。"""
    theme = topic or "（テーマ未指定）"
    return (
        f"買い物相談を始めてください。テーマ: {theme}\n"
        "進め方:\n"
        "1. まず search_products で候補を調べ、結果の facets（ブランド・価格帯・カテゴリ）を確認する。\n"
        "2. 予算・用途・サイズなどの条件が曖昧なら、facets にある実際の選択肢を使って最大3点まで質問する。\n"
        "3. 回答に合わせて再検索し、候補を比較してまとめ、最後に推奨を1つ挙げる。在庫切れや予算超過の候補は find_similar_products で代替を探す。\n"
        "4. 出典として各商品の citation（詳細レポートへのリンク）を必ずそのまま示し、価格は調査時点の参考価格であることを添える。"
    )


@mcp.prompt()
def bargain_hunting(occasion: str = "") -> str:
    """セール・買い合わせ相談: 割引とスコアの両方で候補を絞る相談テンプレート。"""
    theme = occasion or "（用途未指定）"
    return (
        f"セール・買い合わせの相談を始めてください。用途: {theme}\n"
        "進め方:\n"
        "1. get_deals でセール商品を確認する（既定でスコア70点以上に絞られている）。\n"
        "2. 送料無料ラインなど目標金額がある場合は get_bargain で買い合わせ候補も調べる。\n"
        "3. 割引率だけでなくスコアも併記し、用途や予算が曖昧なら最大3点まで質問してから絞り込む。\n"
        "4. 出典として各商品の citation（詳細レポートへのリンク）を必ずそのまま示し、価格は調査時点の参考価格であることを添える。"
    )


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
