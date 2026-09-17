from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.backend import KiroBackend
from kiro_acp.gateway.config import Settings
from tests.conftest import fake_agent_command


class FakeKiroBackend(KiroBackend):
    """Backend that launches the scripted fake agent instead of kiro-cli."""

    def _make_agent(self, *, permissions, model=None, mode=None, effort=None):
        agent = super()._make_agent(permissions=permissions, model=model, mode=mode, effort=effort)
        agent.options.raw_command = fake_agent_command()
        agent.client.command = agent.options.command()
        return agent


def make_settings(workspace: Path, **overrides) -> Settings:
    defaults = dict(
        workspace=str(workspace),
        api_key="secret",
        permissions="allow-once",
        default_model="claude-haiku-4.5",
        session_idle_ttl=30,
        harness_agent="kiro_planner",
        provision_harness_agent=False,
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
    assert [m["id"] for m in openai["data"]] == [
        "claude-haiku-4.5",
        "claude-sonnet-4.6",
        "gpt-5.6-terra",
    ]
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
    assert response.json()["choices"][0]["message"]["content"].startswith("[claude-haiku-4.5]")


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
    assert "[kiro:execute] Running: ls" in message["reasoning_content"]
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


async def test_responses_streaming(client: httpx.AsyncClient) -> None:
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
