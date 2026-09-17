"""Kiro backend for the gateway: process/session management and turn execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from kiro_acp import __version__
from kiro_acp.acp import (
    ACPError,
    ACPRemoteError,
    ClientHandlers,
    KiroAgent,
    KiroLaunchOptions,
    LocalFileSystem,
    LocalTerminals,
    MetadataUpdate,
    PermissionPolicy,
    PermissionRule,
    PlanUpdate,
    Session,
    StopReason,
    TextDelta,
    ThoughtDelta,
    ToolCallEvent,
    TurnComplete,
)
from kiro_acp.acp.session import normalize_effort
from kiro_acp.acp.types import ModelInfo
from kiro_acp.gateway.activity import render_completed, render_plan, render_started
from kiro_acp.gateway.codex import CodexCatalogCache
from kiro_acp.gateway.config import Settings
from kiro_acp.gateway.conversation import JSON, Conversation, ToolCallPart, ToolDef
from kiro_acp.gateway.harness_agent import agent_config, ensure_harness_agent
from kiro_acp.gateway.inline_agent import custom_agent, harness_custom_agent
from kiro_acp.gateway.limits import StreamLimiter
from kiro_acp.gateway.mcp_servers import (
    McpServerError,
    discover,
    normalize_server,
    parse_catalogue,
)
from kiro_acp.gateway.mcp_servers import (
    signature as mcp_signature,
)
from kiro_acp.gateway.mcp_turn import END, BridgeCall, PendingTurn
from kiro_acp.gateway.metrics import Metrics
from kiro_acp.gateway.prompting import assistant_message, render_prompt
from kiro_acp.gateway.structured import strip_json_fences, validate_json_reply
from kiro_acp.gateway.toolbridge.broker import BridgeSession, ToolBridgeBroker
from kiro_acp.gateway.toolcalls import ToolCallParser
from kiro_acp.gateway.turn import (
    OutputDone,
    OutputEvent,
    OutputText,
    OutputThought,
    OutputToolCall,
    estimate_tokens,
)

LOG = logging.getLogger("kiro_acp.gateway.backend")


class GatewayError(Exception):
    """An error with an HTTP status and an API-style error type."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 400,
        error_type: str | None = None,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_type = error_type or ("invalid_request_error" if status < 500 else "api_error")
        self.code = code
        self.retry_after = retry_after

    @classmethod
    def from_kiro(cls, message: str) -> GatewayError:
        """Classify a Kiro-side failure so SDK retry logic behaves sensibly."""
        status, error_type, code, retry_after = classify_kiro_error(message)
        return cls(
            message, status=status, error_type=error_type, code=code, retry_after=retry_after
        )


# Ordered: the first matching pattern wins, so specific classes precede generic ones.
# (pattern, HTTP status, API error type, code, Retry-After seconds)
_ERROR_RULES: list[tuple[re.Pattern[str], int, str, str, int | None]] = [
    (re.compile(r"already in progress"), 409, "invalid_request_error", "session_busy", 2),
    (re.compile(r"invalid model id"), 400, "invalid_request_error", "invalid_model", None),
    (
        re.compile(
            r"not entitled|unentitled|not enabled for (?:your|this)|not available (?:to|for) your"
            r"|subscription does not include|not included in your"
        ),
        403,
        "permission_error",
        "model_not_entitled",
        None,
    ),
    (
        re.compile(
            r"(?:monthly|daily|weekly) (?:usage )?limit|usage limit|monthlylimiterror"
            r"|freetierlimitexceeded|limit has been reached"
        ),
        429,
        "rate_limit_error",
        "usage_limit",
        3600,
    ),
    (
        re.compile(
            r"throttl|rate.?limit|too many requests|quota|\b429\b|slow down"
            r"|toomanyrequestsexception|servicequotaexceededexception"
        ),
        429,
        "rate_limit_error",
        "rate_limited",
        30,
    ),
    (
        re.compile(
            r"the model .{0,80}is not available|temporarily unavailable|model (?:is )?unavailable"
            r"|model is not available"
        ),
        503,
        "overloaded_error",
        "model_unavailable",
        30,
    ),
    (
        re.compile(r"improperly formed request|malformed request|validationexception"),
        400,
        "invalid_request_error",
        "malformed_request",
        None,
    ),
    (
        re.compile(
            r"overloaded|capacity|unavailable|not available|\b503\b|\b529\b|temporarily"
            r"|internal server error|internal failure|dispatch failure"
            r"|failed to generate a response|try again"
        ),
        503,
        "overloaded_error",
        "kiro_unavailable",
        10,
    ),
    (re.compile(r"timed out|timeout|deadline"), 504, "api_error", "kiro_timeout", None),
    (
        re.compile(
            r"unauthorized|not logged in|not signed in|not authenticated|expired ?token"
            r"|authentication|accessdenied|invalid bearer|session (?:has )?expired"
            r"|login (?:has )?expired|(?:http|status)\s*(?:code\s*)?40[13]\b"
            r"|unrecognizedclientexception|invalidsignatureexception"
        ),
        502,
        "api_error",
        "kiro_auth",
        None,
    ),
    (
        re.compile(
            r"econnrefused|econnreset|econnaborted|ehostunreach|socket hang ?up|fetch failed"
            r"|connection reset|connection refused|broken pipe"
        ),
        502,
        "api_error",
        "kiro_connection",
        5,
    ),
]


def classify_kiro_error(message: str) -> tuple[int, str, str, int | None]:
    """Map Kiro error text to (status, error_type, code, retry_after).

    Classes, most specific first: ``session_busy`` (a prompt is already running on the
    session), ``invalid_model``, ``model_not_entitled``, ``usage_limit`` (plan quota),
    ``rate_limited``, ``model_unavailable`` (capacity for one model), ``malformed_request``,
    ``kiro_unavailable``, ``kiro_timeout``, ``kiro_auth``, ``kiro_connection``; anything
    else is ``kiro_error``.
    """
    lowered = message.lower()
    for pattern, status, error_type, code, retry_after in _ERROR_RULES:
        if pattern.search(lowered):
            return status, error_type, code, retry_after
    return 502, "api_error", "kiro_error", None


