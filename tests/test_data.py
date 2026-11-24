"""Offline unit tests for the data layer (no network)."""

import asyncio

from amazon_hikaku_mcp.data import DataStore, build_deals, build_products, nfkc

INDEX = [
    {
        "asin": "B000TEST01",
        "title": "テスト ワイヤレスイヤホン",
        "categories": ["ワイヤレスイヤホン"],
        "summary": "テスト用イヤホン",
        "price_value": 5980,
        "score": 92,
        "image": "https://example.com/i.jpg",
        "in_stock": True,
        "availability": "在庫あり。",
        "last_investigated": "2026-11-22T00:00:00+09:00",
        "specs": "driver:11mm",
        "specs_json": {"ドライバー": "11mm"},
        "permalink": "/b000test01/",
    },
    {
        "asin": "B000TEST02",
        "title": "格安ケーブル",
        "categories": ["USBケーブル"],
        "summary": "買い合わせ用",
        "price_value": 480,
        "score": 70,
        "image": "",
        "in_stock": True,
        "availability": "在庫あり。",
        "last_investigated": "2026-11-22T00:00:00+09:00",
        "specs_json": "{}",
        "permalink": "/b000test02/",
    },
]
SUMMARY = [
    {"asin": "B000TEST01", "brand": "Anker", "category": "ワイヤレスイヤホン", "price": 5980, "score": 92},
    {"asin": "B000TEST02", "brand": "エレコム", "category": "USBケーブル", "price": 480, "score": 70},
]
DEALS = [
    {
        "asin": "B000TEST01",
        "title": "テスト ワイヤレスイヤホン",
        "brand": "Anker",
        "category": "ワイヤレスイヤホン",
        "priceRaw": 3980,
        "score": 92,
        "inStock": True,
        "savingsPercentage": 33,
        "dealBadge": "タイムセール",
        "lastInvestigated": "2026-11-22T00:00:00+09:00",
    }
]


def make_store() -> DataStore:
    store = DataStore()
    store.products = build_products(INDEX, SUMMARY, None)
    store.by_asin = {p["asin"]: p for p in store.products}
    store.deals = build_deals(DEALS)
    store.categorygroups = {
        "オーディオ": {"slug": "audio", "categories": ["ワイヤレスイヤホン"]},
        "PC周辺": {"slug": "pc", "categories": ["USBケーブル"]},
    }
    store.brandgroups = [
        {"name": "Anker", "slug": "anker", "pattern": __import__("re").compile("^Anker$")},
    ]
    for p in store.products:
        p["brand_group"] = store.brand_group_of(p["brand"])
    return store


def test_nfkc():
    assert nfkc("Ｗｉｒｅｌｅｓｓ") == "wireless"
    assert nfkc("イヤホン") == "イヤホン"


def test_product_summary_has_ready_made_citation():
    from amazon_hikaku_mcp.data import product_summary

    store = make_store()
    summary = product_summary(store.by_asin["B000TEST01"])
    assert summary["citation"] == "[詳細レポート](https://www.amazon-hikaku.com/b000test01/)"
    assert summary["detail_url"].endswith("/b000test01/")
    assert summary["score_label"] == "独自スコア 92点"
    assert summary["price_label"] == "参考価格 5,980円（データ更新時点）"
    env = store.envelope(products=[summary])
    assert "citation" in env["citation_note"]
    assert "score_label" in env["display_note"]


def test_brand_joined_from_summary():
    store = make_store()
    assert store.by_asin["B000TEST01"]["brand"] == "Anker"


def test_search_fullwidth_query_and_filters():
    store = make_store()
    hits = store.search(query="ワイヤレス", min_score=80)
    assert [h["asin"] for h in hits] == ["B000TEST01"]
    assert store.search(min_score=95) == []


def test_category_expansion_by_parent_and_slug():
    store = make_store()
    assert [h["asin"] for h in store.search(category="オーディオ", min_score=0)] == ["B000TEST01"]
    assert [h["asin"] for h in store.search(category="pc", min_score=0)] == ["B000TEST02"]


def test_brand_group_filter():
    store = make_store()
    assert [h["asin"] for h in store.search(brand="anker", min_score=0)] == ["B000TEST01"]


