"""OpenAI Chat Completions: ``POST /v1/chat/completions``."""

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
    custom_input,
    custom_tool,
    header_options,
    image_from_data_url,
    int_or_none,
    json_dumps,
    new_id,
    now,
    read_json,
    sse,
    sse_response,
    stop_list,
    stream_error_body,
    text_of_content,
)
from kiro_acp.gateway.turn import OutputDone, OutputText, OutputThought, OutputToolCall

LOG = logging.getLogger("kiro_acp.gateway.openai_chat")


def build_conversation(body: JSON) -> Conversation:
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise GatewayError("'messages' must be a non-empty array", code="invalid_messages")
    conversation = Conversation()
    system_parts: list[str] = []
    for raw in raw_messages:
        if not isinstance(raw, dict):
            raise GatewayError("Each message must be an object", code="invalid_messages")
        role = raw.get("role")
        content = raw.get("content")
        if role in ("system", "developer"):
            system_parts.append(text_of_content(content))
        elif role == "user":
            conversation.messages.append(Message("user", user_parts(content), name=raw.get("name")))
        elif role == "assistant":
            parts: list[Part] = []
            text = text_of_content(content)
            if text:
                parts.append(TextPart(text))
            for call in raw.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                if call.get("type") == "custom":
                    custom = call.get("custom") or {}
                    parts.append(
                        ToolCallPart(
                            id=str(call.get("id") or new_id("call_")),
                            name=str(custom.get("name", "")),
                            arguments={"input": str(custom.get("input") or "")},
                        )
                    )
                    continue
                function = call.get("function") or {}
                parts.append(
                    ToolCallPart(
                        id=str(call.get("id") or new_id("call_")),
                        name=str(function.get("name", "")),
                        arguments=parse_arguments(function.get("arguments")),
                    )
                )
            legacy = raw.get("function_call")
            if isinstance(legacy, dict) and legacy.get("name"):
                parts.append(
                    ToolCallPart(
                        id=new_id("call_"),
                        name=str(legacy["name"]),
                        arguments=parse_arguments(legacy.get("arguments")),
                    )
                )
            conversation.messages.append(Message("assistant", parts))
        elif role in ("tool", "function"):
            result = ToolResultPart(
                call_id=str(raw.get("tool_call_id") or raw.get("name") or ""),
                content=text_of_content(content),
                name=raw.get("name"),
            )
            if conversation.messages and conversation.messages[-1].role == "tool":
                conversation.messages[-1].parts.append(result)
            else:
                conversation.messages.append(Message("tool", [result]))
        else:
            raise GatewayError(f"Unsupported message role {role!r}", code="invalid_role")
    conversation.system = "\n\n".join(p for p in system_parts if p.strip())
    conversation.tools = parse_tools(body)
    conversation.tool_choice = parse_tool_choice(body.get("tool_choice", body.get("function_call")))
    conversation.json_output = parse_response_format(body.get("response_format"))
    effort = body.get("reasoning_effort")
    if effort is None and isinstance(body.get("reasoning"), dict):
        effort = body["reasoning"].get("effort")
    conversation.effort = effort
    return conversation


def user_parts(content: Any) -> list[Part]:
    if content is None:
        return [TextPart("")]
    if isinstance(content, str):
        return [TextPart(content)]
    parts: list[Part] = []
    for part in content:
        if isinstance(part, str):
            parts.append(TextPart(part))
            continue
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind in ("text", "input_text"):
            parts.append(TextPart(str(part.get("text", ""))))
        elif kind in ("image_url", "input_image"):
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str):
                raise GatewayError("image_url.url must be a string", code="invalid_image")
            parts.append(image_from_data_url(url))
        elif kind in ("input_audio", "audio", "file"):
            raise GatewayError(
                f"Content part type {kind!r} is not supported", code="unsupported_content"
            )
        else:
            raise GatewayError(f"Unknown content part type {kind!r}", code="unsupported_content")
    return parts or [TextPart("")]


