"""Native tool bridging: expose a client's tools to Kiro as an MCP server.

Kiro cannot accept client tool definitions over ACP, but it can run MCP servers
passed in ``session/new``. The gateway therefore spawns (through Kiro) a small
stdio MCP server per harness session (:mod:`server`) that advertises the client's
tools. When the model calls one, the server forwards the call to the gateway over
a Unix socket (:mod:`broker`), the gateway returns it to the HTTP client as a
normal ``tool_use`` / ``function_call``, and the client's result is delivered
back as the MCP tool's response, so Kiro's turn continues natively.
"""