def test_search_page_total_and_facets():
    store = make_store()
    page = store.search_page(min_score=0)
    assert page["total"] == 2
    assert len(page["items"]) == 2
    facets = page["facets"]
    assert facets["price_min"] == 480
    assert facets["price_max"] == 5980
    assert facets["score_ge_80"] == 1
    assert {b["brand"] for b in facets["brands"]} == {"Anker", "エレコム"}
    limited = store.search_page(min_score=0, limit=1)
    assert limited["total"] == 2
    assert len(limited["items"]) == 1


def test_bargain_price_cap():
    store = make_store()
    hits = store.search(max_price=2000, min_score=0)
    assert [h["asin"] for h in hits] == ["B000TEST02"]


def test_badge_matching_and_census():
    from amazon_hikaku_mcp.data import badge_census, badge_matches

    assert badge_matches("プライム会員限定セール", "プライム")
    assert badge_matches("ブラックフライデーセール", "ブラックフライデー")
    assert not badge_matches("タイムセール", "ブラックフライデー")
    assert badge_matches("タイムセール", None)
    census = badge_census(build_deals(DEALS))
    assert census == [{"badge": "タイムセール", "count": 1}]


def test_deals_normalized_snake_case():
    deals = build_deals(DEALS)
    assert deals[0]["savings_percentage"] == 33
    assert deals[0]["price"] == 3980
    assert deals[0]["detail_url"].endswith("/b000test01/")


def test_ensure_fresh_no_network_when_loaded():
    store = make_store()
    store._loaded_at = __import__("time").time()
    asyncio.run(store.ensure_fresh())  # must not raise / fetch


# --- improvement regression tests (2026-11-22 third-party review) -----------

import re as _re

from amazon_hikaku_mcp.data import (
    deal_summary,
    parse_rationale,
    token_matches,
)

