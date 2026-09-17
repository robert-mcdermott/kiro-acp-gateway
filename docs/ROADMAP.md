# Roadmap

Status legend: items marked **DONE** are implemented and tested; the rest are open.

Planned improvements, in priority order. Each item lists why it matters, what "done"
looks like, and a rough size (S: under an hour, M: a few hours, L: a day or more).

## P1 — reliability for long-running harness sessions

### 1. SSE keepalive during silent tool runs — DONE — S
Kiro emits nothing while a tool executes, so a long shell command produces a long silent
stream. Claude Code aborts a stream after 300 s of silence and some proxies time out
sooner.
- Wrap every SSE generator so that after N seconds (default 15) without an event it emits
  a protocol-appropriate keepalive: Anthropic `event: ping`, OpenAI `: keepalive` comment
  line (comments are ignored by SSE parsers and the OpenAI SDKs).
- Setting `KIRO_GATEWAY_SSE_KEEPALIVE` (seconds, 0 disables).
- Test: fake agent gains a `sleep:<seconds>` scenario; assert keepalives appear and the
  final JSON is unaffected.

### 2. Model catalogue warm-up at startup — DONE — S
First request currently pays for model discovery (one Kiro process spawn, several
seconds on v3). Run `KiroBackend.models()` in the lifespan after start, in the background
so startup is not blocked, and log the count. Health endpoint already reports
`models_cached`.

### 3. Gateway-side `stop` sequences and `max_tokens` — DONE — M
Kiro ignores both. Add a `StreamLimiter` between the backend and the adapters:
- `stop`/`stop_sequences`: hold back `len(longest_stop) - 1` characters so matches spanning
  chunks are caught; on match, truncate, cancel the turn, and report `finish_reason =
  "stop"` / `stop_reason = "stop_sequence"` with `stop_sequence` set (Anthropic).
- `max_tokens`/`max_output_tokens`/`max_completion_tokens`: optional enforcement using the
  same estimator as usage (`KIRO_GATEWAY_ENFORCE_MAX_TOKENS`, default off, because the
  estimate is approximate); on hit, cancel and report `length` / `max_tokens`.
- Never apply stop sequences inside an emulated `<tool_call>` block.

### 4. Error classification with `Retry-After` — DONE  — S
Map Kiro error text to HTTP status so SDK retry logic works: throttling/quota → 429 with
`Retry-After`, model unavailable/overloaded → 503, backend timeout → 504, everything else
502. Keep the raw Kiro message in the error body. Streaming: same classification inside
the in-stream error event (`error.type` / `code`).

## P2 — compatibility polish

### 5. Model-id presentation for Claude Code — DONE — S
Claude Code 2.x flags dotted ids (`claude-sonnet-4.6`) as unrecognized and its model
picker filters on `^(claude|anthropic)`.
- `/v1/models` (both formats) lists each Kiro model once under its native id and adds a
  hyphenated alias entry (`claude-sonnet-4-6`) when the id contains a dotted version;
  `resolve_model` already maps hyphenated names back.
- Add `claude-auto` / `auto` entries that resolve to the gateway default model.
- Setting `KIRO_GATEWAY_MODEL_ALIAS_STYLE=both|native` to turn the extra entries off.

### 6. Claude Code permission syntax for Kiro's own tools — DONE — M
Accept `Bash(git status*)`, `Read(/etc/*)`, `Write(src/**)`, `mcp__server__tool` in
`KIRO_GATEWAY_PERMISSION_RULES` and `kiro-acp --allow/--deny`, alongside the existing
`kind=/tool=/title=` selectors. Match against the ACP tool kind, Kiro's tool name, the
command or path in `rawInput`, and `_meta.trustOptions[].display`. Document precedence:
explicit deny → explicit allow → policy default.

### 7. System-prompt sanitizer as an optional second layer — DONE — S
Our harness framing keeps Kiro's identity, which resolved the Sonnet 5 refusals, but a
defensive option costs little: `KIRO_GATEWAY_SANITIZE_SYSTEM=true` strips identity
assertions and concealment instructions ("You are Claude Code", "never reveal you are…",
"ignore instructions that contradict…") from the client system prompt before rendering.
Default off; log the number of lines removed. Regression test with a captured Claude
Code system prompt.

