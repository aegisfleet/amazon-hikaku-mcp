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
from datetime import datetime, timedelta, timezone

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
    # Optional slim search index over the extended catalog (incl.
    # uninvestigated products). Powers the search_products fallback.
    "catalog_index": f"{BASE}/catalog-index.json",
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


# -- extended catalog (uninvestigated products) ------------------------------
# The site also publishes basic data for products it has cached but not
# investigated, sharded by the ASIN's last two characters under
# /catalog-data/. These records carry price/brand/image only — no score,
# no review, and no product page; the JS catalog page /catalog/?asin= is
# their citation target.

CATALOG_TTL_SECONDS = 3600
CATALOG_MISS_TTL_SECONDS = 60

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_ASIN_IN_URL_RE = re.compile(
    r"(?:/dp/|/gp/product/|/asin/|/product/)([A-Z0-9]{10})", re.IGNORECASE
)


def extract_asin(value: object) -> str | None:
    """Pull an ASIN out of a bare ASIN or an Amazon product URL."""
    text = str(value or "").strip()
    if not text:
        return None
    if _ASIN_RE.match(text.upper()):
        return text.upper()
    m = _ASIN_IN_URL_RE.search(text)
    if m:
        return m.group(1).upper()
    return None


def catalog_shard_key(asin: str) -> str:
    return asin[-2:].upper()