INDEX2 = [
    {
        "asin": "B000SONY01", "title": "SONY ワイヤレスイヤホン WF-1000X",
        "categories": ["ワイヤレスイヤホン"],
        "summary": "ノイズキャンセリング対応のフラッグシップ",
        "price_value": 30000, "score": 95, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000POW001", "title": "大容量モバイルバッテリー 20000mAh",
        "categories": ["モバイルバッテリー"], "summary": "スマホ充電用",
        "price_value": 5000, "score": 88, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000POUCH1", "title": "ガジェットポーチ",
        "categories": ["ガジェットポーチ"],
        "summary": "モバイルバッテリーを収納できるポーチ",
        "price_value": 500, "score": 75, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000CHG001", "title": "Type-C 充電器 65W",
        "categories": ["充電器"], "summary": "USB PD対応",
        "price_value": 4000, "score": 90, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
]
SUMMARY2 = [
    {"asin": "B000SONY01", "brand": "SONY", "category": "ワイヤレスイヤホン", "price": 30000, "score": 95},
    {"asin": "B000POW001", "brand": "Anker", "category": "モバイルバッテリー", "price": 5000, "score": 88},
    {"asin": "B000POUCH1", "brand": "無印良品", "category": "ガジェットポーチ", "price": 500, "score": 75},
    {"asin": "B000CHG001", "brand": "CIO", "category": "充電器", "price": 4000, "score": 90},
]


def make_store2() -> DataStore:
    store = DataStore()
    store.products = build_products(INDEX2, SUMMARY2, None)
    store.by_asin = {p["asin"]: p for p in store.products}
    store.deals = []
    store.categorygroups = {}
    store.brandgroups = [
        {"name": "Sony", "slug": "sony", "pattern": _re.compile(r"SONY|ソニー")},
        {"name": "Anker", "slug": "anker", "pattern": _re.compile(r"^Anker$")},
    ]
    for p in store.products:
        p["brand_group"] = store.brand_group_of(p["brand"])
    return store


def test_deal_summary_has_labels_and_citation():
    store = make_store()
    summary = deal_summary(store.deals[0])
    assert summary["citation"].endswith("/b000test01/)")
    assert summary["score_label"] == "独自スコア 92点"
    assert summary["price_label"] == "参考価格 3,980円（データ更新時点）"
    assert summary["savings_percentage"] == 33
    assert summary["deal_badge"] == "タイムセール"


def test_brand_alias_katakana_finds_english_brand():
    store = make_store2()
    hits = store.search(brand="ソニー", min_score=0)
    assert [h["asin"] for h in hits] == ["B000SONY01"]
    hits = store.search(brand="Sony", min_score=0)
    assert [h["asin"] for h in hits] == ["B000SONY01"]


def test_synonym_expansion_noise_cancelling():
    store = make_store2()
    hits = store.search(query="ノイキャン", min_score=0)
    assert [h["asin"] for h in hits] == ["B000SONY01"]


def test_synonym_expansion_usb_c_type_c():
    store = make_store2()
    hits = store.search(query="USB-C 充電器", min_score=0)
    assert [h["asin"] for h in hits] == ["B000CHG001"]


def test_anc_does_not_match_inside_english_words():
    assert not token_matches("ノイキャン", "high performance model")
    assert token_matches("ノイキャン", "anc チップ搭載")


def test_relevance_sort_and_price_bucket():
    store = make_store2()
    hits = store.search(query="モバイルバッテリー", min_score=0, sort="relevance")
    assert hits[0]["asin"] == "B000POW001"
    # price_asc must not let the cheap description-only pouch jump ahead
    hits = store.search(query="モバイルバッテリー", min_score=0, sort="price_asc")
    assert hits[0]["asin"] == "B000POW001"


def test_min_price_and_pagination():
    store = make_store2()
    hits = store.search(min_score=0, min_price=1000)
    assert {h["asin"] for h in hits} == {"B000SONY01", "B000POW001", "B000CHG001"}
    page1 = store.search_page(min_score=0, limit=2, offset=0)
    page2 = store.search_page(min_score=0, limit=2, offset=2)
    assert page1["total"] == 4 and page1["has_more"]
    assert not page2["has_more"]
    assert {p["asin"] for p in page1["items"]}.isdisjoint(
        {p["asin"] for p in page2["items"]}
    )


def test_parse_rationale_pros_cons():
    pros, cons = parse_rationale(
        "[基本点: 70]\n[加点: +5] 安全性: 承認取得済み\n[減点: -3] 価格: やや高い\n[合計: 97]"
    )
    assert pros == ["安全性: 承認取得済み"]
    assert cons == ["価格: やや高い"]


def test_similar_products_same_category():
    store = make_store2()
    result = store.similar_products("B000POW001")
    assert result is not None
    assert result["items"] == []  # only one product in that category
    result = store.similar_products("B000SONY01", max_price=100)
    assert result["items"] == []
    assert store.similar_products("B000UNKNOWN") is None


def test_relaxation_suggestions():
    store = make_store2()
    suggestions = store.relaxation_suggestions(min_score=99)
    assert suggestions and suggestions[0]["relax"] == "min_score"
    assert suggestions[0]["estimated_count"] == 4


def test_envelope_notes_can_be_omitted():
    store = make_store()
    assert "citation_note" in store.envelope(products=[])
    bare = store.envelope(notes=False, parents=[])
    assert "citation_note" not in bare and "display_note" not in bare


DEALS2 = DEALS + [
    {
        "asin": "B000WATCH1", "title": "格安スマートウォッチ", "brand": "TRAUSI",
        "category": "スマートウォッチ", "priceRaw": 1999, "score": 72,
        "inStock": True, "savingsPercentage": 93, "dealBadge": "プライム会員限定セール",
        "lastInvestigated": "2026-11-22T00:00:00+09:00",
    }
]


def test_deals_query_recommended_sort_and_scoped_badges():
    store = make_store()
    store.deals = build_deals(DEALS2)
    result = store.deals_query(min_score=0, sort="recommended")
    # score-led balance: 92pt/33% (953) beats 72pt/93% (813)
    assert result["items"][0]["asin"] == "B000TEST01"
    result = store.deals_query(min_score=0, sort="savings")
    assert result["items"][0]["asin"] == "B000WATCH1"
    scoped = store.deals_query(category="スマートウォッチ", min_score=0)
    assert scoped["available_badges"] == [{"badge": "プライム会員限定セール", "count": 1}]
    assert scoped["total"] == 1
    paged = store.deals_query(min_score=0, limit=1, offset=1)
    assert paged["total"] == 2 and not paged["has_more"]
    assert paged["items"][0]["asin"] != store.deals_query(min_score=0, limit=1)["items"][0]["asin"]


# --- follow-up fixes F-1..F-5 (2026-11-22 re-verification) ------------------

INDEX3 = [
    {
        "asin": "B000TGT001", "title": "ワイヤレスイヤホン 対象商品",
        "categories": ["ワイヤレスイヤホン"], "summary": "対象",
        "price_value": 5600, "score": 95, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000NEAR01", "title": "ワイヤレスイヤホン 近接A",
        "categories": ["ワイヤレスイヤホン"], "summary": "近接",
        "price_value": 3778, "score": 90, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000NEAR02", "title": "ワイヤレスイヤホン 近接B",
        "categories": ["ワイヤレスイヤホン"], "summary": "近接",
        "price_value": 5990, "score": 88, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000FAR001", "title": "ワイヤレスイヤホン 高級",
        "categories": ["ワイヤレスイヤホン"], "summary": "高級",
        "price_value": 27800, "score": 99, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000MOBA01", "title": "モバイルバッテリー 本体",
        "categories": ["モバイルバッテリー"], "summary": "本体",
        "price_value": 4000, "score": 80, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
    {
        "asin": "B000SEAT01", "title": "ベビーカーシート",
        "categories": ["ベビーカー"], "summary": "モバイルバッテリーが付属するシート",
        "price_value": 12608, "score": 99, "image": "", "in_stock": True,
        "availability": "在庫あり。", "last_investigated": "2026-11-20T00:00:00+09:00",
        "specs_json": {},
    },
]
SUMMARY3 = [
    {"asin": a, "brand": "TestBrand", "category": c, "price": p, "score": s}
    for a, c, p, s in [
        ("B000TGT001", "ワイヤレスイヤホン", 5600, 95),
        ("B000NEAR01", "ワイヤレスイヤホン", 3778, 90),
        ("B000NEAR02", "ワイヤレスイヤホン", 5990, 88),
        ("B000FAR001", "ワイヤレスイヤホン", 27800, 99),
        ("B000MOBA01", "モバイルバッテリー", 4000, 80),
        ("B000SEAT01", "ベビーカー", 12608, 99),
    ]
]


def make_store3() -> DataStore:
    store = DataStore()
    store.products = build_products(INDEX3, SUMMARY3, None)
    store.by_asin = {p["asin"]: p for p in store.products}
    store.deals = []
    store.categorygroups = {}
    store.brandgroups = []
    for p in store.products:
        p["brand_group"] = ""
    return store


def test_f1_badges_respect_score_floor():
    store = make_store()
    store.deals = build_deals(DEALS2)
    result = store.deals_query(min_score=85, sale_type="プライム")
    assert result["total"] == 0
    # the 72-point Prime deal is below the floor, so its badge must not appear
    assert result["available_badges"] == [{"badge": "タイムセール", "count": 1}]
    everything = store.deals_query(min_score=0)
    assert sum(b["count"] for b in everything["available_badges"]) == everything["total"] == 2


def test_f2_score_sort_buckets_title_matches_first():
    store = make_store3()
    hits = store.search(query="モババ", min_score=0)  # default sort="score"
    assert hits[0]["asin"] == "B000MOBA01"  # 80pt title match
    assert hits[1]["asin"] == "B000SEAT01"  # 99pt description-only match


def test_f3_similar_products_price_neighborhood_and_total():
    store = make_store3()
    result = store.similar_products("B000TGT001", limit=5)
    assert result["total"] == 3
    assert [p["asin"] for p in result["items"]] == ["B000NEAR01", "B000NEAR02", "B000FAR001"]
    capped = store.similar_products("B000TGT001", max_price=6000)
    assert capped["total"] == 2
    assert [p["asin"] for p in capped["items"]] == ["B000NEAR01", "B000NEAR02"]


def test_f4_parse_rationale_trims_outer_parens():
    pros, cons = parse_rationale(
        "[加点: +10] (3000円台でLDAC、ANC対応)\n"
        "[加点: +5] （静音性が高い）\n"
        "[減点: -3] AIアダプティブANC (最大-52dB) の制約\n"
        "[減点: -2] (a) と (b) の両方"
    )
    assert pros == ["3000円台でLDAC、ANC対応", "静音性が高い"]
    assert cons == ["AIアダプティブANC (最大-52dB) の制約", "(a) と (b) の両方"]


def test_f5_deal_badge_of():
    store = make_store()
    assert store.deal_badge_of("B000TEST01") == "タイムセール"
    assert store.deal_badge_of("B000TEST02") is None


# --- ChatGPT spec round (2026-11-22): exclude_brand / min_score -------------

def test_exclude_brand_with_alias():
    store = make_store2()
    all_hits = store.search(min_score=0)
    assert "B000SONY01" in {h["asin"] for h in all_hits}
    for term in ("ソニー", "Sony", "SONY"):
        hits = store.search(min_score=0, exclude_brand=term)
        assert "B000SONY01" not in {h["asin"] for h in hits}
        assert hits  # other brands remain


def test_similar_products_min_score():
    store = make_store3()
    result = store.similar_products("B000TGT001", min_score=92)
    assert result["total"] == 1
    assert [p["asin"] for p in result["items"]] == ["B000FAR001"]  # only the 99pt one


# --- GuardMiddleware security tests -----------------------------------------

def test_guard_middleware_security_headers_and_body_cap():
    from starlette.responses import PlainTextResponse
    from amazon_hikaku_mcp.server import GuardMiddleware, MAX_BODY_BYTES, MAX_TRACKED_IPS

    async def dummy_app(scope, receive, send):
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"") or b""
            if not msg.get("more_body", False):
                break
        resp = PlainTextResponse(f"ok:{len(body)}")
        await resp(scope, receive, send)

    guarded = GuardMiddleware(dummy_app)

    async def call_asgi(headers=None, body=b""):
        sent = []
        called = False

        async def receive():
            nonlocal called
            if not called:
                called = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)

        scope = {
            "type": "http",
            "method": "POST" if body else "GET",
            "path": "/mcp",
            "headers": headers or [],
            "client": ("192.0.2.55", 12345),
        }
        await guarded(scope, receive, send)
        start = next(m for m in sent if m["type"] == "http.response.start")
        hdrs = {k.decode("latin1").lower(): v.decode("latin1") for k, v in start.get("headers", [])}
        return start["status"], hdrs

    # Normal request succeeds and includes security headers + X-Forwarded-For IP resolution
    status, hdrs = asyncio.run(
        call_asgi(headers=[(b"x-forwarded-for", b"203.0.113.1, 198.51.100.2")])
    )
    assert status == 200
    assert hdrs.get("x-content-type-options") == "nosniff"
    assert hdrs.get("x-frame-options") == "DENY"
    assert hdrs.get("referrer-policy") == "no-referrer"
    assert hdrs.get("cache-control") == "no-store"
    assert "198.51.100.2" in guarded._hits

    # Oversized Content-Length rejected with 413
    status_cl, hdrs_cl = asyncio.run(
        call_asgi(headers=[(b"content-length", str(MAX_BODY_BYTES + 1).encode())])
    )
    assert status_cl == 413
    assert hdrs_cl.get("x-content-type-options") == "nosniff"

    # Invalid Content-Length rejected with 400
    status_bad, _ = asyncio.run(
        call_asgi(headers=[(b"content-length", b"not-a-number")])
    )
    assert status_bad == 400

    # Streaming body exceeding MAX_BODY_BYTES without Content-Length header
    status_stream, hdrs_stream = asyncio.run(
        call_asgi(headers=[], body=b"a" * (MAX_BODY_BYTES + 10))
    )
    assert status_stream == 413
    assert hdrs_stream.get("x-content-type-options") == "nosniff"

    # Bounded IP table pruning
    for i in range(MAX_TRACKED_IPS + 20):
        guarded._hits[f"10.0.{i // 256}.{i % 256}"].append(0.0)
    guarded._prune_stale_ips(cutoff=100.0)
    assert len(guarded._hits) <= MAX_TRACKED_IPS




