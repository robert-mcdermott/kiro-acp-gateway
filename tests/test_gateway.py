from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.backend import KiroBackend
from kiro_acp.gateway.config import Settings
from tests.conftest import fake_agent_command


class FakeKiroBackend(KiroBackend):
    """Backend that launches the scripted fake agent (or another command) instead of kiro-cli."""

    def __init__(self, settings: Settings, command: list[str] | None = None) -> None:
        super().__init__(settings)
        self._command = command

    def _make_agent(self, **kwargs):
        agent = super()._make_agent(**kwargs)
        agent.options.raw_command = self._command or fake_agent_command()
        agent.client.command = agent.options.command()
        return agent


def make_settings(workspace: Path, **overrides) -> Settings:
    defaults = dict(
        workspace=str(workspace),
        api_key="secret",
        permissions="allow-once",
        default_model="claude-opus-4.8",
        session_idle_ttl=30,
        harness_agent="kiro_planner",
        harness_agent_mcp="kiro_planner",
        provision_harness_agent=False,
        tool_mode="emulate",
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


@pytest.fixture
async def client(workspace: Path, engine: str):
    settings = make_settings(workspace, engine=engine)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


def sse_events(body: str) -> list[tuple[str | None, dict | str]]:
    events = []
    for block in body.strip().split("\n\n"):
        event = None
        data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if data is None:
            continue
        events.append((event, json.loads(data) if data != "[DONE]" else data))
    return events


# --------------------------------------------------------------------------- basics


async def test_health_and_auth(client: httpx.AsyncClient) -> None:
    health = await client.get("/health", headers={"Authorization": ""})
    assert health.status_code == 200 and health.json()["status"] == "ok"
    denied = await client.get("/v1/models", headers={"Authorization": "Bearer wrong"})
    assert denied.status_code == 401
    assert denied.json()["error"]["type"] == "authentication_error"
    anth = await client.get(
        "/v1/messages/count_tokens",
        headers={"Authorization": "", "x-api-key": "nope", "anthropic-version": "2023-06-01"},
    )
    assert anth.status_code in (401, 405)


async def test_models_openai_and_anthropic(client: httpx.AsyncClient) -> None:
    openai = (await client.get("/v1/models")).json()
    assert openai["object"] == "list"
    ids = [m["id"] for m in openai["data"]]
    assert ids[:3] == ["claude-opus-4.8", "claude-sonnet-4.6", "gpt-5.6-terra"]
    assert {"claude-opus-4-8", "claude-sonnet-4-6", "gpt-5-6-terra", "claude-auto", "auto"} <= set(
        ids
    )
    auto = await client.post(
        "/v1/chat/completions",
        json={"model": "claude-auto", "messages": [{"role": "user", "content": "who"}]},
    )
    assert auto.json()["choices"][0]["message"]["content"].startswith("[claude-opus-4.8]")
    anthropic = (await client.get("/v1/models", headers={"anthropic-version": "2023-06-01"})).json()
    assert anthropic["data"][0]["type"] == "model"
    one = await client.get("/v1/models/claude-sonnet-4-6-20260101")
    assert one.status_code == 200 and one.json()["id"] == "claude-sonnet-4.6"


# --------------------------------------------------------------------------- chat completions


async def test_chat_completion_basic(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4.6",
            "messages": [
                {"role": "system", "content": "be brief"},
                {"role": "user", "content": "echo: hello there"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello there"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] > 0
    assert body["kiro"]["credits"] == 0.01


async def test_chat_completion_streaming(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: streamed reply"}],
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    assert response.status_code == 200
    events = sse_events(response.text)
    assert events[-1][1] == "[DONE]"
    chunks = [e[1] for e in events if isinstance(e[1], dict)]
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "streamed reply"
    assert any(c["choices"] and c["choices"][0]["finish_reason"] == "stop" for c in chunks)
    assert chunks[-1]["usage"]["total_tokens"] > 0


async def test_chat_unknown_model_falls_back(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "who"}]},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"].startswith("[claude-opus-4.8]")


async def test_chat_model_alias_normalization(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4-6-20260101",
            "messages": [{"role": "user", "content": "who"}],
        },
    )
    assert response.json()["choices"][0]["message"]["content"].startswith("[claude-sonnet-4.6]")


async def test_chat_tool_emulation_roundtrip(client: httpx.AsyncClient) -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run",
                "description": "Run a command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }
    ]
    # Ask the fake agent to echo a tool call block verbatim.
    call = '<tool_call>{"name": "run", "arguments": {"cmd": "ls"}}</tool_call>'
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: I will run it. " + call}],
            "tools": tools,
        },
    )
    body = response.json()
    message = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert message["content"] == "I will run it."
    assert message["tool_calls"][0]["function"]["name"] == "run"
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"cmd": "ls"}
    # Second request with the tool result: the transcript must include the result.
    follow = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": "echo: I will run it. " + call},
                message,
                {
                    "role": "tool",
                    "tool_call_id": message["tool_calls"][0]["id"],
                    "content": "a.txt",
                },
                {"role": "user", "content": "history?"},
            ],
            "tools": tools,
        },
    )
    assert follow.status_code == 200
    assert follow.json()["kiro"]["reused_session"] is True


async def test_harness_mode_uses_toolless_agent(client: httpx.AsyncClient) -> None:
    tools = [{"type": "function", "function": {"name": "run", "parameters": {}}}]
    with_tools = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "who"}], "tools": tools},
    )
    assert with_tools.json()["kiro"]["agent"] == "kiro_planner"
    without = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
    )
    assert without.json()["kiro"]["agent"] == "kiro_default"


def test_harness_agent_provisioning(tmp_path: Path) -> None:
    from kiro_acp.gateway.harness_agent import MARKER, agent_config, ensure_harness_agent

    path = ensure_harness_agent("kiro-gateway-harness", tmp_path / "agents")
    data = json.loads(path.read_text())
    assert data["tools"] == [] and data["mcpServers"] == {} and MARKER in data["description"]
    # A user-authored file with the same name is left untouched.
    custom = tmp_path / "agents" / "mine.json"
    custom.write_text(json.dumps({"name": "mine", "tools": ["shell"]}))
    ensure_harness_agent("mine", tmp_path / "agents")
    assert json.loads(custom.read_text())["tools"] == ["shell"]
    # A stale gateway-managed file is refreshed.
    stale = dict(agent_config("kiro-gateway-harness"))
    stale["prompt"] = "old"
    path.write_text(json.dumps(stale))
    ensure_harness_agent("kiro-gateway-harness", tmp_path / "agents")
    assert json.loads(path.read_text())["prompt"] != "old"


