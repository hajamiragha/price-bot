"""
Interactive Telegram bot: category browser, per-product price queries with
tiered access control, and an admin panel -- all as Telegram menus/commands
(no separate web dashboard).

This is a LONG-RUNNING process (long-polls getUpdates in a loop) and must
run continuously, e.g. under systemd on a VPS -- see deploy/price-bot-bot.service.
It cannot run as a GitHub Actions cron job like the old admin_bot.py did:
buttons need an instant reply, and a job that only wakes up every few
minutes can't give one.

Env vars required:
  BOT_TOKEN        bot token from BotFather
  ADMIN_ID         your numeric Telegram user id (always full access)
Env vars optional:
  REQUIRED_CHANNELS  comma-separated @username or -100... channel ids that
                     every user (including admin) must belong to before the
                     bot responds to anything. Default: "@bazarpricedaily"
  PRICE_BOT_DB       sqlite file path (default: bot.db, see db.py)
  POLL_TIMEOUT       long-poll seconds per getUpdates call (default: 25)

Run `python3 migrate.py` once before the first start to create/seed the DB.
"""

import os
import sys
import time
import traceback

import requests

import db
from report import build_message, send_message_in_parts, send_telegram

POLL_TIMEOUT = int(os.environ.get("POLL_TIMEOUT", "25"))


# --------------------------------------------------------------- Telegram IO

def telegram_get(bot_token: str, method: str, **params):
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    resp = requests.get(url, params=params, timeout=POLL_TIMEOUT + 10)
    resp.raise_for_status()
    return resp.json()


