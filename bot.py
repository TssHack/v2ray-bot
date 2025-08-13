# -*- coding: utf-8 -*-
"""
Telethon bot that:
- Enforces mandatory channel membership (managed by admin panel)
- After /start and membership verification, asks user to share phone number via a button
- Stores user info (id, username, phone, join date) in SQLite
- Fetches server list from ehsan-v2ray.vercel.app/ehsan (configurable) and sends 3 items
- Appends a purchase message with @abj0o
- Admin panel (only ADMIN_ID) to: toggle bot on/off, add/remove/list channels, list users

Requirements:
    pip install telethon aiohttp aiosqlite python-dotenv

Environment variables (or hardcode below):
    API_ID=12345
    API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
    BOT_TOKEN=123456789:ABC-DEF...
    ADMIN_ID=123456789       # numeric Telegram user id
    SOURCE_URL=https://ehsan-v2ray.vercel.app/ehsan

Notes:
- For membership checks, ensure the bot is added to the target channels and has permission
  to see members (being admin is safest). Use public @usernames for channels.
"""
import asyncio
import os
import random
from datetime import datetime

import aiohttp
import aiosqlite
from telethon import TelegramClient, events, Button
from telethon.errors import UsernameNotOccupiedError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipant, ChannelParticipantLeft, ChannelParticipantBanned

# ------------------ Config ------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SOURCE_URL = os.getenv("SOURCE_URL", "https://ehsan-v2ray.vercel.app/ehsan")
DB_PATH = os.getenv("DB_PATH", "bot.db")

# ------------------ SQL ------------------
INIT_SQL = r"""
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  user_id INTEGER PRIMARY KEY,
  username TEXT,
  phone TEXT,
  joined_at TEXT
);
"""

# default settings
DEFAULT_SETTINGS = {
    "bot_enabled": "1"  # "1" -> on, "0" -> off
}

# in-memory state for admin flows
admin_flow_state = {}

# ------------------ Helpers ------------------
async def db_get(conn: aiosqlite.Connection, key: str, default: str = "") -> str:
    async with conn.execute("SELECT value FROM settings WHERE key=?", (key,)) as cur:
        row = await cur.fetchone()
        return row[0] if row else default

async def db_set(conn: aiosqlite.Connection, key: str, value: str):
    await conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    await conn.commit()

async def list_channels(conn: aiosqlite.Connection):
    async with conn.execute("SELECT username FROM channels ORDER BY id ASC") as cur:
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def add_channel(conn: aiosqlite.Connection, username: str) -> bool:
    try:
        await conn.execute("INSERT INTO channels(username) VALUES(?)", (username,))
        await conn.commit()
        return True
    except Exception:
        return False

async def remove_channel(conn: aiosqlite.Connection, username: str) -> bool:
    cur = await conn.execute("DELETE FROM channels WHERE username=?", (username,))
    await conn.commit()
    return cur.rowcount > 0

async def save_user(conn: aiosqlite.Connection, user_id: int, username: str | None, phone: str | None):
    now = datetime.utcnow().isoformat()
    await conn.execute(
        "INSERT INTO users(user_id, username, phone, joined_at) VALUES(?,?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, phone=COALESCE(excluded.phone, users.phone)",
        (user_id, username, phone, now)
    )
    await conn.commit()

async def get_users(conn: aiosqlite.Connection):
    async with conn.execute("SELECT user_id, username, phone, joined_at FROM users ORDER BY joined_at DESC") as cur:
        return await cur.fetchall()

async def is_member(client: TelegramClient, user_id: int, channel_username: str) -> bool:
    """Return True if user is a member (not left/banned)."""
    try:
        entity = await client.get_entity(channel_username)
    except UsernameNotOccupiedError:
        return False
    try:
        res = await client(GetParticipantRequest(entity, user_id))
        participant = res.participant
        if isinstance(participant, (ChannelParticipantLeft, ChannelParticipantBanned)):
            return False
        # Any other ChannelParticipant means present
        return isinstance(participant, ChannelParticipant)
    except Exception:
        # If bot lacks permission, conservatively say not a member
        return False

async def check_all_memberships(client: TelegramClient, user_id: int, channels: list[str]) -> list[str]:
    not_joined = []
    for ch in channels:
        ok = await is_member(client, user_id, ch)
        if not ok:
            not_joined.append(ch)
    return not_joined

