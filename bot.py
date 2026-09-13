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
- /create          -> admin-only product creation wizard (name, photos,
                       video, description, price, stock)
- /reset           -> cancel the product creation wizard at any point

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
from dataclasses import dataclass, field
from datetime import datetime, timezone

import cloudinary
import cloudinary.uploader
from aiohttp import web
from dotenv import load_dotenv
from supabase import create_client, Client
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
    WebAppInfo,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
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

# Telegram usernames (without the leading "@") allowed to run /create.
# Can be overridden via the ADMIN_USERNAMES env var, comma-separated.
ADMIN_USERNAMES = {
    u.strip().lstrip("@").lower()
    for u in os.environ.get("ADMIN_USERNAMES", "xha.studio,lavaflows11").split(",")
    if u.strip()
}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def is_admin(update: Update) -> bool:
    username = (update.effective_user.username or "").lower()
    return username in ADMIN_USERNAMES


# ---------------------------------------------------------------------------
# Supabase — this is what the webapp reads product metadata from (name,
# price, description, stock, photo/video URLs).
# ---------------------------------------------------------------------------
# 1. Create a project at https://supabase.com
# 2. In the SQL editor, create a "products" table, e.g.:
#
#      create table products (
#        id bigint primary key,
#        name text not null,
#        description text,
#        price numeric not null default 0,
#        stock integer not null default 0,
#        photos jsonb not null default '[]'::jsonb,
#        video text,
#        created_at timestamptz not null default now()
#      );
#
#    (Enable Row Level Security and add a public read-only policy if the
#    webapp reads directly with the anon key; the bot itself should use the
#    service_role key so it can bypass RLS to write.)
# 3. Project Settings -> API -> copy the Project URL and the service_role
#    (or anon, if you only need read/write via RLS policies) key into
#    SUPABASE_URL / SUPABASE_KEY below.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

db: Client | None = None
if SUPABASE_URL and SUPABASE_KEY:
    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    logger.info("Supabase initialized (media hosted on Cloudinary)")
else:
    logger.warning(
        "SUPABASE_URL / SUPABASE_KEY not set — products will NOT be synced to the webapp."
    )


# ---------------------------------------------------------------------------
# Cloudinary — permanent hosting for product photos/videos.
# Sign up free at https://cloudinary.com, then Dashboard -> copy Cloud name,
# API Key, API Secret into these env vars. Free tier, no card, and URLs
# never break on redeploy (unlike a local file store, which gets wiped
# whenever the host's container restarts).
# ---------------------------------------------------------------------------
CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

cloudinary_ready = bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)
if cloudinary_ready:
    cloudinary.config(
        cloud_name=CLOUDINARY_CLOUD_NAME,
        api_key=CLOUDINARY_API_KEY,
        api_secret=CLOUDINARY_API_SECRET,
        secure=True,
    )
    logger.info("Cloudinary configured (cloud_name=%s)", CLOUDINARY_CLOUD_NAME)
else:
    logger.warning("Cloudinary env vars not set — product photos/videos will NOT be uploaded.")


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


ORDERS: dict[int, PendingOrder] = {}
_next_order_id = 1


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
        meta = f" ({item['meta']})" if item.get("meta") else ""
        lines.append(f"- {item['name']}{meta} x{item['quantity']}")
    total = order.get("total", 0)
    lines.append(f"\nTotal: {total:,.0f} ကျပ်")
    lines.append(f"Payment: {payment_label(order.get('payment', ''))}")
    customer = order.get("customer", {})
    lines.append(f"Name: {customer.get('name', '-')}")
    lines.append(f"Phone: {customer.get('phone', '-')}")
    lines.append(f"Address: {customer.get('address', '-')}")
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
# In-memory product catalog (admin-created)
# ---------------------------------------------------------------------------
# NOTE: like ORDERS, this resets on restart. Swap for a real database/JSON
# file if you need products to survive restarts (the Supabase "products"
# table is the source of truth for the webapp regardless).
@dataclass
class Product:
    product_id: int
    name: str
    photo_file_ids: list = field(default_factory=list)
    video_file_id: str | None = None
    description: str = ""
    price: float = 0.0
    stock: int = 0


PRODUCTS: dict[int, Product] = {}
_next_product_id = 1


def _new_product_id() -> int:
    global _next_product_id
    pid = _next_product_id
    _next_product_id += 1
    return pid