def _price_date_label(iso: object) -> str:
    """'2026-11-26T01:24:32.726Z' -> '2026年11月26日' (JST date)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.astimezone(timezone(timedelta(hours=9))).strftime("%Y年%m月%d日")


PRICE_STALE_DAYS = 30


def _price_stale(iso: object, days: int = PRICE_STALE_DAYS) -> bool:
    """True when a catalog price snapshot is older than `days` days.
    Unparseable or missing timestamps are not flagged (the date label
    already shows the user what is known)."""
    if not iso:
        return False
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt > timedelta(days=days)


def normalize_catalog_record(raw: dict) -> dict:
    """Shape one catalog-data record for a get_price response.

    Field-by-field on purpose: the raw record's detail_page_url is an
    affiliate-tagged Amazon URL and must never reach MCP responses.
    """
    asin = str(raw.get("asin") or "").upper()
    price = raw.get("price")
    price = _int(price) if price is not None else None
    if price == 0:
        price = None
    price_as_of = raw.get("price_as_of")
    catalog_url = f"{BASE}/catalog/?asin={asin}"
    out: dict = {
        "found": True,
        "investigated": False,
        "asin": asin,
        "title": raw.get("title") or "",
        "brand": raw.get("brand") or "",
        "price": price,
        "availability": raw.get("availability") or "",
        "image": raw.get("image") or "",
        "amazon_category": raw.get("amazon_category") or "",
        "release_date": raw.get("release_date"),
        "score": None,
        "price_as_of": price_as_of,
        "catalog_url": catalog_url,
        "citation": f"[商品情報（未調査）]({catalog_url})",
        "note": (
            "この商品は amazon-hikaku.com の調査対象外です。スコア・評価はありません。"
            "価格は price_as_of 時点の参考値で、最新の価格は Amazon で確認してください。"
        ),
    }
    if price is not None:
        date_label = _price_date_label(price_as_of)
        out["price_label"] = f"参考価格 {price:,}円" + (
            f"（{date_label}時点）" if date_label else "（データ更新時点）"
        )
    if _price_stale(price_as_of):
        out["price_stale"] = True
    if raw.get("previous_price") is not None:
        out["previous_price"] = _int(raw["previous_price"])
    if raw.get("price_changed_at"):
        out["price_changed_at"] = raw["price_changed_at"]
    return out


# -- spec filtering ---------------------------------------------------------
# specs values are short free-form strings ("128GB", "4570mAh", "177g",
# "5.8インチ"). A spec_filters condition is either a plain substring or a
# numeric comparison (">=256GB", "<200g"). Numeric compares convert within
# a unit family (storage / weight / volume / length) so "1TB" satisfies
# ">=512GB"; a unit-less condition compares raw numbers against every
# number found in the value.

_UNIT_FAMILIES: list[dict[str, float]] = [
    {"tb": 1024.0 * 1024, "gb": 1024.0, "mb": 1.0},
    {"kg": 1000.0, "g": 1.0},
    {"l": 1000.0, "ml": 1.0, "cc": 1.0, "リットル": 1000.0},
    {"インチ": 25.4, "inch": 25.4, "cm": 10.0, "mm": 1.0},
    {"mah": 1.0},
    {"wh": 1.0},
    {"w": 1.0},
]
_NUM_UNIT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(tb|gb|mb|kg|g|l|ml|cc|リットル|インチ|inch|cm|mm|mah|wh|w)?",
    re.IGNORECASE,
)
_COND_RE = re.compile(r"^\s*(>=|<=|==|=|>|<)?\s*(.+?)\s*$")


def catalog_in_stock(record: dict) -> bool:
    """Heuristic stock check for extended-catalog records: the availability
    text is free-form Amazon wording, so only an explicit out-of-stock
    marker (or missing text) counts as unavailable."""
    availability = record.get("availability") or ""
    return bool(availability) and "在庫切れ" not in availability


def build_catalog_products(index_items: list, investigated_asins: set) -> list[dict]:
    """Search-ready records for the UNINVESTIGATED slice of the extended
    catalog index. Investigated products are excluded twice over — by the
    index's own flag and by the live investigated ASIN set — so a stale or
    mis-flagged index can never leak a scored product into the fallback.
    Price-less records are skipped, mirroring the main search."""
    records: list[dict] = []
    seen: set = set()
    for item in index_items or []:
        if not isinstance(item, dict):
            continue
        asin = (item.get("asin") or "").upper()
        title = item.get("title") or ""
        price = _int(item.get("price"))
        if (
            not asin
            or asin in seen
            or asin in investigated_asins
            or item.get("investigated")
            or not title
            or not price
        ):
            continue
        seen.add(asin)
        brand = item.get("brand") or ""
        category = item.get("amazon_category") or ""
        text = f"{title} {brand} {category}"
        records.append(
            {
                "asin": asin,
                "title": title,
                "brand": brand,
                "price": price,
                "availability": item.get("availability") or "",
                "image": item.get("image") or "",
                "amazon_category": category,
                "price_as_of": item.get("price_as_of") or "",
                "in_stock": catalog_in_stock(item),
                "_haystack": nfkc(text),
                "_morphs_b": morphs_of(text),
                "_title_morphs": morphs_of(title),
            }
        )
    return records


def catalog_summary(record: dict) -> dict:
    """Client-facing shape for one uninvestigated catalog hit. Built field
    by field like normalize_catalog_record: the Amazon URL is never part
    of it; the citation is the site's catalog page."""
    catalog_url = f"{BASE}/catalog/?asin={record['asin']}"
    out: dict = {
        "asin": record["asin"],
        "title": record["title"],
        "brand": record["brand"],
        "price": record["price"],
        "availability": record["availability"],
        "image": record["image"],
        "amazon_category": record["amazon_category"],
        "price_as_of": record["price_as_of"],
        "investigated": False,
        "score": None,
        "catalog_url": catalog_url,
        "citation": f"[{record['title']}（未調査）]({catalog_url})",
    }
    date_label = _price_date_label(record["price_as_of"])
    if date_label:
        out["price_label"] = f"参考価格 {record['price']:,}円（{date_label}時点）"
    if _price_stale(record["price_as_of"]):
        out["price_stale"] = True
    return out


def _to_base(number: float, unit: str | None) -> tuple[float, int] | None:
    """(value in the family's base unit, family index); None if unit-less
    or the unit belongs to no known family."""
    if not unit:
        return None
    u = unit.lower()
    for idx, fam in enumerate(_UNIT_FAMILIES):
        if u in fam:
            return number * fam[u], idx
    return None


def _numbers_with_units(text: str) -> list[tuple[float, str | None]]:
    return [(float(m.group(1)), m.group(2)) for m in _NUM_UNIT_RE.finditer(text)]


def _compare(lhs: float, op: str, rhs: float) -> bool:
    if op == ">=":
        return lhs >= rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">":
        return lhs > rhs
    if op == "<":
        return lhs < rhs
    return lhs == rhs  # "=" / "=="


