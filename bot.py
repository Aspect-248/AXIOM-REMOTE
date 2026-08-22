import logging
import os
import subprocess
import tempfile

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


def open_explorer():
    subprocess.Popen(["explorer.exe"])
    return "Opening File Explorer."


def open_downloads():
    subprocess.Popen(["explorer.exe", os.path.expanduser("~\\Downloads")])
    return "Opening Downloads."


def open_desktop():
    subprocess.Popen(["explorer.exe", os.path.expanduser("~\\Desktop")])
    return "Opening Desktop."


def open_vscode():
    subprocess.Popen("code .", shell=True, cwd=os.path.expanduser("~"))
    return "Opening VS Code."


def lock_pc():
    import ctypes
    ctypes.windll.user32.LockWorkStation()
    return "Locked."


# 60s delay so a shutdown/restart can be cancelled with "cancel shutdown"
# if it was triggered by mistake.
SHUTDOWN_DELAY_SECONDS = 60


def shutdown_pc():
    subprocess.run(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY_SECONDS)])
    return f"Shutting down in {SHUTDOWN_DELAY_SECONDS}s. Send 'cancel shutdown' to abort."


def restart_pc():
    subprocess.run(["shutdown", "/r", "/t", str(SHUTDOWN_DELAY_SECONDS)])
    return f"Restarting in {SHUTDOWN_DELAY_SECONDS}s. Send 'cancel shutdown' to abort."


def cancel_shutdown():
    subprocess.run(["shutdown", "/a"])
    return "Pending shutdown/restart cancelled."


def sleep_pc():
    subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
    return "Sleeping."


def _volume_interface():
    from pycaw.utils import AudioUtilities

    return AudioUtilities.GetSpeakers().EndpointVolume


def mute():
    _volume_interface().SetMute(1, None)
    return "Muted."


def unmute():
    _volume_interface().SetMute(0, None)
    return "Unmuted."


def volume_up():
    vol = _volume_interface()
    level = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() + 0.1))
    vol.SetMasterVolumeLevelScalar(level, None)
    return f"Volume: {round(level * 100)}%"


def volume_down():
    vol = _volume_interface()
    level = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() - 0.1))
    vol.SetMasterVolumeLevelScalar(level, None)
    return f"Volume: {round(level * 100)}%"


def screenshot():
    from PIL import ImageGrab

    path = os.path.join(tempfile.gettempdir(), "axiom_screenshot.png")
    ImageGrab.grab().save(path)
    return {"photo": path}


def status():
    return "AXIOM laptop is on and listening."


# Allowlist: only these exact phrases (case-insensitive) do anything.
# Deliberately NOT a generic "run whatever text you send" handler --
# this bot has real access to the machine, so the set of things it can
# do is kept small and explicit on purpose. Add new commands here.
COMMANDS = {
    "open chrome": open_chrome,
    "open notepad": open_notepad,
    "open explorer": open_explorer,
    "open downloads": open_downloads,
    "open desktop": open_desktop,
    "open vscode": open_vscode,
    "lock": lock_pc,
    "shutdown": shutdown_pc,
    "restart": restart_pc,
    "cancel shutdown": cancel_shutdown,
    "sleep": sleep_pc,
    "mute": mute,
    "unmute": unmute,
    "volume up": volume_up,
    "volume down": volume_down,
    "screenshot": screenshot,
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

    if isinstance(result, dict) and "photo" in result:
        with open(result["photo"], "rb") as f:
            await update.message.reply_photo(f)
    else:
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
