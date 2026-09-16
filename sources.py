"""
Per-site search adapters.

Each adapter takes a search query string and returns a list of candidate
matches: [{"title": str, "price_toman": int, "url": str}, ...]
or None if the site could not be reached / parsed at all.

Digikala and Torob are implemented. To add another source later:
  1. write a function `def search_<site>(query: str) -> list | None`
  2. register it in SOURCES below
"""

import json
import re
import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def _dig(d, *path, default=None):
    """Safely walk a nested dict/list by a sequence of keys/indexes."""
    cur = d
    for p in path:
        try:
            cur = cur[p]
        except (KeyError, IndexError, TypeError):
            return default
    return cur


def search_digikala(query: str, debug: bool = False) -> list | None:
    """
    Digikala public search API. Returns Toman prices (Digikala's API
    reports Rial; we divide by 10). This divisor is the one thing most
    likely to need correcting after the first real run -- sanity check
    the raw numbers Digikala shows on the site vs. what this prints.
    """
    url = "https://api.digikala.com/v1/search/"
    try:
        resp = requests.get(url, params={"q": query}, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[digikala] request failed: {e}")
        return None

    products = _dig(data, "data", "products", default=None)
    if products is None:
        # API shape may have changed -- dump top-level keys so the next
        # debugging pass has something to go on.
        print(f"[digikala] unexpected response shape, top-level keys: {list(data.keys())}")
        return None

    results = []
    for p in products:
        title = p.get("title_fa") or p.get("title_en") or p.get("title")
        if not title:
            continue

        price_rial = (
            _dig(p, "default_variant", "price", "selling_price")
            or _dig(p, "default_variant", "price", "rrp_price")
            or _dig(p, "price", "selling_price")
        )
        if price_rial is None:
            continue
        price_toman = int(price_rial) // 10

        slug = _dig(p, "url", "uri") or _dig(p, "url_fa") or ""
        product_url = f"https://www.digikala.com{slug}" if slug.startswith("/") else slug
        product_id = p.get("id")

        results.append({
            "title": title,
            "price_toman": price_toman,
            "url": product_url,
            "product_id": product_id,
        })

    if debug:
        print(f"[digikala] query={query!r} -> {len(results)} candidates")
        for r in results[:5]:
            print(f"    {r['price_toman']:>12,} toman | {r['title']}")

    return results


def get_digikala_product_detail(product_id, debug: bool = False):
    """Full product detail, which is where per-seller offers live."""
    url = f"https://api.digikala.com/v2/product/{product_id}/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[digikala] product detail request failed for {product_id}: {e}")
        return None

    product = _dig(data, "data", "product")
    if product is None:
        print(f"[digikala] unexpected product-detail shape, top-level keys: {list(data.keys())}")
        return None

    if debug:
        print(f"[digikala] product {product_id} detail keys: {list(product.keys())}")

    return product


def extract_digikala_sellers(product: dict, debug: bool = False) -> list:
    """
    Best-effort extraction of every seller offering this exact product.

    Digikala's real "other sellers" comparison is not a documented API,
    so this reads it off product['variants'] (each variant normally
    carries its own seller + price). If this comes back empty on the
    first real run, the debug key dump above is what to check next --
    the actual field names may differ (e.g. seller info nested one level
    deeper, or under a separate "other_sellers" key parallel to variants).
    """
    sellers = []
    for v in product.get("variants") or []:
        seller = v.get("seller") or {}
        seller_name = seller.get("title") or seller.get("name")
        price_rial = _dig(v, "price", "selling_price")
        if not seller_name or price_rial is None:
            continue

        seller_code = seller.get("code") or seller.get("id")
        seller_url = (
            f"https://www.digikala.com/seller/{seller_code}/" if seller_code else None
        )
        sellers.append({
            "seller_name": seller_name,
            "price_toman": int(price_rial) // 10,
            "url": seller_url,
        })

    if debug:
        print(f"[digikala] extracted {len(sellers)} seller offer(s)")
        for s in sellers[:10]:
            print(f"    {s['price_toman']:>12,} toman | {s['seller_name']}")

    return sellers


TOROB_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml",
}