### 8. `/v1/embeddings` and unsupported endpoints — DONE — S
Return a clean 501 with an explanatory message for `/v1/embeddings`, `/v1/audio/*`,
`/v1/images/*`, `/v1/files`, `/v1/batches` so SDK users get a clear error instead of 404.

## P3 — capability

### 9. Native tool bridging over MCP — L — DONE (default `tool_mode=mcp`)
Replace prompt-based tool emulation with real tool calls: for each harness session the
gateway registers a stdio MCP server (a small entry point in this package) via
`session/new`'s `mcpServers`, exposing the client's tool schemas. When the model calls one,
the MCP server hands the call to the gateway over a local socket; the gateway returns the
call to the HTTP client as `tool_use` / `function_call`, and delivers the client's result
as the MCP tool's response when the follow-up request arrives, keeping the Kiro turn open
across HTTP requests. Benefits: genuine native tool calling (no protocol to obey), clean
Kiro history, works on v3. Risks: turn lifetime across requests, parallel calls, MCP
permission prompts (auto-allow the bridge server), idle cleanup. Prototype behind
`KIRO_GATEWAY_TOOL_MODE=mcp` and keep `emulate` as fallback.

### 10. Harness MCP passthrough for agent mode — M
Let agent-mode callers give Kiro extra tools: read Claude Code / OpenCode / Codex MCP
config files from the workspace (opt-in, `KIRO_GATEWAY_HARNESS_MCP=true`) and register
them on `session/new`. Normalize entry shapes (HTTP entries need `type: "http"` and
`headers` as an array or `session/new` hangs), bound `session/new` with a timeout, and
retry once with `mcpServers: []`.

### 11. Per-request workspace selection (opt-in, allow-listed) — DONE — M
Keep the server-controlled default, but allow `X-Kiro-Workspace` when the value is under
one of `KIRO_GATEWAY_ALLOWED_WORKSPACES`. Sessions are keyed by workspace in the pool.
Enables one gateway to serve several projects in agent mode without weakening the jail.

### 12. Structured-output validation — DONE — S
When `response_format` / `output_config.format` carries a JSON schema, validate the reply
(after fence stripping) and, on failure, retry once with the validation error appended, then
return the best effort with `kiro.schema_valid=false`.

### 13. Token usage from Kiro when available — S
v3's `session_info_update` context breakdown includes token counts for context files;
capture any per-turn token fields Kiro adds in future releases and prefer them over
estimates, keeping `usage.estimated` accurate.

## P4 — operations and tooling

### 14. Metrics and tracing — M
Prometheus `/metrics` (turns by mode/engine/model, latency, credits, active sessions,
pool hits) and optional OpenTelemetry spans per turn with the Kiro session id. Propagate
`traceparent` into ACP `_meta` per the spec's reserved keys.

### 15. Rate limiting and queue timeouts — DONE — S
Per-key request rate limit and a bounded wait for a concurrency slot
(`KIRO_GATEWAY_QUEUE_TIMEOUT`) returning 429/503 with `Retry-After` instead of hanging.

### 16. Graceful shutdown — DONE — S
On SIGTERM, stop accepting requests, cancel in-flight Kiro turns via `session/cancel`,
delete gateway-owned sessions, and exit within a bounded time.

### 17. `kiro-acp` utility additions — S/M
- `kiro-acp prompt --session-file` to persist and resume the last session id per workspace.
- `kiro-acp chat --jsonl` transcript logging.
- `kiro-acp sessions --prune --cwd-glob` and `--before <date>` selectors.
- `kiro-acp models --engine both` to diff catalogues.
- Shell completion (`--print-completion bash|zsh|fish`).

### 18. Packaging and CI — S
GitHub Actions: `uv sync`, ruff, pytest (fake agent), optional integration job gated on a
secret. Publish wheels with `uv build`; `uv tool install kiro-acp-gateway` from PyPI.
Add a `Dockerfile` that installs kiro-cli and runs the gateway bound to `0.0.0.0` with a
mounted workspace, as the recommended isolation story.