@dataclass(slots=True)
class TurnOptions:
    """Per-request knobs resolved by the protocol adapters."""

    model: str | None
    effort: str | None = None
    agent: str | None = None
    permissions: str | None = None
    emulate_tools: bool = False
    request_id: str = ""
    started: float = 0.0
    stop_sequences: list[str] = field(default_factory=list)
    max_tokens: int | None = None
    allow_retry: bool = False  # non-streaming requests may re-prompt to fix invalid JSON output
    workspace: str | None = None
    mcp_servers: list[Any] = field(default_factory=list)  # names (str) or raw definitions (dict)
    inline_agent: JSON | None = None  # validated kiro.agent object (v3 engine only)


@dataclass(eq=False)
class PooledSession:
    agent: KiroAgent
    session: Session
    model: str
    mode: str | None
    effort: str | None
    permissions: str
    workspace: str = ""
    bridge: BridgeSession | None = None
    pending: PendingTurn | None = None
    tools_signature: str = ""
    mcp_signature: str = ""
    fingerprint: str | None = None
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    created: float = field(default_factory=time.monotonic)

    async def close(self, *, delete: bool = False) -> None:
        if self.pending is not None:
            with contextlib.suppress(Exception):
                await self.pending.cancel("session closed")
            self.pending = None
        if delete:
            with contextlib.suppress(Exception):
                await self.agent.close_session(self.session, delete=True)
        with contextlib.suppress(Exception):
            await self.agent.close()


