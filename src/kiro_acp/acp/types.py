"""Typed representations of ACP protocol structures.

Models are intentionally lenient (``extra="allow"``) because agents such as Kiro
extend the protocol with vendor fields (``_meta``, ``models`` on ``session/new``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

JSON = dict[str, Any]

PROTOCOL_VERSION = 1


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"
    # Not part of ACP: used by this client when the agent errors mid-turn.
    ERROR = "error"

    @classmethod
    def parse(cls, value: Any) -> StopReason:
        try:
            return cls(str(value))
        except ValueError:
            return cls.END_TURN


class ToolKind(StrEnum):
    READ = "read"
    EDIT = "edit"
    DELETE = "delete"
    MOVE = "move"
    SEARCH = "search"
    EXECUTE = "execute"
    THINK = "think"
    FETCH = "fetch"
    SWITCH_MODE = "switch_mode"
    OTHER = "other"

    @classmethod
    def parse(cls, value: Any) -> ToolKind:
        try:
            return cls(str(value))
        except ValueError:
            return cls.OTHER


class ToolCallStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"

    @classmethod
    def parse(cls, value: Any) -> ToolCallStatus:
        try:
            return cls(str(value))
        except ValueError:
            return cls.PENDING


class PermissionKind(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALLOW_ALWAYS = "allow_always"
    REJECT_ONCE = "reject_once"
    REJECT_ALWAYS = "reject_always"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: Any) -> PermissionKind:
        normalized = str(value or "").lower().replace("-", "_")
        try:
            return cls(normalized)
        except ValueError:
            return cls.UNKNOWN


class AgentInfo(_Lenient):
    name: str = "unknown"
    title: str | None = None
    version: str = "unknown"


class PromptCapabilities(_Lenient):
    image: bool = False
    audio: bool = False
    embedded_context: bool = Field(default=False, alias="embeddedContext")


class McpCapabilities(_Lenient):
    http: bool = False
    sse: bool = False


class AgentCapabilities(_Lenient):
    load_session: bool = Field(default=False, alias="loadSession")
    prompt_capabilities: PromptCapabilities = Field(
        default_factory=PromptCapabilities, alias="promptCapabilities"
    )
    mcp_capabilities: McpCapabilities = Field(
        default_factory=McpCapabilities, alias="mcpCapabilities"
    )
    session_capabilities: JSON = Field(default_factory=dict, alias="sessionCapabilities")


class InitializeResult(_Lenient):
    protocol_version: int = Field(default=PROTOCOL_VERSION, alias="protocolVersion")
    agent_capabilities: AgentCapabilities = Field(
        default_factory=AgentCapabilities, alias="agentCapabilities"
    )
    agent_info: AgentInfo = Field(default_factory=AgentInfo, alias="agentInfo")
    auth_methods: list[JSON] = Field(default_factory=list, alias="authMethods")


class ModelInfo(_Lenient):
    model_id: str = Field(alias="modelId")
    name: str | None = None
    description: str | None = None


class ModeInfo(_Lenient):
    id: str
    name: str | None = None
    description: str | None = None


class ConfigOption(_Lenient):
    """ACP ``SessionConfigOption`` (v3 engine exposes model/mode/autopilot this way)."""

    id: str
    name: str | None = None
    category: str | None = None
    type: str = "select"
    current_value: Any = Field(default=None, alias="currentValue")
    options: list[JSON] = Field(default_factory=list)

    @property
    def values(self) -> list[str]:
        return [str(o.get("value")) for o in self.options if isinstance(o, dict) and "value" in o]


class SessionInfo(_Lenient):
    """Result of ``session/new`` / ``session/load``.

    Normalizes both Kiro engines: the v2 engine reports models through a
    non-standard ``models`` field, the v3 engine through ACP ``configOptions``
    with ``category: "model"``. Modes (Kiro agents) come from ``modes`` and/or
    the ``mode`` config option.
    """

    session_id: str = Field(alias="sessionId")
    current_model_id: str | None = None
    available_models: list[ModelInfo] = Field(default_factory=list)
    current_mode_id: str | None = None
    available_modes: list[ModeInfo] = Field(default_factory=list)
    config_options: list[ConfigOption] = Field(default_factory=list)
    meta: JSON = Field(default_factory=dict)

    @classmethod
    def from_result(cls, result: JSON, *, session_id: str | None = None) -> SessionInfo:
        models = result.get("models") or {}
        modes = result.get("modes") or {}
        config_options = [
            ConfigOption.model_validate(item)
            for item in result.get("configOptions") or []
            if isinstance(item, dict) and item.get("id")
        ]
        info = cls(
            sessionId=session_id or str(result.get("sessionId", "")),
            current_model_id=models.get("currentModelId"),
            available_models=[
                ModelInfo.model_validate(item)
                for item in models.get("availableModels", [])
                if isinstance(item, dict) and item.get("modelId")
            ],
            current_mode_id=modes.get("currentModeId"),
            available_modes=[
                ModeInfo.model_validate(item)
                for item in modes.get("availableModes", [])
                if isinstance(item, dict) and item.get("id")
            ],
            config_options=config_options,
            meta=result.get("_meta") if isinstance(result.get("_meta"), dict) else {},
        )
        info.apply_config_options(config_options)
        return info

    def apply_config_options(self, config_options: list[ConfigOption]) -> None:
        """Refresh model/mode state from a ``configOptions`` list."""
        if config_options:
            self.config_options = config_options
        for option in config_options:
            if option.category == "model" or option.id == "model":
                if not self.available_models:
                    self.available_models = [
                        ModelInfo(
                            modelId=str(o["value"]),
                            name=o.get("name"),
                            description=o.get("description"),
                        )
                        for o in option.options
                        if isinstance(o, dict) and o.get("value")
                    ]
                if option.current_value is not None:
                    self.current_model_id = str(option.current_value)
            elif option.category == "mode" or option.id == "mode":
                if not self.available_modes:
                    self.available_modes = [
                        ModeInfo(
                            id=str(o["value"]), name=o.get("name"), description=o.get("description")
                        )
                        for o in option.options
                        if isinstance(o, dict) and o.get("value")
                    ]
                if option.current_value is not None:
                    self.current_mode_id = str(option.current_value)

    def config_option(self, option_id: str, *, category: str | None = None) -> ConfigOption | None:
        for option in self.config_options:
            if option.id == option_id or (category is not None and option.category == category):
                return option
        return None

    @property
    def model_ids(self) -> list[str]:
        return [m.model_id for m in self.available_models]

    @property
    def mode_ids(self) -> list[str]:
        return [m.id for m in self.available_modes]


class PlanEntry(_Lenient):
    content: str = ""
    priority: str = "medium"
    status: str = "pending"


class ToolCallLocation(_Lenient):
    path: str
    line: int | None = None


class ToolCall(BaseModel):
    """Accumulated state of one agent tool call across ``tool_call``/``tool_call_update`` events."""

    model_config = ConfigDict(extra="allow")

    id: str
    title: str = ""
    kind: ToolKind = ToolKind.OTHER
    status: ToolCallStatus = ToolCallStatus.PENDING
    tool_name: str | None = None
    content: list[JSON] = Field(default_factory=list)
    locations: list[ToolCallLocation] = Field(default_factory=list)
    raw_input: Any = None
    raw_output: Any = None

    def apply(self, update: JSON) -> None:
        """Merge a ``tool_call`` or ``tool_call_update`` payload into this call."""
        if "title" in update and update["title"] is not None:
            self.title = str(update["title"])
        if "kind" in update and update["kind"] is not None:
            self.kind = ToolKind.parse(update["kind"])
        if "status" in update and update["status"] is not None:
            self.status = ToolCallStatus.parse(update["status"])
        if update.get("content") is not None:
            self.content = [c for c in update["content"] if isinstance(c, dict)]
        if update.get("locations") is not None:
            self.locations = [
                ToolCallLocation.model_validate(loc)
                for loc in update["locations"]
                if isinstance(loc, dict) and loc.get("path")
            ]
        if "rawInput" in update:
            self.raw_input = update["rawInput"]
        if "rawOutput" in update:
            self.raw_output = update["rawOutput"]
        meta = update.get("_meta")
        if isinstance(meta, dict):
            kiro_meta = meta.get("kiro")
            if isinstance(kiro_meta, dict) and kiro_meta.get("toolName"):
                self.tool_name = str(kiro_meta["toolName"])

    @property
    def is_terminal(self) -> bool:
        return self.status in (ToolCallStatus.COMPLETED, ToolCallStatus.FAILED)

    def output_text(self) -> str:
        """Best-effort plain-text rendering of the tool output."""
        parts: list[str] = []
        for item in self.content:
            kind = item.get("type")
            if kind == "content":
                inner = item.get("content") or {}
                if inner.get("type") == "text":
                    parts.append(str(inner.get("text", "")))
            elif kind == "diff":
                parts.append(f"[diff] {item.get('path', '')}")
            elif kind == "terminal":
                parts.append(f"[terminal {item.get('terminalId', '')}]")
        if parts:
            return "\n".join(parts)
        return _raw_output_text(self.raw_output)

    def to_dict(self) -> JSON:
        return {
            "id": self.id,
            "title": self.title,
            "kind": self.kind.value,
            "status": self.status.value,
            "tool_name": self.tool_name,
            "locations": [loc.model_dump(exclude_none=True) for loc in self.locations],
            "raw_input": self.raw_input,
            "raw_output": self.raw_output,
            "content": self.content,
        }


def _raw_output_text(raw: Any) -> str:
    """Flatten Kiro's ``{"items": [{"Text": ...} | {"Json": ...}]}`` raw output shape."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict) and isinstance(raw.get("items"), list):
        chunks: list[str] = []
        for item in raw["items"]:
            if not isinstance(item, dict):
                chunks.append(str(item))
                continue
            if "Text" in item:
                chunks.append(str(item["Text"]))
            elif "Json" in item:
                value = item["Json"]
                if isinstance(value, dict) and "stdout" in value:
                    out = str(value.get("stdout", ""))
                    err = str(value.get("stderr", ""))
                    status = str(value.get("exit_status", ""))
                    text = out
                    if err:
                        text += ("\n" if text else "") + f"[stderr]\n{err}"
                    if status:
                        text += ("\n" if text else "") + f"[{status}]"
                    chunks.append(text)
                else:
                    import json

                    chunks.append(json.dumps(value, ensure_ascii=False))
            else:
                import json

                chunks.append(json.dumps(item, ensure_ascii=False))
        return "\n".join(chunks)
    import json

    return json.dumps(raw, ensure_ascii=False)


