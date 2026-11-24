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

データの範囲と鮮度:
- 検索対象は amazon-hikaku.com が調査・蓄積した商品だけであり、Amazon.co.jp の全商品ではない。ヒットしないことは、Amazon にその商品が存在しないことを意味しない。候補が少ないときは、調査データ自体の選択肢が少ない旨も伝えること。
- 価格・在庫は、調査とは別に更新されるデータの更新時点のもの（価格は調査後に変わっていることがある）。スコア・評価・所見は各商品の last_investigated（調査日）時点の内容。data_fetched_at はこのサーバーがサイトのデータを取得した時刻で、価格の更新時刻そのものではない。価格は変動し得るため、最新価格・在庫は詳細ページでの最終確認を案内すること。

利用上の指針:
- 独自スコアの高い商品を優先すること（目安は80点以上）。ただしスコアだけでなく、用途への適合性・価格・仕様も見て選ぶこと。スコアが低い商品を無条件に推薦しない。
- 回答では各商品の citation（詳細レポートへの Markdown リンク）を出典として必ずそのまま示すこと。リンクを省略した商品紹介はしない。Amazon への直接リンクは提供していない。
- 商品を紹介するときは score_label（独自スコア）と price_label（価格）を必ず併記すること。
- 候補は原則5〜8件程度に絞ること。該当が少ない場合は、除外した候補とその理由も添えてよい。

相談の進め方:
- 用途や目的の相談（「お風呂の水垢を落としたい」「犬が飽きずに遊べるおもちゃ」等）は、文章をそのまま query にせず、商品名・用途・カテゴリの短いキーワードに分解して複数回検索し、結果を統合して ASIN で重複を取り除くこと。カテゴリが異なる商品でも目的に合えば候補にしてよい。
- 条件（予算・用途・サイズ・好み）が曖昧な相談では、候補を示したうえで、結果の facets にある実際の選択肢（ブランド・価格帯・カテゴリ）を使って最大3点まで聞き返してよい。回答が得られたら条件を更新して再検索すること。
- 結果が0件〜数件と少ないときは、応答の suggestions（どの条件を緩めると何件出るか）を参考に、最低スコアの引き下げ→関連カテゴリ・同義語での再検索→価格上限の小幅な拡大、の順に段階的に緩めて再検索してよい。
- ただしユーザーが明示した必須条件（予算上限・除外指定など）は勝手に緩和しない。条件を緩和した場合は何をどう緩めたかを必ず説明し、予算を超える候補は予算内の候補と明確に分けて示すこと。
- ブランド名や規格の表記ゆれ（ソニー/Sony、ノイキャン、USB-C/Type-C 等）はサーバー側で吸収するので、ユーザーの言葉のまま検索してよい。特定ブランドを除外したい場合は exclude_brand を使うこと。
- 「トイレ」のような語は、人間の生活用品とペット用品の両方に一致することがある。facets の category_parents で混在を確認し、用途が人間用なら exclude_category="ペット用品"、ペット用なら category="ペット用品" のように、親カテゴリで寄せたり除外したりすること。
- スペック（ストレージ容量・メモリ・画面サイズ・乾燥容量・重量など）で絞り込みたいときは、まず list_spec_keys（カテゴリ指定可）で実在するスペックキーと値の例を確認し、search_products の spec_filters に渡すこと（例: スマホのストレージなら {"ストレージ": ">=256GB"}）。スペックは全商品に付いているわけではなく、キーを持たない商品は spec_filters ではヒットしない。条件に合う商品が少なくても、スペック情報自体が無い商品が存在し得ることは伝えること。
- 目的の商品が在庫切れ・予算超過のときは、find_similar_products で同カテゴリの代替候補を1回で探せる。代替案を示すときは、元の商品との共通点と相違点（用途・価格差・スコア・主要スペック）を説明すること。
- 比較を求められたら、価格・スコア・主要スペックなど同じ評価軸で横並びにすること。
- 「もっと見せて」と言われたら、同じ条件で offset を増やして続きを取得すること（先頭から取り直さない）。

推測の禁止:
- 商品データに無い情報（メーカーの所在地・製造国・原産国・発売日など）は推測で補わない。本データには原産国や発売日の構造化情報がほぼ無いため、それらを条件に指定されても「データからは確認できない」と伝えること。メーカー所在地と製造国と原産国は別の概念であり、混同しない。

