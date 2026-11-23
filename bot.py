import asyncio
import html
import io
import json
import logging
import os
import random
import sqlite3
import string
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import qrcode
from aiohttp import web
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ---------------------------------------------------------------------------
# Configuration (Environment Variables)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "0000").strip()
DB_PATH = os.getenv("DATABASE_PATH", "degit_store.db").strip()

DEFAULT_FORCE_CHANNEL = os.getenv("FORCE_JOIN_CHANNEL", "").strip()  # e.g. @yourchannel or -1001234567890
DEFAULT_FORCE_LINK = os.getenv("FORCE_JOIN_LINK", "").strip()        # e.g. https://t.me/yourchannel
DEFAULT_PAYMENT_CHANNEL = os.getenv("PAYMENT_CHANNEL_ID", "").strip()# e.g. -1001234567890 or @paymentchannel
DEFAULT_UPI_ID = os.getenv("ADMIN_UPI_ID", "yourupi@oksbi").strip()
DEFAULT_REF_COIN = int(os.getenv("PER_REFERRAL_COIN", "1"))

DEVELOPER_USERNAME = "@ai_brain_box_2"
SUPPORT_USERNAME = "@ai_brain_box_2"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("DegitStoreBot")

# In-memory sessions & conversation states
user_states: Dict[int, Dict[str, Any]] = {}
admin_sessions: set[int] = set()
last_failed_notif_users: Dict[int, List[Dict[str, Any]]] = {}

# Main Menu Button Labels
BTN_FREE_PRODUCTS = "Free Products"
BTN_PAID_PRODUCTS = "Paid Products"
BTN_REF_STATUS = "Referral Status"
BTN_PURCHASE_HIST = "Purchase History"
BTN_REF_HIST = "Referral History"
BTN_WALLET = "Wallet"
BTN_DEVELOPER = "Developer"
BTN_SUPPORT = "Help Support"

USER_MENU_BUTTONS = {
    BTN_FREE_PRODUCTS,
    BTN_PAID_PRODUCTS,
    BTN_REF_STATUS,
    BTN_PURCHASE_HIST,
    BTN_REF_HIST,
    BTN_WALLET,
    BTN_DEVELOPER,
    BTN_SUPPORT,
}

# Admin Menu Button Labels
ADM_ADD_PROD = "➕ Add Product"
ADM_EDIT_PROD = "✏️ Edit Products"
ADM_REF_LB = "🏆 Referral Leaderboard"
ADM_EDIT_BAL = "⚖️ Edit Balance"
ADM_DASHBOARD = "📊 Dashboard"
ADM_MONEY_HIST = "💵 Add Money History"
ADM_SEND_BONUS = "🎁 Send Bonus"
ADM_SEND_NOTIF = "📢 Send Notification"
ADM_PAY_REQS = "💳 Payment Requests"
ADM_ADD_COUPON = "🎟 Add Coupon Code"
ADM_MANAGE_CODES = "🛠 Manage Codes"
ADM_BAL_LB = "💰 Balance Leaderboard"
ADM_USERS = "👥 Users"
ADM_SETTINGS = "⚙️ Bot Settings"
ADM_EXIT = "🔙 Main Menu"

ADMIN_MENU_BUTTONS = {
    ADM_ADD_PROD,
    ADM_EDIT_PROD,
    ADM_REF_LB,
    ADM_EDIT_BAL,
    ADM_DASHBOARD,
    ADM_MONEY_HIST,
    ADM_SEND_BONUS,
    ADM_SEND_NOTIF,
    ADM_PAY_REQS,
    ADM_ADD_COUPON,
    ADM_MANAGE_CODES,
    ADM_BAL_LB,
    ADM_USERS,
    ADM_SETTINGS,
    ADM_EXIT,
}


# ---------------------------------------------------------------------------
# Helpers & Database
# ---------------------------------------------------------------------------
def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def format_size(size_bytes: Optional[int]) -> str:
    if not size_bytes or size_bytes <= 0:
        return "Unknown"
    units = ["B", "KB", "MB", "GB"]
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size_bytes} B"


def detect_file_type(file_name: Optional[str], mime_type: Optional[str] = None) -> str:
    if file_name and "." in file_name:
        ext = file_name.rsplit(".", 1)[-1].strip().upper()
        if ext:
            return ext
    if mime_type:
        return mime_type.split("/")[-1].upper()
    return "OTHER"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                rs_balance REAL DEFAULT 0.0,
                coins INTEGER DEFAULT 0,
                bonus_rs REAL DEFAULT 0.0,
                bonus_coins INTEGER DEFAULT 0,
                referred_by INTEGER DEFAULT NULL,
                referral_rewarded INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL CHECK(type IN ('free', 'paid')),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL,
                images_json TEXT NOT NULL DEFAULT '[]',
                files_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER,
                product_type TEXT NOT NULL,
                product_title TEXT NOT NULL,
                price REAL NOT NULL,
                files_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                coins_earned INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS payment_requests (
                order_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                username TEXT DEFAULT '',
                full_name TEXT DEFAULT '',
                amount REAL NOT NULL,
                screenshot_file_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                channel_chat_id TEXT DEFAULT '',
                channel_message_id INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS coupons (
                code TEXT PRIMARY KEY,
                currency TEXT NOT NULL CHECK(currency IN ('rs', 'coin')),
                amount REAL NOT NULL,
                max_claims INTEGER NOT NULL,
                claims_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS coupon_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                claimed_at TEXT NOT NULL,
                UNIQUE(code, user_id)
            );

            CREATE TABLE IF NOT EXISTS bonus_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL,
                reason TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );
            """
        )

        defaults = {
            "force_channel": DEFAULT_FORCE_CHANNEL,
            "force_link": DEFAULT_FORCE_LINK,
            "payment_channel": DEFAULT_PAYMENT_CHANNEL,
            "upi_id": DEFAULT_UPI_ID,
            "ref_coin": str(DEFAULT_REF_COIN),
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def upsert_user(user, referred_by: Optional[int] = None) -> sqlite3.Row:
    username = user.username or ""
    full_name = user.full_name or "User"
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE user_id = ?", (user.id,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
                (username, full_name, user.id),
            )
            conn.commit()
            return conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()
        else:
            valid_ref = None
            if referred_by and referred_by != user.id:
                ref_row = conn.execute(
                    "SELECT user_id FROM users WHERE user_id = ?", (referred_by,)
                ).fetchone()
                if ref_row:
                    valid_ref = referred_by
            conn.execute(
                """
                INSERT INTO users (user_id, username, full_name, referred_by, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (user.id, username, full_name, valid_ref, utc_now_str()),
            )
            conn.commit()
            return conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user.id,)
            ).fetchone()


def get_user(user_id: int) -> Optional[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def format_user_label(row: sqlite3.Row) -> str:
    uname = row["username"]
    fname = row["full_name"] or "User"
    if uname:
        return f"@{html.escape(uname)}"
    return f"{html.escape(fname)} ({row['user_id']})"


def user_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_FREE_PRODUCTS), KeyboardButton(BTN_PAID_PRODUCTS)],
        [KeyboardButton(BTN_REF_STATUS)],
        [KeyboardButton(BTN_PURCHASE_HIST), KeyboardButton(BTN_REF_HIST)],
        [KeyboardButton(BTN_WALLET)],
        [KeyboardButton(BTN_DEVELOPER), KeyboardButton(BTN_SUPPORT)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def admin_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(ADM_ADD_PROD), KeyboardButton(ADM_EDIT_PROD)],
        [KeyboardButton(ADM_REF_LB), KeyboardButton(ADM_EDIT_BAL)],
        [KeyboardButton(ADM_DASHBOARD), KeyboardButton(ADM_MONEY_HIST)],
        [KeyboardButton(ADM_SEND_BONUS), KeyboardButton(ADM_SEND_NOTIF)],
        [KeyboardButton(ADM_PAY_REQS), KeyboardButton(ADM_ADD_COUPON)],
        [KeyboardButton(ADM_MANAGE_CODES), KeyboardButton(ADM_BAL_LB)],
        [KeyboardButton(ADM_USERS), KeyboardButton(ADM_SETTINGS)],
        [KeyboardButton(ADM_EXIT)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def is_user_joined_channel(bot, user_id: int) -> bool:
    force_channel = get_setting("force_channel", "").strip()
    if not force_channel:
        return True
    try:
        chat_id: Any = int(force_channel) if force_channel.lstrip("-").isdigit() else force_channel
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        )
    except TelegramError as e:
        logger.warning("Channel check error for %s: %s", force_channel, e)
        return False


