# price-bot

بات تلگرامی که هر روز ساعت ۱۴:۰۰ و ۱۸:۰۰ (وقت تهران) قیمت کالاهای `products.json` رو
سرچ می‌کنه و به کانال `@bazarpricedaily` می‌فرسته.

فعلاً فقط **دیجی‌کالا** به عنوان منبع فعاله. Torob و Emalls بعداً اضافه می‌شن
(کد در `sources.py` طوری نوشته شده که اضافه کردنشون فقط یعنی یک تابع جدید).

## راه‌اندازی (یک‌بار)

توی تنظیمات این ریپو، Settings → Secrets and variables → Actions → New repository secret،
این سه‌تا رو اضافه کن:

| نام | مقدار |
|---|---|
| `BOT_TOKEN` | توکن بات از BotFather |
| `CHANNEL_ID` | `@bazarpricedaily` |
| `ADMIN_ID` | آیدی عددی تلگرامت |

## تست دستی

تب **Actions** → workflow `Daily price report` یا `Admin backoffice` → **Run workflow**.
اولین اجرا رو حتماً دستی بزن و لاگش رو چک کن — مخصوصاً برای Digikala، چون تبدیل
قیمت (ریال به تومان، تقسیم بر ۱۰) و ساختار دقیق API بدون یک اجرای واقعی قابل تایید
صددرصد نیست.

## بک‌آفیس (اضافه/حذف کالا)

توی چت خصوصی با بات (نه کانال) این دستورات رو بفرست:

```
/add Philips NA230 | philips na230
/add اسم و سرچ یکسان
/remove Philips NA230
/list
```

هر ۱۰ دقیقه یک‌بار `Admin backoffice` این پیام‌ها رو چک می‌کنه و `products.json`
رو آپدیت می‌کنه. برای تست فوری، به‌جای صبر کردن، از تب Actions همون workflow رو
دستی Run کن.
