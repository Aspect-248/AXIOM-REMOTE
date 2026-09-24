"""Closes the hacker-screen kiosk window after a delay, run as a
genuinely independent process (not a thread inside bot.py).

Why a separate process: a thread's auto-close timer dies with its
parent process. If the bot restarts or crashes while a kiosk window
is open -- which happens routinely during development, and could
happen for other reasons -- an in-process timer would never fire,
leaving a fullscreen window stuck open with nothing left running to
close it (reproduced this directly while testing). A separate OS
process keeps running regardless of what happens to bot.py.

Why match by --user-data-dir marker instead of the launched PID: Edge
relaunches itself internally after the initial launch (visible as
--edge-skip-compat-layer-relaunch in the final process's command
line), so the PID subprocess.Popen() returns often doesn't match the
actual running browser by the time this fires -- also reproduced
directly, killing that stale PID had no effect and left the whole
process tree (browser + gpu + renderer + crashpad + utility) orphaned.
The --user-data-dir path is unique per launch and shared by every
process in that launch's tree, so matching on it catches all of them
regardless of any internal relaunch.
"""
import logging
import os
import shutil
import sys
import time

import psutil

log = logging.getLogger("hacker_screen_watchdog")
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    filename=os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log"),
)


def _kill_all_matching(marker: str) -> int:
    """Kill every process whose command line contains marker, waiting
    for each to actually die before moving on. Returns how many were
    found (not necessarily all successfully killed -- callers should
    call this again to see if any are still turning up)."""
    matches = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info["cmdline"] or []
            if any(marker in arg for arg in cmdline):
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for proc in matches:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            proc.wait(timeout=2)
        except (psutil.NoSuchProcess, psutil.TimeoutExpired):
            pass

    return len(matches)


def main():
    marker = sys.argv[1]
    duration_seconds = float(sys.argv[2])
    user_data_dir = sys.argv[3]

    time.sleep(duration_seconds)

    # Edge's startup involves multiple internal relaunch/spawn stages,
    # so a single kill pass can miss processes that appear slightly
    # after it runs. By the time this fires (after the full display
    # duration), Edge should be long settled, but retry generously
    # anyway rather than assume that.
    total_found = 0
    for attempt in range(15):
        found = _kill_all_matching(marker)
        total_found += found
        log.info("hacker_screen_watchdog attempt %d: found/killed %d", attempt, found)
        if found == 0:
            break
        time.sleep(1)
    else:
        log.warning("hacker_screen_watchdog: processes still matching after all retries")

    log.info("hacker_screen_watchdog done, total_found=%d", total_found)

    # Windows file locks can briefly outlive a killed process (seen on
    # the user-data-dir's cache/log files specifically), so retry
    # rather than leave a one-shot ignore_errors failure.
    for _ in range(5):
        time.sleep(1)
        shutil.rmtree(user_data_dir, ignore_errors=True)
        if not os.path.exists(user_data_dir):
            break


if __name__ == "__main__":
    main()
