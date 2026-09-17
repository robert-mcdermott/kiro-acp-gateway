"""Gateway-side broker: Unix socket server that bridge processes connect to."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kiro_acp.gateway.toolbridge import protocol

LOG = logging.getLogger("kiro_acp.gateway.toolbridge")
JSON = dict[str, Any]

ToolCallListener = Callable[[str, str, JSON], Awaitable[None]]  # (call_id, name, arguments)


@dataclass
class BridgeSession:
    """State for one harness session's bridge."""

    token: str
    tools: list[JSON]
    writer: asyncio.StreamWriter | None = None
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    listener: ToolCallListener | None = None
    pending_calls: dict[str, tuple[str, JSON]] = field(default_factory=dict)

    async def send(self, message: JSON) -> None:
        if self.writer is None or self.writer.is_closing():
            return
        self.writer.write(protocol.encode(message))
        with contextlib.suppress(ConnectionError, OSError):
            await self.writer.drain()

    async def deliver_result(self, call_id: str, content: str, *, is_error: bool = False) -> bool:
        if call_id not in self.pending_calls:
            return False
        self.pending_calls.pop(call_id, None)
        await self.send(
            {
                "type": protocol.TOOL_RESULT,
                "call_id": call_id,
                "content": content,
                "is_error": is_error,
            }
        )
        return True

    async def cancel(self, reason: str = "cancelled") -> None:
        self.pending_calls.clear()
        await self.send({"type": protocol.CANCEL, "call_id": None, "reason": reason})


class ToolBridgeBroker:
    """Accepts connections from :mod:`server` processes and routes tool calls."""

    def __init__(self, socket_dir: str | None = None) -> None:
        self.socket_dir = socket_dir or tempfile.mkdtemp(prefix="kiro-gateway-bridge-")
        self.socket_path = os.path.join(self.socket_dir, "broker.sock")
        self._server: asyncio.AbstractServer | None = None
        self._sessions: dict[str, BridgeSession] = {}

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.socket_path)
        self._server = await asyncio.start_unix_server(
            self._on_connection, path=self.socket_path, limit=64 * 1024 * 1024
        )
        os.chmod(self.socket_path, 0o600)
        LOG.info("Tool bridge broker listening on %s", self.socket_path)

    async def stop(self) -> None:
        for session in list(self._sessions.values()):
            await session.cancel("gateway shutting down")
            if session.writer is not None:
                session.writer.close()
        self._sessions.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        with contextlib.suppress(FileNotFoundError, OSError):
            os.unlink(self.socket_path)
        with contextlib.suppress(OSError):
            os.rmdir(self.socket_dir)

    def register(self, tools: list[JSON]) -> BridgeSession:
        token = secrets.token_urlsafe(24)
        session = BridgeSession(token=token, tools=tools)
        self._sessions[token] = session
        return session

    def unregister(self, session: BridgeSession) -> None:
        self._sessions.pop(session.token, None)
        if session.writer is not None:
            session.writer.close()

    def mcp_server_config(self, session: BridgeSession, *, name: str = "harness") -> JSON:
        """The ``mcpServers`` entry to pass to ``session/new``."""
        import sys

        return {
            "name": name,
            "command": sys.executable,
            "args": ["-m", "kiro_acp.gateway.toolbridge.server"],
            "env": [
                {"name": "KIRO_BRIDGE_SOCKET", "value": self.socket_path},
                {"name": "KIRO_BRIDGE_TOKEN", "value": session.token},
                {"name": "PYTHONPATH", "value": os.pathsep.join(p for p in sys.path if p)},
            ],
        }

    async def _on_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        session: BridgeSession | None = None
        try:
            hello_line = await asyncio.wait_for(reader.readline(), 30)
            hello = protocol.decode(hello_line)
            session = (
                self._sessions.get(str(hello.get("token", "")))
                if hello.get("type") == protocol.HELLO
                else None
            )
            if session is None:
                LOG.warning("Bridge connection with unknown token rejected")
                writer.close()
                return
            session.writer = writer
            await session.send({"type": protocol.TOOLS, "tools": session.tools})
            session.connected.set()
            while line := await reader.readline():
                message = protocol.decode(line)
                if message.get("type") == protocol.TOOL_CALL:
                    call_id = str(message.get("call_id"))
                    name = str(message.get("name", ""))
                    arguments = (
                        message.get("arguments")
                        if isinstance(message.get("arguments"), dict)
                        else {}
                    )
                    session.pending_calls[call_id] = (name, arguments)
                    if session.listener is not None:
                        await session.listener(call_id, name, arguments)
                    else:
                        LOG.warning("Tool call %s arrived with no active turn; failing it", call_id)
                        await session.deliver_result(call_id, "no active turn", is_error=True)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, TimeoutError) as error:
            LOG.debug("Bridge connection ended: %s", error)
        finally:
            if session is not None and session.writer is writer:
                session.writer = None
                session.connected.clear()
            writer.close()
