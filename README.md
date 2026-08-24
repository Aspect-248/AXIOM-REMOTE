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
- `open explorer`
- `open downloads`
- `open desktop`
- `open vscode`
- `lock`
- `shutdown` / `restart` — 60s delay, cancel with `cancel shutdown`
- `sleep`
- `mute` / `unmute`
- `volume up` / `volume down`
- `screenshot` — replies with a photo of the current screen
- `status`
- `stats` — CPU/RAM/disk/battery/uptime
- `get <filename>` — searches Desktop/Downloads/Documents for a
  matching file (exact or partial name) and sends it back as a
  document
- `play pause` / `next track` / `previous track` — media keys
- `webcam` — takes a photo with the webcam and sends it back
- `find` — beeps loudly (also unmutes/maxes volume first), and sends
  back a screenshot + a webcam photo plus your location. Uses Windows
  Location Services (WiFi-positioning, accurate to tens/hundreds of
  meters) with a reverse-geocoded address and a Google Maps link;
  falls back to coarse IP-based geolocation only if that's
  unavailable. If the webcam capture fails it just sends the
  screenshot alone rather than failing the whole command.
- `say <text>` — speaks the text out loud via Windows TTS
- `type <text>` — copies the text to the clipboard and pastes it into
  whatever window currently has focus on the laptop (note: this
  overwrites your current clipboard contents)
- `record` — records an 8-second screen capture and sends it back as
  a video
- `prank` — swaps the mouse buttons for 20s, wiggles the cursor, says
  something silly out loud, and opens a fresh Notepad window with a
  silly message typed into it (never touches any other already-open
  Notepad window)
- `register face` — captures a webcam frame and registers it as
  "you" for face recognition (one person only; re-run to replace)
- `check camera` — on-demand check: is the person in view you, a
  stranger, or nobody?

Send a file or photo directly to the bot (no command needed) and it
saves it to `~\Downloads\FromTelegram` on the laptop.

## Voice commands

Send a Telegram voice message instead of typing and the bot
transcribes it locally (via `faster-whisper`, no cloud/API key
involved) and runs whatever command it heard, same as if you'd typed
it. It replies with what it heard first so you can tell if the
transcription was off. First voice message after a fresh start takes
a couple seconds longer while the model loads into memory.

Add more by adding an entry to the `COMMANDS` dict in `bot.py`.

Each command also accepts alternate phrasings (see the `ALIASES` dict
in `bot.py` — e.g. "shut down", "turn off", and "power off" all trigger
`shutdown`) and tolerates small typos via fuzzy matching, so you don't
need to type a command exactly right for it to run.

## Proactive alerts

The bot also messages you unprompted (no command needed) when:
- battery drops to 15% or below while unplugged
- disk (C:) usage reaches 90%
- the laptop has been idle for 2+ hours

Each alert fires once per threshold crossing, then stays quiet until
things clearly recover, so it won't spam you. Checked every 5 minutes.
Thresholds live at the top of `check_alerts()` in `bot.py`.

## Notification mirror

Windows toast notifications (email, chat apps, etc.) get forwarded to
Telegram automatically, no command needed. Checked every 15 seconds
via Windows' notification-listener API (requires "Notifications" to
be turned on in Windows Settings > System). Only *new* notifications
are sent -- whatever's already in the Action Center when the bot
starts is used as a baseline, not dumped as a flood of messages.

This reads notifications via `notification_worker.py`, run as a
**separate subprocess** rather than in-process. That's deliberate: a
malformed notification was found to trigger a native crash (segfault)
deep in the underlying `winsdk` library when reading its text, and no
amount of Python `try/except` can catch that -- it's a hard native
crash. Isolating it in its own short-lived subprocess means a crash
there just fails that one 15-second poll (logged as a warning) instead
of taking the whole bot down.

## Face recognition / intruder alert

Setup: send `register face` once, facing the webcam. After that, the
bot checks the webcam every 3 minutes and messages you unprompted
with a photo if it sees a face that isn't you. No command needed once
registered; `check camera` runs the same check on demand for testing.

Heads up: this means the webcam LED will blink briefly every check,
even though nothing was asked of it -- that's the visible tradeoff of
proactive (vs. on-demand) monitoring. Only recognizes one person
("you" vs. "not you") -- re-running `register face` replaces the
previous registration rather than adding a second person.

Uses OpenCV's built-in YuNet (detection) + SFace (recognition) DNN
models rather than the `dlib`-based `face_recognition` package, which
needs a C++ compiler toolchain to install on Windows and often fails.
Model files live in `models/` (downloaded from the official
`opencv/opencv_zoo` repo, checksum-verified); your registered face
(`models/owner_face.npy`) is generated locally and gitignored -- it's
personal biometric data, never committed.
