"""Normalized events emitted while a prompt turn is running."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kiro_acp.acp.types import JSON, PermissionRequest, PlanEntry, StopReason, ToolCall


@dataclass(slots=True)
class TextDelta:
    """A chunk of assistant-visible text (``agent_message_chunk``)."""

    text: str


@dataclass(slots=True)
class ThoughtDelta:
    """A chunk of agent reasoning (``agent_thought_chunk``)."""

    text: str


@dataclass(slots=True)
class UserMessageDelta:
    """Echo of user content, typically seen during ``session/load`` replay."""

    text: str


@dataclass(slots=True)
class ToolCallEvent:
    """A tool call was announced, updated, or finished.

    ``phase`` is one of ``"announced"`` (Kiro's early ``tool_call_chunk``),
    ``"started"`` (``tool_call``), ``"updated"`` or ``"completed"``
    (``tool_call_update``).
    """

    call: ToolCall
    phase: str
    update: JSON = field(default_factory=dict)


@dataclass(slots=True)
class PlanUpdate:
    entries: list[PlanEntry]


@dataclass(slots=True)
class PermissionDecision:
    """Recorded outcome of a permission request handled by the client policy."""

    request: PermissionRequest
    outcome: JSON
    reason: str = ""

    @property
    def granted(self) -> bool:
        return self.outcome.get("outcome", {}).get("outcome") == "selected" and not str(
            self.outcome.get("outcome", {}).get("optionId", "")
        ).startswith("reject")


@dataclass(slots=True)
class MetadataUpdate:
    """Kiro ``_kiro.dev/metadata`` (context usage, credit metering, turn duration)."""

    data: JSON


@dataclass(slots=True)
class ModeChanged:
    mode_id: str


@dataclass(slots=True)
class ExtensionNotification:
    """Any other vendor notification (``_kiro.dev/...``) not otherwise interpreted."""

    method: str
    params: Any


@dataclass(slots=True)
class TurnComplete:
    stop_reason: StopReason
    result: JSON = field(default_factory=dict)
    error: str | None = None


TurnEvent = (
    TextDelta
    | ThoughtDelta
    | UserMessageDelta
    | ToolCallEvent
    | PlanUpdate
    | PermissionDecision
    | MetadataUpdate
    | ModeChanged
    | ExtensionNotification
    | TurnComplete
)


def event_to_dict(event: TurnEvent) -> JSON:
    """Serialize an event for JSONL output."""
    match event:
        case TextDelta(text=text):
            return {"type": "text", "text": text}
        case ThoughtDelta(text=text):
            return {"type": "thought", "text": text}
        case UserMessageDelta(text=text):
            return {"type": "user_message", "text": text}
        case ToolCallEvent(call=call, phase=phase):
            return {"type": "tool_call", "phase": phase, "call": call.to_dict()}
        case PlanUpdate(entries=entries):
            return {"type": "plan", "entries": [e.model_dump() for e in entries]}
        case PermissionDecision(request=request, outcome=outcome, reason=reason):
            return {
                "type": "permission",
                "tool_call_id": request.tool_call_id,
                "title": request.title,
                "kind": request.kind.value,
                "tool_name": request.tool_name,
                "raw_input": request.raw_input,
                "outcome": outcome.get("outcome"),
                "reason": reason,
            }
        case MetadataUpdate(data=data):
            return {"type": "metadata", "data": data}
        case ModeChanged(mode_id=mode_id):
            return {"type": "mode", "mode_id": mode_id}
        case ExtensionNotification(method=method, params=params):
            return {"type": "notification", "method": method, "params": params}
        case TurnComplete(stop_reason=stop_reason, result=result, error=error):
            return {
                "type": "turn_complete",
                "stop_reason": stop_reason.value,
                "result": result,
                "error": error,
            }
    return {"type": "unknown"}
