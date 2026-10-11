"""MCP server that hands a fenced Claude Code session the bot's fixed
toolbox (the same tools the API backend gets) and nothing else.

Launched per request by agent.ClaudeCodeAgent via --mcp-config, over stdio.
Files/photos the tools want sent to the owner, and guard-mode requests,
can't live in this process's memory, so they're appended as JSON lines to
$AXIOM_OUTBOX_DIR/outbox.jsonl for the bot to pick up after Claude Code exits.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import bot
from agent import Tool

log = logging.getLogger("telegram-remote.mcp")

OUTBOX_DIR = os.environ.get("AXIOM_OUTBOX_DIR")
if not OUTBOX_DIR:
    raise SystemExit("AXIOM_OUTBOX_DIR is not set")
OUTBOX_FILE = os.path.join(OUTBOX_DIR, "outbox.jsonl")


def _write_outbox(items):
    with open(OUTBOX_FILE, "a", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item) + "\n")


def _flush_bot_outbox():
    items = list(bot._agent_outbox)
    bot._agent_outbox.clear()
    if items:
        _write_outbox(items)


def _set_guard_mode(args):
    _write_outbox([{"action": "guard", "enabled": bool(args.get("enabled"))}])
    return "Guard mode change requested; the bot applies it and confirms to the owner."


TOOLS = {t.name: t for t in bot._build_agent_tools()}
# The bot process owns guard state, so this tool only files a request.
TOOLS["set_guard_mode"].fn = _set_guard_mode


# --- Power mode: run_powershell, gated by an approval code ---
#
# Only registered when the bot launches this server for a `do` task. Before
# anything runs, the exact command is written to approval_<id>.req.json; the
# bot shows it to the owner in Telegram and writes approval_<id>.resp.json
# once a valid one-time code arrives (or the owner says no / it times out).
POWER_MODE = os.environ.get("AXIOM_POWER_MODE") == "1"
APPROVAL_WAIT_SECONDS = 330
MAX_COMMAND_CHARS = 1500
DEFAULT_COMMAND_TIMEOUT = 60
MAX_COMMAND_TIMEOUT = 600
MAX_OUTPUT_CHARS = 15000
POWERSHELL_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

# A tripwire, not a wall: string matching can't stop a determined obfuscated
# command. The real gate is the owner reading the command before approving.
_BLOCKED_PATTERNS = [
    r"\.env\b",
    r"totp",
    r"\.credentials",
    r"[\\/]\.claude\b",
    r"owner_face",
    r"known_faces",
    r"guard_state",
    r"bot\.log",
    r"cmdkey",
    r"vaultcmd",
    r"telegram_bot_token",
    r"anthropic_api_key",
]
_SECRET_ENV_VARS = {
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_ALLOWED_CHAT_ID",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
}


def _blocked_reason(command: str):
    lowered = command.lower()
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, lowered):
            return f"matches protected pattern {pattern!r}"
    return None


def _wait_for_approval(command: str, purpose: str, timeout: int):
    request_id = uuid.uuid4().hex[:12]
    request = {"id": request_id, "command": command, "purpose": purpose, "timeout": timeout}
    tmp_path = os.path.join(OUTBOX_DIR, f"approval_{request_id}.req.tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(request, f)
    os.replace(tmp_path, os.path.join(OUTBOX_DIR, f"approval_{request_id}.req.json"))

    resp_path = os.path.join(OUTBOX_DIR, f"approval_{request_id}.resp.json")
    deadline = time.time() + APPROVAL_WAIT_SECONDS
    while time.time() < deadline:
        if os.path.exists(resp_path):
            with open(resp_path, encoding="utf-8") as f:
                response = json.load(f)
            return bool(response.get("approved")), str(response.get("reason") or "")
        time.sleep(0.3)
    return False, "No approval arrived in time."


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    half = MAX_OUTPUT_CHARS // 2
    return f"{text[:half]}\n...[{len(text) - MAX_OUTPUT_CHARS} chars cut]...\n{text[-half:]}"


def _run_powershell(args):
    command = str(args.get("command") or "").strip()
    purpose = str(args.get("purpose") or "").strip() or "(no reason given)"
    timeout = max(1, min(int(args.get("timeout_seconds") or DEFAULT_COMMAND_TIMEOUT), MAX_COMMAND_TIMEOUT))

    if not command:
        raise ValueError("command is empty")
    if len(command) > MAX_COMMAND_CHARS:
        raise ValueError(
            f"Command is {len(command)} characters; the owner has to read every command on a phone, "
            f"so the limit is {MAX_COMMAND_CHARS}. Split it into smaller steps."
        )
    blocked = _blocked_reason(command)
    if blocked:
        log.warning("Power mode blocked command (%s): %s", blocked, command)
        raise ValueError(f"Refused: the command {blocked}. These files and credentials are off-limits in every mode.")

    approved, reason = _wait_for_approval(command, purpose, timeout)
    if not approved:
        log.info("Power mode DENIED (%s): %s", reason, command)
        raise ValueError(f"The owner did not approve this command ({reason or 'denied'}). Do not retry it; report back instead.")

    log.info("Power mode APPROVED, running: %s", command)
    env = {k: v for k, v in os.environ.items() if k.upper() not in _SECRET_ENV_VARS}
    try:
        proc = subprocess.run(
            [
                POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + command,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=os.path.expanduser("~"),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return f"The command was approved but ran past its {timeout}s timeout and was stopped."

    out = proc.stdout.strip()
    err = proc.stderr.strip()
    parts = [f"exit code: {proc.returncode}"]
    if out:
        parts.append("stdout:\n" + out)
    if err:
        parts.append("stderr:\n" + err)
    return _clip("\n".join(parts))


if POWER_MODE:
    TOOLS["run_powershell"] = Tool(
        "run_powershell",
        "Run a PowerShell command on the laptop, as the owner (not elevated -- no admin rights). "
        "The owner sees this exact command in Telegram and must approve each one with a one-time code, "
        "so make each command small, readable and honest; never obfuscate or chain unrelated actions. "
        "If it's denied or times out, stop and report instead of retrying.",
        {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The exact PowerShell command (max 1500 characters)."},
                "purpose": {"type": "string", "description": "One plain sentence on why you're running it."},
                "timeout_seconds": {"type": "integer", "description": "Kill it after this long (default 60, max 600)."},
            },
            "required": ["command", "purpose"],
        },
        _run_powershell,
    )


def _to_content(output):
    if isinstance(output, str):
        return [types.TextContent(type="text", text=output or "Done.")]
    content = []
    for block in output:
        if block["type"] == "image":
            src = block["source"]
            content.append(types.ImageContent(type="image", data=src["data"], mime_type=src["media_type"]))
        else:
            content.append(types.TextContent(type="text", text=block["text"]))
    return content


async def on_list_tools(ctx, params):
    return types.ListToolsResult(
        tools=[
            types.Tool(name=t.name, description=t.description, input_schema=t.input_schema)
            for t in TOOLS.values()
        ]
    )


async def on_call_tool(ctx, params):
    tool = TOOLS.get(params.name)
    if tool is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"No such tool: {params.name}")], is_error=True
        )
    log.info("MCP tool call: %s %s", params.name, params.arguments)
    try:
        output = await asyncio.to_thread(tool.fn, params.arguments or {})
        _flush_bot_outbox()
        return types.CallToolResult(content=_to_content(output))
    except Exception as e:
        log.exception("MCP tool failed: %s", params.name)
        bot._agent_outbox.clear()
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Tool failed: {e}")], is_error=True
        )


server = Server("axiom", on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
