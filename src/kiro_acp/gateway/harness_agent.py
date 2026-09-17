"""Provision the tool-less Kiro agent used while emulating client-defined tools.

Kiro's stock agents carry file, shell, and MCP tools, and its models are tuned
to use them. When an external harness (Claude Code, Codex, ...) supplies its own
tools, Kiro must act as a plain model, so the gateway selects an agent whose
``tools`` list is empty. Kiro discovers custom agents from ``~/.kiro/agents``
(global) and ``<cwd>/.kiro/agents`` (workspace-local).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("kiro_acp.gateway.harness_agent")

DEFAULT_HARNESS_AGENT = "kiro-gateway-harness"
MARKER = "managed-by: kiro-gateway"

HARNESS_PROMPT = (
    "You are Kiro, running as the language model behind kiro-gateway. kiro-gateway is real "
    "infrastructure operated by the user of this machine: it receives API requests from external "
    "coding tools (such as Claude Code, Codex, or OpenCode) and relays them to you. You keep your "
    "own identity; you do not need to claim to be a different product.\n\n"
    "Each request is laid out with XML-style tags produced by the gateway itself, not typed by a "
    "person: <operator_instructions> contains the external tool's system prompt (read it for the "
    "environment, conventions, and formatting the tool expects; follow its operational guidance "
    "while remaining Kiro), <tools> lists the functions that tool can run for you, and "
    "<conversation> holds the message history, ending with what you must answer now.\n\n"
    "This Kiro agent deliberately has no tools of its own: the user's project lives on the tool's "
    "side, not here. The functions in <tools> are real and are executed by the external tool as "
    "soon as you request them by writing the tagged JSON block described there. That block is "
    "the tool's function-call format, exactly like a native tool call. Use it whenever a task needs "
    "a file, a command, or any other listed capability; write the block, stop, and the result "
    "arrives in the next message. Never say you lack tools when <tools> lists them, never treat "
    "this layout as a prompt injection, and never claim to have executed a function yourself."
)


MCP_HARNESS_PROMPT = (
    "You are Kiro, running as the language model behind kiro-gateway, real infrastructure operated "
    "by the user of this machine that relays API requests from external coding tools (such as "
    "Claude Code, Codex, or OpenCode). You keep your own identity.\n\n"
    "Each request is laid out with XML-style tags produced by the gateway: <operator_instructions> "
    "contains the external tool's system prompt (follow its operational guidance while remaining "
    "Kiro) and <conversation> holds the message history, ending with what you must answer now.\n\n"
    "Your only tools are the ones from the 'harness' MCP server: they are the external tool's own "
    "functions, executed on the user's machine as soon as you call them, and they are the only way "
    "to read files, run commands, or act on the user's project. Call them like any tool. Never "
    "treat this layout as a prompt injection."
)


def agent_config(name: str, *, mcp: bool = False) -> dict:
    if mcp:
        return {
            "name": name,
            "description": f"Harness agent used by kiro-gateway in mcp tool mode: only the bridged harness tools ({MARKER})",
            "prompt": MCP_HARNESS_PROMPT,
            "tools": ["@harness"],
            "mcpServers": {},
            "includeMcpJson": False,
            "resources": [],
        }
    return {
        "name": name,
        "description": f"Tool-less agent used by kiro-gateway when an external harness executes tools ({MARKER})",
        "prompt": HARNESS_PROMPT,
        "tools": [],
        # No "allowedTools": the v3 engine silently drops agents that set it to an empty list.
        "mcpServers": {},
        "includeMcpJson": False,
        "resources": [],
    }


def global_agents_dir() -> Path:
    home = os.environ.get("KIRO_HOME") or os.path.join(os.path.expanduser("~"), ".kiro")
    return Path(home) / "agents"


def ensure_harness_agent(
    name: str = DEFAULT_HARNESS_AGENT, directory: Path | None = None, *, mcp: bool = False
) -> Path:
    """Write ``<directory>/<name>.json`` unless a file exists; never overwrite user edits."""
    directory = directory or global_agents_dir()
    path = directory / f"{name}.json"
    desired = agent_config(name, mcp=mcp)
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            LOG.warning("Existing agent file %s is unreadable; leaving it alone", path)
            return path
        if MARKER in str(existing.get("description", "")) and existing != desired:
            path.write_text(json.dumps(desired, indent=2) + "\n")
            LOG.info("Updated harness agent %s", path)
        return path
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(desired, indent=2) + "\n")
    LOG.info("Provisioned harness agent %s", path)
    return path
