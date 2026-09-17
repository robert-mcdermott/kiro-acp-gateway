"""OpenAI Responses API: ``POST /v1/responses`` (+ ``GET/DELETE /v1/responses/{id}``)."""

from __future__ import annotations

import copy
import logging
from collections import OrderedDict
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
    text_of_content,
)
from kiro_acp.gateway.protocols.openai_chat import parse_arguments
from kiro_acp.gateway.turn import OutputDone, OutputText, OutputThought, OutputToolCall

LOG = logging.getLogger("kiro_acp.gateway.openai_responses")


class ResponseStore:
    """In-memory store so ``previous_response_id`` can continue a conversation."""

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._items: OrderedDict[str, tuple[Conversation, JSON]] = OrderedDict()

    def put(self, response_id: str, conversation: Conversation, response: JSON) -> None:
        self._items[response_id] = (copy.deepcopy(conversation), response)
        self._items.move_to_end(response_id)
        while len(self._items) > self.capacity:
            self._items.popitem(last=False)

    def get(self, response_id: str) -> tuple[Conversation, JSON] | None:
        item = self._items.get(response_id)
        if item is not None:
            self._items.move_to_end(response_id)
        return item

    def delete(self, response_id: str) -> bool:
        return self._items.pop(response_id, None) is not None


STORE = ResponseStore()


def parse_input(conversation: Conversation, value: Any) -> list[ToolDef]:
    """Fold ``input`` into the conversation; returns tools declared inline (``additional_tools``)."""
    extra_tools: list[ToolDef] = []
    if value is None:
        return extra_tools
    if isinstance(value, str):
        conversation.messages.append(Message("user", [TextPart(value)]))
        return extra_tools
    if not isinstance(value, list):
        raise GatewayError("'input' must be a string or an array of items", code="invalid_input")
    for item in value:
        if isinstance(item, str):
            conversation.messages.append(Message("user", [TextPart(item)]))
            continue
        if not isinstance(item, dict):
            raise GatewayError("Input items must be objects", code="invalid_input")
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            role = item.get("role")
            content = item.get("content")
            if role in ("system", "developer"):
                text = text_of_content(content)
                conversation.system = (
                    (conversation.system + "\n\n" + text).strip() if conversation.system else text
                )
            elif role == "user":
                conversation.messages.append(Message("user", message_parts(content)))
            elif role == "assistant":
                text = text_of_content(content)
                conversation.messages.append(Message("assistant", [TextPart(text)] if text else []))
            else:
                raise GatewayError(f"Unsupported message role {role!r}", code="invalid_role")
        elif kind == "function_call":
            part = ToolCallPart(
                id=str(item.get("call_id") or item.get("id") or new_id("call_")),
                name=str(item.get("name", "")),
                arguments=parse_arguments(item.get("arguments")),
            )
            if conversation.messages and conversation.messages[-1].role == "assistant":
                conversation.messages[-1].parts.append(part)
            else:
                conversation.messages.append(Message("assistant", [part]))
        elif kind == "custom_tool_call":
            part = ToolCallPart(
                id=str(item.get("call_id") or item.get("id") or new_id("call_")),
                name=str(item.get("name", "")),
                arguments={"input": str(item.get("input") or "")},
            )
            if conversation.messages and conversation.messages[-1].role == "assistant":
                conversation.messages[-1].parts.append(part)
            else:
                conversation.messages.append(Message("assistant", [part]))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output")
            result = ToolResultPart(
                call_id=str(item.get("call_id", "")),
                content=text_of_content(output) if not isinstance(output, str) else output,
            )
            if conversation.messages and conversation.messages[-1].role == "tool":
                conversation.messages[-1].parts.append(result)
            else:
                conversation.messages.append(Message("tool", [result]))
        elif kind == "additional_tools":
            # Codex TUI adds plugin/skill tools mid-conversation as an input item.
            extra_tools.extend(parse_tools({"tools": item.get("tools") or []}))
        elif kind in (
            "reasoning",
            "item_reference",
            "web_search_call",
            "file_search_call",
            "computer_call",
        ):
            continue
        else:
            LOG.warning("Ignoring unsupported Responses input item type %r", kind)
    return extra_tools