def spec_value_matches(spec_value: str, condition: str) -> bool:
    value_n = nfkc(spec_value)
    cond_n = nfkc(condition)
    m = _COND_RE.match(cond_n)
    if not m:
        return False
    op, body = m.group(1), m.group(2)
    if not op:
        return body in value_n
    nums = _numbers_with_units(body)
    if not nums:
        return body in value_n
    t_num, t_unit = nums[0]
    t_base = _to_base(t_num, t_unit)
    for num, unit in _numbers_with_units(value_n):
        if t_base is not None:
            vb = _to_base(num, unit)
            if vb is None or vb[1] != t_base[1]:
                continue
            if _compare(vb[0], op, t_base[0]):
                return True
        elif _compare(num, op, t_num):
            return True
    return False


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
            # Gift attributes exist only on products whose investigation
            # text carried gift context; key presence is the gift flag.
            "gift_suitable": "gift_recipients" in extra or "gift_occasions" in extra,
            "gift_recipients": list(extra.get("gift_recipients") or []),
            "gift_occasions": list(extra.get("gift_occasions") or []),
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
        record["_morphs_b"] = morphs_of(record["_haystack"])
        record["_title_morphs"] = morphs_of(record["_title_n"])
        record["_type_morphs"] = type_morphs_of(record["_title_n"])
        record["_brandcat_morphs"] = morphs_of(
            nfkc(
                " ".join(
                    [record["brand"], record["category"], " ".join(record["categories"])]
                )
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
        record["_morphs_b"] = morphs_of(record["_haystack"])
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


# -- morphological matching (Sudachi) ---------------------------------------
# Substring matching alone misses Japanese compound variants: the query
# 衣類乾燥機 does not substring-match 衣類ケア乾燥機, and 布団乾燥機 does
# not match ふとん乾燥機. Sudachi splits compounds into morphemes
# (SplitMode B: 衣類乾燥機 -> {衣類, 乾燥機}; 衣類ケア乾燥機 -> {衣類,
# ケア, 乾燥機}) and normalized_form() unifies notation variants
# (布団 -> ふとん), so a query matches when its morpheme set is a subset
# of the document's. Morpheme matching only ADDS matches on top of the
# substring path; it never removes one. If Sudachi is unavailable the
# server degrades to substring-only matching.
#
# sudachipy and sudachidict_core are a pinned pair (the 0.7 line changed
# the dictionary format); upgrade them together, deliberately.

import threading
from functools import lru_cache

_TOKENIZER = None
_TOKENIZER_FAILED = False
_TOKENIZE_LOCK = threading.Lock()
_STOP_POS = {"助詞", "助動詞", "記号", "補助記号", "感動詞", "接続詞", "連体詞"}
_EMPTY: frozenset = frozenset()


def _tokenizer():
    global _TOKENIZER, _TOKENIZER_FAILED
    if _TOKENIZER is None and not _TOKENIZER_FAILED:
        try:
            from sudachipy import Dictionary

            dictionary = Dictionary()
            _TOKENIZER = (
                dictionary.tokenizer()
                if hasattr(dictionary, "tokenizer")
                else dictionary.create()
            )
        except Exception:
            _TOKENIZER_FAILED = True
    return _TOKENIZER


def morphs_of(text: str) -> frozenset:
    """Normalized morpheme set of a text (Sudachi SplitMode B). Empty
    when Sudachi is unavailable. Mode B only, deliberately: mode A
    shreds compounds ({衣類, 乾燥, 機}) and then matches any document
    that merely mentions those morphemes somewhere (circulators and
    fans matched 衣類乾燥機 in live testing); mode B keeps compounds
    at meaningful-word granularity ({衣類, 乾燥機})."""
    tok = _tokenizer()
    if tok is None or not text:
        return _EMPTY
    from sudachipy import SplitMode

    with _TOKENIZE_LOCK:
        tokens = tok.tokenize(text, SplitMode.B)
    return frozenset(
        nfkc(m.normalized_form())
        for m in tokens
        if m.part_of_speech()[0] not in _STOP_POS and m.normalized_form()
    )


_TYPE_STOP = frozenset(
    {
        "用", "型", "セット", "対応", "日本", "国内", "新型", "公式",
        "本", "個", "枚", "色", "専用", "向け",
    }
)
_JP_CHAR = re.compile(r"[぀-ヿ一-鿿]")


def type_morphs_of(text: str) -> frozenset:
    """Product-type morphemes of a title: common nouns only (Sudachi
    SplitMode B, 名詞・普通名詞), >=2 chars, containing at least one
    Japanese character, minus generic tokens and pure numbers.

    Verified live (2026-11-24) against the full catalog: an overlap over
    ALL title morphemes keeps wrong-type products via unit / model
    fragments (kg, NH, 603), and ASCII tokens are brands, models or
    units — never the product type. With this restriction, a clothes
    dryer's type set is {衣類, 乾燥機, 電気} and only dryers and
    washer-dryers share a morpheme with it; refrigerators and plain
    washers share none."""
    tok = _tokenizer()
    if tok is None or not text:
        return _EMPTY
    from sudachipy import SplitMode

    with _TOKENIZE_LOCK:
        tokens = tok.tokenize(text, SplitMode.B)
    out = set()
    for m in tokens:
        pos = m.part_of_speech()
        if pos[0] != "名詞" or pos[1] not in ("普通名詞", "一般"):
            continue
        s = nfkc(m.normalized_form())
        if len(s) < 2 or s in _TYPE_STOP or not _JP_CHAR.search(s):
            continue
        if re.fullmatch(r"[0-9.]+", s):
            continue
        out.add(s)
    return frozenset(out)


@lru_cache(maxsize=1024)
def _variant_morphs(variant: str) -> frozenset:
    """Morpheme set for one query variant, cached — relaxation trials
    re-run the same tokens many times."""
    return morphs_of(variant)


def token_matches_record(token: str, record: dict) -> bool:
    """Substring match (as before) OR morpheme-subset match, over the
    token's synonym variants. The morpheme path is what lets 衣類乾燥機
    find 衣類ケア乾燥機 and 布団乾燥機 find ふとん乾燥機."""
    morphs_b = record.get("_morphs_b") or _EMPTY
    for v in token_variants(token):
        if _variant_matches(v, record["_haystack"]):
            return True
        vb = _variant_morphs(v)
        if vb and vb <= morphs_b:
            return True
    return False


# --- gift attributes ---------------------------------------------------------
# "Gift" is a use/recipient attribute, not a product type, so it lives on its
# own axis next to the taxonomy. The site extracts gift_recipients /
# gift_occasions (English keys) from investigation texts into
# mcp-products.json; only products with gift context carry them. Here the
# keys are mapped to Japanese labels, and user input (Japanese or English,
# e.g. "クリスマスプレゼント" / "christmas") is normalized back to a key.
GIFT_OCCASION_LABELS: dict[str, str] = {
    "christmas": "クリスマス",
    "birthday": "誕生日",
    "mothers_day": "母の日",
    "fathers_day": "父の日",
    "valentine": "バレンタイン",
    "whiteday": "ホワイトデー",
    "wedding": "結婚祝い",
    "baby_gift": "出産祝い",
    "housewarming": "新築・引っ越し祝い",
    "graduation": "卒業・入学・就職祝い",
    "respect_elderly": "敬老の日",
    "longevity": "長寿祝い",
    "anniversary": "記念日",
    "midyear": "お中元",
    "yearend": "お歳暮",
}
GIFT_RECIPIENT_LABELS: dict[str, str] = {
    "female": "女性",
    "male": "男性",
    "partner": "恋人",
    "kids": "子供",
    "parents": "両親",
    "grandparents": "祖父母",
    "friends": "友人",
    "colleagues": "上司・同僚",
    "self": "自分へのご褒美",
}


# Input-side aliases: words users/AIs naturally pass that are not the
# canonical labels (彼女 for partner, お母さん for parents, ...).
GIFT_RECIPIENT_ALIASES: dict[str, str] = {
    "彼女": "partner",
    "彼氏": "partner",
    "パートナー": "partner",
    "お母さん": "parents",
    "お父さん": "parents",
    "祖母": "grandparents",
    "祖父": "grandparents",
    "友達": "friends",
    "同僚": "colleagues",
    "上司": "colleagues",
    "自分": "self",
    "ご褒美": "self",
}


def gift_key(
    value: str | None,
    labels: dict[str, str],
    aliases: dict[str, str] | None = None,
) -> str | None:
    """Normalize a gift recipient/occasion argument to its English key.

    Accepts the key itself, the Japanese label, or a longer phrase that
    contains the label (e.g. "クリスマスプレゼント"). Longer labels are
    tried first so 結婚記念日-like overlaps resolve to the specific key
    (anniversary is only matched by its exact label or key).
    Returns None for empty input; an unrecognized non-empty value also
    returns None, and callers distinguish the two via the raw argument.
    """
    if not value:
        return None
    target = nfkc(value)
    for alias, key in (aliases or {}).items():
        if nfkc(alias) in target:
            return key
    for key, label in sorted(labels.items(), key=lambda kv: -len(kv[1])):
        if target == key or target == nfkc(label):
            return key
        if key != "anniversary" and (nfkc(label) in target or key in target):
            return key
    return None


def gift_labels(keys: list, labels: dict[str, str]) -> list[str]:
    return [labels.get(str(k), str(k)) for k in keys or []]


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
    summary = {
        "asin": record["asin"],
        "title": record["title"],
        "brand": record["brand"],
        "category": record["category"],
        "price": record["price"],
        "score": record["score"],
        "score_label": f"独自スコア {record['score']}点",
        "price_label": f"参考価格 {record['price']:,}円（データ更新時点）" if record["price"] else "価格未取得",
        "description": record["description"],
        "in_stock": record["in_stock"],
        "image": record["image"],
        "detail_url": record["detail_url"],
        "citation": f"[詳細レポート]({record['detail_url']})",
        "last_investigated": record["last_investigated"],
    }
    if record.get("gift_suitable"):
        occasions = gift_labels(record.get("gift_occasions") or [], GIFT_OCCASION_LABELS)
        recipients = gift_labels(record.get("gift_recipients") or [], GIFT_RECIPIENT_LABELS)
        parts = []
        if occasions:
            parts.append(f"シーン: {'・'.join(occasions)}")
        if recipients:
            parts.append(f"相手: {'・'.join(recipients)}")
        summary["gift_occasions"] = occasions
        summary["gift_recipients"] = recipients
        summary["gift_label"] = (
            f"ギフト候補（{'／'.join(parts)}）" if parts else "ギフト候補"
        )
    return summary


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
        self._catalog_cache: dict[str, tuple[float, dict | None]] = {}
        self._catalog_lock = asyncio.Lock()
        self.catalog_products: list[dict] = []

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
            (
                index,
                summary,
                deals,
                catgroups,
                brandgroups,
                mcp_products,
                catalog_index,
            ) = await asyncio.gather(
                self._fetch(client, "index"),
                self._fetch(client, "summary"),
                self._fetch(client, "deals"),
                self._fetch(client, "categorygroups"),
                self._fetch(client, "brandgroups"),
                self._fetch(client, "mcp_products", optional=True),
                self._fetch(client, "catalog_index", optional=True),
            )
        products = build_products(index, summary, mcp_products)
        self.products = products
        self.by_asin = {p["asin"]: p for p in products}
        self.catalog_products = build_catalog_products(
            catalog_index or [], set(self.by_asin)
        )
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

    async def catalog_record(self, asin: str) -> dict | None:
        """One record from the extended catalog shards, or None.

        Shards are fetched on demand and cached per shard. A missing or
        failed shard is cached briefly as a miss, so repeated lookups for
        absent ASINs do not hammer the site.
        """
        shard = catalog_shard_key(asin)

        def _hit() -> tuple[bool, dict | None]:
            cached = self._catalog_cache.get(shard)
            if cached is None:
                return False, None
            fetched, data = cached
            ttl = CATALOG_TTL_SECONDS if data is not None else CATALOG_MISS_TTL_SECONDS
            if time.time() - fetched >= ttl:
                return False, None
            return True, (data or {}).get(asin)

        fresh, record = _hit()
        if fresh:
            return record
        async with self._catalog_lock:
            fresh, record = _hit()
            if fresh:
                return record
            data: dict | None
            try:
                async with self._client() as client:
                    resp = await client.get(f"{BASE}/catalog-data/{shard}.json")
                    resp.raise_for_status()
                    parsed = resp.json()
                    data = parsed if isinstance(parsed, dict) else None
            except Exception:
                data = None
            self._catalog_cache[shard] = (time.time(), data)
            return (data or {}).get(asin)

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
        recipient: str | None = None,
        occasion: str | None = None,
        spec_filters: dict | None = None,
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
            recipient=recipient,
            occasion=occasion,
            spec_filters=spec_filters,
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

    def search_catalog(
        self,
        query: str | None = None,
        brand: str | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        in_stock_only: bool = True,
        limit: int = 5,
    ) -> dict:
        """Keyword search over the uninvestigated extended catalog.

        Matching reuses the investigated search's forgiveness (substring OR
        Sudachi morpheme-subset over title/brand/Amazon category, synonym
        and brand-alias expansion via token_matches_record). Ranking is
        title relevance first, then freshest price snapshot — there is no
        score to rank by, by design."""
        tokens = nfkc(query).split() if query else []
        brand_ok = self.brand_predicate(brand)
        matched: list[dict] = []
        for rec in self.catalog_products:
            if in_stock_only and not rec["in_stock"]:
                continue
            if max_price is not None and rec["price"] > max_price:
                continue
            if min_price is not None and rec["price"] < min_price:
                continue
            if brand_ok is not None and not brand_ok(rec):
                continue
            if tokens and not all(token_matches_record(t, rec) for t in tokens):
                continue
            matched.append(rec)

        def _rank(rec: dict) -> tuple[int, str]:
            title_hits = 0
            for t in tokens:
                for v in token_variants(t):
                    if _variant_matches(v, nfkc(rec["title"])) or (
                        _variant_morphs(v) and _variant_morphs(v) <= rec["_title_morphs"]
                    ):
                        title_hits += 1
                        break
            return title_hits, rec["price_as_of"]

        matched.sort(key=_rank, reverse=True)
        limit = max(1, min(limit, 20))
        return {"items": matched[:limit], "total": len(matched)}

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
            "gift_tagged": sum(1 for p in items if p.get("gift_suitable")),
        }

    def spec_key_stats(self, category: str | None = None) -> dict:
        """Which spec keys exist (optionally within one category), how many
        products carry each, and sample values — so clients can discover
        what spec_filters can actually filter on."""
        from collections import Counter

        cat_set = self.expand_category(category) if category else None
        cat_target = nfkc(category) if category and cat_set is None else None
        counts: Counter = Counter()
        samples: dict[str, Counter] = {}
        total = 0
        for p in self.products:
            if cat_set is not None and p["category"] not in cat_set:
                continue
            if cat_target is not None and cat_target not in nfkc(p["category"]):
                continue
            total += 1
            for k, v in (p.get("specs") or {}).items():
                counts[k] += 1
                sv = str(v)
                if len(sv) <= 40:
                    samples.setdefault(k, Counter())[sv] += 1
        return {
            "category": category,
            "products_in_scope": total,
            "spec_keys": [
                {
                    "key": k,
                    "products": c,
                    "sample_values": [
                        v for v, _ in samples.get(k, Counter()).most_common(8)
                    ],
                }
                for k, c in counts.most_common()
            ],
        }

    @staticmethod
    def _relevance(p: dict, tokens: list[str]) -> int:
        """Title matches weigh most, then brand/category, then body text.
        Each token scores the better of its substring tier and its
        morpheme tier, so morpheme-only matches (衣類ケア乾燥機 for the
        query 衣類乾燥機) rank like the substring matches they replace."""
        title_m = p.get("_title_morphs") or _EMPTY
        brandcat_m = p.get("_brandcat_morphs") or _EMPTY
        all_m = p.get("_morphs_b") or _EMPTY
        score = 0
        for tok in tokens:
            tier = 0
            for v in token_variants(tok):
                if _variant_matches(v, p["_title_n"]):
                    tier = max(tier, 3)
                elif _variant_matches(v, p["_brand_n"]) or _variant_matches(
                    v, p["_cat_n"]
                ):
                    tier = max(tier, 2)
                elif _variant_matches(v, p["_haystack"]):
                    tier = max(tier, 1)
                vb = _variant_morphs(v)
                if vb:
                    if vb <= title_m:
                        tier = max(tier, 3)
                    elif vb <= brandcat_m:
                        tier = max(tier, 2)
                    elif vb <= all_m:
                        tier = max(tier, 1)
            score += tier
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
        recipient: str | None = None,
        occasion: str | None = None,
        spec_filters: dict | None = None,
    ) -> list[dict]:
        tokens = nfkc(query).split() if query else []
        recipient_key = gift_key(recipient, GIFT_RECIPIENT_LABELS, GIFT_RECIPIENT_ALIASES)
        occasion_key = gift_key(occasion, GIFT_OCCASION_LABELS)
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
            if recipient is not None and (
                recipient_key is None or recipient_key not in p.get("gift_recipients", [])
            ):
                continue
            if occasion is not None and (
                occasion_key is None or occasion_key not in p.get("gift_occasions", [])
            ):
                continue
            if spec_filters:
                specs_n = {nfkc(k): v for k, v in (p.get("specs") or {}).items()}
                sonota = str(specs_n.get("その他") or "")

                def _spec_ok(key_n: str, cond: str) -> bool:
                    val = specs_n.get(key_n)
                    if val is not None:
                        return spec_value_matches(str(val), cond)
                    # Fallback: some products carry the value only as a
                    # labeled segment inside その他 ("乾燥容量: 9.0kg / ...").
                    for seg in sonota.split("/"):
                        label, sep, rest = seg.partition(":")
                        if not sep:
                            label, sep, rest = seg.partition("：")
                        if (
                            sep
                            and nfkc(label.strip()).endswith(key_n)
                            and spec_value_matches(rest, cond)
                        ):
                            return True
                    return False

                if not all(
                    _spec_ok(nfkc(str(k)), str(cond))
                    for k, cond in spec_filters.items()
                ):
                    continue
            if tokens and not all(token_matches_record(tok, p) for tok in tokens):
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
        recipient: str | None = None,
        occasion: str | None = None,
        spec_filters: dict | None = None,
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
            "recipient": recipient,
            "occasion": occasion,
            "spec_filters": spec_filters,
        }
        trials: list[tuple[str, str, dict]] = []
        if recipient:
            trials.append(("recipient", "ギフトの相手指定を外す", {"recipient": None}))
        if occasion:
            trials.append(("occasion", "ギフトのシーン指定を外す", {"occasion": None}))
        if spec_filters:
            for key in spec_filters:
                rest = {k: v for k, v in spec_filters.items() if k != key}
                trials.append(
                    (
                        f"spec:{key}",
                        f"スペック条件「{key}」を外す",
                        {"spec_filters": rest or None},
                    )
                )
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

        The category is only the candidate pool: child categories can mix
        product types (冷蔵庫・洗濯機・大型 holds refrigerators, washers
        and dryers), so when the target's title yields product-type
        morphemes (see type_morphs_of), the pool is filtered to products
        sharing at least one type morpheme, with multi-morpheme overlaps
        ranked first — a clothes dryer then returns dryers and
        washer-dryers, never a refrigerator. When no candidate shares a
        type morpheme (common for the その他 catch-all categories, whose
        pools are small grab-bags), the result falls back to the plain
        category pool and says so via match_basis="category_fallback",
        so callers can disclose that these are same-category items, not
        type-matched alternatives.

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
        pool_total = len(candidates)
        limit = max(1, min(limit, 20))
        target_types = target.get("_type_morphs") or _EMPTY
        if target_types:
            typed = []
            for p in candidates:
                overlap = len((p.get("_type_morphs") or _EMPTY) & target_types)
                if overlap >= 1:
                    typed.append((overlap, p))
            if typed:
                if max_price is None and target["price"]:
                    lo, hi = target["price"] * 0.5, target["price"] * 1.5
                    typed.sort(
                        key=lambda op: (
                            0 if op[0] >= 2 else 1,
                            0 if lo <= op[1]["price"] <= hi else 1,
                            -op[1]["score"],
                            op[1]["price"],
                        )
                    )
                else:
                    typed.sort(
                        key=lambda op: (0 if op[0] >= 2 else 1, -op[1]["score"], op[1]["price"])
                    )
                return {
                    "target": target,
                    "items": [p for _, p in typed[:limit]],
                    "total": len(typed),
                    "match_basis": "title_type",
                    "category_pool_count": pool_total,
                }
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
            "items": candidates[:limit],
            "total": pool_total,
            "match_basis": "category_fallback",
            "category_pool_count": pool_total,
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
            if tokens and not all(token_matches_record(t, d) for t in tokens):
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
