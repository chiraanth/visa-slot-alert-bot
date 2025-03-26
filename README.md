# 🛂 visa-slot-alert-bot 🇺🇸
> A Telegram bot that monitors [visaslots.info](https://visaslots.info/) for US visa appointment availability at Indian consulates. It alerts users with detailed, timely messages, allowing highly personalized filtering and automation support.

---

## 🌐 Table of Contents
- [✨ Features](#-features)
- [🛠️ Setup](#-setup)
  - [1. Clone & Install Dependencies](#1-clone--install-dependencies)
  - [2. Configure Environment Variables](#2-configure-environment-variables)
  - [3. Create Your Telegram Bot (BotFather)](#3-create-your-telegram-bot-botfather)
  - [4. Get Your Telegram Chat ID](#4-get-your-telegram-chat-id)
  - [5. Run the Bot](#5-run-the-bot)
- [⚙️ Run as a Background Service](#️-run-as-a-background-service)
- [📌 How It Works](#-how-it-works)
- [📋 Commands](#-commands)
- [🧠 Pro Tips](#-pro-tips)

---

## ✨ Features

- ⚡ **Real-time scraping** from [visaslots.info](https://visaslots.info/)
- 🎯 **Personalized filters**:
  - Visa type (B1, B2, F-1, H-1B, etc.)
  - Consulate city (Mumbai, Hyderabad, etc.)
  - Consulate type (VAC / CONSULAR)
  - Year filter (2025, 2026)
  - Alert interval (e.g., every 10 minutes)
- 📦 Sends **Telegram alerts** with full details of availability
- 🔕 **Smart notification** system prevents spam during no-slot conditions
- 🧩 Modular and easy to extend
- 🖥️ Optional: Runs 24/7 as a **systemd service** (Linux)

---

## 🛠️ Setup

### 1. Clone & Install Dependencies

```bash
git clone https://github.com/yourusername/visa-slot-alert-bot.git
cd visa-slot-alert-bot

# Optional: Use a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file at the project root:

```env
BOT_TOKEN=your_bot_token_here
CHAT_ID=your_chat_id_here
```

🛡️ Your `.env` file contains sensitive information. Ensure it's listed in `.gitignore` and never pushed to GitHub. 
Refer to `.env.sample` as a template.

### 3. Create Your Telegram Bot (BotFather)

1. In Telegram, search `@BotFather`
2. Send `/start`, then `/newbot`
3. Choose a name and a unique username (must end in `bot`)
4. BotFather will return a **bot token** — this goes in your `.env` as `BOT_TOKEN`
5. Send a message like `/start` to your new bot to activate it for the first time

### 4. Get Your Telegram Chat ID

To get your `CHAT_ID`, do the following:

1. Send a message to your bot (e.g., `/start`)
2. Use this cURL command to get updates:
```bash
curl https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
```
3. Look for `"chat":{"id":<YOUR_ID>}` in the JSON
4. Copy and paste it into your `.env` as `CHAT_ID`

### 5. Run the Bot

```bash
python3 bot.py
```

Your bot will begin polling Telegram and respond to user interactions.
Look for:
```
🤖 Bot is running...
```

Interact with it on Telegram:
- `/start`
- `/set_visa`
- `/set_consulate`
- `/start_alerts`
- `/stop`

---

## ⚙️ Run as a Background Service (Linux only)

### Step 1: Create a systemd Service File
Create the file: `service/visa-alert-bot.service`

```ini
[Unit]
Description=Visa Slot Alert Telegram Bot
After=network.target

[Service]
WorkingDirectory=/home/yourusername/visa-slot-alert-bot
ExecStart=/usr/bin/python3 bot.py
Restart=always
User=yourusername
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

Update the paths and username appropriately.

### Step 2: Enable the Service
```bash
sudo cp service/visa-alert-bot.service /etc/systemd/system/
sudo systemctl daemon-reexec
sudo systemctl enable visa-alert-bot
sudo systemctl start visa-alert-bot
```

Check its status:
```bash
sudo systemctl status visa-alert-bot
```

---

## 📌 How It Works

1. Scrapes [visaslots.info](https://visaslots.info/) every X minutes (your configured interval)
2. Matches results with your selected visa type, city, consulate type, and year
3. Sends alerts via Telegram when:
   - Slots are available (with full details)
   - Alternative consulates have availability (optional notice)
4. Will not repeatedly notify you unless conditions change
5. Keeps checking silently in the background

---

## 📋 Commands

| Command | Description |
|--------|-------------|
| `/start` | Initializes the bot and shows a welcome message |
| `/set_visa` | Select visa type (B1, B2, F-1, etc.) |
| `/set_consulate` | Choose city and consulate type (VAC/CONSULAR) |
| `/set_interval` | Set time interval for checks (5 to 60 min) |
| `/start_alerts` | Begin the periodic checking and alert loop |
| `/stop` | Stop alerts |

---

## 🧠 Pro Tips

- 🔁 Want multiple people to receive alerts? Share the bot token with them and ask each to start it and get their chat ID
- 🧪 Test locally first before enabling systemd
- ☁️ Use a VPS (like DigitalOcean or AWS) to host it 24/7 if you're traveling
- 🔍 Use `getUpdates` often while debugging to confirm if messages are reaching your bot

---

> Built with ❤️ for all Indian students, travelers, professionals, and families navigating the US visa process.

---

