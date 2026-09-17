"""Scripted ACP agent speaking newline-delimited JSON-RPC over stdio.

Behaviour is driven by the *prompt text* so tests can request scenarios:

* ``"echo: <text>"``          – streams ``<text>`` back in chunks.
* ``"tool"``                  – announces a tool call, requests permission, completes it, then says ``done``.
* ``"thought"``               – emits a thought chunk then text.
* ``"slow"``                  – streams words slowly (for cancellation tests).
* ``"error"``                 – answers ``session/prompt`` with a JSON-RPC error.
* ``"fs"``                    – calls ``fs/read_text_file`` on the client for ``notes.txt`` and echoes it.
* ``"terminal"``              – runs ``echo hi`` through the client's terminal methods.
* ``"crash"``                 – exits the process mid-turn.
* ``"/effort <level>"``       – replies ``Effort set to <level>`` (v2 style).
* anything else              – replies with a fixed sentence.

Set ``FAKE_ACP_ENGINE=v3`` to advertise ``configOptions`` instead of Kiro's
``models`` extension and to reject ``session/set_model``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

JSON = dict[str, Any]
ENGINE = os.environ.get("FAKE_ACP_ENGINE", "v2")
MODELS = ["claude-haiku-4.5", "claude-sonnet-4.6", "gpt-5.6-terra"]
MODES = ["kiro_default", "kiro_planner"]


class Agent:
    def __init__(self) -> None:
        self.writer: asyncio.StreamWriter | None = None
        self.next_id = 1
        self.pending: dict[Any, asyncio.Future[Any]] = {}
        self.sessions: dict[str, JSON] = {}
        self.cancelled: set[str] = set()
        self.session_counter = 0

    async def send(self, message: JSON) -> None:
        assert self.writer is not None
        self.writer.write(json.dumps(message).encode() + b"\n")
        await self.writer.drain()

    async def request(self, method: str, params: JSON) -> Any:
        request_id = self.next_id
        self.next_id += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return await future

    async def notify(self, method: str, params: JSON) -> None:
        await self.send({"jsonrpc": "2.0", "method": method, "params": params})

    async def update(self, session_id: str, update: JSON) -> None:
        await self.notify("session/update", {"sessionId": session_id, "update": update})

    async def handle(self, message: JSON) -> None:
        if "method" not in message:
            future = self.pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(RuntimeError(json.dumps(message["error"])))
                else:
                    future.set_result(message.get("result"))
            return
        method = message["method"]
        params = message.get("params") or {}
        request_id = message.get("id")
        if request_id is None:
            if method == "session/cancel":
                self.cancelled.add(params.get("sessionId", ""))
            return
        try:
            result = await self.dispatch(method, params)
        except RpcError as error:
            await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": error.code, "message": error.message, "data": error.data},
                }
            )
            return
        await self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def dispatch(self, method: str, params: JSON) -> Any:
        if method == "initialize":
            caps: JSON = {
                "loadSession": True,
                "promptCapabilities": {
                    "image": True,
                    "audio": False,
                    "embeddedContext": ENGINE == "v3",
                },
                "mcpCapabilities": {"http": True, "sse": False},
                "sessionCapabilities": {"list": {}} if ENGINE == "v3" else {},
            }
            return {
                "protocolVersion": 1,
                "agentCapabilities": caps,
                "agentInfo": {"name": "Fake ACP Agent", "version": ENGINE},
                "authMethods": [],
            }
        if method == "session/new":
            self.session_counter += 1
            session_id = f"fake-{self.session_counter}"
            self.sessions[session_id] = {
                "cwd": params.get("cwd"),
                "model": MODELS[0],
                "mode": MODES[0],
                "autopilot": "on",
            }
            return self.session_result(session_id)
        if method == "session/load":
            session_id = params["sessionId"]
            self.sessions.setdefault(
                session_id,
                {"cwd": params.get("cwd"), "model": MODELS[0], "mode": MODES[0], "autopilot": "on"},
            )
            await self.update(
                session_id,
                {
                    "sessionUpdate": "user_message_chunk",
                    "content": {"type": "text", "text": "earlier prompt"},
                },
            )
            await self.update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "earlier answer"},
                },
            )
            result = self.session_result(session_id)
            result.pop("sessionId", None)
            return result
        if method == "session/list":
            if ENGINE != "v3":
                raise RpcError(-32601, "Method not found", "session/list")
            return {
                "sessions": [
                    {"sessionId": sid, "cwd": s["cwd"]} for sid, s in self.sessions.items()
                ]
            }
        if method == "_kiro/session/delete":
            if ENGINE != "v3":
                raise RpcError(-32601, "Method not found", method)
            self.sessions.pop(params.get("sessionId", ""), None)
            return {"success": True}
        if method == "session/set_model":
            if ENGINE == "v3":
                raise RpcError(
                    -32603, "Internal error", {"details": "no persistence classification"}
                )
            session = self.session(params)
            if params.get("modelId") not in MODELS:
                raise RpcError(
                    -32603, "Internal error", f"Model '{params.get('modelId')}' not found"
                )
            session["model"] = params["modelId"]
            return {}
        if method == "session/set_mode":
            session = self.session(params)
            if params.get("modeId") not in MODES:
                raise RpcError(-32603, "Internal error", f"Mode '{params.get('modeId')}' not found")
            session["mode"] = params["modeId"]
            return {}
        if method == "session/set_config_option":
            if ENGINE != "v3":
                raise RpcError(-32601, "Method not found", "session/set_config_option")
            session = self.session(params)
            config_id, value = params.get("configId"), params.get("value")
            if config_id == "model" and value in MODELS:
                session["model"] = value
            elif config_id == "mode" and value in MODES:
                session["mode"] = value
            elif config_id == "autopilot" and value in ("on", "off"):
                session["autopilot"] = value
            else:
                raise RpcError(-32602, "Invalid params", f"bad option {config_id}={value}")
            return {"configOptions": self.config_options(session)}
        if method == "session/prompt":
            return await self.prompt(params)
        raise RpcError(-32601, "Method not found", method)

    def session(self, params: JSON) -> JSON:
        session = self.sessions.get(params.get("sessionId", ""))
        if session is None:
            raise RpcError(-32602, "Invalid params", "unknown session")
        return session

    def config_options(self, session: JSON) -> list[JSON]:
        return [
            {
                "type": "select",
                "id": "mode",
                "name": "Mode",
                "category": "mode",
                "currentValue": session["mode"],
                "options": [{"value": m, "name": m} for m in MODES],
            },
            {
                "type": "select",
                "id": "model",
                "name": "Model",
                "category": "model",
                "currentValue": session["model"],
                "options": [{"value": m, "name": m} for m in MODELS],
            },
            {
                "type": "select",
                "id": "autopilot",
                "name": "Autopilot",
                "currentValue": session["autopilot"],
                "options": [{"value": "on"}, {"value": "off"}],
            },
        ]

    def session_result(self, session_id: str) -> JSON:
        session = self.sessions[session_id]
        result: JSON = {"sessionId": session_id}
        if ENGINE == "v3":
            result["configOptions"] = self.config_options(session)
            result["modes"] = {
                "currentModeId": session["mode"],
                "availableModes": [{"id": m, "name": m} for m in MODES],
            }
        else:
            result["modes"] = {
                "currentModeId": session["mode"],
                "availableModes": [{"id": m, "name": m} for m in MODES],
            }
            result["models"] = {
                "currentModelId": session["model"],
                "availableModels": [{"modelId": m, "name": m} for m in MODELS],
            }
        return result

    async def prompt(self, params: JSON) -> JSON:
        session_id = params["sessionId"]
        session = self.session(params)
        self.cancelled.discard(session_id)
        blocks = params.get("prompt") or []
        text = "".join(str(b.get("text", "")) for b in blocks if b.get("type") == "text")
        images = [b for b in blocks if b.get("type") == "image"]
        session.setdefault("history", []).append(text)
        # The gateway renders a transcript; the scenario is whatever the last user turn says.
        if '<message role="user">' in text:
            text = text.rsplit('<message role="user">', 1)[1].split("</message>", 1)[0].strip()
        if "\n\n(Reminder: your tools are" in text:
            text = text.split("\n\n(Reminder: your tools are", 1)[0].strip()

        async def say(chunk: str) -> None:
            await self.update(
                session_id,
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk},
                },
            )

        if text.startswith("/effort "):
            level = text.split(" ", 1)[1].strip()
            if level in ("low", "medium", "high", "max"):
                await say(f"Effort set to {level}\n")
            else:
                await say(f"invalid value '{level}' for 'output_config.effort'\n")
            return {"stopReason": "end_turn"}
        if text == "error":
            raise RpcError(-32603, "Internal error", "Encountered an error in the response stream")
        if text == "crash":
            await say("about to")
            os._exit(3)
        if text == "thought":
            await self.update(
                session_id,
                {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "thinking hard"},
                },
            )
            await say("after thinking")
        elif text == "tool":
            await self.notify(
                "_kiro.dev/session/update",
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": "tool_call_chunk",
                        "toolCallId": "call-1",
                        "title": "shell",
                        "kind": "execute",
                    },
                },
            )
            await self.update(
                session_id,
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call-1",
                    "title": "Running: ls",
                    "kind": "execute",
                    "rawInput": {"command": "ls"},
                    "_meta": {"kiro": {"toolName": "shell"}},
                },
            )
            decision = await self.request(
                "session/request_permission",
                {
                    "sessionId": session_id,
                    "toolCall": {
                        "toolCallId": "call-1",
                        "title": "Running: ls",
                        "rawInput": {"command": "ls"},
                    },
                    "options": [
                        {"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
                        {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                        {"optionId": "reject_once", "name": "No", "kind": "reject_once"},
                    ],
                },
            )
            outcome = (decision or {}).get("outcome", {})
            if outcome.get("outcome") == "selected" and str(outcome.get("optionId", "")).startswith(
                "allow"
            ):
                await self.update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "call-1",
                        "status": "completed",
                        "content": [
                            {
                                "type": "content",
                                "content": {"type": "text", "text": "a.txt\nb.txt\n"},
                            }
                        ],
                        "rawOutput": {
                            "items": [
                                {
                                    "Json": {
                                        "exit_status": "exit status: 0",
                                        "stdout": "a.txt\nb.txt\n",
                                        "stderr": "",
                                    }
                                }
                            ]
                        },
                    },
                )
                await say("done")
            else:
                await self.update(
                    session_id,
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "call-1",
                        "status": "failed",
                    },
                )
                await say("permission denied")
        elif text == "fs":
            content = await self.request(
                "fs/read_text_file",
                {"sessionId": session_id, "path": os.path.join(session["cwd"], "notes.txt")},
            )
            await say("file says: " + str((content or {}).get("content", "")))
        elif text == "terminal":
            created = await self.request(
                "terminal/create", {"sessionId": session_id, "command": "echo", "args": ["hi"]}
            )
            terminal_id = created["terminalId"]
            await self.request(
                "terminal/wait_for_exit", {"sessionId": session_id, "terminalId": terminal_id}
            )
            output = await self.request(
                "terminal/output", {"sessionId": session_id, "terminalId": terminal_id}
            )
            await self.request(
                "terminal/release", {"sessionId": session_id, "terminalId": terminal_id}
            )
            await say("terminal says: " + str(output.get("output", "")).strip())
        elif text == "slow":
            for word in ["one ", "two ", "three ", "four ", "five ", "six "]:
                if session_id in self.cancelled:
                    await self.notify(
                        "_kiro.dev/metadata", {"sessionId": session_id, "turnDurationMs": 1}
                    )
                    return {"stopReason": "cancelled"}
                await say(word)
                await asyncio.sleep(0.3)
        elif text.startswith("echo: "):
            payload = text[len("echo: ") :]
            for i in range(0, len(payload), 7):
                await say(payload[i : i + 7])
        elif text == "plan":
            await self.update(
                session_id,
                {
                    "sessionUpdate": "plan",
                    "entries": [{"content": "step 1", "priority": "high", "status": "in_progress"}],
                },
            )
            await say("planned")
        elif text == "long":
            await say("x" * 5000)
            return {"stopReason": "max_tokens"}
        elif images:
            await say(f"saw {len(images)} image(s): {images[0].get('mimeType')}")
        elif text.startswith("history?"):
            await say(json.dumps(session["history"][:-1]))
        else:
            await say(f"[{session['model']}] The fake agent replies.")
        await self.notify(
            "_kiro.dev/metadata",
            {
                "sessionId": session_id,
                "contextUsagePercentage": 1.5,
                "meteringUsage": [{"value": 0.01, "unit": "credit", "unitPlural": "credits"}],
                "turnDurationMs": 5,
            },
        )
        return {"stopReason": "end_turn"}


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    transport, protocol = await loop.connect_write_pipe(
        asyncio.streams.FlowControlMixin, sys.stdout
    )
    agent = Agent()
    agent.writer = asyncio.StreamWriter(transport, protocol, None, loop)
    print("fake agent started", file=sys.stderr, flush=True)
    tasks: set[asyncio.Task[None]] = set()
    while line := await reader.readline():
        message = json.loads(line)
        task = asyncio.create_task(agent.handle(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    for task in tasks:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
