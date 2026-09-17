from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_acp.acp import (
    ACPRemoteError,
    ClientHandlers,
    KiroAgent,
    KiroLaunchOptions,
    LocalFileSystem,
    LocalTerminals,
    PermissionDecision,
    PermissionPolicy,
    PermissionRule,
    StopReason,
    TextDelta,
    ThoughtDelta,
    ToolCallEvent,
    TurnComplete,
)
from kiro_acp.acp.session import EffortNotSupported
from tests.conftest import fake_agent_command


def make_agent(workspace: Path, engine: str, **handler_kwargs) -> KiroAgent:
    handlers = ClientHandlers(**handler_kwargs)
    return KiroAgent(
        KiroLaunchOptions(engine=engine, raw_command=fake_agent_command()),
        cwd=workspace,
        handlers=handlers,
    )


async def test_initialize_and_session_info(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        assert agent.info.agent_info.name == "Fake ACP Agent"
        session = await agent.new_session()
        assert session.model_id == "claude-opus-4.8"
        assert "claude-sonnet-4.6" in session.info.model_ids
        assert session.mode_id == "kiro_default"
        assert session.info.mode_ids == ["kiro_default", "kiro_planner"]


async def test_prompt_streams_text_and_metadata(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("echo: hello streaming world")
        assert result.text == "hello streaming world"
        assert result.stop_reason == StopReason.END_TURN
        assert result.metadata["meteringUsage"][0]["value"] == 0.01
        assert result.ok


async def test_set_model_and_mode(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session(model="claude-sonnet-4.6", mode="kiro_planner")
        assert session.model_id == "claude-sonnet-4.6"
        assert session.mode_id == "kiro_planner"
        result = await session.prompt_text("anything")
        assert result.text.startswith("[claude-sonnet-4.6]")
        with pytest.raises(ACPRemoteError, match="Unknown model"):
            await session.set_model("nope")


async def test_effort(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        if engine == "v2":
            await session.set_effort("xhigh")
            assert session.effort == "max"
            # Applied through _kiro.dev/commands/execute, not a prompt turn.
            assert (await session.prompt_text("echo: ping")).text == "ping"
            with pytest.raises(ValueError, match="Unknown effort level"):
                await session.set_effort("medium-rare")
        else:
            with pytest.raises(EffortNotSupported):
                await session.set_effort("high")


async def test_effort_from_launch_is_not_fatal(workspace: Path, engine: str) -> None:
    agent = KiroAgent(
        KiroLaunchOptions(engine=engine, raw_command=fake_agent_command(), effort="high"),
        cwd=workspace,
    )
    async with agent:
        session = await agent.new_session()
        if engine == "v3":
            assert session.effort_error


async def test_tool_call_with_permission_allow(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine, permissions=PermissionPolicy("allow-once")) as agent:
        session = await agent.new_session()
        events = [e async for e in session.prompt([{"type": "text", "text": "tool"}])]
        phases = [e.phase for e in events if isinstance(e, ToolCallEvent)]
        assert phases[0] == "announced"
        assert "started" in phases and phases[-1] == "completed"
        decision = next(e for e in events if isinstance(e, PermissionDecision))
        assert decision.granted and decision.request.title == "Running: ls"
        assert decision.request.tool_name == "shell"
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "done"
        completed = [e for e in events if isinstance(e, ToolCallEvent) and e.phase == "completed"][
            0
        ]
        assert "a.txt" in completed.call.output_text()


async def test_tool_call_denied_by_policy(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine, permissions=PermissionPolicy("deny")) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("tool")
        assert result.text == "permission denied"
        assert result.permissions and not result.permissions[0].granted


async def test_permission_rules(workspace: Path, engine: str) -> None:
    policy = PermissionPolicy("deny", rules=[PermissionRule.parse("allow:kind=execute;tool=sh*")])
    async with make_agent(workspace, engine, permissions=policy) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("tool")
        assert result.text == "done"
        assert result.permissions[0].reason == "rule:allow"


def test_claude_code_style_rules() -> None:
    from kiro_acp.acp.types import PermissionRequest, ToolKind

    def request(
        title: str, kind: str, raw: dict, tool_name: str | None = None
    ) -> PermissionRequest:
        return PermissionRequest(
            session_id="s",
            tool_call_id="c",
            title=title,
            kind=ToolKind(kind),
            tool_name=tool_name,
            raw_input=raw,
            options=[],
            raw={},
        )

    ls = request("Running: ls -la", "execute", {"command": "ls -la"}, "shell")
    rm = request("Running: rm -rf /", "execute", {"command": "rm -rf /"}, "shell")
    read_etc = request("Reading /etc/passwd", "read", {"path": "/etc/passwd"}, "read")
    write_src = request("Creating src/a.py", "edit", {"path": "src/a.py"}, "write")
    assert PermissionRule.parse("allow:Bash(ls*)").matches(ls)
    assert not PermissionRule.parse("allow:Bash(ls*)").matches(rm)
    assert PermissionRule.parse("deny:Bash(rm *)").matches(rm)
    assert PermissionRule.parse("allow:Bash").matches(rm) and not PermissionRule.parse(
        "allow:Bash"
    ).matches(read_etc)
    assert PermissionRule.parse("deny:Read(/etc/*)").matches(read_etc)
    assert PermissionRule.parse("allow:Write(src/*)").matches(write_src)
    assert not PermissionRule.parse("allow:Edit(tests/*)").matches(write_src)
    mcp = request("query", "other", {}, "@db/query")
    assert PermissionRule.parse("allow:mcp__db__query").matches(mcp)
    with pytest.raises(ValueError, match="Unknown tool"):
        PermissionRule.parse("allow:Teleport(x)")


async def test_ask_callback(workspace: Path, engine: str) -> None:
    seen = []

    async def ask(request):
        seen.append(request.title)
        return {"outcome": {"outcome": "selected", "optionId": request.options[0].option_id}}

    async with make_agent(
        workspace, engine, permissions=PermissionPolicy("ask", callback=ask)
    ) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("tool")
        assert result.text == "done" and seen == ["Running: ls"]


async def test_thoughts(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        events = [e async for e in session.prompt([{"type": "text", "text": "thought"}])]
        assert any(isinstance(e, ThoughtDelta) and e.text == "thinking hard" for e in events)
        result = await session.collect(session.prompt([{"type": "text", "text": "thought"}]))
        assert result.thoughts == "thinking hard" and result.text == "after thinking"


async def test_remote_error_becomes_turn_error(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("error")
        assert result.stop_reason == StopReason.ERROR
        assert "Encountered an error" in (result.error or "")


async def test_cancel_and_timeout(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("slow", timeout=0.7)
        assert result.stop_reason == StopReason.CANCELLED
        assert result.text.startswith("one two")
        # A follow-up turn still works after a cancelled one.
        result = await session.prompt_text("echo: ok")
        assert result.text == "ok"


async def test_explicit_cancel(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        chunks: list[str] = []
        async for event in session.prompt([{"type": "text", "text": "slow"}]):
            if isinstance(event, TextDelta):
                chunks.append(event.text)
                if len(chunks) == 2:
                    await session.cancel()
            if isinstance(event, TurnComplete):
                assert event.stop_reason == StopReason.CANCELLED
        assert len(chunks) < 6


async def test_agent_crash_mid_turn(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        result = await session.prompt_text("crash")
        assert result.stop_reason == StopReason.ERROR
        assert "exit status: 3" in (result.error or "")


async def test_filesystem_and_terminal_handlers(workspace: Path, engine: str) -> None:
    async with make_agent(
        workspace,
        engine,
        filesystem=LocalFileSystem(workspace),
        terminals=LocalTerminals(workspace),
    ) as agent:
        assert agent.client.handlers.capabilities() == {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": True,
        }
        session = await agent.new_session()
        assert (await session.prompt_text("fs")).text == "file says: hello from notes\n"
        assert (await session.prompt_text("terminal")).text == "terminal says: hi"


async def test_filesystem_confined_to_root(tmp_path: Path) -> None:
    fs = LocalFileSystem(tmp_path / "root")
    (tmp_path / "root").mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    with pytest.raises(Exception, match="outside the workspace"):
        await fs.read_text_file({"path": str(tmp_path / "secret.txt")})
    await fs.write_text_file({"path": "sub/new.txt", "content": "x"})
    assert (tmp_path / "root" / "sub" / "new.txt").read_text() == "x"
    assert (await fs.read_text_file({"path": "sub/new.txt", "line": 1, "limit": 1}))[
        "content"
    ] == "x"


async def test_load_session_replays_history(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        first = await agent.new_session()
        await first.prompt_text("echo: one")
        loaded = await agent.load_session(first.session_id)
        assert loaded.session_id == first.session_id
        result = await loaded.prompt_text("history?")
        assert "echo: one" in result.text


async def test_session_list(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        await agent.new_session()
        if engine == "v3":
            assert agent.supports_session_list
            assert len(await agent.list_sessions()) == 1
        else:
            assert not agent.supports_session_list


async def test_delete_session(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        if engine == "v3":
            assert agent.supports_session_delete
            assert await agent.delete_session(session.session_id) is True
            assert await agent.list_sessions() == []
            info = await agent.discover()
            assert info.model_ids and await agent.list_sessions() == []
        else:
            assert await agent.delete_session(session.session_id) is False


async def test_image_prompt_block(workspace: Path, engine: str) -> None:
    from kiro_acp.acp.session import image_block

    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        result = await session.collect(session.prompt([image_block("aGk=", "image/png")]))
        assert result.text == "saw 1 image(s): image/png"


async def test_concurrent_turns_are_serialized(workspace: Path, engine: str) -> None:
    async with make_agent(workspace, engine) as agent:
        session = await agent.new_session()
        results = await asyncio.gather(
            session.prompt_text("echo: a"), session.prompt_text("echo: b")
        )
        assert sorted(r.text for r in results) == ["a", "b"]
