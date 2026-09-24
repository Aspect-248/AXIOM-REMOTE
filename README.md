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
- `calc <expression>` — safe arithmetic (`+ - * / // % **`, parens,
  and `sqrt/sin/cos/tan/log/log10/log2/exp/abs/round/floor/ceil/
  radians/degrees`, plus `pi/e/tau`). Not `eval()` -- a whitelisted
  AST evaluator, so arbitrary code can't run through it.
- `print <filename>` — sends a file from Desktop/Downloads/Documents
  to the default printer. Depends on Windows having a working "print"
  file association for that extension -- if that's broken (e.g. after
  uninstalling a PDF reader), you'll get a clear error saying so
  rather than a silent failure.
- `read <filename>` — reads a `.txt`/`.pdf`/etc. file out loud via
  TTS (first 2000 characters, truncated beyond that)
- `record` — records an 8-second screen capture and sends it back as
  a video
- `prank` — swaps the mouse buttons for 20s, wiggles the cursor, says
  something silly out loud, and opens a fresh Notepad window with a
  silly message typed into it (never touches any other already-open
  Notepad window)
- `register face` — registers you (the primary owner) for face
  recognition, 3 samples; re-run to replace
- `register face as <name>` — registers another specific person by
  name (e.g. "register face as Mum"), 5 samples; they sit in front of
  the webcam the same way. Re-using a name replaces that person's
  previous registration. Any number of named people can coexist
  alongside the owner.
- `check camera` — on-demand check: reports who's in view by name
  ("That's you and Mum -- recognized"), an unrecognized face, or
  nobody
- `remind me <text> in <N> seconds/minutes/hours/days` — one-off
  reminder, e.g. "remind me submit lab report in 2 hours"
- `remind me <text> at <time>` — e.g. "remind me join class at 5pm"
  or "at 14:30"; if that time already passed today, fires tomorrow
- `timer <N>` / `timer <N> minutes` — plain countdown timer, messages
  you when it's done
- `hacker screen` — full-screen Matrix rain + a fake hacking terminal
  sequence for ~20s, purely cosmetic theater (not a fake virus/
  ransomware warning -- deliberately over-the-top instead), then
  closes itself automatically
- `scan network` — ping-sweeps your subnet and lists every responding
  device's IP/MAC (and hostname where resolvable)
- `listen` — records ~6 seconds from the microphone and sends it back
  as an audio clip

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

Setup: send `register face` once, facing the webcam -- it takes 3
quick samples a moment apart (helps it generalize across lighting/
angle rather than relying on one snapshot). After that, the bot
checks the webcam every 30 minutes and messages you unprompted with a
photo if it sees a face that isn't recognized. No command needed once
registered; `check camera` runs the same check on demand for testing.

Beyond just you, `register face as <name>` registers any number of
other specific people by name (5 samples each -- they sit in front of
the webcam the same way, one person in frame at a time). Anyone
registered, under any name, counts as recognized; `check camera`
reports who specifically it saw (e.g. "That's you and Mum --
recognized"). Re-using a name replaces that person's previous
registration; each identity's samples are only ever matched against
each other, never mixed together.

Heads up: this means the webcam LED will blink briefly every check,
even though nothing was asked of it -- that's the visible tradeoff of
proactive (vs. on-demand) monitoring.

Two things specifically to cut false "unrecognized face" alerts on
the real owner (a single cosine-similarity check against one
reference photo turned out to misfire sometimes on lighting/angle/
expression changes): registration matches against the best of all 3
enrolled samples, not just one; and the proactive check only alerts
if a *second* frame, taken ~2s after the first, also fails to match
-- a single bad frame (blink, glare) no longer triggers it alone.

Every webcam capture (this feature, `webcam`, `find`) runs through
`webcam_worker.py` as a subprocess with a hard 15s timeout, not
in-process. `cv2.VideoCapture.read()` was found to hang indefinitely
on a flaky driver in practice -- the webcam LED stayed on for hours
with no exception raised, because a hung call never reaches its own
`finally: cap.release()`. A blocking hang like that can't be
interrupted from Python; the subprocess gets hard-killed on timeout
instead, which forces the OS to release the camera regardless of how
stuck the driver is.

Uses OpenCV's built-in YuNet (detection) + SFace (recognition) DNN
models rather than the `dlib`-based `face_recognition` package, which
needs a C++ compiler toolchain to install on Windows and often fails.
Model files live in `models/` (downloaded from the official
`opencv/opencv_zoo` repo, checksum-verified); registered faces
(`models/owner_face.npy` and `models/known_faces/*.npy`) are
generated locally and gitignored -- personal biometric data, never
committed.

## Git reminder

Every 30 minutes, checks a fixed list of project folders
(`GIT_WATCH_FOLDERS` in `bot.py`) for uncommitted changes via `git
status --porcelain`, and messages you once per folder if it finds
any -- stays quiet again once you commit, until the next time it goes
dirty. Add more folders to the list to watch other repos.

## USB device monitor

Every 60 seconds, messages you unprompted the name of any USB device
that gets plugged in (via `Get-PnpDevice`). Whatever's already
plugged in when the bot starts is used as a baseline, not reported.

## Hacker screen reliability

`hacker screen` launches a dedicated, isolated Edge kiosk window
(`--user-data-dir` set to a fresh temp folder so it can't collide
with or get confused for your normal browsing) and closes it via
`hacker_screen_watchdog.py`, run as its own independent OS process
rather than a thread inside the bot -- so it still closes the window
on schedule even if the bot restarts or crashes while it's showing.

Getting the close to actually work reliably took real iteration:
Edge relaunches itself internally after the initial launch, so the
PID the bot gets back from launching it often doesn't match the
actual running browser process anymore by the time the close fires --
tracking by PID alone left the whole process tree (browser + gpu +
renderer + crashpad + utility) orphaned. The watchdog instead matches
every process by the unique `--user-data-dir` path in its command
line (shared by the whole tree regardless of any internal relaunch),
and retries the kill pass since a single pass can still miss
processes if it runs mid-relaunch.

## Console-flash fix

The bot runs windowless via `pythonw.exe`. Launching a console-
subsystem program (`git`, `powershell`, `cmd` via `shell=True`,
`shutdown`) from a windowless process gives Windows nothing to attach
its console to, so it briefly creates and shows one anyway -- this
was visible as a quick double CMD-window flash roughly every 30
minutes (`git status` runs once per `GIT_WATCH_FOLDERS` entry, so two
folders = two flashes back to back, close in time to the intruder
check since both run on the same interval). Every such subprocess
call now passes `creationflags=NO_CONSOLE_WINDOW` to suppress it.
