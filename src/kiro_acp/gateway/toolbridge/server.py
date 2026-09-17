"""Stdio MCP server spawned by Kiro; forwards tool calls to the gateway broker.

Run as ``python -m kiro_acp.gateway.toolbridge.server`` with environment:

* ``KIRO_BRIDGE_SOCKET`` – Unix socket path of the gateway broker
* ``KIRO_BRIDGE_TOKEN`` – per-session token identifying this bridge

The server speaks MCP (JSON-RPC 2.0, one message per line) on stdin/stdout and
implements ``initialize``, ``ping``, ``tools/list`` and ``tools/call``. The tool
list is supplied by the broker after the hello handshake; each ``tools/call``
blocks until the broker delivers the client's result.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from kiro_acp.gateway.toolbridge import protocol

JSON = dict[str, Any]
PROTOCOL_VERSION = "2025-06-18"
STREAM_LIMIT = 64 * 1024 * 1024  # tool lists and arguments can be large (file contents)


def log(message: str) -> None:
    print(f"[kiro-gateway bridge] {message}", file=sys.stderr, flush=True)


class BridgeServer:
    def __init__(self, socket_path: str, token: str) -> None:
        self.socket_path = socket_path
        self.token = token
        self.tools: list[JSON] = []
        self.tools_ready = asyncio.Event()
        self.pending: dict[str, asyncio.Future[JSON]] = {}
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamWriter | None = None
        self.counter = 0
        self.closed = False

    # ---------------------------------------------------------------- broker link

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_unix_connection(
            self.socket_path, limit=STREAM_LIMIT
        )
        await self.send_broker({"type": protocol.HELLO, "token": self.token})
        asyncio.create_task(self._broker_loop())

    async def send_broker(self, message: JSON) -> None:
        assert self.writer is not None
        self.writer.write(protocol.encode(message))
        await self.writer.drain()

    async def _broker_loop(self) -> None:
        assert self.reader is not None
        try:
            while line := await self.reader.readline():
                message = protocol.decode(line)
                kind = message.get("type")
                if kind == protocol.TOOLS:
                    self.tools = list(message.get("tools") or [])
                    self.tools_ready.set()
                elif kind == protocol.TOOL_RESULT:
                    future = self.pending.pop(str(message.get("call_id")), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                elif kind == protocol.CANCEL:
                    call_id = message.get("call_id")
                    targets = [call_id] if call_id else list(self.pending)
                    for target in targets:
                        future = self.pending.pop(str(target), None)
                        if future is not None and not future.done():
                            future.set_result(
                                {
                                    "content": f"Tool call cancelled: {message.get('reason', '')}",
                                    "is_error": True,
                                }
                            )
        except Exception as error:
            log(f"broker link failed: {error!r}")
        finally:
            self.closed = True
            for future in self.pending.values():
                if not future.done():
                    future.set_result({"content": "gateway connection closed", "is_error": True})
            self.pending.clear()
            self.tools_ready.set()

    # ---------------------------------------------------------------- MCP side

    async def serve_stdio(self) -> None:
        loop = asyncio.get_running_loop()
        stdin = asyncio.StreamReader(limit=STREAM_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(stdin), sys.stdin)
        transport, proto = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, sys.stdout
        )
        self.stdout = asyncio.StreamWriter(transport, proto, None, loop)
        while line := await stdin.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            asyncio.create_task(self._handle(message))

    async def _reply(
        self, request_id: Any, result: JSON | None = None, error: JSON | None = None
    ) -> None:
        assert self.stdout is not None
        payload: JSON = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result if result is not None else {}
        self.stdout.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        await self.stdout.drain()

    async def _handle(self, message: JSON) -> None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if request_id is None:  # notification
            return
        if method == "initialize":
            await self._reply(
                request_id,
                {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "kiro-gateway-harness", "version": "1"},
                },
            )
        elif method == "ping":
            await self._reply(request_id, {})
        elif method == "tools/list":
            await asyncio.wait_for(self.tools_ready.wait(), 30)
            await self._reply(request_id, {"tools": self.tools})
        elif method == "tools/call":
            await self._tools_call(request_id, params)
        else:
            await self._reply(
                request_id, error={"code": -32601, "message": f"Method not found: {method}"}
            )

    async def _tools_call(self, request_id: Any, params: JSON) -> None:
        name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        self.counter += 1
        call_id = f"{self.token[:8]}-{self.counter}-{os.getpid()}"
        future: asyncio.Future[JSON] = asyncio.get_running_loop().create_future()
        self.pending[call_id] = future
        if self.closed:
            await self._reply(
                request_id,
                {"content": [{"type": "text", "text": "gateway unavailable"}], "isError": True},
            )
            return
        await self.send_broker(
            {"type": protocol.TOOL_CALL, "call_id": call_id, "name": name, "arguments": arguments}
        )
        result = await future
        content = str(result.get("content", ""))
        await self._reply(
            request_id,
            {
                "content": [{"type": "text", "text": content or "(no output)"}],
                "isError": bool(result.get("is_error")),
            },
        )


async def main() -> None:
    socket_path = os.environ.get("KIRO_BRIDGE_SOCKET")
    token = os.environ.get("KIRO_BRIDGE_TOKEN")
    if not socket_path or not token:
        print("KIRO_BRIDGE_SOCKET and KIRO_BRIDGE_TOKEN are required", file=sys.stderr)
        sys.exit(2)
    server = BridgeServer(socket_path, token)
    try:
        await server.connect()
    except Exception as error:
        log(f"cannot connect to gateway broker at {socket_path}: {error!r}")
        sys.exit(1)
    await server.serve_stdio()


if __name__ == "__main__":
    asyncio.run(main())
