"""
Configuration for Keeper Bot - Example Template
Copy this file to config.py and replace values with your own.
"""
import os
from pathlib import Path

# Telegram API credentials (get from https://my.telegram.org)
API_ID = 123456
API_HASH = "your_api_hash_here"

# Phone number (with country code)
PHONE_NUMBER = "+12345678900"

# Session file name
SESSION_NAME = "my_session"

# The destination bot token - this is where deleted messages will be forwarded
# (get from @BotFather)
KEEPER_BOT_TOKEN = "your_bot_token_here"

# Where to forward: bot's chat with you (we'll auto-detect)
# After first run, the bot will save your chat ID with the Bot
DESTINATION_CHAT_ID = None  # Will be auto-detected on first /start

# Admin user ID (your Telegram account ID) - only this user can run admin commands
ADMIN_USER_ID = 123456789

# Settings
# Cache size: how many recent messages to keep in memory (per chat)
CACHE_SIZE_PER_CHAT = 200

# What to track
TRACK_DELETED_MESSAGES = True
TRACK_TTL_MEDIA = True  # Self-destructing photos/videos

# Where to listen
TRACK_PRIVATE_CHATS = True
TRACK_GROUPS = True

# Should we also track messages in Saved Messages?
TRACK_SAVED_MESSAGES = False

# Log file for stats
LOG_FILE = Path(__file__).parent / "keeper.log"
