"""Minimal JSON-RPC 2.0 message helpers for newline-delimited stdio transports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

JSON = dict[str, Any]
RequestId = int | str


@dataclass(slots=True)
class Request:
    id: RequestId
    method: str
    params: Any = None


@dataclass(slots=True)
class Notification:
    method: str
    params: Any = None


@dataclass(slots=True)
class Response:
    id: RequestId
    result: Any = None
    error: JSON | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass(slots=True)
class Malformed:
    raw: JSON = field(default_factory=dict)
    reason: str = ""


Message = Request | Notification | Response | Malformed


def encode(message: JSON) -> bytes:
    """Serialize one JSON-RPC message as a single newline-terminated line."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def request(request_id: RequestId, method: str, params: Any = None) -> JSON:
    message: JSON = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def notification(method: str, params: Any = None) -> JSON:
    message: JSON = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = params
    return message


def success(request_id: RequestId, result: Any) -> JSON:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: RequestId | None, code: int, message: str, data: Any = None) -> JSON:
    body: JSON = {"code": code, "message": message}
    if data is not None:
        body["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": body}


def parse(raw: JSON) -> Message:
    """Classify a decoded JSON object as a request, notification, or response."""
    if not isinstance(raw, dict):
        return Malformed(raw={"value": raw}, reason="message is not a JSON object")
    has_method = "method" in raw
    has_id = "id" in raw
    if has_method and has_id:
        if not isinstance(raw["method"], str):
            return Malformed(raw=raw, reason="method is not a string")
        return Request(id=raw["id"], method=raw["method"], params=raw.get("params"))
    if has_method:
        if not isinstance(raw["method"], str):
            return Malformed(raw=raw, reason="method is not a string")
        return Notification(method=raw["method"], params=raw.get("params"))
    if has_id:
        err = raw.get("error")
        if err is not None and not isinstance(err, dict):
            return Malformed(raw=raw, reason="error is not an object")
        return Response(id=raw["id"], result=raw.get("result"), error=err)
    return Malformed(raw=raw, reason="message has neither method nor id")
