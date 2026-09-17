"""Tool-call emulation: prompt protocol and incremental parser.

Kiro is an agent, not a raw model, so client-defined tools (Claude Code's
``Bash``/``Edit``, Codex's ``shell`` ...) cannot be passed through ACP. The
gateway instead describes the tools in the prompt and asks the model to emit::

    <tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>

:class:`ToolCallParser` extracts those blocks from the streamed text.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from kiro_acp.gateway.conversation import JSON, ToolCallPart, ToolChoice, ToolDef

OPEN_TAG = "<tool_call>"
CLOSE_TAG = "</tool_call>"

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(slots=True)
class ParsedToolCall:
    name: str
    arguments: JSON
    raw: str
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:24]}")

    def to_part(self) -> ToolCallPart:
        return ToolCallPart(id=self.id, name=self.name, arguments=self.arguments)


class ToolCallParser:
    """Incremental splitter of model text into plain text and tool calls.

    Feed chunks with :meth:`feed`; each call returns ``(text, calls)`` where
    ``text`` is safe to forward immediately and ``calls`` are fully parsed
    tool calls. Call :meth:`flush` at end of stream.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self._buffer = ""
        self._in_call = False
        self.calls: list[ParsedToolCall] = []

    def feed(self, chunk: str) -> tuple[str, list[ParsedToolCall]]:
        if not self.enabled:
            return chunk, []
        self._buffer += chunk
        out_text: list[str] = []
        out_calls: list[ParsedToolCall] = []
        while True:
            if self._in_call:
                end = self._buffer.find(CLOSE_TAG)
                if end < 0:
                    break
                body = self._buffer[:end]
                self._buffer = self._buffer[end + len(CLOSE_TAG) :]
                self._in_call = False
                call = _parse_body(body)
                if call is None:
                    out_text.append(f"{OPEN_TAG}{body}{CLOSE_TAG}")
                else:
                    self.calls.append(call)
                    out_calls.append(call)
                continue
            start = self._buffer.find(OPEN_TAG)
            if start >= 0:
                out_text.append(self._buffer[:start])
                self._buffer = self._buffer[start + len(OPEN_TAG) :]
                self._in_call = True
                continue
            # Hold back a possible partial "<tool_call>" prefix at the tail.
            keep = _partial_tag_suffix(self._buffer)
            emit = self._buffer[: len(self._buffer) - keep]
            self._buffer = self._buffer[len(self._buffer) - keep :]
            out_text.append(emit)
            break
        text = "".join(out_text)
        if out_calls:
            text = text.rstrip() if not self._buffer.strip() else text
        return text, out_calls

    def flush(self) -> tuple[str, list[ParsedToolCall]]:
        if not self.enabled:
            return "", []
        rest = self._buffer
        self._buffer = ""
        if self._in_call:
            self._in_call = False
            call = _parse_body(rest)
            if call is not None:
                self.calls.append(call)
                return "", [call]
            return f"{OPEN_TAG}{rest}", []
        return rest, []


def _partial_tag_suffix(text: str) -> int:
    longest = 0
    for length in range(1, len(OPEN_TAG)):
        if text.endswith(OPEN_TAG[:length]):
            longest = length
    return longest


def _parse_body(body: str) -> ParsedToolCall | None:
    text = body.strip()
    fenced = _FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name") or data.get("tool") or data.get("function")
    if not isinstance(name, str) or not name:
        return None
    arguments: Any = data.get("arguments", data.get("input", data.get("parameters", {})))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"input": arguments}
    if not isinstance(arguments, dict):
        arguments = {"value": arguments}
    call = ParsedToolCall(name=name, arguments=arguments, raw=body)
    if isinstance(data.get("id"), str) and data["id"]:
        call.id = data["id"]
    return call


def tool_instructions(tools: list[ToolDef], choice: ToolChoice) -> str:
    """System-prompt text describing the emulated tool protocol."""
    names = ", ".join(tool.name for tool in tools)
    lines = [
        "# Tool calling protocol",
        "",
        f"You have the following tools available: {names}.",
        "They are executed for you by the harness that sent this request; you call a tool by writing",
        "a block exactly like this (this IS how you read files, edit files, run commands, and so on):",
        "",
        f'{OPEN_TAG}{{"name": "<tool name>", "arguments": {{<JSON arguments matching the schema>}}}}{CLOSE_TAG}',
        "",
        "Rules:",
        "- Arguments must be a valid JSON object; use the exact parameter names from the schema.",
        "- You may include several blocks in one reply to call tools in parallel.",
        "- After emitting tool call blocks, stop and wait; the results come back in the next message.",
        "- Any text outside the blocks is shown to the user. Do not describe or repeat the blocks.",
        "- Never wrap the blocks in code fences and never invent tools that are not listed.",
        "- Never say you lack tools or cannot act: if a task needs a tool, call it with a block.",
        "",
        "## Available tools",
    ]
    for tool in tools:
        lines.append("")
        lines.append(f"### {tool.name}")
        if tool.description:
            lines.append(tool.description.strip())
        lines.append("Parameters (JSON Schema):")
        lines.append(json.dumps(tool.parameters, ensure_ascii=False, separators=(",", ":")))
    lines.append("")
    if choice.mode == "required":
        lines.append("You MUST call at least one tool in this reply.")
    elif choice.mode == "named" and choice.name:
        lines.append(f"You MUST call the tool `{choice.name}` in this reply.")
    return "\n".join(lines)


def tool_reminder(tools: list[ToolDef]) -> str:
    """Short reminder placed right before the model's reply in long prompts."""
    names = ", ".join(tool.name for tool in tools)
    return (
        f"(Reminder: your tools are {names}. Call one by writing "
        f'{OPEN_TAG}{{"name": "...", "arguments": {{...}}}}{CLOSE_TAG}; the harness runs it and replies.)'
    )


def render_tool_call(call: ToolCallPart) -> str:
    return f"{OPEN_TAG}{json.dumps({'name': call.name, 'arguments': call.arguments}, ensure_ascii=False)}{CLOSE_TAG}"


def strip_json_fences(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text.strip()