def parse_arguments(value: Any) -> JSON:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"__raw__": str(value)}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def parse_tools(body: JSON) -> list[ToolDef]:
    tools: list[ToolDef] = []
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "custom":
            custom = raw.get("custom") or raw
            if isinstance(custom, dict) and custom.get("name"):
                tools.append(custom_tool(custom))
            continue
        if raw.get("type", "function") != "function":
            continue
        function = raw.get("function") or raw
        name = function.get("name")
        if not name:
            continue
        tools.append(
            ToolDef(
                name=str(name),
                description=str(function.get("description") or ""),
                parameters=function.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    for raw in body.get("functions") or []:
        if isinstance(raw, dict) and raw.get("name"):
            tools.append(
                ToolDef(
                    name=str(raw["name"]),
                    description=str(raw.get("description") or ""),
                    parameters=raw.get("parameters") or {"type": "object", "properties": {}},
                )
            )
    return tools


def parse_tool_choice(value: Any) -> ToolChoice:
    if value is None or value == "auto":
        return ToolChoice("auto")
    if value == "none":
        return ToolChoice("none")
    if value in ("required", "any"):
        return ToolChoice("required")
    if isinstance(value, dict):
        function = value.get("function") or value
        name = function.get("name")
        if name:
            return ToolChoice("named", str(name))
    raise GatewayError("Invalid tool_choice", code="invalid_tool_choice")


def parse_response_format(value: Any) -> JsonOutput | None:
    if not isinstance(value, dict):
        return None
    kind = value.get("type")
    if kind == "json_object":
        return JsonOutput()
    if kind == "json_schema":
        schema = value.get("json_schema") or {}
        return JsonOutput(schema=schema.get("schema"), name=schema.get("name"))
    return None


def finish_reason(finish: str) -> str:
    return {
        "stop": "stop",
        "length": "length",
        "tool_calls": "tool_calls",
        "refusal": "content_filter",
        "cancelled": "stop",
        "error": "stop",
    }.get(finish, "stop")


def tool_call_json(
    call: ToolCallPart, index: int | None = None, custom: set[str] | None = None
) -> JSON:
    data: JSON
    if custom and call.name in custom:
        data = {
            "id": call.id,
            "type": "custom",
            "custom": {"name": call.name, "input": custom_input(call)},
        }
    else:
        data = {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": json_dumps(call.arguments)},
        }
    if index is not None:
        data["index"] = index
    return data


def usage_json(usage: JSON) -> JSON:
    return {
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


def make_router(backend_dep, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    @router.post("/chat/completions")
    async def chat_completions(request: Request, backend: KiroBackend = Depends(backend_dep)):
        body = await read_json(request)
        if body.get("n") not in (None, 1):
            raise GatewayError("Only n=1 is supported", code="unsupported_parameter")
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
                stop_sequences=stop_list(body.get("stop")),
                max_tokens=int_or_none(body.get("max_completion_tokens") or body.get("max_tokens")),
            ),
            body=body,
        )
        completion_id = new_id("chatcmpl-")
        created = now()
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        expose_thoughts = backend.settings.expose_thoughts

        if body.get("stream"):
            return sse_response(
                stream_chat(
                    backend,
                    conversation,
                    opts,
                    completion_id,
                    created,
                    model_name or model or "kiro",
                    include_usage,
                    expose_thoughts,
                ),
                keepalive=backend.settings.sse_keepalive,
            )

        text = ""
        thoughts = ""
        calls: list[ToolCallPart] = []
        custom = {t.name for t in conversation.tools if t.kind == "custom"}
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
        message: JSON = {
            "role": "assistant",
            "content": text if text or not calls else None,
            "refusal": None,
        }
        if thoughts and expose_thoughts:
            message["reasoning_content"] = thoughts
        if calls:
            message["tool_calls"] = [tool_call_json(c, custom=custom) for c in calls]
        response: JSON = {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": model_name or model or "kiro",
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "logprobs": None,
                    "finish_reason": finish_reason(done.finish),
                }
            ],
            "usage": usage_json(done.usage),
            "kiro": done.kiro,
        }
        return JSONResponse(response)

    return router


async def stream_chat(
    backend: KiroBackend,
    conversation: Conversation,
    opts: TurnOptions,
    completion_id: str,
    created: int,
    model: str,
    include_usage: bool,
    expose_thoughts: bool,
) -> AsyncIterator[str]:
    custom = {t.name for t in conversation.tools if t.kind == "custom"}

    def chunk(delta: JSON, finish: str | None = None, usage: JSON | None = None) -> str:
        payload: JSON = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }
        if usage is not None:
            payload["usage"] = usage
        return sse(payload)

    yield chunk({"role": "assistant", "content": ""})
    call_index = 0
    try:
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                match event:
                    case OutputText(text=text):
                        if text:
                            yield chunk({"content": text})
                    case OutputThought(text=text):
                        if text and expose_thoughts:
                            yield chunk({"reasoning_content": text})
                    case OutputToolCall(call=call):
                        yield chunk({"tool_calls": [tool_call_json(call, call_index, custom)]})
                        call_index += 1
                    case OutputDone(finish=finish, error=error, usage=usage, kiro=kiro):
                        if finish == "error":
                            yield sse(stream_error_body(error or "Kiro turn failed"))
                        else:
                            final: JSON = {}
                            yield chunk(final, finish_reason(finish))
                        if include_usage:
                            payload = {
                                "id": completion_id,
                                "object": "chat.completion.chunk",
                                "created": created,
                                "model": model,
                                "choices": [],
                                "usage": usage_json(usage),
                                "kiro": kiro,
                            }
                            yield sse(payload)
    except GatewayError as error:
        yield sse(
            {"error": {"message": error.message, "type": error.error_type, "code": error.code}}
        )
    except Exception as error:  # pragma: no cover - defensive
        LOG.exception("streaming chat failed")
        yield sse({"error": {"message": str(error), "type": "api_error", "code": "internal_error"}})
    yield "data: [DONE]\n\n"
