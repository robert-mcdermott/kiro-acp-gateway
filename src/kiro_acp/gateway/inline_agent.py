"""Inline (per-request) agent definitions, delivered to the v3 engine over the wire.

The v3 engine (Kiro Agent Server) accepts ``_meta.kiro.customAgents`` on ``session/new``
and registers each entry as a selectable mode. Each entry is ``{"id", "prompt", "tools",
"description"?, "mcpServers"?, "resources"?}``; ``tools`` absent means *no* tools, so it
is always sent. The v2 engine only takes ``--agent <name>`` at launch, so inline agents are
a v3 feature.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from kiro_acp.gateway.conversation import JSON

MAX_PROMPT = 200_000


class InlineAgentError(ValueError):
    pass


def parse_inline_agent(raw: Any) -> JSON:
    """Validate a request's ``kiro.agent`` object into the wire entry (without ``id``)."""
    if not isinstance(raw, dict):
        raise InlineAgentError("kiro.agent must be an object")
    prompt = raw.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise InlineAgentError("kiro.agent.prompt must be a non-empty string")
    if len(prompt) > MAX_PROMPT:
        raise InlineAgentError("kiro.agent.prompt is too long")
    tools = raw.get("tools", ["*"])
    if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
        raise InlineAgentError("kiro.agent.tools must be a list of tool names")
    entry: JSON = {"prompt": prompt, "tools": tools}
    for key in ("description", "name"):
        if isinstance(raw.get(key), str) and raw[key]:
            entry["description"] = raw[key]
            break
    if isinstance(raw.get("resources"), list):
        entry["resources"] = [r for r in raw["resources"] if isinstance(r, str)]
    return entry


def agent_id(entry: JSON) -> str:
    digest = hashlib.sha1(json.dumps(entry, sort_keys=True).encode()).hexdigest()[:10]
    return f"gateway-inline-{digest}"


def custom_agent(entry: JSON, *, mcp_servers: JSON | None = None) -> JSON:
    wire = {"id": agent_id(entry), **entry}
    if mcp_servers:
        wire["mcpServers"] = mcp_servers
        names = [f"@{name}" for name in mcp_servers]
        wire["tools"] = list(dict.fromkeys([*wire.get("tools", []), *names]))
    return wire


def harness_custom_agent(config: JSON) -> JSON:
    """The provisioned harness agent file, as a wire entry for v3 sessions."""
    wire: JSON = {
        "id": config["name"],
        "prompt": config["prompt"],
        "tools": list(config.get("tools", [])),
    }
    if config.get("description"):
        wire["description"] = config["description"]
    if config.get("mcpServers"):
        wire["mcpServers"] = config["mcpServers"]
    if config.get("resources"):
        wire["resources"] = config["resources"]
    return wire
