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
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

import bot

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
