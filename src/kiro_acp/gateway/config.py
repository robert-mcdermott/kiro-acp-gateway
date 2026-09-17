"""Gateway configuration (environment variables prefixed ``KIRO_GATEWAY_``).

The prefix deliberately differs from ``KIRO_*``: variables such as ``KIRO_API_KEY``
are read by ``kiro-cli`` itself and must not be shadowed by gateway settings.
"""

from __future__ import annotations

import fnmatch
import json
import os
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from kiro_acp.acp.handlers import POLICY_NAMES
from kiro_acp.acp.kiro import DEFAULT_ENGINE

PermissionMode = Literal["deny", "allow-once", "allow-always", "allow-all"]


class Settings(BaseSettings):
    """All gateway knobs. Every field maps to ``KIRO_GATEWAY_<UPPER_NAME>``."""

    model_config = SettingsConfigDict(env_prefix="KIRO_GATEWAY_", env_file=".env", extra="ignore")

    # --- Kiro process -------------------------------------------------------
    cli: str = Field(default="kiro-cli", description="kiro-cli executable")
    engine: str = Field(default=DEFAULT_ENGINE, description="Kiro agent engine: v3 or v2")
    workspace: str = Field(default_factory=os.getcwd, description="Directory Kiro operates in")
    allowed_workspaces: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Glob patterns of directories a request may select with X-Kiro-Workspace (empty disables per-request workspaces)",
    )
    agent: str | None = Field(default=None, description="Default Kiro agent (ACP mode) id")
    effort: str | None = Field(default=None, description="Default reasoning effort")
    default_model: str | None = Field(
        default=None, description="Model when the request omits or mis-names one"
    )
    model_fallback: bool = Field(
        default=True, description="Map unknown model names to the default model instead of erroring"
    )
    model_aliases: Annotated[dict[str, str], NoDecode] = Field(
        default_factory=dict,
        description="Extra alias -> Kiro model mappings; keys may be shell globs (e.g. 'gpt-4*': 'gpt-5.6-terra')",
    )
    models_cache_ttl: float = Field(default=600.0, description="Seconds to cache the model list")
    model_alias_style: Literal["both", "native"] = Field(
        default="both",
        description="'both' also lists hyphenated ids (claude-sonnet-4-6) and claude-auto/auto for Claude Code; 'native' lists Kiro ids only",
    )

    # --- permissions ---------------------------------------------------------
    permissions: PermissionMode = Field(
        default="deny", description="Policy for Kiro's own tool permission requests"
    )
    permission_rules: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="Ordered rules like 'allow:kind=read,search'"
    )
    harness_permissions: PermissionMode = Field(
        default="deny", description="Policy while emulating client-defined tools (harness mode)"
    )
    harness_engine: str | None = Field(
        default="v2",
        description="Kiro engine for harness-mode turns (client-defined tools); empty = same as engine. "
        "v2 follows the emulated tool protocol far more reliably than v3.",
    )
    harness_workspace: str = Field(
        default="",
        description="Directory Kiro runs in for harness turns (client-defined tools). Empty = a fresh empty scratch "
        "directory, so Kiro does not load the gateway workspace's README/AGENTS/steering files into harness prompts",
    )
    harness_agent: str | None = Field(
        default="kiro-gateway-harness",
        description="Tool-less Kiro agent selected while emulating client tools (empty = keep the default agent)",
    )
    provision_harness_agent: bool = Field(
        default=True,
        description="Create ~/.kiro/agents/<harness_agent>.json at startup when missing",
    )
    allow_permission_override: bool = Field(
        default=False, description="Honour the X-Kiro-Permissions request header"
    )
    serve_fs: bool = Field(
        default=False, description="Advertise fs/read_text_file + fs/write_text_file"
    )
    serve_terminal: bool = Field(default=False, description="Advertise terminal/* to the agent")

    # --- HTTP ----------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 8000
    api_key: str = Field(
        default="", description="Bearer / x-api-key required by API routes when set"
    )
    api_keys: Annotated[list[str], NoDecode] = Field(
        default_factory=list, description="Additional accepted API keys"
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "info"
    debug_acp: bool = Field(default=False, description="Log raw ACP traffic")

    # --- streaming -----------------------------------------------------------
    sse_keepalive: float = Field(
        default=15.0,
        description="Seconds of silence before an SSE keepalive (Anthropic ping / OpenAI comment) is sent; 0 disables",
    )
    warmup: bool = Field(
        default=True, description="Load the model catalogue in the background at startup"
    )
    enforce_max_tokens: bool = Field(
        default=False,
        description="Cut off output at the request's max_tokens using the token estimator (Kiro itself ignores it)",
    )

    # --- execution -----------------------------------------------------------
    max_concurrency: int = Field(default=4, ge=1, description="Max simultaneous Kiro turns")
    timeout: float = Field(default=900.0, description="Max seconds for one turn")
    queue_timeout: float = Field(
        default=60.0,
        description="Seconds a request waits for a free turn slot before 503 (0 = wait forever)",
    )
    rate_limit_rpm: int = Field(
        default=0,
        ge=0,
        description="Requests per minute per API key (or client address); 0 disables",
    )
    shutdown_grace: float = Field(
        default=10.0, description="Seconds to wait for in-flight turns to cancel during shutdown"
    )
    session_mode: Literal["affinity", "stateless"] = Field(
        default="affinity",
        description="affinity: reuse a Kiro session when the conversation prefix matches; stateless: fresh session per request",
    )
    session_idle_ttl: float = Field(
        default=600.0, description="Seconds an idle affinity session is kept"
    )
    max_sessions: int = Field(
        default=8, ge=1, description="Max live Kiro processes kept for affinity"
    )
    delete_sessions: bool = Field(
        default=True,
        description="Delete Kiro's stored copy of gateway sessions when they are closed (v3 engine)",
    )

    # --- translation ---------------------------------------------------------
    tool_mode: Literal["mcp", "emulate", "reject", "ignore"] = Field(
        default="mcp",
        description="How client-defined tools are handled: mcp (native calls via a bridged MCP server), "
        "emulate (tagged-block prompting), reject with 400, or ignore",
    )
    harness_agent_mcp: str | None = Field(
        default="kiro-gateway-harness-mcp",
        description="Kiro agent used in mcp tool mode (tools: ['@harness']); provisioned alongside the tool-less agent",
    )
    mcp_batch_window: float = Field(
        default=0.5,
        description="Seconds to wait for additional parallel tool calls before answering the client",
    )
    tool_activity: Literal["none", "text", "thought"] = Field(
        default="thought",
        description="How Kiro's own tool activity is surfaced: hidden, inline text, or as reasoning/thinking",
    )
    tool_activity_detail: Literal["brief", "full"] = Field(
        default="full",
        description="brief: one line per tool call; full: arguments, diffs, and output excerpts",
    )
    expose_thoughts: bool = Field(
        default=True, description="Forward agent thought chunks as reasoning/thinking"
    )
    sanitize_system: bool = Field(
        default=False,
        description="Strip identity/concealment lines from client system prompts before rendering (defensive; off by default)",
    )
    usage_estimates: bool = Field(
        default=True, description="Report estimated token usage (Kiro reports credits, not tokens)"
    )
    max_prompt_chars: int = Field(
        default=2_000_000, description="Reject prompts larger than this many characters"
    )
    max_image_bytes: int = Field(
        default=5 * 1024 * 1024,
        ge=0,
        description="Reject image inputs larger than this many decoded bytes (an oversized image can wedge a Kiro session); 0 disables",
    )
    validate_json_output: bool = Field(
        default=True,
        description="Validate structured-output replies against the requested JSON schema and retry once (non-streaming)",
    )

    @field_validator("permissions", "harness_permissions", mode="before")
    @classmethod
    def _normalize_policy(cls, value: str) -> str:
        value = str(value).strip().lower().replace("_", "-")
        if value not in POLICY_NAMES or value == "ask":
            raise ValueError(
                f"permission policy must be one of deny, allow-once, allow-always, allow-all (got {value!r})"
            )
        return value

    @field_validator("engine", "harness_engine", mode="before")
    @classmethod
    def _normalize_engine(cls, value: str | None) -> str | None:
        if value is None or str(value).strip() == "":
            return None
        value = str(value).strip().lower()
        if value not in ("v1", "v2", "v3"):
            raise ValueError("engine must be v3, v2, or v1")
        return value

    @field_validator("workspace")
    @classmethod
    def _abs_workspace(cls, value: str) -> str:
        return os.path.realpath(os.path.expanduser(value))

    @field_validator("model_aliases", mode="before")
    @classmethod
    def _parse_aliases(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("{"):
                return json.loads(text)
            pairs = {}
            for item in text.split(","):
                if "=" in item:
                    key, _, target = item.partition("=")
                    pairs[key.strip()] = target.strip()
            return pairs
        return value

    @field_validator(
        "permission_rules", "api_keys", "cors_origins", "allowed_workspaces", mode="before"
    )
    @classmethod
    def _parse_list(cls, value: object) -> object:
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                return json.loads(text)
            return [
                item.strip() for item in text.split(";" if ";" in text else ",") if item.strip()
            ]
        return value

    def accepted_keys(self) -> set[str]:
        keys = set(self.api_keys)
        if self.api_key:
            keys.add(self.api_key)
        return keys

    def workspace_allowed(self, path: str) -> bool:
        real = os.path.realpath(os.path.expanduser(path))
        if real == self.workspace:
            return True
        for pattern in self.allowed_workspaces:
            expanded = os.path.expanduser(pattern)
            if fnmatch.fnmatchcase(real, expanded) or fnmatch.fnmatchcase(real + "/", expanded):
                return True
            if expanded.endswith("/**") and (real + "/").startswith(expanded[:-2]):
                return True
        return False

    def alias_for(self, model: str) -> str | None:
        for pattern, target in self.model_aliases.items():
            if fnmatch.fnmatchcase(model, pattern) or fnmatch.fnmatchcase(
                model.lower(), pattern.lower()
            ):
                return target
        return None