async def test_text_after_tool_call_is_dropped(client: httpx.AsyncClient) -> None:
    tools = [{"type": "function", "function": {"name": "run", "parameters": {}}}]
    call = '<tool_call>{"name": "run", "arguments": {"cmd": "hostname"}}</tool_call>'
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: Running it. " + call + " MYHOST"}],
            "tools": tools,
        },
    )
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "Running it."
    assert body["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "run"
    assert body["kiro"]["dropped_text_after_tool_calls"].strip() == "MYHOST"


async def test_chat_tool_emulation_streaming(client: httpx.AsyncClient) -> None:
    tools = [{"type": "function", "function": {"name": "run", "parameters": {}}}]
    call = '<tool_call>{"name": "run", "arguments": {"cmd": "ls"}}</tool_call>'
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: " + call}],
            "tools": tools,
            "stream": True,
        },
    )
    chunks = [e[1] for e in sse_events(response.text) if isinstance(e[1], dict)]
    tool_chunks = [c for c in chunks if c["choices"] and "tool_calls" in c["choices"][0]["delta"]]
    assert (
        tool_chunks
        and tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "run"
    )
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"


async def test_chat_tools_rejected_when_configured(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, tool_mode="reject")
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
        ) as http,
    ):
        response = await http.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "t"}}],
            },
        )
        assert response.status_code == 400 and response.json()["error"]["code"] == "tools_disabled"


async def test_chat_backend_error_shape(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "error"}]},
    )
    assert response.status_code == 502
    assert response.json()["error"]["type"] == "api_error"
    streamed = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "error"}], "stream": True},
    )
    events = sse_events(streamed.text)
    assert (
        any(isinstance(e[1], dict) and "error" in e[1] for e in events)
        and events[-1][1] == "[DONE]"
    )


async def test_chat_reasoning_and_thoughts(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "thought"}],
            "reasoning_effort": "high",
        },
    )
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "after thinking"
    assert message["reasoning_content"] == "thinking hard"


async def test_chat_image_data_url(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
                    ],
                }
            ],
        },
    )
    assert response.json()["choices"][0]["message"]["content"] == "saw 1 image(s): image/png"
    bad = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}
                    ],
                }
            ],
        },
    )
    assert bad.status_code == 400


async def test_chat_kiro_tool_activity_as_reasoning(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "tool"}]},
    )
    message = response.json()["choices"][0]["message"]
    assert message["content"] == "done"
    assert "⚙ Running: ls" in message["reasoning_content"]
    assert "a.txt" in message["reasoning_content"]  # execute output excerpt
    assert response.json()["kiro"]["tool_calls"][0]["title"] == "Running: ls"


async def test_session_affinity_reuses_process(client: httpx.AsyncClient) -> None:
    first = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "echo: one"}]},
    )
    reply = first.json()["choices"][0]["message"]
    assert first.json()["kiro"]["reused_session"] is False
    second = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": "echo: one"},
                {"role": "assistant", "content": reply["content"]},
                {"role": "user", "content": "history?"},
            ],
        },
    )
    body = second.json()
    assert body["kiro"]["reused_session"] is True
    assert body["kiro"]["session_id"] == first.json()["kiro"]["session_id"]
    # Only the new message was sent to the reused session.
    assert (
        json.loads(body["choices"][0]["message"]["content"]) == ["[User]\necho: one"]
        or "echo: one" in body["choices"][0]["message"]["content"]
    )
    # Different model -> new session.
    third = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-4.6",
            "messages": [
                {"role": "user", "content": "echo: one"},
                {"role": "assistant", "content": reply["content"]},
                {"role": "user", "content": "history?"},
            ],
        },
    )
    assert third.json()["kiro"]["reused_session"] is False


async def test_stateless_mode(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, session_mode="stateless")
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
        ) as http,
    ):
        first = await http.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "echo: a"}]},
        )
        second = await http.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [
                    {"role": "user", "content": "echo: a"},
                    {"role": "assistant", "content": "a"},
                    {"role": "user", "content": "echo: b"},
                ],
            },
        )
        assert first.json()["kiro"]["reused_session"] is False
        assert second.json()["kiro"]["reused_session"] is False
    assert app.state.backend.health()["live_sessions"] == 0


async def test_permission_header_requires_opt_in(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "tool"}]},
        headers={"X-Kiro-Permissions": "deny"},
    )
    assert response.status_code == 403


# --------------------------------------------------------------------------- reliability (roadmap P1)


async def test_sse_keepalive_during_silence(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, sse_keepalive=0.2)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        chat = await http.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [{"role": "user", "content": "sleep:0.9"}],
                "stream": True,
            },
        )
        assert chat.text.count(": keepalive") >= 2
        chunks = [e[1] for e in sse_events(chat.text) if isinstance(e[1], dict)]
        assert (
            "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
            == "before after"
        )
        msg = await http.post(
            "/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json={
                "model": "x",
                "max_tokens": 10,
                "stream": True,
                "messages": [{"role": "user", "content": "sleep:0.9"}],
            },
        )
        names = [e[0] for e in sse_events(msg.text)]
        assert names.count("ping") >= 3 and names[-1] == "message_stop"


async def test_error_classification_and_retry_after(client: httpx.AsyncClient) -> None:
    throttled = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "error:Request throttled, too many requests"}],
        },
    )
    assert throttled.status_code == 429
    assert throttled.headers.get("retry-after") == "30"
    assert throttled.json()["error"]["type"] == "rate_limit_error"
    unavailable = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "error:The model is not available right now"}],
        },
    )
    assert (
        unavailable.status_code == 503 and unavailable.json()["error"]["type"] == "overloaded_error"
    )
    streamed = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "error:upstream timed out"}],
            "stream": True,
        },
    )
    errors = [
        e[1]["error"]
        for e in sse_events(streamed.text)
        if isinstance(e[1], dict) and "error" in e[1]
    ]
    assert errors and errors[0]["code"] == "kiro_timeout"


