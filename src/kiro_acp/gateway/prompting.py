"""Render a :class:`Conversation` into ACP prompt content blocks."""

from __future__ import annotations

import json
import logging

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
from kiro_acp.gateway.sanitizer import sanitize_system
from kiro_acp.gateway.toolcalls import render_tool_call, tool_instructions, tool_reminder

LOG = logging.getLogger("kiro_acp.gateway.prompting")

HARNESS_PREAMBLE = (
    "This is an API request relayed by kiro-gateway from an external coding tool. Answer the "
    "conversation below on the tool's behalf. Output only your reply: no role labels, no tags, "
    "no commentary about this request's structure."
)

TRANSCRIPT_NOTE = (
    "The conversation so far is reproduced here because this session has no memory of it. Treat it "
    "as the real conversation history."
)


def build_system_text(
    conversation: Conversation, *, emulate_tools: bool, sanitize: bool = False
) -> str:
    sections: list[str] = [HARNESS_PREAMBLE]
    system = conversation.system.strip()
    if system and sanitize:
        system, removed = sanitize_system(system)
        if removed:
            LOG.info("Sanitized client system prompt: removed %d line(s)", removed)
    if system.strip():
        sections.append("<operator_instructions>\n" + system.strip() + "\n</operator_instructions>")
    if emulate_tools and conversation.tools and conversation.tool_choice.mode != "none":
        sections.append(
            "<tools>\n"
            + tool_instructions(conversation.tools, conversation.tool_choice)
            + "\n</tools>"
        )
    if conversation.json_output is not None:
        note = "Respond with a single JSON value and nothing else (no prose, no code fences)."
        if conversation.json_output.schema:
            note += "\nThe JSON must conform to this JSON Schema:\n" + json.dumps(
                conversation.json_output.schema, ensure_ascii=False
            )
        sections.append("<output_format>\n" + note + "\n</output_format>")
    return "\n\n".join(sections)


def render_message(message: Message) -> str:
    """Render one message as a tagged transcript entry (images are attached separately)."""
    if message.role == "assistant":
        chunks: list[str] = []
        text = message.text().strip()
        if text:
            chunks.append(text)
        for call in message.tool_calls:
            chunks.append(render_tool_call(call))
        body = "\n".join(chunks) if chunks else "(empty)"
        return f'<message role="assistant">\n{body}\n</message>'
    if message.role == "tool" or message.tool_results:
        blocks = []
        for result in message.tool_results:
            attrs = f' call_id="{result.call_id}"'
            if result.name:
                attrs += f' name="{result.name}"'
            if result.is_error:
                attrs += ' error="true"'
            blocks.append(
                f"<tool_result{attrs}>\n{result.content.strip() or '(no output)'}\n</tool_result>"
            )
        text = message.text().strip()
        if text:
            blocks.append(f'<message role="user">\n{text}\n</message>')
        return "\n".join(blocks)
    attrs = f' name="{message.name}"' if message.name else ""
    body = message.text().strip()
    if message.images and not body:
        body = "(see attached image)"
    return f'<message role="user"{attrs}>\n{body}\n</message>'


def render_prompt(
    conversation: Conversation,
    *,
    start: int,
    include_system: bool,
    emulate_tools: bool,
    sanitize: bool = False,
) -> list[JSON]:
    """Build ``session/prompt`` blocks for messages ``conversation.messages[start:]``.

    ``include_system`` is true for a fresh session (the system text and, when
    ``start > 0``, a transcript of the earlier messages are prepended).
    """
    blocks: list[JSON] = []
    sections: list[str] = []
    if include_system:
        sections.append(
            build_system_text(conversation, emulate_tools=emulate_tools, sanitize=sanitize)
        )
    messages = conversation.messages
    if include_system and start > 0:
        # Fresh session but the client already has history: replay it as a transcript.
        start = 0
    rendered: list[str] = []
    for message in messages[start:]:
        for image in message.images:
            blocks.append(image_block(image.data_base64, image.mime_type))
        rendered.append(render_message(message))
    note = (TRANSCRIPT_NOTE + "\n") if (start == 0 and len(messages) > 1 and include_system) else ""
    sections.append("<conversation>\n" + note + "\n".join(rendered) + "\n</conversation>")
    if emulate_tools and conversation.tools and conversation.tool_choice.mode != "none":
        sections.append(tool_reminder(conversation.tools))
    text = "\n\n".join(section for section in sections if section)
    if text:
        blocks.append(text_block(text))
    if not blocks:
        blocks.append(
            text_block(
                '<conversation>\n<message role="user">\n(empty message)\n</message>\n</conversation>'
            )
        )
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