def build_force_join_markup() -> InlineKeyboardMarkup:
    force_link = get_setting("force_link", "").strip()
    force_channel = get_setting("force_channel", "").strip()
    if not force_link and force_channel.startswith("@"):
        force_link = f"https://t.me/{force_channel.lstrip('@')}"
    buttons = []
    if force_link:
        buttons.append([InlineKeyboardButton("📢 Join Channel / Group", url=force_link)])
    buttons.append([InlineKeyboardButton("✅ Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(buttons)


async def process_referral_reward(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    per_ref_coin = int(get_setting("ref_coin", "1") or "1")
    with get_db() as conn:
        u = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if not u or not u["referred_by"] or u["referral_rewarded"]:
            return
        referrer_id = u["referred_by"]
        now_str = utc_now_str()
        conn.execute(
            "UPDATE users SET coins = coins + ? WHERE user_id = ?",
            (per_ref_coin, referrer_id),
        )
        conn.execute(
            "UPDATE users SET referral_rewarded = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            "INSERT OR IGNORE INTO referrals (referrer_id, referred_id, coins_earned, created_at) "
            "VALUES (?, ?, ?, ?)",
            (referrer_id, user_id, per_ref_coin, now_str),
        )
        conn.commit()

    ref_label = f"@{html.escape(u['username'])}" if u["username"] else html.escape(u["full_name"] or str(user_id))
    try:
        await context.bot.send_message(
            chat_id=referrer_id,
            text=(
                f"🎉 <b>New Referral Joined!</b>\n\n"
                f"🪙 <b>{per_ref_coin} Coin</b> has been added to your wallet!\n"
                f"👤 <b>Referral User:</b> {ref_label}\n"
                f"🕒 <b>Join Time:</b> {now_str} UTC"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        pass


def generate_upi_qr_bytes(upi_id: str, amount: float, order_id: str) -> io.BytesIO:
    upi_uri = (
        f"upi://pay?pa={upi_id}&pn=DegitStoreBoT&am={amount:.2f}"
        f"&tn={order_id}&cu=INR"
    )
    img = qrcode.make(upi_uri)
    bio = io.BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return bio


async def send_product_files(bot, chat_id: int, product_title: str, files: List[Dict[str, Any]], is_paid: bool = False) -> None:
    lines = [f"📦 <b>{html.escape(product_title)}</b>\n"]
    for idx, f in enumerate(files, 1):
        fname = html.escape(f.get("file_name") or f"file_{idx}")
        fsize = format_size(f.get("file_size"))
        ftype = detect_file_type(f.get("file_name"), f.get("mime_type"))
        if is_paid:
            lines.append(f"{idx}. 📄 <b>{fname}</b> | 📦 {fsize} | 🏷 Type: <b>{ftype}</b>")
        else:
            lines.append(f"{idx}. 📄 <b>{fname}</b> | 📦 {fsize}")

    await bot.send_message(
        chat_id=chat_id,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )

    for f in files:
        tg_type = f.get("tg_type", "document")
        fid = f["file_id"]
        try:
            if tg_type == "photo":
                await bot.send_photo(chat_id=chat_id, photo=fid)
            elif tg_type == "video":
                await bot.send_video(chat_id=chat_id, video=fid)
            elif tg_type == "audio":
                await bot.send_audio(chat_id=chat_id, audio=fid)
            else:
                await bot.send_document(chat_id=chat_id, document=fid)
        except TelegramError as e:
            logger.error("Failed to send file %s: %s", fid, e)


# ---------------------------------------------------------------------------
# User Commands & Verification
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    user_states.pop(user.id, None)

    referred_by = None
    if context.args:
        arg = context.args[0].strip()
        if arg.isdigit():
            referred_by = int(arg)

    upsert_user(user, referred_by=referred_by)

    force_channel = get_setting("force_channel", "").strip()
    if force_channel:
        joined = await is_user_joined_channel(context.bot, user.id)
        if not joined:
            await update.message.reply_text(
                f"👋 Welcome to <b>Degit Store BoT</b>, {html.escape(user.first_name or 'User')}!\n\n"
                f"🔒 To access the store, please join our official channel/group first and then click <b>Verify</b>.",
                reply_markup=build_force_join_markup(),
                parse_mode=ParseMode.HTML,
            )
            return

    with get_db() as conn:
        conn.execute("UPDATE users SET verified = 1 WHERE user_id = ?", (user.id,))
        conn.commit()
    await process_referral_reward(context, user.id)

    await update.message.reply_text(
        f"🎉 Welcome to <b>Degit Store BoT</b>, {html.escape(user.first_name or 'User')}!\n\n"
        f"Use the menu below to browse Free & Paid products, manage your Wallet, or track your Referrals.",
        reply_markup=user_main_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def cb_verify_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    user = query.from_user
    upsert_user(user)

    joined = await is_user_joined_channel(context.bot, user.id)
    if not joined:
        await query.answer("❌ You haven't joined the channel/group yet!", show_alert=True)
        return

    await query.answer("✅ Verified successfully!")
    with get_db() as conn:
        conn.execute("UPDATE users SET verified = 1 WHERE user_id = ?", (user.id,))
        conn.commit()
    await process_referral_reward(context, user.id)

    try:
        await query.message.delete()
    except TelegramError:
        pass

    await context.bot.send_message(
        chat_id=user.id,
        text=(
            f"✅ <b>Verification Successful!</b>\n\n"
            f"Welcome to <b>Degit Store BoT</b>! Choose an option from the menu below."
        ),
        reply_markup=user_main_keyboard(),
        parse_mode=ParseMode.HTML,
    )


async def ensure_verified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if not user:
        return False
    upsert_user(user)
    if not await is_user_joined_channel(context.bot, user.id):
        msg = update.effective_message
        if msg:
            await msg.reply_text(
                "🔒 Please join our channel/group first and click <b>Verify</b>.",
                reply_markup=build_force_join_markup(),
                parse_mode=ParseMode.HTML,
            )
        return False
    return True


# ---------------------------------------------------------------------------
# User Menu Feature Handlers
# ---------------------------------------------------------------------------
async def show_free_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as conn:
        products = conn.execute(
            "SELECT * FROM products WHERE type = 'free' ORDER BY id DESC"
        ).fetchall()

    if not products:
        await update.message.reply_text("🆓 No Free Products available right now.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"🎁 {p['title']} — 🪙 {int(p['price'])} Coins",
                callback_data=f"fprod_view:{p['id']}",
            )
        ]
        for p in products
    ]
    await update.message.reply_text(
        "🆓 <b>Free Products</b>\nSelect a product below:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def show_paid_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_db() as conn:
        products = conn.execute(
            "SELECT * FROM products WHERE type = 'paid' ORDER BY id DESC"
        ).fetchall()

    if not products:
        await update.message.reply_text("💎 No Paid Products available right now.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"💎 {p['title']} — ₹{p['price']:.2f}",
                callback_data=f"pprod_view:{p['id']}",
            )
        ]
        for p in products
    ]
    await update.message.reply_text(
        "💎 <b>Paid Products</b>\nSelect a product below:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


async def send_referral_status(bot, chat_id: int, user_id: int) -> None:
    per_ref_coin = int(get_setting("ref_coin", "1") or "1")
    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(coins_earned), 0) AS total_coins "
            "FROM referrals WHERE referrer_id = ?",
            (user_id,),
        ).fetchone()
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start={user_id}"

    await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🤝 <b>Referral Status</b>\n\n"
            f"👥 <b>Total Referrals:</b> {row['cnt']}\n"
            f"🪙 <b>Total Coins Earned:</b> {row['total_coins']} Coins\n"
            f"🎁 <b>Per Referral Reward:</b> {per_ref_coin} Coin(s)\n\n"
            f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>\n\n"
            f"Share this link with friends to earn coins!"
        ),
        parse_mode=ParseMode.HTML,
    )


async def show_purchase_history_prompt(update: Update) -> None:
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("💎 Paid Products", callback_data="phist:paid"),
                InlineKeyboardButton("🆓 Free Products", callback_data="phist:free"),
            ]
        ]
    )
    await update.message.reply_text(
        "🧾 <b>Purchase History</b>\nSelect a category:",
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


async def show_referral_history(update: Update) -> None:
    user_id = update.effective_user.id
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT r.coins_earned, r.created_at, u.user_id, u.username, u.full_name
            FROM referrals r
            JOIN users u ON u.user_id = r.referred_id
            WHERE r.referrer_id = ?
            ORDER BY r.id DESC
            """,
            (user_id,),
        ).fetchall()

    if not rows:
        await update.message.reply_text("📜 You haven't referred any users yet.")
        return

    lines = ["📜 <b>Referral History</b>\n"]
    for idx, r in enumerate(rows[:50], 1):
        label = format_user_label(r)
        lines.append(
            f"{idx}. {label} — 🪙 +{r['coins_earned']} Coin(s)\n"
            f"   🕒 {r['created_at']} UTC"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def show_wallet(update: Update) -> None:
    user = get_user(update.effective_user.id)
    if not user:
        return
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("➕ Add Rs", callback_data="add_rs"),
                InlineKeyboardButton("🎟 Add Coupon Code", callback_data="redeem_coupon"),
            ]
        ]
    )
    await update.message.reply_text(
        f"👛 <b>Your Wallet</b>\n\n"
        f"💵 <b>Total Rs Balance:</b> ₹{user['rs_balance']:.2f}\n"
        f"🪙 <b>Total Coin Balance:</b> {user['coins']} Coins\n\n"
        f"🎁 <b>Bonus Coins:</b> {user['bonus_coins']} Coins\n"
        f"🎁 <b>Bonus Rs:</b> ₹{user['bonus_rs']:.2f}",
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Product & Wallet Callback Handlers
# ---------------------------------------------------------------------------
async def cb_product_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    data = query.data
    user_id = query.from_user.id

    if data.startswith("fprod_view:") or data.startswith("pprod_view:"):
        is_paid = data.startswith("pprod_view:")
        prod_id = int(data.split(":", 1)[1])
        with get_db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (prod_id,)).fetchone()
        if not p:
            await query.answer("Product not found!", show_alert=True)
            return
        await query.answer()

        images = json.loads(p["images_json"])
        price_str = f"₹{p['price']:.2f}" if is_paid else f"🪙 {int(p['price'])} Coins"
        caption = (
            f"<b>{html.escape(p['title'])}</b>\n\n"
            f"{html.escape(p['description'])}\n\n"
            f"💰 <b>Price:</b> {price_str}"
        )
        if is_paid:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🛒 Buy", callback_data=f"pprod_buy:{prod_id}"),
                        InlineKeyboardButton("➕ Add Rs", callback_data="add_rs"),
                    ]
                ]
            )
        else:
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🛒 Buy", callback_data=f"fprod_buy:{prod_id}"),
                        InlineKeyboardButton("🪙 Earn Coins", callback_data="earn_coins"),
                    ]
                ]
            )

        chat_id = query.message.chat_id
        if len(images) == 1:
            await context.bot.send_photo(
                chat_id=chat_id,
                photo=images[0],
                caption=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        elif len(images) > 1:
            media = [InputMediaPhoto(media=img) for img in images[:10]]
            await context.bot.send_media_group(chat_id=chat_id, media=media)
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        return

    if data.startswith("fprod_buy:") or data.startswith("pprod_buy:"):
        is_paid = data.startswith("pprod_buy:")
        prod_id = int(data.split(":", 1)[1])
        with get_db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (prod_id,)).fetchone()
        user = get_user(user_id)
        if not p or not user:
            await query.answer("Product or user not found!", show_alert=True)
            return
        await query.answer()

        if is_paid:
            bal = float(user["rs_balance"])
            price = float(p["price"])
            rem = bal - price
            text = (
                f"🛒 <b>Confirm Paid Purchase</b>\n\n"
                f"📦 <b>Product:</b> {html.escape(p['title'])}\n"
                f"💰 <b>Price:</b> ₹{price:.2f}\n"
                f"👛 <b>Your Balance:</b> ₹{bal:.2f}\n"
                f"📉 <b>Balance After Purchase:</b> ₹{rem:.2f}"
            )
            conf_cb = f"pprod_conf:{prod_id}"
        else:
            bal = int(user["coins"])
            price = int(p["price"])
            rem = bal - price
            text = (
                f"🛒 <b>Confirm Free Purchase</b>\n\n"
                f"📦 <b>Product:</b> {html.escape(p['title'])}\n"
                f"🪙 <b>Price:</b> {price} Coins\n"
                f"👛 <b>Your Coin Balance:</b> {bal} Coins\n"
                f"📉 <b>Balance After Purchase:</b> {rem} Coins"
            )
            conf_cb = f"fprod_conf:{prod_id}"

        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data=conf_cb),
                    InlineKeyboardButton("❌ Cancel", callback_data="cancel_buy"),
                ]
            ]
        )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "cancel_buy":
        await query.answer("Cancelled")
        try:
            await query.message.delete()
        except TelegramError:
            pass
        return

    if data.startswith("fprod_conf:") or data.startswith("pprod_conf:"):
        is_paid = data.startswith("pprod_conf:")
        prod_id = int(data.split(":", 1)[1])

        with get_db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (prod_id,)).fetchone()
            user = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
            if not p or not user:
                await query.answer("Product not found!", show_alert=True)
                return

            if is_paid:
                if float(user["rs_balance"]) < float(p["price"]):
                    await query.answer("❌ Insufficient Rs balance! Please Add Rs.", show_alert=True)
                    return
                conn.execute(
                    "UPDATE users SET rs_balance = rs_balance - ? WHERE user_id = ?",
                    (float(p["price"]), user_id),
                )
            else:
                if int(user["coins"]) < int(p["price"]):
                    await query.answer("❌ Insufficient Coins! Refer friends to earn coins.", show_alert=True)
                    return
                conn.execute(
                    "UPDATE users SET coins = coins - ? WHERE user_id = ?",
                    (int(p["price"]), user_id),
                )

            conn.execute(
                """
                INSERT INTO purchases (user_id, product_id, product_type, product_title, price, files_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    p["id"],
                    "paid" if is_paid else "free",
                    p["title"],
                    float(p["price"]),
                    p["files_json"],
                    utc_now_str(),
                ),
            )
            conn.commit()

        await query.answer("✅ Purchase successful!")
        try:
            await query.message.edit_text(
                f"✅ <b>Purchase Completed!</b>\nSending files for <b>{html.escape(p['title'])}</b>...",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

        files = json.loads(p["files_json"])
        await send_product_files(
            context.bot, query.message.chat_id, p["title"], files, is_paid=is_paid
        )
        return

    if data == "earn_coins":
        await query.answer()
        await send_referral_status(context.bot, query.message.chat_id, user_id)
        return

    if data == "add_rs":
        await query.answer()
        user_states[user_id] = {"step": "add_rs_amount"}
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="💵 <b>Add Rs to Wallet</b>\n\nPlease enter the amount (in ₹) you want to add:",
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "redeem_coupon":
        await query.answer()
        user_states[user_id] = {"step": "redeem_coupon_code"}
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="🎟 <b>Redeem Coupon Code</b>\n\nPlease enter your coupon code:",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("phist:"):
        ptype = data.split(":", 1)[1]
        await query.answer()
        with get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM purchases WHERE user_id = ? AND product_type = ? ORDER BY id DESC LIMIT 30",
                (user_id, ptype),
            ).fetchall()
        if not rows:
            await context.bot.send_message(
                chat_id=query.message.chat_id,
                text=f"🧾 No {ptype} product purchases found.",
            )
            return

        lines = [f"🧾 <b>{ptype.capitalize()} Purchase History</b>\n"]
        buttons = []
        for idx, r in enumerate(rows, 1):
            files = json.loads(r["files_json"])
            fnames = ", ".join(html.escape(f.get("file_name") or "File") for f in files) or "No file"
            total_sz = format_size(sum(int(f.get("file_size") or 0) for f in files))
            lines.append(
                f"{idx}. <b>{html.escape(r['product_title'])}</b>\n"
                f"   📄 {fnames} ({total_sz})\n"
                f"   🕒 {r['created_at']} UTC"
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"📥 Download: {r['product_title'][:25]}",
                        callback_data=f"phist_dl:{r['id']}",
                    )
                ]
            )
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons[:15]),
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("phist_dl:"):
        pid = int(data.split(":", 1)[1])
        with get_db() as conn:
            r = conn.execute(
                "SELECT * FROM purchases WHERE id = ? AND user_id = ?", (pid, user_id)
            ).fetchone()
        if not r:
            await query.answer("Purchase record not found!", show_alert=True)
            return
        await query.answer("Sending files...")
        files = json.loads(r["files_json"])
        await send_product_files(
            context.bot,
            query.message.chat_id,
            r["product_title"],
            files,
            is_paid=(r["product_type"] == "paid"),
        )


