# Kiro CLI ACP behaviour (observed with kiro-cli 2.22.0)

These notes record what `kiro-cli acp` actually sends, which the library relies on. They
were captured with the probe scripts used while building this project.

## Launch flags

```text
kiro-cli acp [--agent-engine v2|v3] [--model ID] [--effort L] [--agent NAME]
             [--trust-all-tools] [--trust-tools A,B] [--auth-method cli] [-v]
```

- `--agent-engine v3` rejects `--model`, `--effort`, `--agent`, `--trust-all-tools`.
- `--auth-method cli` is only valid with v3. Without it, v3 sends the client
  `_kiro/auth/getAccessToken` requests and stalls if they are not answered.

## initialize

| | v2 | v3 |
|---|---|---|
| `agentInfo` | `Kiro CLI Agent 2.22.0` | absent (KAS 0.66 logs to stderr) |
| `loadSession` | true | true |
| `promptCapabilities` | image | image, embeddedContext |
| `mcpCapabilities` | http | http, sse |
| `sessionCapabilities` | `{}` | `list`, `fork` |
| `authMethods` | `[]` | `aws-builder-id`, `aws-iam-identity-center` |
| `_meta.kiro.extensionMethods` | – | `_kiro/session/*`, `_kiro/workflow/*`, `_kiro/knowledge`, ... |

## session/new

- v2 result: `sessionId`, `modes` (Kiro agents, e.g. `kiro_default`, `kiro_planner`), and a
  non-standard `models: {currentModelId, availableModels: [{modelId, name, description}]}`.
- v3 result: `sessionId` (`sess_...`), `modes` (`vibe`, `spec`, `plan`, `autonomous`, ...),
  `configOptions` with `mode` (category `mode`), `model` (category `model`), `autopilot`
  (`on`/`off`), `contentCollection`, and `_meta` with session metadata. On a cold start the
  first session can arrive before the model catalogue is loaded; later sessions have it.
- Before the result, v2 emits `_kiro.dev/commands/available`,
  `_kiro.dev/mcp/server_initialized`, `_kiro.dev/subagent/list_update`; v3 emits
  `session/update` `config_option_update` / `available_commands_update`, `_kiro/mcp/status`,
  `_kiro/governance/state`, `_kiro/tools/didChange`, `_kiro/sessions/changed`.

## Model, mode, effort

| Operation | v2 | v3 |
|---|---|---|
| Select model | `session/set_model {sessionId, modelId}` (accepts unknown ids, fails at prompt time) | `session/set_config_option {configId: "model", value}` (`session/set_model` errors) |
| Select agent | `session/set_mode` | `session/set_mode` or config option `mode` |
| Effort | request `_kiro.dev/commands/execute` `{"sessionId", "command": {"command": "effort", "args": {"value": "high"}}}` → `{"success": true, "message": "Effort set to high"}` (2.22.0, confirmed); older CLIs: prompt text `/effort <level>` → `Effort set to X` | no ACP surface observed; `/effort` is treated as chat |
| Other slash commands | `/model`, `/tools`, `/context` answer as text | treated as chat |

`session/list` exists on v3 only. v3 persists every session it creates under
`~/.kiro/sessions` (plus `~/.kiro/session-index`) and shows them in the `/sessions`
dashboard. It advertises no `delete`/`close` capability and rejects the spec methods, but
the undocumented extension `_kiro/session/delete {sessionId}` works and answers
`{"success": true}`. The library uses it (`KiroAgent.delete_session`), the CLI exposes it
(`kiro-acp sessions --delete/--prune`), and the gateway deletes its own throwaway
sessions by default (`KIRO_GATEWAY_DELETE_SESSIONS`). `session/load` works on both and replays history as
`user_message_chunk` / `agent_message_chunk` / `tool_call` updates.

## Prompt turn updates

- Text: `session/update` → `agent_message_chunk` with `content: {type: text}`.
- v2 announces tools early with `_kiro.dev/session/update` → `tool_call_chunk`
  (`toolCallId`, `title` = tool name, `kind`), then standard `tool_call`
  (`title`, `kind`, `locations`, `rawInput`, `content` diffs, `_meta.kiro.toolName`) and
  `tool_call_update` (`status`, `rawOutput: {items: [{Text}|{Json: {stdout, stderr, exit_status}}]}`).
- v3 sends `tool_call` with `status`, `rawInput`, `locations`, `_meta.kiro.toolOrigin`, then
  several `tool_call_update`s; `rawOutput` is a string or `{message}` and completed calls
  carry `content: [{type: content, content: {type: text}}]`.
- v3 metadata rides on `session_info_update._meta.kiro.kind`: `context_usage`
  (`usagePercentage`), `turn_start`, `turn_end` (`stopReason`), `turn_completion`
  (`promptTurnSummaries[{usage, unit: credit, usedTools}]`, `elapsedTime`, `requestIds`),
  `focus_update`, `interaction_resolved`.
- v2 metadata is `_kiro.dev/metadata {contextUsagePercentage, meteringUsage, turnDurationMs}`.
- Result: `{stopReason: end_turn | cancelled | ...}`. `session/cancel` produces
  `cancelled` after any in-flight chunks.

## Permission requests

- v2: `session/request_permission` with string ids, `toolCall {toolCallId, title, rawInput}`,
  options `allow_once` (`optionId: allow_once`), `allow_always`, `reject_once`, and
  `_meta.trustOptions`. Writes and shell commands ask; reads do not.
- v3: with `autopilot: on` (the default) tools run without asking. With `autopilot: off`,
  shell commands ask: options `accept` (`allow_once`), `reject` (`reject_once`),
  `always-reject` (`reject_always`) plus `_meta.kiro.consent` describing the matched
  permission rule from `~/.kiro/settings/permissions.yaml`. File writes did not ask in the
  observed configuration.