async def test_stop_sequences_enforced(client: httpx.AsyncClient) -> None:
    chat = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: one two three four"}],
            "stop": ["three"],
        },
    )
    body = chat.json()
    assert body["choices"][0]["message"]["content"] == "one two "
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["kiro"]["reused_session"] is False
    msg = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "stop_sequences": ["two"],
            "stream": True,
            "messages": [{"role": "user", "content": "echo: one two three"}],
        },
    )
    events = sse_events(msg.text)
    text = "".join(
        e[1]["delta"]["text"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta"
    )
    assert text == "one "
    delta = next(e[1] for e in events if e[0] == "message_delta")
    assert delta["delta"] == {"stop_reason": "stop_sequence", "stop_sequence": "two"}


async def test_max_tokens_enforced_when_enabled(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, enforce_max_tokens=True)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        response = await http.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "long"}], "max_tokens": 5},
        )
        body = response.json()
        assert len(body["choices"][0]["message"]["content"]) == 20
        assert body["choices"][0]["finish_reason"] == "length"
        anthropic = await http.post(
            "/v1/messages",
            headers=ANTHROPIC_HEADERS,
            json={"model": "x", "max_tokens": 5, "messages": [{"role": "user", "content": "long"}]},
        )
        assert anthropic.json()["stop_reason"] == "max_tokens"


async def test_structured_output_validation_and_retry(client: httpx.AsyncClient) -> None:
    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
        "additionalProperties": False,
    }
    fmt = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema}}
    good = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": 'echo: ```json\n{"ok": true}\n```'}],
            "response_format": fmt,
        },
    )
    body = good.json()
    assert body["choices"][0]["message"]["content"] == '{"ok": true}'
    assert body["kiro"]["schema_valid"] is True
    # First reply is invalid; the gateway re-prompts and the fake agent corrects itself.
    fixed = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "badjson"}],
            "response_format": fmt,
        },
    )
    body = fixed.json()
    assert body["kiro"]["schema_valid"] is True
    assert body["choices"][0]["message"]["content"] == '{"ok": true}'
    # Streaming cannot retry; it reports validity only.
    streamed = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "badjson"}],
            "response_format": fmt,
            "stream": True,
            "stream_options": {"include_usage": True},
        },
    )
    chunks = [e[1] for e in sse_events(streamed.text) if isinstance(e[1], dict)]
    assert chunks[-1]["kiro"]["schema_valid"] is False
    assert any("invalid JSON" in err or "ok" in err for err in chunks[-1]["kiro"]["schema_errors"])
    anthropic = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 50,
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
            "messages": [{"role": "user", "content": 'echo: {"ok": "nope"}'}],
        },
    )
    assert anthropic.json()["kiro"]["schema_valid"] is True  # retried and corrected


async def test_per_request_workspace_allowlist(
    workspace: Path, engine: str, tmp_path: Path
) -> None:
    other = tmp_path / "other-project"
    other.mkdir()
    (other / "notes.txt").write_text("other")
    forbidden = tmp_path / "secret"
    forbidden.mkdir()
    settings = make_settings(
        workspace, engine=engine, allowed_workspaces=[str(tmp_path / "other-*")]
    )
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        ok = await http.post(
            "/v1/chat/completions",
            headers={"X-Kiro-Workspace": str(other)},
            json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["kiro"]["workspace"] == str(other.resolve())
        denied = await http.post(
            "/v1/chat/completions",
            headers={"X-Kiro-Workspace": str(forbidden)},
            json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
        )
        assert (
            denied.status_code == 403 and denied.json()["error"]["code"] == "workspace_not_allowed"
        )
        missing = await http.post(
            "/v1/chat/completions",
            headers={"X-Kiro-Workspace": str(tmp_path / "other-missing")},
            json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
        )
        assert missing.status_code == 400
        # Affinity is per workspace: same conversation in the default workspace starts a new session.
        reply = ok.json()["choices"][0]["message"]["content"]
        follow_other = await http.post(
            "/v1/chat/completions",
            headers={"X-Kiro-Workspace": str(other)},
            json={
                "model": "x",
                "messages": [
                    {"role": "user", "content": "who"},
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": "echo: again"},
                ],
            },
        )
        assert follow_other.json()["kiro"]["reused_session"] is True
        follow_default = await http.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [
                    {"role": "user", "content": "who"},
                    {"role": "assistant", "content": reply},
                    {"role": "user", "content": "echo: again"},
                ],
            },
        )
        assert follow_default.json()["kiro"]["reused_session"] is False


async def test_workspace_header_disabled_by_default(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    response = await client.post(
        "/v1/chat/completions",
        headers={"X-Kiro-Workspace": str(elsewhere)},
        json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "workspace_not_allowed"


async def test_rate_limit_per_key(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, rate_limit_rpm=2)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        codes = [(await http.get("/v1/models")).status_code for _ in range(3)]
        assert codes[:2] == [200, 200] and codes[2] == 429
        third = await http.get("/v1/models")
        assert third.headers.get("retry-after")


async def test_queue_timeout_returns_503(workspace: Path, engine: str) -> None:
    import asyncio

    settings = make_settings(workspace, engine=engine, max_concurrency=1, queue_timeout=0.3)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http,
    ):
        slow = asyncio.create_task(
            http.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "sleep:3"}]},
            )
        )
        for _ in range(100):  # wait until the slow turn actually holds the slot
            if app.state.backend.health()["active_turns"]:
                break
            await asyncio.sleep(0.05)
        busy = await http.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
        )
        assert busy.status_code == 503 and busy.json()["error"]["code"] == "busy"
        assert busy.headers.get("retry-after")
        assert (await slow).status_code == 200


async def test_graceful_shutdown_cancels_turns(workspace: Path, engine: str) -> None:
    import asyncio
    import time

    settings = make_settings(workspace, engine=engine, shutdown_grace=5)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=30,
        ) as http:
            slow = asyncio.create_task(
                http.post(
                    "/v1/chat/completions",
                    json={"model": "x", "messages": [{"role": "user", "content": "slow"}]},
                )
            )
            await asyncio.sleep(0.5)
            started = time.monotonic()
            await app.state.backend.stop()
            assert time.monotonic() - started < 4
            response = await slow
            assert response.status_code == 200
            assert response.json()["choices"][0]["finish_reason"] == "stop"
            assert "six" not in response.json()["choices"][0]["message"]["content"]


async def test_unsupported_endpoints_return_501(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/embeddings", json={"model": "x", "input": "hi"})
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "not_implemented"
    assert (await client.get("/v1/files")).status_code == 501


async def test_warmup_loads_models(client: httpx.AsyncClient) -> None:
    import asyncio

    for _ in range(50):
        if client.app.state.backend.health()["models_cached"]:  # type: ignore[attr-defined]
            break
        await asyncio.sleep(0.1)
    assert client.app.state.backend.health()["models_cached"] == 3  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- native MCP tool bridge


@pytest.fixture
async def mcp_client(workspace: Path, engine: str):
    settings = make_settings(workspace, engine=engine, tool_mode="mcp", mcp_batch_window=0.3)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=60,
        ) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


