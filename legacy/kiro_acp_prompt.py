#!/usr/bin/env python3
"""Send one prompt to Kiro CLI through ACP with interactive permissions."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

JSON = dict[str, Any]
Handler = Callable[[JSON], Awaitable[Any]]


class ACPError(RuntimeError):
    pass


class KiroACPClient:
    def __init__(
        self,
        command: list[str],
        cwd: str,
        permission_policy: str = "ask",
        debug: bool = False,
    ) -> None:
        self.command = command
        self.cwd = os.path.abspath(cwd)
        self.permission_policy = permission_policy
        self.debug = debug
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.notifications: asyncio.Queue[JSON] = asyncio.Queue()
        self.next_id = 1
        self.reader_task: asyncio.Task[None] | None = None
        self.stderr_task: asyncio.Task[None] | None = None
        self.handlers: dict[str, Handler] = {
            "session/request_permission": self._request_permission,
        }

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
                    print(
                        f"[kiro stdout] {line.decode(errors='replace').rstrip()}",
                        file=sys.stderr,
                    )
                    continue

                if self.debug:
                    print(f"<-- {json.dumps(message)}", file=sys.stderr)

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
            error = ACPError(f"Kiro ACP stdout closed (exit status: {code})")
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()

    async def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            print(f"[kiro] {line.decode(errors='replace').rstrip()}", file=sys.stderr)

    async def _send(self, message: JSON) -> None:
        assert self.process and self.process.stdin
        if self.process.returncode is not None:
            raise ACPError(f"Kiro exited with status {self.process.returncode}")
        if self.debug:
            print(f"--> {json.dumps(message)}", file=sys.stderr)
        self.process.stdin.write(
            json.dumps(message, separators=(",", ":")).encode() + b"\n"
        )
        await self.process.stdin.drain()

    async def request(self, method: str, params: JSON, timeout: float = 60) -> Any:
        request_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
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
            handler = self.handlers.get(method)
            if handler is None:
                raise ACPError(f"Unsupported client method: {method}")
            result = await handler(message.get("params", {}))
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "result": result}
            )
        except Exception as error:
            await self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": str(error)},
                }
            )

    @staticmethod
    def _cancelled_permission() -> JSON:
        return {"outcome": {"outcome": "cancelled"}}

    @staticmethod
    def _option_id(option: JSON) -> Any:
        return option.get("optionId", option.get("id"))

    @staticmethod
    def _option_kind(option: JSON) -> str:
        return str(option.get("kind", "")).lower().replace("-", "_")

    async def _request_permission(self, params: JSON) -> JSON:
        options = params.get("options", [])
        tool_call = params.get("toolCall", {})
        subject = params.get("subject", {})
        title = (
            tool_call.get("title")
            or subject.get("title")
            or params.get("title")
            or "Kiro requests permission"
        )

        print(f"\nPermission request: {title}", file=sys.stderr)

        description = params.get("description") or subject.get("description")
        if description:
            print(description, file=sys.stderr)

        raw_input = tool_call.get("rawInput") or subject.get("rawInput")
        if raw_input is not None:
            print(json.dumps(raw_input, indent=2), file=sys.stderr)

        if self.permission_policy == "deny":
            print("Permission denied by policy.", file=sys.stderr)
            return self._cancelled_permission()

        if self.permission_policy == "allow-once":
            for option in options:
                if self._option_kind(option) in ("allow_once", "allowonce"):
                    option_id = self._option_id(option)
                    print(f"Automatically selected allow-once: {option_id}", file=sys.stderr)
                    return {
                        "outcome": {
                            "outcome": "selected",
                            "optionId": option_id,
                        }
                    }
            print("No allow-once option was offered; denying.", file=sys.stderr)
            return self._cancelled_permission()

        if not sys.stdin.isatty():
            print("Cannot ask for permission without an interactive terminal; denying.", file=sys.stderr)
            return self._cancelled_permission()

        if not options:
            print("Kiro provided no selectable permission options; denying.", file=sys.stderr)
            return self._cancelled_permission()

        for index, option in enumerate(options, start=1):
            name = option.get("name") or option.get("label") or self._option_id(option)
            kind = option.get("kind", "unspecified")
            print(f"{index}. {name} [{kind}]", file=sys.stderr)
        print("0. Cancel", file=sys.stderr)

        answer = await asyncio.to_thread(input, "Select permission: ")
        try:
            selected_index = int(answer)
        except ValueError:
            selected_index = 0

        if selected_index < 1 or selected_index > len(options):
            return self._cancelled_permission()

        selected = options[selected_index - 1]
        return {
            "outcome": {
                "outcome": "selected",
                "optionId": self._option_id(selected),
            }
        }

    async def initialize(self) -> JSON:
        result = await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {
                    "name": "kiro-acp-prompt",
                    "version": "0.6.0",
                },
            },
            timeout=60,
        )
        return result or {}

    async def new_session(self) -> tuple[str, JSON]:
        result = await self.request(
            "session/new",
            {"cwd": self.cwd, "mcpServers": []},
            timeout=120,
        )
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise ACPError(f"session/new returned no sessionId: {result!r}")
        return result["sessionId"], result

    @staticmethod
    def validate_model(session_info: JSON, model: str) -> None:
        models = session_info.get("models", {})
        available = {
            item.get("modelId")
            for item in models.get("availableModels", [])
            if item.get("modelId")
        }
        if available and model not in available:
            raise ACPError(
                f"Unknown model {model!r}; available models: "
                + ", ".join(sorted(available))
            )

    async def set_model(self, session_id: str, session_info: JSON, model: str) -> None:
        self.validate_model(session_info, model)
        await self.request(
            "session/set_model",
            {"sessionId": session_id, "modelId": model},
            timeout=60,
        )

    @staticmethod
    def _extract_text(update: Any) -> str:
        if not isinstance(update, dict):
            return ""
        kind = update.get("sessionUpdate") or update.get("type")
        if kind not in ("agent_message_chunk", "AgentMessageChunk"):
            return ""
        content = update.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            return str(content.get("text", ""))
        if isinstance(content, str):
            return content
        return str(update.get("text", ""))

    async def prompt(self, session_id: str, text: str, timeout: float) -> str:
        request_task = asyncio.create_task(
            self.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=timeout,
            )
        )
        chunks: list[str] = []

        while not request_task.done():
            notification_task = asyncio.create_task(self.notifications.get())
            done, _ = await asyncio.wait(
                {request_task, notification_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            if notification_task in done:
                message = notification_task.result()
                method = message.get("method")
                params = message.get("params", {})
                if method not in (
                    "session/update",
                    "session/notification",
                    "_kiro.dev/session/update",
                ):
                    if self.debug:
                        print(
                            f"[notification] {json.dumps(message)}",
                            file=sys.stderr,
                        )
                    continue
                if params.get("sessionId") != session_id:
                    continue
                update = params.get("update", params)
                chunk = self._extract_text(update)
                if chunk:
                    chunks.append(chunk)
                    print(chunk, end="", flush=True)
                elif self.debug:
                    print(
                        f"[session update] {json.dumps(update)}",
                        file=sys.stderr,
                    )
            else:
                notification_task.cancel()
                try:
                    await notification_task
                except asyncio.CancelledError:
                    pass

        result = await request_task
        if chunks:
            print()
        elif isinstance(result, dict):
            final_text = result.get("finalText") or result.get("text")
            if final_text:
                chunks.append(str(final_text))
                print(final_text)
        return "".join(chunks)

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
            await asyncio.wait_for(self.process.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        for task in (self.reader_task, self.stderr_task):
            if task and not task.done():
                task.cancel()


async def async_main(args: argparse.Namespace) -> None:
    command = [args.kiro, "acp"]
    if args.agent:
        command += ["--agent", args.agent]

    client = KiroACPClient(
        command=command,
        cwd=args.cwd,
        permission_policy=args.permissions,
        debug=args.debug,
    )
    session_id: str | None = None
    try:
        await client.start()
        initialized = await client.initialize()
        if args.debug:
            agent_info = initialized.get("agentInfo", {})
            print(
                "[agent] "
                f"{agent_info.get('name', 'unknown')} "
                f"version={agent_info.get('version', 'unknown')}",
                file=sys.stderr,
            )

        session_id, session_info = await client.new_session()
        if args.debug:
            print(f"[session] {session_id}", file=sys.stderr)
            current_model = session_info.get("models", {}).get("currentModelId")
            print(f"[current model] {current_model}", file=sys.stderr)

        if args.model:
            await client.set_model(session_id, session_info, args.model)
            if args.debug:
                print(f"[model] {args.model}", file=sys.stderr)

        await client.prompt(session_id, args.prompt, args.timeout)
    except (asyncio.TimeoutError, TimeoutError):
        if session_id:
            await client.cancel(session_id)
        raise ACPError("Timed out waiting for Kiro")
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", help="Prompt to send to Kiro")
    parser.add_argument("--cwd", default=os.getcwd(), help="Kiro working directory")
    parser.add_argument("--kiro", default="kiro-cli", help="Kiro executable")
    parser.add_argument("--agent", help="Optional Kiro agent")
    parser.add_argument("--model", help="Model ID, e.g. gpt-5.6-terra")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument(
        "--permissions",
        choices=("deny", "ask", "allow-once"),
        default="ask",
        help="How to answer ACP permission requests",
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    try:
        asyncio.run(async_main(args))
    except (ACPError, FileNotFoundError, KeyboardInterrupt) as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
