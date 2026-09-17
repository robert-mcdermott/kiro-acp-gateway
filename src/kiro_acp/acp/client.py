"""Low-level ACP client: subprocess transport, JSON-RPC routing, and client-side dispatch."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from kiro_acp.acp import jsonrpc
from kiro_acp.acp.errors import (
    JSONRPC_INTERNAL_ERROR,
    JSONRPC_INVALID_PARAMS,
    JSONRPC_METHOD_NOT_FOUND,
    ACPError,
    ACPProcessError,
    ACPRemoteError,
    ACPTimeoutError,
)
from kiro_acp.acp.handlers import ClientHandlers
from kiro_acp.acp.types import (
    JSON,
    PROTOCOL_VERSION,
    InitializeResult,
    PermissionRequest,
    ToolCall,
)

LOG = logging.getLogger("kiro_acp.acp.client")
WIRE = logging.getLogger("kiro_acp.acp.wire")

RequestHandler = Callable[[JSON], Awaitable[Any]]
NotificationListener = Callable[[str, Any], Awaitable[None] | None]

DEFAULT_REQUEST_TIMEOUT = 60.0
STDERR_TAIL_LINES = 200


class ACPClient:
    """Drive one ACP agent subprocess over stdio.

    The client is engine-agnostic. It knows JSON-RPC framing, the ``initialize``
    handshake, how to answer agent-to-client requests through
    :class:`~kiro_acp.acp.handlers.ClientHandlers`, and how to fan out
    notifications to per-session subscribers. Higher-level session behaviour
    lives in :class:`~kiro_acp.acp.session.Session`.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        handlers: ClientHandlers | None = None,
        client_name: str = "kiro-acp",
        client_version: str = "0.1.0",
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> None:
        self.command = [str(part) for part in command]
        self.cwd = os.path.abspath(cwd) if cwd else None
        self.env = dict(env) if env is not None else None
        self.handlers = handlers or ClientHandlers()
        self.client_name = client_name
        self.client_version = client_version
        self.request_timeout = request_timeout

        self.process: asyncio.subprocess.Process | None = None
        self.initialize_result: InitializeResult | None = None
        self.stderr_tail: deque[str] = deque(maxlen=STDERR_TAIL_LINES)

        self._next_id = 1
        self._pending: dict[jsonrpc.RequestId, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._subscribers: dict[str, list[asyncio.Queue[tuple[str, Any]]]] = {}
        self._listeners: list[NotificationListener] = []
        self._tool_calls: dict[str, dict[str, ToolCall]] = {}
        self._closed = asyncio.Event()
        self._exit_error: ACPProcessError | None = None
        self._request_handlers: dict[str, RequestHandler] = {}
        self._install_default_handlers()

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.process is not None:
            raise ACPError("client already started")
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.command,
                cwd=self.cwd,
                env=self.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=64 * 1024 * 1024,
                # Own process group so kiro-cli's children (kiro-cli-chat, MCP servers)
                # die with it instead of lingering as orphans.
                start_new_session=os.name == "posix",
            )
        except FileNotFoundError as error:
            raise ACPProcessError(
                f"Agent executable not found: {self.command[0]!r} ({error})"
            ) from error
        except OSError as error:
            raise ACPProcessError(f"Failed to start agent {self.command!r}: {error}") from error
        self._reader_task = asyncio.create_task(self._read_stdout(), name="acp-stdout")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="acp-stderr")

    def _signal_group(self, sig: int) -> None:
        """Signal the agent's whole process group (falls back to the process itself)."""
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif sig == signal.SIGKILL:
                process.kill()
            else:
                process.terminate()
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(sig)

    async def initialize(self, *, timeout: float | None = None) -> InitializeResult:
        params: JSON = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": self.handlers.capabilities(),
            "clientInfo": {"name": self.client_name, "version": self.client_version},
        }
        result = await self.request("initialize", params, timeout=timeout)
        self.initialize_result = InitializeResult.model_validate(result or {})
        if self.initialize_result.protocol_version != PROTOCOL_VERSION:
            LOG.warning(
                "Agent negotiated protocol version %s (client speaks %s)",
                self.initialize_result.protocol_version,
                PROTOCOL_VERSION,
            )
        return self.initialize_result

    async def close(self, *, grace: float = 3.0) -> None:
        """Close stdin, wait for exit, escalate to terminate/kill, and cancel tasks."""
        process = self.process
        if process is None:
            return
        with contextlib.suppress(Exception):
            await self.handlers.close()
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError):
                await process.stdin.wait_closed()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), grace)
            except TimeoutError:
                self._signal_group(signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 2.0)
                except TimeoutError:
                    self._signal_group(signal.SIGKILL)
                    await process.wait()
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
        # Let the pipe readers hit EOF so the transport's pipes are closed cleanly.
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            try:
                await asyncio.wait_for(asyncio.shield(task), 2.0)
            except (TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        self._fail_pending(self._process_error("agent closed"))
        self._closed.set()

    async def __aenter__(self) -> ACPClient:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def returncode(self) -> int | None:
        return self.process.returncode if self.process else None

    # ------------------------------------------------------------------ handlers

    def _install_default_handlers(self) -> None:
        self._request_handlers["session/request_permission"] = self._handle_request_permission
        self._request_handlers["fs/read_text_file"] = self._handle_fs_read
        self._request_handlers["fs/write_text_file"] = self._handle_fs_write
        self._request_handlers["terminal/create"] = self._handle_terminal("create")
        self._request_handlers["terminal/output"] = self._handle_terminal("output")
        self._request_handlers["terminal/wait_for_exit"] = self._handle_terminal("wait_for_exit")
        self._request_handlers["terminal/kill"] = self._handle_terminal("kill")
        self._request_handlers["terminal/release"] = self._handle_terminal("release")
        # Kiro v3 (KAS) asks the host which shell to present to the model.
        self._request_handlers["_kiro/terminal/shell_type"] = self._handle_shell_type
        self._request_handlers.update(self.handlers.extra)

    def register_request_handler(self, method: str, handler: RequestHandler) -> None:
        """Handle an additional agent-to-client request method."""
        self._request_handlers[method] = handler

    def add_notification_listener(self, listener: NotificationListener) -> None:
        """Observe every notification (after session routing)."""
        self._listeners.append(listener)

    def subscribe(self, session_id: str) -> asyncio.Queue[tuple[str, Any]]:
        """Receive ``(method, params)`` for notifications addressed to ``session_id``."""
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[tuple[str, Any]]) -> None:
        queues = self._subscribers.get(session_id)
        if queues and queue in queues:
            queues.remove(queue)
        if not queues:
            self._subscribers.pop(session_id, None)

    def tool_call(self, session_id: str, tool_call_id: str) -> ToolCall | None:
        return self._tool_calls.get(session_id, {}).get(tool_call_id)

    def track_tool_call(self, session_id: str, update: JSON) -> ToolCall:
        """Merge a ``tool_call``/``tool_call_update`` payload into per-session state."""
        calls = self._tool_calls.setdefault(session_id, {})
        call_id = str(update.get("toolCallId", ""))
        call = calls.get(call_id)
        if call is None:
            call = ToolCall(id=call_id)
            calls[call_id] = call
        call.apply(update)
        return call

    def forget_tool_calls(self, session_id: str) -> None:
        self._tool_calls.pop(session_id, None)

    async def _handle_request_permission(self, params: JSON) -> JSON:
        session_id = str(params.get("sessionId", ""))
        tool_call = params.get("toolCall") or {}
        known = None
        if isinstance(tool_call, dict) and tool_call.get("toolCallId"):
            known = self.tool_call(session_id, str(tool_call["toolCallId"]))
        request = PermissionRequest.from_params(params, known)
        outcome, reason = await self.handlers.permissions.decide(request)
        LOG.info(
            "permission %s: %s [%s] -> %s (%s)",
            request.tool_call_id,
            request.title,
            request.kind.value,
            outcome.get("outcome", {}).get("optionId", outcome.get("outcome", {}).get("outcome")),
            reason,
        )
        await self._broadcast(
            session_id,
            "_client/permission_decision",
            {"request": request, "outcome": outcome, "reason": reason},
        )
        return outcome

    async def _handle_fs_read(self, params: JSON) -> JSON:
        if self.handlers.filesystem is None:
            raise ACPRemoteError(JSONRPC_METHOD_NOT_FOUND, "fs/read_text_file not supported")
        return await self.handlers.filesystem.read_text_file(params)

    async def _handle_fs_write(self, params: JSON) -> JSON:
        if self.handlers.filesystem is None:
            raise ACPRemoteError(JSONRPC_METHOD_NOT_FOUND, "fs/write_text_file not supported")
        return await self.handlers.filesystem.write_text_file(params)

    def _handle_terminal(self, operation: str) -> RequestHandler:
        async def handler(params: JSON) -> JSON:
            terminals = self.handlers.terminals
            if terminals is None:
                raise ACPRemoteError(
                    JSONRPC_METHOD_NOT_FOUND, f"terminal/{operation} not supported"
                )
            return await getattr(terminals, operation)(params)

        return handler

    async def _handle_shell_type(self, params: JSON) -> JSON:
        shell = os.environ.get("SHELL", "/bin/bash")
        name = os.path.basename(shell) or "bash"
        return {"shellType": name, "shell": name}

    # ------------------------------------------------------------------ transport

    async def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stdout = self.process.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as error:
                    LOG.error("Oversized line from agent stdout: %s", error)
                    continue
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    raw = json.loads(stripped)
                except json.JSONDecodeError:
                    LOG.warning(
                        "Non-JSON agent stdout: %s", stripped.decode("utf-8", "replace")[:500]
                    )
                    continue
                if WIRE.isEnabledFor(logging.DEBUG):
                    WIRE.debug("<-- %s", stripped.decode("utf-8", "replace"))
                self._route(jsonrpc.parse(raw))
        finally:
            error = self._process_error("agent stdout closed")
            self._exit_error = error
            self._fail_pending(error)
            await self._broadcast_all("_client/agent_exited", {"error": str(error)})

    async def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        stderr = self.process.stderr
        while True:
            try:
                line = await stderr.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not line:
                break
            text = line.decode("utf-8", "replace").rstrip()
            self.stderr_tail.append(text)
            LOG.debug("agent stderr: %s", text)

    def _route(self, message: jsonrpc.Message) -> None:
        match message:
            case jsonrpc.Response(id=request_id, result=result, error=error):
                future = self._pending.pop(request_id, None)
                if future is None:
                    LOG.debug("Response for unknown request id %r", request_id)
                    return
                if future.done():
                    return
                if error is not None:
                    future.set_exception(
                        ACPRemoteError(
                            int(error.get("code", JSONRPC_INTERNAL_ERROR)),
                            str(error.get("message", "error")),
                            error.get("data"),
                            method=getattr(future, "acp_method", None),
                        )
                    )
                else:
                    future.set_result(result)
            case jsonrpc.Request():
                task = asyncio.create_task(self._dispatch_request(message))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            case jsonrpc.Notification(method=method, params=params):
                task = asyncio.create_task(self._dispatch_notification(method, params))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
            case jsonrpc.Malformed(reason=reason, raw=raw):
                LOG.warning("Malformed agent message (%s): %s", reason, json.dumps(raw)[:300])

    async def _dispatch_request(self, message: jsonrpc.Request) -> None:
        handler = self._request_handlers.get(message.method)
        if handler is None:
            LOG.info("Unsupported agent request %s", message.method)
            await self._send(
                jsonrpc.error(
                    message.id, JSONRPC_METHOD_NOT_FOUND, f"Method not found: {message.method}"
                )
            )
            return
        params = message.params if isinstance(message.params, dict) else {}
        try:
            result = await handler(params)
            await self._send(jsonrpc.success(message.id, result if result is not None else {}))
        except ACPRemoteError as error:
            await self._send(jsonrpc.error(message.id, error.code, error.message, error.data))
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.exception("Handler for %s failed", message.method)
            code = (
                JSONRPC_INVALID_PARAMS
                if isinstance(error, KeyError | ValueError)
                else JSONRPC_INTERNAL_ERROR
            )
            with contextlib.suppress(Exception):
                await self._send(jsonrpc.error(message.id, code, str(error)))

    async def _dispatch_notification(self, method: str, params: Any) -> None:
        session_id = params.get("sessionId") if isinstance(params, dict) else None
        if method in ("session/update", "_kiro.dev/session/update") and isinstance(params, dict):
            update = params.get("update")
            if (
                isinstance(update, dict)
                and update.get("sessionUpdate") in ("tool_call", "tool_call_update")
                and session_id
            ):
                self.track_tool_call(str(session_id), update)
        if session_id:
            await self._broadcast(str(session_id), method, params)
        else:
            await self._broadcast_all(method, params)
        for listener in self._listeners:
            try:
                result = listener(method, params)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                LOG.exception("Notification listener failed for %s", method)

    async def _broadcast(self, session_id: str, method: str, params: Any) -> None:
        for queue in list(self._subscribers.get(session_id, [])):
            queue.put_nowait((method, params))

    async def _broadcast_all(self, method: str, params: Any) -> None:
        for queues in list(self._subscribers.values()):
            for queue in list(queues):
                queue.put_nowait((method, params))

    async def _send(self, message: JSON) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise ACPProcessError("agent not started")
        if process.returncode is not None:
            raise self._process_error("agent exited")
        data = jsonrpc.encode(message)
        if WIRE.isEnabledFor(logging.DEBUG):
            WIRE.debug("--> %s", data.decode("utf-8", "replace").rstrip())
        async with self._write_lock:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError) as error:
                raise self._process_error(f"failed to write to agent stdin: {error}") from error

    def _process_error(self, prefix: str) -> ACPProcessError:
        code = self.returncode
        tail = "\n".join(list(self.stderr_tail)[-15:])
        message = f"{prefix} (exit status: {code})"
        if tail:
            message += f"\n--- agent stderr ---\n{tail}"
        return ACPProcessError(message, returncode=code, stderr=tail)

    def _fail_pending(self, error: ACPProcessError) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    # ------------------------------------------------------------------ requests

    async def request(
        self, method: str, params: JSON | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send a request and await its result (raises :class:`ACPRemoteError` on JSON-RPC error)."""
        if self._exit_error is not None:
            raise self._exit_error
        request_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        future.acp_method = method  # type: ignore[attr-defined]
        self._pending[request_id] = future
        try:
            await self._send(
                jsonrpc.request(request_id, method, params if params is not None else {})
            )
            effective = self.request_timeout if timeout is None else timeout
            if effective is None or effective <= 0:
                return await future
            try:
                return await asyncio.wait_for(future, effective)
            except TimeoutError as error:
                raise ACPTimeoutError(f"{method} timed out after {effective:g}s") from error
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: JSON | None = None) -> None:
        await self._send(jsonrpc.notification(method, params if params is not None else {}))

    async def cancel_session(self, session_id: str) -> None:
        with contextlib.suppress(ACPProcessError):
            await self.notify("session/cancel", {"sessionId": session_id})