# ---------------------------------------------------------------------------
# Payment Approval / Rejection (Admin Panel & Payment Channel)
# ---------------------------------------------------------------------------
async def is_authorized_payment_reviewer(context: ContextTypes.DEFAULT_TYPE, user_id: int, chat_id: int) -> bool:
    if user_id in admin_sessions:
        return True
    pay_ch = get_setting("payment_channel", "").strip()
    if pay_ch:
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
                return True
        except TelegramError:
            pass
    return False


async def cb_payment_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    action, order_id = query.data.split(":", 1)
    approved = action == "pay_app"

    if not await is_authorized_payment_reviewer(context, query.from_user.id, query.message.chat_id):
        await query.answer("❌ Only admins can approve or reject payments!", show_alert=True)
        return

    with get_db() as conn:
        req = conn.execute(
            "SELECT * FROM payment_requests WHERE order_id = ?", (order_id,)
        ).fetchone()
        if not req:
            await query.answer("Order not found!", show_alert=True)
            return
        if req["status"] != "pending":
            await query.answer(f"Already processed: {req['status'].upper()}", show_alert=True)
            return

        new_status = "approved" if approved else "rejected"
        now_str = utc_now_str()
        conn.execute(
            "UPDATE payment_requests SET status = ?, processed_at = ? WHERE order_id = ?",
            (new_status, now_str, order_id),
        )
        if approved:
            conn.execute(
                "UPDATE users SET rs_balance = rs_balance + ? WHERE user_id = ?",
                (float(req["amount"]), req["user_id"]),
            )
        conn.commit()

    await query.answer(f"Payment {new_status.upper()}!")

    status_badge = "✅ APPROVED" if approved else "❌ REJECTED"
    user_label = (
        f"@{html.escape(req['username'])}"
        if req["username"]
        else html.escape(req["full_name"] or str(req["user_id"]))
    )
    updated_caption = (
        f"💳 <b>Payment Request</b>\n\n"
        f"🆔 <b>Order ID:</b> <code>{html.escape(order_id)}</code>\n"
        f"👤 <b>User:</b> {user_label} (<code>{req['user_id']}</code>)\n"
        f"💰 <b>Amount:</b> ₹{req['amount']:.2f}\n"
        f"🕒 <b>Time:</b> {req['created_at']} UTC\n"
        f"📌 <b>Status:</b> <b>{status_badge}</b>"
    )

    # Update clicked message
    try:
        if query.message.photo:
            await query.message.edit_caption(caption=updated_caption, parse_mode=ParseMode.HTML)
        else:
            await query.message.edit_text(text=updated_caption, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass

    # Also update payment channel post if approved/rejected from Admin Panel (or vice versa)
    ch_id = req["channel_chat_id"]
    ch_msg_id = req["channel_message_id"]
    if ch_id and ch_msg_id and not (
        str(query.message.chat_id) == str(ch_id) and query.message.message_id == ch_msg_id
    ):
        try:
            await context.bot.edit_message_caption(
                chat_id=int(ch_id) if ch_id.lstrip("-").isdigit() else ch_id,
                message_id=int(ch_msg_id),
                caption=updated_caption,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass

    # Notify user
    try:
        if approved:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=(
                    f"✅ <b>Payment Approved!</b>\n\n"
                    f"🆔 <b>Order ID:</b> <code>{html.escape(order_id)}</code>\n"
                    f"💰 <b>₹{req['amount']:.2f}</b> has been added to your wallet!"
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=req["user_id"],
                text=(
                    f"❌ <b>Payment Rejected</b>\n\n"
                    f"🆔 <b>Order ID:</b> <code>{html.escape(order_id)}</code>\n"
                    f"💰 <b>Amount:</b> ₹{req['amount']:.2f}\n"
                    f"Please contact {SUPPORT_USERNAME} if you believe this is a mistake."
                ),
                parse_mode=ParseMode.HTML,
            )
    except TelegramError:
        pass


# ---------------------------------------------------------------------------
# Admin Panel Command & Features
# ---------------------------------------------------------------------------
async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return
    if user.id in admin_sessions:
        user_states.pop(user.id, None)
        await update.message.reply_text(
            "🛠 <b>Admin Panel — Degit Store BoT</b>",
            reply_markup=admin_main_keyboard(),
            parse_mode=ParseMode.HTML,
        )
        return
    user_states[user.id] = {"step": "admin_login_password"}
    await update.message.reply_text("🔐 <b>Enter Admin Password:</b>", parse_mode=ParseMode.HTML)


async def show_admin_dashboard(update: Update) -> None:
    since_24h = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        total_bought = conn.execute("SELECT COUNT(*) FROM purchases").fetchone()[0]
        free_bought = conn.execute("SELECT COUNT(*) FROM purchases WHERE product_type = 'free'").fetchone()[0]
        paid_bought = conn.execute("SELECT COUNT(*) FROM purchases WHERE product_type = 'paid'").fetchone()[0]
        approved_pay = conn.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'approved'").fetchone()[0]
        rejected_pay = conn.execute("SELECT COUNT(*) FROM payment_requests WHERE status = 'rejected'").fetchone()[0]
        total_refs = conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0]
        ref_joined_users = conn.execute("SELECT COUNT(DISTINCT referred_id) FROM referrals").fetchone()[0]
        sum_coins, sum_rs, sum_bcoins, sum_brs = conn.execute(
            "SELECT COALESCE(SUM(coins),0), COALESCE(SUM(rs_balance),0), "
            "COALESCE(SUM(bonus_coins),0), COALESCE(SUM(bonus_rs),0) FROM users"
        ).fetchone()
        total_coupons = conn.execute("SELECT COUNT(*) FROM coupons").fetchone()[0]

        u_24h = conn.execute("SELECT COUNT(*) FROM users WHERE created_at >= ?", (since_24h,)).fetchone()[0]
        bal_24h = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM payment_requests WHERE status = 'approved' AND processed_at >= ?",
            (since_24h,),
        ).fetchone()[0]
        ref_24h = conn.execute("SELECT COUNT(*) FROM referrals WHERE created_at >= ?", (since_24h,)).fetchone()[0]

    await update.message.reply_text(
        f"📊 <b>Admin Dashboard</b>\n\n"
        f"👥 <b>Total Users:</b> {total_users}\n"
        f"🛒 <b>Total Products Bought:</b> {total_bought}\n"
        f"   ├ 🆓 Free Products Bought: {free_bought}\n"
        f"   └ 💎 Paid Products Bought: {paid_bought}\n"
        f"✅ <b>Approved Payments:</b> {approved_pay}\n"
        f"❌ <b>Rejected Payments:</b> {rejected_pay}\n"
        f"🤝 <b>Total Referrals:</b> {total_refs}\n"
        f"👤 <b>Total Referral Joined Users:</b> {ref_joined_users}\n"
        f"🪙 <b>Total Coin Balance:</b> {int(sum_coins)} Coins\n"
        f"💵 <b>Total Rs Balance:</b> ₹{sum_rs:.2f}\n"
        f"🎁 <b>Total Bonus Sent:</b> {int(sum_bcoins)} Coins | ₹{sum_brs:.2f}\n"
        f"🎟 <b>Total Coupons Created:</b> {total_coupons}\n\n"
        f"⏱ <b>Last 24 Hours:</b>\n"
        f"   ├ 🆕 Users Joined: {u_24h}\n"
        f"   ├ 💰 Balance Added: ₹{bal_24h:.2f}\n"
        f"   └ 🤝 Referrals: {ref_24h}",
        parse_mode=ParseMode.HTML,
    )


