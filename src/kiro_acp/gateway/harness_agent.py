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
    "You are a language model serving API requests through kiro-gateway. Each request describes "
    "the tools you have and a tool-calling protocol (tagged JSON blocks); those are your only "
    "tools and you use them by writing the blocks exactly as described. The harness executes them "
    "and sends the results back. Never say you lack tools when the request lists them, and never "
    "claim to have run a tool yourself. Do not load skills or steering documents unless asked."
)


def agent_config(name: str) -> dict:
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


def ensure_harness_agent(name: str = DEFAULT_HARNESS_AGENT, directory: Path | None = None) -> Path:
    """Write ``<directory>/<name>.json`` unless a file exists; never overwrite user edits."""
    directory = directory or global_agents_dir()
    path = directory / f"{name}.json"
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            LOG.warning("Existing agent file %s is unreadable; leaving it alone", path)
            return path
        if MARKER in str(existing.get("description", "")) and existing != agent_config(name):
            path.write_text(json.dumps(agent_config(name), indent=2) + "\n")
            LOG.info("Updated harness agent %s", path)
        return path
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(agent_config(name), indent=2) + "\n")
    LOG.info("Provisioned harness agent %s", path)
    return path
