"""Model listing in OpenAI and Anthropic formats."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kiro_acp.gateway.backend import GatewayError, KiroBackend
from kiro_acp.gateway.protocols.common import now


def hyphenated(model_id: str) -> str | None:
    """``claude-sonnet-4.6`` -> ``claude-sonnet-4-6`` (None when nothing changes)."""
    alias = re.sub(r"(?<=\d)\.(?=\d)", "-", model_id)
    return alias if alias != model_id else None


def catalogue(backend: KiroBackend, models) -> list[tuple[str, str | None, str | None]]:
    """(id, display name, description) rows, including Claude Code friendly aliases."""
    rows = [(m.model_id, m.name, m.description) for m in models]
    if backend.settings.model_alias_style == "both":
        seen = {m.model_id for m in models}
        for m in models:
            alias = hyphenated(m.model_id)
            if alias and alias not in seen:
                rows.append((alias, m.name, f"Alias of {m.model_id}"))
                seen.add(alias)
        for alias in ("claude-auto", "auto"):
            if alias not in seen:
                rows.append((alias, "Kiro default model", "Alias of the gateway default model"))
    return rows


def make_router(backend_dep, auth_dep, *, style: str = "auto") -> APIRouter:
    router = APIRouter(dependencies=[Depends(auth_dep)])

    def anthropic_style(request: Request) -> bool:
        if style == "anthropic":
            return True
        if style == "openai":
            return False
        return "anthropic-version" in request.headers or "x-api-key" in request.headers

    @router.get("/models")
    async def list_models(request: Request, backend: KiroBackend = Depends(backend_dep)):
        models = await backend.models()
        created = now()
        rows = catalogue(backend, models)
        if anthropic_style(request):
            data = [
                {
                    "type": "model",
                    "id": model_id,
                    "display_name": name or model_id,
                    "created_at": "2025-01-01T00:00:00Z",
                }
                for model_id, name, _ in rows
            ]
            return JSONResponse(
                {
                    "data": data,
                    "has_more": False,
                    "first_id": data[0]["id"] if data else None,
                    "last_id": data[-1]["id"] if data else None,
                }
            )
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": model_id,
                        "object": "model",
                        "created": created,
                        "owned_by": "kiro",
                        "description": description,
                    }
                    for model_id, _, description in rows
                ],
            }
        )

    @router.get("/models/{model_id:path}")
    async def get_model(
        model_id: str, request: Request, backend: KiroBackend = Depends(backend_dep)
    ):
        models = {m.model_id: m for m in await backend.models()}
        resolved = await backend.resolve_model(model_id)
        model = models.get(resolved or "")
        if model is None:
            raise GatewayError(
                f"Model {model_id!r} not found",
                status=404,
                error_type="not_found_error",
                code="model_not_found",
            )
        if anthropic_style(request):
            return JSONResponse(
                {
                    "type": "model",
                    "id": model.model_id,
                    "display_name": model.name or model.model_id,
                    "created_at": "2025-01-01T00:00:00Z",
                }
            )
        return JSONResponse(
            {
                "id": model.model_id,
                "object": "model",
                "created": now(),
                "owned_by": "kiro",
                "description": model.description,
            }
        )

    return router
