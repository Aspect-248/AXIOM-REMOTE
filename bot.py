import ctypes
import difflib
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import timedelta

import psutil
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
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


def stats():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    uptime = timedelta(seconds=int(time.time() - psutil.boot_time()))

    lines = [
        f"CPU: {cpu}%",
        f"RAM: {mem.percent}% ({mem.used // (1024**3)}GB / {mem.total // (1024**3)}GB)",
        f"Disk (C:): {disk.percent}% used",
        f"Uptime: {uptime}",
    ]
    battery = psutil.sensors_battery()
    if battery:
        plug_state = "plugged in" if battery.power_plugged else "on battery"
        lines.append(f"Battery: {battery.percent}% ({plug_state})")
    return "\n".join(lines)


# Files can only be fetched from these folders, by exact or partial
# filename match -- not an arbitrary path-read.
SEARCH_FOLDERS = [
    os.path.expanduser("~\\Desktop"),
    os.path.expanduser("~\\Downloads"),
    os.path.expanduser("~\\Documents"),
]


def find_file(filename: str):
    filename = filename.strip()
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None

    target = filename.lower()
    matches = []
    for folder in SEARCH_FOLDERS:
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            full_path = os.path.join(folder, name)
            if os.path.isfile(full_path) and target in name.lower():
                matches.append(full_path)

    exact = [m for m in matches if os.path.basename(m).lower() == target]
    if exact:
        return exact[0]
    return matches[0] if matches else None


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
    "stats": stats,
}

# Alternate phrasings that resolve to a command above. Still a fixed,
# known set of actions -- just more ways to say them. Add more here.
ALIASES = {
    "chrome": "open chrome",
    "browser": "open chrome",
    "open browser": "open chrome",
    "notepad": "open notepad",
    "explorer": "open explorer",
    "files": "open explorer",
    "open files": "open explorer",
    "downloads": "open downloads",
    "desktop": "open desktop",
    "vscode": "open vscode",
    "vs code": "open vscode",
    "code": "open vscode",
    "lock pc": "lock",
    "lock laptop": "lock",
    "shut down": "shutdown",
    "turn off": "shutdown",
    "power off": "shutdown",
    "reboot": "restart",
    "cancel": "cancel shutdown",
    "abort shutdown": "cancel shutdown",
    "sleep pc": "sleep",
    "suspend": "sleep",
    "mute volume": "mute",
    "unmute volume": "unmute",
    "vol up": "volume up",
    "louder": "volume up",
    "vol down": "volume down",
    "quieter": "volume down",
    "screen shot": "screenshot",
    "take screenshot": "screenshot",
    "ss": "screenshot",
    "ping": "status",
    "are you there": "status",
    "sysinfo": "stats",
    "system stats": "stats",
    "system info": "stats",
}

# Below this similarity ratio (0-1), a typo is treated as "unknown"
# rather than guessed at.
FUZZY_MATCH_CUTOFF = 0.75


def _normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text)


def _resolve_command(text: str):
    """Return (canonical_command, matched_phrase) or (None, None)."""
    if text in COMMANDS:
        return text, text
    if text in ALIASES:
        alias_target = ALIASES[text]
        return alias_target, text

    known_phrases = list(COMMANDS) + list(ALIASES)
    close = difflib.get_close_matches(text, known_phrases, n=1, cutoff=FUZZY_MATCH_CUTOFF)
    if not close:
        return None, None

    matched_phrase = close[0]
    canonical = matched_phrase if matched_phrase in COMMANDS else ALIASES[matched_phrase]
    return canonical, matched_phrase


