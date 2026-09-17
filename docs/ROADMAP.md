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

### 9. Native tool bridging over MCP — L
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

### 11. Per-request workspace selection (opt-in, allow-listed) — M
Keep the server-controlled default, but allow `X-Kiro-Workspace` when the value is under
one of `KIRO_GATEWAY_ALLOWED_WORKSPACES`. Sessions are keyed by workspace in the pool.
Enables one gateway to serve several projects in agent mode without weakening the jail.

### 12. Structured-output validation — S
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

### 15. Rate limiting and queue timeouts — S
Per-key request rate limit and a bounded wait for a concurrency slot
(`KIRO_GATEWAY_QUEUE_TIMEOUT`) returning 429/503 with `Retry-After` instead of hanging.

### 16. Graceful shutdown — S
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

## Deliberately not planned

- Inferring the workspace from message text or unvalidated headers (a jail, not an anchor).
- A single shared `kiro-cli` process for all sessions (a wedged turn would block everyone).
- Fabricating exact token counts; estimates stay labelled as estimates.
