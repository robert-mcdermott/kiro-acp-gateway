"""Session-level ACP operations: prompt turns as event streams, model/mode/effort control."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_acp.acp.client import ACPClient
from kiro_acp.acp.errors import ACPError, ACPProcessError, ACPRemoteError, ACPTimeoutError
from kiro_acp.acp.events import (
    ExtensionNotification,
    MetadataUpdate,
    ModeChanged,
    PermissionDecision,
    PlanUpdate,
    TextDelta,
    ThoughtDelta,
    ToolCallEvent,
    TurnComplete,
    TurnEvent,
    UserMessageDelta,
)
from kiro_acp.acp.types import (
    JSON,
    ConfigOption,
    PlanEntry,
    SessionInfo,
    StopReason,
    ToolCall,
)

LOG = logging.getLogger("kiro_acp.acp.session")

EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

SESSION_UPDATE_METHODS = ("session/update", "session/notification", "_kiro.dev/session/update")


class EffortNotSupported(ACPError):
    """The agent/engine/model does not allow changing effort on a live session."""


@dataclass
class TurnResult:
    """Everything collected during one prompt turn."""

    stop_reason: StopReason = StopReason.END_TURN
    text: str = ""
    thoughts: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    plan: list[PlanEntry] = field(default_factory=list)
    metadata: JSON = field(default_factory=dict)
    permissions: list[PermissionDecision] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0
    result: JSON = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None and self.stop_reason != StopReason.ERROR

    def to_dict(self) -> JSON:
        return {
            "stop_reason": self.stop_reason.value,
            "text": self.text,
            "thoughts": self.thoughts,
            "tool_calls": [call.to_dict() for call in self.tool_calls],
            "plan": [entry.model_dump() for entry in self.plan],
            "metadata": self.metadata,
            "permissions": [
                {
                    "tool_call_id": d.request.tool_call_id,
                    "title": d.request.title,
                    "kind": d.request.kind.value,
                    "granted": d.granted,
                    "reason": d.reason,
                }
                for d in self.permissions
            ],
            "error": self.error,
            "duration_ms": self.duration_ms,
        }


def text_block(text: str) -> JSON:
    return {"type": "text", "text": text}


def image_block(data_base64: str, mime_type: str, *, uri: str | None = None) -> JSON:
    block: JSON = {"type": "image", "data": data_base64, "mimeType": mime_type}
    if uri:
        block["uri"] = uri
    return block


def resource_link_block(uri: str, name: str, *, mime_type: str | None = None) -> JSON:
    block: JSON = {"type": "resource_link", "uri": uri, "name": name}
    if mime_type:
        block["mimeType"] = mime_type
    return block


def embedded_text_block(uri: str, text: str, *, mime_type: str | None = None) -> JSON:
    resource: JSON = {"uri": uri, "text": text}
    if mime_type:
        resource["mimeType"] = mime_type
    return {"type": "resource", "resource": resource}


class Session:
    """A live ACP session bound to an :class:`ACPClient`."""

    def __init__(
        self, client: ACPClient, info: SessionInfo, *, cwd: str, engine: str | None = None
    ) -> None:
        self.client = client
        self.info = info
        self.cwd = cwd
        self.engine = engine
        self.session_id = info.session_id
        self.effort: str | None = None
        self.effort_error: str | None = None
        self._turn_lock = asyncio.Lock()
        self._active_turn = False
        # Persistent subscription so notifications between turns (e.g. late
        # ``config_option_update`` from the v3 engine) are not lost.
        self._queue: asyncio.Queue[tuple[str, Any]] = client.subscribe(self.session_id)
        self._closed = False

    # ------------------------------------------------------------------ configuration

    @property
    def model_id(self) -> str | None:
        return self.info.current_model_id

    @property
    def mode_id(self) -> str | None:
        return self.info.current_mode_id

    @property
    def is_busy(self) -> bool:
        return self._active_turn

    def close(self) -> None:
        """Stop receiving notifications for this session."""
        if not self._closed:
            self._closed = True
            self.client.unsubscribe(self.session_id, self._queue)

    def drain(self) -> list[TurnEvent]:
        """Apply queued out-of-turn notifications and return the resulting events."""
        events: list[TurnEvent] = []
        while not self._queue.empty():
            method, params = self._queue.get_nowait()
            events.extend(self._translate(method, params))
        return events

    async def wait_for(self, predicate: Callable[[SessionInfo], bool], *, timeout: float) -> bool:
        """Process notifications until ``predicate(self.info)`` holds or ``timeout`` elapses."""
        deadline = time.monotonic() + timeout
        while True:
            self.drain()
            if predicate(self.info):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                method, params = await asyncio.wait_for(self._queue.get(), remaining)
            except TimeoutError:
                return predicate(self.info)
            self._translate(method, params)

    async def set_config_option(self, option_id: str, value: Any) -> list[ConfigOption]:
        result = await self.client.request(
            "session/set_config_option",
            {"sessionId": self.session_id, "configId": option_id, "value": value},
        )
        options = [
            ConfigOption.model_validate(item)
            for item in (result or {}).get("configOptions", [])
            if isinstance(item, dict) and item.get("id")
        ]
        self.info.apply_config_options(options)
        return options

    async def set_model(self, model_id: str) -> None:
        """Select a model, validating against the advertised list when one exists."""
        available = self.info.model_ids
        if available and model_id not in available:
            raise ACPRemoteError(
                -32602,
                f"Unknown model {model_id!r}",
                f"available models: {', '.join(available)}",
                method="set_model",
            )
        option = self.info.config_option("model", category="model")
        if option is not None:
            await self.set_config_option(option.id, model_id)
        else:
            await self.client.request(
                "session/set_model", {"sessionId": self.session_id, "modelId": model_id}
            )
        self.info.current_model_id = model_id

    async def set_mode(self, mode_id: str) -> None:
        available = self.info.mode_ids
        if available and mode_id not in available:
            raise ACPRemoteError(
                -32602,
                f"Unknown mode {mode_id!r}",
                f"available modes: {', '.join(available)}",
                method="set_mode",
            )
        await self.client.request(
            "session/set_mode", {"sessionId": self.session_id, "modeId": mode_id}
        )
        self.info.current_mode_id = mode_id

    async def set_effort(self, level: str) -> None:
        """Change reasoning effort on a live session.

        Uses an ``effort`` config option when the engine exposes one; otherwise
        falls back to the Kiro v2 engine's ``/effort`` slash command delivered
        as a prompt. Raises :class:`EffortNotSupported` when neither works.
        """
        level = normalize_effort(level)
        option = self.info.config_option("effort", category="thought_level")
        if option is not None:
            await self.set_config_option(option.id, level)
            self.effort = level
            return
        if self.engine == "v3":
            # KAS treats slash commands as ordinary chat; without an effort option there is no API.
            raise EffortNotSupported(
                f"The v3 engine exposes no effort setting for model {self.model_id!r}"
            )
        result = await self.prompt_text(f"/effort {level}", timeout=60)
        reply = result.text.strip()
        if result.ok and ("effort set to" in reply.lower() or reply.lower() == "ok"):
            self.effort = level
            return
        raise EffortNotSupported(reply or f"Agent did not accept effort level {level!r}")

    # ------------------------------------------------------------------ prompting

    async def prompt_text(self, text: str, *, timeout: float | None = None) -> TurnResult:
        return await self.collect(self.prompt([text_block(text)], timeout=timeout))

    async def collect(self, events: AsyncIterator[TurnEvent]) -> TurnResult:
        result = TurnResult()
        started = time.monotonic()
        text: list[str] = []
        thoughts: list[str] = []
        seen_calls: dict[str, ToolCall] = {}
        async for event in events:
            match event:
                case TextDelta(text=chunk):
                    text.append(chunk)
                case ThoughtDelta(text=chunk):
                    thoughts.append(chunk)
                case ToolCallEvent(call=call):
                    if call.id not in seen_calls:
                        seen_calls[call.id] = call
                        result.tool_calls.append(call)
                case PlanUpdate(entries=entries):
                    result.plan = entries
                case MetadataUpdate(data=data):
                    result.metadata.update(data)
                case PermissionDecision():
                    result.permissions.append(event)
                case TurnComplete(stop_reason=stop_reason, error=error, result=raw):
                    result.stop_reason = stop_reason
                    result.error = error
                    result.result = raw
        result.text = "".join(text)
        result.thoughts = "".join(thoughts)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    async def prompt(
        self,
        blocks: list[JSON],
        *,
        timeout: float | None = None,
    ) -> AsyncIterator[TurnEvent]:
        """Run one prompt turn and yield normalized events until it completes.

        Only one turn may run at a time per session. ``timeout`` bounds the
        whole turn; on expiry the turn is cancelled via ``session/cancel`` and
        a :class:`TurnComplete` with ``stop_reason="cancelled"`` is emitted.
        """
        async with self._turn_lock:
            self._active_turn = True
            self.drain()
            queue = self._queue
            request_task = asyncio.create_task(
                self.client.request(
                    "session/prompt",
                    {"sessionId": self.session_id, "prompt": blocks},
                    timeout=0,
                ),
                name=f"acp-prompt-{self.session_id[:8]}",
            )
            deadline = time.monotonic() + timeout if timeout and timeout > 0 else None
            timed_out = False
            try:
                while True:
                    if request_task.done() and queue.empty():
                        break
                    getter = asyncio.create_task(queue.get())
                    wait_for = None
                    if deadline is not None:
                        wait_for = max(deadline - time.monotonic(), 0.0)
                    done, _ = await asyncio.wait(
                        {request_task, getter},
                        timeout=wait_for,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if getter in done:
                        method, params = getter.result()
                        for event in self._translate(method, params):
                            yield event
                            if isinstance(event, TurnComplete):
                                return
                        continue
                    getter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await getter
                    if not done and deadline is not None and not timed_out:
                        timed_out = True
                        LOG.warning("Turn timed out after %ss; cancelling", timeout)
                        await self.cancel()
                        deadline = time.monotonic() + 15.0
                        continue
                    if not done and timed_out:
                        request_task.cancel()
                        yield TurnComplete(
                            StopReason.CANCELLED, error=f"turn timed out after {timeout}s"
                        )
                        return
                yield self._complete(request_task, timed_out=timed_out)
            finally:
                if not request_task.done():
                    request_task.cancel()
                    with contextlib.suppress(BaseException):
                        await request_task
                self._active_turn = False

    def _complete(self, request_task: asyncio.Task[Any], *, timed_out: bool) -> TurnComplete:
        try:
            raw = request_task.result() or {}
        except ACPRemoteError as error:
            return TurnComplete(StopReason.ERROR, error=str(error))
        except (ACPProcessError, ACPTimeoutError) as error:
            return TurnComplete(StopReason.ERROR, error=str(error))
        except asyncio.CancelledError:
            return TurnComplete(StopReason.CANCELLED, error="cancelled")
        if not isinstance(raw, dict):
            raw = {"value": raw}
        stop = StopReason.parse(raw.get("stopReason", "end_turn"))
        if timed_out and stop == StopReason.CANCELLED:
            return TurnComplete(stop, raw, error="turn timed out")
        return TurnComplete(stop, raw)

    async def cancel(self) -> None:
        await self.client.cancel_session(self.session_id)

    # ------------------------------------------------------------------ translation

    def _translate(self, method: str, params: Any) -> list[TurnEvent]:
        if method == "_client/permission_decision":
            return [
                PermissionDecision(params["request"], params["outcome"], params.get("reason", ""))
            ]
        if method == "_client/agent_exited":
            return [TurnComplete(StopReason.ERROR, error=str(params.get("error", "agent exited")))]
        if method in SESSION_UPDATE_METHODS:
            update = params.get("update", params) if isinstance(params, dict) else {}
            if not isinstance(update, dict):
                return []
            return self._translate_update(update)
        if method == "_kiro.dev/metadata" and isinstance(params, dict):
            return [MetadataUpdate({k: v for k, v in params.items() if k != "sessionId"})]
        return [ExtensionNotification(method, params)]

    def _translate_update(self, update: JSON) -> list[TurnEvent]:
        kind = str(update.get("sessionUpdate") or update.get("type") or "")
        match kind:
            case "agent_message_chunk" | "AgentMessageChunk":
                text = _content_text(update)
                return [TextDelta(text)] if text else []
            case "agent_thought_chunk" | "AgentThoughtChunk":
                text = _content_text(update)
                return [ThoughtDelta(text)] if text else []
            case "user_message_chunk":
                text = _content_text(update)
                return [UserMessageDelta(text)] if text else []
            case "tool_call_chunk":
                call = self.client.track_tool_call(self.session_id, update)
                return [ToolCallEvent(call, "announced", update)]
            case "tool_call":
                call = self.client.track_tool_call(self.session_id, update)
                return [ToolCallEvent(call, "started", update)]
            case "tool_call_update":
                call = self.client.track_tool_call(self.session_id, update)
                phase = "completed" if call.is_terminal else "updated"
                return [ToolCallEvent(call, phase, update)]
            case "plan":
                entries = [
                    PlanEntry.model_validate(e)
                    for e in update.get("entries", [])
                    if isinstance(e, dict)
                ]
                return [PlanUpdate(entries)]
            case "current_mode_update":
                mode_id = update.get("currentModeId") or update.get("modeId")
                if mode_id:
                    self.info.current_mode_id = str(mode_id)
                    return [ModeChanged(str(mode_id))]
                return []
            case "config_option_update":
                options = [
                    ConfigOption.model_validate(o)
                    for o in update.get("configOptions", [])
                    if isinstance(o, dict) and o.get("id")
                ]
                self.info.apply_config_options(options)
                return [ExtensionNotification("session/update:config_option_update", update)]
            case "usage_update":
                return [MetadataUpdate({k: v for k, v in update.items() if k != "sessionUpdate"})]
            case "session_info_update":
                return self._translate_session_info(update)
            case _:
                return [ExtensionNotification(f"session/update:{kind}", update)]

    def _translate_session_info(self, update: JSON) -> list[TurnEvent]:
        """Kiro v3 packs metering/context data into ``session_info_update._meta.kiro``."""
        meta = update.get("_meta") or {}
        kiro = meta.get("kiro") if isinstance(meta, dict) else None
        if not isinstance(kiro, dict):
            return [ExtensionNotification("session/update:session_info_update", update)]
        kind = kiro.get("kind")
        if kind == "context_usage":
            usage = kiro.get("contextUsage") or {}
            pct = usage.get("usagePercentage", kiro.get("usagePercentage"))
            return [MetadataUpdate({"contextUsagePercentage": pct})]
        if kind == "turn_completion":
            metering = [
                {
                    "value": s.get("usage"),
                    "unit": s.get("unit", "credit"),
                    "unitPlural": s.get("unitPlural", "credits"),
                    "usedTools": s.get("usedTools", []),
                }
                for s in kiro.get("promptTurnSummaries", [])
                if isinstance(s, dict)
            ]
            data: JSON = {"meteringUsage": metering}
            if "elapsedTime" in kiro:
                data["turnDurationMs"] = kiro["elapsedTime"]
            if "requestIds" in kiro:
                data["requestIds"] = kiro["requestIds"]
            return [MetadataUpdate(data)]
        return [ExtensionNotification(f"session/update:session_info_update:{kind}", update)]


def _content_text(update: JSON) -> str:
    content = update.get("content")
    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return str(update.get("text", "") or "")


def normalize_effort(level: str) -> str:
    """Map OpenAI/Anthropic effort names onto Kiro's ``low|medium|high|max`` scale."""
    value = str(level).strip().lower()
    aliases = {
        "minimal": "low",
        "none": "low",
        "xhigh": "max",
        "extra_high": "max",
        "maximum": "max",
    }
    value = aliases.get(value, value)
    if value not in ("low", "medium", "high", "max"):
        raise ValueError(
            f"Unknown effort level {level!r}; expected one of low, medium, high, xhigh, max"
        )
    return value