READ_TOOL = {
    "type": "function",
    "function": {
        "name": "Read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"file_path": {"type": "string"}}},
    },
}
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Run",
        "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}},
    },
}


async def test_mcp_bridge_chat_roundtrip(mcp_client: httpx.AsyncClient) -> None:
    first = await mcp_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": 'mcp:Read:{"file_path": "notes.txt"}'}],
            "tools": [READ_TOOL, BASH_TOOL],
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    message = body["choices"][0]["message"]
    assert body["choices"][0]["finish_reason"] == "tool_calls"
    assert message["content"] == "Calling the tool."
    call = message["tool_calls"][0]
    assert call["function"]["name"] == "Read" and json.loads(call["function"]["arguments"]) == {
        "file_path": "notes.txt"
    }
    assert body["kiro"]["tool_mode"] == "mcp" and body["kiro"]["agent"] == "kiro_planner"
    # The Kiro turn is still open; deliver the result and get the continuation.
    second = await mcp_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [
                {"role": "user", "content": 'mcp:Read:{"file_path": "notes.txt"}'},
                message,
                {"role": "tool", "tool_call_id": call["id"], "content": "hello from notes"},
            ],
            "tools": [READ_TOOL, BASH_TOOL],
        },
    )
    assert second.status_code == 200, second.text
    body2 = second.json()
    assert body2["choices"][0]["message"]["content"] == "result: hello from notes"
    assert body2["choices"][0]["finish_reason"] == "stop"
    assert body2["kiro"]["session_id"] == body["kiro"]["session_id"]
    assert mcp_client.app.state.backend.health()["live_sessions"] == 1  # type: ignore[attr-defined]


async def test_mcp_bridge_parallel_calls_streaming_anthropic(mcp_client: httpx.AsyncClient) -> None:
    tools = [
        {
            "name": "Read",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
        },
        {
            "name": "Bash",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}},
        },
    ]
    first = await mcp_client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "stream": True,
            "tools": tools,
            "messages": [{"role": "user", "content": "mcp2"}],
        },
    )
    events = sse_events(first.text)
    starts = [e[1]["content_block"] for e in events if e[0] == "content_block_start"]
    tool_uses = [b for b in starts if b["type"] == "tool_use"]
    assert [b["name"] for b in tool_uses] == ["Read", "Bash"]
    assert (
        next(e[1] for e in events if e[0] == "message_delta")["delta"]["stop_reason"] == "tool_use"
    )
    assistant_content = [{"type": "text", "text": "Calling the tool."}] + [
        {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {"n": i + 1}}
        for i, b in enumerate(tool_uses)
    ]
    results = [
        {"type": "tool_result", "tool_use_id": b["id"], "content": f"out{i}"}
        for i, b in enumerate(tool_uses)
    ]
    second = await mcp_client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 100,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "mcp2"},
                {"role": "assistant", "content": assistant_content},
                {"role": "user", "content": results},
            ],
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["content"][-1].get("text") == "results: out0 | out1", second.text
    assert second.json()["stop_reason"] == "end_turn"


async def test_mcp_bridge_abandoned_turn_is_cancelled_on_stop(
    mcp_client: httpx.AsyncClient,
) -> None:
    first = await mcp_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": 'mcp:Read:{"file_path": "a"}'}],
            "tools": [READ_TOOL],
        },
    )
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"
    backend = mcp_client.app.state.backend  # type: ignore[attr-defined]
    assert backend.health()["live_sessions"] == 1
    await backend.stop()
    assert backend.health()["live_sessions"] == 0


async def test_mcp_bridge_large_tool_list(mcp_client: httpx.AsyncClient) -> None:
    """Claude Code sends dozens of tools whose schemas exceed 64 KB in one message."""
    big = "x" * 4000
    tools = [READ_TOOL] + [
        {
            "type": "function",
            "function": {
                "name": f"Tool{i}",
                "description": big,
                "parameters": {
                    "type": "object",
                    "properties": {"p": {"type": "string", "description": big}},
                },
            },
        }
        for i in range(30)
    ]
    first = await mcp_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": 'mcp:Read:{"file_path": "big"}'}],
            "tools": tools,
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["choices"][0]["finish_reason"] == "tool_calls"


async def test_mcp_bridge_no_tools_uses_agent_mode(mcp_client: httpx.AsyncClient) -> None:
    response = await mcp_client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "who"}]},
    )
    assert response.json()["kiro"].get("tool_mode") is None
    assert response.json()["kiro"]["agent"] == "kiro_default"


# --------------------------------------------------------------------------- legacy completions


async def test_legacy_completions(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/completions", json={"model": "x", "prompt": "echo: continued", "echo": True}
    )
    body = response.json()
    assert body["object"] == "text_completion"
    assert body["choices"][0]["text"] == "echo: continuedcontinued"
    streamed = await client.post(
        "/v1/completions", json={"model": "x", "prompt": "echo: s", "stream": True}
    )
    chunks = [e[1] for e in sse_events(streamed.text) if isinstance(e[1], dict)]
    assert "".join(c["choices"][0]["text"] for c in chunks) == "s"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


# --------------------------------------------------------------------------- responses


