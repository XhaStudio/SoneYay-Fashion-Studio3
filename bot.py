"""
ဖက်ရှင်ဆိုင် Telegram Bot
--------------------------
- /start           -> ကြိုဆိုစာ + ဆိုင်ရဲ့ Web App ကိုဖွင့်ပေးမယ့် ခလုတ်
- /whoami          -> chat ID ကို ပြန်ပြောပေးမယ် (ADMIN_CHAT_ID သတ်မှတ်ဖို့ သုံးပါ)
- HTTP API         -> Web App ကနေ POST /api/order ကို တိုက်ရိုက် ခေါ်ပါတယ် (fetch
                       သုံးထားပါတယ်၊ Telegram ရဲ့ sendData မဟုတ်ပါ) — ဒါမှသာ Mini App
                       window က ပိတ်မသွားဘဲ "ပို့ပြီးပါပြီ" animation ကို
                       မိမိကိုယ်တိုင် ပြနိုင်မှာဖြစ်ပါတယ် (Telegram က အလိုအလျောက်
                       ပိတ်ခံရမည့်အစား)။
                         * COD အော်ဒါများကို ချက်ချင်း အတည်ပြုပြီး admin ဆီ
                           ပို့ပေးပါတယ်။
                         * KBZPay/WavePay အော်ဒါများ (screenshot ပါလျှင်ပါ) ကို
                           admin ဆီ approve/reject လုပ်ဖို့ ပို့ပေးပါတယ်။
- Approve/Reject   -> admin က ခလုတ်နှိပ်လိုက်ရင် customer ဆီ အကြောင်းကြားပြီး
                       order message ကို update လုပ်ပေးပါတယ်။

Setup လုပ်နည်း:
    ၁. pip install -r requirements.txt
    ၂. .env ဖိုင်ကို ဖြည့်ပါ (BOT_TOKEN, ADMIN_CHAT_ID, wallet အချက်အလက်များ, API_PORT)
    ၃. Run ရန်: python bot.py
    ၄. API_PORT ကို internet ပေါ်က HTTPS နဲ့ ဝင်ရောက်နိုင်အောင် စီစဉ်ပါ (ဥပမာ
       reverse proxy/nginx + TLS certificate, ဒါမှမဟုတ် host ရဲ့ built-in HTTPS
       proxy) ပြီးရင် အဲဒီ public URL ကို web app ရဲ့ app.js ထဲက API_BASE_URL
       မှာ ထည့်ပါ။ Browser တွေက https:// page ကနေ http:// ကို ခေါ်လို့ မရပါ။
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass

from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# ချိန်ညှိချက်များ (Config)
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
SHOP_URL = os.environ.get(
    "SHOP_URL", "https://xhastudio.github.io/SoneYay-Fashion-Studio2/"
)
# Render (နဲ့ PaaS host အများစု) က public port ကို PORT ဆိုတဲ့ env var ကနေ
# အလိုအလျောက် သတ်မှတ်ပေးပါတယ် — app က ဒီ port ကိုပဲ listen လုပ်ရပါမယ်
# (fixed number မသုံးရပါ) — ဒါမှ public https://xxxx.onrender.com URL က
# တကယ် ရောက်နိုင်မှာပါ။
API_PORT = int(os.environ.get("PORT", os.environ.get("API_PORT", "8080")))
# API ကို ခေါ်ခွင့်ပြုမယ့် origin များ (comma-separated)။ စမ်းနေတုန်း "*"
# ထားလို့ရပေမယ့် တကယ် launch လုပ်ခင်မှာ မင်းရဲ့ shop origin အစစ်ကို
# ကန့်သတ်ပေးပါ။
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")

KBZPAY_NAME = os.environ.get("KBZPAY_NAME", "")
KBZPAY_NUMBER = os.environ.get("KBZPAY_NUMBER", "")
WAVEPAY_NAME = os.environ.get("WAVEPAY_NAME", "")
WAVEPAY_NUMBER = os.environ.get("WAVEPAY_NUMBER", "")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Order များကို memory ထဲမှာ ခေတ္တသိမ်းထားခြင်း
# ---------------------------------------------------------------------------
# မှတ်ချက်: bot ကို restart ချလိုက်တိုင်း ဒီ data တွေ ပျောက်သွားပါမယ်။
# Restart ပြီးလည်း order history ကို ဆက်ထားချင်ရင် SQLite/Postgres လို
# database အစစ်တစ်ခုခုနဲ့ အစားထိုးရပါလိမ့်မယ်။
@dataclass
class PendingOrder:
    order_id: int
    user_id: int
    username: str
    order: dict
    status: str = "pending_review"  # pending_review | approved | rejected


ORDERS: dict[int, PendingOrder] = {}
_next_order_id = 1


def _new_order_id() -> int:
    global _next_order_id
    oid = _next_order_id
    _next_order_id += 1
    return oid


def payment_label(code: str) -> str:
    return {
        "COD": "ငွေချေပြီးမှလက်ခံရန် (COD)",
        "KBZPay": "KBZPay",
        "WavePay": "WavePay",
    }.get(code, code or "-")


def format_order_text(order: dict, header: str = "🛍️ မှာယူမှု အတည်ပြုပြီး!") -> str:
    lines = [f"{header}\n"]
    for item in order.get("items", []):
        meta = f" ({item['meta']})" if item.get("meta") else ""
        lines.append(f"- {item['name']}{meta} x{item['quantity']}")
    total = order.get("total", 0)
    lines.append(f"\nစုစုပေါင်း: {total:,.0f} ကျပ်")
    lines.append(f"ငွေပေးချေမှု: {payment_label(order.get('payment', ''))}")
    customer = order.get("customer", {})
    lines.append(f"အမည်: {customer.get('name', '-')}")
    lines.append(f"ဖုန်းနံပါတ်: {customer.get('phone', '-')}")
    lines.append(f"လိပ်စာ: {customer.get('address', '-')}")
    return "\n".join(lines)


def _review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ အတည်ပြုမည်", callback_data=f"approve:{order_id}"),
                InlineKeyboardButton("❌ ပယ်ချမည်", callback_data=f"reject:{order_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Telegram handler များ
# ---------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [
            InlineKeyboardButton(
                text="Open Shop Web App 🛍️🏪",
                web_app=WebAppInfo(url=SHOP_URL),
            )
        ]
    ]
    await update.message.reply_text(
        "ကျွန်ုပ်တို့ ဖက်ရှင်ဆိုင်မှ ကြိုဆိုပါတယ်! ပစ္စည်းများကြည့်ရှုရန် အောက်က ခလုတ်ကို နှိပ်ပါ:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"မင်းရဲ့ chat ID က: {update.effective_chat.id}")


async def handle_review_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, order_id_str = query.data.split(":", 1)
    order_id = int(order_id_str)
    pending = ORDERS.get(order_id)

    if pending is None:
        await query.edit_message_text("ဒီ order ကို ရှာမတွေ့တော့ပါ။")
        return

    if action == "approve":
        pending.status = "approved"
        await context.bot.send_message(
            chat_id=pending.user_id,
            text=f"✅ မင်းရဲ့ order #{order_id} ကို အတည်ပြုပြီးပါပြီ! မကြာမီ ပို့ဆောင်ပေးပါမယ်။",
        )
        result_text = f"#{order_id} ✅ အတည်ပြုပြီး"
    else:
        pending.status = "rejected"
        await context.bot.send_message(
            chat_id=pending.user_id,
            text=(
                f"❌ မင်းရဲ့ order #{order_id} ကို အတည်ပြု၍မရပါ။ "
                f"ကျေးဇူးပြု၍ ဆက်သွယ်ပါ (သို့) ပြန်လည် မှာယူကြည့်ပါ။"
            ),
        )
        result_text = f"#{order_id} ❌ ပယ်ချပြီး"

    # Admin ရဲ့ message ကို update လုပ်ခြင်း (text နှင့် photo caption နှစ်မျိုးစလုံးအတွက် အလုပ်လုပ်ပါတယ်)။
    if query.message.photo:
        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n{result_text}", reply_markup=None
        )
    else:
        await query.edit_message_text(
            text=f"{query.message.text}\n\n{result_text}", reply_markup=None
        )


# ---------------------------------------------------------------------------
# HTTP API (Web App ကနေ sendData မဟုတ်ဘဲ fetch နဲ့ တိုက်ရိုက် ခေါ်တာပါ)
# ---------------------------------------------------------------------------
@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        response = web.Response()
    else:
        response = await handler(request)
    response.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


async def api_order(request: web.Request) -> web.Response:
    bot = request.app["bot"]
    try:
        data = await request.post()
        order = json.loads(data["order"])
        telegram_user_id = int(data["telegram_user_id"])
        username = data.get("username") or str(telegram_user_id)
        photo_field = data.get("photo")  # aiohttp FileField, ဒါမှမဟုတ် None
    except Exception:
        logger.exception("/api/order ကို data ပုံစံမှားနေတယ်")
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    order_id = _new_order_id()
    payment = order.get("payment", "COD")
    pending = PendingOrder(order_id, telegram_user_id, username, order)
    ORDERS[order_id] = pending
    logger.info("Order အသစ် #%s လာသည် — %s (%s): %s", order_id, username, telegram_user_id, payment)

    try:
        if payment == "COD":
            await bot.send_message(chat_id=telegram_user_id, text=format_order_text(order))
            if ADMIN_CHAT_ID:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"#{order_id} COD Order အသစ် — @{username} (id: {telegram_user_id})\n\n"
                        + format_order_text(order)
                    ),
                    reply_markup=_review_keyboard(order_id),
                )
        else:
            wallet_name = KBZPAY_NAME if payment == "KBZPay" else WAVEPAY_NAME
            wallet_number = KBZPAY_NUMBER if payment == "KBZPay" else WAVEPAY_NUMBER
            await bot.send_message(
                chat_id=telegram_user_id,
                text=(
                    f"{format_order_text(order, header='🧾 Order လက်ခံရရှိပြီး — ငွေပေးချေမှု စောင့်ဆိုင်းနေသည်')}\n\n"
                    f"လွှဲပေးရမည့် အကောင့်: {payment}: {wallet_name} — {wallet_number}\n\n"
                    f"ကျွန်ုပ်တို့ အသင်းက ငွေလွှဲမှုကို စစ်ဆေးပြီး မကြာမီ အတည်ပြုပေးပါမယ်။"
                ),
            )
            caption = (
                f"#{order_id} {payment} Order အသစ် — @{username} (id: {telegram_user_id})\n\n"
                + format_order_text(pending.order, header="🧾 အတည်ပြုရန် စောင့်နေသော Order")
            )
            if ADMIN_CHAT_ID:
                if photo_field is not None and hasattr(photo_field, "file"):
                    photo_bytes = photo_field.file.read()
                    await bot.send_photo(
                        chat_id=ADMIN_CHAT_ID,
                        photo=photo_bytes,
                        caption=caption,
                        reply_markup=_review_keyboard(order_id),
                    )
                else:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=caption + "\n\n(Screenshot ပါမလာပါ)",
                        reply_markup=_review_keyboard(order_id),
                    )
    except Exception as exc:
        logger.exception("Order #%s ကို Telegram ဆီ ပို့လို့မရပါ", order_id)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "order_id": order_id})


async def health_check(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "fashion-shop-bot"})


async def start_web_server(application: Application) -> None:
    app = web.Application(middlewares=[cors_middleware])
    app["bot"] = application.bot
    app.router.add_get("/", health_check)
    app.router.add_post("/api/order", api_order)
    app.router.add_route("OPTIONS", "/api/order", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    application.bot_data["web_runner"] = runner  # reference ကို အသက်ဝင်နေအောင် ထားရန်
    logger.info("HTTP API ကို 0.0.0.0:%s မှာ listen လုပ်နေပါပြီ", API_PORT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("ကျေးဇူးပြု၍ .env ဖိုင်ထဲမှာ BOT_TOKEN ကို ထည့်ပါ။")

    # Python 3.14 က "loop မရှိရင် အလိုအလျောက် ဖန်တီးပေးမယ်" ဆိုတဲ့ အရင်
    # asyncio.get_event_loop() ရဲ့ fallback behavior ကို ဖြုတ်လိုက်ပါတယ်။
    # python-telegram-bot 21.x ရဲ့ run_polling() က ဒီ fallback ကို
    # အားကိုးနေတာမို့ — 3.14 မှာလည်း အလုပ်ဖြစ်အောင် loop ကို ကိုယ်တိုင်
    # ဖန်တီးပြီး သတ်မှတ်ပေးလိုက်ပါတယ်။
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = (
        Application.builder().token(BOT_TOKEN).post_init(start_web_server).build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(
        CallbackQueryHandler(handle_review_decision, pattern=r"^(approve|reject):\d+$")
    )

    logger.info("Bot စတင်နေပါပြီ...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