async def fetch_servers() -> list[str]:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as sess:
        async with sess.get(SOURCE_URL) as resp:
            text = await resp.text()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines

def pick_three(items: list[str]) -> list[str]:
    if len(items) <= 3:
        return items
    return random.sample(items, 3)

# ------------------ Bot ------------------
client = TelegramClient("bot", API_ID, API_HASH).start(bot_token=BOT_TOKEN)

JOIN_TEXT = (
    "برای ادامه، لطفاً در کانال‌های زیر عضو شوید و سپس روی دکمه *انجام شد* بزنید.\n"
    "پس از تایید، شماره‌تان را با دکمه *ارسال شماره* برای ما بفرستید."
)
AFTER_SEND_TEXT = (
    "🔐 سه سرور پیشنهادی برای شما:\n\n{servers}\n\n"
    "برای *خرید اشتراک اختصاصی و نامحدود* به آیدی زیر پیام دهید:\n@abj0o"
)

# ---------- UI builders ----------

def join_keyboard(channels: list[str]):
    rows = [[Button.url(f"عضویت در {ch}", f"https://t.me/{ch.lstrip('@')}")] for ch in channels]
    rows.append([Button.inline("✅ انجام شد", data=b"verify_membership")])
    return rows


ADMIN_MENU = [
    [Button.inline("🔌 روشن/خاموش ربات", b"toggle_bot"), Button.inline("📢 کانال‌های اجباری", b"channels_menu")],
    [Button.inline("👥 کاربران", b"users_menu")],
]

CHANNELS_MENU = [
    [Button.inline("➕ افزودن کانال", b"ch_add"), Button.inline("➖ حذف کانال", b"ch_remove")],
    [Button.inline("📃 لیست کانال‌ها", b"ch_list"), Button.inline("⬅️ بازگشت", b"admin_back")],
]

USERS_MENU = [
    [Button.inline("📃 لیست کاربران", b"u_list"), Button.inline("⬅️ بازگشت", b"admin_back")],
]

# ------------------ Handlers ------------------
@client.on(events.NewMessage(pattern=r"^/start"))
async def start_handler(event: events.NewMessage.Event):
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(INIT_SQL)
        # ensure default settings
        for k, v in DEFAULT_SETTINGS.items():
            await db_set(conn, k, await db_get(conn, k, v))

        bot_enabled = await db_get(conn, "bot_enabled", "1")
        if bot_enabled != "1" and event.sender_id != ADMIN_ID:
            await event.reply("ربات فعلاً غیرفعال است. لطفاً بعداً امتحان کنید.")
            return

        chs = await list_channels(conn)
        await save_user(conn, event.sender_id, (await event.get_sender()).username, None)

    if chs:
        kb = join_keyboard(chs)
        await event.reply(JOIN_TEXT, buttons=kb, parse_mode="markdown")
    else:
        # No channels required → ask phone immediately
        await ask_phone(event)

async def ask_phone(event_or_conv):
    btn = [
        [Button.request_phone("دریافت کانفینگ رایگان")]
    ]
    await event_or_conv.reply("برای ادامه روی دکمه زیر بزنید تا کانفینگ خود را دریافت کنید.:", buttons=btn)