async def test_responses_basic_and_previous_id(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/responses", json={"model": "x", "instructions": "be nice", "input": "echo: first"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "response" and body["status"] == "completed"
    assert body["output"][-1]["type"] == "message"
    assert body["output"][-1]["content"][0]["text"] == "first"
    assert body["usage"]["total_tokens"] > 0
    follow = await client.post(
        "/v1/responses",
        json={
            "model": "x",
            "previous_response_id": body["id"],
            "input": [{"role": "user", "content": [{"type": "input_text", "text": "history?"}]}],
        },
    )
    assert follow.status_code == 200
    assert "echo: first" in follow.json()["output"][-1]["content"][0]["text"]
    fetched = await client.get(f"/v1/responses/{body['id']}")
    assert fetched.status_code == 200 and fetched.json()["id"] == body["id"]
    missing = await client.post(
        "/v1/responses", json={"model": "x", "previous_response_id": "resp_nope", "input": "x"}
    )
    assert missing.status_code == 404


async def test_responses_function_calls(client: httpx.AsyncClient) -> None:
    tools = [
        {
            "type": "function",
            "name": "shell",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        }
    ]
    call = '<tool_call>{"name": "shell", "arguments": {"cmd": "ls"}}</tool_call>'
    response = await client.post(
        "/v1/responses", json={"model": "x", "input": "echo: " + call, "tools": tools}
    )
    body = response.json()
    fc = [item for item in body["output"] if item["type"] == "function_call"]
    assert fc and fc[0]["name"] == "shell" and json.loads(fc[0]["arguments"]) == {"cmd": "ls"}
    follow = await client.post(
        "/v1/responses",
        json={
            "model": "x",
            "tools": tools,
            "input": [
                {"role": "user", "content": "echo: " + call},
                fc[0],
                {"type": "function_call_output", "call_id": fc[0]["call_id"], "output": "a.txt"},
                {"role": "user", "content": "history?"},
            ],
        },
    )
    assert follow.status_code == 200
    assert follow.json()["kiro"]["reused_session"] is True


async def test_responses_codex_item_types(client: httpx.AsyncClient) -> None:
    """Codex sends namespace tool groups, additional_tools items, and item types we don't know."""
    tools = [
        {
            "type": "function",
            "name": "exec_command",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
        },
        {
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [{"type": "function", "name": "spawn_agent", "parameters": {}}],
        },
        {"type": "web_search"},
    ]
    call = '<tool_call>{"name": "spawn_agent", "arguments": {}}</tool_call>'
    body = {
        "model": "x",
        "tools": tools,
        "input": [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "dev"}],
            },
            {
                "type": "additional_tools",
                "tools": [{"type": "function", "name": "plugin_tool", "parameters": {}}],
            },
            {"type": "some_future_item", "payload": 1},
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "echo: " + call}],
            },
        ],
    }
    response = await client.post("/v1/responses", json=body)
    assert response.status_code == 200, response.text
    fc = [item for item in response.json()["output"] if item["type"] == "function_call"]
    assert fc and fc[0]["name"] == "spawn_agent"


EXEC_TOOL = {
    "type": "custom",
    "name": "exec",
    "description": "Run JavaScript to call nested tools such as tools.exec_command(...).",
    "format": {
        "type": "grammar",
        "syntax": "lark",
        "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/",
    },
}
EXEC_JS = 'await tools.apply_patch("*** Begin Patch\\n*** End Patch");'


async def test_responses_custom_tool_roundtrip(client: httpx.AsyncClient) -> None:
    """Codex code mode: the whole workspace is one freeform ``custom`` tool named ``exec``."""
    call = json.dumps({"name": "exec", "arguments": {"input": EXEC_JS}})
    body = {
        "model": "x",
        "tools": [EXEC_TOOL, {"type": "function", "name": "wait", "parameters": {}}],
        "input": "echo: <tool_call>" + call + "</tool_call>",
    }
    response = await client.post("/v1/responses", json=body)
    assert response.status_code == 200, response.text
    items = [i for i in response.json()["output"] if i["type"] == "custom_tool_call"]
    assert items and items[0]["name"] == "exec" and items[0]["input"] == EXEC_JS
    assert items[0]["id"].startswith("ctc_") and items[0]["call_id"]
    assert not [i for i in response.json()["output"] if i["type"] == "function_call"]
    follow = await client.post(
        "/v1/responses",
        json={
            **body,
            "input": [
                {"role": "user", "content": body["input"]},
                items[0],
                {"type": "custom_tool_call_output", "call_id": items[0]["call_id"], "output": "ok"},
                {"role": "user", "content": "history?"},
            ],
        },
    )
    assert follow.status_code == 200, follow.text
    assert follow.json()["kiro"]["reused_session"] is True


async def test_responses_custom_tool_streaming(client: httpx.AsyncClient) -> None:
    call = json.dumps({"name": "exec", "arguments": {"input": EXEC_JS}})
    response = await client.post(
        "/v1/responses",
        json={
            "model": "x",
            "tools": [EXEC_TOOL],
            "stream": True,
            "input": "echo: <tool_call>" + call + "</tool_call>",
        },
    )
    events = sse_events(response.text)
    names = [e[0] for e in events]
    assert "response.custom_tool_call_input.delta" in names
    done = next(e[1] for e in events if e[0] == "response.custom_tool_call_input.done")
    assert done["input"] == EXEC_JS
    assert "response.function_call_arguments.done" not in names
    final = events[-1][1]["response"]
    assert final["output"][-1]["type"] == "custom_tool_call"


async def test_responses_custom_tool_over_mcp_bridge(mcp_client: httpx.AsyncClient) -> None:
    """The bridge advertises a custom tool as a one-argument function; the reply is a custom_tool_call."""
    js = json.dumps({"input": EXEC_JS})
    response = await mcp_client.post(
        "/v1/responses", json={"model": "x", "tools": [EXEC_TOOL], "input": "mcp:exec:" + js}
    )
    assert response.status_code == 200, response.text
    items = [i for i in response.json()["output"] if i["type"] == "custom_tool_call"]
    assert items and items[0]["input"] == EXEC_JS
    second = await mcp_client.post(
        "/v1/responses",
        json={
            "model": "x",
            "tools": [EXEC_TOOL],
            "input": [
                {"role": "user", "content": "mcp:exec:" + js},
                *response.json()["output"],
                {
                    "type": "custom_tool_call_output",
                    "call_id": items[0]["call_id"],
                    "output": "done",
                },
            ],
        },
    )
    assert second.status_code == 200, second.text
    text = second.json()["output"][-1]["content"][0]["text"]
    assert "result: done" in text


async def test_chat_custom_tool(client: httpx.AsyncClient) -> None:
    call = json.dumps({"name": "exec", "arguments": {"input": EXEC_JS}})
    tools = [{"type": "custom", "custom": {"name": "exec", "description": "Run JS"}}]
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "tools": tools,
            "messages": [{"role": "user", "content": "echo: <tool_call>" + call + "</tool_call>"}],
        },
    )
    assert response.status_code == 200, response.text
    message = response.json()["choices"][0]["message"]
    tool_call = message["tool_calls"][0]
    assert tool_call["type"] == "custom" and tool_call["custom"] == {
        "name": "exec",
        "input": EXEC_JS,
    }
    follow = await client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "tools": tools,
            "messages": [
                {"role": "user", "content": "echo: <tool_call>" + call + "</tool_call>"},
                message,
                {"role": "tool", "tool_call_id": tool_call["id"], "content": "ok"},
                {"role": "user", "content": "history?"},
            ],
        },
    )
    assert follow.status_code == 200, follow.text
    assert follow.json()["kiro"]["reused_session"] is True


async def test_refusal_is_surfaced(client: httpx.AsyncClient) -> None:
    """Kiro's CONTENT_FILTERED metadata becomes a refusal finish reason, never a retry."""
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "refuse"}]},
    )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["choices"][0]["finish_reason"] == "content_filter"
    assert body["kiro"]["refusal"]["category"] == "content_filter"
    assert body["kiro"]["recommended_model"] == "claude-opus-4.8"
    anthropic = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 50, "messages": [{"role": "user", "content": "refuse"}]},
    )
    assert anthropic.json()["stop_reason"] == "refusal"