class KiroBackend:
    """Owns Kiro processes and turns conversations into output events."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._turn_slots = asyncio.Semaphore(settings.max_concurrency)
        self._pool: dict[str, PooledSession] = {}
        self._pool_lock = asyncio.Lock()
        self._models: list[ModelInfo] = []
        self._default_model: str | None = None
        self._models_at = 0.0
        self._models_lock = asyncio.Lock()
        self._reaper: asyncio.Task[None] | None = None
        self._warmup: asyncio.Task[None] | None = None
        self._harness_dir: str | None = None
        self.metrics = Metrics()
        self.codex_catalog = CodexCatalogCache()
        self.mcp_catalogue: dict[str, JSON] = {}
        self._discovered: dict[str, dict[str, JSON]] = {}
        self._active: set[PooledSession] = set()
        self.bridge_broker: ToolBridgeBroker | None = None
        self.started = False

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.settings.provision_harness_agent:
            for name, mcp in (
                (self.settings.harness_agent, False),
                (self.settings.harness_agent_mcp, True),
            ):
                if not name:
                    continue
                try:
                    ensure_harness_agent(name, mcp=mcp)
                except OSError as error:
                    LOG.warning("Could not provision harness agent %r: %s", name, error)
        try:
            self.mcp_catalogue = parse_catalogue(self.settings.mcp_servers)
        except McpServerError as error:
            raise RuntimeError(f"KIRO_GATEWAY_MCP_SERVERS: {error}") from error
        if self.mcp_catalogue:
            LOG.info("MCP catalogue: %s", ", ".join(sorted(self.mcp_catalogue)))
        for name in self.settings.mcp_servers_default:
            if name not in self.mcp_catalogue:
                raise RuntimeError(
                    f"KIRO_GATEWAY_MCP_SERVERS_DEFAULT names unknown server {name!r}"
                )
        if not self.settings.harness_workspace:
            # Harness turns: the client executes every tool, so Kiro's cwd only matters
            # for what it auto-loads (README, AGENTS.md, steering). Give it nothing.
            self._harness_dir = tempfile.mkdtemp(prefix="kiro-gateway-harness-")
        if self.settings.tool_mode == "mcp":
            self.bridge_broker = ToolBridgeBroker()
            await self.bridge_broker.start()
        self.started = True
        self._reaper = asyncio.create_task(self._reap_idle(), name="kiro-session-reaper")
        if self.settings.warmup:
            self._warmup = asyncio.create_task(self._warm_up(), name="kiro-warmup")

    async def _warm_up(self) -> None:
        try:
            models = await self.models()
            LOG.info(
                "Model catalogue loaded: %d models (default %s)", len(models), self._default_model
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            LOG.warning("Model warm-up failed: %s", error)

    async def stop(self) -> None:
        self.started = False
        active = list(self._active)
        if active:
            LOG.info("Shutting down: cancelling %d in-flight Kiro turn(s)", len(active))
            for pooled in active:
                with contextlib.suppress(Exception):
                    await pooled.session.cancel()
            deadline = time.monotonic() + self.settings.shutdown_grace
            while self._active and time.monotonic() < deadline:  # noqa: ASYNC110 - polling a set of turns
                await asyncio.sleep(0.1)
            for pooled in list(self._active):
                await pooled.close(delete=self.settings.delete_sessions)
            self._active.clear()
        if self._warmup is not None and not self._warmup.done():
            self._warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._warmup
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
        if self._harness_dir:
            shutil.rmtree(self._harness_dir, ignore_errors=True)
            self._harness_dir = None
        async with self._pool_lock:
            pooled = list(self._pool.values())
            self._pool.clear()
        await asyncio.gather(
            *(p.close(delete=self.settings.delete_sessions) for p in pooled), return_exceptions=True
        )
        if self.bridge_broker is not None:
            await self.bridge_broker.stop()
            self.bridge_broker = None

    async def _reap_idle(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, max(5.0, self.settings.session_idle_ttl / 4)))
            now = time.monotonic()
            expired: list[PooledSession] = []
            async with self._pool_lock:
                for key, pooled in list(self._pool.items()):
                    if not pooled.busy and now - pooled.last_used > self.settings.session_idle_ttl:
                        expired.append(self._pool.pop(key))
            for pooled in expired:
                LOG.info("Closing idle Kiro session %s", pooled.session.session_id)
                await pooled.close(delete=self.settings.delete_sessions)

    # ------------------------------------------------------------------ models

    async def models(self, *, force: bool = False) -> list[ModelInfo]:
        async with self._models_lock:
            fresh = time.monotonic() - self._models_at < self.settings.models_cache_ttl
            if self._models and fresh and not force:
                return self._models
            agent = self._make_agent(permissions="deny")
            try:
                await agent.start()
                info = await agent.discover(delete=self.settings.delete_sessions)
            finally:
                await agent.close()
            if not info.available_models and self._models:
                # A session racing a token refresh can answer with an empty or default
                # catalogue; that is "no evidence", not a new truth. Keep the snapshot.
                LOG.warning(
                    "Kiro advertised no models this time; keeping the previous catalogue of %d",
                    len(self._models),
                )
                self._models_at = time.monotonic()
                return self._models
            self._models = list(info.available_models)
            self._default_model = info.current_model_id
            self._models_at = time.monotonic() if self._models else 0.0
            if not self._models:
                LOG.warning(
                    "Kiro advertised no models; model names will be passed through unchanged"
                )
            return self._models

    async def default_model(self) -> str | None:
        if self.settings.default_model:
            return self.settings.default_model
        if self._default_model is None:
            await self.models()
        return self._default_model

    async def resolve_model(self, requested: str | None) -> str | None:
        """Map a client model name to a Kiro model id (``None`` = leave Kiro's default)."""
        models = await self.models()
        ids = [m.model_id for m in models]
        if not requested:
            return await self.default_model()
        if not ids:
            return (
                requested
                if requested.lower() not in ("kiro", "default", "auto", "claude-auto")
                else None
            )
        if requested in ids:
            return requested
        alias = self.settings.alias_for(requested)
        if alias and alias in ids:
            return alias
        normalized = normalize_model_name(requested)
        for candidate in ids:
            if normalize_model_name(candidate) == normalized:
                return candidate
        for candidate in ids:
            if normalized and normalize_model_name(candidate).startswith(normalized):
                return candidate
        if requested.lower() in ("kiro", "default", "auto", "claude-auto"):
            return await self.default_model()
        if self.settings.model_fallback:
            fallback = await self.default_model()
            LOG.info("Model %r not available; using %r", requested, fallback)
            return fallback
        raise GatewayError(
            f"Unknown model {requested!r}. Available: {', '.join(ids)}",
            status=404,
            error_type="not_found_error",
            code="model_not_found",
        )

    # ------------------------------------------------------------------ agents

    def _handlers(
        self,
        permissions: str,
        workspace: str | None = None,
        extra_rules: list[PermissionRule] | None = None,
    ) -> ClientHandlers:
        root = workspace or self.settings.workspace
        rules = list(extra_rules or []) + [
            PermissionRule.parse(rule) for rule in self.settings.permission_rules
        ]
        return ClientHandlers(
            permissions=PermissionPolicy(permissions, rules=rules),
            filesystem=LocalFileSystem(root) if self.settings.serve_fs else None,
            terminals=LocalTerminals(root) if self.settings.serve_terminal else None,
        )

    def _make_agent(
        self,
        *,
        permissions: str,
        model: str | None = None,
        mode: str | None = None,
        effort: str | None = None,
        engine: str | None = None,
        workspace: str | None = None,
        extra_rules: list[PermissionRule] | None = None,
    ) -> KiroAgent:
        options = KiroLaunchOptions(
            executable=self.settings.cli,
            engine=engine or self.settings.engine,
            model=model,
            effort=effort,
            agent=mode,
            trust_all_tools=permissions == "allow-all",
            verbose=1 if self.settings.debug_acp else 0,
        )
        cwd = workspace or self.settings.workspace
        return KiroAgent(
            options,
            cwd=cwd,
            handlers=self._handlers(permissions, cwd, extra_rules),
            client_name="kiro-gateway",
            client_version=__version__,
            request_timeout=120.0,
        )

    def _resolve_mcp_servers(self, opts: TurnOptions) -> None:
        """Turn request names/definitions into ACP ``mcpServers`` entries (agent mode only)."""
        requested = list(opts.mcp_servers)
        opts.mcp_servers = []
        if opts.emulate_tools:
            if requested:
                LOG.info("Ignoring mcp_servers on a harness request %s", opts.request_id)
            return
        available = dict(self.mcp_catalogue)
        discovered: dict[str, JSON] = {}
        if self.settings.mcp_discovery:
            workspace = self.workspace_for(opts)
            if workspace not in self._discovered:
                self._discovered[workspace] = discover(workspace)
            discovered = self._discovered[workspace]
            available.update(discovered)
        chosen: dict[str, JSON] = {}
        for name in self.settings.mcp_servers_default:
            chosen[name] = available[name]
        chosen.update(discovered)
        for item in requested:
            if isinstance(item, str):
                if item not in available:
                    raise GatewayError(
                        f"Unknown MCP server {item!r}; configured: "
                        f"{', '.join(sorted(available)) or 'none'}",
                        status=400,
                        code="unknown_mcp_server",
                    )
                chosen[item] = available[item]
            elif isinstance(item, dict):
                if not self.settings.allow_request_mcp_servers:
                    raise GatewayError(
                        "Inline MCP server definitions are disabled "
                        "(KIRO_GATEWAY_ALLOW_REQUEST_MCP_SERVERS); use a catalogue name",
                        status=403,
                        error_type="permission_error",
                        code="mcp_server_not_allowed",
                    )
                name = str(item.get("name") or "")
                try:
                    chosen[name] = normalize_server(
                        name, {k: v for k, v in item.items() if k != "name"}
                    )
                except McpServerError as error:
                    raise GatewayError(str(error), status=400, code="invalid_mcp_server") from error
            else:
                raise GatewayError(
                    "mcp_servers entries must be names or objects", code="invalid_mcp_server"
                )
        opts.mcp_servers = list(chosen.values())

    def _inline_agent_wire(self, opts: TurnOptions) -> JSON:
        assert opts.inline_agent is not None
        wire = custom_agent(opts.inline_agent)
        if opts.mcp_servers:
            refs = [f"@{s['name']}" for s in opts.mcp_servers]
            wire["tools"] = list(dict.fromkeys([*wire.get("tools", []), *refs]))
        return wire

    def workspace_for(self, opts: TurnOptions) -> str:
        """Kiro's cwd for a turn: the request's workspace, else the harness scratch
        directory for harness turns, else the configured workspace."""
        if opts.workspace:
            return opts.workspace
        if opts.emulate_tools:
            return self.settings.harness_workspace or self._harness_dir or self.settings.workspace
        return self.settings.workspace

    def engine_for(self, opts: TurnOptions) -> str:
        if opts.emulate_tools and self.settings.harness_engine:
            return self.settings.harness_engine
        return self.settings.engine

    async def _spawn(
        self, opts: TurnOptions, permissions: str, tools: list[ToolDef] | None = None
    ) -> PooledSession:
        engine = self.engine_for(opts)
        use_bridge = (
            bool(tools)
            and opts.emulate_tools
            and self.settings.tool_mode == "mcp"
            and self.bridge_broker is not None
        )
        bridge: BridgeSession | None = None
        mcp_servers: list[JSON] = []
        extra_rules: list[PermissionRule] = []
        if use_bridge:
            assert self.bridge_broker is not None and tools is not None
            bridge = self.bridge_broker.register([mcp_tool(t) for t in tools])
            mcp_servers = [self.bridge_broker.mcp_server_config(bridge)]
            # The bridged tools are executed by the harness, so let Kiro call them freely.
            extra_rules = [
                PermissionRule.parse("allow:title=*@harness/*"),
                PermissionRule.parse("allow:tool=@harness/*"),
            ]
        mcp_servers.extend(opts.mcp_servers)
        meta: JSON | None = None
        if engine == "v3":
            custom_agents: list[JSON] = []
            if opts.inline_agent is not None:
                custom_agents.append(self._inline_agent_wire(opts))
            elif opts.emulate_tools and opts.agent in (
                self.settings.harness_agent,
                self.settings.harness_agent_mcp,
            ):
                # No agent file needed on v3: send the harness agent over the wire.
                custom_agents.append(
                    harness_custom_agent(
                        agent_config(opts.agent, mcp=opts.agent == self.settings.harness_agent_mcp)
                    )
                )
            if custom_agents:
                meta = {"kiro": {"customAgents": custom_agents}}
        agent = self._make_agent(
            permissions=permissions,
            model=opts.model,
            mode=opts.agent,
            effort=opts.effort,
            engine=engine,
            workspace=self.workspace_for(opts),
            extra_rules=extra_rules,
        )
        try:
            await agent.start()
            session = await agent.new_session(
                model=opts.model,
                mode=opts.agent,
                effort=opts.effort,
                mcp_servers=mcp_servers,
                autopilot=(permissions == "allow-all") if engine == "v3" else None,
                meta=meta,
            )
        except ACPRemoteError as error:
            await agent.close()
            if bridge is not None:
                self.bridge_broker.unregister(bridge)
            if "Unknown" in str(error):
                raise GatewayError(str(error), status=400, code="kiro_error") from error
            raise GatewayError.from_kiro(str(error)) from error
        except ACPError as error:
            await agent.close()
            if bridge is not None:
                self.bridge_broker.unregister(bridge)
            raise GatewayError.from_kiro(str(error)) from error
        return PooledSession(
            agent=agent,
            session=session,
            model=opts.model,
            mode=opts.agent,
            effort=opts.effort,
            permissions=permissions,
            workspace=self.workspace_for(opts),
            bridge=bridge,
            tools_signature=tools_signature(tools) if use_bridge else "",
            mcp_signature=mcp_signature(opts.mcp_servers),
        )

    # ---------------------------------------------------------------- session selection

    async def _acquire(
        self, conversation: Conversation, opts: TurnOptions, permissions: str
    ) -> tuple[PooledSession, int, bool]:
        """Return ``(pooled, start_index, fresh)``."""
        signature = (
            tools_signature(conversation.tools)
            if opts.emulate_tools and self.settings.tool_mode == "mcp"
            else ""
        )
        """Return ``(pooled, start_index, fresh)``."""
        if self.settings.session_mode == "affinity":
            start = conversation.prefix_length_for_affinity()
            if start > 0:
                key = conversation.fingerprint(start)
                async with self._pool_lock:
                    pooled = self._pool.get(key)
                    if (
                        pooled is not None
                        and not pooled.busy
                        and pooled.model == opts.model
                        and pooled.mode == opts.agent
                        and pooled.permissions == permissions
                        and pooled.agent.engine == self.engine_for(opts)
                        and pooled.workspace == self.workspace_for(opts)
                        and pooled.tools_signature == signature
                        and pooled.mcp_signature == mcp_signature(opts.mcp_servers)
                        and (opts.effort is None or pooled.effort == opts.effort)
                        and pooled.agent.client.is_running
                    ):
                        pooled.busy = True
                        del self._pool[key]
                        LOG.debug(
                            "Reusing session %s for prefix %s", pooled.session.session_id, key[:12]
                        )
                        return pooled, start, False
        pooled = await self._spawn(opts, permissions, conversation.tools if signature else None)
        pooled.busy = True
        return pooled, 0, True

    async def _release(self, pooled: PooledSession, fingerprint: str | None, *, keep: bool) -> None:
        pooled.busy = False
        pooled.last_used = time.monotonic()
        if not keep or fingerprint is None or self.settings.session_mode != "affinity":
            await pooled.close(delete=self.settings.delete_sessions)
            if pooled.bridge is not None and self.bridge_broker is not None:
                self.bridge_broker.unregister(pooled.bridge)
            return
        pooled.fingerprint = fingerprint
        evicted: list[PooledSession] = []
        async with self._pool_lock:
            self._pool[fingerprint] = pooled
            while len(self._pool) > self.settings.max_sessions:
                oldest_key = min(self._pool, key=lambda k: self._pool[k].last_used)
                if oldest_key == fingerprint and len(self._pool) == 1:
                    break
                evicted.append(self._pool.pop(oldest_key))
        for item in evicted:
            await item.close(delete=self.settings.delete_sessions)

    # ------------------------------------------------------------------ turns

    def resolve_permissions(self, opts: TurnOptions) -> str:
        if opts.permissions:
            if not self.settings.allow_permission_override:
                raise GatewayError(
                    "Permission overrides are disabled (KIRO_GATEWAY_ALLOW_PERMISSION_OVERRIDE)",
                    status=403,
                    error_type="permission_error",
                )
            return opts.permissions
        if opts.emulate_tools:
            return self.settings.harness_permissions
        return self.settings.permissions

    async def run(
        self, conversation: Conversation, opts: TurnOptions
    ) -> AsyncIterator[OutputEvent]:
        """Execute one turn, yielding output events. Cancels Kiro if the consumer stops early."""
        if conversation.total_chars() > self.settings.max_prompt_chars:
            raise GatewayError("Prompt too large", status=413, code="prompt_too_large")
        check_image_sizes(conversation, self.settings.max_image_bytes)
        opts.started = time.monotonic()
        if opts.effort:
            try:
                opts.effort = normalize_effort(opts.effort)
            except ValueError as error:
                raise GatewayError(str(error), code="invalid_effort") from error
        permissions = self.resolve_permissions(opts)
        if opts.workspace:
            candidate = os.path.realpath(os.path.expanduser(opts.workspace))
            if not self.settings.allowed_workspaces and candidate != self.settings.workspace:
                raise GatewayError(
                    "Per-request workspaces are disabled (set KIRO_GATEWAY_ALLOWED_WORKSPACES)",
                    status=403,
                    error_type="permission_error",
                    code="workspace_not_allowed",
                )
            if not os.path.isdir(candidate):
                raise GatewayError(
                    f"Workspace is not a directory: {opts.workspace}",
                    status=400,
                    code="invalid_workspace",
                )
            if not self.settings.workspace_allowed(candidate):
                raise GatewayError(
                    f"Workspace {candidate} is not in KIRO_GATEWAY_ALLOWED_WORKSPACES",
                    status=403,
                    error_type="permission_error",
                    code="workspace_not_allowed",
                )
            opts.workspace = candidate
        self._resolve_mcp_servers(opts)
        if opts.inline_agent is not None:
            if not self.settings.allow_request_agents:
                raise GatewayError(
                    "Inline agent definitions are disabled (KIRO_GATEWAY_ALLOW_REQUEST_AGENTS)",
                    status=403,
                    error_type="permission_error",
                    code="agent_not_allowed",
                )
            if self.engine_for(opts) != "v3":
                raise GatewayError(
                    "Inline agent definitions need the v3 engine (KIRO_GATEWAY_ENGINE=v3)",
                    status=400,
                    code="agent_requires_v3",
                )
            opts.agent = self._inline_agent_wire(opts)["id"]
        if opts.agent is None:
            if opts.emulate_tools:
                opts.agent = (
                    self.settings.harness_agent_mcp
                    if self.settings.tool_mode == "mcp" and self.bridge_broker is not None
                    else self.settings.harness_agent
                )
            else:
                opts.agent = self.settings.agent
        try:
            await asyncio.wait_for(
                self._turn_slots.acquire(),
                self.settings.queue_timeout if self.settings.queue_timeout > 0 else None,
            )
        except TimeoutError as error:
            raise GatewayError(
                f"All {self.settings.max_concurrency} Kiro turn slots are busy; try again later",
                status=503,
                error_type="overloaded_error",
                code="busy",
                retry_after=max(1, int(self.settings.queue_timeout)),
            ) from error
        try:
            pooled, start, fresh = await self._acquire(conversation, opts, permissions)
            self._active.add(pooled)
            if (
                opts.emulate_tools
                and self.settings.tool_mode == "mcp"
                and pooled.bridge is not None
            ):
                async with contextlib.aclosing(
                    self._run_mcp(conversation, opts, pooled, start, fresh)
                ) as mcp_events:
                    async for event in mcp_events:
                        yield event
                return
            session = pooled.session
            LOG.info(
                "turn %s: %s engine=%s agent=%s model=%s permissions=%s session=%s%s",
                opts.request_id,
                "harness" if opts.emulate_tools else "agent",
                pooled.agent.engine,
                session.mode_id,
                session.model_id,
                permissions,
                session.session_id,
                " (reused)" if not fresh else "",
            )
            blocks = render_prompt(
                conversation,
                start=start,
                include_system=fresh,
                emulate_tools=opts.emulate_tools,
                sanitize=self.settings.sanitize_system,
                image_capable=image_capable(pooled.agent),
            )
            parser = ToolCallParser(enabled=opts.emulate_tools)
            limiter = StreamLimiter(
                stop_sequences=[seq for seq in opts.stop_sequences if seq],
                max_tokens=opts.max_tokens if self.settings.enforce_max_tokens else None,
            )
            text_parts: list[str] = []
            trailing_parts: list[str] = []
            thought_parts: list[str] = []
            calls: list[ToolCallPart] = []
            kiro_meta: JSON = {
                "session_id": session.session_id,
                "engine": pooled.agent.engine,
                "reused_session": not fresh,
                "agent": session.mode_id,
                "model": session.model_id,
                "workspace": pooled.workspace,
            }
            if session.effort_error:
                kiro_meta["effort_warning"] = session.effort_error
            if opts.mcp_servers:
                kiro_meta["mcp_servers"] = [s["name"] for s in opts.mcp_servers]
            finish = "stop"
            error: str | None = None
            completed = False
            fingerprint: str | None = None
            try:
                async with contextlib.aclosing(
                    session.prompt(blocks, timeout=self.settings.timeout)
                ) as turn:
                    async for event in turn:
                        match event:
                            case TextDelta(text=chunk):
                                if limiter.hit:
                                    continue
                                text, new_calls = parser.feed(chunk)
                                if text and limiter.active:
                                    text = limiter.feed(text)
                                if text and calls:
                                    # Text after a tool call is the model guessing at the
                                    # result; the harness will supply the real one.
                                    trailing_parts.append(text)
                                    text = ""
                                if text:
                                    text_parts.append(text)
                                    yield OutputText(text)
                                for parsed in new_calls:
                                    part = parsed.to_part()
                                    calls.append(part)
                                    yield OutputToolCall(part)
                                if limiter.hit:
                                    LOG.info(
                                        "Turn %s hit %s limit; cancelling Kiro",
                                        opts.request_id,
                                        limiter.hit,
                                    )
                                    await session.cancel()
                            case ThoughtDelta(text=chunk):
                                if self.settings.expose_thoughts:
                                    thought_parts.append(chunk)
                                    yield OutputThought(chunk)
                            case ToolCallEvent(call=call, phase=phase):
                                line = None
                                if phase == "started":
                                    line = render_started(
                                        call, detail=self.settings.tool_activity_detail
                                    )
                                elif phase == "completed":
                                    line = render_completed(
                                        call, detail=self.settings.tool_activity_detail
                                    )
                                if line and self.settings.tool_activity == "thought":
                                    thought_parts.append(line + "\n")
                                    yield OutputThought(line + "\n")
                                elif line and self.settings.tool_activity == "text":
                                    text_parts.append(line + "\n")
                                    yield OutputText(line + "\n")
                                if phase in ("started", "completed"):
                                    kiro_meta.setdefault("tool_calls", []).append(call.to_dict())
                            case PlanUpdate(entries=entries):
                                if entries and self.settings.tool_activity == "thought":
                                    rendered = render_plan(entries) + "\n"
                                    thought_parts.append(rendered)
                                    yield OutputThought(rendered)
                                kiro_meta["plan"] = [e.model_dump() for e in entries]
                            case MetadataUpdate(data=data):
                                merge_metadata(kiro_meta, data)
                            case TurnComplete(stop_reason=stop, error=turn_error):
                                completed = True
                                tail, tail_calls = parser.flush()
                                if limiter.active:
                                    tail = (limiter.feed(tail) if tail else "") + limiter.flush()
                                if tail and calls:
                                    trailing_parts.append(tail)
                                    tail = ""
                                if tail:
                                    text_parts.append(tail)
                                    yield OutputText(tail)
                                for parsed in tail_calls:
                                    part = parsed.to_part()
                                    calls.append(part)
                                    yield OutputToolCall(part)
                                finish, error = map_stop(stop, turn_error, bool(calls))
                                if detect_refusal(kiro_meta) and finish in ("stop", "tool_calls"):
                                    finish = "refusal"
                                if limiter.hit == "stop":
                                    finish, error = "stop", None
                                elif limiter.hit == "length":
                                    finish, error = "length", None
                text = "".join(text_parts)
                if calls:
                    text = text.rstrip()
                if trailing_parts:
                    kiro_meta["dropped_text_after_tool_calls"] = "".join(trailing_parts)
                    LOG.debug(
                        "Dropped %d chars of text after tool calls",
                        len(kiro_meta["dropped_text_after_tool_calls"]),
                    )
                thoughts = "".join(thought_parts)
                if (
                    conversation.json_output is not None
                    and not calls
                    and finish in ("stop", "length")
                ):
                    text = strip_json_fences(text)
                    valid, errors = validate_json_reply(text, conversation.json_output.schema)
                    if (
                        not valid
                        and opts.allow_retry
                        and self.settings.validate_json_output
                        and error is None
                    ):
                        LOG.info(
                            "Turn %s: JSON output invalid (%s); retrying once",
                            opts.request_id,
                            errors[0] if errors else "?",
                        )
                        text, valid, errors = await self._retry_json(
                            session, text, errors, conversation
                        )
                    kiro_meta["schema_valid"] = valid
                    if errors:
                        kiro_meta["schema_errors"] = errors[:5]
                usage = self._usage(conversation, text, thoughts, calls)
                fingerprint = None
                if completed and error is None and finish in ("stop", "tool_calls"):
                    fingerprint = conversation.fingerprint_after(assistant_message(text, calls))
                self._record_turn(opts, pooled, finish, kiro_meta, fresh)
                yield OutputDone(
                    finish=finish,
                    text=text,
                    thoughts=thoughts,
                    tool_calls=calls,
                    usage=usage,
                    kiro=kiro_meta,
                    error=error,
                    session_id=session.session_id,
                    stop_sequence=limiter.stop_sequence,
                )
            finally:
                if not completed:
                    LOG.info(
                        "Turn %s ended early; cancelling Kiro session %s",
                        opts.request_id,
                        session.session_id,
                    )
                    with contextlib.suppress(Exception):
                        await session.cancel()
                    await self._release(pooled, None, keep=False)
                elif limiter.hit:
                    # Kiro's history holds the untruncated reply; do not reuse this session.
                    await self._release(pooled, None, keep=False)
                else:
                    await self._release(
                        pooled, fingerprint if error is None else None, keep=error is None
                    )
                self._active.discard(pooled)
        finally:
            self._turn_slots.release()

    async def _run_mcp(
        self,
        conversation: Conversation,
        opts: TurnOptions,
        pooled: PooledSession,
        start: int,
        fresh: bool,
    ) -> AsyncIterator[OutputEvent]:
        """Harness turn with native tool calls through the MCP bridge."""
        session = pooled.session
        assert pooled.bridge is not None
        kiro_meta: JSON = {
            "session_id": session.session_id,
            "engine": pooled.agent.engine,
            "reused_session": not fresh,
            "agent": session.mode_id,
            "model": session.model_id,
            "workspace": pooled.workspace,
            "tool_mode": "mcp",
        }
        LOG.info(
            "turn %s: harness(mcp) engine=%s agent=%s model=%s session=%s%s",
            opts.request_id,
            pooled.agent.engine,
            session.mode_id,
            session.model_id,
            session.session_id,
            " (continuing pending turn)"
            if pooled.pending is not None
            else (" (reused)" if not fresh else ""),
        )
        pending = pooled.pending
        new_messages = conversation.messages[start:]
        if pending is not None:
            # Continuation: hand the client's tool results to the blocked MCP calls.
            results = [r for m in new_messages for r in m.tool_results]
            extra_text = "\n\n".join(
                m.text().strip() for m in new_messages if m.role == "user" and m.text().strip()
            )
            delivered = 0
            for result in results:
                content = result.content
                if extra_text and result is results[-1]:
                    content += f"\n\n[User message]: {extra_text}"
                if await pending.deliver(result.call_id, content, is_error=result.is_error):
                    delivered += 1
                else:
                    LOG.warning(
                        "Turn %s: result for unknown call id %s", opts.request_id, result.call_id
                    )
            if pending.awaiting and not delivered:
                # The client sent something else while calls were outstanding: give up on them.
                for call_id in list(pending.awaiting):
                    await pending.deliver(
                        call_id, "No result was provided for this tool call.", is_error=True
                    )
        else:
            blocks = render_prompt(
                conversation,
                start=start,
                include_system=fresh,
                emulate_tools=False,
                sanitize=self.settings.sanitize_system,
                image_capable=image_capable(pooled.agent),
            )
            pending = PendingTurn(session=session, bridge=pooled.bridge)
            pending.start(blocks, timeout=0)
            pooled.pending = pending
        text_parts: list[str] = []
        thought_parts: list[str] = []
        calls: list[ToolCallPart] = []
        pending_calls: list[BridgeCall] = []
        finish = "stop"
        error: str | None = None
        completed = False
        keep = False
        fingerprint: str | None = None
        try:
            batch_deadline: float | None = None
            while True:
                timeout = None
                if batch_deadline is not None:
                    timeout = max(batch_deadline - time.monotonic(), 0.0)
                elif self.settings.timeout > 0:
                    timeout = self.settings.timeout
                try:
                    item = await pending.next_event(timeout)
                except TimeoutError:
                    if batch_deadline is not None:
                        break  # batch window closed
                    error = f"Kiro produced no output for {self.settings.timeout:g}s"
                    finish = "error"
                    await pending.cancel("timeout")
                    break
                if item is END:
                    completed = True
                    break
                if isinstance(item, Exception):
                    finish, error = "error", str(item)
                    completed = True
                    break
                if isinstance(item, BridgeCall):
                    pending_calls.append(item)
                    if batch_deadline is None:
                        batch_deadline = time.monotonic() + self.settings.mcp_batch_window
                    continue
                match item:
                    case TextDelta(text=chunk):
                        # With native calls the model is blocked until results return, so any
                        # text seen now was produced before the call; keep it.
                        text_parts.append(chunk)
                        yield OutputText(chunk)
                    case ThoughtDelta(text=chunk):
                        if self.settings.expose_thoughts:
                            thought_parts.append(chunk)
                            yield OutputThought(chunk)
                    case ToolCallEvent(call=call, phase=phase):
                        if "@harness/" in (call.title or "") or (call.tool_name or "").startswith(
                            "@harness"
                        ):
                            continue
                        if phase in ("started", "completed"):
                            kiro_meta.setdefault("tool_calls", []).append(call.to_dict())
                    case MetadataUpdate(data=data):
                        merge_metadata(kiro_meta, data)
                    case TurnComplete(stop_reason=stop, error=turn_error):
                        finish, error = map_stop(stop, turn_error, False)
                        if detect_refusal(kiro_meta) and finish == "stop":
                            finish = "refusal"
                        completed = True
                        break
                    case _:
                        pass
            for bridge_call in pending_calls:
                part = ToolCallPart(
                    id=f"call_{bridge_call.call_id}",
                    name=bridge_call.name,
                    arguments=bridge_call.arguments,
                )
                pending.register_call(part.id, bridge_call.call_id, part)
                calls.append(part)
                yield OutputToolCall(part)
            if calls:
                finish = "tool_calls"
            text = "".join(text_parts).rstrip() if calls else "".join(text_parts)
            thoughts = "".join(thought_parts)
            usage = self._usage(conversation, text, thoughts, calls)
            if completed:
                pooled.pending = None
                keep = error is None
            else:
                keep = True  # turn still open, waiting for tool results
            fingerprint = None
            if keep and finish in ("stop", "tool_calls"):
                fingerprint = conversation.fingerprint_after(assistant_message(text, calls))
            self._record_turn(opts, pooled, finish, kiro_meta, fresh)
            yield OutputDone(
                finish=finish,
                text=text,
                thoughts=thoughts,
                tool_calls=calls,
                usage=usage,
                kiro=kiro_meta,
                error=error,
                session_id=session.session_id,
            )
            if not keep:
                pooled.pending = None
        finally:
            if pooled.pending is not None and not keep:
                await pooled.pending.cancel("request ended")
                pooled.pending = None
            if not completed and not keep:
                await self._release(pooled, None, keep=False)
            else:
                await self._release(pooled, fingerprint if keep else None, keep=keep)
            self._active.discard(pooled)

    async def _retry_json(
        self, session: Session, text: str, errors: list[str], conversation: Conversation
    ) -> tuple[str, bool, list[str]]:
        """Ask the same session to correct an invalid structured reply (non-streaming only)."""
        problem = "; ".join(errors[:3]) or "the reply was not valid JSON"
        schema = conversation.json_output.schema if conversation.json_output else None
        prompt = (
            "Your previous reply did not satisfy the required output format: "
            f"{problem}.\nReply again with only the corrected JSON value and nothing else"
            + (
                f", conforming to this JSON Schema:\n{json.dumps(schema, ensure_ascii=False)}"
                if schema
                else ""
            )
            + "."
        )
        result = await session.prompt_text(prompt, timeout=self.settings.timeout)
        if not result.ok:
            return text, False, errors
        corrected = strip_json_fences(result.text)
        valid, new_errors = validate_json_reply(corrected, schema)
        return (corrected, valid, new_errors) if valid else (text, False, errors)

    def _usage(
        self, conversation: Conversation, text: str, thoughts: str, calls: list[ToolCallPart]
    ) -> JSON:
        if not self.settings.usage_estimates:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "estimated": True,
            }
        prompt_tokens = estimate_tokens(conversation.system) + sum(
            estimate_tokens(m.text())
            + sum(estimate_tokens(json.dumps(c.arguments)) for c in m.tool_calls)
            + sum(estimate_tokens(r.content) for r in m.tool_results)
            for m in conversation.messages
        )
        completion_tokens = (
            estimate_tokens(text)
            + estimate_tokens(thoughts)
            + sum(estimate_tokens(json.dumps(c.arguments)) for c in calls)
        )
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "estimated": True,
        }

    def _record_turn(
        self, opts: TurnOptions, pooled: PooledSession, finish: str, kiro_meta: JSON, fresh: bool
    ) -> None:
        self.metrics.record_turn(
            mode="harness" if opts.emulate_tools else "agent",
            engine=pooled.agent.engine,
            model=pooled.session.model_id or opts.model or "default",
            finish=finish,
            seconds=time.monotonic() - opts.started if opts.started else 0.0,
            credits=float(kiro_meta.get("credits", 0.0) or 0.0),
            reused=not fresh,
        )

    def render_metrics(self) -> str:
        return self.metrics.render(
            active_turns=len(self._active),
            live_sessions=len(self._pool),
            models_cached=len(self._models),
        )

    def health(self) -> JSON:
        return {
            "status": "ok" if self.started else "starting",
            "backend": "kiro-cli-acp",
            "engine": self.settings.engine,
            "workspace": self.settings.workspace,
            "permissions": self.settings.permissions,
            "session_mode": self.settings.session_mode,
            "live_sessions": len(self._pool),
            "active_turns": len(self._active),
            "models_cached": len(self._models),
            "version": __version__,
        }