def telegram_post(bot_token: str, method: str, **payload):
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def send_text(bot_token, chat_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return telegram_post(bot_token, "sendMessage", **payload)


def edit_text(bot_token, chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        telegram_post(bot_token, "editMessageText", **payload)
    except requests.HTTPError:
        # message too old / identical content / etc -- fall back to a new message
        send_text(bot_token, chat_id, text, reply_markup)


def answer_callback(bot_token, callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = show_alert
    telegram_post(bot_token, "answerCallbackQuery", **payload)


def check_membership(bot_token, required_channels: list, user_id: int) -> bool:
    """True iff user_id is a member/admin/creator of every channel listed.
    The bot itself must already be a member (usually admin) of each
    channel for getChatMember to work at all."""
    for channel in required_channels:
        try:
            resp = telegram_get(
                bot_token, "getChatMember", chat_id=channel, user_id=user_id
            )
        except requests.HTTPError:
            return False
        status = resp.get("result", {}).get("status")
        if status not in ("member", "administrator", "creator"):
            return False
    return True


# ------------------------------------------------------------- access rules

def access_check_and_consume(conn, level: str, telegram_id: int, product):
    """Returns (allowed: bool, denial_message: str | None). On allow, also
    performs the matching side effect (setting the special-user cooldown,
    or consuming a regular-user's monthly credit) so callers don't need a
    separate commit step."""
    if level == "admin":
        return True, None

    if level == "special":
        remaining = db.get_cooldown_remaining_seconds(conn, telegram_id, product["id"])
        if remaining > 0:
            wait = db.format_seconds(remaining)
            return False, (
                f"⏳ برای «{product['name']}» باید {wait} دیگه صبر کنی.\n"
                f"(هر کالا برای هر کاربر هر {db.SPECIAL_COOLDOWN_HOURS} ساعت یک‌بار قابل استعلامه.)"
            )
        db.set_cooldown(conn, telegram_id, product["id"])
        return True, None

    # regular
    remaining_q, reset_hint = db.check_regular_quota(conn, telegram_id)
    if remaining_q <= 0:
        return False, (
            f"🚫 سقف {db.REGULAR_MONTHLY_LIMIT} استعلام رایگان این ماهت تموم شده.\n"
            f"از {reset_hint} دوباره فعال میشه.\n"
            f"برای استعلام نامحدودتر (فقط با فاصله‌ی {db.SPECIAL_COOLDOWN_HOURS} ساعته‌ی هر کالا) "
            f"می‌تونی کاربر ویژه بشی."
        )
    if not db.consume_regular_quota(conn, telegram_id):
        return False, "🚫 سقف استعلامت تموم شده."
    return True, None


# ------------------------------------------------------------------ content

ABOUT_TEXT = (
    "🤖 <b>درباره‌ی این ربات</b>\n\n"
    "این ربات قیمت کالاهای منتخب رو هم‌زمان از چند فروشگاه آنلاین (دیجی‌کالا، ترب و ...) "
    "جمع می‌کنه و کمترین/میانگین/بیشترین قیمت به‌همراه لیست فروشنده‌ها و لینک مستقیم هرکدوم "
    "رو نشونت میده. گزارش روزانه هم توی کانال منتشر می‌شه.\n\n"
    "<b>شرط استفاده:</b> عضویت در کانال قیمت (لینک بالای همین پیام). بدون عضویت، "
    "ربات به هیچ درخواستی جواب نمی‌ده.\n\n"
    "<b>سطوح دسترسی:</b>\n"
    f"🔹 <b>کاربر عادی</b> — ماهانه {db.REGULAR_MONTHLY_LIMIT} استعلام رایگان، از هر دسته‌بندی که بخوای.\n"
    f"🔸 <b>کاربر ویژه</b> (مشترک) — استعلام از هر تعداد کالا آزاده، فقط هر کالا برای خودت "
    f"هر {db.SPECIAL_COOLDOWN_HOURS} ساعت یک‌بار قابل استعلامه؛ استعلام گروهیِ کل یک دسته‌بندی هم "
    "فقط برای همین سطح فعاله.\n"
    "⭐️ <b>ادمین</b> — دسترسی کامل و نامحدود.\n\n"
    "🛠 پلن‌های اشتراک (خرید دسترسی ویژه) به‌زودی از همین منو اضافه می‌شه.\n\n"
    "با ارسال /start در هر لحظه به همین صفحه برمی‌گردی."
)

HELP_TEXT = (
    "دستورات ادمین:\n"
    "/add <name> | <query> | [category-slug] — افزودن کالا\n"
    "/remove <name> — حذف کالا\n"
    "/list — لیست کالاها\n"
    "/addcategory <slug> | <title> | [parent-slug] — افزودن دسته‌بندی\n"
    "/categories — نمایش درخت دسته‌بندی‌ها\n"
    "/setlevel <telegram_id> <admin|special|regular> — تعیین سطح دسترسی\n"
    "/users — لیست کاربران و سطح‌شون\n"
    "/help — همین راهنما"
)


def welcome_text() -> str:
    return (
        "👋 خوش اومدی!\n\n"
        "از منوی زیر می‌تونی دسته‌بندی کالاها رو ببینی و قیمت لحظه‌ای بگیری.\n"
        "برای توضیح کامل، محدودیت‌ها و شرایط استفاده، «ℹ️ درباره‌ی ربات» رو بزن."
    )


def not_member_text(required_channels: list) -> str:
    links = "\n".join(f"• {ch}" for ch in required_channels)
    return (
        "برای استفاده از ربات باید عضو کانال(های) زیر باشی:\n"
        f"{links}\n\n"
        "بعد از عضویت، دکمه‌ی زیر رو بزن."
    )


# ------------------------------------------------------------------ keyboards

def kb(rows):
    return {"inline_keyboard": rows}


def btn(text, callback_data):
    return {"text": text, "callback_data": callback_data}


def main_menu_keyboard():
    return kb([
        [btn("📋 دسته‌بندی‌ها", "cat:root")],
        [btn("ℹ️ درباره‌ی ربات", "about")],
    ])


def join_keyboard(required_channels: list):
    rows = []
    for ch in required_channels:
        if ch.startswith("@"):
            rows.append([btn(f"عضویت در {ch}", "noop")])
    rows.append([btn("✅ عضو شدم، بررسی کن", "checkjoin")])
    return kb(rows)


def category_list_keyboard(categories, back_target=None):
    rows = [[btn(f"📁 {c['title']}", f"cat:{c['id']}")] for c in categories]
    if back_target is not None:
        rows.append([btn("🔙 بازگشت", back_target)])
    else:
        rows.append([btn("🔙 منوی اصلی", "main")])
    return kb(rows)


def product_list_keyboard(products, category_id, back_target, allow_bulk: bool):
    rows = [[btn(f"🔍 {p['name']}", f"prod:{p['id']}")] for p in products]
    if allow_bulk and products:
        rows.append([btn("📊 استعلام همه‌ی این دسته", f"bulk:{category_id}")])
    rows.append([btn("🔙 بازگشت", back_target)])
    return kb(rows)


# --------------------------------------------------------------- navigation

def user_level(conn, telegram_id: int, admin_id: int) -> str:
    row = db.get_or_create_user(
        conn, telegram_id, force_level="admin" if telegram_id == admin_id else None
    )
    return row["level"]


def show_category_screen(bot_token, conn, chat_id, category_id, level, edit=None):
    """category_id is an int, or None for the root (top-level) screen."""
    children = db.list_categories(conn, parent_id=category_id)
    if children:
        parent_row = db.get_category_by_id(conn, category_id) if category_id else None
        back_target = f"cat:{parent_row['parent_id']}" if (parent_row and parent_row['parent_id']) else "main"
        title = parent_row["title"] if parent_row else "📋 دسته‌بندی‌ها"
        text = f"📁 <b>{title}</b>\n\nیکی رو انتخاب کن:" if parent_row else "📋 <b>دسته‌بندی‌ها</b>\n\nیکی رو انتخاب کن:"
        markup = category_list_keyboard(children, back_target if parent_row else None)
    else:
        # leaf category: list its products
        products = db.list_products(conn, category_id)
        cat_row = db.get_category_by_id(conn, category_id)
        title = cat_row["title"] if cat_row else "کالاها"
        back_target = f"cat:{cat_row['parent_id']}" if (cat_row and cat_row["parent_id"]) else "cat:root"
        text = f"📁 <b>{title}</b>\n\n" + ("کالایی هنوز ثبت نشده." if not products else "کالای مورد نظر رو انتخاب کن:")
        markup = product_list_keyboard(products, category_id, back_target, allow_bulk=level in ("admin", "special"))

    if edit:
        edit_text(bot_token, chat_id, edit, text, markup)
    else:
        send_text(bot_token, chat_id, text, markup)


def show_root_categories(bot_token, conn, chat_id, level, edit=None):
    show_category_screen(bot_token, conn, chat_id, None, level, edit=edit)


# ---------------------------------------------------------- product queries

def run_single_query(bot_token, conn, chat_id, telegram_id, level, product):
    allowed, denial = access_check_and_consume(conn, level, telegram_id, product)
    if not allowed:
        send_text(bot_token, chat_id, denial)
        return
    try:
        message = build_message(product["name"], product["query"])
    except Exception as e:
        send_text(bot_token, chat_id, f"⚠️ خطا در استعلام «{product['name']}»: {e}")
        return
    send_message_in_parts(bot_token, chat_id, product["name"], message)


def run_bulk_query(bot_token, conn, chat_id, telegram_id, level, category_id):
    if level not in ("admin", "special"):
        send_text(bot_token, chat_id, "این قابلیت فقط برای کاربران ویژه فعاله.")
        return

    products = db.list_products(conn, category_id)
    if not products:
        send_text(bot_token, chat_id, "کالایی توی این دسته‌بندی نیست.")
        return

    skipped = []
    ran = 0
    for product in products:
        allowed, denial = access_check_and_consume(conn, level, telegram_id, product)
        if not allowed:
            skipped.append((product["name"], denial))
            continue
        ran += 1
        try:
            message = build_message(product["name"], product["query"])
            send_message_in_parts(bot_token, chat_id, product["name"], message)
        except Exception as e:
            send_text(bot_token, chat_id, f"⚠️ خطا در استعلام «{product['name']}»: {e}")

    if skipped:
        lines = ["⏳ این کالاها رد شدن (هنوز توی کول‌داون هستن):"]
        for name, msg in skipped:
            lines.append(f"• {name}")
        send_text(bot_token, chat_id, "\n".join(lines))

    if ran == 0 and not skipped:
        send_text(bot_token, chat_id, "استعلامی انجام نشد.")


# ------------------------------------------------------------- admin panel

def handle_admin_command(conn, text: str) -> str:
    text = text.strip()

    if text in ("/start", "/help"):
        return HELP_TEXT

    if text == "/list":
        products = db.list_products(conn)
        if not products:
            return "لیست خالیه."
        lines = []
        for p in products:
            cat = db.get_category_by_id(conn, p["category_id"]) if p["category_id"] else None
            cat_label = cat["title"] if cat else "—"
            lines.append(f"{p['id']}. {p['name']}  ({p['query']})  [{cat_label}]")
        return "\n".join(lines)

    if text.startswith("/addcategory"):
        arg = text[len("/addcategory"):].strip()
        parts = [x.strip() for x in arg.split("|")]
        if len(parts) < 2:
            return "فرمت درست: /addcategory <slug> | <title> | [parent-slug]"
        slug, title = parts[0], parts[1]
        parent_slug = parts[2] if len(parts) > 2 and parts[2] else None
        try:
            db.add_category(conn, slug, title, parent_slug)
        except ValueError as e:
            return str(e)
        return f"دسته‌بندی ثبت شد: {title} ({slug})" + (f" زیرِ {parent_slug}" if parent_slug else "")

    if text == "/categories":
        tops = db.list_categories(conn)
        if not tops:
            return "دسته‌بندی‌ای ثبت نشده."
        lines = []
        for top in tops:
            lines.append(f"📁 {top['title']} ({top['slug']})")
            for child in db.list_categories(conn, parent_id=top["id"]):
                lines.append(f"   └ {child['title']} ({child['slug']})")
        return "\n".join(lines)

    if text.startswith("/add"):
        arg = text[len("/add"):].strip()
        if not arg:
            return "فرمت درست: /add <name> | <query> | [category-slug]"
        parts = [x.strip() for x in arg.split("|")]
        if len(parts) == 1:
            name = query = parts[0]
            category_slug = None
        else:
            name, query = parts[0], parts[1]
            category_slug = parts[2] if len(parts) > 2 and parts[2] else None
        if db.get_product_by_name(conn, name):
            return f"«{name}» از قبل توی لیست هست."
        try:
            db.add_product(conn, name, query, category_slug)
        except ValueError as e:
            return str(e)
        return f"اضافه شد: {name}  (سرچ: {query})" + (f"  [{category_slug}]" if category_slug else "")

    if text.startswith("/remove"):
        name = text[len("/remove"):].strip()
        if not name:
            return "فرمت درست: /remove <name>"
        if db.remove_product(conn, name):
            return f"حذف شد: {name}"
        return f"«{name}» توی لیست پیدا نشد."

    if text.startswith("/setlevel"):
        arg = text[len("/setlevel"):].strip()
        parts = arg.split()
        if len(parts) != 2 or parts[1] not in ("admin", "special", "regular"):
            return "فرمت درست: /setlevel <telegram_id> <admin|special|regular>"
        try:
            target_id = int(parts[0])
        except ValueError:
            return "آیدی عددی تلگرام معتبر نیست."
        db.get_or_create_user(conn, target_id)
        db.set_user_level(conn, target_id, parts[1])
        return f"سطح دسترسی {target_id} شد: {parts[1]}"

    if text == "/users":
        users = db.list_users(conn)
        if not users:
            return "هیچ کاربری هنوز ثبت نشده."
        return "\n".join(f"{u['telegram_id']} — {u['level']}" for u in users)

    return "دستور نامشخص. /help رو بزن."


# ------------------------------------------------------------- update loop

def handle_message(bot_token, conn, admin_id, required_channels, message):
    sender_id = message.get("from", {}).get("id")
    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if sender_id is None:
        return

    if sender_id == admin_id and text.startswith("/") and text not in ("/start", "/help"):
        reply = handle_admin_command(conn, text)
        send_text(bot_token, chat_id, reply)
        return

    if text in ("/start", "/help"):
        if not check_membership(bot_token, required_channels, sender_id):
            send_text(bot_token, chat_id, not_member_text(required_channels), join_keyboard(required_channels))
            return
        level = user_level(conn, sender_id, admin_id)
        send_text(bot_token, chat_id, welcome_text(), main_menu_keyboard())
        return

    send_text(bot_token, chat_id, "برای شروع /start رو بزن.")


def handle_callback(bot_token, conn, admin_id, required_channels, callback_query):
    data = callback_query.get("data", "")
    sender_id = callback_query["from"]["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    answer_callback(bot_token, callback_query["id"])

    if data == "noop":
        return

    if data == "checkjoin":
        if check_membership(bot_token, required_channels, sender_id):
            level = user_level(conn, sender_id, admin_id)
            edit_text(bot_token, chat_id, message_id, welcome_text(), main_menu_keyboard())
        else:
            edit_text(bot_token, chat_id, message_id, not_member_text(required_channels), join_keyboard(required_channels))
        return

    if not check_membership(bot_token, required_channels, sender_id):
        edit_text(bot_token, chat_id, message_id, not_member_text(required_channels), join_keyboard(required_channels))
        return

    level = user_level(conn, sender_id, admin_id)

    if data == "main":
        edit_text(bot_token, chat_id, message_id, welcome_text(), main_menu_keyboard())
        return

    if data == "about":
        edit_text(bot_token, chat_id, message_id, ABOUT_TEXT, kb([[btn("🔙 منوی اصلی", "main")]]))
        return

    if data == "cat:root":
        show_root_categories(bot_token, conn, chat_id, level, edit=message_id)
        return

    if data.startswith("cat:"):
        cat_id = int(data.split(":", 1)[1])
        show_category_screen(bot_token, conn, chat_id, cat_id, level, edit=message_id)
        return

    if data.startswith("prod:"):
        product_id = int(data.split(":", 1)[1])
        product = db.get_product(conn, product_id)
        if product is None:
            send_text(bot_token, chat_id, "این کالا دیگه موجود نیست.")
            return
        run_single_query(bot_token, conn, chat_id, sender_id, level, product)
        return

    if data.startswith("bulk:"):
        cat_id = int(data.split(":", 1)[1])
        run_bulk_query(bot_token, conn, chat_id, sender_id, level, cat_id)
        return


def process_update(bot_token, conn, admin_id, required_channels, update):
    if "message" in update:
        handle_message(bot_token, conn, admin_id, required_channels, update["message"])
    elif "callback_query" in update:
        handle_callback(bot_token, conn, admin_id, required_channels, update["callback_query"])


def main():
    bot_token = os.environ["BOT_TOKEN"]
    admin_id = int(os.environ["ADMIN_ID"])
    required_channels = [
        c.strip() for c in os.environ.get("REQUIRED_CHANNELS", "@bazarpricedaily").split(",") if c.strip()
    ]

    conn = db.get_conn()
    db.init_db(conn)

    offset = int(db.get_meta(conn, "update_offset", "0"))
    print(f"[bot] starting, offset={offset}, required_channels={required_channels}")

    while True:
        try:
            resp = telegram_get(bot_token, "getUpdates", offset=offset, timeout=POLL_TIMEOUT)
            updates = resp.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    process_update(bot_token, conn, admin_id, required_channels, update)
                except Exception:
                    traceback.print_exc()
                db.set_meta(conn, "update_offset", str(offset))
        except requests.RequestException as e:
            print(f"[bot] network error, retrying in 5s: {e}")
            time.sleep(5)
        except KeyboardInterrupt:
            print("[bot] stopped")
            break


if __name__ == "__main__":
    sys.exit(main())
