# price-bot

بات تلگرامی قیمت که از چند منبع (دیجی‌کالا، ترب و ...) قیمت کالاها رو جمع می‌کنه.
دو بخش داره:

1. **گزارش روزانه** — هر روز ساعت ۱۴:۰۰ و ۱۸:۰۰ (وقت تهران) قیمت کالاهای ثبت‌شده رو
   به کانال `@bazarpricedaily` می‌فرسته.
2. **ربات تعاملی** (`bot.py`) — منوی دسته‌بندی، استعلام قیمت لحظه‌ای با دکمه، سطوح
   دسترسی (ادمین/ویژه/عادی)، و پنل مدیریت از طریق دستورات تلگرام.

فعلاً فقط **دیجی‌کالا** و **ترب** به عنوان منبع فعالن. Emalls بعداً اضافه می‌شه
(کد در `sources.py` طوری نوشته شده که اضافه کردنش فقط یعنی یک تابع جدید).

## چرا دو تا سیستم موازی هست؟

بخش تعاملی (دکمه‌ها، منوها) نیاز داره ربات لحظه‌ای جواب بده — این با کرون هر
۱۰ دقیقه‌ی GitHub Actions ممکن نیست (کاربر باید تا ۱۰ دقیقه صبر کنه). برای همین
`bot.py` باید یک پروسه‌ی همیشه‌روشن روی یک VPS باشه (`systemd`، بدون نیاز به دامنه
یا webhook — از long-polling استفاده می‌کنه). چون این پروسه به یک پایگاه‌داده‌ی
زنده نیاز داره (کول‌داون‌ها و سهمیه‌های ماهانه که مدام تغییر می‌کنن)، از یک فایل
SQLite محلی (`bot.db`) استفاده می‌کنه، نه `products.json` کامیت‌شده توی گیت.

تا وقتی VPS آماده نشده، سیستم قدیمی (`scraper.py` + `admin_bot.py` +
`products.json` روی GitHub Actions) دست‌نخورده و کار می‌کنه — چیزی خراب نشده.
وقتی VPS راه افتاد، طبق مراحل زیر مهاجرت کن و بعدش می‌تونی workflowهای
`report.yml`/`admin.yml` رو غیرفعال/حذف کنی.

## راه‌اندازی روی VPS (برای فعال کردن ربات تعاملی)

