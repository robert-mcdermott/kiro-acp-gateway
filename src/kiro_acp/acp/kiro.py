"""Kiro CLI specifics: launching ``kiro-cli acp`` and creating sessions.

Two agent engines exist in Kiro CLI 2.22+:

* ``v2`` (Rust "Kiro CLI Agent") – advertises models via a non-standard
  ``models`` field on ``session/new``, supports ``session/set_model``, and
  handles ``/effort`` slash commands sent as prompts.
* ``v3`` (KAS, "Kiro Agent Server") – spec-compliant ``configOptions`` for
  ``mode``, ``model`` and ``autopilot``; supports ``session/list``; requires
  ``--auth-method cli`` so it does not ask the client for access tokens; asks
  the client for ``_kiro/terminal/shell_type``; and uses the client's ``fs``/
  ``terminal`` capabilities when advertised.

:class:`KiroAgent` hides those differences behind one API.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from kiro_acp.acp.client import ACPClient
from kiro_acp.acp.errors import ACPError, ACPRemoteError
from kiro_acp.acp.handlers import ClientHandlers
from kiro_acp.acp.recorder import FrameRecorder
from kiro_acp.acp.session import EffortNotSupported, Session
from kiro_acp.acp.types import JSON, InitializeResult, ModelInfo, SessionInfo

LOG = logging.getLogger("kiro_acp.acp.kiro")

ENGINES = ("v3", "v2", "v1")
DEFAULT_ENGINE = "v3"


@dataclass(slots=True)
class KiroLaunchOptions:
    """Command-line options for ``kiro-cli acp``."""

    executable: str = "kiro-cli"
    engine: str = DEFAULT_ENGINE
    model: str | None = None
    effort: str | None = None
    agent: str | None = None
    trust_all_tools: bool = False
    trust_tools: Sequence[str] = ()
    verbose: int = 0
    extra_args: Sequence[str] = ()
    env: dict[str, str] = field(default_factory=dict)
    raw_command: Sequence[str] | None = None
    """Launch this exact argv instead of ``kiro-cli acp ...`` (any ACP agent, or a test double)."""

    def command(self) -> list[str]:
        if self.raw_command:
            return [str(part) for part in self.raw_command]
        argv = [self.executable, "acp", "--agent-engine", self.engine]
        v3 = self.engine == "v3"
        if v3:
            # Keep credential handling inside kiro-cli instead of `_kiro/auth/getAccessToken`.
            argv += ["--auth-method", "cli"]
        if self.model and not v3:
            # v3 rejects --model; KiroAgent selects it through the `model` config option.
            argv += ["--model", self.model]
        if self.effort and not v3:
            argv += ["--effort", self.effort]
        if self.agent and not v3:
            # v3 rejects --agent; KiroAgent applies it with session/set_mode.
            argv += ["--agent", self.agent]
        if self.trust_all_tools and not v3:
            # v3 rejects --trust-all-tools; KiroAgent turns the `autopilot` option on instead.
            argv.append("--trust-all-tools")
        if self.trust_tools:
            argv += ["--trust-tools", ",".join(self.trust_tools)]
        argv += ["-v"] * max(0, int(self.verbose))
        argv += list(self.extra_args)
        return argv

    def resolved_executable(self) -> str | None:
        return shutil.which(self.executable) if os.sep not in self.executable else self.executable


class KiroAgent:
    """A running ``kiro-cli acp`` process with engine-aware session helpers."""

    def __init__(
        self,
        options: KiroLaunchOptions | None = None,
        *,
        cwd: str | os.PathLike[str] | None = None,
        handlers: ClientHandlers | None = None,
        client_name: str = "kiro-acp",
        client_version: str = "0.1.0",
        request_timeout: float = 60.0,
        record_frames: str | None = None,
    ) -> None:
        self.options = options or KiroLaunchOptions()
        self.cwd = os.path.abspath(cwd or os.getcwd())
        record_dir = record_frames or os.environ.get("KIRO_ACP_RECORD_FRAMES")
        recorder = None
        if record_dir:
            recorder = FrameRecorder(
                record_dir,
                meta={
                    "client": f"{client_name} {client_version}",
                    "command": self.options.command(),
                    "engine": self.options.engine,
                    "model": self.options.model,
                    "cwd": self.cwd,
                },
            )
        env = dict(os.environ)
        env.update(self.options.env)
        # Marker for orphan detection (``kiro-acp doctor``): the pid that spawned this agent.
        env.setdefault("KIRO_ACP_PARENT_PID", str(os.getpid()))
        self.client = ACPClient(
            self.options.command(),
            cwd=self.cwd,
            env=env,
            handlers=handlers,
            client_name=client_name,
            client_version=client_version,
            request_timeout=request_timeout,
            recorder=recorder,
        )
        self.sessions: dict[str, Session] = {}
        self.model_list_timeout = 20.0

    # ------------------------------------------------------------------ lifecycle

    async def start(self, *, initialize_timeout: float = 60.0) -> InitializeResult:
        await self.client.start()
        result = await self.client.initialize(timeout=initialize_timeout)
        if result.agent_info.name == "unknown" and self.engine == "v3":
            result.agent_info.name = "Kiro Agent Server"
        return result

    async def close(self) -> None:
        await self.client.close()

    async def __aenter__(self) -> KiroAgent:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def info(self) -> InitializeResult:
        if self.client.initialize_result is None:
            raise ACPError("agent not initialized")
        return self.client.initialize_result

    @property
    def engine(self) -> str:
        return self.options.engine

    @property
    def supports_session_list(self) -> bool:
        return "list" in (self.info.agent_capabilities.session_capabilities or {})

    # ------------------------------------------------------------------ sessions

    async def new_session(
        self,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: list[JSON] | None = None,
        model: str | None = None,
        mode: str | None = None,
        effort: str | None = None,
        autopilot: bool | None = None,
        timeout: float = 120.0,
        meta: JSON | None = None,
        _attempt: int = 0,
    ) -> Session:
        """Create a session and apply model/mode/effort/autopilot settings.

        Settings passed at launch (``KiroLaunchOptions.model`` etc.) already
        apply to the first session; explicit arguments here override them.
        On the v3 engine a cold start can answer ``session/new`` before the
        model catalogue is loaded; the session then waits for the
        ``config_option_update`` and, failing that, is replaced by a fresh one.
        """
        session_cwd = os.path.abspath(cwd or self.cwd)
        params: JSON = {"cwd": session_cwd, "mcpServers": mcp_servers or []}
        if meta:
            # v3: ``_meta.kiro.customAgents`` registers inline agents as selectable modes.
            params["_meta"] = meta
        result = await self.client.request("session/new", params, timeout=timeout)
        if not isinstance(result, dict) or not result.get("sessionId"):
            raise ACPError(f"session/new returned no sessionId: {result!r}")
        session = Session(
            self.client, SessionInfo.from_result(result), cwd=session_cwd, engine=self.engine
        )
        self.sessions[session.session_id] = session
        if self.engine == "v3" and not session.info.model_ids:
            ready = await session.wait_for(
                lambda info: bool(info.model_ids), timeout=self.model_list_timeout
            )
            if not ready:
                if _attempt < 2:
                    LOG.info(
                        "Session %s advertised no models; creating a fresh session",
                        session.session_id,
                    )
                    await self.close_session(session, delete=True)
                    return await self.new_session(
                        cwd=cwd,
                        mcp_servers=mcp_servers,
                        model=model,
                        mode=mode,
                        effort=effort,
                        autopilot=autopilot,
                        timeout=timeout,
                        meta=meta,
                        _attempt=_attempt + 1,
                    )
                LOG.warning(
                    "Session %s advertised no models within %.0fs",
                    session.session_id,
                    self.model_list_timeout,
                )
        if self.engine == "v3":
            model = model or self.options.model
            mode = mode or self.options.agent
            effort = effort or self.options.effort
        else:
            session.effort = self.options.effort
        if (
            autopilot is None
            and self.options.trust_all_tools
            and session.info.config_option("autopilot")
        ):
            autopilot = True
        await self._configure(session, model=model, mode=mode, effort=effort, autopilot=autopilot)
        return session

    async def load_session(
        self,
        session_id: str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        mcp_servers: list[JSON] | None = None,
        timeout: float = 120.0,
    ) -> Session:
        """Resume an existing session (``session/load``); history is replayed as notifications."""
        if not self.info.agent_capabilities.load_session:
            raise ACPError("agent does not support session/load")
        session_cwd = os.path.abspath(cwd or self.cwd)
        result = await self.client.request(
            "session/load",
            {"sessionId": session_id, "cwd": session_cwd, "mcpServers": mcp_servers or []},
            timeout=timeout,
        )
        info = SessionInfo.from_result(
            result if isinstance(result, dict) else {}, session_id=session_id
        )
        session = Session(self.client, info, cwd=session_cwd, engine=self.engine)
        self.sessions[session_id] = session
        session.drain()
        return session

    async def list_sessions(self, *, cwd: str | None = None) -> list[JSON]:
        params: JSON = {}
        if cwd:
            params["cwd"] = os.path.abspath(cwd)
        result = await self.client.request("session/list", params)
        return list((result or {}).get("sessions", []))

    @property
    def supports_session_delete(self) -> bool:
        caps = self.info.agent_capabilities.session_capabilities or {}
        return "delete" in caps or self.engine == "v3"

    async def delete_session(self, session_id: str) -> bool:
        """Delete a stored session.

        Uses the ACP ``session/delete`` method when advertised; otherwise the Kiro v3
        engine's undocumented ``_kiro/session/delete`` extension. Returns ``False`` when
        the agent has no way to delete sessions.
        """
        caps = self.info.agent_capabilities.session_capabilities or {}
        methods = ["session/delete"] if "delete" in caps else []
        if self.engine == "v3":
            methods.append("_kiro/session/delete")
        for method in methods:
            try:
                await self.client.request(method, {"sessionId": session_id}, timeout=30)
                self.sessions.pop(session_id, None)
                return True
            except ACPRemoteError as error:
                if not error.is_method_not_found:
                    raise
        return False

    async def close_session(self, session: Session, *, delete: bool = False) -> None:
        self.sessions.pop(session.session_id, None)
        session.close()
        if "close" in (self.info.agent_capabilities.session_capabilities or {}):
            try:
                await self.client.request(
                    "session/close", {"sessionId": session.session_id}, timeout=10
                )
            except ACPRemoteError:
                LOG.debug("session/close rejected", exc_info=True)
        self.client.forget_tool_calls(session.session_id)
        if delete:
            try:
                await self.delete_session(session.session_id)
            except ACPRemoteError:
                LOG.debug("Could not delete session %s", session.session_id, exc_info=True)

    async def _configure(
        self,
        session: Session,
        *,
        model: str | None,
        mode: str | None,
        effort: str | None,
        autopilot: bool | None,
    ) -> None:
        if mode and mode != session.mode_id:
            await session.set_mode(mode)
        if model and model != session.model_id:
            await session.set_model(model)
        if autopilot is not None:
            option = session.info.config_option("autopilot")
            if option is not None:
                await session.set_config_option(option.id, "on" if autopilot else "off")
        if effort and effort != session.effort:
            try:
                await session.set_effort(effort)
            except EffortNotSupported as error:
                session.effort_error = str(error)
                LOG.warning("Effort %r not applied: %s", effort, error)

    # ------------------------------------------------------------------ discovery

    async def discover(
        self, *, cwd: str | None = None, attempts: int = 3, delete: bool = True
    ) -> SessionInfo:
        """Create a throwaway session to learn available models and modes.

        The v3 engine occasionally answers the first ``session/new`` after a cold
        start before its model catalogue is loaded, so this retries with fresh
        sessions until models are advertised or ``attempts`` are exhausted.
        """
        import asyncio

        info: SessionInfo | None = None
        for attempt in range(1, max(1, attempts) + 1):
            session = await self.new_session(cwd=cwd)
            info = session.info
            await self.close_session(session, delete=delete)
            if info.available_models or attempt == attempts:
                break
            LOG.info("No models advertised on attempt %d; retrying discovery", attempt)
            await asyncio.sleep(1.0 * attempt)
        assert info is not None
        return info

    @staticmethod
    def models_of(info: SessionInfo) -> list[ModelInfo]:
        return list(info.available_models)


def describe_capabilities(info: InitializeResult) -> dict[str, Any]:
    caps = info.agent_capabilities
    return {
        "agent": {"name": info.agent_info.name, "version": info.agent_info.version},
        "protocolVersion": info.protocol_version,
        "loadSession": caps.load_session,
        "prompt": caps.prompt_capabilities.model_dump(by_alias=True),
        "mcp": caps.mcp_capabilities.model_dump(by_alias=True),
        "sessionCapabilities": sorted(caps.session_capabilities or {}),
        "authMethods": [m.get("id") for m in info.auth_methods if isinstance(m, dict)],
    }
