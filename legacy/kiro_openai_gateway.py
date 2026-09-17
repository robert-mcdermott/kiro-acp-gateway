#!/usr/bin/env python3
"""OpenAI-compatible FastAPI gateway backed by `kiro-cli acp`.

Endpoints:
  GET  /health
  GET  /v1/models
  POST /v1/chat/completions

Environment:
  KIRO_CLI=kiro-cli
  KIRO_WORKSPACE=/absolute/path/to/workspace
  KIRO_API_KEY=optional-gateway-api-key
  KIRO_PERMISSIONS=deny|allow-once
  KIRO_MAX_CONCURRENCY=2
  KIRO_TIMEOUT=900
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

JSON = dict[str, Any]
LOG = logging.getLogger("kiro-gateway")


class ACPError(RuntimeError):
    pass


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: Literal["developer", "system", "user", "assistant", "tool"]
    content: str | list[JSON] | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    model: str = "auto"
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    n: int = 1
    user: str | None = None
    reasoning_effort: str | None = None
    tools: list[JSON] | None = None
    tool_choice: Any | None = None
    response_format: JSON | None = None
    logprobs: bool | None = None


class KiroACPClient:
    def __init__(
        self,
        command: list[str],
        cwd: str,
        permission_policy: str,
        debug: bool = False,
    ) -> None:
        self.command = command
        self.cwd = cwd
        self.permission_policy = permission_policy
        self.debug = debug
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: asyncio.Queue[JSON] = asyncio.Queue()
        self.next_id = 1
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.stderr_tail: list[str] = []

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            cwd=self.cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(self._read_stdout())
        self.stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message: JSON = json.loads(line)
                except json.JSONDecodeError:
                    LOG.warning("Non-JSON Kiro stdout: %s", line.decode(errors="replace").rstrip())
                    continue
                if self.debug:
                    LOG.debug("ACP <-- %s", json.dumps(message))
                if "method" in message and "id" in message:
                    asyncio.create_task(self._dispatch_agent_request(message))
                elif "method" in message:
                    await self.notifications.put(message)
                elif "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(ACPError(str(message["error"])))
                        else:
                            future.set_result(message.get("result"))
        finally:
            code = self.process.returncode if self.process else None
            details = "\n".join(self.stderr_tail[-10:])
            error = ACPError(
                f"Kiro ACP stdout closed (exit status: {code})"
                + (f"\n{details}" if details else "")
            )
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail.append(text)
            del self.stderr_tail[:-100]
            LOG.info("kiro: %s", text)

    async def _send(self, message: JSON) -> None:
        assert self.process and self.process.stdin
        if self.process.returncode is not None:
            raise ACPError(f"Kiro exited with status {self.process.returncode}")
        if self.debug:
            LOG.debug("ACP --> %s", json.dumps(message))
        self.process.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        await self.process.stdin.drain()

    async def request(self, method: str, params: JSON, timeout: float = 60) -> Any:
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def notify(self, method: str, params: JSON) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _dispatch_agent_request(self, message: JSON) -> None:
        request_id = message["id"]
        method = message["method"]
        try:
            if method != "session/request_permission":
                raise ACPError(f"Unsupported client method: {method}")
            result = self._permission_result(message.get("params", {}))
            await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:
            await self._send({
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": str(error)},
            })

    def _permission_result(self, params: JSON) -> JSON:
        tool_call = params.get("toolCall", {})
        LOG.info(
            "Permission request: title=%r kind=%r input=%s",
            tool_call.get("title") or params.get("title"),
            tool_call.get("kind"),
            json.dumps(tool_call.get("rawInput")),
        )
        if self.permission_policy == "allow-once":
            for option in params.get("options", []):
                kind = str(option.get("kind", "")).lower().replace("-", "_")
                if kind in ("allow_once", "allowonce"):
                    return {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option.get("optionId", option.get("id")),
                        }
                    }
        return {"outcome": {"outcome": "cancelled"}}

    async def initialize(self) -> JSON:
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "kiro-openai-gateway", "version": "0.1.0"},
            },
            60,
        ) or {}

    async def new_session(self) -> tuple[str, JSON]:
        result = await self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": []},
            120,
        )
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise ACPError(f"session/new returned no sessionId: {result!r}")
        return result["sessionId"], result

    async def set_model(self, session_id: str, session_info: JSON, model: str) -> None:
        available = {
            item.get("modelId")
            for item in session_info.get("models", {}).get("availableModels", [])
            if item.get("modelId")
        }
        if available and model not in available:
            raise ACPError(f"Unknown model {model!r}; available: {', '.join(sorted(available))}")
        await self.request(
            "session/set_model",
            {"sessionId": session_id, "modelId": model},
            60,
        )

    async def begin_prompt(self, session_id: str, prompt: str, timeout: float) -> asyncio.Task[Any]:
        return asyncio.create_task(
            self.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout,
            )
        )

    async def cancel(self, session_id: str) -> None:
        try:
            await self.notify("session/cancel", {"sessionId": session_id})
        except Exception:
            pass

    async def close(self) -> None:
        if not self.process:
            return
        if self.process.stdin and not self.process.stdin.is_closing():
            self.process.stdin.close()
            try:
                await self.process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task and not task.done():
                task.cancel()


class KiroRun:
    def __init__(self, model: str, prompt: str) -> None:
        self.model = model
        self.prompt_text = prompt
        self.client: KiroACPClient | None = None
        self.session_id: str | None = None
        self.prompt_task: asyncio.Task[Any] | None = None
        self.metadata: JSON = {}

    async def start(self) -> None:
        await CONCURRENCY.acquire()
        try:
            self.client = KiroACPClient(
                [KIRO_CLI, "acp"],
                WORKSPACE,
                PERMISSIONS,
                DEBUG_ACP,
            )
            await self.client.start()
            await self.client.initialize()
            self.session_id, info = await self.client.new_session()
            if self.model:
                await self.client.set_model(self.session_id, info, self.model)
            self.prompt_task = await self.client.begin_prompt(
                self.session_id, self.prompt_text, TIMEOUT
            )
        except Exception:
            await self.close()
            raise

    async def events(self) -> AsyncIterator[JSON]:
        assert self.client and self.prompt_task and self.session_id
        while not self.prompt_task.done():
            notification_task = asyncio.create_task(self.client.notifications.get())
            done, _ = await asyncio.wait(
                {self.prompt_task, notification_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if notification_task in done:
                message = notification_task.result()
                method = message.get("method")
                params = message.get("params", {})
                if params.get("sessionId") != self.session_id:
                    continue
                if method == "_kiro.dev/metadata":
                    self.metadata.update(params)
                    continue
                if method in ("session/update", "session/notification", "_kiro.dev/session/update"):
                    yield params.get("update", params)
            else:
                notification_task.cancel()
                try:
                    await notification_task
                except asyncio.CancelledError:
                    pass
        result = await self.prompt_task
        yield {"sessionUpdate": "turn_complete", "result": result or {}}

    async def close(self) -> None:
        if self.client:
            if self.session_id and self.prompt_task and not self.prompt_task.done():
                await self.client.cancel(self.session_id)
            await self.client.close()
            self.client = None
        if CONCURRENCY.locked() or CONCURRENCY._value < MAX_CONCURRENCY:
            CONCURRENCY.release()


KIRO_CLI = os.getenv("KIRO_CLI", "kiro-cli")
WORKSPACE = os.path.realpath(os.getenv("KIRO_WORKSPACE", os.getcwd()))
API_KEY = os.getenv("KIRO_API_KEY", "")
PERMISSIONS = os.getenv("KIRO_PERMISSIONS", "deny")
MAX_CONCURRENCY = max(1, int(os.getenv("KIRO_MAX_CONCURRENCY", "2")))
TIMEOUT = float(os.getenv("KIRO_TIMEOUT", "900"))
DEBUG_ACP = os.getenv("KIRO_DEBUG_ACP", "false").lower() in ("1", "true", "yes")
CONCURRENCY = asyncio.Semaphore(MAX_CONCURRENCY)
MODEL_CACHE: tuple[float, list[JSON]] | None = None
MODEL_LOCK = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not os.path.isdir(WORKSPACE):
        raise RuntimeError(f"KIRO_WORKSPACE is not a directory: {WORKSPACE}")
    if PERMISSIONS not in ("deny", "allow-once"):
        raise RuntimeError("KIRO_PERMISSIONS must be deny or allow-once")
    LOG.info("Workspace=%s permissions=%s concurrency=%s", WORKSPACE, PERMISSIONS, MAX_CONCURRENCY)
    yield


app = FastAPI(title="Kiro OpenAI Gateway", version="0.1.0", lifespan=lifespan)


def openai_error(message: str, status: int = 400, code: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "server_error",
                "param": None,
                "code": code,
            }
        },
    )


async def authorize(authorization: str | None = Header(default=None)) -> None:
    if not API_KEY:
        return
    if authorization != f"Bearer {API_KEY}":
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Invalid API key", "type": "authentication_error"}},
            headers={"WWW-Authenticate": "Bearer"},
        )


def content_text(content: str | list[JSON] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if part.get("type") != "text":
            raise ValueError(f"Unsupported message content type: {part.get('type')!r}")
        parts.append(str(part.get("text", "")))
    return "\n".join(parts)


def render_prompt(messages: list[ChatMessage]) -> str:
    labels = {
        "developer": "Developer instructions",
        "system": "System instructions",
        "user": "User",
        "assistant": "Assistant",
        "tool": "Tool",
    }
    rendered: list[str] = []
    for message in messages:
        text = content_text(message.content)
        label = labels[message.role]
        if message.name:
            label += f" ({message.name})"
        rendered.append(f"{label}:\n{text}")
    return "\n\n".join(rendered)


def validate_request(body: ChatCompletionRequest) -> None:
    if body.n != 1:
        raise ValueError("Only n=1 is supported")
    if body.reasoning_effort is not None:
        raise ValueError("reasoning_effort is not exposed by the current raw Kiro ACP server")
    if body.tools is not None or body.tool_choice is not None:
        raise ValueError("Client-defined OpenAI tools are not supported")
    if body.response_format is not None:
        raise ValueError("response_format is not supported")
    if body.logprobs:
        raise ValueError("logprobs are not supported")


def chunk_text(update: JSON) -> str:
    if update.get("sessionUpdate") not in ("agent_message_chunk", "AgentMessageChunk"):
        return ""
    content = update.get("content")
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text", ""))
    return str(content) if isinstance(content, str) else ""


def finish_reason(result: JSON) -> str:
    reason = result.get("stopReason", "end_turn")
    return "length" if reason in ("max_tokens", "length") else "stop"


async def list_kiro_models() -> list[JSON]:
    global MODEL_CACHE
    async with MODEL_LOCK:
        now = time.monotonic()
        if MODEL_CACHE and now - MODEL_CACHE[0] < 300:
            return MODEL_CACHE[1]
        client = KiroACPClient([KIRO_CLI, "acp"], WORKSPACE, "deny", DEBUG_ACP)
        await CONCURRENCY.acquire()
        try:
            await client.start()
            await client.initialize()
            _, info = await client.new_session()
            models = info.get("models", {}).get("availableModels", [])
            MODEL_CACHE = (now, models)
            return models
        finally:
            await client.close()
            CONCURRENCY.release()


@app.get("/health")
async def health() -> JSON:
    return {
        "status": "ok",
        "backend": "kiro-cli-acp",
        "workspace": WORKSPACE,
        "permissions": PERMISSIONS,
    }


@app.get("/v1/models", dependencies=[Depends(authorize)])
async def models() -> JSONResponse:
    try:
        kiro_models = await list_kiro_models()
    except Exception as error:
        LOG.exception("Failed to list models")
        return openai_error(str(error), 502, "kiro_backend_error")
    created = int(time.time())
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": item["modelId"],
                "object": "model",
                "created": created,
                "owned_by": "kiro",
            }
            for item in kiro_models
            if item.get("modelId")
        ],
    })


@app.post("/v1/chat/completions", dependencies=[Depends(authorize)])
async def chat_completions(body: ChatCompletionRequest, request: Request):
    try:
        validate_request(body)
        prompt = render_prompt(body.messages)
    except ValueError as error:
        return openai_error(str(error), 400)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

    if body.stream:
        async def stream() -> AsyncIterator[str]:
            run = KiroRun(body.model, prompt)
            try:
                await run.start()
                first = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": body.model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(first, separators=(',', ':'))}\n\n"
                async for update in run.events():
                    if await request.is_disconnected():
                        break
                    text = chunk_text(update)
                    if text:
                        event = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": body.model,
                            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
                        }
                        yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                    elif update.get("sessionUpdate") == "turn_complete":
                        event = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": body.model,
                            "choices": [{
                                "index": 0,
                                "delta": {},
                                "finish_reason": finish_reason(update.get("result", {})),
                            }],
                        }
                        yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as error:
                LOG.exception("Streaming completion failed")
                event = {"error": {"message": str(error), "type": "server_error", "code": "kiro_backend_error"}}
                yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                await run.close()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    run = KiroRun(body.model, prompt)
    chunks: list[str] = []
    result: JSON = {}
    try:
        await run.start()
        async for update in run.events():
            text = chunk_text(update)
            if text:
                chunks.append(text)
            elif update.get("sessionUpdate") == "turn_complete":
                result = update.get("result", {})
    except ACPError as error:
        LOG.exception("Completion failed")
        status = 400 if "Unknown model" in str(error) else 502
        return openai_error(str(error), status, "kiro_backend_error")
    except Exception as error:
        LOG.exception("Completion failed")
        return openai_error(str(error), 502, "kiro_backend_error")
    finally:
        await run.close()

    response: JSON = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": body.model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(chunks), "refusal": None},
            "logprobs": None,
            "finish_reason": finish_reason(result),
        }],
        "usage": None,
    }
    if run.metadata:
        response["kiro"] = {
            key: value
            for key, value in run.metadata.items()
            if key not in ("sessionId",)
        }
    return JSONResponse(response)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, error: HTTPException):
    if isinstance(error.detail, dict) and "error" in error.detail:
        return JSONResponse(error.detail, status_code=error.status_code, headers=error.headers)
    return openai_error(str(error.detail), error.status_code)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.getenv("KIRO_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("KIRO_PORT", "8000")))
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
