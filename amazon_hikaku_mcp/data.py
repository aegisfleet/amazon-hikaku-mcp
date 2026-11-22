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
        record["_title_n"] = nfkc(record["title"])
        record["_brand_n"] = nfkc(record["brand"])
        record["_cat_n"] = nfkc(record["category"])
        products.append(record)
    return products


def build_deals(deal_items: list) -> list[dict]:
    """Normalize deals/index.json (camelCase) to the unified summary shape."""
    deals: list[dict] = []
    for item in deal_items or []:
        asin = str(item.get("asin") or "").upper()
        if not asin:
            continue
        record = {
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
        record["_haystack"] = nfkc(
            " ".join(
                [
                    record["title"],
                    record["brand"],
                    record["category"],
                    record["description"],
                ]
            )
        )
        deals.append(record)
    return deals


def badge_matches(badge: str, sale_type: str | None) -> bool:
    """True when a deal badge names the requested sale type.

    Substring match after NFKC normalization, so "プライム" hits
    プライム会員限定セール and "ブラックフライデー" hits any Black Friday
    variant Amazon labels the deal with.
    """
    if not sale_type:
        return True
    return nfkc(sale_type) in nfkc(badge or "")


def badge_census(deals: list[dict]) -> list[dict]:
    """Distinct deal badges with counts, most common first."""
    from collections import Counter

    counts = Counter(d["deal_badge"] for d in deals if d.get("deal_badge"))
    return [{"badge": b, "count": c} for b, c in counts.most_common()]


# --- search forgiveness: brand aliases and query synonyms ------------------
# Katakana <-> English brand names, resolved in both directions after NFKC
# normalization, so brand="ソニー" finds Sony products and vice versa.
_BRAND_ALIAS_PAIRS = [
    ("Sony", "ソニー"), ("Apple", "アップル"), ("Panasonic", "パナソニック"),
    ("Logicool", "ロジクール"), ("Logitech", "ロジクール"), ("Xiaomi", "シャオミ"),
    ("Samsung", "サムスン"), ("LG", "エルジー"), ("ASUS", "エイスース"),
    ("Acer", "エイサー"), ("Dell", "デル"), ("Lenovo", "レノボ"),
    ("Microsoft", "マイクロソフト"), ("Google", "グーグル"), ("Amazon", "アマゾン"),
    ("Anker", "アンカー"), ("Elecom", "エレコム"), ("Buffalo", "バッファロー"),
    ("Dyson", "ダイソン"), ("Shark", "シャーク"), ("Philips", "フィリップス"),
    ("Hitachi", "日立"), ("Toshiba", "東芝"), ("Sharp", "シャープ"),
    ("Fujifilm", "富士フイルム"), ("Canon", "キヤノン"), ("Nikon", "ニコン"),
    ("Casio", "カシオ"), ("Seiko", "セイコー"), ("Citizen", "シチズン"),
    ("Nike", "ナイキ"), ("Adidas", "アディダス"), ("New Balance", "ニューバランス"),
    ("The North Face", "ノースフェイス"), ("Coleman", "コールマン"),
    ("Snow Peak", "スノーピーク"), ("mont-bell", "モンベル"), ("Bosch", "ボッシュ"),
    ("Makita", "マキタ"), ("Lego", "レゴ"), ("Bandai", "バンダイ"),
    ("Nintendo", "任天堂"), ("Nintendo", "ニンテンドー"), ("Sanrio", "サンリオ"),
    ("MUJI", "無印良品"), ("Iris Ohyama", "アイリスオーヤマ"), ("Yamazen", "山善"),
    ("Nitori", "ニトリ"),
]

BRAND_ALIASES: dict[str, set[str]] = {}
for _a, _b in _BRAND_ALIAS_PAIRS:
    BRAND_ALIASES.setdefault(nfkc(_a), set()).update({nfkc(_a), nfkc(_b)})
    BRAND_ALIASES.setdefault(nfkc(_b), set()).update({nfkc(_a), nfkc(_b)})


def alias_variants(term: str) -> set[str]:
    """All NFKC forms a brand term may appear as (itself plus aliases)."""
    base = nfkc(term)
    return {base} | BRAND_ALIASES.get(base, set())


# Abbreviations / alternate spellings users actually type. A query token in
# a group is satisfied when ANY group member matches (OR within the group,
# AND across tokens).
_SYNONYM_GROUPS_RAW = [
    ["ノイキャン", "ノイズキャンセリング", "ANC", "アクティブノイズキャンセリング"],
    ["USB-C", "Type-C", "USB Type-C", "USBタイプC", "タイプC"],
    ["モババ", "モバイルバッテリー", "モバイルバッテリ"],
    ["ドラレコ", "ドライブレコーダー"],
    ["イヤホン", "イヤフォン"],
    ["ヘッドホン", "ヘッドフォン"],
]
SYNONYM_GROUPS: list[list[str]] = [[nfkc(v) for v in g] for g in _SYNONYM_GROUPS_RAW]
_SYNONYM_INDEX: dict[str, list[str]] = {}
for _group in SYNONYM_GROUPS:
    for _variant in _group:
        _SYNONYM_INDEX[_variant] = _group


def token_variants(token: str) -> list[str]:
    """Synonym variants for one NFKC query token (itself when ungrouped)."""
    return _SYNONYM_INDEX.get(token, [token])


def _variant_matches(variant: str, haystack: str) -> bool:
    """Substring match; ASCII variants need word boundaries so that e.g.
    "anc" does not fire inside "performance"."""
    if variant.isascii():
        pattern = r"(?<![a-z0-9])" + re.escape(variant) + r"(?![a-z0-9])"
        return re.search(pattern, haystack) is not None
    return variant in haystack


def token_matches(token: str, haystack: str) -> bool:
    """True when a query token (or any of its synonyms) appears in text."""
    return any(_variant_matches(v, haystack) for v in token_variants(token))


_RATIONALE_LINE = re.compile(r"^\[(加点|減点):\s*[+\-]?\d+\]\s*(.+)$")


def _trim_outer_parens(text: str) -> str:
    """Remove one outer (...) / （...） pair when it wraps the whole string.

    Inner parentheses are untouched, and a string whose first pair closes
    early (e.g. "(a) and (b)") is returned unchanged.
    """
    t = text.strip()
    if len(t) < 2 or t[0] not in "(（":
        return t
    closer = ")" if t[0] == "(" else "）"
    depth = 0
    for i, ch in enumerate(t):
        if ch in "(（":
            depth += 1
        elif ch in ")）":
            depth -= 1
            if depth == 0 and i < len(t) - 1:
                return t
    if depth == 0 and t[-1] == closer:
        return t[1:-1].strip()
    return t


def parse_rationale(rationale: object) -> tuple[list[str], list[str]]:
    """Split a score_rationale into (pros, cons) from its 加点/減点 lines."""
    pros: list[str] = []
    cons: list[str] = []
    for line in str(rationale or "").splitlines():
        m = _RATIONALE_LINE.match(line.strip())
        if m:
            target = pros if m.group(1) == "加点" else cons
            target.append(_trim_outer_parens(m.group(2)))
    return pros, cons


def _investigated_ts(record: dict) -> float:
    """Epoch seconds of last_investigated (0 when unparseable)."""
    try:
        return datetime.fromisoformat(str(record.get("last_investigated") or "")).timestamp()
    except ValueError:
        return 0.0


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


def deal_summary(deal: dict) -> dict:
    """Summary shape for a deal: the common product summary (with citation /
    score_label / price_label) plus the deal-only fields."""
    summary = product_summary(deal)
    summary["savings_percentage"] = deal["savings_percentage"]
    summary["deal_badge"] = deal["deal_badge"]
    return summary


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
        for d in self.deals:
            d["brand_group"] = self.brand_group_of(d["brand"])
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
        targets = alias_variants(brand)
        group_names = {
            g["name"]
            for g in self.brandgroups
            if targets & {nfkc(g["name"]), nfkc(g["slug"])}
        }

        def matches(record: dict) -> bool:
            if group_names and record.get("brand_group") in group_names:
                return True
            brand_n = nfkc(record["brand"])
            return any(t in brand_n for t in targets)

        return matches

    # -- queries -----------------------------------------------------------
    def search(
        self,
        query: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        exclude_brand: str | None = None,
        exclude_category: str | None = None,
        min_score: int | None = None,
        max_price: int | None = None,
        min_price: int | None = None,
        in_stock_only: bool = True,
        sort: str = "score",
        limit: int = 8,
        offset: int = 0,
        updated_within_days: int | None = None,
    ) -> list[dict]:
        full = self._filter_sorted(
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
            updated_within_days=updated_within_days,
        )
        offset = max(0, offset)
        return full[offset : offset + max(1, min(limit, 20))]

    def search_page(self, *args, limit: int = 8, offset: int = 0, **kwargs) -> dict:
        """Like search(), but also returns the total match count, a has_more
        flag and facets (brands / categories / price range of the full match
        set) so clients can offer concrete refinement options to the user."""
        full = self._filter_sorted(*args, **kwargs)
        offset = max(0, offset)
        items = full[offset : offset + max(1, min(limit, 20))]
        return {
            "items": items,
            "total": len(full),
            "has_more": offset + len(items) < len(full),
            "facets": self.facets_of(full),
        }

    def facets_of(self, items: list[dict]) -> dict:
        from collections import Counter

        brands = Counter(p["brand"] for p in items if p["brand"])
        categories = Counter(p["category"] for p in items if p["category"])
        parent_of: dict[str, str] = {}
        for parent, info in self.categorygroups.items():
            for child in (info or {}).get("categories") or []:
                parent_of[child] = parent
        parents = Counter(
            parent_of[p["category"]] for p in items if p["category"] in parent_of
        )
        prices = [p["price"] for p in items if p["price"]]
        return {
            "brands": [{"brand": b, "count": c} for b, c in brands.most_common(5)],
            "categories": [
                {"category": c, "count": n} for c, n in categories.most_common(5)
            ],
            "category_parents": [
                {"parent": name, "count": n} for name, n in parents.most_common(5)
            ],
            "price_min": min(prices) if prices else None,
            "price_max": max(prices) if prices else None,
            "score_ge_80": sum(1 for p in items if p["score"] >= 80),
        }

    @staticmethod
    def _relevance(p: dict, tokens: list[str]) -> int:
        """Title matches weigh most, then brand/category, then body text."""
        score = 0
        for tok in tokens:
            variants = token_variants(tok)
            if any(_variant_matches(v, p["_title_n"]) for v in variants):
                score += 3
            elif any(
                _variant_matches(v, p["_brand_n"]) or _variant_matches(v, p["_cat_n"])
                for v in variants
            ):
                score += 2
            elif any(_variant_matches(v, p["_haystack"]) for v in variants):
                score += 1
        return score

    def _filter_sorted(
        self,
        query: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        exclude_brand: str | None = None,
        exclude_category: str | None = None,
        min_score: int | None = None,
        max_price: int | None = None,
        min_price: int | None = None,
        in_stock_only: bool = True,
        sort: str = "score",
        updated_within_days: int | None = None,
    ) -> list[dict]:
        tokens = nfkc(query).split() if query else []
        cat_set = self.expand_category(category)
        cat_target = nfkc(category) if category and cat_set is None else None
        excl_set = self.expand_category(exclude_category)
        excl_target = (
            nfkc(exclude_category) if exclude_category and excl_set is None else None
        )
        brand_ok = self.brand_predicate(brand)
        brand_ng = self.brand_predicate(exclude_brand)
        cutoff = (
            time.time() - updated_within_days * 86400
            if updated_within_days
            else None
        )
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
            if min_price is not None and p["price"] < min_price:
                continue
            if cutoff is not None and _investigated_ts(p) < cutoff:
                continue
            if cat_set is not None and p["category"] not in cat_set:
                continue
            if cat_target is not None and cat_target not in nfkc(p["category"]):
                continue
            if excl_set is not None and p["category"] in excl_set:
                continue
            if excl_target is not None and excl_target in nfkc(p["category"]):
                continue
            if brand_ok is not None and not brand_ok(p):
                continue
            if brand_ng is not None and brand_ng(p):
                continue
            if tokens and not all(token_matches(tok, p["_haystack"]) for tok in tokens):
                continue
            results.append(p)
        if sort == "relevance" and tokens:
            results.sort(
                key=lambda p: (-self._relevance(p, tokens), -p["score"], p["price"])
            )
        elif sort == "newest":
            results.sort(key=lambda p: (-_investigated_ts(p), -p["score"]))
        elif sort == "price_asc":
            if tokens:
                # Items matching only in body text (accessories, cases, ...)
                # sink below title/brand/category matches even when cheaper.
                results.sort(
                    key=lambda p: (
                        0 if self._relevance(p, tokens) >= 2 else 1,
                        p["price"],
                        -p["score"],
                    )
                )
            else:
                results.sort(key=lambda p: (p["price"], -p["score"]))
        elif sort == "price_desc":
            if tokens:
                results.sort(
                    key=lambda p: (
                        0 if self._relevance(p, tokens) >= 2 else 1,
                        -p["price"],
                        -p["score"],
                    )
                )
            else:
                results.sort(key=lambda p: (-p["price"], -p["score"]))
        elif tokens:
            # Default score sort: title/brand/category matches form the
            # first bucket so a high-score item that only mentions the
            # keyword in its description cannot outrank real matches.
            results.sort(
                key=lambda p: (
                    0 if self._relevance(p, tokens) >= 2 else 1,
                    -p["score"],
                    p["price"],
                )
            )
        else:
            results.sort(key=lambda p: (-p["score"], p["price"]))
        return results

    def relaxation_suggestions(
        self,
        query: str | None = None,
        category: str | None = None,
        brand: str | None = None,
        exclude_brand: str | None = None,
        exclude_category: str | None = None,
        min_score: int | None = None,
        max_price: int | None = None,
        in_stock_only: bool = True,
    ) -> list[dict]:
        """For a 0/few-result search: which single relaxation would find items?"""
        base = {
            "query": query,
            "category": category,
            "brand": brand,
            "exclude_brand": exclude_brand,
            "exclude_category": exclude_category,
            "min_score": min_score,
            "max_price": max_price,
            "in_stock_only": in_stock_only,
        }
        trials: list[tuple[str, str, dict]] = []
        if min_score:
            trials.append(
                ("min_score", f"最低スコアを{min_score}点から0点に緩める", {"min_score": 0})
            )
        if max_price is not None:
            trials.append(("max_price", "価格上限を外す", {"max_price": None}))
        if brand:
            trials.append(("brand", "ブランド指定を外す", {"brand": None}))
        if category:
            trials.append(("category", "カテゴリ指定を外す", {"category": None}))
        if in_stock_only:
            trials.append(("in_stock_only", "在庫なしの商品も含める", {"in_stock_only": False}))
        suggestions = []
        for key, label, override in trials:
            count = len(self._filter_sorted(**{**base, **override}))
            if count:
                suggestions.append(
                    {"relax": key, "description": label, "estimated_count": count}
                )
        suggestions.sort(key=lambda s: -s["estimated_count"])
        return suggestions[:3]

    def similar_products(
        self,
        asin: str,
        max_price: int | None = None,
        limit: int = 5,
        min_score: int = 0,
    ) -> dict | None:
        """Same-category, in-stock alternatives for a product.

        With an explicit max_price: everything at or under it, score order.
        Without one: candidates in the target's price neighborhood
        (0.5x-1.5x) come first, then the rest, score order within each
        bucket — so a 5,600 yen earbud is not "replaced" by a 27,800 yen one.
        """
        target = self.by_asin.get((asin or "").strip().upper())
        if target is None:
            return None
        candidates = [
            p
            for p in self.products
            if p["asin"] != target["asin"]
            and p["category"] == target["category"]
            and p["in_stock"]
            and p["price"]
            and p["score"] >= min_score
            and (max_price is None or p["price"] <= max_price)
        ]
        total = len(candidates)
        if max_price is None and target["price"]:
            lo, hi = target["price"] * 0.5, target["price"] * 1.5
            candidates.sort(
                key=lambda p: (
                    0 if lo <= p["price"] <= hi else 1,
                    -p["score"],
                    p["price"],
                )
            )
        else:
            candidates.sort(key=lambda p: (-p["score"], p["price"]))
        return {
            "target": target,
            "items": candidates[: max(1, min(limit, 20))],
            "total": total,
        }

    def deal_badge_of(self, asin: str) -> str | None:
        """The live deal badge for an ASIN, or None when it is not on sale."""
        for d in self.deals:
            if d["asin"] == asin and d["in_stock"] and d["deal_badge"]:
                return d["deal_badge"]
        return None

    def deals_query(
        self,
        category: str | None = None,
        min_savings_percentage: int = 0,
        min_score: int = 0,
        sale_type: str | None = None,
        max_price: int | None = None,
        brand: str | None = None,
        query: str | None = None,
        sort: str = "recommended",
        limit: int = 8,
        offset: int = 0,
    ) -> dict:
        """Filtered / sorted / paged deals.

        available_badges is the badge census after every filter EXCEPT
        sale_type (category / brand / query / max_price / min_score /
        min_savings_percentage applied), so its counts never contradict
        the result set: with sale_type=None its counts sum to the number
        of badged deals in total.
        """
        cat_set = self.expand_category(category)
        brand_ok = self.brand_predicate(brand)
        tokens = nfkc(query).split() if query else []

        def in_scope(d: dict) -> bool:
            if cat_set is not None and d["category"] not in cat_set:
                return False
            if cat_set is None and category and nfkc(category) not in nfkc(d["category"]):
                return False
            if max_price is not None and (not d["price"] or d["price"] > max_price):
                return False
            if brand_ok is not None and not brand_ok(d):
                return False
            if tokens and not all(token_matches(t, d["_haystack"]) for t in tokens):
                return False
            return True

        scoped = [d for d in self.deals if d["in_stock"] and in_scope(d)]
        floored = [
            d
            for d in scoped
            if d["savings_percentage"] >= min_savings_percentage
            and d["score"] >= min_score
        ]
        badges = badge_census(floored)
        filtered = [d for d in floored if badge_matches(d["deal_badge"], sale_type)]
        if sort == "savings":
            filtered.sort(key=lambda d: (-d["savings_percentage"], -d["score"]))
        elif sort == "score":
            filtered.sort(key=lambda d: (-d["score"], -d["savings_percentage"]))
        elif sort == "price_asc":
            filtered.sort(key=lambda d: (d["price"], -d["score"]))
        else:  # recommended: score leads, savings supports
            filtered.sort(
                key=lambda d: (-(d["score"] * 10 + d["savings_percentage"]), -d["score"])
            )
        offset = max(0, offset)
        items = filtered[offset : offset + max(1, min(limit, 20))]
        return {
            "items": items,
            "total": len(filtered),
            "has_more": offset + len(items) < len(filtered),
            "available_badges": badges,
        }

    def envelope(self, notes: bool = True, **extra) -> dict:
        """Response envelope. The citation/display notes are only included
        when the response actually contains products (notes=False), so
        metadata-only and empty responses stay small."""
        base = {
            "data_fetched_at": self.fetched_at,
            "data_stale": self.stale,
            "data_source": self.source,
        }
        if notes:
            base["citation_note"] = (
                "回答では各商品の citation（詳細レポートへの Markdown リンク）を"
                "出典として必ずそのまま含めること。リンクのない商品紹介は不完全な回答になる。"
            )
            base["display_note"] = (
                "各商品の紹介には score_label（独自スコア）と price_label（価格）を"
                "必ず併記すること。スコアも価格もない紹介は不完全な回答になる。"
            )
        base.update(extra)
        return base
