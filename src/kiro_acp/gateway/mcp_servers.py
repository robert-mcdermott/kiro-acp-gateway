"""MCP servers for agent-mode turns: catalogue, discovery, and the ACP wire shape.

Kiro accepts MCP servers per session in ``session/new.mcpServers`` (on both engines) as an
array of ``{"name", "command", "args", "env": [{"name", "value"}], "type": "stdio"}`` or
``{"name", "type": "http" | "sse", "url", "headers": [{"name", "value"}]}``. Missing
``env``/``headers`` arrays or a url entry without ``type`` make ``session/new`` fail or
hang, so every entry is normalised here.

The gateway keeps a catalogue of servers (``KIRO_GATEWAY_MCP_SERVERS``, plus servers
discovered from the workspace's client config files when ``KIRO_GATEWAY_MCP_DISCOVERY`` is
on). Requests attach servers by name; full definitions are only accepted with
``KIRO_GATEWAY_ALLOW_REQUEST_MCP_SERVERS`` because a stdio definition runs a command on the
gateway host.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any

from kiro_acp.gateway.conversation import JSON

LOG = logging.getLogger("kiro_acp.gateway.mcp")

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class McpServerError(ValueError):
    pass


def _pairs(raw: Any) -> list[JSON]:
    """``{"K": "v"}`` or ``[{"name", "value"}]`` -> ACP's array-of-pairs form."""
    if isinstance(raw, dict):
        return [{"name": str(k), "value": str(v)} for k, v in raw.items()]
    if isinstance(raw, list):
        out = []
        for item in raw:
            if isinstance(item, dict) and "name" in item:
                out.append({"name": str(item["name"]), "value": str(item.get("value", ""))})
        return out
    return []


def normalize_server(name: str, spec: Any) -> JSON:
    """One catalogue/config entry (any supported client shape) -> ACP ``mcpServers`` element."""
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise McpServerError(f"Invalid MCP server name {name!r}")
    if not isinstance(spec, dict):
        raise McpServerError(f"MCP server {name!r}: definition must be an object")
    if spec.get("disabled") is True or spec.get("enabled") is False:
        raise McpServerError(f"MCP server {name!r} is disabled")
    kind = str(spec.get("type") or "").lower()
    url = spec.get("url")
    if url or kind in ("http", "sse", "remote", "streamable-http", "streamable_http"):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise McpServerError(f"MCP server {name!r}: a remote server needs an http(s) url")
        return {
            "name": name,
            "type": "sse" if kind == "sse" else "http",
            "url": url,
            "headers": _pairs(spec.get("headers")),
        }
    command = spec.get("command")
    args = spec.get("args")
    if isinstance(command, list):  # OpenCode: "command": ["npx", "-y", "server"]
        if not command:
            raise McpServerError(f"MCP server {name!r}: empty command")
        command, args = command[0], command[1:]
    if not isinstance(command, str) or not command:
        raise McpServerError(f"MCP server {name!r}: a stdio server needs a command")
    env = spec.get("env") if spec.get("env") is not None else spec.get("environment")
    entry: JSON = {
        "name": name,
        "type": "stdio",
        "command": command,
        "args": [str(a) for a in (args or [])],
        "env": _pairs(env),
    }
    if isinstance(spec.get("cwd"), str):
        entry["cwd"] = spec["cwd"]
    return entry


def parse_catalogue(source: str) -> dict[str, JSON]:
    """``KIRO_GATEWAY_MCP_SERVERS``: inline JSON or a path to a JSON file."""
    text = source.strip()
    if not text:
        return {}
    if not text.startswith("{"):
        path = os.path.expanduser(text)
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            raise McpServerError(f"Cannot read MCP catalogue {path}: {error}") from error
    try:
        data = _load_jsonc(text)
    except ValueError as error:
        raise McpServerError(f"MCP catalogue is not valid JSON: {error}") from error
    return servers_from_config(data)


def servers_from_config(data: Any) -> dict[str, JSON]:
    """Accept the Claude Code/Cursor (``mcpServers``), VS Code (``servers``), OpenCode
    (``mcp``) and bare-map shapes; returns normalised entries keyed by name."""
    if not isinstance(data, dict):
        return {}
    block = None
    for key in ("mcpServers", "servers", "mcp"):
        if isinstance(data.get(key), dict):
            block = data[key]
            break
    if block is None:
        block = data
    out: dict[str, JSON] = {}
    for name, spec in block.items():
        try:
            out[name] = normalize_server(name, spec)
        except McpServerError as error:
            LOG.debug("Skipping MCP server: %s", error)
    return out


_JSONC_COMMENT = re.compile(r"^[ \t]*//[^\n]*$|/\*.*?\*/", re.M | re.S)


def _load_jsonc(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        cleaned = re.sub(r",(\s*[}\]])", r"\1", _JSONC_COMMENT.sub("", text))
        return json.loads(cleaned)


DISCOVERY_FILES = (
    ".mcp.json",  # Claude Code (project scope)
    os.path.join(".cursor", "mcp.json"),
    os.path.join(".vscode", "mcp.json"),
    "opencode.json",
    "opencode.jsonc",
)


def discover(workspace: str) -> dict[str, JSON]:
    """MCP servers declared by coding-agent config files in ``workspace``."""
    found: dict[str, JSON] = {}
    for relative in DISCOVERY_FILES:
        path = os.path.join(workspace, relative)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                data = _load_jsonc(handle.read())
        except (OSError, ValueError) as error:
            LOG.warning("Ignoring unreadable MCP config %s: %s", path, error)
            continue
        servers = servers_from_config(data)
        for name, entry in servers.items():
            if name not in found:
                found[name] = entry
        if servers:
            LOG.info("Discovered %d MCP server(s) in %s", len(servers), path)
    return found


def signature(servers: list[JSON]) -> str:
    if not servers:
        return ""
    payload = json.dumps(sorted(servers, key=lambda s: s["name"]), sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def agent_mcp_servers(servers: list[JSON]) -> JSON:
    """ACP array elements -> the ``mcpServers`` map used inside an agent definition."""
    out: JSON = {}
    for entry in servers:
        if entry.get("type") in ("http", "sse"):
            out[entry["name"]] = {
                "type": entry["type"],
                "url": entry["url"],
                "headers": {p["name"]: p["value"] for p in entry.get("headers", [])},
            }
        else:
            out[entry["name"]] = {
                "command": entry["command"],
                "args": list(entry.get("args", [])),
                "env": {p["name"]: p["value"] for p in entry.get("env", [])},
            }
    return out
