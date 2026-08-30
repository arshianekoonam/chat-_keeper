<div align="center">
  <img src="https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python Badge"/>
  <img src="https://img.shields.io/badge/Telegram-2CA5E0?style=for-the-badge&logo=telegram&logoColor=white" alt="Telegram Badge"/>
  
  <h1>🤖 Chat Keeper Bot</h1>
  <p><strong>Your Ultimate Shield Against Deleted Telegram Messages!</strong></p>
</div>

<br>

## 🚀 Overview

**Chat Keeper Bot** is a powerful Telegram Userbot that sits quietly in the background and makes sure you never miss a deleted message or a self-destructing media again. Using Pyrogram, it listens to your chats and instantly forwards any deleted content to a private, secure destination bot. 

Have you ever seen the *"This message was deleted"* text and wondered what it was? **Never again!**

---

## ✨ Features

- 🛡️ **Anti-Delete System**: Automatically captures and saves messages right before they are deleted by the sender.
- 📸 **Self-Destructing Media Saver**: Intercepts "view once" (TTL) photos and videos and stores them safely.
- 👥 **Group & Private Support**: Works flawlessly in private chats (DMs) as well as groups.
- ⚡ **Ultra Fast**: Powered by an efficient caching system that handles messages with zero lag.
- 🎛️ **Admin Control Panel**: Manage your tracked chats securely through a private Telegram bot interface.
- 🚫 **Block System**: Ability to exclude specific users or chats from being tracked.

---

## 🛠️ Installation & Setup

1. **Clone the repository:**
   ```bash
   git clone https://github.com/arshianekoonam/chat-_keeper.git
   cd chat-_keeper
   ```

2. **Install the dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure the bot:**
   - Copy the example configuration file:
     ```bash
     cp config.example.py config.py
     ```
   - Open `config.py` and edit the following variables:
     - Get your `API_ID` and `API_HASH` by logging into [my.telegram.org](https://my.telegram.org).
     - Get a new bot token from [@BotFather](https://t.me/BotFather) and set it as `KEEPER_BOT_TOKEN`.
     - Set `PHONE_NUMBER` to your personal Telegram phone number (with country code).
     - Set `ADMIN_USER_ID` to your numerical Telegram ID.

4. **Run the bot:**
   ```bash
   python keeper_bot.py
   ```
   *Note: Because this is a Userbot, on the very first run, it will ask for a **Telegram verification code** in the terminal. After entering the code, it automatically creates a `.session` file connected to your account so you won't need to log in again.*

---

## ⚙️ Configuration Variables

| Variable | Description |
|----------|-------------|
| `API_ID` | Your Telegram API ID |
| `API_HASH` | Your Telegram API HASH |
| `KEEPER_BOT_TOKEN` | The Bot token that will act as your archive/storage |
| `ADMIN_USER_ID` | Your numeric Telegram ID to control the bot |
| `TRACK_DELETED_MESSAGES` | Set `True` to enable deleted message tracking |
| `TRACK_TTL_MEDIA` | Set `True` to enable "view once" media saving |

---

## ⚠️ Disclaimer

> [!WARNING]  
> This project is for personal and educational purposes only. Please respect the privacy of others and use this tool responsibly. The developer is not responsible for any misuse of this software.

---
<br>

<div align="center">
  <p>developed pixel by pixel with ❤️ by arshia</p>
</div>