プレゼント・ギフトの相談:
- プレゼントの相談では、相手・シーン・予算を早めに確認し、search_products の recipient（ギフトの相手）と occasion（ギフトのシーン）と max_price で絞ること。指定できる値は recipient: 女性 / 男性 / 恋人 / 子供 / 両親 / 祖父母 / 友人 / 上司・同僚 / 自分へのご褒美、occasion: クリスマス / 誕生日 / 母の日 / 父の日 / バレンタイン / ホワイトデー / 結婚祝い / 出産祝い / 新築・引っ越し祝い / 卒業・入学・就職祝い / 敬老の日 / 長寿祝い / 記念日 / お中元 / お歳暮。
- 各商品の gift_label / gift_recipients / gift_occasions は、調査文の用途記述から抽出したギフト属性。付いている商品は根拠のあるギフト候補として優先してよい。ただし属性が無い商品がギフト不向きとは限らない（属性は調査文にギフト文脈があった商品にだけ付く）。件数が少ないときは、属性なしの通常検索の候補も「ギフト属性は付いていないが候補になり得るもの」として分けて示してよい。
- ラッピング・のし・メッセージカードの可否や、クリスマス等の期限までの配送可否はデータに無いため断定しない。「データからは確認できない」と伝え、詳細ページでの確認を案内すること。
- ギフト相談の進め方には gift_consultation プロンプトも利用できる。

