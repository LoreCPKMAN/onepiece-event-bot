# One Piece Card Game – Event Monitor Bot

A Telegram bot that monitors the [One Piece Card Game official events page](https://en.onepiece-cardgame.com/events/) and notifies a Telegram group whenever a new **Regional** or **Treasure Cup** event is posted.

---

## How it works

1. **Every hour** the bot scrapes the main events page.
2. Events whose names contain `Regional` or `Treasure Cup` (case-insensitive) are extracted with their name, period, and URL.
3. The URL list is compared against `seen_events.json` (stored on disk).
4. For each **new** URL a second request fetches the event detail page to collect venue/city information and a registration link if available.
5. A formatted Telegram message is sent to the configured group.

---

## Prerequisites

- Python 3.11+
- A Telegram bot token (from BotFather)
- The numeric ID of the Telegram group where notifications should be sent

---

## Step 1 – Create a Telegram bot via BotFather

1. Open Telegram and search for **@BotFather**.
2. Send `/newbot` and follow the prompts (choose a name and username).
3. Copy the **token** it gives you – this is your `TELEGRAM_BOT_TOKEN`.

---

## Step 2 – Get the Telegram group chat ID

1. Add your new bot to the target Telegram group (make it an admin so it can post).
2. Send any message in the group.
3. Open this URL in your browser (replace `<TOKEN>` with your bot token):

   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```

4. Find the `"chat"` object in the JSON response. The `"id"` field (a negative number for groups, e.g. `-1001234567890`) is your `TELEGRAM_CHAT_ID`.

---

## Step 3 – Local development

```bash
# Clone / copy the project, then:
cd onepiece-event-bot

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_BOT_TOKEN="your_token_here"
export TELEGRAM_CHAT_ID="-1001234567890"

# Run the bot
python main.py
```

On first run the bot immediately performs one check, then repeats every hour.  
`seen_events.json` is created automatically in the same directory.

---

## Step 4 – Deploy to Railway

1. Push the project to a GitHub repository (or use the Railway CLI to deploy directly).
2. In Railway, create a **New Project → Deploy from GitHub repo**.
3. Go to **Variables** and add:
   | Key | Value |
   |-----|-------|
   | `TELEGRAM_BOT_TOKEN` | your bot token |
   | `TELEGRAM_CHAT_ID` | your group chat ID |
4. **Attach a Volume** to the service (right-click the service → *Attach Volume*, mount path e.g. `/data`).
   Railway exposes it as `RAILWAY_VOLUME_MOUNT_PATH` and the bot stores `seen_events.json` there automatically.
   Without a volume the file is wiped on every restart.
5. Railway auto-detects the `Procfile` and runs the `worker` process; `railway.json` sets the restart policy to `ALWAYS`.
6. The bot starts, performs an immediate check, and then runs hourly.

> **Serverless / App Sleeping:** Railway sleeps a service after 10 minutes without outbound traffic, and a worker
> is never woken up again. The bot sends a lightweight keep-alive request to the Telegram API every 4 minutes to stay awake.
>
> **First run:** when no `seen_events.json` exists, the bot records every event already on the site *without* notifying,
> then sends a single "Bot avviato" summary. If you see that summary after every restart, the volume is not attached correctly.

---

## Notification format

```
🏴‍☠️ NEW EVENT DETECTED!

📌 Name: Championship 26-27 Regionals Season 2
📅 Period: August 2026 onwards
📍 Location: Indianapolis, IN / Oklahoma City, OK / Calgary, AB
🔗 Link: https://en.onepiece-cardgame.com/events/regional-season2-26-27.html
📝 Register: https://...  (if found on the detail page)
```

---

## Files

| File | Purpose |
|------|---------|
| `main.py` | Bot logic – scraping, scheduling, Telegram notifications |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway / Heroku process definition |
| `seen_events.json` | Auto-generated – tracks already-notified event URLs |

---

## Customising the keyword filter

Edit the `KEYWORDS` list in `main.py`:

```python
KEYWORDS = ["regional", "treasure cup"]
```

Add or remove lowercase strings to change which event names trigger notifications.
