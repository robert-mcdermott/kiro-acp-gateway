"""Wire format between the MCP bridge server and the gateway broker (newline JSON)."""

from __future__ import annotations

import json
from typing import Any

JSON = dict[str, Any]


def encode(message: JSON) -> bytes:
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


def decode(line: bytes) -> JSON:
    data = json.loads(line.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("bridge message must be an object")
    return data


# bridge -> broker
HELLO = "hello"  # {"type": "hello", "token": str}
TOOL_CALL = "tool_call"  # {"type": "tool_call", "call_id": str, "name": str, "arguments": dict}
# broker -> bridge
TOOLS = "tools"  # {"type": "tools", "tools": [{"name", "description", "inputSchema"}]}
TOOL_RESULT = (
    "tool_result"  # {"type": "tool_result", "call_id": str, "content": str, "is_error": bool}
)
CANCEL = "cancel"  # {"type": "cancel", "call_id": str | null, "reason": str}