async def _handle_get_file(update: Update, filename: str):
    log.info("Fetching file: %s", filename)
    path = find_file(filename)
    if path is None:
        folders = ", ".join(os.path.basename(f) for f in SEARCH_FOLDERS)
        await update.message.reply_text(f"No file matching '{filename}' found in {folders}.")
        return

    try:
        with open(path, "rb") as f:
            await update.message.reply_document(f, filename=os.path.basename(path))
    except Exception as e:
        log.exception("Failed to send file: %s", path)
        await update.message.reply_text(f"Failed to send file: {e}")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)

    if not ALLOWED_CHAT_ID or chat_id != ALLOWED_CHAT_ID:
        log.warning("Ignored message from unauthorized chat_id=%s", chat_id)
        return

    raw_text = (update.message.text or "").strip()

    get_match = re.match(r"(?i)^get\s+(.+)$", raw_text)
    if get_match:
        await _handle_get_file(update, get_match.group(1))
        return

    text = _normalize(raw_text)
    canonical, matched_phrase = _resolve_command(text)
    handler = COMMANDS.get(canonical) if canonical else None

    if handler is None:
        available = ", ".join(sorted(COMMANDS))
        await update.message.reply_text(f"Unknown command. Available: {available}")
        return

    log.info("Executing command: %s (matched: %s)", canonical, matched_phrase)
    try:
        result = handler()
    except Exception as e:
        log.exception("Command failed: %s", canonical)
        await update.message.reply_text(f"Command failed: {e}")
        return

    if text != canonical and isinstance(result, str):
        result = f"({canonical}) {result}"

    if isinstance(result, dict) and "photo" in result:
        with open(result["photo"], "rb") as f:
            await update.message.reply_photo(f)
    else:
        await update.message.reply_text(result)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> float:
    info = _LastInputInfo()
    info.cbSize = ctypes.sizeof(_LastInputInfo)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    millis_idle = ctypes.windll.kernel32.GetTickCount() - info.dwTime
    return millis_idle / 1000.0


# Proactive alert thresholds. Each has a low/high pair so an alert
# fires once when crossing into the bad range, then stays quiet until
# things clearly recover (hysteresis) instead of repeating every check.
BATTERY_LOW_PERCENT = 15
BATTERY_RECOVERED_PERCENT = 25
DISK_FULL_PERCENT = 90
DISK_RECOVERED_PERCENT = 80
IDLE_ALERT_SECONDS = 2 * 60 * 60  # 2 hours
ALERT_CHECK_INTERVAL_SECONDS = 5 * 60

_alert_state = {"battery_low": False, "disk_full": False, "idle": False}


async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    battery = psutil.sensors_battery()
    if battery is not None:
        if (
            battery.percent <= BATTERY_LOW_PERCENT
            and not battery.power_plugged
            and not _alert_state["battery_low"]
        ):
            _alert_state["battery_low"] = True
            await context.bot.send_message(
                chat_id=ALLOWED_CHAT_ID,
                text=f"Battery low: {battery.percent}%. Plug in the laptop.",
            )
        elif battery.power_plugged or battery.percent >= BATTERY_RECOVERED_PERCENT:
            _alert_state["battery_low"] = False

    disk_percent = psutil.disk_usage("C:\\").percent
    if disk_percent >= DISK_FULL_PERCENT and not _alert_state["disk_full"]:
        _alert_state["disk_full"] = True
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"Disk (C:) is {disk_percent}% full.",
        )
    elif disk_percent < DISK_RECOVERED_PERCENT:
        _alert_state["disk_full"] = False

    idle_seconds = get_idle_seconds()
    if idle_seconds >= IDLE_ALERT_SECONDS and not _alert_state["idle"]:
        _alert_state["idle"] = True
        idle_display = timedelta(seconds=int(idle_seconds))
        await context.bot.send_message(
            chat_id=ALLOWED_CHAT_ID,
            text=f"Laptop has been idle for {idle_display}.",
        )
    elif idle_seconds < 60:
        _alert_state["idle"] = False


def main():
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    if not ALLOWED_CHAT_ID:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is not set (see .env.example)")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.job_queue.run_repeating(check_alerts, interval=ALERT_CHECK_INTERVAL_SECONDS, first=60)

    log.info("Bot starting, listening for commands from chat_id=%s", ALLOWED_CHAT_ID)
    app.run_polling()


if __name__ == "__main__":
    main()
