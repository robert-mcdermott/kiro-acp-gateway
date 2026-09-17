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


def sse_response(generator: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(generator, media_type="text/event-stream", headers=SSE_HEADERS)


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
