"""
Daily price report.

For every product in products.json: find the single best-matching
listing per source, pull that product's per-seller offers, and post one
message per product to CHANNEL_ID with:
  - product name
  - min / avg / max across every seller found (all sources combined)
  - a price-range histogram (bucket width scales with price level)
  - per-source sections listing every seller, cheapest first, hyperlinked

Env vars required: BOT_TOKEN, CHANNEL_ID
"""

import difflib
import json
import os
import re
import sys

import requests

from sources import SOURCES, get_digikala_product_detail, extract_digikala_sellers

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def to_persian_digits(s: str) -> str:
    return "".join(PERSIAN_DIGITS[int(ch)] if ch.isdigit() else ch for ch in s)


def format_toman(n: int) -> str:
    return to_persian_digits(f"{n:,.0f}" if isinstance(n, float) else f"{n:,}") + " تومان"


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _whole_word_hits(query_tokens: set, title: str) -> int:
    """Count query tokens that appear in title as a whole word/number,
    not merely as a substring (so 'na230' doesn't match 'na230/09')."""
    hits = 0
    for t in query_tokens:
        pattern = r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])"
        if re.search(pattern, title):
            hits += 1
    return hits


def best_match(query: str, candidates: list, threshold: float = 0.3):
    """Pick the single listing that best identifies the product itself
    (used to decide *which* product page to pull sellers from -- not to
    aggregate prices across different listings anymore)."""
    if not candidates:
        return None

    q = normalize(query)
    q_tokens = set(q.split())

    scored = []
    for c in candidates:
        title = normalize(c["title"])
        exact_hits = _whole_word_hits(q_tokens, title)
        exact_score = exact_hits / max(len(q_tokens), 1)
        ratio = difflib.SequenceMatcher(None, q, title).ratio()
        score = max(exact_score, ratio)
        scored.append((score, c))

    best_score = max(s for s, _ in scored)
    if best_score < threshold:
        return None

    tied = [c for s, c in scored if s == best_score]
    return min(tied, key=lambda c: c["price_toman"])


def bucket_width(price: float) -> int:
    if price < 15_000_000:
        return 500_000
    if price < 30_000_000:
        return 1_000_000
    if price < 60_000_000:
        return 2_000_000
    if price < 150_000_000:
        return 5_000_000
    return 10_000_000


def bucket_sellers(prices: list) -> list:
    """[(bucket_start, bucket_end, count), ...] sorted ascending."""
    if not prices:
        return []
    width = bucket_width(sum(prices) / len(prices))
    counts = {}
    for p in prices:
        start = int(p // width) * width
        counts[start] = counts.get(start, 0) + 1
    return [(start, start + width, count) for start, count in sorted(counts.items())]


def load_products() -> list:
    with open("products.json", encoding="utf-8") as f:
        return json.load(f)


def get_sellers_for_source(source_name: str, chosen: dict) -> list:
    """Per-seller offers for the chosen listing. Falls back to treating
    the single matched listing as one 'seller' when the site has no
    seller-comparison data available (or isn't wired up yet)."""
    sellers = []
    if source_name == "Digikala" and chosen.get("product_id"):
        detail = get_digikala_product_detail(chosen["product_id"], debug=True)
        if detail:
            sellers = extract_digikala_sellers(detail, debug=True)

    if not sellers:
        sellers = [{
            "seller_name": chosen["title"],
            "price_toman": chosen["price_toman"],
            "url": chosen.get("url"),
        }]
    return sellers


def build_message(product: dict) -> str:
    name = product["name"]
    query = product["query"]

    all_prices = []
    sections = []  # (source_name, sellers or None)

    for source_name, search_fn in SOURCES.items():
        candidates = search_fn(query, debug=True) if source_name == "Digikala" else search_fn(query)
        chosen = best_match(query, candidates or [])

        sellers = get_sellers_for_source(source_name, chosen) if chosen else []
        all_prices.extend(s["price_toman"] for s in sellers)
        sections.append((source_name, sellers))

    lines = [f"📦 <b>{escape_html(name)}</b>", ""]

    if all_prices:
        avg = sum(all_prices) / len(all_prices)
        lines.append(
            f"📊 کمترین: {format_toman(min(all_prices))} | "
            f"میانگین: {format_toman(avg)} | "
            f"بیشترین: {format_toman(max(all_prices))}"
        )
        lines.append("")
        lines.append("بازه‌های قیمت:")
        for start, end, count in bucket_sellers(all_prices):
            lines.append(
                f"🔸 {to_persian_digits(str(count))} فروشنده: "
                f"{format_toman(start)} تا {format_toman(end)}"
            )
        lines.append("")

    for source_name, sellers in sections:
        lines.append(f"🛒 <b>{escape_html(source_name)}</b>")
        if not sellers:
            lines.append("پیدا نشد")
        else:
            for s in sorted(sellers, key=lambda x: x["price_toman"]):
                label = escape_html(s["seller_name"])
                price = format_toman(s["price_toman"])
                if s.get("url"):
                    lines.append(f'• <a href="{s["url"]}">{label}</a> — {price}')
                else:
                    lines.append(f"• {label} — {price}")
        lines.append("")

    return "\n".join(lines).strip()


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not resp.ok:
        print(f"[telegram] send failed: {resp.status_code} {resp.text}")
    resp.raise_for_status()


def main():
    bot_token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHANNEL_ID"]

    products = load_products()
    if not products:
        print("no products configured, nothing to do")
        return

    for product in products:
        try:
            message = build_message(product)
            print("----")
            print(message)
            send_telegram(bot_token, chat_id, message)
        except Exception as e:
            print(f"[scraper] failed on product {product.get('name')!r}: {e}")


if __name__ == "__main__":
    sys.exit(main())