def admin_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton("🆕 Create new product")]],
        resize_keyboard=True,
    )


async def store_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str, resource_type: str) -> str | None:
    """Downloads a Telegram file and uploads it to Cloudinary, returning a permanent https:// URL."""
    if not cloudinary_ready:
        return None
    tg_file = await context.bot.get_file(file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())
    result = cloudinary.uploader.upload(
        file_bytes,
        resource_type=resource_type,  # "image" or "video"
        folder="soneyay-products",
    )
    return result.get("secure_url")


# ---------------------------------------------------------------------------
# /create conversation states
# ---------------------------------------------------------------------------
ASK_NAME, ASK_PHOTOS, ASK_VIDEO, ASK_DESCRIPTION, ASK_PRICE, ASK_STOCK = range(6)


async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update):
        await update.message.reply_text("🚫 You are not allowed to use this command.")
        return ConversationHandler.END

    context.user_data["new_product"] = {"photos": []}
    await update.message.reply_text(
        "🆕 Let's create a new product.\n\nWhat is the product *name*?",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_NAME


async def create_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_product", None)
    await update.message.reply_text(
        "🔄 Product creation has been reset.",
        reply_markup=admin_menu_keyboard() if is_admin(update) else ReplyKeyboardRemove(),
    )
    return ConversationHandler.END


async def ask_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_product"]["name"] = update.message.text.strip()
    await update.message.reply_text(
        "📸 Now send the product *photos*.\n"
        "You can send several, one at a time. When you're done, type /done.",
        parse_mode="Markdown",
    )
    return ASK_PHOTOS


async def ask_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        context.user_data["new_product"]["photos"].append(file_id)
        count = len(context.user_data["new_product"]["photos"])
        await update.message.reply_text(f"✅ Photo {count} saved. Send another, or type /done.")
        return ASK_PHOTOS

    await update.message.reply_text("Please send a photo, or type /done when finished.")
    return ASK_PHOTOS


async def photos_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not context.user_data["new_product"]["photos"]:
        await update.message.reply_text("You haven't sent any photos yet. Send at least one, or type /skip.")
        return ASK_PHOTOS

    await update.message.reply_text(
        "🎥 Now send a *video* for the product (optional).\n"
        "Send the video, or type /skip to skip this step.",
        parse_mode="Markdown",
    )
    return ASK_VIDEO


async def ask_video(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.video:
        context.user_data["new_product"]["video"] = update.message.video.file_id
    await update.message.reply_text("📝 Please send a short *description* for this product.", parse_mode="Markdown")
    return ASK_DESCRIPTION


async def video_skip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_product"]["video"] = None
    await update.message.reply_text("📝 Please send a short *description* for this product.", parse_mode="Markdown")
    return ASK_DESCRIPTION


async def ask_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text("💰 What is the *price*? (numbers only, e.g. 25000)", parse_mode="Markdown")
    return ASK_PRICE


async def ask_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", "")
    try:
        price = float(raw)
    except ValueError:
        await update.message.reply_text("That doesn't look like a number. Please send the price again, e.g. 25000.")
        return ASK_PRICE

    context.user_data["new_product"]["price"] = price
    await update.message.reply_text("📦 How many *stocks* (quantity) are available?", parse_mode="Markdown")
    return ASK_STOCK


async def ask_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip().replace(",", "")
    try:
        stock = int(raw)
    except ValueError:
        await update.message.reply_text("That doesn't look like a whole number. Please send the stock count again, e.g. 10.")
        return ASK_STOCK

    data = context.user_data["new_product"]
    data["stock"] = stock

    product_id = _new_product_id()
    product = Product(
        product_id=product_id,
        name=data["name"],
        photo_file_ids=data["photos"],
        video_file_id=data.get("video"),
        description=data["description"],
        price=data["price"],
        stock=stock,
    )
    PRODUCTS[product_id] = product
    context.user_data.pop("new_product", None)

    photo_urls: list[str] = []
    video_url: str | None = None
    for file_id in product.photo_file_ids:
        url = await store_telegram_file(context, file_id, "image")
        if url:
            photo_urls.append(url)
    if product.video_file_id:
        video_url = await store_telegram_file(context, product.video_file_id, "video")

    if db is not None:
        try:
            db.table("products").upsert(
                {
                    "id": product_id,
                    "name": product.name,
                    "description": product.description,
                    "price": product.price,
                    "stock": product.stock,
                    "photos": photo_urls,
                    "video": video_url,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).execute()
        except Exception:
            logger.exception("Failed to sync product #%s to Supabase", product_id)

    summary = (
        f"✅ Product #{product_id} created!\n\n"
        f"Name: {product.name}\n"
        f"Photos: {len(product.photo_file_ids)}\n"
        f"Video: {'Yes' if product.video_file_id else 'No'}\n"
        f"Description: {product.description}\n"
        f"Price: {product.price:,.0f} ကျပ်\n"
        f"Stock: {product.stock}\n\n"
        + ("🔥 Synced to the webapp." if db is not None else "⚠️ Supabase not configured — this product is NOT visible on the webapp.")
    )

    if product.photo_file_ids:
        await update.message.reply_photo(photo=product.photo_file_ids[0], caption=summary)
    else:
        await update.message.reply_text(summary)

    await update.message.reply_text(
        "You can add another product with /create, or reset anytime with /reset.",
        reply_markup=admin_menu_keyboard(),
    )
    return ConversationHandler.END


create_conversation = ConversationHandler(
    entry_points=[
        CommandHandler("create", create_start),
        MessageHandler(filters.Regex("^🆕 Create new product$"), create_start),
    ],
    states={
        ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_name)],
        ASK_PHOTOS: [
            CommandHandler("done", photos_done),
            MessageHandler(filters.PHOTO, ask_photos),
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_photos),
        ],
        ASK_VIDEO: [
            CommandHandler("skip", video_skip),
            MessageHandler(filters.VIDEO, ask_video),
        ],
        ASK_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_description)],
        ASK_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_price)],
        ASK_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_stock)],
    },
    fallbacks=[CommandHandler("reset", create_reset)],
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
    if is_admin(update):
        await update.message.reply_text(
            "👋 Admin menu:",
            reply_markup=admin_menu_keyboard(),
        )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your chat ID is: {update.effective_chat.id}")


