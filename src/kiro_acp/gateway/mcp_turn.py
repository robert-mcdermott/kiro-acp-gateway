"""Harness turns backed by the MCP tool bridge.

A Kiro turn that calls a bridged tool blocks inside the MCP server until the
HTTP client returns the tool result in its *next* request. :class:`PendingTurn`
therefore pumps the ACP event stream into a queue that successive HTTP requests
consume from, and remembers which tool calls are awaiting results.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass, field
from typing import Any

from kiro_acp.acp import Session, TextDelta, ThoughtDelta, TurnComplete
from kiro_acp.acp.events import TurnEvent
from kiro_acp.gateway.conversation import JSON, ToolCallPart
from kiro_acp.gateway.toolbridge.broker import BridgeSession

LOG = logging.getLogger("kiro_acp.gateway.mcp_turn")


@dataclass(slots=True)
class BridgeCall:
    call_id: str
    name: str
    arguments: JSON


_END = object()


@dataclass
class PendingTurn:
    """One Kiro turn whose events are consumed across HTTP requests."""

    session: Session
    bridge: BridgeSession
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    awaiting: dict[str, ToolCallPart] = field(default_factory=dict)  # client id -> call part
    bridge_ids: dict[str, str] = field(default_factory=dict)  # client id -> bridge call id
    text: str = ""
    thoughts: str = ""
    finished: bool = False
    result: TurnComplete | None = None
    started: float = field(default_factory=time.monotonic)
    last_activity: float = field(default_factory=time.monotonic)

    def start(self, blocks: list[JSON], timeout: float) -> None:
        self.bridge.listener = self._on_bridge_call
        self.task = asyncio.create_task(self._pump(blocks, timeout), name="kiro-mcp-turn")

    async def _pump(self, blocks: list[JSON], timeout: float) -> None:
        try:
            async with aclosing(self.session.prompt(blocks, timeout=timeout)) as events:
                async for event in events:
                    self.last_activity = time.monotonic()
                    if isinstance(event, TextDelta):
                        self.text += event.text
                    elif isinstance(event, ThoughtDelta):
                        self.thoughts += event.text
                    if isinstance(event, TurnComplete):
                        self.result = event
                    self.queue.put_nowait(event)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # pragma: no cover - defensive
            LOG.exception("MCP turn pump failed")
            self.result = TurnComplete.__new__(TurnComplete)
            self.queue.put_nowait(error)
        finally:
            self.finished = True
            self.queue.put_nowait(_END)

    async def _on_bridge_call(self, call_id: str, name: str, arguments: JSON) -> None:
        self.last_activity = time.monotonic()
        self.queue.put_nowait(BridgeCall(call_id, name, arguments))

    async def next_event(
        self, timeout: float | None = None
    ) -> TurnEvent | BridgeCall | object | Exception:
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    @property
    def is_end(self) -> bool:
        return self.finished and self.queue.empty()

    def register_call(self, client_id: str, bridge_call_id: str, part: ToolCallPart) -> None:
        self.awaiting[client_id] = part
        self.bridge_ids[client_id] = bridge_call_id

    async def deliver(
        self,
        client_id: str,
        content: str,
        *,
        is_error: bool = False,
        images: list[JSON] | None = None,
    ) -> bool:
        bridge_id = self.bridge_ids.pop(client_id, None)
        self.awaiting.pop(client_id, None)
        self.last_activity = time.monotonic()
        if bridge_id is None:
            return False
        return await self.bridge.deliver_result(
            bridge_id, content, is_error=is_error, images=images
        )

    async def cancel(self, reason: str = "cancelled") -> None:
        await self.bridge.cancel(reason)
        with contextlib.suppress(Exception):
            await self.session.cancel()
        if self.task is not None and not self.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 5.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                self.task.cancel()
                with contextlib.suppress(BaseException):
                    await self.task


END = _END
