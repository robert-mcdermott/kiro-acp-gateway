"""Helpers shared by protocol adapters."""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from kiro_acp.gateway.backend import GatewayError, TurnOptions
from kiro_acp.gateway.conversation import JSON, ImagePart

_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>[\w.+-]+/[\w.+-]+)?(?:;charset=[\w-]+)?;base64,(?P<data>.+)$", re.DOTALL
)

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:24]}"


def now() -> int:
    return int(time.time())


def sse(data: Any, event: str | None = None) -> str:
    payload = (
        data
        if isinstance(data, str)
        else json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    )
    if event:
        return f"event: {event}\ndata: {payload}\n\n"
    return f"data: {payload}\n\n"


def sse_response(
    generator: AsyncIterator[str], *, keepalive: float = 0.0, ping: str = ": keepalive\n\n"
) -> StreamingResponse:
    if keepalive and keepalive > 0:
        generator = with_keepalive(generator, keepalive, ping)
    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


async def with_keepalive(
    source: AsyncIterator[str], interval: float, ping: str
) -> AsyncIterator[str]:
    """Yield from ``source``, inserting ``ping`` whenever it is silent for ``interval`` seconds.

    Kiro emits nothing while one of its tools runs, and some clients (Claude Code) abort a
    stream that is silent for too long.
    """
    import asyncio
    from contextlib import aclosing

    async with aclosing(source) as events:
        iterator = events.__aiter__()
        pending: asyncio.Task[str] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=interval)
                if not done:
                    yield ping
                    continue
                task, pending = pending, None
                try:
                    item = task.result()
                except StopAsyncIteration:
                    return
                yield item
        finally:
            if pending is not None and not pending.done():
                pending.cancel()


def stream_error_body(message: str) -> JSON:
    from kiro_acp.gateway.backend import classify_kiro_error

    _status, error_type, code, _retry = classify_kiro_error(message)
    return {"error": {"message": message, "type": error_type, "code": code}}


def image_from_data_url(url: str, *, default_mime: str = "image/png") -> ImagePart:
    match = _DATA_URL_RE.match(url.strip())
    if not match:
        raise GatewayError(
            "Only base64 data: URLs are supported for images (remote image URLs are not fetched)",
            code="unsupported_image",
        )
    data = match.group("data").strip()
    try:
        base64.b64decode(data, validate=True)
    except ValueError as error:
        raise GatewayError("Invalid base64 image data", code="invalid_image") from error
    return ImagePart(mime_type=match.group("mime") or default_mime, data_base64=data)


def header_options(request: Request, opts: TurnOptions) -> TurnOptions:
    """Apply ``X-Kiro-*`` request headers (agent, effort, permissions)."""
    agent = request.headers.get("x-kiro-agent")
    effort = request.headers.get("x-kiro-effort")
    permissions = request.headers.get("x-kiro-permissions")
    if agent:
        opts.agent = agent
    if effort:
        opts.effort = effort
    if permissions:
        opts.permissions = permissions.strip().lower()
    opts.request_id = request.headers.get("x-request-id") or new_id("req_")
    return opts


def stop_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str) and v]
    return []


def int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


async def read_json(request: Request) -> JSON:
    try:
        body = await request.json()
    except json.JSONDecodeError as error:
        raise GatewayError(f"Invalid JSON body: {error}", code="invalid_json") from error
    if not isinstance(body, dict):
        raise GatewayError("Request body must be a JSON object", code="invalid_json")
    return body


def text_of_content(content: Any) -> str:
    """Flatten string / list-of-parts content into text (parts of unknown type are ignored)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, str):
                pieces.append(part)
            elif isinstance(part, dict):
                kind = part.get("type")
                if kind in ("text", "input_text", "output_text") and isinstance(
                    part.get("text"), str
                ):
                    pieces.append(part["text"])
                elif kind == "refusal" and isinstance(part.get("refusal"), str):
                    pieces.append(part["refusal"])
        return "".join(pieces)
    return str(content)


StreamFactory = Callable[[], Awaitable[None]]
