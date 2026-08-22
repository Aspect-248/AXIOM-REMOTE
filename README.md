# telegram-remote

Send commands from Telegram to control this laptop. Locked down to an
allowlist of specific commands and a single Telegram chat ID — it does
not execute arbitrary text as shell commands.

## Setup

1. **Create a bot:** in Telegram, message [@BotFather](https://t.me/BotFather),
   send `/newbot`, follow the prompts. It gives you a bot token
   (looks like `123456789:AAExampleTokenHere`).
2. **Get your chat ID:** message your new bot anything (e.g. "hi"), then
   open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a
   browser. Look for `"chat":{"id":123456789,...}` in the response —
   that number is your chat ID.
3. **Configure:** copy `.env.example` to `.env` and fill in both values:
   ```
   TELEGRAM_BOT_TOKEN=...
   TELEGRAM_ALLOWED_CHAT_ID=...
   ```
4. **Install deps:**
   ```
   pip install -r requirements.txt
   ```
5. **Run:**
   ```
   python bot.py
   ```

Leave it running, then message your bot from Telegram. Messages from
any chat ID other than the one in `.env` are silently ignored.

## Commands

- `open chrome`
- `open notepad`
- `lock`
- `status`

Add more by adding an entry to the `COMMANDS` dict in `bot.py`.
