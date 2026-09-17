# Architecture

## Layers

```text
┌──────────────────────────────────────────────────────────────────────┐
│ kiro-gateway (FastAPI)                                               │
│  protocols/openai_chat  protocols/openai_responses                   │
│  protocols/openai_completions  protocols/anthropic  protocols/models │
│            │ Conversation (protocol-neutral)                         │
│            ▼                                                         │
│  backend.KiroBackend ── session pool, prefix affinity, model         │
│            │             resolution, tool emulation, output events   │
├────────────┼─────────────────────────────────────────────────────────┤
│ kiro_acp.acp (library)                                               │
│  kiro.KiroAgent ── launches kiro-cli acp, hides v2/v3 differences    │
│  session.Session ── prompt turns → TurnEvent stream                  │
│  client.ACPClient ── JSON-RPC over stdio, dispatch, subscriptions    │
│  handlers ── PermissionPolicy, LocalFileSystem, LocalTerminals       │
├──────────────────────────────────────────────────────────────────────┤
│ kiro-acp (CLI) ── thin wrapper over KiroAgent/Session with renderers │
└──────────────────────────────────────────────────────────────────────┘
```

## ACP client

`ACPClient` owns one agent subprocess. A reader task parses newline-delimited JSON-RPC
from stdout and routes each message:

- **Responses** resolve the future registered by `request()`. Request ids are integers
  issued by the client; agent-issued request ids live in a separate space, so collisions
  are impossible.
- **Agent requests** (`session/request_permission`, `fs/*`, `terminal/*`,
  `_kiro/terminal/shell_type`, anything registered with `register_request_handler`) are
  dispatched on their own tasks so the agent can have several in flight while a prompt is
  running. Unknown methods get JSON-RPC `-32601`.
- **Notifications** are broadcast to per-session subscriber queues (matched on
  `params.sessionId`) and to global listeners. `tool_call`/`tool_call_update` payloads are
  merged into per-session `ToolCall` state so permission requests can be enriched.

Process exit fails all pending requests with `ACPProcessError` (including the stderr tail)
and pushes an `_client/agent_exited` notification so running turns end cleanly.

`Session` holds a persistent subscription from creation, so notifications sent between
turns (the v3 engine's late `config_option_update`) are not lost. `Session.prompt()`
issues `session/prompt` without a request timeout, then multiplexes the response future
with the notification queue, translating updates into `TurnEvent`s. A turn timeout sends
`session/cancel` and waits for the agent's `cancelled` stop reason.

## Kiro engines

`KiroLaunchOptions.command()` builds the `kiro-cli acp` argv. The v3 engine rejects
`--model`, `--effort`, `--agent`, and `--trust-all-tools`; `KiroAgent.new_session` applies
those through ACP instead (`set_config_option` for model and autopilot, `set_mode` for the
agent). `SessionInfo.from_result` normalizes both engines' ways of advertising models and
modes. See `KIRO_ACP_NOTES.md` for the observed wire behaviour.

## Gateway data flow

1. A protocol adapter validates the HTTP body and builds a `Conversation`
   (system text, `Message`s with text/image/tool-call/tool-result parts, `ToolDef`s,
   tool choice, JSON-output request, effort).
2. `KiroBackend.resolve_model` maps the requested model to a Kiro model id.
3. `KiroBackend.run` acquires a session:
   - **affinity**: hash the conversation prefix up to and including the last assistant
     message; if a pooled session finished its last turn with that fingerprint (and the
     same model, agent, permissions, effort), reuse it and send only the new messages.
   - otherwise spawn `kiro-cli acp`, create a session, and render the whole conversation
     as a transcript preceded by the system text (and the tool protocol when emulating).
4. Kiro's `TurnEvent`s become `OutputText`, `OutputThought`, `OutputToolCall`, and
   `OutputDone`. Text passes through `ToolCallParser`, which extracts
   `<tool_call>{...}</tool_call>` blocks when client tools are being emulated.
5. After a successful turn the session is returned to the pool keyed by the fingerprint
   the client will present next (`Conversation.fingerprint_after`). Fingerprints use a
   canonical, id-free form so a client echoing our reply hashes identically.
6. The adapter encodes output events as JSON or SSE in its protocol's shape.

If the HTTP client disconnects mid-stream, the generator is closed, `session/cancel` is
sent, and the session is discarded (its state would be ambiguous).

Concurrency is bounded by a semaphore (`max_concurrency`); the pool is bounded by
`max_sessions` with LRU eviction and an idle reaper.

## Tool emulation

Kiro is an agent with its own tools; ACP has no way to inject a client's tool schemas. The
gateway therefore prepends a protocol description listing the client's tools and asks the
model to emit tool calls as tagged JSON blocks. The parser is incremental and tolerant:
partial tags at chunk boundaries are held back, fenced JSON is accepted, invalid JSON is
passed through as text. Prompting alone is not enough: Kiro's stock agents have native
tools and the models use them in preference to the emulated protocol. Harness-mode turns
therefore run under a tool-less Kiro agent (`harness_agent.py` provisions
`~/.kiro/agents/kiro-gateway-harness.json` with `"tools": []`), and any remaining
permission requests default to `deny` (`harness_permissions`), so the harness remains the
only actor on the workspace.

## Testing

`tests/fake_agent/agent.py` is a scripted ACP agent (v2 or v3 flavour via
`FAKE_ACP_ENGINE`) that exercises every code path deterministically: streaming text,
thoughts, tool calls with permission requests, client fs/terminal calls, cancellation,
errors, crashes, session load/list. The gateway tests run the whole HTTP stack over it
with `httpx.ASGITransport`. Integration tests against the real `kiro-cli` are opt-in
(`KIRO_INTEGRATION=1`).