## Custom agents

Agent JSON files in `~/.kiro/agents` (global) or `<cwd>/.kiro/agents` (workspace) appear as
ACP modes on both engines. Minimal tool-less agent that both engines accept:

```json
{"name": "x", "description": "...", "prompt": "...", "tools": [], "mcpServers": {}, "includeMcpJson": false}
```

Custom agents still inherit default resources (steering files, skills, `AGENTS.md`) and
the on-demand skill loader (`disclose_context`) unless the Kiro setting
`chat.disableInheritingDefaultResources` is `true`; `"resources": []` alone does not stop
that. The harness agent's prompt tells the model not to load them.

The v3 engine silently omits an agent whose file contains `"allowedTools": []`, and omits
agents with invalid configs (it reports those via `_kiro/customAgent/config_error`).
`kiro-cli agent list` still shows them, so that command is not a reliable check for v3.

## Client capabilities

- v3 uses `fs/read_text_file` (`line: 0`, `limit: 2001`) and `terminal/create`
  (`command`, `cwd`, `env`) when the client advertises them, falling back to its own tools
  when the client answers with an error.
- v3 asks `_kiro/terminal/shell_type {sessionId}`; answering `{shellType: "zsh"}` works.
- v2 ignores client fs/terminal capabilities.

## Environment

`kiro-cli` reads `KIRO_*` environment variables (for example `KIRO_API_KEY`). Setting
`KIRO_API_KEY` to an unrelated value breaks Kiro's authentication and empties the model
catalogue, which is why this project namespaces its own variables as `KIRO_ACP_*` and
`KIRO_GATEWAY_*`.

## Extensions seen in Kiro Crew (not yet used here)

Observed in kirodotdev/kirocrew (`src/kiro_crew/acp/`, kiro-cli 2.14–2.21 probes),
recorded for the roadmap (P6):

| Method / field | Direction | Notes |
|---|---|---|
| `_kiro.dev/commands/execute` | client → agent | `{"sessionId", "command": {"command": "effort", "args": {"value": "high"}}}` → `{"success": true, "message": "..."}` on 2.22 (now used for effort); string form gets no response. v2 only. |
| `session/new._meta.kiro.customAgents` | client → agent | v3: inline agent definitions (max 50), activated with `session/set_mode`. Confirmed on 2.22: `[{"id", "prompt", "tools": [...], "description"?, "mcpServers"?: {name: {command,args,env}}}]`; the id appears in `modes.availableModes` and the `mode` config option; `tools: ["@name"]` resolves against both in-agent and session-level `mcpServers`. Registering an id that also exists as a file does not error. |
| `_kiro.dev/session/terminate` | client → agent | v2: evict a session from a multiplexed process and reap its MCP children. |
| `_session/steer` | client → agent | v2: `{"sessionId", "message": "<user_message>...</user_message>"}` mid-turn; `steering_consumed` update confirms. |
| `session/load._meta["_kiro.dev/session_file"]` | client → agent | Load from an explicit session file; success = `modes` in the response; transcript is replayed as updates. |
| `_kiro/auth/getAccessToken` | agent → client | v3 without `--auth-method cli`: client must answer `{accessToken, expiresAt, ...}` or error `-32000`. |
| `_kiro.dev/metadata.stopReason` / `refusal` | agent → client | `CONTENT_FILTERED` with `refusal: {category, explanation, recommendedModel}`. |
| `_kiro.dev/compaction/status`, `clear/status`, `agent/switched`, `mcp/oauth_request`, `mcp/server_initialized`, `mcp/server_init_failure`, `subagent/list_update` | agent → client | Notifications; `/compact` is sent as a prompt and a fresh metadata frame follows compaction by about a second. |
| `tool_call._meta.kiro.toolName`, `rawInput.__tool_use_purpose` | agent → client | Trusted tool identity and the model's stated purpose; `title` is model-authored. |
| `session/request_permission._meta.kiro.consent` | agent → client | v3: `{capability, resource, askType}`. |
| `initialize.protocolVersion` | client → agent | Kiro Crew sends `"2025-08-22"` on v2 and `1` on v3; kiro-cli 2.21 answers `1` either way. |

## Per-model observations (kiro-cli 2.22, September 2026)

What this project has actually seen; "reported" marks findings taken from other
projects' notes rather than reproduced here. Check `uv run kiro-acp models` for the
current catalogue.

| Model | Native tool calls (mcp mode) | Emulated tool protocol (emulate mode) | Thought chunks | Effort | Notes |
|---|---|---|---|---|---|
| `claude-sonnet-5` | reliable (Claude Code verified) | refused the harness framing on v3 as "injected instructions" until the identity-preserving prompt; fine on v2 | not observed on v2 | v3: only when the model advertises the option | Gateway default (Kiro's default). |
| `claude-sonnet-4.6` | reliable (Claude Code, Codex, OpenCode verified) | reliable | not observed on v2 | v2: via `commands/execute` | Recommended general-purpose choice. |
| `claude-opus-4.8` | reliable | reliable | emitted on v2 (reported) | v2: via `commands/execute` | Highest capability; slower. |
| `gpt-5.6-luna` | reliable (Codex code mode `exec`, inline-agent MCP tool, stall recovery verified) | mostly (small model; keep prompts short) | not observed | v2: via `commands/execute`; v3 `/effort` rejected as chat | Small, fast, inexpensive: use for background calls and tests. |
| `gpt-5.6-terra` / `gpt-5.6-sol` | works | mostly (reported) | not observed | as luna | Experimental previews. |
| `claude-haiku-4.5` | untested | unsuitable for harness prompts (loses the tool list) | | | Not used by this project. |