class PermissionOption(_Lenient):
    option_id: str = Field(alias="optionId")
    name: str = ""
    kind: PermissionKind = PermissionKind.UNKNOWN

    @classmethod
    def from_acp(cls, raw: JSON) -> PermissionOption:
        return cls(
            optionId=str(raw.get("optionId", raw.get("id", ""))),
            name=str(raw.get("name") or raw.get("label") or raw.get("optionId", "")),
            kind=PermissionKind.parse(raw.get("kind")),
        )


class PermissionRequest(BaseModel):
    """A ``session/request_permission`` request as seen by policy code."""

    model_config = ConfigDict(extra="allow")

    session_id: str
    tool_call_id: str
    title: str
    kind: ToolKind
    tool_name: str | None
    raw_input: Any
    options: list[PermissionOption]
    raw: JSON

    @classmethod
    def from_params(cls, params: JSON, known_call: ToolCall | None = None) -> PermissionRequest:
        tool_call = params.get("toolCall") or {}
        if not isinstance(tool_call, dict):
            tool_call = {}
        title = (
            tool_call.get("title")
            or (known_call.title if known_call else None)
            or "Permission request"
        )
        kind_raw = tool_call.get("kind") or (known_call.kind.value if known_call else None)
        tool_name = None
        meta = tool_call.get("_meta")
        if isinstance(meta, dict) and isinstance(meta.get("kiro"), dict):
            tool_name = meta["kiro"].get("toolName")
        if tool_name is None and known_call is not None:
            tool_name = known_call.tool_name
        raw_input = tool_call.get("rawInput")
        if raw_input is None and known_call is not None:
            raw_input = known_call.raw_input
        options = [
            PermissionOption.from_acp(opt)
            for opt in params.get("options", [])
            if isinstance(opt, dict)
        ]
        return cls(
            session_id=str(params.get("sessionId", "")),
            tool_call_id=str(tool_call.get("toolCallId", "")),
            title=str(title),
            kind=ToolKind.parse(kind_raw) if kind_raw else ToolKind.OTHER,
            tool_name=tool_name,
            raw_input=raw_input,
            options=options,
            raw=params,
        )

    def option_of_kind(self, *kinds: PermissionKind) -> PermissionOption | None:
        for kind in kinds:
            for option in self.options:
                if option.kind == kind:
                    return option
        return None


def selected(option_id: str) -> JSON:
    return {"outcome": {"outcome": "selected", "optionId": option_id}}


def cancelled() -> JSON:
    return {"outcome": {"outcome": "cancelled"}}
