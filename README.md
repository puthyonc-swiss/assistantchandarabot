# SwissKH Report Bot — Setup Guide

## What this bot does
1. You send a screenshot to the bot (private chat in Telegram)
2. Bot asks Gemini AI to read the round number from the photo
3. Bot asks you the start time (since photos don't usually show this)
4. Bot shows you a draft report (photo + caption) for approval
5. Bot shows checkboxes for your groups — tap the ones you want, then "Send"
6. Bot posts the photo + caption to those groups

## Files in this project
- `bot.py` — the bot's logic (don't need to edit this normally)
- `config.py` — **edit this** to change your groups, live score link, event name
- `requirements.txt` — list of Python packages needed
- `render.yaml` — tells Render how to run the bot

## Setup steps

### 1. Edit `config.py`
Open `config.py` and replace the placeholder values:
- `LIVE_SCORE_LINK` → your real live score URL
- `GROUPS` → your real group names + chat IDs (see below for how to get IDs)
- `EVENT_NAME` → your tournament's name

### 2. Get your group Chat IDs
1. Add your bot to each Telegram group (search its username, add like a person)
2. Send any message in that group (e.g. "test")
3. In your browser, visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   (replace `<YOUR_TOKEN>` with your real bot token)
4. Look for `"chat":{"id": -1001234567890, ...}` in the response — that number
   is the chat ID. Put it in `config.py`.

### 3. Deploy to Render
1. Push this folder to a GitHub repo (or upload directly if Render supports it)
2. In Render dashboard: New → Worker (NOT "Web Service")
3. Connect your repo
4. Build command: `pip install -r requirements.txt`
5. Start command: `python bot.py`
6. Add environment variables (Render dashboard → Environment tab):
   - `TELEGRAM_BOT_TOKEN` = your real bot token
   - `GEMINI_API_KEY` = your real Gemini API key
7. Deploy

### 4. Test it
1. Open Telegram, find your bot, send `/start`
2. Send a screenshot photo
3. Follow the prompts (round number if needed, then start time)
4. Check the draft looks right, tap your group checkboxes, tap "Send"
5. Check the group received the photo + caption

## Notes
- Never put your bot token or API key directly in any code file — always use
  Render's environment variables.
- If you ever leak a token by accident, revoke it immediately via @BotFather
  (`/mybots` → select bot → API Token → Revoke) and generate a new one.
