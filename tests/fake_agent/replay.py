"""Replay a recorded ACP session (``KIRO_ACP_RECORD_FRAMES`` JSONL) as an ACP agent.

Usage: ``python -m tests.fake_agent.replay <recording.jsonl>`` (or run this file).

For every request the client sends, the next recorded client request with the same
method is matched and everything the agent sent after it, up to and including its
response, is replayed: notifications verbatim, agent-to-client requests as real requests
(the answer is awaited but not checked), and the response with the id rewritten to the
live request's id. Session ids are the recorded ones, so a client that uses what
``session/new`` returns sees a consistent session.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any


def load(path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with open(path, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    header = lines[0] if lines and "kiro_acp_recording" in lines[0] else {}
    frames = lines[1:] if header else lines
    return header, frames


class Replayer:
    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self.frames = frames
        self.cursor = 0  # first unreplayed frame
        self.pending: dict[Any, asyncio.Future[Any]] = {}
        self.counter = 1000

    def send(self, message: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()

    async def handle(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            future = self.pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                future.set_result(message.get("result"))
            return
        if message.get("id") is None:
            return  # notifications (session/cancel) need no replay
        method = message["method"]
        live_id = message["id"]
        # find the next recorded outbound request with this method
        index = None
        for i in range(self.cursor, len(self.frames)):
            frame = self.frames[i]
            if (
                frame["dir"] == "out"
                and frame["frame"].get("method") == method
                and "id" in frame["frame"]
            ):
                index = i
                break
        if index is None:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": live_id,
                    "error": {"code": -32601, "message": f"no recorded {method}"},
                }
            )
            return
        recorded_id = self.frames[index]["frame"]["id"]
        i = index + 1
        while i < len(self.frames):
            frame = self.frames[i]
            i += 1
            if frame["dir"] != "in":
                continue
            payload = frame["frame"]
            if "method" in payload:
                if payload.get("id") is None:
                    self.send(payload)
                else:
                    self.counter += 1
                    request = {**payload, "id": self.counter}
                    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
                    self.pending[self.counter] = future
                    self.send(request)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(future, 30)
                continue
            if payload.get("id") == recorded_id:
                self.send({**payload, "id": live_id})
                self.cursor = i
                return
        self.send(
            {
                "jsonrpc": "2.0",
                "id": live_id,
                "error": {"code": -32603, "message": "recording ended"},
            }
        )


async def main(path: str) -> None:
    _, frames = load(path)
    replayer = Replayer(frames)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    tasks: set[asyncio.Task[None]] = set()
    while True:
        line = await reader.readline()
        if not line:
            break
        if not line.strip():
            continue
        task = asyncio.create_task(replayer.handle(json.loads(line)))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    for task in tasks:
        task.cancel()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
