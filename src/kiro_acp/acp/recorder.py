"""Record every ACP frame to a JSONL file for debugging and replay.

Each file starts with a header line ``{"kiro_acp_recording": 1, ...}`` (client version,
command, engine, cwd, timestamp) followed by one line per frame::

    {"t": <seconds since start>, "dir": "out" | "in", "frame": {...}}

``out`` frames are what the client sent to the agent, ``in`` frames what the agent sent.
Recordings can be replayed by ``tests/fake_agent/replay.py`` to reproduce a session
without Kiro. Set ``KIRO_ACP_RECORD_FRAMES=<dir>`` (CLI) or
``KIRO_GATEWAY_RECORD_FRAMES=<dir>`` (gateway) to enable.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any


class FrameRecorder:
    def __init__(
        self, directory: str | os.PathLike[str], *, meta: dict[str, Any] | None = None
    ) -> None:
        self.directory = os.path.abspath(os.path.expanduser(directory))
        os.makedirs(self.directory, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        self.path = os.path.join(
            self.directory, f"acp-{stamp}-{os.getpid()}-{id(self) & 0xFFFF:04x}.jsonl"
        )
        self._started = time.monotonic()
        self._file = open(self.path, "a", encoding="utf-8")  # noqa: SIM115 - long-lived handle
        header = {
            "kiro_acp_recording": 1,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **(meta or {}),
        }
        self._file.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._file.flush()

    def record(self, direction: str, frame: Any) -> None:
        if self._file.closed:
            return
        entry = {"t": round(time.monotonic() - self._started, 4), "dir": direction, "frame": frame}
        self._file.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()
