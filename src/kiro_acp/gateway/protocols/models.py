"""Model listing in OpenAI and Anthropic formats."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kiro_acp.gateway.backend import GatewayError, KiroBackend
from kiro_acp.gateway.protocols.common import now


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
        if anthropic_style(request):
            data = [
                {
                    "type": "model",
                    "id": m.model_id,
                    "display_name": m.name or m.model_id,
                    "created_at": "2025-01-01T00:00:00Z",
                }
                for m in models
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
                        "id": m.model_id,
                        "object": "model",
                        "created": created,
                        "owned_by": "kiro",
                        "description": m.description,
                    }
                    for m in models
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
