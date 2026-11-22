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


def test_bargain_price_cap():
    store = make_store()
    hits = store.search(max_price=2000, min_score=0)
    assert [h["asin"] for h in hits] == ["B000TEST02"]


def test_deals_normalized_snake_case():
    deals = build_deals(DEALS)
    assert deals[0]["savings_percentage"] == 33
    assert deals[0]["price"] == 3980
    assert deals[0]["detail_url"].endswith("/b000test01/")


def test_ensure_fresh_no_network_when_loaded():
    store = make_store()
    store._loaded_at = __import__("time").time()
    asyncio.run(store.ensure_fresh())  # must not raise / fetch
