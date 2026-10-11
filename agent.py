"""Claude-powered natural-language front end for the bot.

Turns a free-form message ("send me the lab report I edited yesterday")
into calls on a small, fixed set of tools that bot.py supplies. Claude
never gets a shell: the only things it can do are the tools handed to
Agent(), each bounded the same way the exact-match commands are.

Kept free of bot.py imports (and of a top-level `anthropic` import) so
the bot still starts if the SDK or API key is missing.
"""

import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

log = logging.getLogger("telegram-remote.agent")

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 8000
# Opus 5.5 always thinks; effort is the only dial. Low keeps replies
# quick for what is mostly "pick the right tool" work.
EFFORT = "low"
MAX_TOOL_ROUNDS = 8
HISTORY_MESSAGES = 16
HISTORY_TTL_SECONDS = 20 * 60
MAX_CALLS_PER_HOUR = 30

FALLBACK_BETA = "server-side-fallback-2026-07-01"


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    # Receives the model's input dict; returns text, or a list of
    # content blocks (e.g. an image block plus a text block).
    fn: Callable[[dict], "str | list"]


def _echo_content(content):
    """After a mid-output refusal fallback, thinking/tool_use blocks that
    came before the final `fallback` marker must not be sent back."""
    last_fallback = max(
        (i for i, block in enumerate(content) if block.type == "fallback"), default=-1
    )
    if last_fallback < 0:
        return content
    return [
        block
        for i, block in enumerate(content)
        if i > last_fallback or block.type not in ("thinking", "redacted_thinking", "tool_use")
    ]


class Agent:
    def __init__(self, system_prompt: str, tools: list[Tool]):
        self._system = system_prompt
        self._tools = {t.name: t for t in tools}
        self._tool_schemas = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in tools
        ]
        self._history: list[dict] = []
        self._last_activity = 0.0
        self._call_times: deque[float] = deque()
        self._use_fallbacks = True
        self._client = None

    def available(self) -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def reset(self):
        self._history.clear()

    def _get_client(self):
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, client, messages, system):
        import anthropic

        kwargs = dict(
            model=os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=self._tool_schemas,
            messages=messages,
            output_config={"effort": EFFORT},
        )
        if self._use_fallbacks:
            try:
                return client.beta.messages.create(
                    betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **kwargs
                )
            except anthropic.BadRequestError as e:
                if "fallback" not in str(e).lower():
                    raise
                log.warning("Server-side fallbacks rejected, continuing without: %s", e)
                self._use_fallbacks = False
        return client.messages.create(**kwargs)

    def _run_tool(self, block) -> dict:
        tool = self._tools.get(block.name)
        if tool is None:
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"No such tool: {block.name}",
                "is_error": True,
            }
        log.info("Agent tool call: %s %s", block.name, block.input)
        try:
            output = tool.fn(block.input or {})
        except Exception as e:
            log.exception("Agent tool failed: %s", block.name)
            return {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Tool failed: {e}",
                "is_error": True,
            }
        return {"type": "tool_result", "tool_use_id": block.id, "content": output or "Done."}

    def run(self, user_text: str, client=None) -> str:
        """Blocking -- call from a worker thread. Returns the reply text."""
        now = time.time()
        if now - self._last_activity > HISTORY_TTL_SECONDS:
            self._history.clear()
        self._last_activity = now

        while self._call_times and now - self._call_times[0] > 3600:
            self._call_times.popleft()
        if len(self._call_times) >= MAX_CALLS_PER_HOUR:
            return f"That's {MAX_CALLS_PER_HOUR} AI requests in the last hour -- pausing to limit spend."
        self._call_times.append(now)

        client = client or self._get_client()
        system = f"{self._system}\n\nCurrent local time: {datetime.now():%A %Y-%m-%d %H:%M}."
        messages = self._history + [{"role": "user", "content": user_text}]

        reply = ""
        for _ in range(MAX_TOOL_ROUNDS):
            response = self._create(client, messages, system)

            if response.stop_reason == "refusal":
                return "Claude declined that request."

            messages.append({"role": "assistant", "content": _echo_content(response.content)})
            reply = "".join(b.text for b in response.content if b.type == "text").strip()

            if response.stop_reason != "tool_use":
                if response.stop_reason == "max_tokens":
                    reply = (reply + " (cut off)").strip()
                break

            results = [self._run_tool(b) for b in response.content if b.type == "tool_use"]
            messages.append({"role": "user", "content": results})
        else:
            reply = reply or f"Stopped after {MAX_TOOL_ROUNDS} steps without finishing."

        self._history.append({"role": "user", "content": user_text})
        self._history.append({"role": "assistant", "content": reply or "Done."})
        del self._history[:-HISTORY_MESSAGES]
        return reply
