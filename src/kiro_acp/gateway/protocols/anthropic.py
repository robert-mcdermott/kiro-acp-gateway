"""Anthropic Messages API: ``POST /v1/messages``, ``/v1/messages/count_tokens``."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kiro_acp.gateway.backend import GatewayError, KiroBackend, TurnOptions
from kiro_acp.gateway.conversation import (
    JSON,
    Conversation,
    ImagePart,
    JsonOutput,
    Message,
    Part,
    TextPart,
    ToolCallPart,
    ToolChoice,
    ToolDef,
    ToolResultPart,
)
from kiro_acp.gateway.protocols.common import (
    header_options,
    image_from_data_url,
    int_or_none,
    new_id,
    read_json,
    sse,
    sse_response,
    stop_list,
)
from kiro_acp.gateway.turn import (
    OutputDone,
    OutputText,
    OutputThought,
    OutputToolCall,
    estimate_tokens,
)

LOG = logging.getLogger("kiro_acp.gateway.anthropic")

# Schemas for Anthropic-defined (schema-less) client tools so they can be emulated.
BUILTIN_TOOL_SCHEMAS: dict[str, tuple[str, JSON]] = {
    "bash": (
        "Run a bash command on the user's machine and return its output.",
        {
            "type": "object",
            "properties": {"command": {"type": "string"}, "restart": {"type": "boolean"}},
            "required": ["command"],
        },
    ),
    "text_editor": (
        "View, create, and edit files. Commands: view, create, str_replace, insert, undo_edit.",
        {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["view", "create", "str_replace", "insert", "undo_edit"],
                },
                "path": {"type": "string"},
                "file_text": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "insert_line": {"type": "integer"},
                "view_range": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["command", "path"],
        },
    ),
}


def system_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n\n".join(
            str(block.get("text", ""))
            for block in value
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(value)


def image_part(block: JSON) -> ImagePart:
    source = block.get("source") or {}
    if source.get("type") == "base64":
        return ImagePart(
            mime_type=str(source.get("media_type") or "image/png"),
            data_base64=str(source.get("data", "")),
        )
    if source.get("type") == "url":
        return image_from_data_url(str(source.get("url", "")))
    raise GatewayError("Unsupported image source", code="invalid_image")


def tool_result_text(content: Any) -> tuple[str, list[ImagePart]]:
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    images: list[ImagePart] = []
    for block in content:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict):
            if block.get("type") == "text":
                texts.append(str(block.get("text", "")))
            elif block.get("type") == "image":
                images.append(image_part(block))
    return "\n".join(texts), images


def build_conversation(body: JSON) -> Conversation:
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise GatewayError("'messages' must be a non-empty array", code="invalid_messages")
    conversation = Conversation(system=system_text(body.get("system")))
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise GatewayError("Each message must be an object", code="invalid_messages")
        role = raw.get("role")
        content = raw.get("content")
        if role == "user":
            parse_user(conversation, content)
        elif role == "assistant":
            parts: list[Part] = []
            if isinstance(content, str):
                parts.append(TextPart(content))
            else:
                for block in content or []:
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "text":
                        parts.append(TextPart(str(block.get("text", ""))))
                    elif kind == "tool_use":
                        args = block.get("input")
                        parts.append(
                            ToolCallPart(
                                id=str(block.get("id") or new_id("toolu_")),
                                name=str(block.get("name", "")),
                                arguments=args if isinstance(args, dict) else {"value": args},
                            )
                        )
                    # thinking / redacted_thinking / server tool blocks are dropped.
            conversation.messages.append(Message("assistant", parts))
        elif role == "system":
            conversation.system = (conversation.system + "\n\n" + system_text(content)).strip()
        else:
            raise GatewayError(f"Unsupported message role {role!r}", code="invalid_role")
    conversation.tools = parse_tools(body.get("tools") or [])
    conversation.tool_choice = parse_tool_choice(body.get("tool_choice"))
    output_config = body.get("output_config") or {}
    if isinstance(output_config, dict):
        if output_config.get("effort"):
            conversation.effort = str(output_config["effort"])
        fmt = output_config.get("format")
        if isinstance(fmt, dict) and fmt.get("type") == "json_schema":
            conversation.json_output = JsonOutput(schema=fmt.get("schema"))
    return conversation


def parse_user(conversation: Conversation, content: Any) -> None:
    if isinstance(content, str) or content is None:
        conversation.messages.append(Message("user", [TextPart(content or "")]))
        return
    results: list[ToolResultPart] = []
    parts: list[Part] = []
    for block in content:
        if isinstance(block, str):
            parts.append(TextPart(block))
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(TextPart(str(block.get("text", ""))))
        elif kind == "image":
            parts.append(image_part(block))
        elif kind == "tool_result":
            text, images = tool_result_text(block.get("content"))
            results.append(
                ToolResultPart(
                    call_id=str(block.get("tool_use_id", "")),
                    content=text,
                    is_error=bool(block.get("is_error")),
                )
            )
            parts.extend(images)
        elif kind == "document":
            source = block.get("source") or {}
            if source.get("type") == "text":
                parts.append(TextPart(f"[document]\n{source.get('data', '')}"))
            else:
                raise GatewayError(
                    "Only plain-text documents are supported", code="unsupported_content"
                )
        else:
            continue
    if results:
        conversation.messages.append(
            Message(
                "tool", [*results, *[p for p in parts if not isinstance(p, TextPart) or p.text]]
            )
        )
    else:
        conversation.messages.append(Message("user", parts or [TextPart("")]))


def parse_tools(raw_tools: list[Any]) -> list[ToolDef]:
    tools: list[ToolDef] = []
    for raw in raw_tools:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        kind = str(raw.get("type") or "custom")
        if kind in ("custom",) or "input_schema" in raw:
            if name:
                tools.append(
                    ToolDef(
                        name=str(name),
                        description=str(raw.get("description") or ""),
                        parameters=raw.get("input_schema") or {"type": "object", "properties": {}},
                    )
                )
            continue
        family = kind.split("_20")[0]
        if family in BUILTIN_TOOL_SCHEMAS and name:
            description, schema = BUILTIN_TOOL_SCHEMAS[family]
            tools.append(ToolDef(name=str(name), description=description, parameters=schema))
        else:
            LOG.info("Ignoring unsupported Anthropic tool type %s", kind)
    return tools


def parse_tool_choice(value: Any) -> ToolChoice:
    if not isinstance(value, dict):
        return ToolChoice("auto")
    kind = value.get("type")
    if kind == "any":
        return ToolChoice("required")
    if kind == "none":
        return ToolChoice("none")
    if kind == "tool" and value.get("name"):
        return ToolChoice("named", str(value["name"]))
    return ToolChoice("auto")


def stop_reason(finish: str) -> str:
    return {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "refusal": "refusal",
        "cancelled": "end_turn",
    }.get(finish, "end_turn")


def usage_json(usage: JSON) -> JSON:
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }


def thinking_requested(body: JSON) -> bool:
    thinking = body.get("thinking")
    if not isinstance(thinking, dict):
        return False
    return thinking.get("type") not in (None, "disabled")


def make_router(backend_dep, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    @router.post("/messages")
    async def messages(request: Request, backend: KiroBackend = Depends(backend_dep)):
        body = await read_json(request)
        conversation = build_conversation(body)
        model_name = str(body.get("model") or "")
        model = await backend.resolve_model(model_name)
        emulate = bool(conversation.tools) and conversation.tool_choice.mode != "none"
        if conversation.tools and backend.settings.tool_mode == "reject":
            raise GatewayError(
                "Client-defined tools are disabled on this gateway (KIRO_GATEWAY_TOOL_MODE=reject)",
                code="tools_disabled",
            )
        if backend.settings.tool_mode == "ignore":
            emulate = False
        opts = header_options(
            request,
            TurnOptions(
                model=model,
                effort=conversation.effort,
                emulate_tools=emulate,
                allow_retry=not body.get("stream"),
                stop_sequences=stop_list(body.get("stop_sequences")),
                max_tokens=int_or_none(body.get("max_tokens")),
            ),
        )
        message_id = new_id("msg_")
        show_thoughts = backend.settings.expose_thoughts and thinking_requested(body)
        display_model = model_name or model or "kiro"

        if body.get("stream"):
            return sse_response(
                stream_messages(
                    backend, conversation, opts, message_id, display_model, show_thoughts
                ),
                keepalive=backend.settings.sse_keepalive,
                ping=sse({"type": "ping"}, "ping"),
            )

        text = ""
        thoughts = ""
        calls: list[ToolCallPart] = []
        done: OutputDone | None = None
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                match event:
                    case OutputText(text=chunk):
                        text += chunk
                    case OutputThought(text=chunk):
                        thoughts += chunk
                    case OutputToolCall(call=call):
                        calls.append(call)
                    case OutputDone():
                        done = event
        assert done is not None
        if done.finish == "error":
            raise GatewayError.from_kiro(done.error or "Kiro turn failed")
        text, thoughts, calls = done.text, done.thoughts, done.tool_calls
        content: list[JSON] = []
        if thoughts and show_thoughts:
            content.append({"type": "thinking", "thinking": thoughts, "signature": ""})
        if text or not calls:
            content.append({"type": "text", "text": text})
        for call in calls:
            content.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        return JSONResponse(
            {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": display_model,
                "content": content,
                "stop_reason": "stop_sequence" if done.stop_sequence else stop_reason(done.finish),
                "stop_sequence": done.stop_sequence,
                "usage": usage_json(done.usage),
                "kiro": done.kiro,
            }
        )

    @router.post("/messages/count_tokens")
    async def count_tokens(request: Request, backend: KiroBackend = Depends(backend_dep)):
        body = await read_json(request)
        conversation = build_conversation(body)
        total = estimate_tokens(conversation.system)
        for message in conversation.messages:
            total += estimate_tokens(message.text())
            total += sum(estimate_tokens(str(c.arguments)) for c in message.tool_calls)
            total += sum(estimate_tokens(r.content) for r in message.tool_results)
        for tool in conversation.tools:
            total += estimate_tokens(tool.description) + estimate_tokens(str(tool.parameters))
        return JSONResponse({"input_tokens": total})

    return router


async def stream_messages(
    backend: KiroBackend,
    conversation: Conversation,
    opts: TurnOptions,
    message_id: str,
    model: str,
    show_thoughts: bool,
) -> AsyncIterator[str]:
    index = -1
    open_block: str | None = None
    text_total = ""
    thoughts_total = ""

    def start_block(block: JSON) -> str:
        nonlocal index, open_block
        index += 1
        open_block = block["type"]
        return sse(
            {"type": "content_block_start", "index": index, "content_block": block},
            "content_block_start",
        )

    def stop_block() -> str:
        nonlocal open_block
        open_block = None
        return sse({"type": "content_block_stop", "index": index}, "content_block_stop")

    yield sse(
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        "message_start",
    )
    yield sse({"type": "ping"}, "ping")
    try:
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                match event:
                    case OutputThought(text=chunk):
                        if not chunk or not show_thoughts:
                            continue
                        if open_block != "thinking":
                            if open_block is not None:
                                yield stop_block()
                            yield start_block({"type": "thinking", "thinking": "", "signature": ""})
                        thoughts_total += chunk
                        yield sse(
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "thinking_delta", "thinking": chunk},
                            },
                            "content_block_delta",
                        )
                    case OutputText(text=chunk):
                        if not chunk:
                            continue
                        if open_block != "text":
                            if open_block is not None:
                                yield stop_block()
                            yield start_block({"type": "text", "text": ""})
                        text_total += chunk
                        yield sse(
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {"type": "text_delta", "text": chunk},
                            },
                            "content_block_delta",
                        )
                    case OutputToolCall(call=call):
                        if open_block is not None:
                            yield stop_block()
                        yield start_block(
                            {"type": "tool_use", "id": call.id, "name": call.name, "input": {}}
                        )
                        yield sse(
                            {
                                "type": "content_block_delta",
                                "index": index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            },
                            "content_block_delta",
                        )
                        yield stop_block()
                    case OutputDone(finish=finish, error=error, usage=usage):
                        if open_block is not None:
                            yield stop_block()
                        if finish == "error":
                            yield sse(
                                {
                                    "type": "error",
                                    "error": {
                                        "type": "api_error",
                                        "message": error or "Kiro turn failed",
                                    },
                                },
                                "error",
                            )
                            return
                        if index < 0:
                            yield start_block({"type": "text", "text": ""})
                            yield stop_block()
                        yield sse(
                            {
                                "type": "message_delta",
                                "delta": {
                                    "stop_reason": "stop_sequence"
                                    if event.stop_sequence
                                    else stop_reason(finish),
                                    "stop_sequence": event.stop_sequence,
                                },
                                "usage": {
                                    "output_tokens": usage.get("completion_tokens", 0),
                                    "input_tokens": usage.get("prompt_tokens", 0),
                                },
                            },
                            "message_delta",
                        )
                        yield sse({"type": "message_stop"}, "message_stop")
    except GatewayError as gw_error:
        yield sse(
            {"type": "error", "error": {"type": gw_error.error_type, "message": gw_error.message}},
            "error",
        )