# --- pet vs human category split (2026-11-23) --------------------------------

def _store3_with_groups():
    store = make_store3()
    store.categorygroups = {
        "家電": {"slug": "electronics", "categories": ["ワイヤレスイヤホン"]},
        "日用品": {"slug": "daily", "categories": ["モバイルバッテリー", "ベビーカー"]},
    }
    return store


def test_exclude_category_by_parent_child_and_slug():
    store = _store3_with_groups()
    hits = store.search(min_score=0, exclude_category="家電")  # parent name
    assert {h["asin"] for h in hits} == {"B000MOBA01", "B000SEAT01"}
    hits = store.search(min_score=0, exclude_category="electronics")  # parent slug
    assert {h["asin"] for h in hits} == {"B000MOBA01", "B000SEAT01"}
    hits = store.search(min_score=0, exclude_category="ベビーカー")  # child name
    assert "B000SEAT01" not in {h["asin"] for h in hits}
    assert len(hits) == 5


def test_facets_include_category_parents():
    store = _store3_with_groups()
    page = store.search_page(min_score=0)
    parents = {f["parent"]: f["count"] for f in page["facets"]["category_parents"]}
    assert parents == {"家電": 4, "日用品": 2}


# --- gift attributes (2026-11-24) ---------------------------------------------

