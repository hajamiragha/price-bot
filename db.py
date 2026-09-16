"""
Shared SQLite data layer for the interactive bot (bot.py) and the daily
report (daily_report.py). This replaces products.json/admin_state.json as
the source of truth once the bot moves to an always-on VPS process --
those two files can't hold live per-user cooldowns/quotas without turning
every interaction into a git commit.

The DB file itself (default: bot.db, override with PRICE_BOT_DB) is NOT
meant to be committed to git -- it's runtime state, listed in .gitignore.

Schema:
  categories(id, slug UNIQUE, title, parent_id -> categories.id)
    -- supports one level of nesting: "لوازم خانگی" (parent) ->
       "هواپز" / "دستگاه اسپرسو" (children). More depth works too, the
       bot only ever walks parent_id links, it doesn't assume a fixed depth.
  products(id, name, query, category_id -> categories.id)
    -- category_id is nullable (a product doesn't have to be categorized
       to be usable by the daily report, but the bot's category browser
       will simply never surface it).
  users(telegram_id, level, regular_used, regular_period_start, joined_at)
    -- level: 'admin' | 'special' | 'regular'. regular_used/period_start
       implement the regular user's 10-queries-per-calendar-month cap.
  cooldowns(telegram_id, product_id, next_allowed_at)
    -- one row per (user, product) the user has queried; implements the
       special user's 3h-per-product-per-user cooldown.

All timestamps are ISO-8601 UTC (datetime.utcnow().isoformat()). The
monthly quota resets on UTC calendar-month change, not a rolling 30 days --
simpler to reason about and explain to users ("resets on the 1st").
"""

import os
import sqlite3
from datetime import datetime, timedelta

DB_PATH = os.environ.get("PRICE_BOT_DB", "bot.db")

