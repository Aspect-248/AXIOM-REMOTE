"""Claude Code backend for ask mode.

Same toolbox as agent.Agent, but driven through the Claude Code program so
requests count against a Claude subscription instead of API billing.

Fenced: all built-in tools (shell, file, web) are switched off with
--tools "", only the MCP server in mcp_server.py is loaded
(--strict-mcp-config), only its tools are pre-approved, and anything else
that would need a permission prompt is denied outright.
"""

import glob
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import deque
from datetime import datetime

from agent import EFFORT, HISTORY_MESSAGES, HISTORY_TTL_SECONDS, MAX_CALLS_PER_HOUR

log = logging.getLogger("telegram-remote.claude-code")

DEFAULT_MODEL = "sonnet"
TIMEOUT_SECONDS = 180
LOGIN_CACHE_SECONDS = 60
MCP_SERVER_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")


def find_claude_code() -> "str | None":
    """The desktop app bundles Claude Code under a version-numbered folder
    that changes on every update, so look up the newest copy each time."""
    override = os.environ.get("CLAUDE_CODE_EXE")
    if override and os.path.isfile(override):
        return override
    on_path = shutil.which("claude")
    if on_path:
        return on_path
    # The Claude desktop app is a packaged Store app: only processes it
    # launches see its files at %APPDATA%; everything else (this bot, a
    # normal terminal) has to use the real location inside the package folder.
    patterns = [
        os.path.join(
            os.environ.get("LOCALAPPDATA", ""), "Packages", "Claude_*",
            "LocalCache", "Roaming", "Claude", "claude-code", "*", "*", "claude.exe",
        ),
        os.path.join(os.environ.get("APPDATA", ""), "Claude", "claude-code", "*", "*", "claude.exe"),
    ]
    candidates = [path for pattern in patterns for path in glob.glob(pattern)]

    def version_key(path):
        version = os.path.basename(os.path.dirname(os.path.dirname(path)))
        return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))

    return max(candidates, key=version_key) if candidates else None


class ClaudeCodeAgent:
    def __init__(self, system_prompt: str):
        self._system = system_prompt
        self._history: list[tuple[str, str]] = []
        self._last_activity = 0.0
        self._call_times: deque[float] = deque()
        self._login_checked_at = 0.0
        self._login_ok = False

    def _logged_in(self, exe: str) -> bool:
        now = time.time()
        if now - self._login_checked_at < LOGIN_CACHE_SECONDS:
            return self._login_ok
        self._login_checked_at = now
        try:
            proc = subprocess.run(
                [exe, "auth", "status"],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            self._login_ok = bool(json.loads(proc.stdout).get("loggedIn"))
        except Exception:
            log.exception("Claude Code login check failed")
            self._login_ok = False
        return self._login_ok

    def unavailable_reason(self) -> "str | None":
        exe = find_claude_code()
        if exe is None:
            return "Claude Code isn't installed (looked on PATH and in the Claude desktop app's folder)."
        if not os.path.isfile(os.path.join(os.path.dirname(sys.executable), "python.exe")):
            return "Can't find python.exe next to the bot's interpreter to run the tool server."
        if not self._logged_in(exe):
            return "Claude Code isn't signed in. Run: claude auth login --claudeai"
        return None

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def reset(self):
        self._history.clear()

    def run(self, user_text: str) -> "tuple[str, list]":
        """Blocking -- call from a worker thread. Returns (reply, outbox
        items); items are photos/files to send plus guard requests."""
        now = time.time()
        if now - self._last_activity > HISTORY_TTL_SECONDS:
            self._history.clear()
        self._last_activity = now

        while self._call_times and now - self._call_times[0] > 3600:
            self._call_times.popleft()
        if len(self._call_times) >= MAX_CALLS_PER_HOUR:
            return f"That's {MAX_CALLS_PER_HOUR} AI requests in the last hour -- pausing to limit usage.", []
        self._call_times.append(now)

        exe = find_claude_code()
        if exe is None:
            raise RuntimeError("Claude Code isn't installed.")

        outbox = tempfile.mkdtemp(prefix="axiom_ask_")
        try:
            config_path = os.path.join(outbox, "mcp.json")
            with open(config_path, "w") as f:
                json.dump(
                    {
                        "mcpServers": {
                            "axiom": {
                                "command": os.path.join(os.path.dirname(sys.executable), "python.exe"),
                                "args": [MCP_SERVER_SCRIPT],
                                "env": {"AXIOM_OUTBOX_DIR": outbox},
                            }
                        }
                    },
                    f,
                )

            if self._history:
                lines = [f"{who}: {text}" for who, text in self._history]
                prompt = "Earlier in this chat:\n" + "\n".join(lines) + f"\n\nOwner's new message: {user_text}"
            else:
                prompt = user_text

            system = f"{self._system}\n\nCurrent local time: {datetime.now():%A %Y-%m-%d %H:%M}."
            cmd = [
                exe, "-p", "--output-format", "json",
                "--tools", "",
                "--strict-mcp-config", "--mcp-config", config_path,
                "--allowedTools", "mcp__axiom",
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--system-prompt", system,
                "--model", os.environ.get("CLAUDE_CODE_MODEL") or DEFAULT_MODEL,
                "--effort", EFFORT,
                "--disable-slash-commands", "--no-session-persistence",
                "--setting-sources", "",
            ]
            # An API key in the environment would silently switch Claude Code
            # from the subscription to pay-per-use billing.
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            work_dir = os.path.join(tempfile.gettempdir(), "axiom_ask_cwd")
            os.makedirs(work_dir, exist_ok=True)

            log.info("Claude Code ask: %s", user_text)
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    cwd=work_dir,
                    env=env,
                    timeout=TIMEOUT_SECONDS,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except subprocess.TimeoutExpired:
                raise RuntimeError(f"Claude Code took longer than {TIMEOUT_SECONDS}s and was stopped.")

            result = None
            try:
                parsed = json.loads(proc.stdout)
                if isinstance(parsed, list):
                    parsed = next((e for e in reversed(parsed) if e.get("type") == "result"), None)
                result = parsed
            except ValueError:
                pass
            if not isinstance(result, dict):
                raise RuntimeError((proc.stderr or proc.stdout or "no output").strip()[:300])
            if result.get("is_error"):
                raise RuntimeError(str(result.get("result") or "Claude Code reported an error")[:300])

            reply = str(result.get("result") or "").strip()
            items = []
            outbox_file = os.path.join(outbox, "outbox.jsonl")
            if os.path.exists(outbox_file):
                with open(outbox_file, encoding="utf-8") as f:
                    items = [json.loads(line) for line in f if line.strip()]
        finally:
            shutil.rmtree(outbox, ignore_errors=True)

        self._history.append(("Owner", user_text))
        self._history.append(("Assistant", reply or "Done."))
        del self._history[:-HISTORY_MESSAGES]
        return reply, items