MCP_GIFT = [
    {
        "asin": "B000TEST01",
        "brand": "Anker",
        "score_rationale": "[基本点: 70]",
        "gift_recipients": ["partner", "female"],
        "gift_occasions": ["christmas"],
    },
    {
        "asin": "B000TEST02",
        "brand": "エレコム",
        "score_rationale": None,
    },
]


def make_gift_store() -> DataStore:
    store = DataStore()
    store.products = build_products(INDEX, SUMMARY, MCP_GIFT)
    store.by_asin = {p["asin"]: p for p in store.products}
    store.deals = build_deals(DEALS)
    return store


def test_gift_key_normalization():
    from amazon_hikaku_mcp.data import (
        GIFT_OCCASION_LABELS,
        GIFT_RECIPIENT_ALIASES,
        GIFT_RECIPIENT_LABELS,
        gift_key,
    )

    assert gift_key("クリスマス", GIFT_OCCASION_LABELS) == "christmas"
    assert gift_key("クリスマスプレゼント", GIFT_OCCASION_LABELS) == "christmas"
    assert gift_key("christmas", GIFT_OCCASION_LABELS) == "christmas"
    assert gift_key("母の日", GIFT_OCCASION_LABELS) == "mothers_day"
    assert gift_key("彼女", GIFT_RECIPIENT_LABELS, GIFT_RECIPIENT_ALIASES) == "partner"
    assert gift_key("両親", GIFT_RECIPIENT_LABELS, GIFT_RECIPIENT_ALIASES) == "parents"
    assert gift_key("合格祝い", GIFT_OCCASION_LABELS) is None
    assert gift_key("", GIFT_OCCASION_LABELS) is None
    assert gift_key(None, GIFT_OCCASION_LABELS) is None


