"""
Backoffice: polls Telegram getUpdates for commands from the admin only,
edits products.json accordingly. Run on a schedule (see
.github/workflows/admin.yml); the workflow commits any resulting changes.

Env vars required: BOT_TOKEN, ADMIN_ID
Commands (sent as a private message to the bot, not the channel):
  /add <name> | <query>   e.g. "/add Philips NA230 | philips na230"
  /add <query>             uses the same text as both name and query
  /remove <name>
  /list
  /help
"""

import json
import os

import requests

STATE_FILE = "admin_state.json"
PRODUCTS_FILE = "products.json"

HELP_TEXT = (
    "دستورات:\n"
    "/add <name> | <query> — افزودن کالا (مثال: /add Philips NA230 | philips na230)\n"
    "/add <query> — افزودن کالا با نام یکسان برای نام و سرچ\n"
    "/remove <name> — حذف کالا\n"
    "/list — نمایش لیست کالاها"
)


def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


def telegram_get(method: str, bot_token: str, **params):
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def telegram_post(method: str, bot_token: str, **payload):
    url = f"https://api.telegram.org/bot{bot_token}/{method}"
    resp = requests.post(url, json=payload, timeout=20)
    resp.raise_for_status()
    return resp.json()


def reply(bot_token: str, chat_id, text: str):
    telegram_post("sendMessage", bot_token, chat_id=chat_id, text=text)


def handle_command(text: str, products: list) -> str:
    text = text.strip()

    if text in ("/start", "/help"):
        return HELP_TEXT

    if text == "/list":
        if not products:
            return "لیست خالیه."
        lines = [f"{i+1}. {p['name']}  ({p['query']})" for i, p in enumerate(products)]
        return "\n".join(lines)

    if text.startswith("/add"):
        arg = text[len("/add"):].strip()
        if not arg:
            return "فرمت درست: /add <name> | <query>"
        if "|" in arg:
            name, query = [part.strip() for part in arg.split("|", 1)]
        else:
            name = query = arg
        if any(p["name"].lower() == name.lower() for p in products):
            return f"«{name}» از قبل توی لیست هست."
        products.append({"name": name, "query": query})
        return f"اضافه شد: {name}  (سرچ: {query})"

    if text.startswith("/remove"):
        name = text[len("/remove"):].strip()
        if not name:
            return "فرمت درست: /remove <name>"
        before = len(products)
        products[:] = [p for p in products if p["name"].lower() != name.lower()]
        if len(products) == before:
            return f"«{name}» توی لیست پیدا نشد."
        return f"حذف شد: {name}"

    return "دستور نامشخص. /help رو بزن."


def main():
    bot_token = os.environ["BOT_TOKEN"]
    admin_id = int(os.environ["ADMIN_ID"])

    state = load_json(STATE_FILE, {"offset": 0})
    products = load_json(PRODUCTS_FILE, [])

    updates = telegram_get(
        "getUpdates", bot_token, offset=state["offset"], timeout=0
    )["result"]

    if not updates:
        print("no new updates")
        return

    changed = False
    for update in updates:
        state["offset"] = update["update_id"] + 1

        message = update.get("message")
        if not message or "text" not in message:
            continue

        sender_id = message.get("from", {}).get("id")
        chat_id = message["chat"]["id"]

        if sender_id != admin_id:
            print(f"ignoring message from unauthorized user {sender_id}")
            continue

        before = json.dumps(products, sort_keys=True)
        reply_text = handle_command(message["text"], products)
        after = json.dumps(products, sort_keys=True)
        if before != after:
            changed = True

        reply(bot_token, chat_id, reply_text)

    save_json(STATE_FILE, state)
    if changed:
        save_json(PRODUCTS_FILE, products)
    print(f"processed {len(updates)} update(s), products changed: {changed}")


if __name__ == "__main__":
    main()