def mcp_tool(tool: ToolDef) -> JSON:
    return {
        "name": tool.name,
        "description": tool.description or tool.name,
        "inputSchema": tool.parameters or {"type": "object", "properties": {}},
    }


def tools_signature(tools: list[ToolDef] | None) -> str:
    import hashlib

    if not tools:
        return ""
    payload = json.dumps(
        sorted((t.name, t.description, json.dumps(t.parameters, sort_keys=True)) for t in tools)
    )
    return hashlib.sha1(payload.encode()).hexdigest()


def map_stop(stop: StopReason, error: str | None, has_calls: bool) -> tuple[str, str | None]:
    if error or stop == StopReason.ERROR:
        return "error", error or "Kiro turn failed"
    if stop == StopReason.CANCELLED:
        return "cancelled", None
    if stop == StopReason.REFUSAL:
        return "refusal", None
    if stop in (StopReason.MAX_TOKENS, StopReason.MAX_TURN_REQUESTS):
        return "length", None
    return ("tool_calls" if has_calls else "stop"), None


def image_capable(agent: KiroAgent) -> bool:
    """Whether the agent advertised image prompt input during ``initialize``."""
    result = agent.client.initialize_result
    if result is None:
        return True
    return bool(result.agent_capabilities.prompt_capabilities.image)


