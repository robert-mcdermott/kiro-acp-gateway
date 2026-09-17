"""Opt-in tests against a real ``kiro-cli`` (``KIRO_INTEGRATION=1 uv run pytest -m integration``)."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
import pytest

from kiro_acp.acp import ClientHandlers, KiroAgent, KiroLaunchOptions, PermissionPolicy, StopReason
from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.config import Settings
from tests.conftest import integration_enabled

pytestmark = pytest.mark.integration

MODEL = "gpt-5.6-luna"

if not integration_enabled() or shutil.which("kiro-cli") is None:
    pytest.skip("set KIRO_INTEGRATION=1 with kiro-cli installed", allow_module_level=True)


@pytest.mark.parametrize("engine", ["v3", "v2"])
async def test_prompt_roundtrip(workspace: Path, engine: str) -> None:
    options = KiroLaunchOptions(engine=engine, model=MODEL)
    async with KiroAgent(
        options, cwd=workspace, handlers=ClientHandlers(PermissionPolicy("deny"))
    ) as agent:
        session = await agent.new_session()
        assert MODEL in session.info.model_ids
        assert session.model_id == MODEL
        result = await session.prompt_text("Reply with exactly the word: pong", timeout=120)
        assert result.ok and "pong" in result.text.lower()
        assert result.stop_reason == StopReason.END_TURN
        assert result.metadata.get("meteringUsage")


@pytest.mark.parametrize("engine", ["v3", "v2"])
async def test_tool_permission_denied(workspace: Path, engine: str) -> None:
    options = KiroLaunchOptions(engine=engine, model=MODEL)
    async with KiroAgent(
        options, cwd=workspace, handlers=ClientHandlers(PermissionPolicy("deny"))
    ) as agent:
        session = await agent.new_session(autopilot=False)
        result = await session.prompt_text(
            "Run the shell command `touch denied.txt` then reply with the word: FINISHED",
            timeout=180,
        )
        assert result.ok
        assert not (workspace / "denied.txt").exists()


async def test_gateway_chat_and_messages(workspace: Path) -> None:
    settings = Settings(
        _env_file=None, workspace=str(workspace), permissions="deny", default_model=MODEL
    )
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw", timeout=300
        ) as http,
    ):
        models = await http.get("/v1/models")
        assert MODEL in [m["id"] for m in models.json()["data"]]
        chat = await http.post(
            "/v1/chat/completions",
            json={
                "model": MODEL,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            },
        )
        assert chat.status_code == 200, chat.text
        assert "pong" in chat.json()["choices"][0]["message"]["content"].lower()
        msg = await http.post(
            "/v1/messages",
            headers={"anthropic-version": "2023-06-01"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
            },
        )
        assert msg.status_code == 200, msg.text
        assert "pong" in msg.json()["content"][0]["text"].lower()
