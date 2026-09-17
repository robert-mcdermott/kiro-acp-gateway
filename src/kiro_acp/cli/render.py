"""Render turn events for the terminal (text, json, jsonl)."""

from __future__ import annotations

import json
import sys
from typing import TextIO

from kiro_acp.acp.events import (
    MetadataUpdate,
    PermissionDecision,
    PlanUpdate,
    TextDelta,
    ThoughtDelta,
    ToolCallEvent,
    TurnComplete,
    TurnEvent,
    event_to_dict,
)
from kiro_acp.acp.types import ToolCallStatus


class TextRenderer:
    """Stream assistant text to stdout; optional tool/thought activity to stderr."""

    def __init__(
        self,
        *,
        show_tools: bool = False,
        show_thoughts: bool = False,
        quiet: bool = False,
        out: TextIO = sys.stdout,
        err: TextIO = sys.stderr,
    ) -> None:
        self.show_tools = show_tools
        self.show_thoughts = show_thoughts
        self.quiet = quiet
        self.out = out
        self.err = err
        self._wrote_text = False
        self._in_thought = False

    def status(self, message: str) -> None:
        if not self.quiet:
            print(message, file=self.err, flush=True)

    def handle(self, event: TurnEvent) -> None:
        match event:
            case TextDelta(text=text):
                if self._in_thought and self.show_thoughts:
                    print("", file=self.err, flush=True)
                    self._in_thought = False
                self.out.write(text)
                self.out.flush()
                self._wrote_text = True
            case ThoughtDelta(text=text):
                if self.show_thoughts:
                    if not self._in_thought:
                        self.err.write("[thinking] ")
                        self._in_thought = True
                    self.err.write(text)
                    self.err.flush()
            case ToolCallEvent(call=call, phase=phase):
                if self.show_tools and not self.quiet:
                    if phase == "started":
                        self.err.write(f"[tool] {call.title} ({call.kind.value})\n")
                    elif phase == "completed":
                        marker = "ok" if call.status == ToolCallStatus.COMPLETED else "failed"
                        self.err.write(f"[tool] {call.title}: {marker}\n")
                    self.err.flush()
            case PermissionDecision(request=request, reason=reason):
                if not self.quiet:
                    verdict = "allowed" if event.granted else "denied"
                    self.err.write(f"[permission] {request.title}: {verdict} ({reason})\n")
                    self.err.flush()
            case PlanUpdate(entries=entries):
                if self.show_tools and not self.quiet:
                    for entry in entries:
                        self.err.write(f"[plan] [{entry.status}] {entry.content}\n")
                    self.err.flush()
            case MetadataUpdate():
                pass
            case TurnComplete(stop_reason=stop_reason, error=error):
                if self._wrote_text:
                    self.out.write("\n")
                    self.out.flush()
                if error:
                    self.err.write(f"error: {error}\n")
                elif stop_reason.value not in ("end_turn",) and not self.quiet:
                    self.err.write(f"[stop] {stop_reason.value}\n")
                self.err.flush()


class JsonlRenderer:
    def __init__(self, out: TextIO = sys.stdout) -> None:
        self.out = out

    def status(self, message: str) -> None:
        pass

    def handle(self, event: TurnEvent) -> None:
        self.out.write(json.dumps(event_to_dict(event), ensure_ascii=False, default=str) + "\n")
        self.out.flush()


def print_json(data: object, out: TextIO = sys.stdout) -> None:
    out.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")
    out.flush()
