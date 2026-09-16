"""
Daily price report -- DB-backed version, for the VPS deployment.

Same output as scraper.py, but reads the product list from bot.db (the
same DB bot.py serves the interactive menus from) instead of
products.json, so admin edits made through the bot immediately show up
here too. Run on a schedule via deploy/price-bot-report.timer (systemd),
not GitHub Actions -- see README.md.

Env vars required: BOT_TOKEN, CHANNEL_ID
Env vars optional: PRICE_BOT_DB (sqlite path, default bot.db)
"""

import os
import sys

import db
from report import build_message, send_message_in_parts


def main():
    bot_token = os.environ["BOT_TOKEN"]
    chat_id = os.environ["CHANNEL_ID"]

    conn = db.get_conn()
    db.init_db(conn)

    products = db.list_products(conn)
    if not products:
        print("no products configured, nothing to do")
        return

    for product in products:
        try:
            message = build_message(product["name"], product["query"])
            print("----")
            print(message)
            send_message_in_parts(bot_token, chat_id, product["name"], message)
        except Exception as e:
            print(f"[daily_report] failed on product {product['name']!r}: {e}")


if __name__ == "__main__":
    sys.exit(main())
