"""Render a :class:`Conversation` into ACP prompt content blocks."""

from __future__ import annotations

import json

from kiro_acp.acp.session import image_block, text_block
from kiro_acp.gateway.conversation import (
    JSON,
    Conversation,
    ImagePart,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from kiro_acp.gateway.toolcalls import render_tool_call, tool_instructions, tool_reminder

HARNESS_PREAMBLE = (
    "You are serving as the language model behind an API. Reply as the assistant to the conversation "
    "below. Output only the assistant's next reply: no role labels, no commentary about this transcript."
)

TRANSCRIPT_NOTE = (
    "The conversation so far is reproduced here as a transcript because this session has no memory "
    "of it. Treat it as the real conversation history."
)


def build_system_text(conversation: Conversation, *, emulate_tools: bool) -> str:
    sections: list[str] = [HARNESS_PREAMBLE]
    if conversation.system.strip():
        sections.append("# Operator instructions\n\n" + conversation.system.strip())
    if emulate_tools and conversation.tools and conversation.tool_choice.mode != "none":
        sections.append(tool_instructions(conversation.tools, conversation.tool_choice))
    if conversation.json_output is not None:
        note = "Respond with a single JSON value and nothing else (no prose, no code fences)."
        if conversation.json_output.schema:
            note += "\nThe JSON must conform to this JSON Schema:\n" + json.dumps(
                conversation.json_output.schema, ensure_ascii=False
            )
        sections.append("# Output format\n\n" + note)
    return "\n\n".join(sections)


def render_message(message: Message) -> str:
    """Render one message as transcript text (images are attached separately)."""
    if message.role == "assistant":
        chunks: list[str] = []
        text = message.text().strip()
        if text:
            chunks.append(text)
        for call in message.tool_calls:
            chunks.append(render_tool_call(call))
        return "[Assistant]\n" + ("\n".join(chunks) if chunks else "(empty)")
    if message.role == "tool" or message.tool_results:
        blocks = []
        for result in message.tool_results:
            label = f"[Tool result for {result.name or 'tool'} call {result.call_id}"
            label += " (error)]" if result.is_error else "]"
            blocks.append(label + "\n" + (result.content.strip() or "(no output)"))
        text = message.text().strip()
        if text:
            blocks.append("[User]\n" + text)
        return "\n\n".join(blocks)
    label = "[User]" if not message.name else f"[User ({message.name})]"
    body = message.text().strip()
    if message.images and not body:
        body = "(see attached image)"
    return f"{label}\n{body}"


def render_prompt(
    conversation: Conversation,
    *,
    start: int,
    include_system: bool,
    emulate_tools: bool,
) -> list[JSON]:
    """Build ``session/prompt`` blocks for messages ``conversation.messages[start:]``.

    ``include_system`` is true for a fresh session (the system text and, when
    ``start > 0``, a transcript of the earlier messages are prepended).
    """
    blocks: list[JSON] = []
    sections: list[str] = []
    if include_system:
        sections.append(build_system_text(conversation, emulate_tools=emulate_tools))
    messages = conversation.messages
    if include_system and start > 0:
        # Fresh session but the client already has history: replay it as a transcript.
        start = 0
    if start == 0 and len(messages) > 1 and include_system:
        sections.append("# Conversation\n\n" + TRANSCRIPT_NOTE)
    for message in messages[start:]:
        for image in message.images:
            blocks.append(image_block(image.data_base64, image.mime_type))
        sections.append(render_message(message))
    if emulate_tools and conversation.tools and conversation.tool_choice.mode != "none":
        sections.append(tool_reminder(conversation.tools))
    text = "\n\n".join(section for section in sections if section)
    if text:
        blocks.append(text_block(text))
    if not blocks:
        blocks.append(text_block("[User]\n(empty message)"))
    return blocks


def assistant_message(text: str, calls: list[ToolCallPart]) -> Message:
    parts: list[TextPart | ToolCallPart] = []
    if text:
        parts.append(TextPart(text))
    parts.extend(calls)
    return Message(role="assistant", parts=list(parts))


__all__ = [
    "ImagePart",
    "ToolResultPart",
    "assistant_message",
    "build_system_text",
    "render_message",
    "render_prompt",
]