def message_parts(content: Any) -> list[Part]:
    if content is None:
        return [TextPart("")]
    if isinstance(content, str):
        return [TextPart(content)]
    parts: list[Part] = []
    for part in content:
        if isinstance(part, str):
            parts.append(TextPart(part))
        elif isinstance(part, dict):
            kind = part.get("type")
            if kind in ("input_text", "text", "output_text"):
                parts.append(TextPart(str(part.get("text", ""))))
            elif kind == "input_image":
                url = part.get("image_url")
                if isinstance(url, dict):
                    url = url.get("url")
                if not isinstance(url, str):
                    raise GatewayError(
                        "input_image requires image_url as a data: URL", code="invalid_image"
                    )
                parts.append(image_from_data_url(url))
            elif kind == "input_file":
                raise GatewayError("input_file is not supported", code="unsupported_content")
    return parts or [TextPart("")]


def parse_tools(body: JSON) -> list[ToolDef]:
    tools: list[ToolDef] = []
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        if raw.get("type") == "namespace" and isinstance(raw.get("tools"), list):
            # Codex groups related functions under a namespace; expose them by their own names.
            tools.extend(parse_tools({"tools": raw["tools"]}))
            continue
        if raw.get("type") == "custom" and raw.get("name"):
            tools.append(custom_tool(raw))
            continue
        if raw.get("type") != "function":
            continue
        name = raw.get("name") or (raw.get("function") or {}).get("name")
        if not name:
            continue
        tools.append(
            ToolDef(
                name=str(name),
                description=str(raw.get("description") or ""),
                parameters=raw.get("parameters") or {"type": "object", "properties": {}},
            )
        )
    return tools


def parse_tool_choice(value: Any) -> ToolChoice:
    if value in (None, "auto"):
        return ToolChoice("auto")
    if value == "none":
        return ToolChoice("none")
    if value == "required":
        return ToolChoice("required")
    if isinstance(value, dict) and value.get("type") == "function" and value.get("name"):
        return ToolChoice("named", str(value["name"]))
    if isinstance(value, dict) and value.get("type") in ("allowed_tools",):
        return ToolChoice("auto")
    raise GatewayError("Invalid tool_choice", code="invalid_tool_choice")


def parse_text_format(body: JSON) -> JsonOutput | None:
    text = body.get("text")
    fmt = text.get("format") if isinstance(text, dict) else None
    if not isinstance(fmt, dict):
        return None
    if fmt.get("type") == "json_schema":
        return JsonOutput(schema=fmt.get("schema"), name=fmt.get("name"))
    if fmt.get("type") == "json_object":
        return JsonOutput()
    return None


def build_conversation(body: JSON) -> Conversation:
    conversation = Conversation()
    previous = body.get("previous_response_id")
    if previous:
        stored = STORE.get(str(previous))
        if stored is None:
            raise GatewayError(
                f"Previous response {previous!r} not found",
                status=404,
                error_type="not_found_error",
                code="response_not_found",
            )
        conversation = copy.deepcopy(stored[0])
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        conversation.system = instructions
    extra_tools = parse_input(conversation, body.get("input"))
    if not conversation.messages:
        raise GatewayError("'input' is required", code="invalid_input")
    conversation.tools = parse_tools(body)
    known = {t.name for t in conversation.tools}
    conversation.tools.extend(t for t in extra_tools if t.name not in known)
    conversation.tool_choice = parse_tool_choice(body.get("tool_choice"))
    conversation.json_output = parse_text_format(body)
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        conversation.effort = str(reasoning["effort"])
    return conversation


def usage_json(usage: JSON, reasoning_tokens: int = 0) -> JSON:
    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": usage.get("total_tokens", 0),
    }


