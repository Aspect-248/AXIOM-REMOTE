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
# Power-mode ("do") tasks run several steps and wait on human approvals; time
# spent waiting for an approval doesn't count against this.
POWER_TIMEOUT_SECONDS = 1800
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


def _kill_tree(proc):
    """claude.exe spawns the tool server, which spawns PowerShell; killing
    only the parent would orphan them."""
    subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def _serve_approvals(outbox: str, approval, handled: set) -> float:
    """Hand each new approval request the tool server filed to `approval`
    (blocking) and write back its answer. Returns seconds spent waiting."""
    waited = 0.0
    for path in sorted(glob.glob(os.path.join(outbox, "approval_*.req.json"))):
        if path in handled:
            continue
        handled.add(path)
        try:
            with open(path, encoding="utf-8") as f:
                request = json.load(f)
        except (OSError, ValueError):
            continue
        started = time.time()
        try:
            approved, reason = approval(request)
        except Exception as e:
            log.exception("Approval handler failed")
            approved, reason = False, f"approval failed: {e}"
        response_path = path.replace(".req.json", ".resp.json")
        with open(response_path + ".tmp", "w", encoding="utf-8") as f:
            json.dump({"approved": bool(approved), "reason": reason}, f)
        os.replace(response_path + ".tmp", response_path)
        waited += time.time() - started
    return waited


class ClaudeCodeAgent:
    def __init__(self, system_prompt: str, power: bool = False):
        self._power = power
        self.last_usage: "dict | None" = None
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

    def run(self, user_text: str, approval=None) -> "tuple[str, list]":
        """Blocking -- call from a worker thread. Returns (reply, outbox
        items); items are photos/files to send plus guard requests.

        `approval(request) -> (approved, reason)` is called (blocking) for
        each command a power-mode task wants to run."""
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
                                "env": {
                                    "AXIOM_OUTBOX_DIR": outbox,
                                    **({"AXIOM_POWER_MODE": "1"} if self._power else {}),
                                },
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
            timeout = POWER_TIMEOUT_SECONDS if self._power else TIMEOUT_SECONDS
            if self._power:
                # Claude Code gives up on a slow MCP tool call; a tool that waits on
                # a human approval is slow by design.
                env["MCP_TOOL_TIMEOUT"] = str(timeout * 1000)
            work_dir = os.path.join(tempfile.gettempdir(), "axiom_ask_cwd")
            os.makedirs(work_dir, exist_ok=True)

            log.info("Claude Code %s: %s", "do" if self._power else "ask", user_text)
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=work_dir,
                env=env,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            deadline = time.time() + timeout
            send_input, handled = prompt, set()
            try:
                while True:
                    try:
                        stdout, stderr = proc.communicate(input=send_input, timeout=0.5)
                        break
                    except subprocess.TimeoutExpired:
                        send_input = None
                    if approval is not None:
                        deadline += _serve_approvals(outbox, approval, handled)
                    if time.time() > deadline:
                        raise RuntimeError(f"Claude Code took longer than {timeout}s and was stopped.")
            finally:
                if proc.poll() is None:
                    _kill_tree(proc)

            result = None
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, list):
                    parsed = next((e for e in reversed(parsed) if e.get("type") == "result"), None)
                result = parsed
            except ValueError:
                pass
            if not isinstance(result, dict):
                raise RuntimeError((stderr or stdout or "no output").strip()[:300])
            if result.get("is_error"):
                raise RuntimeError(str(result.get("result") or "Claude Code reported an error")[:300])

            reply = str(result.get("result") or "").strip()
            usage = result.get("usage") or {}
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
            self.last_usage = {
                # List-price equivalent: what the same tokens would cost on the API.
                # A subscription isn't billed per request; this just shows relative weight.
                "cost_usd": float(result.get("total_cost_usd") or 0.0),
                "input_tokens": int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + cache_read,
                "cache_read_tokens": cache_read,
                "output_tokens": int(usage.get("output_tokens") or 0),
                "turns": int(result.get("num_turns") or 0),
                "api_seconds": round((result.get("duration_api_ms") or 0) / 1000, 1),
                "model": next(iter(result.get("modelUsage") or {}), ""),
            }
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
