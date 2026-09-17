"""Legacy OpenAI Completions: ``POST /v1/completions``."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import aclosing

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kiro_acp.gateway.backend import GatewayError, KiroBackend, TurnOptions
from kiro_acp.gateway.conversation import JSON, Conversation, Message, TextPart
from kiro_acp.gateway.protocols.common import (
    header_options,
    int_or_none,
    new_id,
    now,
    read_json,
    sse,
    sse_response,
    stop_list,
    stream_error_body,
)
from kiro_acp.gateway.protocols.openai_chat import finish_reason, usage_json
from kiro_acp.gateway.turn import OutputDone, OutputText

LOG = logging.getLogger("kiro_acp.gateway.openai_completions")


def prompt_text(body: JSON) -> str:
    prompt = body.get("prompt", "")
    if isinstance(prompt, list):
        if len(prompt) != 1:
            raise GatewayError("Only a single prompt is supported", code="unsupported_parameter")
        prompt = prompt[0]
    if not isinstance(prompt, str):
        raise GatewayError("'prompt' must be a string", code="invalid_prompt")
    return prompt


def make_router(backend_dep, auth_dep) -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    @router.post("/completions")
    async def completions(request: Request, backend: KiroBackend = Depends(backend_dep)):
        body = await read_json(request)
        if body.get("n") not in (None, 1):
            raise GatewayError("Only n=1 is supported", code="unsupported_parameter")
        prompt = prompt_text(body)
        suffix = body.get("suffix")
        system = "Continue the text the user provides. Output only the continuation."
        if suffix:
            system += f" The continuation must lead naturally into this suffix: {suffix!r}"
        conversation = Conversation(system=system, messages=[Message("user", [TextPart(prompt)])])
        model_name = str(body.get("model") or "")
        model = await backend.resolve_model(model_name)
        opts = header_options(
            request,
            TurnOptions(
                model=model,
                stop_sequences=stop_list(body.get("stop")),
                max_tokens=int_or_none(body.get("max_tokens")),
            ),
            body=body,
        )
        completion_id = new_id("cmpl-")
        created = now()
        echo = bool(body.get("echo"))
        display_model = model_name or model or "kiro"

        if body.get("stream"):
            return sse_response(
                stream_completion(
                    backend, conversation, opts, completion_id, created, display_model, echo, prompt
                ),
                keepalive=backend.settings.sse_keepalive,
            )

        text = ""
        done: OutputDone | None = None
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                if isinstance(event, OutputText):
                    text += event.text
                elif isinstance(event, OutputDone):
                    done = event
        assert done is not None
        if done.finish == "error":
            raise GatewayError.from_kiro(done.error or "Kiro turn failed")
        return JSONResponse(
            {
                "id": completion_id,
                "object": "text_completion",
                "created": created,
                "model": display_model,
                "choices": [
                    {
                        "text": (prompt if echo else "") + text,
                        "index": 0,
                        "logprobs": None,
                        "finish_reason": finish_reason(done.finish),
                    }
                ],
                "usage": usage_json(done.usage),
                "kiro": done.kiro,
            }
        )

    return router


async def stream_completion(
    backend: KiroBackend,
    conversation: Conversation,
    opts: TurnOptions,
    completion_id: str,
    created: int,
    model: str,
    echo: bool,
    prompt: str,
) -> AsyncIterator[str]:
    def chunk(text: str, finish: str | None = None) -> str:
        return sse(
            {
                "id": completion_id,
                "object": "text_completion",
                "created": created,
                "model": model,
                "choices": [{"text": text, "index": 0, "logprobs": None, "finish_reason": finish}],
            }
        )

    if echo:
        yield chunk(prompt)
    try:
        async with aclosing(backend.run(conversation, opts)) as events:
            async for event in events:
                if isinstance(event, OutputText) and event.text:
                    yield chunk(event.text)
                elif isinstance(event, OutputDone):
                    if event.finish == "error":
                        yield sse(stream_error_body(event.error or "Kiro turn failed"))
                    else:
                        yield chunk("", finish_reason(event.finish))
    except GatewayError as error:
        yield sse(
            {"error": {"message": error.message, "type": error.error_type, "code": error.code}}
        )
    yield "data: [DONE]\n\n"