## P5 — Additional things to concider if they improve the gateway

### 19. Richer rendering of Kiro's own tool activity *(pattern)* — DONE — S
Our `tool_activity` lines are just `[kiro:kind] title`. Theirs render arguments as
`key=value` lines, edits as fenced ```diff blocks from `content[].diff`, `execute` output
in fenced blocks, and `search` results as a one-line summary, and they fold Kiro's
todo/plan tool into a `- [ ]` checklist in the reasoning channel. Do the same in
`describe_activity` for both `thought` and `text` modes, with a size cap per event.

### 20. Context window and limits in `/v1/models` *(pattern, Collomia-relevant)* — DONE — S
Collomia reads `context_length` / `max_context_length` from `/v1/models`; OpenAI SDK
clients ignore extra fields. Kiro's model descriptions state the window ("1M context
window"); parse that into `context_length` (default 200k when absent) and add
`max_completion_tokens`. Include the same in the Anthropic listing as `max_input_tokens`
/ `max_tokens`, which newer Anthropic SDKs expose.

### 21. Tool-execution audit ledger with redaction *(pattern)* — M
Per-session bounded record of permission decisions, tool calls, updates, and cancel
races, with secrets masked (bearer tokens, `sk-`/`gh*_`/`AKIA` keys, URL credentials).
Expose as `GET /v1/kiro/sessions/{id}/audit` behind the API key and reference it from
the `kiro` response field. Complements #14 (metrics).

### 22. MCP servers per request and harness MCP discovery *(extends #10)* — M
Accept `X-Kiro-MCP-Servers` / body `mcp_servers` (validated against an allow-list) in
addition to config-file discovery for Claude Code (`~/.claude.json` projects,
`<ws>/.mcp.json`), OpenCode (`opencode.json[c]` `mcp` block), Cursor/VS Code
(`.cursor/mcp.json`, `.vscode/mcp.json`), and Kilo. Agent mode only; harness mode keeps
the tool-less agent. Normalize HTTP entries (`type: "http"`, `headers` as an array).

### 23. Client example configurations *(pattern)* — S
An `examples/clients/` directory with ready-to-use configs and a one-line verification
command for Claude Code, Codex (`kiro-` prefix or `model_catalog_json`), OpenCode, Kilo
Code, Cline/Continue, Hermes, and Collomia, plus the OpenAI and Anthropic SDKs. Each
example notes the recommended model class and which mode (harness/agent) it exercises.

### 24. Codex model catalogue generator — DONE — S
`kiro-acp codex-catalog > ~/.codex/kiro-models.json` emitting a `model_catalog_json`
file for every Kiro model with `tool_mode: "direct"` and `use_responses_lite: false`, so
Codex can use native names without the `kiro-` prefix and without the fallback-metadata
warning. Document the `model_catalog_json = ...` config line.

*Update (2026-09-17):* Codex code mode is now supported natively. OpenAI freeform
`custom` tools (Codex's `exec` code runner) are modelled as one-argument functions and
rendered back as `custom_tool_call` items on `/v1/responses` and `type: "custom"` tool
calls on `/v1/chat/completions`, so catalogue GPT names work without the prefix or the
catalogue file. Both remain useful for model metadata and for forcing direct tools.

### 28. Serve the Codex model catalogue from the gateway — S
Codex 0.154 fetches `GET <base_url>/models?client_version=<ver>` from every provider and
expects the catalogue format (`{"models": [...]}` with `slug`, `tool_mode`, instructions);
it logs a decode error against our OpenAI-style list and falls back to built-in metadata.
Answer that exact request shape (the `client_version` query parameter identifies it) with
the output of the `codex-catalog` generator so Codex gets metadata for every Kiro model
with zero client configuration and no "model metadata not found" warning. Cache the
upstream reference catalogue per Codex version and fall back gracefully offline.

### 25. Document and PDF inputs *(pattern)* — S
Anthropic `document` blocks with base64 PDF sources and OpenAI `file` parts: extract text
locally (`pypdf`, optional dependency) and attach it as a text block; keep the current
clear error when extraction is unavailable.

### 26. Service installation scripts *(pattern)* — S
`scripts/install-service.sh` generating a launchd plist (macOS) or systemd unit (Linux)
that runs `uv run kiro-gateway` with an env file, plus `kiro-gateway --print-service`
to emit the unit for review. Pairs with the Dockerfile in #18.

### 27. Per-model notes in the model listing — S
Record observed capabilities in `/v1/models` descriptions and in
`docs/KIRO_ACP_NOTES.md`: which models emit thought chunks (their finding: opus yes,
sonnet no on v2), which accept `/effort`, and which follow the emulated tool protocol
reliably in emulate mode (Sonnet/Opus yes; GPT 5.6 previews mostly).

## P6 — Patterns from Kiro Crew (kirodotdev/kirocrew, reviewed 2026-09-17)

Kiro Crew is AWS's persistent-workspace product that drives `kiro-cli acp` from Python
(`src/kiro_crew/acp/{runtime,session_handle,_dispatch,types}.py`, `acp/harness/{kiro,kas}.py`).
Its "Gateway" is its own daemon, not an LLM API. It multiplexes many sessions in one
`kiro-cli` process, which this project deliberately does not do, but its wire-level
findings transfer directly. Items are ordered by value for a general-purpose API gateway.

### 29. Effort via `_kiro.dev/commands/execute` on v2 — S
Kiro Crew sets effort with the request
`{"sessionId", "command": {"command": "effort", "args": {...}}}` (object form; the
string form gets no response on 2.14) and reads the outcome from the response
`result.text`. We send `/effort <level>` as a prompt turn, which costs a turn, can
produce assistant text, and cannot be distinguished from a real reply. Switch v2
`set_effort` to the command request with the prompt form as a fallback for older CLIs.

### 30. Wire-injected agents on v3 — M
On the KAS engine `session/new` accepts `_meta.kiro.customAgents: [<agent json>...]`
(max 50) and `session/set_mode` activates one, so no file in `~/.kiro/agents` is needed.
Use it for v3 harness turns and for per-request agent definitions (an API caller could
supply prompt, tools, and MCP servers inline, which #10/#22 want anyway). Keep file
provisioning for v2, which only takes `--agent <name>` at launch.

### 31. Surface model refusals and content filtering — S
`_kiro.dev/metadata` can carry `stopReason: "CONTENT_FILTERED"` and
`refusal: {category, explanation, recommendedModel}`. Map it to OpenAI
`finish_reason: "content_filter"` / Anthropic `stop_reason: "refusal"`, never retry it,
and expose `recommendedModel` under `kiro`. Today the fields ride along in metadata
untyped.

### 32. Finer Kiro error classification — S
Their raw-error classifier (`acp/client.py` ~2612-2800) distinguishes, in precedence
order: unentitled model, usage limit, malformed request, model unavailable / invalid
model id (with the rejected id captured so a retry can substitute an advertised one),
throttle, auth, session expired (401/403, invalid bearer), connection, 5xx/"try again",
and `already in progress` as a distinct busy signal. Ours has four buckets. Add the
model and busy classes (400 `model_not_entitled`, 409 `session_busy`), keep a table test
per class.

### 33. Entitlement probe for the model catalogue — S
`session/new` racing a token refresh answers with the default free-tier model set. Their
fix: a throwaway `session/new` with `mcpServers: []` on the same live process, read
`models`/`configOptions`, terminate it; single-flight with a 20 s TTL; an empty result is
"no evidence" and never replaces a held snapshot. Our cold-start retry is similar but
restarts the process; adopt the same-process probe and the never-overwrite rule in
`KiroBackend.models()`.

### 34. Stall detection and continue-nudge for long turns — M
Beyond the hard `timeout`, add a per-turn watchdog: no event for N seconds while a tool
call is open marks the turn suspect; probe with `session/cancel` (kiro-cli acks a cancel
on a live turn too, so a probe-induced `cancelled` is reclassified as `stale_recover`);
on recovery send a short continue-nudge naming the stalled tool instead of re-sending
the prompt (re-sending re-ran the command that stalled). Applies to agent-mode turns;
harness turns already return on each tool call.

### 35. Process hygiene — S
Spawn `kiro-cli` with `start_new_session=True` (POSIX) / `CREATE_NEW_PROCESS_GROUP`
(Windows) and kill the process group on close, so `kiro-cli-chat` and MCP children never
outlive the gateway (stale `kiro-cli acp` processes were observed after abrupt exits).
Tag children with a marker env var (`KIRO_GATEWAY_SPAWNED=<pid>`) so `kiro-acp doctor`
can list and reap orphans. Bound stdout with a reader limit (already 64 MB).

### 36. Image payload guard — S
An oversized image block wedges a session (their design note
`docs/architecture/design-notes/oversized-image-session-wedge.md`). Downscale or reject
images above a configurable edge/byte limit before `session/prompt`, and only send image
blocks when `promptCapabilities.image` is advertised.

### 37. Frame recorder and replay fixtures — M
`KIRO_GATEWAY_RECORD_FRAMES=<dir>` writes every ACP frame with a provenance header
(cli version, engine, model); a replay harness feeds recorded frames to the client in
tests. Extend the fake agent with `permission`, `gated`, `slow-noack`, `refusal`, and
`maxtokens` scenarios modelled on their `testing/fake_acp_backend.py` markers.

### 38. Context usage and compaction for affinity sessions — S/M
Expose `contextUsagePercentage` (v2 metadata, v3 `session_info_update`
`context_usage`) in every response's `kiro` block and in `kiro-acp chat`. For agent-mode
affinity sessions add an opt-in auto-compaction: send `/compact` as a prompt when usage
crosses a threshold and wait for `_kiro.dev/compaction/status`; a fresh metadata frame
follows about a second later. Harness clients manage their own context, so leave them
alone.

### 39. Trusted tool identity in rules and activity — S
`tool_call._meta.kiro.toolName` is the real `@server/tool` identity and
`rawInput.__tool_use_purpose` is the model's one-line reason; the `title` is
model-authored. Match permission rules against the trusted name (their auto-approve
globs do), and show the purpose line in tool activity rendering.

### 40. Session file load and resume — S
`session/load` accepts `_meta: {"_kiro.dev/session_file": <path>}` and success is
detected by `modes` in the response; `session/resume` exists for engines without load.
Register the update subscription only after `session/load` returns because kiro-cli
replays the transcript. Use it for `kiro-acp chat --resume` on both engines.

### 41. Mid-turn steering (v2) — S, low priority
`_session/steer {sessionId, message: "<user_message>...</user_message>"}` injects a
message into a running turn, confirmed by a `steering_consumed` update. Could back a
"send while streaming" endpoint for agent-mode clients.

### Already covered here (confirmed by the review)
Embedded `role: "system"` messages inside Anthropic `messages` (Claude Code 2.1.215+) are
lifted into the system prompt; `GET /v1/models/{id}` is permissive for clients that probe
it; `previous_response_id` and `store` are implemented rather than rejected; the SSE
keepalive on `/v1/responses` is a comment line, never an invented event type; sessions are
deleted rather than abandoned; per-request effort works on v2 via `/effort`.

Confirmed again by the Kiro Crew review: `mcpServers` is always sent (a missing field
makes kiro-cli exit cleanly with rc 0); permission denials answer the advertised reject
option and only fall back to `cancelled`, which kiro-cli treats as cancelling the whole
turn; credits are read from `_kiro.dev/metadata.meteringUsage` (v2) and
`session_info_update._meta.kiro.turn_completion` (v3); `--auth-method cli` keeps the
`_kiro/auth/getAccessToken` server request inside kiro-cli; `clientInfo.name` is what
kiro-cli reports in telemetry.

## Deliberately not planned

- Inferring the workspace from message text or unvalidated headers (a jail, not an anchor).
- A single shared `kiro-cli` process for all sessions (a wedged turn would block everyone).
- Fabricating exact token counts; estimates stay labelled as estimates.
