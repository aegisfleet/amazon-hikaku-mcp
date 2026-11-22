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
    assert summary["price_label"] == "5,980円（調査時点の参考価格）"
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
    assert summary["price_label"] == "3,980円（調査時点の参考価格）"
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