def _extract_next_data(html: str, debug: bool = False):
    """Torob (and many Next.js sites) embed the full page data as JSON in
    a <script id="__NEXT_DATA__">...</script> tag -- easier and more
    stable than reverse-engineering their internal XHR endpoints."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
    )
    if not m:
        if debug:
            print("[torob] __NEXT_DATA__ script tag not found")
        return None
    try:
        return json.loads(m.group(1))
    except Exception as e:
        if debug:
            print(f"[torob] failed to parse __NEXT_DATA__ JSON: {e}")
        return None


def search_torob(query: str, debug: bool = False) -> list | None:
    """
    Torob search page. Torob server-renders its results into a
    __NEXT_DATA__ JSON blob on the page itself (props.pageProps.products),
    so a plain GET + a bit of JSON parsing works -- no separate API call
    needed. Prices are already in Toman (unlike Digikala's Rial).

    NOTE: each result here is a "base product" aggregated across many
    shops (see shop_text, e.g. "در ۳۲۴ فروشگاه") -- it is NOT the
    per-seller list. Getting the real per-seller breakdown for the
    chosen product still requires parsing that product's own detail page
    (its url is returned here as "url"); that part is implemented below
    in get_torob_product_detail / extract_torob_sellers.
    """
    url = "https://torob.com/search/"
    try:
        resp = requests.get(
            url, params={"query": query}, headers=TOROB_HEADERS, timeout=20
        )
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"[torob] request failed: {e}")
        return None

    data = _extract_next_data(html, debug=debug)
    if data is None:
        return None

    products = _dig(data, "props", "pageProps", "products", default=None)
    if products is None:
        print("[torob] unexpected page shape, no props.pageProps.products")
        return None

    results = []
    for p in products:
        title = p.get("name1")
        if not title:
            continue
        price_toman = p.get("price")
        if not price_toman:
            continue

        slug = p.get("web_client_absolute_url") or ""
        product_url = f"https://torob.com{slug}" if slug.startswith("/") else slug

        results.append({
            "title": title,
            "price_toman": int(price_toman),
            "url": product_url,
            "product_id": p.get("random_key"),
            "shop_count_text": p.get("shop_text"),
        })

    if debug:
        print(f"[torob] query={query!r} -> {len(results)} candidates")
        for r in results[:5]:
            print(f"    {r['price_toman']:>12,} toman | {r['title']}  ({r.get('shop_count_text')})")

    return results


def get_torob_product_detail(product_url: str, debug: bool = False):
    """Fetch a Torob product detail page (the /p/<key>/<slug>/ page) and
    return its baseProduct dict (props.pageProps.baseProduct) -- this is
    where the per-seller offer lists (products_info / products_in_store_info)
    live."""
    try:
        resp = requests.get(product_url, headers=TOROB_HEADERS, timeout=20)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        print(f"[torob] product detail request failed for {product_url}: {e}")
        return None

    data = _extract_next_data(html, debug=debug)
    if data is None:
        return None

    base_product = _dig(data, "props", "pageProps", "baseProduct", default=None)
    if base_product is None:
        page_props = _dig(data, "props", "pageProps", default={})
        print(f"[torob] no baseProduct found, pageProps keys: {list(page_props.keys())}")
        return None

    if debug:
        print(f"[torob] baseProduct keys: {list(base_product.keys())}")
    return base_product


def extract_torob_sellers(base_product: dict, debug: bool = False) -> list:
    """
    Per-seller offers from a Torob product detail page's baseProduct dict.

    Online ("خرید اینترنتی") sellers live in products_info.result[], each
    with shop_name (+ shop_name2 city), price (already Toman), and page_url
    -- a real seller-specific deep link through
    api.torob.com/v4/product-page/redirect/?...&prk=<seller_offer_id>&...

    Offline/in-person ("خرید حضوری") sellers -- products_in_store_info.result[]
    -- are deliberately NOT included here. Their page_url is a raw JSON
    contact-info API rather than a browsable page (no real deep link), and
    more importantly their prices are unverified/self-reported by the shop
    and were seen to be wildly out of line with every online source for the
    same product (e.g. ~9.5M vs. a ~14-26M cluster everywhere else) --
    including them skews min/avg and the histogram with noise, not signal.
    """
    sellers = []

    for r in _dig(base_product, "products_info", "result", default=[]) or []:
        shop_name = r.get("shop_name")
        price_toman = r.get("price")
        if not shop_name or not price_toman:
            continue
        city = r.get("shop_name2")
        label = f"{shop_name} ({city})" if city else shop_name
        sellers.append({
            "seller_name": label,
            "price_toman": int(price_toman),
            "url": r.get("page_url"),
        })

    if debug:
        print(f"[torob] extracted {len(sellers)} seller offer(s)")
        for s in sellers[:10]:
            print(f"    {s['price_toman']:>12,} toman | {s['seller_name']}")

    return sellers


SOURCES = {
    "Digikala": search_digikala,
    "Torob": search_torob,
    # "Emalls": search_emalls,    # to be added
}
