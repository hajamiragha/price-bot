"""
Per-site search adapters.

Each adapter takes a search query string and returns a list of candidate
matches: [{"title": str, "price_toman": int, "url": str}, ...]
or None if the site could not be reached / parsed at all.

Only Digikala is implemented for now. To add another source later:
  1. write a function `def search_<site>(query: str) -> list | None`
  2. register it in SOURCES below
"""

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

        results.append({"title": title, "price_toman": price_toman, "url": product_url})

    if debug:
        print(f"[digikala] query={query!r} -> {len(results)} candidates")
        for r in results[:5]:
            print(f"    {r['price_toman']:>12,} toman | {r['title']}")

    return results


SOURCES = {
    "Digikala": search_digikala,
    # "Torob": search_torob,      # to be added
    # "Emalls": search_emalls,    # to be added
}
