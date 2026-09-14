"""
Fashion Shop Telegram Bot
--------------------------
- /start           -> welcome message + button that opens the shop Web App
- /whoami          -> replies with your chat ID (use this to set ADMIN_CHAT_ID)
- HTTP API         -> the Web App calls POST /api/order directly (fetch, not
                       Telegram's sendData) so the Mini App window can stay
                       open and show its own "submitted" animation instead of
                       being force-closed by Telegram.
                         * COD orders are confirmed immediately and forwarded
                           to the admin.
                         * KBZPay/WavePay orders (with an optional screenshot
                           upload) are forwarded to the admin for approval.
- Approve/Reject   -> admin taps a button, customer gets notified, and the
                       order message is updated.

Setup:
    1. pip install -r requirements.txt
    2. Fill in the .env file (BOT_TOKEN, ADMIN_CHAT_ID, wallet details, API_PORT)
    3. Run: python bot.py
    4. Make sure API_PORT is reachable over HTTPS from the internet (e.g. via
       a reverse proxy/nginx + TLS cert, or your host's built-in HTTPS proxy)
       and set that public URL as API_BASE_URL in the web app's app.js.
       Browsers refuse to call plain http:// from an https:// page.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

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
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")
SHOP_URL = os.environ.get(
    "SHOP_URL", "https://xhastudio.github.io/SoneYay-Fashion-Studio2/"
)
# Render (and most PaaS hosts) inject PORT automatically and require the app
# to bind to it; API_PORT is kept as a fallback for local/manual runs.
API_PORT = int(os.environ.get("PORT", os.environ.get("API_PORT", "8080")))
# Comma-separated list of origins allowed to call the API. Use "*" while
# testing; lock this to your real shop origin before going live.
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
# ---------------------------------------------------------------------------
# In-memory order tracking
# ---------------------------------------------------------------------------
# NOTE: this resets whenever the bot restarts. Swap for a real database
# (Supabase table/Postgres/etc.) if you need orders to survive restarts.
@dataclass
class PendingOrder:
    order_id: int
    user_id: int
    username: str
    order: dict
    status: str = "pending_review"  # pending_review | approved | rejected
    # Tracks which Telegram messages for this order have actually been sent,
    # so a retried /api/order call for the same client_order_id only retries
    # the step that failed instead of re-sending everything (which is what
    # caused duplicate customer messages before).
    customer_notified: bool = False
    admin_notified: bool = False


ORDERS: dict[int, PendingOrder] = {}
_next_order_id = 1

# Maps a client-generated request id -> the order_id it produced, so that a
# retried /api/order call (e.g. after the customer's connection dropped
# before they saw our response) doesn't create a second order / send a
# duplicate Telegram notification for the same checkout attempt.
SEEN_CLIENT_ORDER_IDS: dict[str, int] = {}


def _new_order_id() -> int:
    global _next_order_id
    oid = _next_order_id
    _next_order_id += 1
    return oid


def payment_label(code: str) -> str:
    return {
        "COD": "Cash on Delivery (COD)",
        "KBZPay": "KBZPay",
        "WavePay": "WavePay",
    }.get(code, code or "-")


def format_order_text(order: dict, header: str = "🛍️ Order Confirmed!") -> str:
    lines = [f"{header}\n"]
    for item in order.get("items", []):
        lines.append(f"Choice: {item.get('meta') or '-'}")
        lines.append(f"Name: {item.get('name', '-')}")
        lines.append(f"Amount: {item.get('quantity', '-')}")
        lines.append("")
    total = order.get("total", 0)
    lines.append(f"Payment: {payment_label(order.get('payment', ''))}")
    customer = order.get("customer", {})
    lines.append(f"Phone: {customer.get('phone', '-')}")
    lines.append(f"Address: {customer.get('address', '-')}")
    lines.append(f"Total: {total:,.0f} ကျပ်")
    return "\n".join(lines)


def _review_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve:{order_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order_id}"),
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Telegram handlers
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
        "Welcome to SoneYay Fashion Studio! Click below to browse products:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your chat ID is: {update.effective_chat.id}")


async def handle_review_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action, order_id_str = query.data.split(":", 1)
    order_id = int(order_id_str)
    pending = ORDERS.get(order_id)

    if pending is None:
        await query.edit_message_text("This order no longer exists.")
        return

    if action == "approve":
        pending.status = "approved"
        await context.bot.send_message(
            chat_id=pending.user_id,
            text=f"✅ Your order #{order_id} has been approved! We'll get it shipped soon.",
        )
        result_text = f"#{order_id} ✅ Approved"
    else:
        pending.status = "rejected"
        await context.bot.send_message(
            chat_id=pending.user_id,
            text=(
                f"❌ Your order #{order_id} could not be confirmed. "
                f"Please contact us or try checking out again."
            ),
        )
        result_text = f"#{order_id} ❌ Rejected"

    # Update the admin's message (works for both text and photo captions).
    if query.message.photo:
        await query.edit_message_caption(
            caption=f"{query.message.caption}\n\n{result_text}", reply_markup=None
        )
    else:
        await query.edit_message_text(
            text=f"{query.message.text}\n\n{result_text}", reply_markup=None
        )


# ---------------------------------------------------------------------------
# HTTP API (called directly from the Web App via fetch, not sendData)
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
        photo_field = data.get("photo")  # aiohttp FileField, or None
        client_order_id = data.get("client_order_id") or None
    except Exception:
        logger.exception("Bad /api/order payload")
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    # If the web app already sent this exact checkout attempt (e.g. it's
    # retrying after "Failed to fetch"), reuse the same order instead of
    # creating a new one. We still fall through below so that any step
    # (customer message / admin message) that didn't actually succeed last
    # time gets retried — but customer_notified/admin_notified stop us from
    # re-sending anything that already went out.
    if client_order_id and client_order_id in SEEN_CLIENT_ORDER_IDS:
        order_id = SEEN_CLIENT_ORDER_IDS[client_order_id]
        pending = ORDERS[order_id]
    else:
        order_id = _new_order_id()
        pending = PendingOrder(order_id, telegram_user_id, username, order)
        ORDERS[order_id] = pending
        if client_order_id:
            SEEN_CLIENT_ORDER_IDS[client_order_id] = order_id
        logger.info(
            "New order #%s from %s (%s): %s",
            order_id, username, telegram_user_id, order.get("payment", "COD"),
        )

    payment = order.get("payment", "COD")

    if photo_field is not None and not hasattr(photo_field, "file"):
        # Helps diagnose "admin gets text instead of the photo" reports:
        # this fires if the "photo" field arrived as a plain string/bytes
        # instead of an uploaded file (e.g. wrong field name, or the
        # request body was cut off by aiohttp's max body size).
        logger.warning(
            "Order #%s: 'photo' field present but not a file upload (type=%s)",
            order_id, type(photo_field),
        )

    try:
        if payment == "COD":
            if not pending.customer_notified:
                await bot.send_message(chat_id=telegram_user_id, text=format_order_text(order))
                pending.customer_notified = True
            if ADMIN_CHAT_ID and not pending.admin_notified:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"#{order_id} New COD order from @{username} (id: {telegram_user_id})\n\n"
                        + format_order_text(order)
                    ),
                    reply_markup=_review_keyboard(order_id),
                )
                pending.admin_notified = True
        else:
            wallet_name = KBZPAY_NAME if payment == "KBZPay" else WAVEPAY_NAME
            wallet_number = KBZPAY_NUMBER if payment == "KBZPay" else WAVEPAY_NUMBER
            if not pending.customer_notified:
                await bot.send_message(
                    chat_id=telegram_user_id,
                    text=(
                        f"{format_order_text(order, header='🧾 Order Received — Payment Pending')}\n\n"
                        f"Transferred to: {payment}: {wallet_name} — {wallet_number}\n\n"
                        f"Our team will review your payment and confirm shortly."
                    ),
                )
                pending.customer_notified = True
            caption = (
                f"#{order_id} New {payment} order from @{username} (id: {telegram_user_id})\n\n"
                + format_order_text(pending.order, header="🧾 Order Awaiting Approval")
            )
            if ADMIN_CHAT_ID and not pending.admin_notified:
                if photo_field is not None and hasattr(photo_field, "file"):
                    photo_bytes = photo_field.file.read()
                    logger.info("Order #%s: forwarding screenshot (%d bytes) to admin", order_id, len(photo_bytes))
                    await bot.send_photo(
                        chat_id=ADMIN_CHAT_ID,
                        photo=photo_bytes,
                        caption=caption,
                        reply_markup=_review_keyboard(order_id),
                    )
                else:
                    await bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=caption + "\n\n(No screenshot attached)",
                        reply_markup=_review_keyboard(order_id),
                    )
                pending.admin_notified = True
    except Exception as exc:
        logger.exception("Failed to deliver order #%s to Telegram", order_id)
        # Note: we deliberately do NOT pop client_order_id here anymore.
        # Keeping it means a retry of this same checkout attempt will
        # resume from whichever step failed, instead of being treated as
        # a brand new order (which is what caused duplicate messages).
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "order_id": order_id})


async def health(request: web.Request) -> web.Response:
    # Simple 200 for Render's (or any PaaS's) health check / uptime pings.
    return web.json_response({"ok": True})


async def start_web_server(application: Application) -> None:
    # Default aiohttp request-body limit is only 1 MB, which many phone
    # payment-screenshot uploads exceed. Raise it so the KBZPay/WavePay
    # screenshot doesn't get rejected before it ever reaches api_order.
    app = web.Application(middlewares=[cors_middleware], client_max_size=20 * 1024 * 1024)
    app["bot"] = application.bot
    app.router.add_get("/", health)
    app.router.add_post("/api/order", api_order)
    app.router.add_route("OPTIONS", "/api/order", lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", API_PORT)
    await site.start()
    application.bot_data["web_runner"] = runner  # keep a reference alive
    logger.info("HTTP API listening on 0.0.0.0:%s", API_PORT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN in your .env file.")

    application = (
        Application.builder().token(BOT_TOKEN).post_init(start_web_server).build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(
        CallbackQueryHandler(handle_review_decision, pattern=r"^(approve|reject):\d+$")
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
