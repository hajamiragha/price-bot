"""
Daily price report.

For every product in products.json, search each configured source
(sources.SOURCES), pick the best-matching result by fuzzy title match,
and post one Telegram message per product to CHANNEL_ID.

Env vars required: BOT_TOKEN, CHANNEL_ID
"""

import difflib
import json
import os
import sys

import requests

from sources import SOURCES

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"


def to_persian_digits(s: str) -> str:
    return "".join(PERSIAN_DIGITS[int(ch)] if ch.isdigit() else ch for ch in s)


def format_toman(n: int) -> str:
    return to_persian_digits(f"{n:,}") + " تومان"


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def best_match(query: str, candidates: list, threshold: float = 0.3):
    """Pick the candidate whose title best matches the query.
    Falls back to substring/token overlap so brand+model queries
    (e.g. 'philips na230') score well even against long titles."""
    if not candidates:
        return None

    q = normalize(query)
    q_tokens = set(q.split())

    best, best_score = None, 0.0
    for c in candidates:
        title = normalize(c["title"])
        ratio = difflib.SequenceMatcher(None, q, title).ratio()
        token_hits = sum(1 for t in q_tokens if t in title)
        token_score = token_hits / max(len(q_tokens), 1)
        score = max(ratio, token_score)
        if score > best_score:
            best, best_score = c, score

    if best_score < threshold:
        return None
    return best


def load_products() -> list:
    with open("products.json", encoding="utf-8") as f:
        return json.load(f)


def build_message(product: dict) -> str:
    name = product["name"]
    query = product["query"]

    lines = [f"📦 {name}"]
    found_prices = []

    for source_name, search_fn in SOURCES.items():
        candidates = search_fn(query, debug=True) if source_name == "Digikala" else search_fn(query)
        match = best_match(query, candidates or [])
        if match:
            found_prices.append(match["price_toman"])
            price_line = format_toman(match["price_toman"])
            lines.append(f"🔹 {source_name}: {price_line}")
            if match.get("url"):
                lines.append(f"   {match['url']}")
        else:
            lines.append(f"🔹 {source_name}: پیدا نشد")

    if len(found_prices) > 1:
        summary = (
            f"📊 کمترین: {format_toman(min(found_prices))} | "
            f"بیشترین: {format_toman(max(found_prices))}"
        )
        lines.insert(1, summary)

    return "\n".join(lines)


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
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