def base_response(response_id: str, created: int, model: str, body: JSON) -> JSON:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": "in_progress",
        "error": None,
        "incomplete_details": None,
        "instructions": body.get("instructions"),
        "max_output_tokens": body.get("max_output_tokens"),
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "previous_response_id": body.get("previous_response_id"),
        "reasoning": body.get("reasoning") or {"effort": None, "summary": None},
        "store": body.get("store", True),
        "temperature": body.get("temperature", 1.0),
        "text": body.get("text") or {"format": {"type": "text"}},
        "tool_choice": body.get("tool_choice", "auto"),
        "tools": body.get("tools") or [],
        "top_p": body.get("top_p", 1.0),
        "truncation": body.get("truncation", "disabled"),
        "usage": None,
        "user": body.get("user"),
        "metadata": body.get("metadata") or {},
    }


def message_item(item_id: str, text: str, status: str = "completed") -> JSON:
    return {
        "id": item_id,
        "type": "message",
        "status": status,
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def function_item(item_id: str, call: ToolCallPart, status: str = "completed") -> JSON:
    return {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": call.id,
        "name": call.name,
        "arguments": json_dumps(call.arguments),
    }


def custom_item(item_id: str, call: ToolCallPart, status: str = "completed") -> JSON:
    return {
        "id": item_id,
        "type": "custom_tool_call",
        "status": status,
        "call_id": call.id,
        "name": call.name,
        "input": custom_input(call),
    }


def call_item(
    item_id: str, call: ToolCallPart, custom: set[str], status: str = "completed"
) -> JSON:
    if call.name in custom:
        return custom_item(item_id, call, status)
    return function_item(item_id, call, status)


def custom_tool_names(conversation: Conversation) -> set[str]:
    return {t.name for t in conversation.tools if t.kind == "custom"}


def reasoning_item(item_id: str, text: str) -> JSON:
    return {
        "id": item_id,
        "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text}] if text else [],
    }


def make_router(backend_dep, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    @router.post("/responses")
    async def create_response(request: Request, backend: KiroBackend = Depends(backend_dep)):
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
                max_tokens=int_or_none(body.get("max_output_tokens")),
            ),
        )
        response_id = new_id("resp_")
        created = now()
        response = base_response(response_id, created, model_name or model or "kiro", body)
        store = body.get("store", True) is not False

        if body.get("stream"):
            return sse_response(
                stream_response(backend, conversation, opts, response, store),
                keepalive=backend.settings.sse_keepalive,
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
        finalize(
            response,
            text,
            thoughts,
            calls,
            done,
            backend.settings.expose_thoughts,
            custom_tool_names(conversation),
        )
        if store:
            STORE.put(response_id, continued(conversation, text, calls), response)
        return JSONResponse(response)

    @router.get("/responses/{response_id}")
    async def get_response(response_id: str):
        stored = STORE.get(response_id)
        if stored is None:
            raise GatewayError(
                "Response not found",
                status=404,
                error_type="not_found_error",
                code="response_not_found",
            )
        return JSONResponse(stored[1])

    @router.delete("/responses/{response_id}")
    async def delete_response(response_id: str):
        if not STORE.delete(response_id):
            raise GatewayError(
                "Response not found",
                status=404,
                error_type="not_found_error",
                code="response_not_found",
            )
        return JSONResponse({"id": response_id, "object": "response", "deleted": True})

    return router


def continued(conversation: Conversation, text: str, calls: list[ToolCallPart]) -> Conversation:
    extended = copy.deepcopy(conversation)
    parts: list[Part] = [TextPart(text)] if text else []
    parts.extend(calls)
    extended.messages.append(Message("assistant", parts))
    return extended


def finalize(
    response: JSON,
    text: str,
    thoughts: str,
    calls: list[ToolCallPart],
    done: OutputDone,
    expose_thoughts: bool,
    custom: set[str] | None = None,
) -> None:
    custom = custom or set()
    output: list[JSON] = []
    if thoughts and expose_thoughts:
        output.append(reasoning_item(new_id("rs_"), thoughts))
    if text or not calls:
        output.append(message_item(new_id("msg_"), text))
    for call in calls:
        prefix = "ctc_" if call.name in custom else "fc_"
        output.append(call_item(new_id(prefix), call, custom))
    response["output"] = output
    response["usage"] = usage_json(done.usage, reasoning_tokens=len(thoughts) // 4)
    response["kiro"] = done.kiro
    if done.finish == "length":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "max_output_tokens"}
    elif done.finish == "cancelled":
        response["status"] = "incomplete"
        response["incomplete_details"] = {"reason": "cancelled"}
    else:
        response["status"] = "completed"


async def stream_response(
    backend: KiroBackend, conversation: Conversation, opts: TurnOptions, response: JSON, store: bool
) -> AsyncIterator[str]:
    seq = 0

    def emit(event_type: str, **payload: Any) -> str:
        nonlocal seq
        data = {"type": event_type, "sequence_number": seq, **payload}
        seq += 1
        return sse(data, event_type)

    yield emit("response.created", response=response)
    yield emit("response.in_progress", response=response)
    output_index = 0
    text = ""
    thoughts = ""
    calls: list[ToolCallPart] = []
    msg_id: str | None = None
    rs_id: str | None = None
    expose_thoughts = backend.settings.expose_thoughts
    custom = custom_tool_names(conversation)
    try:
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                match event:
                    case OutputThought(text=chunk):
                        if not chunk or not expose_thoughts:
                            continue
                        if rs_id is None:
                            rs_id = new_id("rs_")
                            yield emit(
                                "response.output_item.added",
                                output_index=output_index,
                                item=reasoning_item(rs_id, ""),
                            )
                            yield emit(
                                "response.reasoning_summary_part.added",
                                item_id=rs_id,
                                output_index=output_index,
                                summary_index=0,
                                part={"type": "summary_text", "text": ""},
                            )
                        thoughts += chunk
                        yield emit(
                            "response.reasoning_summary_text.delta",
                            item_id=rs_id,
                            output_index=output_index,
                            summary_index=0,
                            delta=chunk,
                        )
                    case OutputText(text=chunk):
                        if not chunk:
                            continue
                        if rs_id is not None:
                            yield emit(
                                "response.reasoning_summary_text.done",
                                item_id=rs_id,
                                output_index=output_index,
                                summary_index=0,
                                text=thoughts,
                            )
                            yield emit(
                                "response.reasoning_summary_part.done",
                                item_id=rs_id,
                                output_index=output_index,
                                summary_index=0,
                                part={"type": "summary_text", "text": thoughts},
                            )
                            yield emit(
                                "response.output_item.done",
                                output_index=output_index,
                                item=reasoning_item(rs_id, thoughts),
                            )
                            response["output"].append(reasoning_item(rs_id, thoughts))
                            rs_id = None
                            output_index += 1
                        if msg_id is None:
                            msg_id = new_id("msg_")
                            yield emit(
                                "response.output_item.added",
                                output_index=output_index,
                                item=message_item(msg_id, "", "in_progress"),
                            )
                            yield emit(
                                "response.content_part.added",
                                item_id=msg_id,
                                output_index=output_index,
                                content_index=0,
                                part={"type": "output_text", "text": "", "annotations": []},
                            )
                        text += chunk
                        yield emit(
                            "response.output_text.delta",
                            item_id=msg_id,
                            output_index=output_index,
                            content_index=0,
                            delta=chunk,
                            logprobs=[],
                        )
                    case OutputToolCall(call=call):
                        if msg_id is not None:
                            yield emit(
                                "response.output_text.done",
                                item_id=msg_id,
                                output_index=output_index,
                                content_index=0,
                                text=text,
                                logprobs=[],
                            )
                            yield emit(
                                "response.content_part.done",
                                item_id=msg_id,
                                output_index=output_index,
                                content_index=0,
                                part={"type": "output_text", "text": text, "annotations": []},
                            )
                            yield emit(
                                "response.output_item.done",
                                output_index=output_index,
                                item=message_item(msg_id, text),
                            )
                            response["output"].append(message_item(msg_id, text))
                            msg_id = None
                            output_index += 1
                        calls.append(call)
                        if call.name in custom:
                            ctc_id = new_id("ctc_")
                            raw_input = custom_input(call)
                            yield emit(
                                "response.output_item.added",
                                output_index=output_index,
                                item={**custom_item(ctc_id, call, "in_progress"), "input": ""},
                            )
                            yield emit(
                                "response.custom_tool_call_input.delta",
                                item_id=ctc_id,
                                output_index=output_index,
                                delta=raw_input,
                            )
                            yield emit(
                                "response.custom_tool_call_input.done",
                                item_id=ctc_id,
                                output_index=output_index,
                                input=raw_input,
                            )
                            yield emit(
                                "response.output_item.done",
                                output_index=output_index,
                                item=custom_item(ctc_id, call),
                            )
                            response["output"].append(custom_item(ctc_id, call))
                            output_index += 1
                            continue
                        fc_id = new_id("fc_")
                        yield emit(
                            "response.output_item.added",
                            output_index=output_index,
                            item={**function_item(fc_id, call, "in_progress"), "arguments": ""},
                        )
                        yield emit(
                            "response.function_call_arguments.delta",
                            item_id=fc_id,
                            output_index=output_index,
                            delta=json_dumps(call.arguments),
                        )
                        yield emit(
                            "response.function_call_arguments.done",
                            item_id=fc_id,
                            output_index=output_index,
                            arguments=json_dumps(call.arguments),
                        )
                        yield emit(
                            "response.output_item.done",
                            output_index=output_index,
                            item=function_item(fc_id, call),
                        )
                        response["output"].append(function_item(fc_id, call))
                        output_index += 1
                    case OutputDone(finish=finish, error=error, usage=usage, kiro=kiro):
                        if rs_id is not None:
                            yield emit(
                                "response.reasoning_summary_text.done",
                                item_id=rs_id,
                                output_index=output_index,
                                summary_index=0,
                                text=thoughts,
                            )
                            yield emit(
                                "response.output_item.done",
                                output_index=output_index,
                                item=reasoning_item(rs_id, thoughts),
                            )
                            response["output"].append(reasoning_item(rs_id, thoughts))
                            output_index += 1
                        if msg_id is not None:
                            yield emit(
                                "response.output_text.done",
                                item_id=msg_id,
                                output_index=output_index,
                                content_index=0,
                                text=text,
                                logprobs=[],
                            )
                            yield emit(
                                "response.content_part.done",
                                item_id=msg_id,
                                output_index=output_index,
                                content_index=0,
                                part={"type": "output_text", "text": text, "annotations": []},
                            )
                            yield emit(
                                "response.output_item.done",
                                output_index=output_index,
                                item=message_item(msg_id, text),
                            )
                            response["output"].append(message_item(msg_id, text))
                            output_index += 1
                        if finish == "error":
                            response["status"] = "failed"
                            response["error"] = {
                                "code": "server_error",
                                "message": error or "Kiro turn failed",
                            }
                            yield emit("response.failed", response=response)
                            yield emit(
                                "error",
                                code="server_error",
                                message=error or "Kiro turn failed",
                                param=None,
                            )
                            return
                        if not response["output"]:
                            empty_id = new_id("msg_")
                            response["output"].append(message_item(empty_id, ""))
                        response["usage"] = usage_json(usage, reasoning_tokens=len(thoughts) // 4)
                        response["kiro"] = kiro
                        if finish == "length":
                            response["status"] = "incomplete"
                            response["incomplete_details"] = {"reason": "max_output_tokens"}
                            yield emit("response.incomplete", response=response)
                        else:
                            response["status"] = "completed"
                            yield emit("response.completed", response=response)
                        if store:
                            STORE.put(
                                response["id"], continued(conversation, text, calls), response
                            )
    except GatewayError as error:
        response["status"] = "failed"
        response["error"] = {"code": error.code or "server_error", "message": error.message}
        yield emit("response.failed", response=response)
        yield emit("error", code=error.code or "server_error", message=error.message, param=None)