セールについて:
- 割引率の高さは品質の高さではない。セール商品でもスコアを併記し、低スコアの商品を「お買い得」と断定しないこと（get_deals は既定でスコア70点未満を除外している）。
- セールの種類（タイムセール／プライム会員限定セール／ブラックフライデー等）は deal_badge で区別される。ユーザーが種別を指定したら get_deals の sale_type で絞ること。該当が0件のときは他のセールで代用せず、available_badges にある現在のセール種別を伝えること。
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
    exclude_brand: str | None = None,
    exclude_category: str | None = None,
    min_score: int = 80,
    max_price: int | None = None,
    min_price: int | None = None,
    in_stock_only: bool = True,
    sort: str = "score",
    limit: int = 8,
    offset: int = 0,
    updated_within_days: int | None = None,
    recipient: str | None = None,
    occasion: str | None = None,
    spec_filters: dict[str, str] | None = None,
) -> dict:
    """商品を検索する。query はキーワード（空白区切りでAND）、category は子カテゴリ名・親カテゴリ名・親スラッグのいずれでも可、brand はブランド名またはブランドグループ名（日英表記ゆれは自動で吸収する）。exclude_brand は除外したいブランド名（表記ゆれは同様に吸収する）。exclude_category は除外したいカテゴリ名（子・親・親スラッグのいずれでも可。親を指定すると配下の子カテゴリをすべて除外する）。

    spec_filters はスペックでの絞り込み（例: {"ストレージ": ">=256GB"}、
    {"容量": ">=9kg"}、{"OS": "Android"}）。キーはスペック項目名（list_spec_keys で
    カテゴリごとの実在キーと値の例を確認できる）、値は部分一致の文字列か、
    >= / <= / > / < / = を付けた数値条件（単位は GB/TB、kg/g、L/mL、インチ/cm/mm、
    mAh 等に対応し、単位換算も行う）。複数キーは AND。そのキーを持たない商品は
    原則ヒットしない（スペック情報が無い商品は条件から漏れる）が、「その他」の
    自由記述内に「乾燥容量: 9.0kg」のようなラベル付き表記がある場合は拾う。

    recipient / occasion はプレゼント検索用: recipient はギフトの相手（女性 / 男性 /
    恋人 / 子供 / 両親 / 祖父母 / 友人 / 上司・同僚 / 自分へのご褒美）、occasion は
    ギフトのシーン（クリスマス / 誕生日 / 母の日 / 父の日 / バレンタイン /
    ホワイトデー / 結婚祝い / 出産祝い / 新築・引っ越し祝い / 卒業・入学・就職祝い /
    敬老の日 / 長寿祝い / 記念日 / お中元 / お歳暮）。調査文から抽出したギフト属性を
    持つ商品だけに絞る（属性の無い商品はヒットしない）。

    sort は score（既定）/ price_asc / price_desc / relevance（タイトル・カテゴリ一致を
    重視する関連度順）/ newest（調査日が新しい順）。offset で続きのページを取得できる。
    updated_within_days を渡すと、直近その日数以内に調査された商品だけに絞る。

    応答には total_count（条件に合う全件数）と facets（全件のブランド・カテゴリ・
    親カテゴリ・価格帯）が含まれる。条件が曖昧な相談では、facets の実際の選択肢を
    使って絞り込みを提案できる。
    結果が3件以下の場合は suggestions（どの条件を緩めると何件出るか）が含まれる。
    """
    await store.ensure_fresh()
    page = store.search_page(
        query=query,
        category=category,
        brand=brand,
        exclude_brand=exclude_brand,
        exclude_category=exclude_category,
        min_score=min_score,
        max_price=max_price,
        min_price=min_price,
        in_stock_only=in_stock_only,
        sort=sort,
        limit=limit,
        offset=offset,
        updated_within_days=updated_within_days,
        recipient=recipient,
        occasion=occasion,
        spec_filters=spec_filters,
    )
    extra: dict = {}
    if page["total"] <= 3:
        extra["suggestions"] = store.relaxation_suggestions(
            query=query,
            category=category,
            brand=brand,
            exclude_brand=exclude_brand,
            exclude_category=exclude_category,
            min_score=min_score,
            max_price=max_price,
            in_stock_only=in_stock_only,
            recipient=recipient,
            occasion=occasion,
            spec_filters=spec_filters,
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
async def list_spec_keys(category: str | None = None) -> dict:
    """スペック絞り込み（search_products の spec_filters）に使えるスペック項目の一覧を返す。

    category を渡すとそのカテゴリ（子・親・親スラッグ可）の範囲で集計する。
    各キーについて、そのキーを持つ商品数と値の例（頻出順）を返す。スペックは
    全商品に付いているわけではないため、絞り込み前にここで実在するキーと
    値の書き方を確認すること。
    """
    await store.ensure_fresh()
    stats = store.spec_key_stats(category)
    return store.envelope(
        notes=False,
        category=stats["category"],
        products_in_scope=stats["products_in_scope"],
        spec_keys=stats["spec_keys"],
        spec_note="spec_filters のキーはここの key をそのまま使う。値を持たない商品は spec_filters の対象にならない。",
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
            "deal_badge": store.deal_badge_of(record["asin"]),
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
    min_score: int = 0,
) -> dict:
    """指定商品と同カテゴリの代替候補を返す。

    目的の商品が在庫切れ・予算超過のときに、探し直さずに代替案を提示するために使う。
    max_price 未指定の場合は、対象商品の価格の0.5〜1.5倍の価格帯にある候補を
    優先し、各価格帯の中ではスコア順に並べる。min_score で候補の最低スコアも
    指定できる。
    """
    await store.ensure_fresh()
    result = store.similar_products(asin, max_price=max_price, limit=limit, min_score=min_score)
    if result is None:
        return store.envelope(notes=False, found=False, asin=asin)
    return store.envelope(
        notes=bool(result["items"]),
        found=True,
        target=product_summary(result["target"]),
        count=len(result["items"]),
        total_count=result["total"],
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
    available_badges には、sale_type 以外の現在の条件に合う範囲で開催中の
    セール種別と件数が入る（結果が0件のときに、存在しないセール種別が
    件数つきで返ることはない）。
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
async def get_recommendations(
    category: str | None = None,
    limit: int = 8,
    recipient: str | None = None,
    occasion: str | None = None,
) -> dict:
    """独自スコア90点以上の高評価おすすめ商品を返す。

    recipient / occasion を渡すと、ギフト属性を持つ商品の中からのおすすめになる
    （指定できる値は search_products と同じ）。
    """
    await store.ensure_fresh()
    page = store.search_page(
        category=category,
        min_score=90,
        sort="score",
        limit=limit,
        recipient=recipient,
        occasion=occasion,
    )
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
MAX_TRACKED_IPS = 4096

SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store"),
]


class _BodyTooLargeError(Exception):
    """Raised when a streaming HTTP request body exceeds MAX_BODY_BYTES."""


def _client_ip(scope: dict, headers: dict[bytes, bytes]) -> str:
    """Resolve client IP, preferring the last X-Forwarded-For hop on Cloud Run."""
    xff = headers.get(b"x-forwarded-for")
    if xff:
        try:
            parts = [p.strip() for p in xff.decode("latin1").split(",") if p.strip()]
            if parts:
                return parts[-1]
        except Exception:
            pass
    client = scope.get("client")
    return client[0] if client else "unknown"


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
        "4. 出典として各商品の citation（詳細レポートへのリンク）を必ずそのまま示し、価格はデータ更新時点の参考価格であることを添える。"
    )


