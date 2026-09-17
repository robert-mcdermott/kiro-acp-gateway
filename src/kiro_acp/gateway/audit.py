"""Per-session audit ledger: permission decisions, tool calls, turns, cancels, stalls.

Bounded (sessions × records), in memory, with secrets masked before storage so the
ledger can be shown to operators without leaking what a tool saw or was given.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict, deque
from typing import Any

from kiro_acp.gateway.conversation import JSON

_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}"),
    re.compile(
        r"(?i)\b(?:api[_-]?key|token|secret|password|passwd|authorization)\s*[=:]\s*['\"]?[^\s'\"&]{6,}"
    ),
    re.compile(r"://[^/\s:@]+:[^/\s@]+@"),
]
_SECRET_KEYS = re.compile(
    r"(?i)(api[_-]?key|token|secret|password|passwd|authorization|credential)"
)
EXCERPT = 400


def redact_text(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(
            lambda m: m.group(0)[: min(8, len(m.group(0)) // 3)] + "[REDACTED]", text
        )
    return text


def redact(value: Any, *, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            str(k): ("[REDACTED]" if _SECRET_KEYS.search(str(k)) and v else redact(v, key=str(k)))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        text = redact_text(value)
        return text if len(text) <= EXCERPT else text[:EXCERPT] + f"... ({len(text)} chars)"
    return value


class AuditLedger:
    def __init__(self, *, max_sessions: int = 200, max_records: int = 500) -> None:
        self.max_sessions = max_sessions
        self.max_records = max_records
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.max_records > 0

    def record(self, session_id: str, kind: str, **data: Any) -> None:
        if not self.enabled or not session_id:
            return
        entry = {"time": time.time(), "kind": kind, **redact(data)}
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                info = {
                    "records": deque(maxlen=self.max_records),
                    "created": time.time(),
                    "dropped": 0,
                }
                self._sessions[session_id] = info
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            else:
                self._sessions.move_to_end(session_id)
            if len(info["records"]) == self.max_records:
                info["dropped"] += 1
            info["records"].append(entry)
            info["updated"] = entry["time"]

    def session(self, session_id: str) -> JSON | None:
        with self._lock:
            info = self._sessions.get(session_id)
            if info is None:
                return None
            return {
                "session_id": session_id,
                "created": info["created"],
                "updated": info.get("updated"),
                "dropped": info["dropped"],
                "records": list(info["records"]),
            }

    def sessions(self) -> list[JSON]:
        with self._lock:
            out = []
            for session_id, info in self._sessions.items():
                kinds: dict[str, int] = {}
                for entry in info["records"]:
                    kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
                out.append(
                    {
                        "session_id": session_id,
                        "created": info["created"],
                        "updated": info.get("updated"),
                        "records": len(info["records"]),
                        "kinds": kinds,
                    }
                )
            return out
