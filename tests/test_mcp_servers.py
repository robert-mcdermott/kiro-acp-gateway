import json

import pytest

from kiro_acp.gateway.inline_agent import InlineAgentError, custom_agent, parse_inline_agent
from kiro_acp.gateway.mcp_servers import (
    McpServerError,
    discover,
    normalize_server,
    parse_catalogue,
    servers_from_config,
)


def test_normalize_shapes() -> None:
    stdio = normalize_server("a", {"command": "uvx", "args": ["x"], "env": {"K": "v"}})
    assert stdio == {
        "name": "a",
        "type": "stdio",
        "command": "uvx",
        "args": ["x"],
        "env": [{"name": "K", "value": "v"}],
    }
    opencode = normalize_server(
        "b", {"type": "local", "command": ["node", "s.js"], "environment": {"E": "1"}}
    )
    assert (
        opencode["command"] == "node"
        and opencode["args"] == ["s.js"]
        and opencode["env"][0]["name"] == "E"
    )
    http = normalize_server("c", {"url": "https://x/mcp", "headers": {"A": "b"}})
    assert http == {
        "name": "c",
        "type": "http",
        "url": "https://x/mcp",
        "headers": [{"name": "A", "value": "b"}],
    }
    sse = normalize_server("d", {"type": "sse", "url": "https://x/sse"})
    assert sse["type"] == "sse" and sse["headers"] == []
    with pytest.raises(McpServerError):
        normalize_server("bad name!", {"command": "x"})
    with pytest.raises(McpServerError):
        normalize_server("e", {"url": "ftp://x"})
    with pytest.raises(McpServerError):
        normalize_server("f", {"args": ["no", "command"]})


def test_catalogue_from_file_and_config_shapes(tmp_path) -> None:
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"servers": {"vs": {"type": "stdio", "command": "x"}}}))
    assert list(parse_catalogue(str(path))) == ["vs"]
    assert parse_catalogue("") == {}
    assert list(servers_from_config({"one": {"command": "x"}})) == ["one"]
    with pytest.raises(McpServerError):
        parse_catalogue("{not json")


def test_discover_reads_client_config_files(tmp_path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text('{"mcpServers": {"cur": {"command": "c"}}}')
    (tmp_path / ".vscode").mkdir()
    (tmp_path / ".vscode" / "mcp.json").write_text(
        '{\n  // comment\n  "servers": {"code": {"type": "http", "url": "https://x/m"},},\n}'
    )
    (tmp_path / "opencode.jsonc").write_text(
        '{"mcp": {"oc": {"type": "remote", "url": "https://y"}}}'
    )
    found = discover(str(tmp_path))
    assert set(found) == {"cur", "code", "oc"}
    assert found["code"]["type"] == "http"


def test_inline_agent_parsing() -> None:
    entry = parse_inline_agent({"prompt": "Hi", "name": "bot"})
    assert entry == {"prompt": "Hi", "tools": ["*"], "description": "bot"}
    wire = custom_agent(entry, mcp_servers={"s": {"command": "x"}})
    assert wire["id"].startswith("gateway-inline-") and wire["tools"] == ["*", "@s"]
    assert wire["mcpServers"] == {"s": {"command": "x"}}
    for bad in ({}, {"prompt": ""}, {"prompt": "x", "tools": "read"}, "nope"):
        with pytest.raises(InlineAgentError):
            parse_inline_agent(bad)
