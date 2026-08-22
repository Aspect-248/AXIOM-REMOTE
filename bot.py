import logging
import os
import subprocess

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("telegram-remote")


def open_chrome():
    subprocess.Popen("start chrome", shell=True)
    return "Opening Chrome."


def open_notepad():
    subprocess.Popen(["notepad.exe"])
    return "Opening Notepad."


def lock_pc():
    import ctypes
    ctypes.windll.user32.LockWorkStation()
    return "Locked."


def status():
    return "AXIOM laptop is on and listening."


# Allowlist: only these exact phrases (case-insensitive) do anything.
# Deliberately NOT a generic "run whatever text you send" handler --
# this bot has real access to the machine, so the set of things it can
# do is kept small and explicit on purpose. Add new commands here.
COMMANDS = {
    "open chrome": open_chrome,
    "open notepad": open_notepad,
    "lock": lock_pc,
    "status": status,
}


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored message from unauthorized chat_id=%s", chat_id)
        return

    text = (update.message.text or "").strip().lower()
    handler = COMMANDS.get(text)

    if handler is None:
        available = ", ".join(sorted(COMMANDS))
        await update.message.reply_text(f"Unknown command. Available: {available}")
        return

    log.info("Executing command: %s", text)
    try:
        result = handler()
    except Exception as e:
        log.exception("Command failed: %s", text)
        await update.message.reply_text(f"Command failed: {e}")
        return

    await update.message.reply_text(result)


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED_CHAT_ID:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is not set (see .env.example)")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info("Bot starting, listening for commands from chat_id=%s", ALLOWED_CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
