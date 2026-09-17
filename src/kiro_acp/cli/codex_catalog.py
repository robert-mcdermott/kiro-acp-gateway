"""Generate a Codex ``model_catalog_json`` file that lists Kiro's models in direct tool mode.

Codex CLI ships a bundled model catalogue. Names it recognizes (``gpt-5.6-*``,
``gpt-6-*``) are switched to its "Responses Lite" wire format and *code mode*, which
sends no ordinary function tools and therefore cannot work through the gateway's
tool emulation. A ``model_catalog_json`` file replaces the bundled catalogue, so
every Kiro model can be listed with ``tool_mode: "direct"`` and
``use_responses_lite: false`` while keeping Codex's own base instructions.

The base instructions are copied from a reference entry in Codex's published
catalogue (fetched from GitHub for the installed Codex version) so Codex keeps its
real system prompt; nothing here invents one.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import urllib.request
from typing import Any

CATALOG_URL = (
    "https://raw.githubusercontent.com/openai/codex/{ref}/codex-rs/models-manager/models.json"
)
REFERENCE_SLUGS = (
    "gpt-5.6-luna",
    "gpt-5.6-terra",
    "gpt-5.6-sol",
    "gpt-6-astra",
    "gpt-5.5",
    "gpt-5.4",
)


def codex_version() -> str | None:
    exe = shutil.which("codex")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(r"(\d+\.\d+\.\d+)", out)
    return match.group(1) if match else None


def fetch_reference_catalog(
    version: str | None, *, timeout: float = 20.0
) -> tuple[dict[str, Any], str]:
    refs = [f"rust-v{version}"] if version else []
    refs.append("main")
    last_error: Exception | None = None
    for ref in refs:
        url = CATALOG_URL.format(ref=ref)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - fixed GitHub URL
                return json.loads(response.read().decode("utf-8")), ref
        except Exception as error:  # pragma: no cover - network
            last_error = error
    raise RuntimeError(f"Could not download Codex's model catalogue: {last_error}")


def reference_entry(catalog: dict[str, Any]) -> dict[str, Any]:
    models = catalog.get("models") or []
    by_slug = {m.get("slug"): m for m in models if isinstance(m, dict)}
    for slug in REFERENCE_SLUGS:
        if slug in by_slug:
            return by_slug[slug]
    for model in models:
        if isinstance(model, dict) and model.get("base_instructions"):
            return model
    raise RuntimeError("Codex catalogue has no usable reference entry")


def build_catalog(
    kiro_models: list[dict[str, Any]],
    reference: dict[str, Any],
    *,
    context_window: int | None = None,
) -> dict[str, Any]:
    """Clone ``reference`` for each Kiro model with direct tools and the standard wire format."""
    entries = []
    for index, model in enumerate(kiro_models):
        entry = copy.deepcopy(reference)
        entry["slug"] = model["id"]
        entry["display_name"] = f"{model['id']} (Kiro)"
        entry["description"] = model.get("description") or "Served by kiro-gateway"
        entry["priority"] = index + 1
        entry["visibility"] = "list"
        entry["supported_in_api"] = True
        entry["tool_mode"] = "direct"
        entry["use_responses_lite"] = False
        entry["prefer_websockets"] = False
        entry["supports_search_tool"] = False
        entry["minimal_client_version"] = None
        window = context_window or model.get("context_length")
        if window:
            entry["context_window"] = window
            entry["max_context_window"] = window
        entries.append(entry)
    return {"models": entries}