def test_gift_attributes_reach_summary():
    from amazon_hikaku_mcp.data import product_summary

    store = make_gift_store()
    summary = product_summary(store.by_asin["B000TEST01"])
    assert summary["gift_occasions"] == ["クリスマス"]
    assert summary["gift_recipients"] == ["恋人", "女性"]
    assert "ギフト候補" in summary["gift_label"]
    plain = product_summary(store.by_asin["B000TEST02"])
    assert "gift_label" not in plain


def test_search_by_recipient_and_occasion():
    store = make_gift_store()
    hits = store.search(min_score=0, recipient="恋人")
    assert {h["asin"] for h in hits} == {"B000TEST01"}
    hits = store.search(min_score=0, occasion="クリスマス")
    assert {h["asin"] for h in hits} == {"B000TEST01"}
    hits = store.search(min_score=0, recipient="両親")
    assert hits == []
    hits = store.search(min_score=0, occasion="合格祝い")  # unknown value
    assert hits == []
    page = store.search_page(min_score=0, occasion="クリスマス")
    assert page["total"] == 1
    assert page["facets"]["gift_tagged"] == 1


def test_relaxation_suggestions_offer_dropping_gift_filters():
    store = make_gift_store()
    suggestions = store.relaxation_suggestions(min_score=0, recipient="両親")
    keys = {s["relax"] for s in suggestions}
    assert "recipient" in keys


def test_spec_value_matches():
    from amazon_hikaku_mcp.data import spec_value_matches

    assert spec_value_matches("128GB", ">=128GB")
    assert spec_value_matches("1TB", ">=512GB")  # unit conversion within family
    assert not spec_value_matches("128GB", ">=256GB")
    assert spec_value_matches("4570mAh", ">=4000mAh")
    assert not spec_value_matches("5.8インチ", ">=6インチ")
    assert spec_value_matches("6.1インチ", ">=6")  # unit-less compares raw numbers
    assert spec_value_matches("177g", "<200g")
    assert spec_value_matches("約50g", "<=50g")
    assert spec_value_matches("6.0kg", ">=5kg")
    assert spec_value_matches("500mL", ">=0.5L")
    assert spec_value_matches("Android 15", "Android")  # plain substring
    assert not spec_value_matches("iOS 18", "Android")


