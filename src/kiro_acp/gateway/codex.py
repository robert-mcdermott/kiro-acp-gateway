"""Serve a Codex model catalogue from the gateway.

Codex CLI (0.154+) asks every model provider for ``GET <base_url>/models?client_version=<v>``
and expects its own catalogue format (``{"models": [{"slug", "tool_mode", ...}]}``). Against
an OpenAI-style list it logs a decode error and falls back to built-in metadata, printing
"Model metadata for <model> not found" for Kiro names. Answering that request with a
catalogue built for the installed Codex version gives Codex real metadata for every Kiro
model with no client configuration.

The reference entry (base instructions and defaults) comes from Codex's published
catalogue for the requesting version; it is cached per version, and failures are cached
briefly so an offline gateway does not retry on every request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from kiro_acp.cli.codex_catalog import build_catalog, fetch_reference_catalog, reference_entry

LOG = logging.getLogger("kiro_acp.gateway.codex")

SUCCESS_TTL = 6 * 3600.0
FAILURE_TTL = 600.0


class CodexCatalogCache:
    """Per-Codex-version cache of the reference catalogue entry."""

    def __init__(self, fetch=fetch_reference_catalog) -> None:
        self._fetch = fetch
        self._entries: dict[str, tuple[dict[str, Any] | None, float]] = {}
        self._lock = asyncio.Lock()

    async def reference(self, version: str | None) -> dict[str, Any] | None:
        key = version or "main"
        async with self._lock:
            cached = self._entries.get(key)
            if cached is not None:
                entry, expires = cached
                if time.monotonic() < expires:
                    return entry
            try:
                catalog, ref = await asyncio.to_thread(self._fetch, version)
                entry = reference_entry(catalog)
                LOG.info("Loaded Codex reference catalogue %s for client %s", ref, key)
                self._entries[key] = (entry, time.monotonic() + SUCCESS_TTL)
                return entry
            except Exception as error:
                LOG.warning("Codex catalogue unavailable for client %s: %s", key, error)
                self._entries[key] = (None, time.monotonic() + FAILURE_TTL)
                return None


def codex_models(
    models: list[dict[str, Any]], reference: dict[str, Any], *, tool_mode: str
) -> dict[str, Any]:
    return build_catalog(models, reference, tool_mode=tool_mode)
