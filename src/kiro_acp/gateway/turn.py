"""Protocol-neutral output of one gateway turn."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

from kiro_acp.gateway.conversation import JSON, ToolCallPart

FinishReason = Literal["stop", "length", "tool_calls", "error", "cancelled", "refusal"]


@dataclass(slots=True)
class OutputText:
    text: str


@dataclass(slots=True)
class OutputThought:
    text: str


@dataclass(slots=True)
class OutputToolCall:
    call: ToolCallPart


@dataclass(slots=True)
class OutputDone:
    finish: FinishReason
    text: str
    thoughts: str
    tool_calls: list[ToolCallPart]
    usage: JSON
    kiro: JSON = field(default_factory=dict)
    error: str | None = None
    session_id: str | None = None
    stop_sequence: str | None = None


OutputEvent = OutputText | OutputThought | OutputToolCall | OutputDone


def estimate_tokens(text: str) -> int:
    """Rough token estimate (Kiro meters credits, not tokens)."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))