def check_image_sizes(conversation: Conversation, max_bytes: int) -> None:
    if max_bytes <= 0:
        return
    for message in conversation.messages:
        for image in message.images:
            decoded = len(image.data_base64) * 3 // 4
            if decoded > max_bytes:
                raise GatewayError(
                    f"Image input of about {decoded // 1024} KB exceeds the limit of "
                    f"{max_bytes // 1024} KB (KIRO_GATEWAY_MAX_IMAGE_BYTES)",
                    status=400,
                    error_type="invalid_request_error",
                    code="image_too_large",
                )


def detect_refusal(kiro_meta: JSON) -> bool:
    """Kiro flags a model/content-filter refusal in ``_kiro.dev/metadata``.

    ``stopReason: "CONTENT_FILTERED"`` and/or ``refusal: {category, explanation,
    recommendedModel}``. The refusal object is normalised into ``kiro_meta["refusal"]``
    (and ``recommended_model``) so clients can act on it; the finish reason becomes
    ``refusal`` (OpenAI ``content_filter`` / Anthropic ``refusal``) and is never retried.
    """
    refusal = kiro_meta.get("refusal")
    filtered = str(kiro_meta.get("stopReason") or "").upper() == "CONTENT_FILTERED"
    if not filtered and not isinstance(refusal, dict):
        return False
    if not isinstance(refusal, dict):
        refusal = {"category": "content_filtered"}
    kiro_meta["refusal"] = refusal
    recommended = refusal.get("recommendedModel") or refusal.get("recommended_model")
    if recommended:
        kiro_meta["recommended_model"] = recommended
    return True


def merge_metadata(target: JSON, data: JSON) -> None:
    for key, value in data.items():
        if key == "meteringUsage" and isinstance(value, list):
            target.setdefault("meteringUsage", []).extend(value)
            credits = sum(
                float(item.get("value", 0) or 0) for item in value if isinstance(item, dict)
            )
            target["credits"] = round(target.get("credits", 0.0) + credits, 6)
        else:
            target[key] = value


def normalize_model_name(name: str) -> str:
    """``claude-sonnet-4-5-20250929`` -> ``claude-sonnet-4.5``; ``claude-opus-4-8-latest`` -> ``claude-opus-4.8``."""
    import re

    value = name.strip().lower()
    # A "kiro-"/"kiro/" prefix lets clients such as Codex see a name outside their own
    # model catalogue (Codex switches to a lite wire format for names it recognizes).
    value = re.sub(r"^kiro[-/:]", "", value)
    value = re.sub(r"-(\d{8})$", "", value)
    value = re.sub(r"-(latest|preview)$", "", value)
    value = re.sub(r"@\d+$", "", value)
    value = re.sub(r"(?<=\d)-(?=\d)", ".", value)
    return value