async def test_image_too_large_is_rejected(client: httpx.AsyncClient) -> None:
    client.app.state.backend.settings.max_image_bytes = 1024  # type: ignore[attr-defined]
    try:
        data = "A" * 4000  # ~3000 decoded bytes
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "x",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "echo: hi"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64," + data},
                            },
                        ],
                    }
                ],
            },
        )
    finally:
        client.app.state.backend.settings.max_image_bytes = 0  # type: ignore[attr-defined]
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "image_too_large"


@pytest.mark.parametrize(
    ("message", "status", "code"),
    [
        ("A prompt is already in progress for this session", 409, "session_busy"),
        ("Invalid model ID: gpt-9", 400, "invalid_model"),
        ("This model is not enabled for your account", 403, "model_not_entitled"),
        ("Your monthly usage limit has been reached", 429, "usage_limit"),
        ("ThrottlingException: rate exceeded", 429, "rate_limited"),
        ("The model 'claude-opus-4.8' is not available right now", 503, "model_unavailable"),
        ("The model you've selected is temporarily unavailable.", 503, "model_unavailable"),
        ("Improperly formed request", 400, "malformed_request"),
        ("Kiro failed to generate a response", 503, "kiro_unavailable"),
        ("request timed out after 30s", 504, "kiro_timeout"),
        ("Not signed in. Run kiro-cli login", 502, "kiro_auth"),
        ("HTTP status 403 from backend", 502, "kiro_auth"),
        ("ECONNRESET while streaming", 502, "kiro_connection"),
        ("something unexpected", 502, "kiro_error"),
    ],
)
def test_classify_kiro_error(message: str, status: int, code: str) -> None:
    from kiro_acp.gateway.backend import classify_kiro_error

    got_status, _, got_code, _ = classify_kiro_error(message)
    assert (got_status, got_code) == (status, code)


async def test_codex_catalog_served_for_client_version(client: httpx.AsyncClient) -> None:
    """Codex asks GET /models?client_version=... and expects its own catalogue format."""
    backend = client.app.state.backend  # type: ignore[attr-defined]

    def fake_fetch(version):
        return {
            "models": [
                {
                    "slug": "gpt-5.6-luna",
                    "base_instructions": "You are Codex.",
                    "tool_mode": "code",
                    "shell_type": "unified_exec",
                }
            ]
        }, f"rust-v{version}"

    backend.codex_catalog._fetch = fake_fetch
    response = await client.get("/v1/models?client_version=0.154.0")
    assert response.status_code == 200, response.text
    body = response.json()
    slugs = [m["slug"] for m in body["models"]]
    assert "claude-opus-4.8" in slugs and "gpt-5.6-terra" in slugs
    entry = body["models"][0]
    assert entry["tool_mode"] == "direct" and entry["use_responses_lite"] is False
    assert entry["base_instructions"] == "You are Codex."
    # Ordinary clients still get the OpenAI list; the Anthropic router never serves it.
    plain = await client.get("/v1/models")
    assert plain.json()["object"] == "list"
    anthropic = await client.get("/anthropic/v1/models?client_version=0.154.0")
    assert "models" not in anthropic.json()


async def test_codex_catalog_falls_back_when_offline(client: httpx.AsyncClient) -> None:
    backend = client.app.state.backend  # type: ignore[attr-defined]

    def failing_fetch(version):
        raise RuntimeError("no network")

    backend.codex_catalog._fetch = failing_fetch
    response = await client.get("/v1/models?client_version=0.154.0")
    assert response.status_code == 200 and response.json()["object"] == "list"


async def test_acp_errors_outside_turns_are_classified(client: httpx.AsyncClient) -> None:
    from kiro_acp.acp.errors import ACPProcessError

    backend = client.app.state.backend  # type: ignore[attr-defined]

    async def not_logged_in(*, force: bool = False):
        raise ACPProcessError("agent stdout closed\nerror: You are not logged in, please log in")

    backend.models = not_logged_in
    response = await client.get("/v1/models")
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "kiro_auth"


MCP_CATALOGUE = json.dumps(
    {
        "mcpServers": {
            "time": {"command": "uvx", "args": ["mcp-server-time"], "env": {"TZ": "UTC"}},
            "docs": {"type": "http", "url": "https://docs.example/mcp", "headers": {"X-Key": "k"}},
            "off": {"command": "x", "disabled": True},
        }
    }
)


@pytest.fixture
async def mcp_catalogue_client(workspace: Path, engine: str, tmp_path: Path):
    (workspace / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"repo": {"command": "npx", "args": ["-y", "repo-mcp"]}}})
    )
    (workspace / "opencode.json").write_text(
        json.dumps(
            {"mcp": {"oc": {"type": "local", "command": ["node", "oc.js"], "enabled": True}}}
        )
    )
    settings = make_settings(
        workspace,
        engine=engine,
        mcp_servers=MCP_CATALOGUE,
        mcp_servers_default=["time"],
        mcp_discovery=True,
    )
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=60,
        ) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


async def test_mcp_servers_default_discovered_and_requested(
    mcp_catalogue_client: httpx.AsyncClient,
) -> None:
    async def names(**extra):
        response = await mcp_catalogue_client.post(
            "/v1/chat/completions",
            json={"model": "x", "messages": [{"role": "user", "content": "mcp?"}], **extra},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        return body["choices"][0]["message"]["content"], body["kiro"].get("mcp_servers")

    # default catalogue entry + everything discovered in the workspace
    text, listed = await names()
    assert text == "mcp servers: time, repo, oc" and listed == ["time", "repo", "oc"]
    # a request adds a catalogue name; disabled entries are not in the catalogue
    text, _ = await names(kiro={"mcp_servers": ["docs"]})
    assert "docs" in text
    bad = await mcp_catalogue_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "mcp?"}],
            "kiro": {"mcp_servers": ["off"]},
        },
    )
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "unknown_mcp_server"
    # inline definitions are refused unless allowed
    inline = await mcp_catalogue_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "mcp?"}],
            "kiro": {"mcp_servers": [{"name": "adhoc", "command": "evil"}]},
        },
    )
    assert inline.status_code == 403 and inline.json()["error"]["code"] == "mcp_server_not_allowed"
    # header form
    header = await mcp_catalogue_client.post(
        "/v1/chat/completions",
        headers={"X-Kiro-MCP-Servers": "docs"},
        json={"model": "x", "messages": [{"role": "user", "content": "mcp?"}]},
    )
    assert "docs" in header.json()["choices"][0]["message"]["content"]