REGULAR_MONTHLY_LIMIT = 10
SPECIAL_COOLDOWN_HOURS = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    parent_id INTEGER REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    category_id INTEGER REFERENCES categories(id)
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    level TEXT NOT NULL DEFAULT 'regular',
    regular_used INTEGER NOT NULL DEFAULT 0,
    regular_period_start TEXT NOT NULL,
    joined_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cooldowns (
    telegram_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    next_allowed_at TEXT NOT NULL,
    PRIMARY KEY (telegram_id, product_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_conn(db_path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA)
    conn.commit()


def _now() -> datetime:
    return datetime.utcnow()


def _now_iso() -> str:
    return _now().isoformat()


# ---------------------------------------------------------------- categories

def add_category(conn, slug: str, title: str, parent_slug: str = None) -> int:
    parent_id = None
    if parent_slug:
        row = conn.execute(
            "SELECT id FROM categories WHERE slug = ?", (parent_slug,)
        ).fetchone()
        if row is None:
            raise ValueError(f"parent category {parent_slug!r} does not exist")
        parent_id = row["id"]

    cur = conn.execute(
        "INSERT INTO categories (slug, title, parent_id) VALUES (?, ?, ?) "
        "ON CONFLICT(slug) DO UPDATE SET title = excluded.title, "
        "parent_id = excluded.parent_id",
        (slug, title, parent_id),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM categories WHERE slug = ?", (slug,)).fetchone()
    return row["id"]


def get_category(conn, slug: str):
    return conn.execute(
        "SELECT * FROM categories WHERE slug = ?", (slug,)
    ).fetchone()


def get_category_by_id(conn, category_id: int):
    return conn.execute(
        "SELECT * FROM categories WHERE id = ?", (category_id,)
    ).fetchone()


def list_categories(conn, parent_id=None):
    """Top-level categories when parent_id is None, else children of that id."""
    if parent_id is None:
        return conn.execute(
            "SELECT * FROM categories WHERE parent_id IS NULL ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM categories WHERE parent_id = ? ORDER BY id", (parent_id,)
    ).fetchall()


def category_path(conn, category_id: int) -> list:
    """[root, ..., leaf] titles, for breadcrumbs."""
    path = []
    cur_id = category_id
    seen = set()
    while cur_id is not None and cur_id not in seen:
        seen.add(cur_id)
        row = get_category_by_id(conn, cur_id)
        if row is None:
            break
        path.append(row)
        cur_id = row["parent_id"]
    return list(reversed(path))


# ------------------------------------------------------------------ products

def add_product(conn, name: str, query: str, category_slug: str = None) -> int:
    category_id = None
    if category_slug:
        cat = get_category(conn, category_slug)
        if cat is None:
            raise ValueError(f"category {category_slug!r} does not exist")
        category_id = cat["id"]

    cur = conn.execute(
        "INSERT INTO products (name, query, category_id) VALUES (?, ?, ?)",
        (name, query, category_id),
    )
    conn.commit()
    return cur.lastrowid


def remove_product(conn, name: str) -> bool:
    cur = conn.execute(
        "DELETE FROM products WHERE lower(name) = lower(?)", (name,)
    )
    conn.commit()
    return cur.rowcount > 0


def list_products(conn, category_id: int = None):
    if category_id is None:
        return conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM products WHERE category_id = ? ORDER BY id", (category_id,)
    ).fetchall()


def get_product(conn, product_id: int):
    return conn.execute(
        "SELECT * FROM products WHERE id = ?", (product_id,)
    ).fetchone()


def get_product_by_name(conn, name: str):
    return conn.execute(
        "SELECT * FROM products WHERE lower(name) = lower(?)", (name,)
    ).fetchone()


# --------------------------------------------------------------------- users

def get_or_create_user(conn, telegram_id: int, force_level: str = None):
    row = conn.execute(
        "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
    ).fetchone()
    if row is None:
        level = force_level or "regular"
        conn.execute(
            "INSERT INTO users (telegram_id, level, regular_used, "
            "regular_period_start, joined_at) VALUES (?, ?, 0, ?, ?)",
            (telegram_id, level, _now_iso(), _now_iso()),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    elif force_level and row["level"] != force_level:
        set_user_level(conn, telegram_id, force_level)
        row = conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row


def set_user_level(conn, telegram_id: int, level: str):
    """Upsert: sets the level whether or not the user row exists yet, so
    this is safe to call directly (e.g. from the admin /setlevel command)
    without a separate get_or_create_user call first."""
    if level not in ("admin", "special", "regular"):
        raise ValueError(f"invalid level {level!r}")
    get_or_create_user(conn, telegram_id)
    conn.execute(
        "UPDATE users SET level = ? WHERE telegram_id = ?", (level, telegram_id)
    )
    conn.commit()


def list_users(conn):
    return conn.execute("SELECT * FROM users ORDER BY joined_at").fetchall()


def _period_key(dt: datetime) -> str:
    return f"{dt.year:04d}-{dt.month:02d}"


def _maybe_reset_regular_quota(conn, user_row) -> sqlite3.Row:
    """If the stored period_start is in a different UTC calendar month than
    now, reset the counter and move period_start to now. Returns the
    (possibly refreshed) user row."""
    period_start = datetime.fromisoformat(user_row["regular_period_start"])
    now = _now()
    if _period_key(period_start) != _period_key(now):
        conn.execute(
            "UPDATE users SET regular_used = 0, regular_period_start = ? "
            "WHERE telegram_id = ?",
            (now.isoformat(), user_row["telegram_id"]),
        )
        conn.commit()
        return conn.execute(
            "SELECT * FROM users WHERE telegram_id = ?",
            (user_row["telegram_id"],),
        ).fetchone()
    return user_row


def check_regular_quota(conn, telegram_id: int):
    """Returns (remaining: int, reset_hint: str) WITHOUT consuming a query."""
    user = get_or_create_user(conn, telegram_id)
    user = _maybe_reset_regular_quota(conn, user)
    remaining = max(0, REGULAR_MONTHLY_LIMIT - user["regular_used"])
    return remaining, _next_month_label()


def consume_regular_quota(conn, telegram_id: int) -> bool:
    """Attempts to spend one of the regular user's monthly queries.
    Returns True if allowed (and records the usage), False if exhausted."""
    user = get_or_create_user(conn, telegram_id)
    user = _maybe_reset_regular_quota(conn, user)
    if user["regular_used"] >= REGULAR_MONTHLY_LIMIT:
        return False
    conn.execute(
        "UPDATE users SET regular_used = regular_used + 1 WHERE telegram_id = ?",
        (telegram_id,),
    )
    conn.commit()
    return True


def _next_month_label() -> str:
    now = _now()
    year, month = (now.year, now.month + 1) if now.month < 12 else (now.year + 1, 1)
    return f"{year:04d}-{month:02d}-01"


# ---------------------------------------------------------------- cooldowns

def get_cooldown_remaining_seconds(conn, telegram_id: int, product_id: int) -> float:
    """0 if the user may query this product right now, else seconds left."""
    row = conn.execute(
        "SELECT next_allowed_at FROM cooldowns WHERE telegram_id = ? AND product_id = ?",
        (telegram_id, product_id),
    ).fetchone()
    if row is None:
        return 0.0
    next_allowed = datetime.fromisoformat(row["next_allowed_at"])
    delta = (next_allowed - _now()).total_seconds()
    return max(0.0, delta)


def set_cooldown(conn, telegram_id: int, product_id: int, hours: float = SPECIAL_COOLDOWN_HOURS):
    next_allowed = (_now() + timedelta(hours=hours)).isoformat()
    conn.execute(
        "INSERT INTO cooldowns (telegram_id, product_id, next_allowed_at) "
        "VALUES (?, ?, ?) ON CONFLICT(telegram_id, product_id) "
        "DO UPDATE SET next_allowed_at = excluded.next_allowed_at",
        (telegram_id, product_id, next_allowed),
    )
    conn.commit()


# ------------------------------------------------------------------- meta

def get_meta(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row is not None else default


def set_meta(conn, key: str, value: str):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def format_seconds(seconds: float) -> str:
    """'2 ساعت و 15 دقیقه' style countdown label."""
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours} ساعت و {minutes} دقیقه"
    if hours:
        return f"{hours} ساعت"
    if minutes:
        return f"{minutes} دقیقه"
    return "کمتر از یک دقیقه"
