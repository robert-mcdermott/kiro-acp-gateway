"""FastAPI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from kiro_acp import __version__
from kiro_acp.gateway.backend import GatewayError, KiroBackend
from kiro_acp.gateway.config import Settings
from kiro_acp.gateway.protocols import (
    anthropic,
    models,
    openai_chat,
    openai_completions,
    openai_responses,
)

LOG = logging.getLogger("kiro_acp.gateway")


def create_app(settings: Settings | None = None, *, backend: KiroBackend | None = None) -> FastAPI:
    settings = settings or Settings()
    backend = backend or KiroBackend(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        import os

        if not os.path.isdir(settings.workspace):
            raise RuntimeError(f"KIRO_GATEWAY_WORKSPACE is not a directory: {settings.workspace}")
        LOG.info(
            "kiro-gateway %s: engine=%s workspace=%s permissions=%s session_mode=%s tool_mode=%s",
            __version__,
            settings.engine,
            settings.workspace,
            settings.permissions,
            settings.session_mode,
            settings.tool_mode,
        )
        if not settings.accepted_keys():
            LOG.warning(
                "No KIRO_GATEWAY_API_KEY configured; the API is unauthenticated (bind to localhost only)"
            )
        await backend.start()
        try:
            yield
        finally:
            await backend.stop()

    app = FastAPI(
        title="Kiro ACP Gateway",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.backend = backend
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    def get_backend() -> KiroBackend:
        return backend

    async def authorize(request: Request) -> None:
        keys = settings.accepted_keys()
        if not keys:
            return
        supplied = None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        supplied = supplied or request.headers.get("x-api-key")
        if supplied not in keys:
            if supplied and supplied.startswith("sk-ant-oat"):
                LOG.warning(
                    "401 on %s: the client sent a Claude account OAuth token instead of the gateway key. "
                    "Claude Code prefers its login over ANTHROPIC_API_KEY; set "
                    "ANTHROPIC_AUTH_TOKEN=<gateway key> or run `claude /logout` (see README, Troubleshooting).",
                    request.url.path,
                )
            elif supplied:
                LOG.warning(
                    "401 on %s: credential does not match KIRO_GATEWAY_API_KEY", request.url.path
                )
            else:
                LOG.warning("401 on %s: no Authorization or x-api-key header", request.url.path)
            anthropic_client = (
                "anthropic-version" in request.headers or "x-api-key" in request.headers
            )
            raise (
                GatewayError(
                    "Invalid API key",
                    status=401,
                    error_type="authentication_error",
                    code="invalid_api_key",
                )
                if not anthropic_client
                else GatewayError(
                    "invalid x-api-key", status=401, error_type="authentication_error"
                )
            )

    openai_routers = [
        openai_chat.make_router(get_backend, authorize),
        openai_completions.make_router(get_backend, authorize),
        openai_responses.make_router(get_backend, authorize),
    ]
    anthropic_router = anthropic.make_router(get_backend, authorize)
    for prefix in ("/v1", "/openai/v1"):
        for router in openai_routers:
            app.include_router(router, prefix=prefix)
    for prefix in ("/v1", "/anthropic/v1"):
        app.include_router(anthropic_router, prefix=prefix)
    app.include_router(models.make_router(get_backend, authorize), prefix="/v1")
    app.include_router(
        models.make_router(get_backend, authorize, style="openai"), prefix="/openai/v1"
    )
    app.include_router(
        models.make_router(get_backend, authorize, style="anthropic"), prefix="/anthropic/v1"
    )

    @app.get("/health")
    async def health():
        return backend.health()

    @app.api_route("/api/hello", methods=["GET", "HEAD"])
    async def api_hello():
        # Claude Code probes this path to check connectivity.
        return {"ok": True}

    @app.get("/")
    async def index():
        return {
            "name": "kiro-acp-gateway",
            "version": __version__,
            "endpoints": [
                "/v1/chat/completions",
                "/v1/completions",
                "/v1/responses",
                "/v1/messages",
                "/v1/messages/count_tokens",
                "/v1/models",
                "/health",
            ],
        }

    def wants_anthropic(request: Request) -> bool:
        return (
            request.url.path.startswith("/anthropic")
            or "/messages" in request.url.path
            or "anthropic-version" in request.headers
        )

    def error_body(request: Request, message: str, error_type: str, code: str | None) -> dict:
        if wants_anthropic(request):
            return {"type": "error", "error": {"type": error_type, "message": message}}
        return {"error": {"message": message, "type": error_type, "param": None, "code": code}}

    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, error: GatewayError):
        if error.status >= 500:
            LOG.error(
                "%s %s -> %s: %s", request.method, request.url.path, error.status, error.message
            )
        headers = {"WWW-Authenticate": "Bearer"} if error.status == 401 else None
        return JSONResponse(
            error_body(request, error.message, error.error_type, error.code),
            status_code=error.status,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError):
        return JSONResponse(
            error_body(request, str(error), "invalid_request_error", "validation_error"),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, error: Exception):
        LOG.exception("Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            error_body(request, f"Internal error: {error}", "api_error", "internal_error"),
            status_code=500,
        )

    return app
