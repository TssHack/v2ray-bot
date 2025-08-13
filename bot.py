# -*- coding: utf-8 -*-
"""
Telethon bot that:
- Enforces mandatory channel membership (managed by admin panel)
- After /start and membership verification, asks user to share phone number via a button
- Stores user info (id, username, phone, join date) in SQLite
- Fetches server list from ehsan-v2ray.vercel.app/ehsan (configurable) and sends 3 items
- Appends a purchase message with @abj0o
- Admin panel (only ADMIN_ID) to: toggle bot on/off, add/remove/list channels, list users
- Database download functionality for admin

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
import shutil
from datetime import datetime

import aiohttp
import aiosqlite
from telethon import TelegramClient, events, Button
from telethon.errors import UsernameNotOccupiedError
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import ChannelParticipant, ChannelParticipantLeft, ChannelParticipantBanned

# ------------------ Config ------------------
API_ID = int(os.getenv("API_ID", "18377832"))
API_HASH = os.getenv("API_HASH", "ed8556c450c6d0fd68912423325dd09c")
BOT_TOKEN = os.getenv("BOT_TOKEN", "6399155863:AAEIFUo7Uu9XTeB7YgFha7u_ixh9piIcBkU")
ADMIN_ID = int(os.getenv("ADMIN_ID", "1848591768"))
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
    cur = await conn.execute("UPDATE settings SET value=? WHERE key=?", (value, key))
    if cur.rowcount == 0:
        await conn.execute("INSERT INTO settings(key,value) VALUES(?,?)", (key, value))
    await conn.commit()

async def save_user(conn: aiosqlite.Connection, user_id: int, username: str | None, phone: str | None):
    now = datetime.utcnow().isoformat()

    # Check if user exists
    async with conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)) as cur:
        exists = await cur.fetchone()
    
    if exists:
        # Update existing user
        if phone is not None:
            await conn.execute(
                "UPDATE users SET username=?, phone=?, joined_at=? WHERE user_id=?",
                (username, phone, now, user_id)
            )
        else:
            await conn.execute(
                "UPDATE users SET username=?, joined_at=? WHERE user_id=?",
                (username, now, user_id)
            )
    else:
        # Insert new user
        await conn.execute(
            "INSERT INTO users(user_id, username, phone, joined_at) VALUES(?,?,?,?)",
            (user_id, username, phone, now)
        )

    await conn.commit()

async def get_users(conn: aiosqlite.Connection):
    async with conn.execute("SELECT user_id, username, phone, joined_at FROM users ORDER BY joined_at DESC") as cur:
        return await cur.fetchall()

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

async def list_channels(conn: aiosqlite.Connection) -> list[str]:
    async with conn.execute("SELECT username FROM channels") as cur:
        rows = await cur.fetchall()
        return [row[0] for row in rows]

async def is_member(client: TelegramClient, user_id: int, channel_username: str) -> bool:
    """Return True if user is a member (not left/banned)."""
    try:
        entity = await client.get_entity(channel_username)
    except UsernameNotOccupiedError:
        return False
    except Exception:
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
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(SOURCE_URL) as resp:
                text = await resp.text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return lines
    except Exception as e:
        print(f"Error fetching servers: {e}")
        return []

def pick_three(items: list[str]) -> list[str]:
    if len(items) <= 3:
        return items
    return random.sample(items, 3)

async def backup_database() -> str:
    """Create a backup of the database and return the backup file path"""
    backup_name = f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    backup_path = f"/tmp/{backup_name}"
    
    try:
        shutil.copy2(DB_PATH, backup_path)
        return backup_path
    except Exception as e:
        print(f"Error creating backup: {e}")
        return None

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
    rows = []
    for ch in channels:
        # Remove @ if it exists for the URL
        clean_ch = ch.lstrip('@')
        rows.append([Button.url(f"عضویت در {ch}", f"https://t.me/{clean_ch}")])
    rows.append([Button.inline("✅ انجام شد", data=b"verify_membership")])
    return rows

ADMIN_MENU = [
    [Button.inline("🔌 روشن/خاموش ربات", b"toggle_bot"), Button.inline("📢 کانال‌های اجباری", b"channels_menu")],
    [Button.inline("👥 کاربران", b"users_menu"), Button.inline("💾 دانلود دیتابیس", b"download_db")],
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
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            await conn.executescript(INIT_SQL)
            # ensure default settings
            for k, v in DEFAULT_SETTINGS.items():
                current_value = await db_get(conn, k, v)
                await db_set(conn, k, current_value)

            bot_enabled = await db_get(conn, "bot_enabled", "1")
            if bot_enabled != "1" and event.sender_id != ADMIN_ID:
                await event.reply("ربات فعلاً غیرفعال است. لطفاً بعداً امتحان کنید.")
                return

            chs = await list_channels(conn)
            sender = await event.get_sender()
            username = sender.username if sender else None
            await save_user(conn, event.sender_id, username, None)

        if chs:
            kb = join_keyboard(chs)
            await event.reply(JOIN_TEXT, buttons=kb, parse_mode="markdown")
        else:
            # No channels required → ask phone immediately
            await ask_phone(event)
    except Exception as e:
        print(f"Error in start_handler: {e}")
        await event.reply("خطایی رخ داد. لطفاً دوباره تلاش کنید.")

async def ask_phone(event_or_conv):
    btn = [
        [Button.request_phone("📱 دریافت کانفینگ رایگان")]
    ]
    await event_or_conv.reply("برای ادامه روی دکمه زیر بزنید تا کانفینگ خود را دریافت کنید:", buttons=btn)

@client.on(events.CallbackQuery)
async def callbacks(event: events.CallbackQuery.Event):
    try:
        # Admin callbacks
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
                async with aiosqlite.connect(DB_PATH) as conn:
                    bot_enabled = await db_get(conn, "bot_enabled", "1")
                status = '✅ روشن' if bot_enabled == '1' else '⛔️ خاموش'
                await event.edit(f"پنل ادمین (وضعیت ربات: {status})", buttons=ADMIN_MENU)
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
                    for i, (uid, uname, phone, joined) in enumerate(users[:50], 1):  # Limit to 50 users per message
                        lines.append(f"{i}. ID: {uid} | @{uname or '-'} | {phone or '-'} | {joined[:10]}")
                    
                    text = "📊 لیست کاربران:\n\n" + "\n".join(lines)
                    if len(users) > 50:
                        text += f"\n\n... و {len(users) - 50} کاربر دیگر"
                    
                    await event.respond(text)
                return
            if event.data == b"download_db":
                await event.respond("در حال آماده‌سازی فایل دیتابیس...")
                backup_path = await backup_database()
                if backup_path:
                    try:
                        await client.send_file(event.sender_id, backup_path, caption="📁 فایل دیتابیس ربات")
                        # Clean up the backup file
                        os.remove(backup_path)
                        await event.respond("✅ فایل دیتابیس با موفقیت ارسال شد.")
                    except Exception as e:
                        await event.respond(f"❌ خطا در ارسال فایل: {str(e)}")
                        if os.path.exists(backup_path):
                            os.remove(backup_path)
                else:
                    await event.respond("❌ خطا در ایجاد فایل پشتیبان.")
                return

        # User callbacks
        if event.data == b"verify_membership":
            async with aiosqlite.connect(DB_PATH) as conn:
                chs = await list_channels(conn)
            if not chs:
                await event.answer("کانالی تعریف نشده است.", alert=True)
                await ask_phone(event)
                return
            
            await event.answer("در حال بررسی عضویت...")
            not_joined = await check_all_memberships(client, event.sender_id, chs)
            if not_joined:
                kb = join_keyboard(chs)
                missing_channels = ", ".join(not_joined)
                await event.edit(f"عضویت شما در کانال‌های زیر کامل نیست:\n{missing_channels}\n\nلطفاً در همه کانال‌ها عضو شوید و دوباره امتحان کنید.", buttons=kb)
            else:
                await event.edit("✅ عضویت تایید شد!")
                await ask_phone(event)

    except Exception as e:
        print(f"Error in callbacks: {e}")
        await event.answer("خطایی رخ داد. لطفاً دوباره تلاش کنید.", alert=True)

@client.on(events.NewMessage(from_users=ADMIN_ID))
async def admin_flows(event: events.NewMessage.Event):
    try:
        state = admin_flow_state.get(event.sender_id)
        if not state:
            return
        
        mode = state[0]
        if mode == "await_channel_add":
            username = event.raw_text.strip()
            if not username.startswith("@"):
                await event.reply("❌ فرمت نادرست است. با @ شروع کنید. (مثال: @mychannel)")
                return
            
            async with aiosqlite.connect(DB_PATH) as conn:
                ok = await add_channel(conn, username)
            
            if ok:
                await event.reply(f"✅ کانال {username} با موفقیت اضافه شد!")
            else:
                await event.reply("⚠️ کانال قبلاً وجود دارد یا خطا رخ داد")
            
            admin_flow_state.pop(event.sender_id, None)
            
        elif mode == "await_channel_remove":
            username = event.raw_text.strip()
            if not username.startswith("@"):
                username = "@" + username  # Auto-add @ if missing
            
            async with aiosqlite.connect(DB_PATH) as conn:
                ok = await remove_channel(conn, username)
            
            if ok:
                await event.reply(f"✅ کانال {username} با موفقیت حذف شد!")
            else:
                await event.reply("⚠️ کانال یافت نشد")
            
            admin_flow_state.pop(event.sender_id, None)
    
    except Exception as e:
        print(f"Error in admin_flows: {e}")
        await event.reply("خطایی رخ داد. لطفاً دوباره تلاش کنید.")
        admin_flow_state.pop(event.sender_id, None)

@client.on(events.NewMessage(pattern=r"^/admin$"))
async def admin_menu(event: events.NewMessage.Event):
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            bot_enabled = await db_get(conn, "bot_enabled", "1")
        status = '✅ روشن' if bot_enabled == '1' else '⛔️ خاموش'
        await event.reply(f"🔧 پنل ادمین\n\nوضعیت ربات: {status}", buttons=ADMIN_MENU)
    except Exception as e:
        print(f"Error in admin_menu: {e}")
        await event.reply("خطایی رخ داد.")

@client.on(events.NewMessage(pattern=r"^/help$"))
async def help_cmd(event: events.NewMessage.Event):
    help_text = """