@client.on(events.CallbackQuery)
async def callbacks(event: events.CallbackQuery.Event):
    if event.sender_id == ADMIN_ID:
        if event.data == b"toggle_bot":
            async with aiosqlite.connect(DB_PATH) as conn:
                cur = await db_get(conn, "bot_enabled", "1")
                newv = "0" if cur == "1" else "1"
                await db_set(conn, "bot_enabled", newv)
            await event.edit(f"وضعیت ربات: {'✅ روشن' if newv=='1' else '⛔️ خاموش'}", buttons=ADMIN_MENU)
            return
        if event.data == b"channels_menu":
            await event.edit("مدیریت کانال‌ها:", buttons=CHANNELS_MENU)
            return
        if event.data == b"users_menu":
            await event.edit("مدیریت کاربران:", buttons=USERS_MENU)
            return
        if event.data == b"admin_back":
            await event.edit("پنل ادمین:", buttons=ADMIN_MENU)
            return
        if event.data == b"ch_add":
            admin_flow_state[event.sender_id] = ("await_channel_add",)
            await event.respond("یوزرنیم کانال عمومی را بفرستید (مثال: @mychannel)")
            return
        if event.data == b"ch_remove":
            admin_flow_state[event.sender_id] = ("await_channel_remove",)
            await event.respond("یوزرنیم کانال برای حذف را بفرستید (مثال: @mychannel)")
            return
        if event.data == b"ch_list":
            async with aiosqlite.connect(DB_PATH) as conn:
                chs = await list_channels(conn)
            txt = "\n".join(chs) if chs else "هیچ کانالی ثبت نشده است."
            await event.respond(f"کانال‌های اجباری:\n{txt}")
            return
        if event.data == b"u_list":
            async with aiosqlite.connect(DB_PATH) as conn:
                users = await get_users(conn)
            if not users:
                await event.respond("لیست کاربران خالی است.")
            else:
                lines = []
                for (uid, uname, phone, joined) in users[:100]:
                    lines.append(f"{uid} | @{uname or '-'} | {phone or '-'} | {joined}")
                await event.respond("کاربران:\n" + "\n".join(lines))
            return

    if event.data == b"verify_membership":
        async with aiosqlite.connect(DB_PATH) as conn:
            chs = await list_channels(conn)
        if not chs:
            await event.answer("کانالی تعریف نشده است.", alert=True)
            await ask_phone(event)
            return
        not_joined = await check_all_memberships(client, event.sender_id, chs)
        if not_joined:
            kb = join_keyboard(chs)
            await event.edit("عضویت شما کامل نیست. لطفاً همه کانال‌ها را عضو شوید و دوباره امتحان کنید.", buttons=kb)
        else:
            await event.edit("✅ عضویت تایید شد.")
            await ask_phone(event)

@client.on(events.NewMessage(from_users=ADMIN_ID))
async def admin_flows(event: events.NewMessage.Event):
    state = admin_flow_state.get(event.sender_id)
    if not state:
        return
    mode = state[0]
    if mode == "await_channel_add":
        username = event.raw_text.strip()
        if not username.startswith("@"):
            await event.reply("فرمت نادرست است. با @ شروع کنید.")
            return
        async with aiosqlite.connect(DB_PATH) as conn:
            ok = await add_channel(conn, username)
        await event.reply("✅ اضافه شد" if ok else "⚠️ قبلاً وجود دارد یا خطا رخ داد")
        admin_flow_state.pop(event.sender_id, None)
    elif mode == "await_channel_remove":
        username = event.raw_text.strip()
        async with aiosqlite.connect(DB_PATH) as conn:
            ok = await remove_channel(conn, username)
        await event.reply("✅ حذف شد" if ok else "⚠️ یافت نشد")
        admin_flow_state.pop(event.sender_id, None)

@client.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_menu(event: events.NewMessage.Event):
    if event.sender_id != ADMIN_ID:
        return
    async with aiosqlite.connect(DB_PATH) as conn:
        bot_enabled = await db_get(conn, "bot_enabled", "1")
    status = '✅ روشن' if bot_enabled == '1' else '⛔️ خاموش'
    await event.reply(f"پنل ادمین (وضعیت ربات: {status})", buttons=ADMIN_MENU)

@client.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(event: events.NewMessage.Event):
    await event.reply("/start - شروع\n/admin - پنل ادمین (فقط مدیر)")

@client.on(events.NewMessage(func=lambda e: bool(e.contact)))
async def on_contact(event: events.NewMessage.Event):
    contact = event.message.contact
    phone = contact.phone_number if contact else None
    async with aiosqlite.connect(DB_PATH) as conn:
        await save_user(conn, event.sender_id, (await event.get_sender()).username, phone)

    # Fetch servers and send 3
    try:
        servers = await fetch_servers()
    except Exception:
        await event.reply("خطا در دریافت سرورها. لطفاً دوباره تلاش کنید.")
        return
    three = pick_three(servers)

    servers_txt = "\n".join(three)
    await event.reply(AFTER_SEND_TEXT.format(servers=servers_txt), parse_mode="markdown")

# -------------- Run --------------
async def main():
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.executescript(INIT_SQL)
        # ensure default settings
        for k, v in DEFAULT_SETTINGS.items():
            await db_set(conn, k, await db_get(conn, k, v))
    print("Bot is running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