async def test_mcp_servers_ignored_for_harness_requests(
    mcp_catalogue_client: httpx.AsyncClient,
) -> None:
    response = await mcp_catalogue_client.post(
        "/v1/chat/completions",
        json={
            "model": "x",
            "messages": [{"role": "user", "content": "echo: hi"}],
            "tools": [READ_TOOL],
            "kiro": {"mcp_servers": ["docs"]},
        },
    )
    assert response.status_code == 200
    assert "mcp_servers" not in response.json()["kiro"]


async def test_inline_agent(client: httpx.AsyncClient, engine: str) -> None:
    body = {
        "model": "x",
        "messages": [{"role": "user", "content": "agent?"}],
        "kiro": {"agent": {"prompt": "You are a haiku bot.", "tools": ["read"]}},
    }
    response = await client.post("/v1/chat/completions", json=body)
    if engine == "v2":
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "agent_requires_v3"
        return
    assert response.status_code == 200, response.text
    reply = response.json()
    text = reply["choices"][0]["message"]["content"]
    assert text.startswith("mode gateway-inline-") and "prompt: You are a haiku bot." in text
    assert "tools: read" in text
    assert reply["kiro"]["agent"].startswith("gateway-inline-")
    # Anthropic route, same extension; invalid definitions are 400
    anthropic = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 50,
            "messages": [{"role": "user", "content": "agent?"}],
            "kiro": {"agent": {"prompt": "Be brief."}},
        },
    )
    assert (
        anthropic.status_code == 200
        and "prompt: Be brief." in anthropic.json()["content"][0]["text"]
    )
    invalid = await client.post(
        "/v1/chat/completions", json={**body, "kiro": {"agent": {"tools": "read"}}}
    )
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "invalid_agent"


async def test_harness_agent_sent_over_the_wire_on_v3(workspace: Path, engine: str) -> None:
    if engine != "v3":
        pytest.skip("v3 only")
    settings = make_settings(
        workspace, engine="v3", harness_engine="v3", harness_agent="kiro-gateway-harness"
    )
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw", headers={"Authorization": "Bearer secret"}
        ) as http:
            response = await http.post(
                "/v1/chat/completions",
                json={
                    "model": "x",
                    "messages": [{"role": "user", "content": "agent?"}],
                    "tools": [READ_TOOL],
                },
            )
            assert response.status_code == 200, response.text
            text = response.json()["choices"][0]["message"]["content"]
            assert text.startswith("mode kiro-gateway-harness;") and "tools: " in text


@pytest.fixture
async def stall_client(workspace: Path, engine: str):
    settings = make_settings(workspace, engine=engine, stall_timeout=0.5, stall_recoveries=1)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://gw",
            headers={"Authorization": "Bearer secret"},
            timeout=60,
        ) as http:
            http.app = app  # type: ignore[attr-defined]
            yield http


async def test_stalled_turn_is_cancelled_and_nudged(stall_client: httpx.AsyncClient) -> None:
    response = await stall_client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "stall"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    message = body["choices"][0]["message"]
    assert "resumed after stall" in message["content"]
    assert body["kiro"]["stalls"] == 1 and body["kiro"]["stall_recoveries"] == 1
    assert "Kiro stalled on Running: sleep 999" in (message.get("reasoning_content") or "")
    # the ledger saw the stall and the recovery turn
    audit = await stall_client.get(body["kiro"]["audit"])
    kinds = [r["kind"] for r in audit.json()["records"]]
    assert "stall" in kinds and kinds.count("turn_end") >= 1


async def test_stall_without_recovery_is_an_error(workspace: Path, engine: str) -> None:
    settings = make_settings(workspace, engine=engine, stall_timeout=0.5, stall_recoveries=0)
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw", headers={"Authorization": "Bearer secret"}
        ) as http:
            response = await http.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "stall"}]},
            )
            assert response.status_code == 504
            assert response.json()["error"]["code"] == "kiro_stall"


async def test_audit_ledger_records_and_redacts(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "tool"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    session_id = body["kiro"]["session_id"]
    assert body["kiro"]["audit"] == f"/v1/kiro/sessions/{session_id}/audit"
    listing = await client.get("/v1/kiro/sessions")
    assert any(row["session_id"] == session_id for row in listing.json()["data"])
    audit = await client.get(body["kiro"]["audit"])
    assert audit.status_code == 200
    kinds = [r["kind"] for r in audit.json()["records"]]
    assert kinds[0] == "turn" and "tool_call" in kinds and "permission" in kinds
    assert "tool_result" in kinds and kinds[-1] == "turn_end"
    permission = next(r for r in audit.json()["records"] if r["kind"] == "permission")
    assert permission["decision"] == "allow_once"
    missing = await client.get("/v1/kiro/sessions/nope/audit")
    assert missing.status_code == 404
    from kiro_acp.gateway.audit import redact

    masked = redact(
        {
            "cmd": "curl -H 'Authorization: Bearer abcdefghijklmnop' https://u:pw@host/x",
            "api_key": "sk-1234567890abcdef",
            "nested": {"token": "ghp_ABCDEFGHIJKLMNOPQRSTUV"},
        }
    )
    assert "abcdefghijklmnop" not in masked["cmd"] and "u:pw@" not in masked["cmd"]
    assert masked["api_key"] == "[REDACTED]" and masked["nested"]["token"] == "[REDACTED]"


async def test_frames_are_recorded_and_replayable(
    workspace: Path, engine: str, tmp_path: Path
) -> None:
    record_dir = tmp_path / "frames"
    settings = make_settings(workspace, engine=engine, record_frames=str(record_dir))
    app = create_app(settings, backend=FakeKiroBackend(settings))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw", headers={"Authorization": "Bearer secret"}
        ) as http:
            first = await http.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "tool"}]},
            )
            assert first.status_code == 200
    recordings = [
        path for path in record_dir.glob("acp-*.jsonl") if "session/prompt" in path.read_text()
    ]
    assert recordings, "no recording with a prompt turn was written"
    lines = [json.loads(line) for line in recordings[-1].read_text().splitlines()]
    assert lines[0]["kiro_acp_recording"] == 1 and lines[0]["engine"] == engine
    methods = [entry["frame"].get("method") for entry in lines[1:] if entry["dir"] == "out"]
    assert methods[:2] == ["initialize", "session/new"] and "session/prompt" in methods
    # Replay the recording through a gateway whose "Kiro" is the replay agent.
    replay_cmd = [
        sys.executable,
        str(Path(__file__).parent / "fake_agent" / "replay.py"),
        str(recordings[-1]),
    ]
    settings2 = make_settings(workspace, engine=engine, provision_harness_agent=False)
    backend = FakeKiroBackend(settings2, command=replay_cmd)
    app2 = create_app(settings2, backend=backend)
    async with app2.router.lifespan_context(app2):
        transport = httpx.ASGITransport(app=app2)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gw", headers={"Authorization": "Bearer secret"}
        ) as http:
            replayed = await http.post(
                "/v1/chat/completions",
                json={"model": "x", "messages": [{"role": "user", "content": "tool"}]},
            )
            assert replayed.status_code == 200, replayed.text
            assert (
                replayed.json()["choices"][0]["message"]["content"]
                == first.json()["choices"][0]["message"]["content"]
            )