def test_search_spec_filters():
    store = make_store()
    hits = store.search(spec_filters={"ドライバー": "11mm"}, min_score=0)
    assert [p["asin"] for p in hits] == ["B000TEST01"]
    assert len(store.search(spec_filters={"ドライバー": ">=10mm"}, min_score=0)) == 1
    assert store.search(spec_filters={"ドライバー": ">=12mm"}, min_score=0) == []
    # products without the key never match
    assert store.search(spec_filters={"ストレージ": ">=128GB"}, min_score=0) == []


def test_spec_key_stats():
    store = make_store()
    stats = store.spec_key_stats()
    keys = {k["key"]: k for k in stats["spec_keys"]}
    assert keys["ドライバー"]["products"] == 1
    assert keys["ドライバー"]["sample_values"] == ["11mm"]
    scoped = store.spec_key_stats("ワイヤレスイヤホン")
    assert scoped["products_in_scope"] == 1


def test_relaxation_suggests_dropping_spec_filter():
    store = make_store()
    suggestions = store.relaxation_suggestions(
        spec_filters={"ドライバー": ">=12mm"}, min_score=0
    )
    assert any(
        s["relax"] == "spec:ドライバー" and s["estimated_count"] == 2
        for s in suggestions
    )


def test_search_spec_filter_falls_back_to_sonota_labels():
    index = [
        {
            "asin": "B000TEST09",
            "title": "ガス衣類乾燥機 9kg",
            "categories": ["衣類乾燥機"],
            "summary": "その他フォールバック検証用",
            "price_value": 150000,
            "score": 85,
            "image": "",
            "in_stock": True,
            "availability": "在庫あり。",
            "last_investigated": "2026-11-22T00:00:00+09:00",
            "specs_json": {"その他": "乾燥容量: 9.0kg / 対応ガス種: 都市ガス（13A）"},
            "permalink": "/b000test09/",
        }
    ]
    summary = [
        {"asin": "B000TEST09", "brand": "リンナイ", "category": "衣類乾燥機", "price": 150000, "score": 85}
    ]
    store = DataStore()
    store.products = build_products(index, summary, None)
    store.by_asin = {p["asin"]: p for p in store.products}
    assert len(store.search(spec_filters={"容量": ">=9kg"}, min_score=0)) == 1
    assert store.search(spec_filters={"容量": ">=10kg"}, min_score=0) == []


def _store_with_titles(pairs: list[tuple[str, str]]) -> DataStore:
    index = []
    summary = []
    for i, (title, category) in enumerate(pairs):
        asin = f"B000MORPH{i}"
        index.append(
            {
                "asin": asin,
                "title": title,
                "categories": [category],
                "summary": "",
                "price_value": 10000,
                "score": 85,
                "image": "",
                "in_stock": True,
                "availability": "在庫あり。",
                "last_investigated": "2026-11-22T00:00:00+09:00",
                "specs_json": "{}",
                "permalink": f"/{asin.lower()}/",
            }
        )
        summary.append(
            {"asin": asin, "brand": "テスト", "category": category, "price": 10000, "score": 85}
        )
    store = DataStore()
    store.products = build_products(index, summary, None)
    store.by_asin = {p["asin"]: p for p in store.products}
    return store


def test_morpheme_matching_compound_variants():
    store = _store_with_titles(
        [
            ("ハイアール 衣類ケア乾燥機 FUWATO 9.0kg JZ-K90A(W)", "衣類乾燥機"),  # B000MORPH0
            ("リンナイ ガス衣類乾燥機 乾太くん 9kg", "衣類乾燥機"),  # B000MORPH1
            ("シャープ 洗濯乾燥機 11kg", "洗濯機"),  # B000MORPH2 (washer-dryer: must NOT match)
            ("レイコップ ふとん乾燥機 RFK-300", "布団乾燥機"),  # B000MORPH3
            ("シャープ 衣類乾燥除湿機 CV-S71-W", "衣類乾燥除湿機"),  # B000MORPH4 (dehumidifier: must NOT match)
        ]
    )
    hits = {p["asin"] for p in store.search(query="衣類乾燥機", min_score=0)}
    assert hits == {"B000MORPH0", "B000MORPH1"}  # 衣類ケア乾燥機 matches; 洗濯乾燥機・除湿機 do not
    hits = {p["asin"] for p in store.search(query="布団乾燥機", min_score=0)}
    assert hits == {"B000MORPH3"}  # notation normalized: 布団 -> ふとん
    hits = {p["asin"] for p in store.search(query="JZ-K90A", min_score=0)}
    assert hits == {"B000MORPH0"}  # model numbers still match via substring