async def show_admin_referral_leaderboard(update: Update) -> None:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT u.user_id, u.username, u.full_name, u.coins,
                   COUNT(r.id) AS ref_count
            FROM users u
            LEFT JOIN referrals r ON r.referrer_id = u.user_id
            GROUP BY u.user_id
            HAVING ref_count > 0
            ORDER BY ref_count DESC, u.coins DESC
            LIMIT 30
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text("🏆 No referrals recorded yet.")
        return

    lines = ["🏆 <b>Referral Leaderboard</b>\n"]
    for idx, r in enumerate(rows, 1):
        label = format_user_label(r)
        lines.append(
            f"{idx}. {label}\n"
            f"   🤝 Referrals: <b>{r['ref_count']}</b>   |   🪙 Coins: <b>{r['coins']}</b>\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def show_admin_add_money_history(update: Update) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payment_requests ORDER BY created_at DESC LIMIT 30"
        ).fetchall()

    if not rows:
        await update.message.reply_text("💵 No add money history found.")
        return

    lines = ["💵 <b>Add Money History (Latest 30)</b>\n"]
    for idx, r in enumerate(rows, 1):
        uname = f"@{html.escape(r['username'])}" if r["username"] else html.escape(r["full_name"] or str(r["user_id"]))
        lines.append(
            f"{idx}. <code>{html.escape(r['order_id'])}</code> | {uname} (<code>{r['user_id']}</code>)\n"
            f"   💰 ₹{r['amount']:.2f} | 📌 {r['status'].upper()} | 🕒 {r['created_at']}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def show_admin_payment_requests(update: Update) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM payment_requests WHERE status = 'pending' ORDER BY created_at ASC LIMIT 20"
        ).fetchall()

    if not rows:
        await update.message.reply_text("💳 No pending payment requests.")
        return

    for r in rows:
        uname = f"@{html.escape(r['username'])}" if r["username"] else html.escape(r["full_name"] or str(r["user_id"]))
        caption = (
            f"💳 <b>Pending Payment Request</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{html.escape(r['order_id'])}</code>\n"
            f"👤 <b>User:</b> {uname} (<code>{r['user_id']}</code>)\n"
            f"💰 <b>Amount:</b> ₹{r['amount']:.2f}\n"
            f"🕒 <b>Time:</b> {r['created_at']} UTC\n"
            f"📌 <b>Status:</b> ⏳ PENDING"
        )
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"pay_app:{r['order_id']}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"pay_rej:{r['order_id']}"),
                ]
            ]
        )
        try:
            await update.message.reply_photo(
                photo=r["screenshot_file_id"],
                caption=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            await update.message.reply_text(
                text=caption,
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )


async def show_admin_manage_codes(update: Update) -> None:
    with get_db() as conn:
        codes = conn.execute("SELECT * FROM coupons ORDER BY created_at DESC").fetchall()
        if not codes:
            await update.message.reply_text("🛠 No coupon codes created yet.")
            return

        for c in codes:
            claimers = conn.execute(
                """
                SELECT u.user_id, u.username, u.full_name
                FROM coupon_claims cc
                JOIN users u ON u.user_id = cc.user_id
                WHERE cc.code = ?
                """,
                (c["code"],),
            ).fetchall()
            claimer_str = ", ".join(format_user_label(u) for u in claimers[:15]) or "None"
            val = f"₹{c['amount']:.2f}" if c["currency"] == "rs" else f"🪙 {int(c['amount'])} Coins"
            rem = max(0, c["max_claims"] - c["claims_count"])
            markup = InlineKeyboardMarkup(
                [[InlineKeyboardButton("🗑 Delete Code", callback_data=f"adm_code_del:{c['code']}")]]
            )
            await update.message.reply_text(
                f"🎟 <b>Code:</b> <code>{html.escape(c['code'])}</code>\n"
                f"💰 <b>Value:</b> {val}\n"
                f"📊 <b>Claimed:</b> {c['claims_count']} / {c['max_claims']} (Remaining: {rem})\n"
                f"👥 <b>Claimed By:</b> {claimer_str}",
                reply_markup=markup,
                parse_mode=ParseMode.HTML,
            )


async def show_admin_balance_leaderboard(update: Update) -> None:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY rs_balance DESC, coins DESC"
        ).fetchall()

    if not rows:
        await update.message.reply_text("💰 No users found.")
        return

    lines = ["💰 <b>Balance Leaderboard (Top 30)</b>\n"]
    for idx, r in enumerate(rows[:30], 1):
        lines.append(
            f"{idx}. {format_user_label(r)} — ₹{r['rs_balance']:.2f} | 🪙 {r['coins']} Coins"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    export_data = [dict(r) for r in rows]
    bio = io.BytesIO(json.dumps(export_data, indent=2).encode("utf-8"))
    bio.name = "balance_leaderboard.json"
    await update.message.reply_document(
        document=bio,
        caption="📄 Full users balance & info JSON file.",
    )


async def send_admin_users_page(bot, chat_id: int, page: int = 0, send_json: bool = False, message_to_edit=None) -> None:
    per_page = 15
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (per_page, page * per_page),
        ).fetchall()
        all_rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall() if send_json else []

    total_pages = max(1, (total + per_page - 1) // per_page)
    lines = [f"👥 <b>All Users (Total: {total}) — Page {page + 1}/{total_pages}</b>\n"]
    for idx, r in enumerate(rows, start=page * per_page + 1):
        lines.append(
            f"{idx}. {format_user_label(r)} | <code>{r['user_id']}</code> | ₹{r['rs_balance']:.2f} | 🪙 {r['coins']}"
        )

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_users_pg:{page - 1}"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"adm_users_pg:{page + 1}"))
    markup = InlineKeyboardMarkup([nav]) if nav else None

    if message_to_edit:
        await message_to_edit.edit_text("\n".join(lines), reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id=chat_id, text="\n".join(lines), reply_markup=markup, parse_mode=ParseMode.HTML)

    if send_json:
        export_data = [dict(r) for r in all_rows]
        bio = io.BytesIO(json.dumps(export_data, indent=2).encode("utf-8"))
        bio.name = "all_users.json"
        await bot.send_document(chat_id=chat_id, document=bio, caption="📄 All Users Info (JSON)")


async def show_admin_settings(update: Update) -> None:
    fc = get_setting("force_channel", "") or "Not Set"
    fl = get_setting("force_link", "") or "Not Set"
    pc = get_setting("payment_channel", "") or "Not Set"
    upi = get_setting("upi_id", "") or "Not Set"
    rc = get_setting("ref_coin", "1")

    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Set Force Join Channel ID/@", callback_data="adm_set:force_channel")],
            [InlineKeyboardButton("🔗 Set Force Join Invite Link", callback_data="adm_set:force_link")],
            [InlineKeyboardButton("💳 Set Payment Log Channel ID/@", callback_data="adm_set:payment_channel")],
            [InlineKeyboardButton("🏦 Set Admin UPI ID", callback_data="adm_set:upi_id")],
            [InlineKeyboardButton("🪙 Set Per Referral Coin", callback_data="adm_set:ref_coin")],
        ]
    )
    await update.message.reply_text(
        f"⚙️ <b>Bot Settings</b>\n\n"
        f"📢 <b>Force Join Channel:</b> <code>{html.escape(fc)}</code>\n"
        f"🔗 <b>Force Join Link:</b> <code>{html.escape(fl)}</code>\n"
        f"💳 <b>Payment Log Channel:</b> <code>{html.escape(pc)}</code>\n"
        f"🏦 <b>Admin UPI ID:</b> <code>{html.escape(upi)}</code>\n"
        f"🪙 <b>Per Referral Coin:</b> {html.escape(rc)}\n\n"
        f"<i>Note: Make sure the bot is an Admin in both channels!</i>",
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


async def show_admin_edit_products(update: Update) -> None:
    with get_db() as conn:
        products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    if not products:
        await update.message.reply_text("✏️ No products found.")
        return

    buttons = [
        [
            InlineKeyboardButton(
                f"{'💎' if p['type']=='paid' else '🆓'} {p['title']}",
                callback_data=f"adm_edit_sel:{p['id']}",
            )
        ]
        for p in products
    ]
    await update.message.reply_text(
        "✏️ <b>Edit Products</b>\nSelect a product to edit or delete:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Admin Callback Handlers
# ---------------------------------------------------------------------------
async def cb_admin_actions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    user_id = query.from_user.id
    if user_id not in admin_sessions:
        await query.answer("❌ Admin session expired. Use /admin to login.", show_alert=True)
        return

    data = query.data
    state = user_states.get(user_id, {})

    if data.startswith("adm_add_type:"):
        ptype = data.split(":", 1)[1]
        user_states[user_id] = {"step": "adm_add_title", "type": ptype, "images": [], "files": []}
        await query.answer()
        await query.message.reply_text(
            f"➕ Adding <b>{ptype.upper()}</b> Product\n\nEnter Product <b>Title</b>:",
            parse_mode=ParseMode.HTML,
        )
        return

    if data in ("adm_add_skip_img", "adm_add_done_img"):
        if state.get("step") != "adm_add_images":
            await query.answer()
            return
        state["step"] = "adm_add_files"
        user_states[user_id] = state
        await query.answer()
        await query.message.reply_text(
            "📁 Now upload Product <b>File(s)</b> (you can send multiple files).\n"
            "When finished, click <b>OK / Save Product</b> below.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ OK / Save Product", callback_data="adm_add_done_file")]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if data == "adm_add_done_file":
        if state.get("step") != "adm_add_files":
            await query.answer()
            return
        if not state.get("files"):
            await query.answer("❌ Please upload at least 1 file!", show_alert=True)
            return

        await query.answer("Saving product...")
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO products (type, title, description, price, images_json, files_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    state["type"],
                    state["title"],
                    state["description"],
                    float(state["price"]),
                    json.dumps(state["images"]),
                    json.dumps(state["files"]),
                    utc_now_str(),
                ),
            )
            users = conn.execute("SELECT user_id FROM users").fetchall()
            conn.commit()

        ptype = state["type"]
        title = state["title"]
        price_txt = f"₹{state['price']:.2f}" if ptype == "paid" else f"🪙 {int(state['price'])} Coins"
        user_states.pop(user_id, None)

        await query.message.reply_text(
            f"✅ Product <b>{html.escape(title)}</b> added! Notifying {len(users)} users...",
            parse_mode=ParseMode.HTML,
        )

        # Broadcast new product message to all users
        sent = 0
        for u in users:
            try:
                await context.bot.send_message(
                    chat_id=u["user_id"],
                    text=(
                        f"🆕 <b>New {ptype.capitalize()} Product Added!</b>\n\n"
                        f"📦 <b>{html.escape(title)}</b>\n"
                        f"💰 <b>Price:</b> {price_txt}\n\n"
                        f"Open <b>{BTN_PAID_PRODUCTS if ptype == 'paid' else BTN_FREE_PRODUCTS}</b> to check it out!"
                    ),
                    parse_mode=ParseMode.HTML,
                )
                sent += 1
            except TelegramError:
                pass
            await asyncio.sleep(0.04)
        await query.message.reply_text(f"📢 New product announcement sent to {sent}/{len(users)} users.")
        return

    if data.startswith("adm_edit_sel:"):
        prod_id = int(data.split(":", 1)[1])
        with get_db() as conn:
            p = conn.execute("SELECT * FROM products WHERE id = ?", (prod_id,)).fetchone()
        if not p:
            await query.answer("Product not found!", show_alert=True)
            return
        await query.answer()
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✏️ Title", callback_data=f"adm_edit_fld:{prod_id}:title"),
                    InlineKeyboardButton("✏️ Description", callback_data=f"adm_edit_fld:{prod_id}:desc"),
                ],
                [
                    InlineKeyboardButton("✏️ Price", callback_data=f"adm_edit_fld:{prod_id}:price"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"adm_del_prod:{prod_id}"),
                ],
            ]
        )
        await query.message.reply_text(
            f"📦 <b>{html.escape(p['title'])}</b> ({p['type'].upper()})\n"
            f"💰 Price: {p['price']}\n"
            f"📝 {html.escape(p['description'])}",
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("adm_edit_fld:"):
        _, pid_s, fld = data.split(":", 2)
        user_states[user_id] = {"step": "adm_edit_value", "prod_id": int(pid_s), "field": fld}
        await query.answer()
        await query.message.reply_text(f"Enter new value for <b>{fld}</b>:", parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_del_prod:"):
        prod_id = int(data.split(":", 1)[1])
        with get_db() as conn:
            conn.execute("DELETE FROM products WHERE id = ?", (prod_id,))
            conn.commit()
        await query.answer("🗑 Product deleted!")
        await query.message.edit_text("🗑 Product has been deleted.")
        return

    if data.startswith("adm_bonus_curr:"):
        curr = data.split(":", 1)[1]
        user_states[user_id] = {"step": "adm_bonus_target", "currency": curr}
        await query.answer()
        await query.message.reply_text(
            "Select target:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("👤 Single User", callback_data="adm_bonus_tgt:single"),
                        InlineKeyboardButton("👥 All Users", callback_data="adm_bonus_tgt:all"),
                    ]
                ]
            ),
        )
        return

    if data.startswith("adm_bonus_tgt:"):
        tgt = data.split(":", 1)[1]
        if state.get("step") != "adm_bonus_target":
            await query.answer()
            return
        state["target"] = tgt
        await query.answer()
        if tgt == "single":
            state["step"] = "adm_bonus_uid"
            user_states[user_id] = state
            await query.message.reply_text("Enter target <b>User ID</b>:", parse_mode=ParseMode.HTML)
        else:
            state["step"] = "adm_bonus_amount"
            user_states[user_id] = state
            await query.message.reply_text("Enter bonus <b>Amount</b>:", parse_mode=ParseMode.HTML)
        return

    if data == "adm_notif_failed":
        failed = last_failed_notif_users.get(user_id, [])
        await query.answer()
        if not failed:
            await query.message.reply_text("✅ No failed users recorded!")
            return
        lines = ["❌ <b>Failed Notification Users</b>\n"]
        for idx, u in enumerate(failed[:100], 1):
            uname = f"@{html.escape(u['username'])}" if u.get("username") else html.escape(u.get("full_name") or "NoUsername")
            lines.append(f"{idx}. {uname} (<code>{u['user_id']}</code>)")
        await query.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_coup_curr:"):
        curr = data.split(":", 1)[1]
        user_states[user_id] = {"step": "adm_coup_amount", "currency": curr}
        await query.answer()
        await query.message.reply_text("Enter coupon <b>Amount</b>:", parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_code_del:"):
        code = data.split(":", 1)[1]
        with get_db() as conn:
            conn.execute("DELETE FROM coupons WHERE code = ?", (code,))
            conn.execute("DELETE FROM coupon_claims WHERE code = ?", (code,))
            conn.commit()
        await query.answer("🗑 Code deleted!")
        await query.message.edit_text(f"🗑 Coupon code <code>{html.escape(code)}</code> deleted.", parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_users_pg:"):
        page = int(data.split(":", 1)[1])
        await query.answer()
        await send_admin_users_page(
            context.bot, query.message.chat_id, page=page, send_json=False, message_to_edit=query.message
        )
        return

    if data.startswith("adm_eb_curr:"):
        curr = data.split(":", 1)[1]
        state["currency"] = curr
        state["step"] = "adm_eb_action"
        user_states[user_id] = state
        await query.answer()
        await query.message.reply_text(
            "Select action:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("➕ Add", callback_data="adm_eb_act:add"),
                        InlineKeyboardButton("➖ Remove", callback_data="adm_eb_act:remove"),
                    ]
                ]
            ),
        )
        return

    if data.startswith("adm_eb_act:"):
        act = data.split(":", 1)[1]
        state["action"] = act
        state["step"] = "adm_eb_amount"
        user_states[user_id] = state
        await query.answer()
        await query.message.reply_text("Enter <b>Amount</b>:", parse_mode=ParseMode.HTML)
        return

    if data.startswith("adm_set:"):
        key = data.split(":", 1)[1]
        user_states[user_id] = {"step": "adm_setting_val", "key": key}
        await query.answer()
        await query.message.reply_text(
            f"Enter new value for <b>{html.escape(key)}</b> (send <code>clear</code> to reset to empty):",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Text, Photo & Document State Machine
# ---------------------------------------------------------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if not message or not user:
        return

    text = (message.text or "").strip()

    # 1. Check if user pressed a User Main Menu button
    if text in USER_MENU_BUTTONS:
        user_states.pop(user.id, None)
        if not await ensure_verified(update, context):
            return
        if text == BTN_FREE_PRODUCTS:
            await show_free_products(update, context)
        elif text == BTN_PAID_PRODUCTS:
            await show_paid_products(update, context)
        elif text == BTN_REF_STATUS:
            await send_referral_status(context.bot, message.chat_id, user.id)
        elif text == BTN_PURCHASE_HIST:
            await show_purchase_history_prompt(update)
        elif text == BTN_REF_HIST:
            await show_referral_history(update)
        elif text == BTN_WALLET:
            await show_wallet(update)
        elif text == BTN_DEVELOPER:
            await message.reply_text(f"👨‍💻 <b>Developer:</b> {DEVELOPER_USERNAME}", parse_mode=ParseMode.HTML)
        elif text == BTN_SUPPORT:
            await message.reply_text(f"🛟 <b>Help & Support:</b> {SUPPORT_USERNAME}", parse_mode=ParseMode.HTML)
        return

    # 2. Check if Admin pressed an Admin Panel button
    if text in ADMIN_MENU_BUTTONS and user.id in admin_sessions:
        user_states.pop(user.id, None)
        if text == ADM_EXIT:
            await message.reply_text("🏠 Switched to Main Menu.", reply_markup=user_main_keyboard())
        elif text == ADM_ADD_PROD:
            await message.reply_text(
                "➕ <b>Add Product</b>\nSelect product type:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("💎 Paid", callback_data="adm_add_type:paid"),
                            InlineKeyboardButton("🆓 Free", callback_data="adm_add_type:free"),
                        ]
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif text == ADM_EDIT_PROD:
            await show_admin_edit_products(update)
        elif text == ADM_REF_LB:
            await show_admin_referral_leaderboard(update)
        elif text == ADM_EDIT_BAL:
            user_states[user.id] = {"step": "adm_eb_uid"}
            await message.reply_text("⚖️ <b>Edit Balance</b>\nEnter target <b>User ID</b>:", parse_mode=ParseMode.HTML)
        elif text == ADM_DASHBOARD:
            await show_admin_dashboard(update)
        elif text == ADM_MONEY_HIST:
            await show_admin_add_money_history(update)
        elif text == ADM_SEND_BONUS:
            await message.reply_text(
                "🎁 <b>Send Bonus</b>\nSelect currency:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("🪙 Coins", callback_data="adm_bonus_curr:coin"),
                            InlineKeyboardButton("💵 Rs (₹)", callback_data="adm_bonus_curr:rs"),
                        ]
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif text == ADM_SEND_NOTIF:
            user_states[user.id] = {"step": "adm_notif_text"}
            await message.reply_text("📢 Enter the notification message to send to all users:")
        elif text == ADM_PAY_REQS:
            await show_admin_payment_requests(update)
        elif text == ADM_ADD_COUPON:
            await message.reply_text(
                "🎟 <b>Add Coupon Code</b>\nSelect reward currency:",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [
                            InlineKeyboardButton("💵 Rs (₹)", callback_data="adm_coup_curr:rs"),
                            InlineKeyboardButton("🪙 Coins", callback_data="adm_coup_curr:coin"),
                        ]
                    ]
                ),
                parse_mode=ParseMode.HTML,
            )
        elif text == ADM_MANAGE_CODES:
            await show_admin_manage_codes(update)
        elif text == ADM_BAL_LB:
            await show_admin_balance_leaderboard(update)
        elif text == ADM_USERS:
            await send_admin_users_page(context.bot, message.chat_id, page=0, send_json=True)
        elif text == ADM_SETTINGS:
            await show_admin_settings(update)
        return

    # 3. Process Active Conversation State
    state = user_states.get(user.id)
    if not state:
        return

    step = state.get("step")

    # --- Admin Password Login ---
    if step == "admin_login_password":
        if text == ADMIN_PASSWORD:
            admin_sessions.add(user.id)
            user_states.pop(user.id, None)
            await message.reply_text(
                "✅ <b>Admin Access Granted!</b>",
                reply_markup=admin_main_keyboard(),
                parse_mode=ParseMode.HTML,
            )
        else:
            user_states.pop(user.id, None)
            await message.reply_text("❌ Incorrect password!")
        return

    # --- Add Rs Flow ---
    if step == "add_rs_amount":
        try:
            amount = float(text)
            if amount <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("❌ Please enter a valid positive number (e.g., 100):")
            return

        order_id = "ORD-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        upi_id = get_setting("upi_id", DEFAULT_UPI_ID)
        qr_bio = generate_upi_qr_bytes(upi_id, amount, order_id)

        user_states[user.id] = {
            "step": "add_rs_screenshot",
            "amount": amount,
            "order_id": order_id,
        }
        await message.reply_photo(
            photo=qr_bio,
            caption=(
                f"💳 <b>Scan & Pay</b>\n\n"
                f"💰 <b>Amount:</b> ₹{amount:.2f}\n"
                f"🏦 <b>Admin UPI ID:</b> <code>{html.escape(upi_id)}</code>\n"
                f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n\n"
                f"📸 <b>Please send your payment screenshot (image) now for verification.</b>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "add_rs_screenshot":
        if not message.photo:
            await message.reply_text("❌ Please send a valid payment screenshot <b>image</b>!", parse_mode=ParseMode.HTML)
            return

        file_id = message.photo[-1].file_id
        amount = float(state["amount"])
        order_id = state["order_id"]
        now_str = utc_now_str()
        uname = user.username or ""
        fname = user.full_name or "User"

        pay_ch = get_setting("payment_channel", "").strip()
        ch_chat_id = ""
        ch_msg_id = None

        user_label = f"@{html.escape(uname)}" if uname else html.escape(fname)
        ch_caption = (
            f"💳 <b>New Payment Request</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
            f"👤 <b>User:</b> {user_label} (<code>{user.id}</code>)\n"
            f"💰 <b>Amount:</b> ₹{amount:.2f}\n"
            f"🕒 <b>Time:</b> {now_str} UTC\n"
            f"📌 <b>Status:</b> ⏳ PENDING"
        )
        ch_markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"pay_app:{order_id}"),
                    InlineKeyboardButton("❌ Reject", callback_data=f"pay_rej:{order_id}"),
                ]
            ]
        )

        if pay_ch:
            try:
                target_ch: Any = int(pay_ch) if pay_ch.lstrip("-").isdigit() else pay_ch
                sent_msg = await context.bot.send_photo(
                    chat_id=target_ch,
                    photo=file_id,
                    caption=ch_caption,
                    reply_markup=ch_markup,
                    parse_mode=ParseMode.HTML,
                )
                ch_chat_id = str(sent_msg.chat_id)
                ch_msg_id = sent_msg.message_id
            except TelegramError as e:
                logger.error("Failed to post payment request to channel %s: %s", pay_ch, e)

        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO payment_requests
                (order_id, user_id, username, full_name, amount, screenshot_file_id, status, channel_chat_id, channel_message_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (order_id, user.id, uname, fname, amount, file_id, ch_chat_id, ch_msg_id, now_str),
            )
            conn.commit()

        user_states.pop(user.id, None)
        await message.reply_text(
            f"✅ <b>Payment Request Submitted!</b>\n\n"
            f"🆔 <b>Order ID:</b> <code>{order_id}</code>\n"
            f"💰 <b>Amount:</b> ₹{amount:.2f}\n\n"
            f"⏳ Your request is pending admin approval. You will be notified once processed.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Redeem Coupon Flow ---
    if step == "redeem_coupon_code":
        code = text.strip()
        user_states.pop(user.id, None)
        with get_db() as conn:
            coupon = conn.execute("SELECT * FROM coupons WHERE code = ?", (code,)).fetchone()
            if not coupon:
                await message.reply_text("❌ Invalid coupon code!")
                return
            if coupon["claims_count"] >= coupon["max_claims"]:
                await message.reply_text("❌ This coupon code has reached its maximum claims limit!")
                return
            already = conn.execute(
                "SELECT 1 FROM coupon_claims WHERE code = ? AND user_id = ?", (code, user.id)
            ).fetchone()
            if already:
                await message.reply_text("❌ You have already claimed this coupon code!")
                return

            amt = float(coupon["amount"])
            if coupon["currency"] == "rs":
                conn.execute(
                    "UPDATE users SET rs_balance = rs_balance + ?, bonus_rs = bonus_rs + ? WHERE user_id = ?",
                    (amt, amt, user.id),
                )
                val_text = f"₹{amt:.2f}"
            else:
                iamt = int(amt)
                conn.execute(
                    "UPDATE users SET coins = coins + ?, bonus_coins = bonus_coins + ? WHERE user_id = ?",
                    (iamt, iamt, user.id),
                )
                val_text = f"🪙 {iamt} Coins"

            conn.execute(
                "UPDATE coupons SET claims_count = claims_count + 1 WHERE code = ?", (code,)
            )
            conn.execute(
                "INSERT INTO coupon_claims (code, user_id, claimed_at) VALUES (?, ?, ?)",
                (code, user.id, utc_now_str()),
            )
            conn.commit()

        await message.reply_text(
            f"✅ <b>Coupon Redeemed!</b>\n\n"
            f"🎉 <b>{val_text}</b> has been added to your balance!",
            parse_mode=ParseMode.HTML,
        )
        return

    # --- Admin-Only States ---
    if user.id not in admin_sessions:
        user_states.pop(user.id, None)
        return

    if step == "adm_add_title":
        if not text:
            await message.reply_text("❌ Please enter a valid title:")
            return
        state["title"] = text
        state["step"] = "adm_add_desc"
        await message.reply_text("Enter Product <b>Description</b>:", parse_mode=ParseMode.HTML)
        return

    if step == "adm_add_desc":
        if not text:
            await message.reply_text("❌ Please enter a valid description:")
            return
        state["description"] = text
        state["step"] = "adm_add_price"
        unit = "₹ (Rs)" if state["type"] == "paid" else "Coins"
        await message.reply_text(f"Enter Product <b>Price</b> in {unit}:", parse_mode=ParseMode.HTML)
        return

    if step == "adm_add_price":
        try:
            price = float(text)
            if price < 0:
                raise ValueError
        except ValueError:
            await message.reply_text("❌ Enter a valid number for price:")
            return
        state["price"] = price
        state["step"] = "adm_add_images"
        await message.reply_text(
            "🖼 Upload Product <b>Image(s)</b> (you can upload multiple images, or click Skip/Done):",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⏭ Skip", callback_data="adm_add_skip_img"),
                        InlineKeyboardButton("✅ Done Images", callback_data="adm_add_done_img"),
                    ]
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "adm_add_images":
        if message.photo:
            state["images"].append(message.photo[-1].file_id)
            await message.reply_text(
                f"🖼 Image #{len(state['images'])} added! Send more or click <b>Done Images</b>.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Done Images", callback_data="adm_add_done_img")]]
                ),
                parse_mode=ParseMode.HTML,
            )
        else:
            await message.reply_text("Please send an image or click Skip / Done Images.")
        return

    if step == "adm_add_files":
        file_entry = None
        if message.document:
            file_entry = {
                "tg_type": "document",
                "file_id": message.document.file_id,
                "file_name": message.document.file_name or "file",
                "file_size": message.document.file_size or 0,
                "mime_type": message.document.mime_type or "",
            }
        elif message.video:
            file_entry = {
                "tg_type": "video",
                "file_id": message.video.file_id,
                "file_name": message.video.file_name or "video.mp4",
                "file_size": message.video.file_size or 0,
                "mime_type": message.video.mime_type or "video/mp4",
            }
        elif message.audio:
            file_entry = {
                "tg_type": "audio",
                "file_id": message.audio.file_id,
                "file_name": message.audio.file_name or "audio.mp3",
                "file_size": message.audio.file_size or 0,
                "mime_type": message.audio.mime_type or "audio/mpeg",
            }
        elif message.photo:
            file_entry = {
                "tg_type": "photo",
                "file_id": message.photo[-1].file_id,
                "file_name": "image.jpg",
                "file_size": message.photo[-1].file_size or 0,
                "mime_type": "image/jpeg",
            }

        if not file_entry:
            await message.reply_text("Please upload a file or click OK / Save Product.")
            return

        state["files"].append(file_entry)
        await message.reply_text(
            f"📄 File #{len(state['files'])} added: <b>{html.escape(file_entry['file_name'])}</b>\n"
            f"Upload more or click <b>OK / Save Product</b>.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ OK / Save Product", callback_data="adm_add_done_file")]]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "adm_edit_value":
        prod_id = state["prod_id"]
        fld = state["field"]
        col_map = {"title": "title", "desc": "description", "price": "price"}
        col = col_map[fld]
        val: Any = text
        if fld == "price":
            try:
                val = float(text)
            except ValueError:
                await message.reply_text("❌ Enter a valid number:")
                return
        with get_db() as conn:
            conn.execute(f"UPDATE products SET {col} = ? WHERE id = ?", (val, prod_id))
            conn.commit()
        user_states.pop(user.id, None)
        await message.reply_text(f"✅ Product {fld} updated!")
        return

    if step == "adm_bonus_uid":
        if not text.isdigit():
            await message.reply_text("❌ Enter a valid numeric User ID:")
            return
        uid = int(text)
        if not get_user(uid):
            await message.reply_text("❌ User not found in database! Enter another User ID:")
            return
        state["target_uid"] = uid
        state["step"] = "adm_bonus_amount"
        await message.reply_text("Enter bonus <b>Amount</b>:", parse_mode=ParseMode.HTML)
        return

    if step == "adm_bonus_amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("❌ Enter a valid positive amount:")
            return

        curr = state["currency"]
        tgt = state["target"]
        user_states.pop(user.id, None)

        if tgt == "single":
            uid = state["target_uid"]
            with get_db() as conn:
                if curr == "rs":
                    conn.execute(
                        "UPDATE users SET rs_balance = rs_balance + ?, bonus_rs = bonus_rs + ? WHERE user_id = ?",
                        (amt, amt, uid),
                    )
                    val_str = f"₹{amt:.2f}"
                else:
                    iamt = int(amt)
                    conn.execute(
                        "UPDATE users SET coins = coins + ?, bonus_coins = bonus_coins + ? WHERE user_id = ?",
                        (iamt, iamt, uid),
                    )
                    val_str = f"🪙 {iamt} Coins"
                conn.commit()
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text=f"🎁 <b>Bonus Received!</b>\n\nAdmin sent you <b>{val_str}</b>!",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
            await message.reply_text(f"✅ Bonus ({val_str}) sent to user <code>{uid}</code>.", parse_mode=ParseMode.HTML)
        else:
            with get_db() as conn:
                users_list = conn.execute("SELECT user_id FROM users").fetchall()
                if curr == "rs":
                    conn.execute(
                        "UPDATE users SET rs_balance = rs_balance + ?, bonus_rs = bonus_rs + ?",
                        (amt, amt),
                    )
                    val_str = f"₹{amt:.2f}"
                else:
                    iamt = int(amt)
                    conn.execute(
                        "UPDATE users SET coins = coins + ?, bonus_coins = bonus_coins + ?",
                        (iamt, iamt),
                    )
                    val_str = f"🪙 {iamt} Coins"
                conn.commit()

            sent_cnt, fail_cnt = 0, 0
            await message.reply_text(f"⏳ Sending bonus to {len(users_list)} users...")
            for u in users_list:
                try:
                    await context.bot.send_message(
                        chat_id=u["user_id"],
                        text=f"🎁 <b>Bonus Received!</b>\n\nAdmin sent you a bonus of <b>{val_str}</b>!",
                        parse_mode=ParseMode.HTML,
                    )
                    sent_cnt += 1
                except TelegramError:
                    fail_cnt += 1
                await asyncio.sleep(0.04)

            await message.reply_text(
                f"🎁 <b>Bonus Broadcast Completed</b>\n\n"
                f"👥 <b>Total Users:</b> {len(users_list)}\n"
                f"✅ <b>Messages Sent:</b> {sent_cnt}\n"
                f"❌ <b>Failed Users:</b> {fail_cnt}",
                parse_mode=ParseMode.HTML,
            )
        return

    if step == "adm_notif_text":
        notif_text = message.text_html or html.escape(text)
        if not notif_text:
            await message.reply_text("❌ Please send a text notification message:")
            return
        user_states.pop(user.id, None)

        with get_db() as conn:
            users_list = conn.execute("SELECT user_id, username, full_name FROM users").fetchall()

        sent_cnt = 0
        failed_list: List[Dict[str, Any]] = []
        await message.reply_text(f"📢 Broadcasting notification to {len(users_list)} users...")

        for u in users_list:
            try:
                await context.bot.send_message(
                    chat_id=u["user_id"],
                    text=f"📢 <b>Notification</b>\n\n{notif_text}",
                    parse_mode=ParseMode.HTML,
                )
                sent_cnt += 1
            except TelegramError:
                failed_list.append(dict(u))
            await asyncio.sleep(0.04)

        last_failed_notif_users[user.id] = failed_list
        markup = (
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ View Failed Users", callback_data="adm_notif_failed")]]
            )
            if failed_list
            else None
        )
        await message.reply_text(
            f"📢 <b>Notification Broadcast Summary</b>\n\n"
            f"👥 <b>Total Users:</b> {len(users_list)}\n"
            f"✅ <b>Sent:</b> {sent_cnt}\n"
            f"❌ <b>Failed:</b> {len(failed_list)}",
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "adm_coup_amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("❌ Enter a valid positive amount:")
            return
        state["amount"] = amt
        state["step"] = "adm_coup_max"
        await message.reply_text("Enter <b>Max Claims</b> (total users who can claim):", parse_mode=ParseMode.HTML)
        return

    if step == "adm_coup_max":
        if not text.isdigit() or int(text) <= 0:
            await message.reply_text("❌ Enter a valid positive integer:")
            return
        state["max_claims"] = int(text)
        state["step"] = "adm_coup_code"
        await message.reply_text("Enter <b>Coupon Code</b>:", parse_mode=ParseMode.HTML)
        return

    if step == "adm_coup_code":
        code = text.strip()
        if not code:
            await message.reply_text("❌ Enter a non-empty coupon code:")
            return
        with get_db() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO coupons (code, currency, amount, max_claims, claims_count, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (code, state["currency"], float(state["amount"]), state["max_claims"], utc_now_str()),
            )
            conn.commit()
        user_states.pop(user.id, None)
        await message.reply_text(
            f"✅ Coupon Code <code>{html.escape(code)}</code> created!",
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "adm_eb_uid":
        if not text.isdigit():
            await message.reply_text("❌ Enter a valid numeric User ID:")
            return
        uid = int(text)
        u = get_user(uid)
        if not u:
            await message.reply_text("❌ User not found! Enter another User ID:")
            return
        state["target_uid"] = uid
        state["step"] = "adm_eb_curr"
        await message.reply_text(
            f"User: {format_user_label(u)} (₹{u['rs_balance']:.2f} | 🪙 {u['coins']} Coins)\nSelect currency:",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🪙 Coins", callback_data="adm_eb_curr:coin"),
                        InlineKeyboardButton("💵 Rs (₹)", callback_data="adm_eb_curr:rs"),
                    ]
                ]
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    if step == "adm_eb_amount":
        try:
            amt = float(text)
            if amt <= 0:
                raise ValueError
        except ValueError:
            await message.reply_text("❌ Enter a valid positive amount:")
            return
        state["amount"] = amt
        state["step"] = "adm_eb_reason"
        await message.reply_text("Enter <b>Reason</b> for this balance edit:", parse_mode=ParseMode.HTML)
        return

    if step == "adm_eb_reason":
        reason = text or "Admin adjustment"
        uid = state["target_uid"]
        curr = state["currency"]
        act = state["action"]
        amt = float(state["amount"])
        user_states.pop(user.id, None)

        with get_db() as conn:
            u = conn.execute("SELECT * FROM users WHERE user_id = ?", (uid,)).fetchone()
            if not u:
                await message.reply_text("❌ User not found!")
                return
            if curr == "rs":
                new_val = (u["rs_balance"] + amt) if act == "add" else max(0.0, u["rs_balance"] - amt)
                conn.execute("UPDATE users SET rs_balance = ? WHERE user_id = ?", (new_val, uid))
                val_txt = f"₹{amt:.2f}"
                bal_txt = f"₹{new_val:.2f}"
            else:
                iamt = int(amt)
                new_val = (u["coins"] + iamt) if act == "add" else max(0, u["coins"] - iamt)
                conn.execute("UPDATE users SET coins = ? WHERE user_id = ?", (new_val, uid))
                val_txt = f"🪙 {iamt} Coins"
                bal_txt = f"🪙 {new_val} Coins"
            conn.commit()

        verb = "Added" if act == "add" else "Removed"
        await message.reply_text(
            f"✅ <b>Balance Updated!</b>\n"
            f"👤 User: <code>{uid}</code>\n"
            f"📌 Action: {verb} {val_txt}\n"
            f"💳 New Balance: {bal_txt}\n"
            f"📝 Reason: {html.escape(reason)}",
            parse_mode=ParseMode.HTML,
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=(
                    f"⚖️ <b>Wallet Balance Updated by Admin</b>\n\n"
                    f"📌 <b>Action:</b> {verb} {val_txt}\n"
                    f"💳 <b>New Balance:</b> {bal_txt}\n"
                    f"📝 <b>Reason:</b> {html.escape(reason)}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
        return

    if step == "adm_setting_val":
        key = state["key"]
        val = "" if text.lower() == "clear" else text.strip()
        if key == "ref_coin":
            if not val.isdigit():
                await message.reply_text("❌ Please enter a valid integer:")
                return
        set_setting(key, val)
        user_states.pop(user.id, None)
        await message.reply_text(
            f"✅ Setting <b>{html.escape(key)}</b> updated to: <code>{html.escape(val or 'Empty')}</code>",
            parse_mode=ParseMode.HTML,
        )


# ---------------------------------------------------------------------------
# Render Web Health Check Server + Polling Runner
# ---------------------------------------------------------------------------
async def start_health_server() -> Optional[web.AppRunner]:
    port_str = os.getenv("PORT", "").strip()
    if not port_str:
        return None
    port = int(port_str)
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="Degit Store BoT is running!")

    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server listening on port %d", port)
    return runner


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing!")

    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("admin", cmd_admin))

    application.add_handler(CallbackQueryHandler(cb_verify_join, pattern=r"^verify_join$"))
    application.add_handler(CallbackQueryHandler(cb_payment_decision, pattern=r"^pay_(app|rej):"))
    application.add_handler(
        CallbackQueryHandler(
            cb_product_actions,
            pattern=r"^(fprod_view|fprod_buy|fprod_conf|pprod_view|pprod_buy|pprod_conf|cancel_buy|earn_coins|add_rs|redeem_coupon|phist|phist_dl)",
        )
    )
    application.add_handler(CallbackQueryHandler(cb_admin_actions, pattern=r"^adm_"))

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO)
            & ~filters.COMMAND,
            handle_message,
        )
    )

    health_runner = await start_health_server()

    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    logger.info("Degit Store BoT started polling...")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        if health_runner:
            await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
