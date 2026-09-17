"""Client-side handlers for agent-to-client ACP requests.

An ACP client must answer ``session/request_permission`` and may optionally
implement the ``fs/*`` and ``terminal/*`` methods. This module provides:

* :class:`PermissionPolicy` – declarative, non-interactive (or callback-driven)
  permission decisions with per-tool-kind rules.
* :class:`LocalFileSystem` – ``fs/read_text_file`` / ``fs/write_text_file``
  confined to a workspace root.
* :class:`LocalTerminals` – ``terminal/*`` backed by local subprocesses.
* :class:`ClientHandlers` – bundles the above and computes the
  ``clientCapabilities`` to advertise during ``initialize``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_acp.acp.errors import ACPError
from kiro_acp.acp.types import (
    JSON,
    PermissionKind,
    PermissionRequest,
    cancelled,
    selected,
)

LOG = logging.getLogger("kiro_acp.acp.handlers")

PermissionCallback = Callable[[PermissionRequest], Awaitable[JSON]]

POLICY_NAMES = ("deny", "allow-once", "allow-always", "allow-all", "ask")


# Claude Code style tool patterns: Bash(git status*), Read(/etc/*), mcp__server__tool.
_TOOL_PATTERN_RE = re.compile(r"^(?P<name>[A-Za-z_][\w-]*)(?:\((?P<arg>.*)\))?$")
_CLAUDE_TOOL_KINDS: dict[str, tuple[str, ...]] = {
    "bash": ("execute",),
    "shell": ("execute",),
    "read": ("read",),
    "write": ("edit",),
    "edit": ("edit",),
    "multiedit": ("edit",),
    "notebookedit": ("edit",),
    "glob": ("search",),
    "grep": ("search",),
    "webfetch": ("fetch",),
    "websearch": ("fetch",),
}


def _request_subject(request: PermissionRequest) -> str:
    """The command or path a permission request is about, for Claude Code style patterns."""
    raw = request.raw_input if isinstance(request.raw_input, dict) else {}
    for key in ("command", "cmd"):
        if isinstance(raw.get(key), str):
            return raw[key]
    for key in ("path", "file_path", "filePath"):
        if isinstance(raw.get(key), str):
            return raw[key]
    locations = raw.get("locations") if isinstance(raw.get("locations"), list) else None
    if locations and isinstance(locations[0], dict) and locations[0].get("path"):
        return str(locations[0]["path"])
    title = request.title
    for prefix in ("Running: ", "Reading ", "Writing ", "Creating ", "Editing ", "Run Command: "):
        if title.startswith(prefix):
            return title[len(prefix) :]
    return title


@dataclass(slots=True)
class PermissionRule:
    """Match a permission request and force a decision.

    Two syntaxes are accepted by :meth:`parse`:

    * selectors — ``"allow:kind=read,search;tool=shell;title=Running: ls*"``. ``kinds``
      matches the ACP tool kind, ``tools`` Kiro's tool name (shell globs), ``titles`` the
      human title (shell globs). Empty selectors match everything.
    * Claude Code tool patterns — ``"allow:Bash(git status*)"``, ``"deny:Read(/etc/*)"``,
      ``"allow:Edit"``, ``"allow:mcp__server__tool"``. The tool name maps to ACP kinds
      (``Bash``→execute, ``Read``→read, ``Write``/``Edit``→edit, ``Glob``/``Grep``→search,
      ``WebFetch``→fetch); the parenthesised glob matches the command or path.
    """

    decision: str
    kinds: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    titles: tuple[str, ...] = ()
    subject: str | None = None
    mcp_tool: str | None = None

    def matches(self, request: PermissionRequest) -> bool:
        if self.kinds and request.kind.value not in self.kinds:
            return False
        if self.tools and not any(
            fnmatch.fnmatch(request.tool_name or "", pattern) for pattern in self.tools
        ):
            return False
        if self.titles and not any(
            fnmatch.fnmatch(request.title, pattern) for pattern in self.titles
        ):
            return False
        if self.mcp_tool is not None:
            raw_name = request.tool_name or ""
            name = raw_name.replace("/", "__").replace("@", "")
            if not name.startswith("mcp__"):
                name = "mcp__" + name
            if not (
                fnmatch.fnmatch(name, self.mcp_tool)
                or fnmatch.fnmatch(raw_name, self.mcp_tool)
                or fnmatch.fnmatch(request.title, self.mcp_tool)
            ):
                return False
        if self.subject is not None:
            subject = _request_subject(request)
            if not (
                fnmatch.fnmatch(subject, self.subject)
                or fnmatch.fnmatch(subject.strip(), self.subject)
            ):
                return False
        return True

    @classmethod
    def parse(cls, text: str) -> PermissionRule:
        decision, _, rest = text.strip().partition(":")
        decision = decision.strip().lower()
        if decision not in ("allow", "deny"):
            raise ValueError(f"Rule must start with allow: or deny: ({text!r})")
        rest = rest.strip()
        if "=" not in rest and rest:
            return cls._parse_tool_pattern(decision, rest, text)
        kinds: list[str] = []
        tools: list[str] = []
        titles: list[str] = []
        for selector in filter(None, (s.strip() for s in rest.split(";"))):
            key, _, values = selector.partition("=")
            items = [v.strip() for v in values.split(",") if v.strip()]
            match key.strip().lower():
                case "kind" | "kinds":
                    kinds.extend(items)
                case "tool" | "tools":
                    tools.extend(items)
                case "title" | "titles":
                    titles.extend(items)
                case _:
                    raise ValueError(f"Unknown rule selector {key!r} in {text!r}")
        return cls(decision=decision, kinds=tuple(kinds), tools=tuple(tools), titles=tuple(titles))

    @classmethod
    def _parse_tool_pattern(cls, decision: str, rest: str, text: str) -> PermissionRule:
        match = _TOOL_PATTERN_RE.match(rest)
        if not match:
            raise ValueError(f"Unrecognized permission rule {text!r}")
        name = match.group("name")
        arg = match.group("arg")
        if name.startswith("mcp__"):
            return cls(decision=decision, mcp_tool=name)
        kinds = _CLAUDE_TOOL_KINDS.get(name.lower())
        if kinds is None:
            raise ValueError(
                f"Unknown tool {name!r} in {text!r}; expected Bash, Read, Write, Edit, Glob, Grep, WebFetch, or mcp__server__tool"
            )
        return cls(decision=decision, kinds=kinds, subject=arg if arg not in (None, "") else None)


@dataclass
class PermissionPolicy:
    """Decide ``session/request_permission`` requests without a human in the loop.

    ``mode``:

    * ``deny`` – reject everything (answer ``reject_once`` if offered, else cancel).
    * ``allow-once`` – pick the ``allow_once`` option when offered.
    * ``allow-always`` – prefer ``allow_always``, fall back to ``allow_once``.
    * ``allow-all`` – like ``allow-always`` (use with ``--trust-all-tools`` to avoid prompts entirely).
    * ``ask`` – delegate to ``callback`` (interactive CLI); denies if no callback.

    ``rules`` are evaluated first, in order; the first matching rule wins.
    """

    mode: str = "deny"
    rules: list[PermissionRule] = field(default_factory=list)
    callback: PermissionCallback | None = None

    def __post_init__(self) -> None:
        if self.mode not in POLICY_NAMES:
            raise ValueError(
                f"Unknown permission mode {self.mode!r}; expected one of {POLICY_NAMES}"
            )

    async def decide(self, request: PermissionRequest) -> tuple[JSON, str]:
        for rule in self.rules:
            if rule.matches(request):
                if rule.decision == "allow":
                    return _allow(request, prefer_always=False), "rule:allow"
                return _deny(request), "rule:deny"
        if self.mode == "deny":
            return _deny(request), "policy:deny"
        if self.mode == "allow-once":
            return _allow(request, prefer_always=False), "policy:allow-once"
        if self.mode in ("allow-always", "allow-all"):
            return _allow(request, prefer_always=True), f"policy:{self.mode}"
        if self.callback is None:
            return _deny(request), "policy:ask-without-callback"
        return await self.callback(request), "policy:ask"


def _allow(request: PermissionRequest, *, prefer_always: bool) -> JSON:
    order = (
        (PermissionKind.ALLOW_ALWAYS, PermissionKind.ALLOW_ONCE)
        if prefer_always
        else (PermissionKind.ALLOW_ONCE, PermissionKind.ALLOW_ALWAYS)
    )
    option = request.option_of_kind(*order)
    if option is None:
        return cancelled()
    return selected(option.option_id)


def _deny(request: PermissionRequest) -> JSON:
    option = request.option_of_kind(PermissionKind.REJECT_ONCE)
    if option is None:
        return cancelled()
    return selected(option.option_id)


class LocalFileSystem:
    """Serve ``fs/read_text_file`` and ``fs/write_text_file`` from a workspace root."""

    def __init__(self, root: str | os.PathLike[str], *, allow_outside_root: bool = False) -> None:
        self.root = Path(root).resolve()
        self.allow_outside_root = allow_outside_root

    def _resolve(self, path: str) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        if (
            not self.allow_outside_root
            and self.root not in resolved.parents
            and resolved != self.root
        ):
            raise ACPError(f"Path {path!r} is outside the workspace root {self.root}")
        return resolved

    async def read_text_file(self, params: JSON) -> JSON:
        path = self._resolve(str(params.get("path", "")))
        text = await asyncio.to_thread(path.read_text, "utf-8", "replace")
        line = params.get("line")
        limit = params.get("limit")
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max(int(line or 1) - 1, 0)
            end = start + int(limit) if limit is not None else None
            text = "".join(lines[start:end])
        return {"content": text}

    async def write_text_file(self, params: JSON) -> JSON:
        path = self._resolve(str(params.get("path", "")))
        content = str(params.get("content", ""))

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        await asyncio.to_thread(_write)
        return {}


@dataclass(slots=True)
class _Terminal:
    process: asyncio.subprocess.Process
    buffer: bytearray
    limit: int | None
    truncated: bool
    reader: asyncio.Task[None]
    exit_code: int | None = None
    signal: str | None = None


class LocalTerminals:
    """Serve ``terminal/*`` requests with local subprocesses."""

    def __init__(self, cwd: str | os.PathLike[str]) -> None:
        self.cwd = str(Path(cwd).resolve())
        self._terminals: dict[str, _Terminal] = {}

    async def create(self, params: JSON) -> JSON:
        command = str(params.get("command", ""))
        if not command:
            raise ACPError("terminal/create requires a command")
        args = [str(a) for a in params.get("args", []) or []]
        env = dict(os.environ)
        for item in params.get("env", []) or []:
            if isinstance(item, dict) and item.get("name"):
                env[str(item["name"])] = str(item.get("value", ""))
        cwd = str(params.get("cwd") or self.cwd)
        limit = params.get("outputByteLimit")
        process = await asyncio.create_subprocess_exec(
            command,
            *args,
            cwd=cwd,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        terminal_id = uuid.uuid4().hex
        term = _Terminal(
            process=process,
            buffer=bytearray(),
            limit=int(limit) if limit is not None else None,
            truncated=False,
            reader=asyncio.create_task(self._pump(terminal_id, process)),
        )
        self._terminals[terminal_id] = term
        return {"terminalId": terminal_id}

    async def _pump(self, terminal_id: str, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None
        term = self._terminals[terminal_id]
        while chunk := await process.stdout.read(4096):
            term.buffer.extend(chunk)
            if term.limit is not None and len(term.buffer) > term.limit:
                overflow = len(term.buffer) - term.limit
                del term.buffer[:overflow]
                term.truncated = True
        code = await process.wait()
        if code < 0:
            term.signal = f"SIG{-code}"
            term.exit_code = None
        else:
            term.exit_code = code

    def _get(self, params: JSON) -> tuple[str, _Terminal]:
        terminal_id = str(params.get("terminalId", ""))
        term = self._terminals.get(terminal_id)
        if term is None:
            raise ACPError(f"Unknown terminalId {terminal_id!r}")
        return terminal_id, term

    async def output(self, params: JSON) -> JSON:
        _, term = self._get(params)
        result: JSON = {
            "output": term.buffer.decode("utf-8", errors="replace"),
            "truncated": term.truncated,
        }
        if term.process.returncode is not None:
            result["exitStatus"] = {"exitCode": term.exit_code, "signal": term.signal}
        return result

    async def wait_for_exit(self, params: JSON) -> JSON:
        _, term = self._get(params)
        await term.reader
        return {"exitCode": term.exit_code, "signal": term.signal}

    async def kill(self, params: JSON) -> JSON:
        _, term = self._get(params)
        if term.process.returncode is None:
            term.process.kill()
        return {}

    async def release(self, params: JSON) -> JSON:
        terminal_id, term = self._get(params)
        if term.process.returncode is None:
            term.process.kill()
        try:
            await asyncio.wait_for(asyncio.shield(term.reader), 5.0)
        except (TimeoutError, Exception):
            term.reader.cancel()
        transport = getattr(term.process, "_transport", None)
        if transport is not None:
            transport.close()
        self._terminals.pop(terminal_id, None)
        return {}

    async def close(self) -> None:
        for terminal_id in list(self._terminals):
            try:
                await self.release({"terminalId": terminal_id})
            except Exception:  # pragma: no cover - best effort cleanup
                LOG.debug("Failed to release terminal %s", terminal_id, exc_info=True)


@dataclass
class ClientHandlers:
    """Bundle of handlers the :class:`~kiro_acp.acp.client.ACPClient` dispatches to."""

    permissions: PermissionPolicy = field(default_factory=PermissionPolicy)
    filesystem: LocalFileSystem | None = None
    terminals: LocalTerminals | None = None
    extra: dict[str, Callable[[JSON], Awaitable[Any]]] = field(default_factory=dict)

    def capabilities(self) -> JSON:
        return {
            "fs": {
                "readTextFile": self.filesystem is not None,
                "writeTextFile": self.filesystem is not None,
            },
            "terminal": self.terminals is not None,
        }

    async def close(self) -> None:
        if self.terminals is not None:
            await self.terminals.close()