🤖 راهنمای ربات:

/start - شروع و دریافت کانفیگ رایگان
/help - نمایش این راهنما

👨‍💼 دستورات مدیر:
/admin - ورود به پنل مدیریت

📞 برای پشتیبانی: @abj0o
    """
    await event.reply(help_text.strip())

@client.on(events.NewMessage(func=lambda e: bool(e.contact)))
async def on_contact(event: events.NewMessage.Event):
    try:
        contact = event.message.contact
        phone = contact.phone_number if contact else None
        
        sender = await event.get_sender()
        username = sender.username if sender else None
        
        async with aiosqlite.connect(DB_PATH) as conn:
            await save_user(conn, event.sender_id, username, phone)

        await event.reply("✅ شماره تماس شما ثبت شد!")
        
        # Fetch servers and send 3
        servers = await fetch_servers()
        if not servers:
            await event.reply("❌ خطا در دریافت سرورها. لطفاً بعداً دوباره تلاش کنید.")
            return
        
        three = pick_three(servers)
        if not three:
            await event.reply("❌ هیچ سروری در دسترس نیست.")
            return

        servers_txt = "\n".join(f"🔗 `{server}`" for server in three)
        final_message = AFTER_SEND_TEXT.format(servers=servers_txt)
        
        await event.reply(final_message, parse_mode="markdown")

    except Exception as e:
        print(f"Error in on_contact: {e}")
        await event.reply("خطایی رخ داد. لطفاً دوباره تلاش کنید.")

@client.on(events.NewMessage(pattern=r"^/stats$"))
async def stats_cmd(event: events.NewMessage.Event):
    if event.sender_id != ADMIN_ID:
        return
    
    try:
        async with aiosqlite.connect(DB_PATH) as conn:
            # Count total users
            async with conn.execute("SELECT COUNT(*) FROM users") as cur:
                total_users = (await cur.fetchone())[0]
            
            # Count users with phone numbers
            async with conn.execute("SELECT COUNT(*) FROM users WHERE phone IS NOT NULL") as cur:
                users_with_phone = (await cur.fetchone())[0]
            
            # Count channels
            async with conn.execute("SELECT COUNT(*) FROM channels") as cur:
                total_channels = (await cur.fetchone())[0]
            
            bot_enabled = await db_get(conn, "bot_enabled", "1")
        
        status = '✅ فعال' if bot_enabled == '1' else '⛔️ غیرفعال'
        
        stats_text = f"""
📊 آمار ربات:

👥 کل کاربران: {total_users}
📱 کاربران با شماره: {users_with_phone}
📢 کانال‌های اجباری: {total_channels}
🔌 وضعیت ربات: {status}

📅 تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        """
        
        await event.reply(stats_text.strip())
    
    except Exception as e:
        print(f"Error in stats_cmd: {e}")
        await event.reply("خطا در دریافت آمار.")

# -------------- Run --------------
if __name__ == "__main__":
    async def runner():
        try:
            # Initialize database
            async with aiosqlite.connect(DB_PATH) as conn:
                await conn.executescript(INIT_SQL)
                for k, v in DEFAULT_SETTINGS.items():
                    current_value = await db_get(conn, k, v)
                    await db_set(conn, k, current_value)
            
            print("🤖 Bot is starting...")
            print(f"📁 Database: {DB_PATH}")
            print(f"👤 Admin ID: {ADMIN_ID}")
            print(f"🌐 Source URL: {SOURCE_URL}")
            print("✅ Bot is running...")
            
            await client.run_until_disconnected()
        
        except Exception as e:
            print(f"❌ Error starting bot: {e}")

    asyncio.run(runner())