async def reset_standalone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Lets /reset work even when the user isn't inside the /create conversation.
    context.user_data.pop("new_product", None)
    await update.message.reply_text(
        "🔄 Nothing to reset, but okay!",
        reply_markup=admin_menu_keyboard() if is_admin(update) else ReplyKeyboardRemove(),
    )


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
    except Exception:
        logger.exception("Bad /api/order payload")
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)

    order_id = _new_order_id()
    payment = order.get("payment", "COD")
    pending = PendingOrder(order_id, telegram_user_id, username, order)
    ORDERS[order_id] = pending
    logger.info("New order #%s from %s (%s): %s", order_id, username, telegram_user_id, payment)

    try:
        if payment == "COD":
            await bot.send_message(chat_id=telegram_user_id, text=format_order_text(order))
            if ADMIN_CHAT_ID:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"#{order_id} New COD order from @{username} (id: {telegram_user_id})\n\n"
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
                    f"{format_order_text(order, header='🧾 Order Received — Payment Pending')}\n\n"
                    f"Transferred to: {payment}: {wallet_name} — {wallet_number}\n\n"
                    f"Our team will review your payment and confirm shortly."
                ),
            )
            caption = (
                f"#{order_id} New {payment} order from @{username} (id: {telegram_user_id})\n\n"
                + format_order_text(pending.order, header="🧾 Order Awaiting Approval")
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
                        text=caption + "\n\n(No screenshot attached)",
                        reply_markup=_review_keyboard(order_id),
                    )
    except Exception as exc:
        logger.exception("Failed to deliver order #%s to Telegram", order_id)
        return web.json_response({"ok": False, "error": str(exc)}, status=502)

    return web.json_response({"ok": True, "order_id": order_id})


async def health(request: web.Request) -> web.Response:
    # Simple 200 for Render's (or any PaaS's) health check / uptime pings.
    return web.json_response({"ok": True})


async def start_web_server(application: Application) -> None:
    app = web.Application(middlewares=[cors_middleware])
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
    application.add_handler(create_conversation)
    application.add_handler(CommandHandler("reset", reset_standalone))
    application.add_handler(
        CallbackQueryHandler(handle_review_decision, pattern=r"^(approve|reject):\d+$")
    )

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
