"""Data layer for the amazon-hikaku MCP server.

Fetches the site's public JSON files, normalizes them into one unified
product shape, and serves in-memory searches. Refresh is TTL based with
single-flight locking and stale-on-error fallback (see DESIGN.md §6).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import httpx

BASE = "https://www.amazon-hikaku.com"
URLS = {
    "index": f"{BASE}/index.json",
    "summary": f"{BASE}/products-summary.json",
    "deals": f"{BASE}/deals/index.json",
    "categorygroups": f"{BASE}/data/categorygroups.json",
    "brandgroups": f"{BASE}/data/brandgroups.json",
    # Optional site-generated MCP dataset (brand + score_rationale included).
    # Absent until the site ships it; the server falls back to index+summary.
    "mcp_products": f"{BASE}/mcp-products.json",
}
TTL_SECONDS = 3600


def nfkc(value: object) -> str:
    """NFKC-normalize and lowercase for forgiving Japanese text matching."""
    return unicodedata.normalize("NFKC", str(value or "")).lower()


def _int(value: object) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0


def _parse_jsonish(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip().startswith("{"):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def build_products(index_items: list, summary_items: list, mcp_items: list | None) -> list[dict]:
    """Merge the three product sources into unified records keyed by ASIN.

    index.json is the richest source but has no brand; products-summary.json
    supplies brand; the optional mcp-products.json supplies brand and
    score_rationale and wins on any field it provides.
    """
    summary_by_asin = {}
    for item in summary_items or []:
        asin = str(item.get("asin") or "").upper()
        if asin:
            summary_by_asin[asin] = item

    mcp_by_asin = {}
    for item in mcp_items or []:
        asin = str(item.get("asin") or "").upper()
        if asin:
            mcp_by_asin[asin] = item

    products: list[dict] = []
    for item in index_items or []:
        asin = str(item.get("asin") or "").upper()
        if not asin:
            continue
        summ = summary_by_asin.get(asin, {})
        extra = mcp_by_asin.get(asin, {})
        categories = item.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]
        specs = _parse_jsonish(item.get("specs_json"))
        brand = extra.get("brand") or summ.get("brand") or ""
        record = {
            "asin": asin,
            "title": extra.get("title") or item.get("title") or summ.get("title") or "",
            "brand": brand,
            "category": (categories[0] if categories else "") or summ.get("category") or "",
            "categories": categories,
            "price": _int(item.get("price_value")) or _int(summ.get("price")),
            "score": _int(item.get("score")) or _int(summ.get("score")),
            "description": item.get("summary") or summ.get("description") or "",
            "image": item.get("image") or summ.get("image") or "",
            "detail_url": f"{BASE}/{asin.lower()}/",
            "in_stock": bool(item.get("in_stock")),
            "availability": item.get("availability") or "",
            "last_investigated": item.get("last_investigated") or "",
            "savings_percentage": item.get("savings_percentage"),
            "parent_asin": item.get("parent_asin") or "",
            "specs": specs,
            "score_rationale": extra.get("score_rationale"),
        }
        record["_haystack"] = nfkc(
            " ".join(
                [
                    record["title"],
                    record["brand"],
                    record["category"],
                    " ".join(record["categories"]),
                    record["description"],
                    str(item.get("specs") or ""),
                ]
            )
        )
        products.append(record)
    return products


def build_deals(deal_items: list) -> list[dict]:
    """Normalize deals/index.json (camelCase) to the unified summary shape."""
    deals: list[dict] = []
    for item in deal_items or []:
        asin = str(item.get("asin") or "").upper()
        if not asin:
            continue
        deals.append(
            {
                "asin": asin,
                "title": item.get("title") or "",
                "brand": item.get("brand") or "",
                "category": item.get("category") or "",
                "price": _int(item.get("priceRaw")),
                "score": _int(item.get("score")),
                "description": item.get("description") or "",
                "image": item.get("image") or "",
                "detail_url": f"{BASE}/{asin.lower()}/",
                "in_stock": bool(item.get("inStock")),
                "last_investigated": item.get("lastInvestigated") or "",
                "savings_percentage": _int(item.get("savingsPercentage")),
                "deal_badge": item.get("dealBadge") or "",
            }
        )
    return deals


def product_summary(record: dict) -> dict:
    return {
        "asin": record["asin"],
        "title": record["title"],
        "brand": record["brand"],
        "category": record["category"],
        "price": record["price"],
        "score": record["score"],
        "score_label": f"独自スコア {record['score']}点",
        "price_label": f"{record['price']:,}円（調査時点の参考価格）" if record["price"] else "価格未取得",
        "description": record["description"],
        "in_stock": record["in_stock"],
        "image": record["image"],
        "detail_url": record["detail_url"],
        "citation": f"[詳細レポート]({record['detail_url']})",
        "last_investigated": record["last_investigated"],
    }


class DataStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._loaded_at = 0.0
        self._attempted_at = 0.0
        self.products: list[dict] = []
        self.by_asin: dict[str, dict] = {}
        self.deals: list[dict] = []
        self.categorygroups: dict = {}
        self.brandgroups: list[dict] = []  # {name, slug, pattern}
        self.stale = False
        self.source = "index+summary"
        self.fetched_at: str | None = None

    # -- loading ---------------------------------------------------------
    async def _fetch(self, client: httpx.AsyncClient, key: str, optional: bool = False):
        try:
            resp = await client.get(URLS[key])
            if optional and resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception:
            if optional:
                return None
            raise

    def _client(self) -> httpx.AsyncClient:
        # Do not use trust_env: some environments ship NO_PROXY entries that
        # httpx cannot parse. Honor an explicit HTTPS proxy when one is set
        # (e.g. a corporate egress proxy); Cloud Run sets none and goes direct.
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        kwargs: dict = {"timeout": 60.0, "follow_redirects": True, "trust_env": False}
        if proxy:
            kwargs["proxy"] = proxy
        ca_bundle = os.environ.get("SSL_CERT_FILE")
        if ca_bundle:
            kwargs["verify"] = ca_bundle
        return httpx.AsyncClient(**kwargs)

    async def _load(self) -> None:
        async with self._client() as client:
            index, summary, deals, catgroups, brandgroups, mcp_products = await asyncio.gather(
                self._fetch(client, "index"),
                self._fetch(client, "summary"),
                self._fetch(client, "deals"),
                self._fetch(client, "categorygroups"),
                self._fetch(client, "brandgroups"),
                self._fetch(client, "mcp_products", optional=True),
            )
        products = build_products(index, summary, mcp_products)
        self.products = products
        self.by_asin = {p["asin"]: p for p in products}
        self.deals = build_deals(deals)
        self.categorygroups = catgroups or {}
        self.brandgroups = []
        for name, info in (brandgroups or {}).items():
            matcher = (info or {}).get("matcher") or {}
            pattern = matcher.get("value") or ""
            try:
                compiled = re.compile(pattern) if pattern else None
            except re.error:
                compiled = None
            self.brandgroups.append(
                {"name": name, "slug": (info or {}).get("slug") or "", "pattern": compiled}
            )
        # Precompute each product's brand group for filter/list use.
        for p in self.products:
            p["brand_group"] = self.brand_group_of(p["brand"])
        self.source = "mcp-products" if mcp_products else "index+summary"
        self._loaded_at = time.time()
        self.fetched_at = datetime.now(timezone.utc).isoformat()

    async def ensure_fresh(self) -> None:
        now = time.time()
        if self.products and now - self._loaded_at < TTL_SECONDS:
            return
        if now - self._attempted_at < 60 and self.products:
            return  # recent failed attempt; serve stale rather than hammering
        async with self._lock:
            if self.products and time.time() - self._loaded_at < TTL_SECONDS:
                return
            self._attempted_at = time.time()
            try:
                await self._load()
                self.stale = False
            except Exception:
                if not self.products:
                    raise
                self.stale = True

    # -- taxonomy helpers --------------------------------------------------
    def brand_group_of(self, brand: str) -> str:
        for group in self.brandgroups:
            if group["pattern"] is not None and group["pattern"].search(brand or ""):
                return group["name"]
        return ""

    def expand_category(self, category: str | None) -> set[str] | None:
        """Resolve a category argument to a set of child category names.

        Accepts a child name, a parent name, or a parent slug. Returns None
        when the argument matches nothing structured (caller falls back to
        substring matching).
        """
        if not category:
            return None
        target = nfkc(category)
        for parent, info in self.categorygroups.items():
            children = (info or {}).get("categories") or []
            if target in (nfkc(parent), nfkc((info or {}).get("slug"))):
                return set(children)
            for child in children:
                if target == nfkc(child):
                    return {child}
        return None

    def brand_predicate(self, brand: str | None):
        if not brand:
            return None
        target = nfkc(brand)
        group_names = {
            g["name"]
            for g in self.brandgroups
            if target in (nfkc(g["name"]), nfkc(g["slug"]))
        }

        def matches(record: dict) -> bool:
            if group_names and record.get("brand_group") in group_names:
                return True
            return target in nfkc(record["brand"])

        return matches

    # -- queries -----------------------------------------------------------
    def search(
        self,
        query: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        min_score: int | None = None,
        max_price: int | None = None,
        in_stock_only: bool = True,
        sort: str = "score",
        limit: int = 8,
    ) -> list[dict]:
        full = self._filter_sorted(
            query=query,
            category=category,
            brand=brand,
            min_score=min_score,
            max_price=max_price,
            in_stock_only=in_stock_only,
            sort=sort,
        )
        return full[: max(1, min(limit, 20))]

    def search_page(self, *args, limit: int = 8, **kwargs) -> dict:
        """Like search(), but also returns the total match count and facets
        (brands / categories / price range of the full match set) so clients
        can offer concrete refinement options to the user."""
        full = self._filter_sorted(*args, **kwargs)
        return {
            "items": full[: max(1, min(limit, 20))],
            "total": len(full),
            "facets": self.facets_of(full),
        }

    @staticmethod
    def facets_of(items: list[dict]) -> dict:
        from collections import Counter

        brands = Counter(p["brand"] for p in items if p["brand"])
        categories = Counter(p["category"] for p in items if p["category"])
        prices = [p["price"] for p in items if p["price"]]
        return {
            "brands": [{"brand": b, "count": c} for b, c in brands.most_common(5)],
            "categories": [
                {"category": c, "count": n} for c, n in categories.most_common(5)
            ],
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "score_ge_80": sum(1 for p in items if p["score"] >= 80),
        }

    def _filter_sorted(
        self,
        query: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        min_score: int | None = None,
        max_price: int | None = None,
        in_stock_only: bool = True,
        sort: str = "score",
    ) -> list[dict]:
        tokens = nfkc(query).split() if query else []
        cat_set = self.expand_category(category)
        cat_target = nfkc(category) if category and cat_set is None else None
        brand_ok = self.brand_predicate(brand)
        results = []
        for p in self.products:
            if in_stock_only and not p["in_stock"]:
                continue
            if not p["price"]:
                continue  # price-less entries are unusable for recommendations
            if min_score is not None and p["score"] < min_score:
                continue
            if max_price is not None and p["price"] > max_price:
                continue
            if cat_set is not None and p["category"] not in cat_set:
                continue
            if cat_target is not None and cat_target not in nfkc(p["category"]):
                continue
            if brand_ok is not None and not brand_ok(p):
                continue
            if tokens and not all(tok in p["_haystack"] for tok in tokens):
                continue
            results.append(p)
        if sort == "price_asc":
            results.sort(key=lambda p: (p["price"], -p["score"]))
        elif sort == "price_desc":
            results.sort(key=lambda p: (-p["price"], -p["score"]))
        else:
            results.sort(key=lambda p: (-p["score"], p["price"]))
        return results

    def envelope(self, **extra) -> dict:
        base = {
            "data_fetched_at": self.fetched_at,
            "data_stale": self.stale,
            "data_source": self.source,
            "citation_note": (
                "回答では各商品の citation（詳細レポートへの Markdown リンク）を"
                "出典として必ずそのまま含めること。リンクのない商品紹介は不完全な回答になる。"
            ),
            "display_note": (
                "各商品の紹介には score_label（独自スコア）と price_label（価格）を"
                "必ず併記すること。スコアも価格もない紹介は不完全な回答になる。"
            ),
        }
        base.update(extra)
        return base
