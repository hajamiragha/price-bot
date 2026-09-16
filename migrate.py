"""
One-off setup script: creates bot.db (if missing) and seeds it with:
  - the starter category tree: لوازم خانگی -> هواپز / دستگاه اسپرسو
  - every product currently in products.json, filed under هواپز (that's
    where Philips NA230 belongs -- re-file others with the bot's own
    /setcategory admin command afterwards if needed)

Safe to re-run: categories are upserted by slug, and products already in
the DB by (name, query) are not duplicated.

Run once, before starting bot.py for the first time:
    python3 migrate.py
"""

import json
import os

import db

PRODUCTS_JSON = "products.json"


def load_products_json() -> list:
    if not os.path.exists(PRODUCTS_JSON):
        return []
    with open(PRODUCTS_JSON, encoding="utf-8") as f:
        return json.load(f)


def seed_categories(conn):
    db.add_category(conn, "home-appliances", "لوازم خانگی")
    db.add_category(conn, "air-fryer", "هواپز", parent_slug="home-appliances")
    db.add_category(conn, "espresso-machine", "دستگاه اسپرسو", parent_slug="home-appliances")


def import_products(conn, default_category_slug="air-fryer"):
    imported = 0
    for p in load_products_json():
        existing = db.get_product_by_name(conn, p["name"])
        if existing is not None:
            print(f"skip (already in DB): {p['name']}")
            continue
        db.add_product(conn, p["name"], p["query"], default_category_slug)
        imported += 1
        print(f"imported: {p['name']}  -> {default_category_slug}")
    return imported


def main():
    conn = db.get_conn()
    db.init_db(conn)
    seed_categories(conn)
    count = import_products(conn)
    print(f"\ndone. {count} product(s) imported. DB file: {db.DB_PATH}")


if __name__ == "__main__":
    main()
