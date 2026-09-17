"""Agent Client Protocol (ACP) client implementation.

The :mod:`kiro_acp.acp` package speaks newline-delimited JSON-RPC 2.0 to an ACP
agent subprocess (such as ``kiro-cli acp``), services agent-to-client requests
(permissions, file system, terminals), and exposes prompt turns as a typed
stream of events.
"""

from kiro_acp.acp.client import ACPClient
from kiro_acp.acp.errors import ACPError, ACPProcessError, ACPRemoteError, ACPTimeoutError
from kiro_acp.acp.events import (
    ExtensionNotification,
    MetadataUpdate,
    PermissionDecision,
    PlanUpdate,
    TextDelta,
    ThoughtDelta,
    ToolCallEvent,
    TurnComplete,
    TurnEvent,
)
from kiro_acp.acp.handlers import (
    ClientHandlers,
    LocalFileSystem,
    LocalTerminals,
    PermissionPolicy,
    PermissionRule,
)
from kiro_acp.acp.kiro import KiroAgent, KiroLaunchOptions
from kiro_acp.acp.session import Session, TurnResult
from kiro_acp.acp.types import (
    AgentCapabilities,
    AgentInfo,
    ModeInfo,
    ModelInfo,
    PermissionOption,
    PermissionRequest,
    PlanEntry,
    SessionInfo,
    StopReason,
    ToolCall,
    ToolCallStatus,
    ToolKind,
)

__all__ = [
    "ACPClient",
    "ACPError",
    "ACPProcessError",
    "ACPRemoteError",
    "ACPTimeoutError",
    "AgentCapabilities",
    "AgentInfo",
    "ClientHandlers",
    "ExtensionNotification",
    "KiroAgent",
    "KiroLaunchOptions",
    "LocalFileSystem",
    "LocalTerminals",
    "MetadataUpdate",
    "ModeInfo",
    "ModelInfo",
    "PermissionDecision",
    "PermissionOption",
    "PermissionPolicy",
    "PermissionRequest",
    "PermissionRule",
    "PlanEntry",
    "PlanUpdate",
    "Session",
    "SessionInfo",
    "StopReason",
    "TextDelta",
    "ThoughtDelta",
    "ToolCall",
    "ToolCallEvent",
    "ToolCallStatus",
    "ToolKind",
    "TurnComplete",
    "TurnEvent",
    "TurnResult",
]
