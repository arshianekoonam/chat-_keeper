"""
Keeper Bot - Captures deleted messages and self-destructing media.
Sends them to a destination bot.

How it works:
1. Userbot (your account) listens to all incoming messages
2. Caches them in memory (text + media file paths)
3. When a message is deleted (MessageDeleted event), finds it in cache
4. Forwards it to the keeper bot (@YourKeeperBot)
5. The keeper bot stores it in a chat with you

Self-destructing media (TTL):
- When someone sends a self-destructing photo/video, we immediately download it
- Save to disk before it expires
- Send to keeper bot
"""
import asyncio
import logging
import time
import os
import io
import json
import shutil
from collections import defaultdict, deque
from contextlib import suppress
from pathlib import Path

from telethon import TelegramClient, events, types
from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageService,
)
from telethon.errors import (
    FloodWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
)

from config import (
    API_ID, API_HASH, PHONE_NUMBER, SESSION_NAME,
    KEEPER_BOT_TOKEN, DESTINATION_CHAT_ID,
    ADMIN_USER_ID,
    CACHE_SIZE_PER_CHAT, TRACK_DELETED_MESSAGES, TRACK_TTL_MEDIA,
    TRACK_PRIVATE_CHATS, TRACK_GROUPS, TRACK_SAVED_MESSAGES,
    LOG_FILE,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logger = logging.getLogger("keeper_bot")

# ---------------------------------------------------------------------------
# Userbot client (your account)
# ---------------------------------------------------------------------------
userbot = TelegramClient(SESSION_NAME, API_ID, API_HASH)

# ---------------------------------------------------------------------------
# Keeper bot client (the bot that receives forwarded messages)
# ---------------------------------------------------------------------------
keeper_bot = TelegramClient("keeper_bot_session", API_ID, API_HASH)

# ---------------------------------------------------------------------------
# Cache: { chat_id: deque of recent messages }
# Each entry: { message_id, text, sender_id, sender_name, chat_name, timestamp, media_info }
# ---------------------------------------------------------------------------
MESSAGE_CACHE: dict[int, deque] = defaultdict(lambda: deque(maxlen=CACHE_SIZE_PER_CHAT))

# Track keeper bot's chat with you (the user)
KEEPER_CHAT_ID = None
MY_USER_ID = None

# Stats
STATS = {
    "messages_tracked": 0,
    "deletions_captured": 0,
    "ttl_captured": 0,
    "errors": 0,
}

# ---------------------------------------------------------------------------
# User management (persisted to JSON files)
# ---------------------------------------------------------------------------
USERS_FILE = Path(__file__).parent / "users.json"
BLOCKED_FILE = Path(__file__).parent / "blocked.json"
SETTINGS_FILE = Path(__file__).parent / "settings.json"

# { user_id: { "username": str, "first_name": str, "last_seen": float, "starts": int } }
USERS_DB: dict[int, dict] = {}
# list of blocked user IDs
BLOCKED_USERS: set[int] = set()


def _load_db():
    """Load users and blocked lists from disk."""
    global USERS_DB, BLOCKED_USERS, KEEPER_CHAT_ID
    
    if DESTINATION_CHAT_ID:
        KEEPER_CHAT_ID = DESTINATION_CHAT_ID
        
    try:
        if SETTINGS_FILE.exists() and not DESTINATION_CHAT_ID:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            if data.get("keeper_chat_id"):
                KEEPER_CHAT_ID = data.get("keeper_chat_id")
    except Exception as e:
        logger.warning(f"Failed to load settings.json: {e}")

    try:
        if USERS_FILE.exists():
            USERS_DB = {int(k): v for k, v in json.loads(USERS_FILE.read_text(encoding="utf-8")).items()}
    except Exception as e:
        logger.warning(f"Failed to load users.json: {e}")
        USERS_DB = {}
    try:
        if BLOCKED_FILE.exists():
            BLOCKED_USERS = set(json.loads(BLOCKED_FILE.read_text(encoding="utf-8")))
    except Exception as e:
        logger.warning(f"Failed to load blocked.json: {e}")
        BLOCKED_USERS = set()


def _save_users():
    try:
        USERS_FILE.write_text(json.dumps(USERS_DB, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save users.json: {e}")


def _save_blocked():
    try:
        BLOCKED_FILE.write_text(json.dumps(list(BLOCKED_USERS), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save blocked.json: {e}")


def _save_settings():
    try:
        data = {"keeper_chat_id": KEEPER_CHAT_ID}
        SETTINGS_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to save settings.json: {e}")


def track_user(user_id: int, username: str = None, first_name: str = None):
    """Track that a user has interacted with the bot."""
    if user_id == ADMIN_USER_ID:
        return  # Don't track admin
    now = time.time()
    if user_id in USERS_DB:
        USERS_DB[user_id]["last_seen"] = now
        USERS_DB[user_id]["starts"] = USERS_DB[user_id].get("starts", 0) + 1
        if username:
            USERS_DB[user_id]["username"] = username
        if first_name:
            USERS_DB[user_id]["first_name"] = first_name
    else:
        USERS_DB[user_id] = {
            "username": username,
            "first_name": first_name,
            "first_seen": now,
            "last_seen": now,
            "starts": 1,
        }
    _save_users()


def is_user_blocked(user_id: int) -> bool:
    return user_id in BLOCKED_USERS


def block_user(user_id: int) -> bool:
    if user_id == ADMIN_USER_ID:
        return False
    if user_id in BLOCKED_USERS:
        return False
    BLOCKED_USERS.add(user_id)
    _save_blocked()
    return True


def unblock_user(user_id: int) -> bool:
    if user_id not in BLOCKED_USERS:
        return False
    BLOCKED_USERS.discard(user_id)
    _save_blocked()
    return True


# Load DB at startup
_load_db()


# Temp dir for TTL media
TEMP_DIR = Path(__file__).parent / "temp_media"
TEMP_DIR.mkdir(exist_ok=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def get_keeper_chat_id():
    """Find the chat ID where keeper bot should send messages (your chat with the bot)."""
    global KEEPER_CHAT_ID
    if KEEPER_CHAT_ID:
        return KEEPER_CHAT_ID

    # Try to get it from the keeper bot's updates
    # When you /start the bot, we'll capture your user ID
    # For now, return None and wait for /start
    return None


async def send_to_keeper(content: str, file=None, caption=None):
    """Send a message to the keeper bot."""
    global KEEPER_CHAT_ID
    if not KEEPER_CHAT_ID:
        logger.warning("Keeper chat ID not set. Send /start to @KeeperArchiveBot first.")
        return False

    try:
        if file:
            await keeper_bot.send_file(
                KEEPER_CHAT_ID,
                file=file,
                caption=caption or content,
                parse_mode="html",
            )
        else:
            await keeper_bot.send_message(
                KEEPER_CHAT_ID,
                content,
                parse_mode="html",
                link_preview=False,
            )
        return True
    except FloodWaitError as e:
        logger.warning(f"Flood wait: {e.seconds}s")
        await asyncio.sleep(e.seconds)
        return False
    except Exception as e:
        logger.error(f"Failed to send to keeper: {e}")
        STATS["errors"] += 1
        return False


def format_message_info(msg_data: dict) -> str:
    """Format info about a captured message for the keeper bot."""
    chat_name = msg_data.get("chat_name", "Unknown")
    sender_name = msg_data.get("sender_name", "Unknown")
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(msg_data["timestamp"]))

    info = (
        f"🗑 <b>Deleted Message</b>\n\n"
        f"👤 <b>Sender:</b> {sender_name}\n"
        f"💬 <b>Chat:</b> {chat_name}\n"
        f"⏰ <b>Time:</b> {timestamp}\n"
    )

    if msg_data.get("text"):
        info += f"\n📝 <b>Text:</b>\n<code>{msg_data['text'][:2000]}</code>\n"

    if msg_data.get("media_type"):
        info += f"\n📎 <b>Media Type:</b> {msg_data['media_type']}\n"
        if msg_data.get("media_caption"):
            info += f"📝 <b>Caption:</b> {msg_data['media_caption']}\n"

    return info


async def cache_message(event):
    """Cache a message for later retrieval if deleted."""
    message = event.message
    if message is None or isinstance(message, MessageService):
        return

    # Skip if we shouldn't track this chat type
    chat = await event.get_chat()
    is_private = bool(message.is_private)
    is_saved = message.out and is_private and chat.id == MY_USER_ID

    if is_saved and not TRACK_SAVED_MESSAGES:
        return
    if is_private and not is_private_chat_allowed(chat):
        return
    if not is_private and not TRACK_GROUPS:
        return

    # Get sender info
    sender = await event.get_sender()
    sender_name = "Unknown"
    if sender:
        if getattr(sender, "username", None):
            sender_name = f"@{sender.username}"
        else:
            first = getattr(sender, "first_name", "") or ""
            last = getattr(sender, "last_name", "") or ""
            sender_name = (first + " " + last).strip() or str(message.sender_id)

    # Get chat name
    chat_name = "Unknown"
    if is_private:
        chat_name = f"Private: {sender_name}"
    else:
        chat_name = getattr(chat, "title", None) or f"Chat {chat.id}"

    # Get media info
    media_info = {
        "media_type": None,
        "media_caption": None,
        "media_path": None,
    }

    if message.media:
        # Check for TTL (self-destructing media) - works for both photos and documents
        ttl = None
        if hasattr(message.media, "ttl_seconds") and message.media.ttl_seconds:
            ttl = message.media.ttl_seconds

        if isinstance(message.media, MessageMediaPhoto):
            media_info["media_type"] = "Photo"
            media_info["media_caption"] = message.text or ""
            if ttl:
                media_info["media_type"] = f"Photo (TTL: {ttl}s)"
                # Download immediately!
                if TRACK_TTL_MEDIA:
                    asyncio.create_task(capture_ttl_media(message, ttl))
        elif isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            if "video" in mime:
                media_info["media_type"] = "Video"
            elif "audio" in mime:
                media_info["media_type"] = "Audio"
            elif "image" in mime:
                media_info["media_type"] = "Photo"
            else:
                media_info["media_type"] = f"File ({mime})"
            media_info["media_caption"] = message.text or ""

            if ttl:
                media_info["media_type"] += f" (TTL: {ttl}s)"
                # Download immediately!
                if TRACK_TTL_MEDIA:
                    asyncio.create_task(capture_ttl_media(message, ttl))

    # Cache the message
    msg_data = {
        "message_id": message.id,
        "text": message.text or "",
        "sender_id": message.sender_id,
        "sender_name": sender_name,
        "chat_name": chat_name,
        "chat_id": message.chat_id,
        "timestamp": message.date.timestamp() if message.date else time.time(),
        "media_type": media_info["media_type"],
        "media_caption": media_info["media_caption"],
        "media_path": media_info["media_path"],
        "has_media": bool(message.media),
    }

    MESSAGE_CACHE[message.chat_id].append(msg_data)
    STATS["messages_tracked"] += 1


def is_private_chat_allowed(chat):
    """Check if we should track this private chat."""
    if not TRACK_PRIVATE_CHATS:
        return False
    # Skip bots in private chats (to avoid loops)
    if getattr(chat, "bot", False):
        return False
    return True


async def capture_ttl_media(message, ttl):
    """Immediately download self-destructing media before it expires."""
    try:
        # First, send a notification that we're capturing
        sender = await message.get_sender()
        sender_name = "Unknown"
        if sender:
            if getattr(sender, "username", None):
                sender_name = f"@{sender.username}"
            else:
                first = getattr(sender, "first_name", "") or ""
                sender_name = first or str(message.sender_id)

        chat = await message.get_chat()
        chat_name = "Private chat"
        if not message.is_private:
            chat_name = getattr(chat, "title", None) or f"Group {chat.id}"

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        logger.info(f"📸 TTL media detected from {sender_name} in {chat_name} (TTL={ttl}s)")

        # Determine file extension
        ext = "bin"
        if isinstance(message.media, MessageMediaPhoto):
            ext = "jpg"
        elif isinstance(message.media, MessageMediaDocument):
            doc = message.media.document
            mime = getattr(doc, "mime_type", "") or ""
            if "video" in mime: ext = "mp4"
            elif "audio" in mime: ext = "mp3"
            elif "image" in mime: ext = "jpg"
            else: ext = "bin"

        filename = f"ttl_{int(time.time())}_{message.id}.{ext}"
        filepath = TEMP_DIR / filename

        # Download the media IMMEDIATELY (before user opens it)
        # Use download_media with a short timeout
        try:
            path = await asyncio.wait_for(
                message.download_media(file=str(filepath)),
                timeout=min(ttl, 30)  # Don't wait longer than TTL or 30s
            )
        except asyncio.TimeoutError:
            logger.warning(f"TTL media download timed out for message {message.id}")
            return
        except Exception as e:
            logger.error(f"TTL media download error: {e}")
            return

        if not path or not filepath.exists() or filepath.stat().st_size == 0:
            logger.warning(f"TTL media download failed for message {message.id}")
            return

        file_size_mb = filepath.stat().st_size / (1024 * 1024)
        logger.info(f"✅ TTL media downloaded: {filepath.name} ({file_size_mb:.1f}MB)")

        # Send to keeper bot
        caption = (
            f"⏱ <b>Self-destructing media saved</b>\n\n"
            f"👤 <b>Sender:</b> {sender_name}\n"
            f"💬 <b>Chat:</b> {chat_name}\n"
            f"⏰ <b>Time:</b> {timestamp}\n"
            f"🔥 <b>TTL:</b> {ttl} seconds\n"
            f"📁 <b>Size:</b> {file_size_mb:.1f}MB\n"
        )
        if message.text:
            caption += f"\n📝 <b>Caption:</b> {message.text}\n"

        success = await send_to_keeper(content=caption, file=str(filepath), caption=caption)

        # Clean up
        with suppress(Exception):
            filepath.unlink()

        if success:
            STATS["ttl_captured"] += 1
            logger.info(f"✅ TTL media sent to keeper bot")

    except Exception as e:
        logger.error(f"Failed to capture TTL media: {e}")
        STATS["errors"] += 1


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------
@userbot.on(events.MessageDeleted())
async def on_message_deleted(event):
    """Handle deleted messages."""
    if not TRACK_DELETED_MESSAGES:
        return

    # event.deleted_ids is a list of deleted message IDs
    # event.chat_id is the chat where deletion happened (may be None for private chats)
    chat_id = event.chat_id

    if chat_id is None:
        # For private chats, we need to check all cached chats
        # This is a Telethon limitation
        for cached_chat_id, msg_deque in list(MESSAGE_CACHE.items()):
            for msg_id in event.deleted_ids:
                for msg_data in list(msg_deque):
                    if msg_data["message_id"] == msg_id:
                        await forward_deleted_message(msg_data)
                        return
        return

    # Specific chat
    msg_deque = MESSAGE_CACHE.get(chat_id)
    if not msg_deque:
        return

    for msg_id in event.deleted_ids:
        for msg_data in list(msg_deque):
            if msg_data["message_id"] == msg_id:
                await forward_deleted_message(msg_data)
                break


async def forward_deleted_message(msg_data: dict):
    """Forward a deleted message to the keeper bot."""
    logger.info(f"🗑 Deleted message from {msg_data['sender_name']} in {msg_data['chat_name']}")

    info_text = format_message_info(msg_data)

    # Try to get the original message from cache and download its media
    # Note: we already have the message in cache, but the message object
    # may already be deleted on Telegram's side
    # So we need to download media BEFORE the deletion event

    # For now, just send the text info
    # (media capture for deleted messages is harder - we'd need to download proactively)
    await send_to_keeper(content=info_text)
    STATS["deletions_captured"] += 1


@userbot.on(events.NewMessage(incoming=True, outgoing=True))
async def on_new_message(event):
    """Cache all incoming messages."""
    await cache_message(event)


# ---------------------------------------------------------------------------
# Keeper bot handlers (for /start command and stats)
# ---------------------------------------------------------------------------
@keeper_bot.on(events.NewMessage(pattern=r"^/start|^\.start"))
async def keeper_start(event):
    """When user starts the keeper bot, save their chat ID."""
    global KEEPER_CHAT_ID
    user_id = event.sender_id

    # Track user
    sender = await event.get_sender()
    username = getattr(sender, "username", None) if sender else None
    first_name = getattr(sender, "first_name", None) if sender else None
    track_user(user_id, username, first_name)

    # Check if blocked
    if is_user_blocked(user_id):
        return  # Silently ignore blocked users

    # Set keeper chat ID (only admin can do this)
    if user_id == ADMIN_USER_ID:
        KEEPER_CHAT_ID = event.chat_id
        _save_settings()

    await event.reply(
        "👋 Hello! I am the deleted messages and self-destructing media saver bot.\n\n"
        "🔍 I am currently in monitoring mode.\n"
        "📊 To view stats: /stats\n"
        "❓ For help: /help\n\n"
        "👨‍💻 <i>Created by arshia</i>"
    )
    logger.info(f"User {user_id} (@{username}) started the bot")


@keeper_bot.on(events.NewMessage(pattern=r"^/stats|^\.stats"))
async def keeper_stats(event):
    """Show stats."""
    user_id = event.sender_id
    if is_user_blocked(user_id):
        return
    if user_id != ADMIN_USER_ID:
        await event.reply("⛔ This command is only for the admin.")
        return

    await event.reply(
        f"📊 <b>Keeper Bot Stats</b>\n\n"
        f"📝 Monitored Messages: {STATS['messages_tracked']}\n"
        f"🗑 Captured Deleted Messages: {STATS['deletions_captured']}\n"
        f"⏱ Captured TTL Media: {STATS['ttl_captured']}\n"
        f"❌ Errors: {STATS['errors']}\n\n"
        f"💬 Monitored Chats: {len(MESSAGE_CACHE)}\n"
        f"👥 Registered Users: {len(USERS_DB)}\n"
        f"🚫 Blocked Users: {len(BLOCKED_USERS)}",
        parse_mode="html",
    )


@keeper_bot.on(events.NewMessage(pattern=r"^/help|^\.help"))
async def keeper_help(event):
    """Show help."""
    user_id = event.sender_id
    if is_user_blocked(user_id):
        return
    is_admin = (user_id == ADMIN_USER_ID)

    help_text = (
        "📖 <b>Help</b>\n\n"
        "🔍 This bot works automatically:\n"
        "• It saves deleted messages in your chats\n"
        "• It saves self-destructing media (TTL photos/videos) before they are deleted\n\n"
        "Commands:\n"
        "/start or .start - Start\n"
        "/stats or .stats - Statistics\n"
        "/help or .help - Help\n\n"
    )
    if is_admin:
        help_text += (
            "\n<b>🔐 Admin Commands (Only for you):</b>\n"
            ".users - List all users\n"
            ".block <user_id> - Block user\n"
            ".unblock <user_id> - Unblock user\n"
            ".blocked - List blocked users\n"
            ".blockall - Block all current users\n"
            ".broadcast <message> - Message to all\n"
            ".clearall - Clear all messages in this chat\n"
        )

    help_text += "\n⚠️ <b>Note:</b> Only messages received after the bot is activated can be saved."
    await event.reply(help_text, parse_mode="html")


# ---------------------------------------------------------------------------
# Admin-only commands
# ---------------------------------------------------------------------------
@keeper_bot.on(events.NewMessage(pattern=r"^\.users$"))
async def admin_users(event):
    """List all users who started the bot."""
    if event.sender_id != ADMIN_USER_ID:
        return

    if not USERS_DB:
        await event.reply("📭 No users registered yet.")
        return

    lines = [f"👥 <b>User List ({len(USERS_DB)} users)</b>\n"]
    for uid, info in USERS_DB.items():
        username = info.get("username")
        name = info.get("first_name") or "?"
        last_seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.get("last_seen", 0)))
        starts = info.get("starts", 1)
        blocked = "🚫" if uid in BLOCKED_USERS else "✅"
        uname = f" @{username}" if username else ""
        lines.append(f"{blocked} <code>{uid}</code> | {name}{uname} | {starts}× | {last_seen}")

    # Split into chunks if too long
    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await event.reply(text[i:i+4000], parse_mode="html")
    else:
        await event.reply(text, parse_mode="html")


@keeper_bot.on(events.NewMessage(pattern=r"^\.block\s+(\S+)"))
async def admin_block(event):
    """Block a user by ID."""
    if event.sender_id != ADMIN_USER_ID:
        return

    arg = event.pattern_match.group(1)
    try:
        uid = int(arg)
    except ValueError:
        await event.reply("❌ Invalid numeric ID.\nExample: <code>.block 123456789</code>", parse_mode="html")
        return

    if uid == ADMIN_USER_ID:
        await event.reply("❌ You cannot block yourself.")
        return

    if block_user(uid):
        info = USERS_DB.get(uid, {})
        name = info.get("first_name") or "?"
        username = info.get("username")
        uname = f" (@{username})" if username else ""
        await event.reply(f"✅ User blocked:\n<code>{uid}</code> | {name}{uname}", parse_mode="html")
    else:
        await event.reply("⚠️ This user is already blocked.")


@keeper_bot.on(events.NewMessage(pattern=r"^\.unblock\s+(\S+)"))
async def admin_unblock(event):
    """Unblock a user by ID."""
    if event.sender_id != ADMIN_USER_ID:
        return

    arg = event.pattern_match.group(1)
    try:
        uid = int(arg)
    except ValueError:
        await event.reply("❌ Invalid numeric ID.", parse_mode="html")
        return

    if unblock_user(uid):
        await event.reply(f"✅ User unblocked: <code>{uid}</code>", parse_mode="html")
    else:
        await event.reply("⚠️ This user was not blocked.")


@keeper_bot.on(events.NewMessage(pattern=r"^\.blocked$"))
async def admin_blocked(event):
    """List blocked users."""
    if event.sender_id != ADMIN_USER_ID:
        return

    if not BLOCKED_USERS:
        await event.reply("📭 Block list is empty.")
        return

    lines = [f"🚫 <b>Blocked Users ({len(BLOCKED_USERS)} users)</b>\n"]
    for uid in BLOCKED_USERS:
        info = USERS_DB.get(uid, {})
        name = info.get("first_name") or "?"
        username = info.get("username")
        uname = f" (@{username})" if username else ""
        lines.append(f"<code>{uid}</code> | {name}{uname}")

    await event.reply("\n".join(lines), parse_mode="html")


@keeper_bot.on(events.NewMessage(pattern=r"^\.blockall$"))
async def admin_blockall(event):
    """Block ALL currently tracked users (admin only)."""
    if event.sender_id != ADMIN_USER_ID:
        return

    if not USERS_DB:
        await event.reply("📭 No user to block.")
        return

    count = 0
    for uid in list(USERS_DB.keys()):
        if uid != ADMIN_USER_ID and uid not in BLOCKED_USERS:
            BLOCKED_USERS.add(uid)
            count += 1
    _save_blocked()

    await event.reply(f"✅ {count} users were blocked.", parse_mode="html")


@keeper_bot.on(events.NewMessage(pattern=r"^\.broadcast\s+(.+)"))
async def admin_broadcast(event):
    """Broadcast a message to all users."""
    if event.sender_id != ADMIN_USER_ID:
        return

    msg = event.pattern_match.group(1).strip()
    if not msg:
        await event.reply("❌ Message is empty.\nExample: <code>.broadcast Hello everyone</code>", parse_mode="html")
        return

    sent = 0
    failed = 0
    for uid in list(USERS_DB.keys()):
        if uid in BLOCKED_USERS:
            continue
        try:
            await keeper_bot.send_message(uid, f"📢 <b>Message from Admin:</b>\n\n{msg}", parse_mode="html")
            sent += 1
            await asyncio.sleep(0.5)  # Avoid flood limits
        except Exception as e:
            failed += 1
            logger.warning(f"Broadcast failed for {uid}: {e}")

    await event.reply(f"✅ Message sent to {sent} users.\n❌ {failed} errors.", parse_mode="html")


@keeper_bot.on(events.NewMessage(pattern=r"^\.clearall$"))
async def admin_clearall(event):
    """Delete ALL messages in the bot's chat with admin."""
    if event.sender_id != ADMIN_USER_ID:
        return

    status_msg = await event.reply("⏳ Clearing messages...")

    # Get the chat where the command was sent
    chat_id = event.chat_id

    # Iterate over all messages in this chat and delete them
    deleted_count = 0
    failed = 0
    try:
        # Get all messages in the chat (we'll iterate backwards)
        # Note: bot can only delete messages it sent OR messages older than 48h
        # if it has delete rights. For private chats with bot, bot can delete
        # all messages it sent.
        async for msg in keeper_bot.iter_messages(chat_id):
            try:
                await msg.delete()
                deleted_count += 1
                # Avoid flood limits
                if deleted_count % 20 == 0:
                    await asyncio.sleep(0.5)
            except Exception as e:
                failed += 1
                logger.warning(f"Failed to delete message {msg.id}: {e}")
    except Exception as e:
        logger.error(f"clearall error: {e}")
        await event.reply(f"❌ Error: {e}")
        return

    # Send a confirmation message (will be the only one left)
    await keeper_bot.send_message(
        chat_id,
        f"🧹 <b>{deleted_count} messages cleared.</b>\n❌ {failed} errors.",
        parse_mode="html",
    )


# ---------------------------------------------------------------------------
# Block check for ALL keeper bot messages
# ---------------------------------------------------------------------------
@keeper_bot.on(events.NewMessage())
async def keeper_check_blocked(event):
    """Silently ignore all messages from blocked users."""
    if event.sender_id == ADMIN_USER_ID:
        return  # Admin can always use the bot
    if is_user_blocked(event.sender_id):
        # Don't process any further
        raise events.StopPropagation


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def main():
    global MY_USER_ID, KEEPER_CHAT_ID

    logger.info("=" * 60)
    logger.info("Keeper Bot starting...")
    logger.info("=" * 60)

    # Start keeper bot first
    await keeper_bot.start(bot_token=KEEPER_BOT_TOKEN)  # type: ignore
    keeper_me = await keeper_bot.get_me()
    logger.info(f"✅ Keeper bot: @{keeper_me.username}")

    # Start userbot (your account)
    await userbot.start(phone=PHONE_NUMBER)  # type: ignore
    if not await userbot.is_user_authorized():
        logger.error("Userbot login failed!")
        return

    me = await userbot.get_me()
    MY_USER_ID = me.id
    logger.info(f"✅ Userbot: {me.first_name} (@{me.username or 'no_username'}) [id={me.id}]")
    logger.info("")
    logger.info("🔍 Keeper Bot is now monitoring your chats!")
    logger.info("   • Deleted messages will be captured")
    logger.info("   • Self-destructing media will be saved")
    logger.info("")
    logger.info("⚠️  To activate, send /start to your keeper bot:")
    logger.info(f"    https://t.me/{keeper_me.username}")
    logger.info("")
    logger.info("Press Ctrl+C to stop.")
    logger.info("=" * 60)

    # Run both clients concurrently
    await asyncio.gather(
        userbot.run_until_disconnected(),
        keeper_bot.run_until_disconnected(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