async def test_metrics_endpoint(client: httpx.AsyncClient) -> None:
    await client.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "echo: hi"}]},
    )
    await client.post("/v1/chat/completions", json={"model": "x", "messages": []})
    unauthenticated = await client.get("/metrics", headers={"Authorization": ""})
    assert unauthenticated.status_code == 401
    response = await client.get("/metrics")
    assert response.status_code == 200
    text = response.text
    assert 'kiro_gateway_turns_total{mode="agent"' in text and 'finish="stop"} 1' in text
    assert "kiro_gateway_turn_seconds_bucket" in text
    assert "kiro_gateway_errors_total{" in text
    assert "kiro_gateway_live_sessions" in text

    response = await client.post(
        "/v1/responses", json={"model": "x", "input": "thought", "stream": True}
    )
    events = sse_events(response.text)
    names = [e[0] for e in events]
    assert names[0] == "response.created" and names[-1] == "response.completed"
    assert (
        "response.output_text.delta" in names and "response.reasoning_summary_text.delta" in names
    )
    final = events[-1][1]["response"]
    assert final["status"] == "completed"
    assert final["output"][-1]["content"][0]["text"] == "after thinking"
    assert final["output"][0]["type"] == "reasoning"


async def test_responses_streaming_error(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/responses", json={"model": "x", "input": "error", "stream": True}
    )
    names = [e[0] for e in sse_events(response.text)]
    assert "response.failed" in names and names[-1] == "error"


# --------------------------------------------------------------------------- anthropic


ANTHROPIC_HEADERS = {"Authorization": "", "x-api-key": "secret", "anthropic-version": "2023-06-01"}


async def test_anthropic_messages_basic(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "claude-sonnet-4-6",
            "max_tokens": 100,
            "system": "sys",
            "messages": [{"role": "user", "content": "echo: hi claude"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["type"] == "message" and body["role"] == "assistant"
    assert body["content"] == [{"type": "text", "text": "hi claude"}]
    assert body["stop_reason"] == "end_turn"
    assert body["usage"]["input_tokens"] > 0
    assert body["model"] == "claude-sonnet-4-6"


async def test_anthropic_tool_use_roundtrip(client: httpx.AsyncClient) -> None:
    tools = [
        {
            "name": "Bash",
            "description": "run",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}},
        },
        {"type": "text_editor_20250728", "name": "str_replace_based_edit_tool"},
        {"type": "web_search_20260209", "name": "web_search"},
    ]
    call = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
    response = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 10,
            "tools": tools,
            "messages": [{"role": "user", "content": "echo: Sure. " + call}],
        },
    )
    body = response.json()
    assert body["stop_reason"] == "tool_use"
    assert body["content"][0] == {"type": "text", "text": "Sure."}
    tool_use = body["content"][1]
    assert (
        tool_use["type"] == "tool_use"
        and tool_use["name"] == "Bash"
        and tool_use["input"] == {"command": "ls"}
    )
    follow = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 10,
            "tools": tools,
            "messages": [
                {"role": "user", "content": "echo: Sure. " + call},
                {"role": "assistant", "content": body["content"]},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use["id"],
                            "content": [{"type": "text", "text": "a.txt"}],
                        },
                        {"type": "text", "text": "history?"},
                    ],
                },
            ],
        },
    )
    assert follow.status_code == 200, follow.text
    assert follow.json()["kiro"]["reused_session"] is True


async def test_anthropic_streaming_with_thinking(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 10,
            "stream": True,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": "thought"}],
        },
    )
    events = sse_events(response.text)
    names = [e[0] for e in events]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    blocks = [e[1]["content_block"]["type"] for e in events if e[0] == "content_block_start"]
    assert blocks == ["thinking", "text"]
    deltas = [e[1]["delta"] for e in events if e[0] == "content_block_delta"]
    assert any(d["type"] == "thinking_delta" and d["thinking"] == "thinking hard" for d in deltas)
    assert "".join(d["text"] for d in deltas if d["type"] == "text_delta") == "after thinking"
    message_delta = next(e[1] for e in events if e[0] == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "end_turn"


async def test_anthropic_streaming_tool_use(client: httpx.AsyncClient) -> None:
    call = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
    response = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={
            "model": "x",
            "max_tokens": 10,
            "stream": True,
            "tools": [{"name": "Bash", "input_schema": {}}],
            "messages": [{"role": "user", "content": "echo: " + call}],
        },
    )
    events = sse_events(response.text)
    starts = [e[1]["content_block"] for e in events if e[0] == "content_block_start"]
    assert starts[-1]["type"] == "tool_use" and starts[-1]["name"] == "Bash"
    partial = next(
        e[1]["delta"]["partial_json"]
        for e in events
        if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "input_json_delta"
    )
    assert json.loads(partial) == {"command": "ls"}
    assert (
        next(e[1] for e in events if e[0] == "message_delta")["delta"]["stop_reason"] == "tool_use"
    )


async def test_anthropic_errors_and_count_tokens(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 10, "messages": []},
    )
    assert response.status_code == 400
    assert response.json() == {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "'messages' must be a non-empty array",
        },
    }
    failed = await client.post(
        "/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "error"}]},
    )
    assert failed.status_code == 502 and failed.json()["type"] == "error"
    count = await client.post(
        "/v1/messages/count_tokens",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "messages": [{"role": "user", "content": "hello world"}]},
    )
    assert count.json()["input_tokens"] >= 1
    prefixed = await client.post(
        "/anthropic/v1/messages",
        headers=ANTHROPIC_HEADERS,
        json={"model": "x", "max_tokens": 10, "messages": [{"role": "user", "content": "echo: p"}]},
    )
    assert prefixed.json()["content"][0]["text"] == "p"