```bash
git clone <repo-url> /opt/price-bot
cd /opt/price-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

فایل تنظیمات محرمانه رو بساز (خارج از گیت، فقط قابل خوندن توسط روت):

```bash
sudo tee /etc/price-bot.env > /dev/null << 'EOF2'
BOT_TOKEN=توکن-بات-از-BotFather
ADMIN_ID=آیدی-عددی-تلگرامت
CHANNEL_ID=@bazarpricedaily
REQUIRED_CHANNELS=@bazarpricedaily
PRICE_BOT_DB=/opt/price-bot/bot.db
EOF2
sudo chmod 600 /etc/price-bot.env
```

**مهم:** ربات باید توی کانال(های) `REQUIRED_CHANNELS` عضو باشه (ترجیحاً ادمین)،
وگرنه چک عضویت کاربرها (`getChatMember`) خطا می‌ده.

پایگاه‌داده رو یک‌بار بساز و کالاهای فعلی `products.json` رو وارد کن:

```bash
PRICE_BOT_DB=/opt/price-bot/bot.db python3 migrate.py
```

سرویس‌های systemd رو نصب کن:

```bash
sudo cp deploy/price-bot-bot.service deploy/price-bot-report.service deploy/price-bot-report.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now price-bot-bot.service
sudo systemctl enable --now price-bot-report.timer
```

بررسی وضعیت / لاگ:

```bash
systemctl status price-bot-bot.service
journalctl -u price-bot-bot.service -f
systemctl list-timers price-bot-report.timer
```

بعد از هر `git pull` روی VPS:

```bash
sudo systemctl restart price-bot-bot.service
```

## سطوح دسترسی و محدودیت‌ها

| سطح | محدودیت |
|---|---|
| ادمین (تو) | نامحدود، بدون هیچ محدودیتی |
| کاربر ویژه (مشترک) | تعداد کالا نامحدود؛ هر کالای مشخص برای یک کاربر هر ۳ ساعت یک‌بار قابل استعلامه؛ استعلام گروهی کل یک دسته‌بندی فقط برای همین سطحه |
| کاربر عادی | ماهانه ۱۰ استعلام، از هر دسته‌بندی که بخواد؛ در اول هر ماه (میلادی/UTC) ریست می‌شه |

همه‌ی کاربرها (ادمین هم همینطور) قبل از استفاده باید عضو کانال(های)
`REQUIRED_CHANNELS` باشن؛ ربات این رو موقع هر تعامل چک می‌کنه.

پلن‌های خرید اشتراک (تبدیل کاربر عادی به ویژه به‌صورت خودکار/پولی) هنوز
معماری نشده — فعلاً سطح‌ها فقط دستی توسط ادمین تنظیم می‌شن (`/setlevel`).

## دستورات ادمین (توی چت خصوصی با ربات، نه کانال)

```
/add <name> | <query> | [category-slug]   افزودن کالا
/remove <name>                             حذف کالا
/list                                      لیست کالاها
/addcategory <slug> | <title> | [parent-slug]   افزودن دسته‌بندی
/categories                                نمایش درخت دسته‌بندی‌ها
/setlevel <telegram_id> <admin|special|regular>  تعیین سطح دسترسی کاربر
/users                                     لیست کاربران و سطح‌شون
/help                                      همین راهنما
```

دسته‌بندی‌های پیش‌فرض (ساخته‌شده توسط `migrate.py`): «لوازم خانگی» به عنوان
دسته‌ی والد، با زیردسته‌های «هواپز» و «دستگاه اسپرسو». زیردسته‌های بیشتر رو
با `/addcategory` اضافه کن.

## تست دستی منطق (بدون نیاز به تلگرام واقعی)

چون این ساندباکس به `api.telegram.org` هم دسترسی نداره (مثل سایت‌های
فروشگاهی، بلاک شده)، همه‌ی منطق دسترسی/کول‌داون/سهمیه/ناوبری منو با
تست‌های واحد روی یک دیتابیس درون‌حافظه‌ای تأیید شده، نه با اجرای واقعی روی
تلگرام. اولین تست واقعی رو حتماً خودت با یک آپدیت تلگرام واقعی (یا از تب
Actions با `workflow_dispatch` برای بخش قدیمی) انجام بده.

---

## (قدیمی) سیستم مبتنی بر GitHub Actions

تا وقتی VPS راه نیفتاده، این بخش هنوز فعاله:

### راه‌اندازی (یک‌بار)

توی تنظیمات این ریپو، Settings → Secrets and variables → Actions → New repository secret،
این سه‌تا رو اضافه کن:

| نام | مقدار |
|---|---|
| `BOT_TOKEN` | توکن بات از BotFather |
| `CHANNEL_ID` | `@bazarpricedaily` |
| `ADMIN_ID` | آیدی عددی تلگرامت |

### تست دستی

تب **Actions** → workflow `Daily price report` یا `Admin backoffice` → **Run workflow**.

### بک‌آفیس قدیمی (اضافه/حذف کالا از products.json)

توی چت خصوصی با بات این دستورات رو بفرست: `/add`, `/remove`, `/list`.
هر ۱۰ دقیقه یک‌بار `Admin backoffice` این پیام‌ها رو چک می‌کنه.

**وقتی VPS راه افتاد و از `bot.py` مطمئن شدی**، می‌تونی `report.yml`،
`admin.yml`، `admin_bot.py`، `products.json` و `admin_state.json` رو حذف کنی
— جایگزین‌شون `daily_report.py` + `bot.py` + `bot.db` هستن.