@mcp.prompt()
def gift_consultation(recipient: str = "", occasion: str = "", budget: str = "") -> str:
    """プレゼント選び相談: 相手・シーン・予算からギフト候補を絞る相談テンプレート。"""
    who = recipient or "（相手未指定）"
    scene = occasion or "（シーン未指定）"
    limit = budget or "（予算未指定）"
    return (
        f"プレゼント選びの相談を始めてください。相手: {who} / シーン: {scene} / 予算: {limit}\n"
        "進め方:\n"
        "1. 相手・シーン・予算のうち不足しているものがあれば、最大3点まで質問する（予算帯の目安を示すと答えやすい）。\n"
        "2. search_products に recipient（相手）と occasion（シーン）と max_price（予算）を指定して候補を探し、gift_label の付いた商品を優先して紹介する。\n"
        "3. 件数が少ない場合は recipient / occasion を外した通常検索も行い、「ギフト属性つきの候補」と「属性はないが候補になり得るもの」を分けて示す。\n"
        "4. ラッピング・のし・期限までの配送可否はデータに無いため断定せず、確認できない旨を添える。出典として各商品の citation を必ずそのまま示し、score_label と price_label を併記する。"
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
        "4. 出典として各商品の citation（詳細レポートへのリンク）を必ずそのまま示し、価格はデータ更新時点の参考価格であることを添える。"
    )


class GuardMiddleware:
    """Body-size cap + simple per-IP rate limit + security headers."""

    def __init__(self, app):
        self.app = app
        self._hits: dict[str, deque] = defaultdict(deque)

    def _prune_stale_ips(self, cutoff: float) -> None:
        stale_ips = [ip for ip, q in self._hits.items() if not q or q[-1] < cutoff]
        for ip in stale_ips:
            self._hits.pop(ip, None)
        if len(self._hits) >= MAX_TRACKED_IPS:
            oldest_ip = min(
                self._hits,
                key=lambda k: self._hits[k][-1] if self._hits[k] else 0.0,
            )
            self._hits.pop(oldest_ip, None)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def guarded_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                raw_headers = list(message.get("headers") or [])
                existing = {k.lower() for k, _ in raw_headers}
                for hk, hv in SECURITY_HEADERS:
                    if hk not in existing:
                        raw_headers.append((hk, hv))
                message = {**message, "headers": raw_headers}
            await send(message)

        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) > MAX_BODY_BYTES:
                    response = JSONResponse({"error": "request too large"}, status_code=413)
                    await response(scope, receive, guarded_send)
                    return
            except ValueError:
                response = JSONResponse({"error": "invalid content-length"}, status_code=400)
                await response(scope, receive, guarded_send)
                return

        now = time.time()
        cutoff = now - 60
        ip = _client_ip(scope, headers)
        if ip not in self._hits and len(self._hits) >= MAX_TRACKED_IPS:
            self._prune_stale_ips(cutoff)
        hits = self._hits[ip]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MINUTE:
            response = JSONResponse({"error": "rate limit"}, status_code=429)
            await response(scope, receive, guarded_send)
            return
        hits.append(now)

        received_bytes = 0

        async def guarded_receive():
            nonlocal received_bytes
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"") or b""
                received_bytes += len(body)
                if received_bytes > MAX_BODY_BYTES:
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except _BodyTooLargeError:
            if not response_started:
                response = JSONResponse({"error": "request too large"}, status_code=413)
                await response(scope, receive, guarded_send)



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
