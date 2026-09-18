# kiro-acp-gateway

Use the [Kiro CLI](https://kiro.dev) coding agent from scripts, custom automations, and
other coding harnesses.

The project has three layers, all built on the
[Agent Client Protocol (ACP)](https://agentclientprotocol.com) that `kiro-cli acp` speaks:

| Layer | What it is |
|---|---|
| `kiro_acp.acp` | A complete async ACP client library for Python: JSON-RPC transport, sessions, prompt turns as typed event streams, permission policies, file-system and terminal handlers, cancellation, and Kiro's protocol extensions. |
| `kiro-acp` | A command-line utility for one-shot prompts, interactive chat, model and agent discovery, and health checks, with text, JSON, and JSONL output for scripting. |
| `kiro-gateway` | An HTTP gateway that exposes Kiro through the **OpenAI Chat Completions**, **OpenAI Responses**, legacy **OpenAI Completions**, and **Anthropic Messages** APIs, so Claude Code, Codex, OpenCode, the OpenAI and Anthropic SDKs, and anything else that speaks those protocols can drive Kiro. |

```text
Claude Code / Codex / OpenCode / SDKs / curl
        │  OpenAI or Anthropic HTTP protocol
        ▼
   kiro-gateway  ──  translates requests, emulates client tools,
        │            reuses Kiro sessions across turns
        │  ACP (JSON-RPC over stdio)
        ▼
   kiro-cli acp  ──  Kiro agent engine (v3 default, v2 supported)
```

Everything is managed with [uv](https://docs.astral.sh/uv/).

## Requirements

- macOS or Linux
- Python 3.11 or newer (uv installs one for you if needed)
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Kiro CLI 2.22 or newer on `PATH`, already logged in (`kiro-cli login`)

## Installation

```bash
git clone https://github.com/rmcdermo/kiro-acp-gateway.git
cd kiro-acp-gateway
uv sync
```

`uv sync` creates `.venv`, installs the package in editable mode, and installs the
development dependencies. Run the tools with `uv run`:

```bash
uv run kiro-acp doctor
```

To install the tools globally instead:

```bash
uv tool install .
```

## The `kiro-acp` utility

`kiro-acp` starts `kiro-cli acp`, opens a session, runs your prompt, and prints the reply.
It is designed for shell scripts: the answer goes to stdout, status goes to stderr, and the
exit code tells you what happened.

### Quick start

```bash
uv run kiro-acp doctor                          # verify kiro-cli and both engines; lists orphaned acp processes (--kill-orphans)
uv run kiro-acp models                          # models Kiro advertises
uv run kiro-acp agents                          # Kiro agents (ACP modes)
uv run kiro-acp prompt "Summarize this repository in three bullets."
```

### Prompting

```bash
# Pick a model, an effort level, and a Kiro agent
uv run kiro-acp prompt --model claude-sonnet-4.6 --effort high --agent vibe \
  "Add type hints to utils.py"

# Read the prompt from stdin
git diff | uv run kiro-acp prompt - --model gpt-5.6-luna \
  --permissions deny "Review this diff for bugs:"

# Attach files and images
uv run kiro-acp prompt --file docs/spec.md --image screenshot.png \
  "Does the UI in the screenshot match the spec?"

# Work in another directory
uv run kiro-acp prompt --cwd ~/code/other-project "Run the tests and fix failures"
```

When both stdin and a positional prompt are given, the positional text is sent and stdin
is ignored; pass `-` as the prompt to read stdin.

### Permissions

Kiro asks the client before it writes files or runs commands. `--permissions` controls the
answer:

| Policy | Behaviour |
|---|---|
| `ask` (default in a terminal) | Prompt interactively on the terminal. |
| `deny` | Reject every request. Kiro can still read files and search. |
| `allow-once` | Approve each request once. |
| `allow-always` | Approve and let Kiro remember the approval where it offers that option. |
| `allow-all` | Trust every tool up front (`--trust-all-tools` on v2, autopilot on v3). |

Rules refine the policy and are evaluated in order before it:

```bash
# Allow reads and searches, allow the shell tool only for ls commands, deny everything else
uv run kiro-acp prompt --permissions deny \
  --allow "kind=read,search,fetch" \
  --allow "tool=shell;title=Running: ls*" \
  "List the largest files here"
```

Rule selectors are `kind=` (ACP tool kind: `read`, `edit`, `delete`, `move`, `search`,
`execute`, `fetch`, `think`, `other`), `tool=` (Kiro tool name, shell globs), and
`title=` (the human title, shell globs). Claude Code's permission syntax works too and
matches the command or path Kiro is asking about:

```bash
uv run kiro-acp prompt --permissions deny \
  --allow "Read" --allow "Glob" --allow "Bash(git status*)" --allow "Bash(uv run pytest*)" \
  --deny "Read(/etc/*)" "Run the tests and summarize failures"
```

`--fs` and `--terminal` let Kiro use the client's file-system and terminal capabilities
(the v3 engine uses them when offered); by default Kiro uses its own built-in tools.

### Output formats

```bash
uv run kiro-acp prompt "..."                       # text: reply on stdout
uv run kiro-acp prompt --show-tools --show-thoughts "..."   # plus tool activity on stderr
uv run kiro-acp prompt --output json "..."         # one JSON document with text, tool calls,
                                                   # permissions, credits, session id
uv run kiro-acp prompt --output jsonl "..."        # one event per line as it happens
```

The JSON output looks like:

```json
{
  "stop_reason": "end_turn",
  "text": "FINISHED",
  "thoughts": "",
  "tool_calls": [{"id": "...", "title": "Running: ls", "kind": "execute", "status": "completed", "raw_input": {"command": "ls"}, "raw_output": {...}}],
  "permissions": [{"tool_call_id": "...", "title": "Running: ls", "kind": "execute", "granted": true, "reason": "policy:allow-once"}],
  "metadata": {"contextUsagePercentage": 6.8, "meteringUsage": [{"value": 0.02, "unit": "credit"}], "turnDurationMs": 4613},
  "session_id": "sess_...",
  "model": "claude-sonnet-4.6",
  "mode": "vibe"
}
```

Exit codes: `0` success, `1` Kiro error, `2` usage error, `3` cancelled or timed out,
`4` `kiro-cli` not found.

### Sessions

```bash
uv run kiro-acp prompt --print-session-id "Start refactoring the parser"   # prints sess_...
uv run kiro-acp prompt --session sess_1234 "Continue with the tests"       # resume it
uv run kiro-acp sessions                                                    # v3 engine only
uv run kiro-acp sessions --delete sess_1234                                 # delete one
uv run kiro-acp sessions --prune --title "New Session" --older-than 24 --dry-run
uv run kiro-acp sessions --prune --all --older-than 168 --yes               # every workspace
uv run kiro-acp chat                                                        # interactive REPL
```

Inside `chat`, `/model <id>`, `/effort <level>`, `/mode <id>`, `/session`, and `/quit` are
handled locally; everything else is sent to Kiro.

### Engines

Kiro CLI ships two agent engines. `kiro-acp` and the gateway default to **v3** (the Kiro
Agent Server); pass `--engine v2` for the older Rust engine. Differences that matter:

| | v3 (default) | v2 |
|---|---|---|
| Model selection | ACP `configOptions` | Kiro `session/set_model` extension |
| Effort | not exposed by ACP for most models (warning) | `/effort <level>` slash command |
| Stored sessions | `session/list`, `session/load`, delete via `_kiro/session/delete` | `session/load` only |
| Permissions | `autopilot` option plus per-request prompts | per-request prompts, `--trust-all-tools` |
| Client `fs`/`terminal` | used when offered | ignored |

### Environment variables

Every option has an environment default: `KIRO_ACP_CLI`, `KIRO_ACP_ENGINE`,
`KIRO_ACP_WORKSPACE`, `KIRO_ACP_MODEL`, `KIRO_ACP_EFFORT`, `KIRO_ACP_AGENT`,
`KIRO_ACP_PERMISSIONS`, `KIRO_ACP_TIMEOUT`.

> The `KIRO_*` namespace belongs to Kiro CLI itself (for example `KIRO_API_KEY`), which is
> why this project uses `KIRO_ACP_*` and `KIRO_GATEWAY_*`.

## The gateway

### Start it

```bash
export KIRO_GATEWAY_WORKSPACE="$PWD"              # the only directory Kiro's own tools may touch
                                                  # (harness clients like Claude Code work in their own cwd)
export KIRO_GATEWAY_API_KEY="$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export KIRO_GATEWAY_PERMISSIONS=deny              # deny | allow-once | allow-always | allow-all
uv run kiro-gateway --port 8000
```

`kiro-gateway --print-config` shows the effective configuration. `GET /health` is
unauthenticated; every `/v1/*` route requires the key as `Authorization: Bearer <key>` or
`x-api-key: <key>` when one is configured. Settings can also live in a `.env` file in the
directory you start the gateway from (see `.env.example`), and the most common ones have
flags: `--port`, `--workspace`, `--engine`, `--permissions`, `--api-key`,
`--default-model`, `--session-mode`, `--max-concurrency`, `--log-level`, `--debug-acp`.

### Connecting any client: the three things it needs

Every client, script, or coding harness needs exactly three values:

| Value | What to use | Notes |
|---|---|---|
| Base URL | `http://127.0.0.1:8000/v1` for OpenAI-style clients, `http://127.0.0.1:8000` for Anthropic-style clients (they add `/v1/messages` themselves) | Both prefixes are also served under `/openai/v1` and `/anthropic/v1`. |
| API key | the value of `KIRO_GATEWAY_API_KEY` | Sent as `Authorization: Bearer <key>` or `x-api-key: <key>`. If the gateway has no key configured, any value is accepted. |
| Model id | any id from `GET /v1/models` (or `uv run kiro-acp models`), e.g. `claude-sonnet-4.6`, `gpt-5.6-luna` | Hyphenated and dated forms (`claude-sonnet-4-6-20260101`) are normalised; unknown names fall back to the default model unless `KIRO_GATEWAY_MODEL_FALLBACK=false`. |

Nothing else is required on the client side. Optional per-request headers:
`X-Kiro-Effort` (`low|medium|high|max`), `X-Kiro-Agent` (a Kiro agent for agent mode),
`X-Kiro-Workspace` (a directory allowed by `KIRO_GATEWAY_ALLOWED_WORKSPACES`), and
`X-Kiro-Permissions` (when `KIRO_GATEWAY_ALLOW_PERMISSION_OVERRIDE=true`). Effort can also
be set in the body (`reasoning_effort`, `reasoning.effort`, `output_config.effort`).

Which of the two modes a request lands in depends only on whether it sends `tools`
(see *Two modes* below): scripts that want Kiro to act as an agent inside
`KIRO_GATEWAY_WORKSPACE` send none; coding harnesses send theirs and run them locally.

### Use it from curl

```bash
export KIRO_GATEWAY_URL=http://127.0.0.1:8000
export KIRO_GATEWAY_KEY=your-gateway-key

# OpenAI Chat Completions
curl -s "$KIRO_GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $KIRO_GATEWAY_KEY" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4.6", "messages": [{"role": "user", "content": "Say hello in five words."}]}'

# OpenAI Responses, streamed (SSE)
curl -sN "$KIRO_GATEWAY_URL/v1/responses" \
  -H "Authorization: Bearer $KIRO_GATEWAY_KEY" -H "Content-Type: application/json" \
  -d '{"model": "gpt-5.6-luna", "input": "List three uses for a gateway like this.", "stream": true}'

# Anthropic Messages
curl -s "$KIRO_GATEWAY_URL/v1/messages" \
  -H "x-api-key: $KIRO_GATEWAY_KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4.6", "max_tokens": 1024, "messages": [{"role": "user", "content": "Say hello in five words."}]}'

# Agent mode in a specific project (requires KIRO_GATEWAY_ALLOWED_WORKSPACES to match)
curl -s "$KIRO_GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $KIRO_GATEWAY_KEY" -H "Content-Type: application/json" \
  -H "X-Kiro-Workspace: /Users/me/code/project-b" -H "X-Kiro-Effort: high" \
  -d '{"model": "claude-opus-4.8", "messages": [{"role": "user", "content": "Find and fix the failing test."}]}'

curl -s "$KIRO_GATEWAY_URL/v1/models" -H "Authorization: Bearer $KIRO_GATEWAY_KEY"   # model ids
curl -s "$KIRO_GATEWAY_URL/health"                                                    # no key needed
```

### Use it from Python with `requests`

No SDK needed. The response bodies are the standard OpenAI / Anthropic shapes plus a
`kiro` object (session id, credits, context usage, Kiro's own tool calls in agent mode).

```python
import json
import requests

GATEWAY = "http://127.0.0.1:8000"
HEADERS = {"Authorization": "Bearer your-gateway-key", "Content-Type": "application/json"}

# Non-streaming chat completion
r = requests.post(
    f"{GATEWAY}/v1/chat/completions",
    headers=HEADERS,
    json={
        "model": "claude-sonnet-4.6",
        "messages": [
            {"role": "system", "content": "You are a terse release-notes writer."},
            {"role": "user", "content": "Summarize the last commit in one line."},
        ],
        "reasoning_effort": "low",           # optional: low | medium | high | max
    },
    timeout=900,                             # Kiro turns can be long; match KIRO_GATEWAY_TIMEOUT
)
r.raise_for_status()
body = r.json()
print(body["choices"][0]["message"]["content"])
print("credits used:", body["kiro"]["credits"], "context:", body["kiro"].get("contextUsagePercentage"))

# Streaming chat completion (Server-Sent Events)
with requests.post(
    f"{GATEWAY}/v1/chat/completions",
    headers=HEADERS,
    json={"model": "gpt-5.6-luna", "messages": [{"role": "user", "content": "Count to five."}], "stream": True},
    stream=True,
    timeout=900,
) as stream:
    for line in stream.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue                          # keepalive comments and blank lines
        data = line[len(b"data: "):]
        if data == b"[DONE]":
            break
        delta = json.loads(data)["choices"][0]["delta"]
        print(delta.get("content", ""), end="", flush=True)
print()

# Anthropic Messages with structured output validated against a schema
r = requests.post(
    f"{GATEWAY}/v1/messages",
    headers={"x-api-key": "your-gateway-key", "anthropic-version": "2023-06-01"},
    json={
        "model": "claude-sonnet-4.6",
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Classify this commit message: 'fix: null check in parser'"}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"kind": {"type": "string"}, "scope": {"type": "string"}},
                    "required": ["kind"],
                },
            }
        },
    },
    timeout=900,
)
print(json.loads(r.json()["content"][0]["text"]), r.json()["kiro"]["schema_valid"])
```

Errors come back with the HTTP status and body described under *Error format*; treat
`429` and `503` as retryable (they carry `Retry-After`) and `4xx` otherwise as your bug.

### Endpoints

| Endpoint | Protocol |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions (streaming and non-streaming, tools, images, `reasoning_effort`, `response_format`) |
| `POST /v1/responses` | OpenAI Responses (`input` items, `instructions`, function and freeform `custom` tools, `previous_response_id`, streaming events) |
| `POST /v1/completions` | Legacy OpenAI Completions (`prompt`, `echo`, streaming) |
| `POST /v1/messages` | Anthropic Messages (streaming and non-streaming, tools, images, thinking blocks, `output_config.effort`) |
| `POST /v1/messages/count_tokens` | Anthropic token counting (estimated) |
| `GET /v1/models`, `GET /v1/models/{id}` | OpenAI format by default; Anthropic format when `anthropic-version` or `x-api-key` is present |
| `GET /v1/kiro/stats` | JSON snapshot of metrics, live sessions, and recent audit activity (feeds the dashboard) |
| `GET /dashboard` | Live dashboard page |
| `GET /v1/kiro/sessions`, `GET /v1/kiro/sessions/{id}/audit` | Audit ledger |
| `GET /metrics` | Prometheus metrics |
| `GET /health` | Gateway status |

The same routes exist under `/openai/v1/...` and `/anthropic/v1/...` if a client cannot share
the `/v1` prefix.

### Use it from the OpenAI SDK

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="your-gateway-key")

response = client.chat.completions.create(
    model="claude-sonnet-4.6",
    messages=[{"role": "user", "content": "What does this repository do?"}],
)
print(response.choices[0].message.content)

stream = client.responses.create(model="gpt-5.6-luna", input="List the top-level files.", stream=True)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
```

Function calling from a script works the same as against OpenAI: pass `tools`, run the
returned `tool_calls` yourself, and send the results back as `tool` messages. Note that
any request with `tools` runs in harness mode (your script executes the tools, Kiro's own
tools are off), and that the Kiro turn stays open until you send the results or
`KIRO_GATEWAY_SESSION_IDLE_TTL` elapses.

### Use it from the Anthropic SDK

```python
from anthropic import Anthropic

client = Anthropic(base_url="http://127.0.0.1:8000", api_key="your-gateway-key")
message = client.messages.create(
    model="claude-sonnet-4-6",           # Anthropic-style ids are mapped to Kiro's
    max_tokens=4096,
    messages=[{"role": "user", "content": "Explain the build system."}],
)
print(message.content[0].text)
```

### Use it from Claude Code

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:8000
export ANTHROPIC_API_KEY=your-gateway-key
export ANTHROPIC_AUTH_TOKEN=your-gateway-key         # see note below
export ANTHROPIC_MODEL=claude-sonnet-4.6            # optional: any Kiro model id
export ANTHROPIC_SMALL_FAST_MODEL=gpt-5.6-luna      # optional: Claude Code's background calls (titles, summaries)
claude
```

Also `/model` inside Claude Code accepts any id from `GET /v1/models`, including the
hyphenated aliases it expects (`claude-sonnet-4-6`, `claude-opus-4-8`) and `claude-auto`.

> **Why both variables?** When Claude Code is logged in to a Claude account it sends its
> account OAuth token as `Authorization: Bearer ...` and ignores `ANTHROPIC_API_KEY`, so the
> gateway answers `401 invalid x-api-key`. Setting `ANTHROPIC_AUTH_TOKEN` to the gateway
> key makes Claude Code send that key as the bearer token instead. Alternatively, keep a
> separate Claude Code profile with no login for gateway use:
>
> ```bash
> CLAUDE_CONFIG_DIR=~/.claude-kiro ANTHROPIC_BASE_URL=http://127.0.0.1:8000 \
>   ANTHROPIC_API_KEY=your-gateway-key claude
> ```
>
> The gateway logs a hint whenever it receives a Claude OAuth token instead of its key.

Claude Code sends its tools (`Bash`, `Edit`, `Read`, ...) with every request. The gateway
describes them to Kiro and turns Kiro's replies into real `tool_use` blocks, so Claude Code
executes the tools itself exactly as it would with the Anthropic API. While serving a
harness the gateway switches Kiro to a tool-less agent (see *Harness mode* below), so only
one agent touches your files.

### Use it from Codex CLI

```toml
# ~/.codex/config.toml
model = "gpt-5.6-luna"            # any Kiro model id; Claude names work too
model_provider = "kiro"

[model_providers.kiro]
name = "Kiro via kiro-gateway"
base_url = "http://127.0.0.1:8000/v1"
env_key = "KIRO_GATEWAY_KEY"
wire_api = "responses"
```

Then `export KIRO_GATEWAY_KEY=your-gateway-key` and run `codex`. Both of Codex's tool
styles work:

- **Direct tools** (`exec_command`, `apply_patch`, `shell`, ...) are ordinary functions.
  `namespace` groups are flattened, tools Codex adds mid-session (`additional_tools`
  items) are merged in, and unknown input item types are skipped with a log line.
- **Code mode**, which Codex uses for GPT names in its own catalogue (`gpt-5.6-luna`,
  `gpt-5.6-terra`, `gpt-6-*`, ...): the whole workspace is one *freeform* `custom` tool
  named `exec` that takes JavaScript source. The gateway presents it to Kiro as a
  one-argument function, returns the call as a `custom_tool_call` item (with the
  `response.custom_tool_call_input.*` streaming events), and accepts the
  `custom_tool_call_output` items Codex sends back. Freeform tools with a `grammar`
  format carry the grammar into the tool description.

**Model metadata comes from the gateway.** Codex asks each provider for
`GET /v1/models?client_version=<its version>` in its own catalogue format. The gateway
answers with an entry for every Kiro model (Codex's real base instructions for that
version, fetched once from Codex's published catalogue and cached), so Codex shows no
"Model metadata for `<model>` not found" warning and needs no `model_catalog_json`. By
default those entries select Codex's direct function tools; `KIRO_GATEWAY_CODEX_TOOL_MODE=code`
keeps code mode, and `KIRO_GATEWAY_CODEX_CATALOG=false` turns the endpoint off. Offline,
the gateway falls back to the plain model list and Codex uses its built-in defaults.

> The older ways still work: `uv run kiro-acp codex-catalog -o ~/.codex/kiro-models.json`
> writes the same catalogue to a file for `model_catalog_json`, and a `kiro-` (or `kiro/`)
> prefix on a model name (`kiro-gpt-5.6-luna`) takes it out of Codex's own catalogue; the
> gateway strips the prefix before resolving the model.

> **Model choice for harnesses.** With the default `mcp` tool mode the model makes native
> tool calls, so any current Kiro model works. In `emulate` mode, Sonnet- and Opus-class
> Kiro models (and the GPT 5.6 previews) follow the emulated tool protocol reliably;
> smaller models tend to lose track of the tool list at harness prompt sizes. Both harnesses were verified end to end with
> `claude-sonnet-4.6` (Claude Code: multi-turn read/write task; Codex: shell command via the
> Responses API).

### Use it from OpenCode and other OpenAI-compatible harnesses

Any harness with an "OpenAI-compatible provider" setting works: give it the base URL
`http://127.0.0.1:8000/v1`, the gateway key, and a Kiro model id. Tool calling, streaming,
and reasoning content are all supported over Chat Completions, and the Responses API is
there for harnesses that prefer it. Custom harnesses (for example ones built on the
Vercel AI SDK or LiteLLM) need nothing gateway-specific. OpenCode example:

```json
{
  "provider": {
    "kiro": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Kiro",
      "options": {"baseURL": "http://127.0.0.1:8000/v1", "apiKey": "your-gateway-key"},
      "models": {
        "claude-sonnet-4.6": {"name": "Claude Sonnet 4.6 (Kiro)"},
        "gpt-5.6-luna": {"name": "GPT 5.6 Luna (Kiro)"}
      }
    }
  }
}
```

Every model id you want to pick in the harness has to be listed under `models`; the ids
come from `GET /v1/models`.

### Two modes

The gateway picks a path for every request based on one thing: whether the request
carries tool definitions (`tools` in OpenAI requests, `tools` in Anthropic requests).

| | Harness mode | Agent mode |
|---|---|---|
| Triggered by | request includes `tools` (Claude Code, Codex, OpenCode, function-calling scripts) | request has no `tools` (pipeline scripts, curl, plain SDK calls) |
| Who runs tools | the client, on its own machine and directory | Kiro, inside `KIRO_GATEWAY_WORKSPACE` |
| Kiro agent | `KIRO_GATEWAY_HARNESS_AGENT_MCP` (bridged tools only) or the tool-less `KIRO_GATEWAY_HARNESS_AGENT` in `emulate` mode | `KIRO_GATEWAY_AGENT` (Kiro default) |
| Engine | `KIRO_GATEWAY_HARNESS_ENGINE` (v2) | `KIRO_GATEWAY_ENGINE` (v3) |
| Permissions | `KIRO_GATEWAY_HARNESS_PERMISSIONS` (deny) | `KIRO_GATEWAY_PERMISSIONS` |
| Output | text plus `tool_calls` / `function_call` / `tool_use` | text; Kiro's own activity as reasoning and in `kiro.tool_calls` |

A script that wants Kiro's own agentic behaviour must therefore send no `tools`. If it
passes tools for some other reason (an SDK helper that always attaches them, for
example), set `KIRO_GATEWAY_TOOL_MODE=ignore` to drop them and force agent mode, or
`reject` to fail such requests with a 400. Each turn logs which mode and engine it ran on.

### Kiro agents the gateway installs

Harness mode needs Kiro agents that carry no tools of their own, so the gateway ships two
and installs them itself. Nothing has to be done by hand on a new machine:

| Agent | Used when | Tools |
|---|---|---|
| `kiro-gateway-harness-mcp` | `KIRO_GATEWAY_TOOL_MODE=mcp` (default) | only `@harness`, the bridged MCP server |
| `kiro-gateway-harness` | `emulate` tool mode | none |

- **Every gateway start** writes `~/.kiro/agents/<name>.json` for both when the file is
  missing (`KIRO_GATEWAY_PROVISION_HARNESS_AGENT=true`, the default).
- **Files the gateway owns are kept current.** Both carry `managed-by: kiro-gateway` in
  their description; when such a file differs from what this gateway version would write
  (after an upgrade, for instance) it is rewritten and the log says `Updated harness agent`.
- **Your files are never overwritten.** An agent file with the same name but without the
  marker, or one that fails to parse, is left alone. To customise one, copy it under a new
  name and point `KIRO_GATEWAY_HARNESS_AGENT` / `KIRO_GATEWAY_HARNESS_AGENT_MCP` at that
  name, or edit it in place and drop the marker from its description.
- They show up wherever Kiro lists agents (the Kiro IDE, `kiro-cli`, `uv run kiro-acp
  agents`) because Kiro reads the global agents directory. Selecting them outside the
  gateway is harmless: one has no tools, the other only an MCP server that exists inside
  a gateway session.
- Set `KIRO_GATEWAY_PROVISION_HARNESS_AGENT=false` where the gateway must not write to the
  home directory and install the two files with your own tooling; the JSON to install is
  `kiro_acp.gateway.harness_agent.agent_config(name, mcp=...)`. With
  `KIRO_GATEWAY_HARNESS_ENGINE=v3` no files are needed at all: the v3 engine receives the
  agent definition over the wire on every session.

### How requests are translated

**Models.** Requests may name any Kiro model id (`uv run kiro-acp models`). Common aliases
are normalized (`claude-sonnet-4-6-20260101` becomes `claude-sonnet-4.6`; `claude-opus-4-8` becomes `claude-opus-4.8`). Unknown names fall back to `KIRO_GATEWAY_DEFAULT_MODEL` or
Kiro's current default unless `KIRO_GATEWAY_MODEL_FALLBACK=false`, in which case they are
rejected with a 404. Extra mappings: `KIRO_GATEWAY_MODEL_ALIASES='gpt-4*=gpt-5.6-terra,o3*=claude-opus-4.6'`.

**Conversations and sessions.** Kiro keeps its own context, but every OpenAI or Anthropic
request carries the whole conversation. In the default `affinity` mode the gateway
fingerprints the conversation prefix and, when it matches a live Kiro session, sends only
the new messages to that session. Otherwise it starts a fresh session and replays the
history as a transcript. `KIRO_GATEWAY_SESSION_MODE=stateless` always starts fresh.
Responses expose `kiro.session_id`, `kiro.reused_session`, `kiro.agent`, and `kiro.model`.

**Client tools (harness mode).** Kiro cannot accept a client's tool definitions over ACP,
so the gateway bridges them. In the default `mcp` tool mode it registers a small MCP server
with each harness session (spawned by Kiro, part of this package) that advertises the
client's tools. When the model calls one, the call is relayed to the gateway over a local
socket and returned to the HTTP client as a normal OpenAI `tool_calls` / Responses
`function_call` / Anthropic `tool_use`. The Kiro turn stays open, blocked inside the MCP
call, until the client sends the tool result in its next request; the gateway then
delivers it and streams the continuation. Models make genuine native tool calls, parallel
calls work, and no text protocol has to be obeyed. Kiro runs the
`kiro-gateway-harness-mcp` agent, whose only tools are the bridged ones.

`KIRO_GATEWAY_TOOL_MODE=emulate` selects the older text protocol instead: tools are
described in the prompt and the model answers with
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>` blocks that the gateway parses.
It works with Sonnet/Opus-class models but depends on the model following instructions.
`reject` refuses requests with tools; `ignore` drops them and runs agent mode.
Anthropic-defined tools (`bash`, `text_editor`) get synthesized schemas in both modes;
server-side tools such as `web_search` are ignored.

Open turns waiting for tool results are pooled like any session and cancelled when the
client abandons them (`KIRO_GATEWAY_SESSION_IDLE_TTL`) or the gateway shuts down.

**Kiro's own tools (agent mode).** When no client tools are supplied, Kiro acts as a full
agent inside `KIRO_GATEWAY_WORKSPACE`, subject to `KIRO_GATEWAY_PERMISSIONS` and
`KIRO_GATEWAY_PERMISSION_RULES` (same rule syntax as the CLI). Its tool activity is
surfaced as reasoning (`reasoning_content`, Responses `reasoning` items, Anthropic
`thinking` blocks) by default; `KIRO_GATEWAY_TOOL_ACTIVITY=text` puts it in the answer and
`none` hides it. Full details are always in the `kiro.tool_calls` extension field.

**Stop sequences and limits.** Kiro ignores `stop`/`stop_sequences` and `max_tokens`, so
the gateway enforces them itself: text is watched as it streams, and when a stop sequence
appears the reply is truncated, the Kiro turn cancelled, and `stop`/`stop_sequence`
reported. `max_tokens` enforcement is opt-in (`KIRO_GATEWAY_ENFORCE_MAX_TOKENS`) because
the count is an estimate. Long silent stretches (Kiro running a tool) are covered by
periodic keepalives so clients with stream watchdogs do not disconnect.

**Errors.** Kiro failures are classified so SDK retry logic works: throttling and quota
errors become `429` with `Retry-After`, a busy session `409`, model unavailable or
overloaded `503`, backend timeouts `504`, auth and connection problems `502`. The full
code table is under *Error format*.

**Effort.** `reasoning_effort`, `reasoning.effort`, and `output_config.effort` map to
Kiro's `low|medium|high|max` (`xhigh` becomes `max`). The v3 engine only exposes effort for
some models; when it cannot be applied the response carries `kiro.effort_warning`.

**Usage.** Kiro meters credits, not tokens. Responses include estimated token counts
(about four characters per token) so client dashboards keep working, plus the real
`kiro.credits` and `kiro.contextUsagePercentage`. Set `KIRO_GATEWAY_USAGE_ESTIMATES=false`
to report zeros.

**Images** are accepted as base64 (`data:` URLs or Anthropic `base64` sources). Remote
image URLs are not fetched. Document blocks (PDFs) are not accepted; put the file in the
workspace and ask Kiro to read it, which also lets the model page through it.

**Per-request options.** Headers: `X-Kiro-Agent` selects a Kiro agent, `X-Kiro-Effort`
sets effort, `X-Kiro-Permissions` overrides the permission policy when
`KIRO_GATEWAY_ALLOW_PERMISSION_OVERRIDE=true`, `X-Kiro-Workspace` selects the directory
Kiro works in when it matches `KIRO_GATEWAY_ALLOWED_WORKSPACES`, and `X-Kiro-MCP-Servers`
attaches catalogue MCP servers by name. The same options can travel in the request body
as a `kiro` object (the OpenAI and Anthropic SDKs pass it through `extra_body`):

```json
"kiro": {"agent": "plan", "effort": "high", "workspace": "/Users/me/code/project-b",
         "mcp_servers": ["jira", "docs"], "permissions": "allow-all"}
```

Headers win over the body. One gateway can therefore serve several projects in agent
mode; sessions are pooled per workspace (and per MCP server set and agent) and the
response's `kiro.workspace`, `kiro.agent`, and `kiro.mcp_servers` show what was used.

**MCP servers for agent-mode turns.** Kiro's own agents already carry the MCP servers
configured in Kiro. A gateway-side catalogue adds servers per request without touching
Kiro's config: `KIRO_GATEWAY_MCP_SERVERS` is either inline JSON or the path of a file in
the usual `mcpServers` shape (Claude Code, Cursor, VS Code `servers`, and OpenCode `mcp`
blocks are all understood; stdio and `http`/`sse` entries; `disabled` entries are
skipped). Requests select entries by name (`kiro.mcp_servers` or `X-Kiro-MCP-Servers`);
`KIRO_GATEWAY_MCP_SERVERS_DEFAULT` names entries attached to every agent-mode turn, and
`KIRO_GATEWAY_MCP_DISCOVERY=true` also attaches whatever the workspace's own `.mcp.json`,
`.cursor/mcp.json`, `.vscode/mcp.json`, or `opencode.json[c]` declares. Full definitions
inline in a request are refused unless `KIRO_GATEWAY_ALLOW_REQUEST_MCP_SERVERS=true`,
because a stdio definition runs a command on the gateway host. Harness requests ignore
MCP servers (the harness executes tools itself). Tool calls made through these servers are
still subject to the permission policy and rules.

**Inline agents (v3 engine).** Instead of naming a Kiro agent, an agent-mode request may
define one: `kiro.agent` as an object with `prompt` (required), `tools` (Kiro tool names;
`["*"]` for all built-in tools, `[]` for none; default all), and an optional
`description`. Attached MCP servers are referenced automatically. The definition travels
to Kiro over the wire and is registered for that session only; nothing is written to
`~/.kiro/agents`. The reply's `kiro.agent` carries the generated id. Requires
`KIRO_GATEWAY_ENGINE=v3` (the default); on v2 the request is rejected with
`agent_requires_v3`. `KIRO_GATEWAY_ALLOW_REQUEST_AGENTS=false` disables it. On v3 the
harness agents are sent the same way, so the files in `~/.kiro/agents` are only needed
for the v2 engine.

```python
r = requests.post(f"{GATEWAY}/v1/chat/completions", headers=HEADERS, json={
    "model": "claude-sonnet-4.6",
    "messages": [{"role": "user", "content": "Summarise open incidents from the last day."}],
    "kiro": {
        "agent": {"prompt": "You are an SRE assistant. Be terse and cite incident ids.", "tools": []},
        "mcp_servers": ["pagerduty"],          # a name from KIRO_GATEWAY_MCP_SERVERS
    },
})
```

**Structured output.** With `response_format` (OpenAI) or `output_config.format`
(Anthropic) carrying a JSON schema, the reply is fence-stripped and validated. A
non-streaming request that fails validation is re-prompted once in the same Kiro session
with the validation errors; the result carries `kiro.schema_valid` and, on failure,
`kiro.schema_errors`. Streaming requests are validated but not retried.

**Kiro's tool activity** (agent mode) is rendered with arguments, unified diffs for edits,
output excerpts for commands, and Kiro's plan as a checklist; `KIRO_GATEWAY_TOOL_ACTIVITY_DETAIL=brief`
reduces it to one line per call. `/v1/models` reports each model's context window
(`context_length`, Anthropic `max_input_tokens`) parsed from Kiro's descriptions.

### Configuration reference

All settings are environment variables with the `KIRO_GATEWAY_` prefix (a `.env` file in
the working directory is also read). The most important ones:

| Variable | Default | Description |
|---|---|---|
| `KIRO_GATEWAY_WORKSPACE` | current directory | Default directory Kiro's own tools operate in (agent mode). Harness clients such as Claude Code run their own tools in their own directory and are unaffected. |
| `KIRO_GATEWAY_ALLOWED_WORKSPACES` | empty | Glob patterns (e.g. `/Users/me/code/*`, `/srv/repos/**`) a request may select with the `X-Kiro-Workspace` header. Empty disables per-request workspaces. |
| `KIRO_GATEWAY_API_KEY` | empty | Key required on `/v1/*`. Empty means no authentication. |
| `KIRO_GATEWAY_HOST` / `PORT` | `127.0.0.1` / `8000` | Bind address. |
| `KIRO_GATEWAY_CLI` | `kiro-cli` | Kiro executable. |
| `KIRO_GATEWAY_ENGINE` | `v3` | Kiro agent engine. |
| `KIRO_GATEWAY_AGENT` | Kiro default | Default Kiro agent (ACP mode). |
| `KIRO_GATEWAY_EFFORT` | unset | Default effort. |
| `KIRO_GATEWAY_DEFAULT_MODEL` | Kiro default | Model for unknown or missing names. |
| `KIRO_GATEWAY_MODEL_FALLBACK` | `true` | Fall back instead of returning 404. |
| `KIRO_GATEWAY_MODEL_ALIASES` | empty | `pattern=model,...` extra aliases. |
| `KIRO_GATEWAY_PERMISSIONS` | `deny` | Policy for Kiro's tool requests in agent mode. |
| `KIRO_GATEWAY_PERMISSION_RULES` | empty | Ordered rules: `allow:kind=read,search`, `deny:tool=shell`, or Claude Code style `allow:Bash(git status*)`, `deny:Read(/etc/*)`, `allow:mcp__server__tool`. |
| `KIRO_GATEWAY_HARNESS_PERMISSIONS` | `deny` | Policy while emulating client tools. |
| `KIRO_GATEWAY_HARNESS_ENGINE` | `v2` | Kiro engine for harness-mode turns (empty = same as `KIRO_GATEWAY_ENGINE`). |
| `KIRO_GATEWAY_HARNESS_WORKSPACE` | *(empty)* | Directory Kiro runs in for harness turns. Empty means a fresh empty scratch directory, so Kiro cannot pull the gateway workspace's README, AGENTS.md, or steering files into a harness prompt (the harness's own project is a different directory). Set a path only if you want Kiro to see one project's steering files during harness turns. |
| `KIRO_GATEWAY_HARNESS_AGENT` | `kiro-gateway-harness` | Tool-less Kiro agent used in harness mode (empty disables). |
| `KIRO_GATEWAY_PROVISION_HARNESS_AGENT` | `true` | Write the harness agent file into `~/.kiro/agents` when missing. |
| `KIRO_GATEWAY_SESSION_MODE` | `affinity` | `affinity` or `stateless`. |
| `KIRO_GATEWAY_SESSION_IDLE_TTL` | `600` | Seconds an idle session is kept. |
| `KIRO_GATEWAY_MAX_SESSIONS` | `8` | Live Kiro processes kept for reuse. |
| `KIRO_GATEWAY_DELETE_SESSIONS` | `true` | Delete Kiro's stored copy of gateway sessions when they are closed (v3). |
| `KIRO_GATEWAY_MAX_CONCURRENCY` | `4` | Simultaneous turns. |
| `KIRO_GATEWAY_QUEUE_TIMEOUT` | `60` | Seconds to wait for a free turn slot before answering `503` with `Retry-After`; `0` waits forever. |
| `KIRO_GATEWAY_RATE_LIMIT_RPM` | `0` | Requests per minute per API key (per client address without keys); `0` disables. Exceeding it returns `429`. |
| `KIRO_GATEWAY_SHUTDOWN_GRACE` | `10` | Seconds to let in-flight turns cancel on shutdown. |
| `KIRO_GATEWAY_TIMEOUT` | `900` | Seconds per turn before cancellation. |
| `KIRO_GATEWAY_STALL_TIMEOUT` | `600` | Seconds of silence from Kiro before an agent-mode turn is cancelled and nudged to continue; `0` disables. |
| `KIRO_GATEWAY_STALL_RECOVERIES` | `1` | Continue-nudges per request before a stall becomes `504 kiro_stall`. |
| `KIRO_GATEWAY_AUDIT_RECORDS` | `500` | Audit ledger records per session (`/v1/kiro/sessions/{id}/audit`); `0` disables. |
| `KIRO_GATEWAY_RECORD_FRAMES` | empty | Directory to write every ACP frame to (JSONL per Kiro process) for debugging and replay. |
| `KIRO_GATEWAY_SSE_KEEPALIVE` | `15` | Seconds of stream silence before a keepalive (`ping` / SSE comment); `0` disables. |
| `KIRO_GATEWAY_WARMUP` | `true` | Load the model catalogue in the background at startup. |
| `KIRO_GATEWAY_ENFORCE_MAX_TOKENS` | `false` | Cut output at the request's `max_tokens` using the token estimator. `stop` sequences are always enforced. |
| `KIRO_GATEWAY_MODEL_ALIAS_STYLE` | `both` | Also list hyphenated ids (`claude-sonnet-4-6`) and `claude-auto`/`auto`; `native` lists Kiro ids only. |
| `KIRO_GATEWAY_SANITIZE_SYSTEM` | `false` | Strip identity and concealment lines from client system prompts (defensive second layer). |
| `KIRO_GATEWAY_TOOL_MODE` | `mcp` | What to do with client tool definitions: `mcp` (native calls via a bridged MCP server), `emulate` (tagged-block prompting), `ignore` (drop them and run agent mode), or `reject` (400). |
| `KIRO_GATEWAY_HARNESS_AGENT_MCP` | `kiro-gateway-harness-mcp` | Kiro agent used in `mcp` mode (`tools: ["@harness"]`). |
| `KIRO_GATEWAY_MCP_BATCH_WINDOW` | `0.5` | Seconds to collect parallel tool calls before answering the client. |
| `KIRO_GATEWAY_TOOL_ACTIVITY` | `thought` | `thought`, `text`, or `none`. |
| `KIRO_GATEWAY_TOOL_ACTIVITY_DETAIL` | `full` | `full` renders arguments, diffs, and output excerpts; `brief` is one line per call. |
| `KIRO_GATEWAY_VALIDATE_JSON_OUTPUT` | `true` | Validate structured-output replies against the JSON schema and retry once (non-streaming). |
| `KIRO_GATEWAY_EXPOSE_THOUGHTS` | `true` | Forward Kiro's thinking. |
| `KIRO_GATEWAY_USAGE_ESTIMATES` | `true` | Estimated token counts. |
| `KIRO_GATEWAY_MAX_IMAGE_BYTES` | `5242880` | Reject image inputs larger than this (decoded bytes) with `400 image_too_large`; an oversized image can wedge a Kiro session. `0` disables. Images are dropped with a note when the agent does not advertise image input. |
| `KIRO_GATEWAY_MCP_SERVERS` | empty | MCP server catalogue for agent-mode requests: inline JSON or a file path in the `mcpServers` shape (stdio or `http`/`sse`). |
| `KIRO_GATEWAY_MCP_SERVERS_DEFAULT` | empty | Catalogue names attached to every agent-mode turn. |
| `KIRO_GATEWAY_MCP_DISCOVERY` | `false` | Also attach servers declared in the workspace's `.mcp.json`, `.cursor/mcp.json`, `.vscode/mcp.json`, or `opencode.json[c]`. |
| `KIRO_GATEWAY_ALLOW_REQUEST_MCP_SERVERS` | `false` | Accept full MCP server definitions in requests (runs commands on the gateway host). |
| `KIRO_GATEWAY_ALLOW_REQUEST_AGENTS` | `true` | Accept inline agent definitions (`kiro.agent` objects); v3 engine only. |
| `KIRO_GATEWAY_CODEX_CATALOG` | `true` | Answer Codex's `GET /v1/models?client_version=...` with a Codex model catalogue for every Kiro model. |
| `KIRO_GATEWAY_CODEX_TOOL_MODE` | `direct` | Tool style that catalogue selects for Codex: `direct` function tools or `code` mode. |
| `KIRO_GATEWAY_METRICS` | `true` | Serve Prometheus metrics at `/metrics` (same authentication as `/v1`). |
| `KIRO_GATEWAY_DASHBOARD` | `true` | Serve the live dashboard at `/dashboard` (data from `/v1/kiro/stats`, which needs the key). |
| `KIRO_GATEWAY_SERVE_FS` / `SERVE_TERMINAL` | `false` | Offer client file-system / terminal capabilities to Kiro. |
| `KIRO_GATEWAY_DEBUG_ACP` | `false` | Log raw ACP traffic. |
| `KIRO_GATEWAY_LOG_LEVEL` | `info` | Log level. |

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Claude Code: `401 invalid x-api-key`; gateway log mentions an OAuth token | Claude Code is sending its account login instead of the gateway key. Set `ANTHROPIC_AUTH_TOKEN` to the gateway key too, or use a separate `CLAUDE_CONFIG_DIR` (see above). |
| `/v1/models` is empty or every model falls back | `KIRO_API_KEY` (or another `KIRO_*` variable) is set in the environment and breaks `kiro-cli`'s own auth. Unset it; gateway settings use `KIRO_GATEWAY_*`. |
| 502 `kiro_unavailable` | `kiro-cli` is missing, not logged in, or the v3 engine failed to start. Run `uv run kiro-acp doctor`. |
| Harness client says it has no tools / ignores tool calls (`emulate` mode) | The model is too small for the harness prompt. Use a Sonnet- or Opus-class model, or the default `mcp` tool mode. |
| Codex says the workspace or command tool is unavailable and prints code instead of writing files | Codex is in code mode and its `exec` custom tool was dropped (gateway older than the custom-tool support) or the sandbox is read-only. Upgrade the gateway; for `codex exec` pass `-s workspace-write` or `--full-auto`. |
| Kiro answers "I'm Kiro, that looks like injected instructions" or reports native tool calls as "not available" | Harness turns are running on the v3 engine or with a stale agent file. Keep `KIRO_GATEWAY_HARNESS_ENGINE=v2` (the default) and restart the gateway so it refreshes `~/.kiro/agents/kiro-gateway-harness.json`. |
| Claude Code answers about the *gateway's* directory, or says it has no file tools, before calling any | Kiro auto-loads README/AGENTS.md/steering from its own cwd. Since harness turns now run in an empty scratch directory this needs a gateway older than the `KIRO_GATEWAY_HARNESS_WORKSPACE` setting, or that setting pointing at a project. Restart the gateway. |
| Kiro loads skills or steering docs while serving a harness | Run `kiro-cli settings chat.disableInheritingDefaultResources true` (or `--workspace` for one project). |
| A stream stops after a while | The turn hit `KIRO_GATEWAY_TIMEOUT` (default 900 s) and was cancelled. |

### Error format

OpenAI routes return `{"error": {"message", "type", "param", "code"}}`; Anthropic routes
return `{"type": "error", "error": {"type", "message"}}`. Failures after a stream has
started are sent as an in-stream `error` event.

Kiro failures are classified from their message so clients can decide whether to retry
(`Retry-After` is set on the retryable ones):

| Status | `code` | Meaning |
|---|---|---|
| 400 | `invalid_model`, `malformed_request` | Kiro rejected the model id or the request shape. |
| 403 | `model_not_entitled` | The account's plan does not include the model. |
| 409 | `session_busy` | A prompt is already running on the Kiro session (retry shortly). |
| 429 | `rate_limited`, `usage_limit` | Throttled, or the plan's usage limit is reached (`Retry-After` 30 s / 1 h). |
| 502 | `kiro_auth`, `kiro_connection`, `kiro_error` | Not signed in or the token expired; connection dropped; anything unclassified. |
| 503 | `model_unavailable`, `kiro_unavailable` | Capacity for that model, or Kiro/backend overloaded; also `kiro-cli` missing. |
| 504 | `kiro_timeout`, `kiro_stall` | The turn or a backend call timed out; or Kiro went silent, was cancelled, and recovery was exhausted. |

A model refusal or content filter is not an error: the reply completes with
`finish_reason: "content_filter"` (OpenAI) / `stop_reason: "refusal"` (Anthropic) and the
`kiro` block carries `refusal: {category, explanation, recommendedModel}` and, when Kiro
suggests one, `recommended_model`. Every response's `kiro` block also reports
`contextUsagePercentage` (how full the Kiro session's context is) and `credits`.

### Running it as a service, in Docker, and monitoring it

**User service.** `scripts/install-service.sh [env-file]` installs a launchd agent
(macOS) or a `systemd --user` unit (Linux) that runs `uv run kiro-gateway` from this
checkout and loads `.env`. `kiro-gateway --print-service launchd|systemd` prints the unit
for review without installing it. Logs go to `~/Library/Logs/kiro-gateway.log` or
`journalctl --user -u kiro-gateway`.

**Docker or Podman.** The `Dockerfile` installs Kiro CLI and the gateway bound to
`0.0.0.0:8000`, with `/workspace` as the agent-mode directory and `/home/kiro/.kiro` for
Kiro's login. The commands are identical for Podman; substitute `podman` for `docker`
(verified with Podman 5.8 on Apple Silicon, arm64 image):

```bash
docker build -t kiro-acp-gateway .
# Log Kiro in once; the device flow prints a URL and code to open in any browser.
docker run -it --rm -v kiro-home:/home/kiro/.kiro kiro-acp-gateway kiro-cli login --use-device-flow
docker run -d --name kiro-gateway -p 127.0.0.1:8000:8000 \
  -v kiro-home:/home/kiro/.kiro -v "$PWD":/workspace:z \
  -e KIRO_GATEWAY_API_KEY=change-me kiro-acp-gateway
curl -s http://127.0.0.1:8000/health
```

The named volume keeps the login and Kiro's agent files across container restarts; `:z`
on the workspace mount is needed on SELinux hosts and harmless elsewhere. Any gateway
setting can be passed with `-e KIRO_GATEWAY_...`. Harness clients (Claude Code, Codex)
keep running their tools on the host; only Kiro's own tools are confined to the mounted
workspace. Until Kiro is logged in, `/health` answers but `/v1/models` returns
`502 kiro_auth` with Kiro's "not logged in" message. Podman ignores the `HEALTHCHECK`
line (OCI format); build with `--format docker` if you want it.

**Stalled turns.** Kiro emits nothing while a tool runs, so a hung command would only
end at `KIRO_GATEWAY_TIMEOUT`. In agent mode the gateway also watches for silence:
after `KIRO_GATEWAY_STALL_TIMEOUT` seconds (600 by default) without any event it cancels
the turn (Kiro acknowledges a cancel on a live turn) and, up to
`KIRO_GATEWAY_STALL_RECOVERIES` times (1), sends the same session a short instruction to
continue from where it left off without re-running the command that stalled, naming
that command. The reply carries `kiro.stalls` and `kiro.stall_recoveries`, and the
recovery note appears in the reasoning stream. When recoveries are exhausted the request
fails with `504 kiro_stall`. Harness turns are not affected: they return to the client on
every tool call.

**Audit ledger.** Every session keeps a bounded record (`KIRO_GATEWAY_AUDIT_RECORDS`,
500 per session, `0` disables) of turns, permission decisions, Kiro's tool calls and
results, harness tool calls, stalls, and turn ends, with secrets masked (bearer tokens,
`sk-`/`gh*_`/`AKIA`/`xox*` keys, URL credentials, and any `token`/`secret`/`password`
field). `GET /v1/kiro/sessions` lists sessions with record counts and
`GET /v1/kiro/sessions/{id}/audit` returns the records; both need the API key, and each
reply's `kiro.audit` gives the path for its session.

**Recording ACP traffic.** `KIRO_GATEWAY_RECORD_FRAMES=<dir>` (gateway) or
`KIRO_ACP_RECORD_FRAMES=<dir>` (CLI) writes every JSON-RPC frame exchanged with each
`kiro-cli` process to a JSONL file with a header naming the command, engine, and model.
Recordings can be replayed by `tests/fake_agent/replay.py` to reproduce a session without
Kiro; `tests/fixtures/acp_frames/` keeps scrubbed recordings of real Kiro versions as
regression fixtures. Recordings contain prompts, tool arguments, and outputs verbatim, so
treat them as sensitive.

**Dashboard.** `http://127.0.0.1:8000/dashboard` is a single self-contained page (no
external assets, works offline) showing what the gateway is doing right now: health,
active turns and free slots, live sessions with model, agent, workspace and idle time,
turns by mode/model/finish reason, latency histograms, credits per model, errors, and the
recent sessions from the audit ledger with links to their records. It refreshes every few
seconds; a header button switches between the light (default) and dark themes and the
choice is remembered per browser. The page itself is public but empty;
it asks for the gateway key once, keeps it in the browser's local storage, and sends it on
every request to `GET /v1/kiro/stats`, the JSON endpoint behind it (usable from scripts
too). `KIRO_GATEWAY_DASHBOARD=false` removes the page. The numbers live in memory and
reset when the gateway restarts; for history use Prometheus.

**Metrics.** `GET /metrics` (same key as `/v1`) serves Prometheus text:
`kiro_gateway_turns_total{mode,engine,model,finish}`, `kiro_gateway_turn_seconds`
(histogram), `kiro_gateway_credits_total{model}`, `kiro_gateway_session_reuse_total`,
`kiro_gateway_errors_total{code,status}`, and gauges for active turns, live sessions, and
cached models. `GET /health` stays unauthenticated for liveness probes.

**CI and packaging.** `.github/workflows/ci.yml` runs ruff and the test suite (fake ACP
agent, no Kiro needed) on Linux and macOS for Python 3.11 to 3.13 and builds wheels with
`uv build`; an optional integration job runs `KIRO_INTEGRATION=1` tests on a self-hosted
runner with a logged-in Kiro CLI. `examples/clients/` holds ready-to-use configurations
for every verified client.

### Security

The gateway hands an AI agent access to a directory and, depending on the permission
policy, a shell. Treat it as privileged infrastructure:

- Bind to `127.0.0.1` unless you put TLS and real authentication in front of it.
- Always set `KIRO_GATEWAY_API_KEY` before exposing it beyond localhost.
- Point `KIRO_GATEWAY_WORKSPACE` at one project, never at a home directory.
- Start with `KIRO_GATEWAY_PERMISSIONS=deny`; enable `allow-*` only for trusted callers in a disposable or version-controlled workspace.
- Consider containers or VMs for isolation; permission prompts are not a sandbox.
- Prompts, file paths, and tool output appear in logs at `debug` level.

## Using the library

```python
import asyncio
from kiro_acp.acp import (
    ClientHandlers, KiroAgent, KiroLaunchOptions, PermissionPolicy, PermissionRule,
    TextDelta, ToolCallEvent, TurnComplete,
)

async def main() -> None:
    handlers = ClientHandlers(
        permissions=PermissionPolicy("deny", rules=[PermissionRule.parse("allow:kind=read,search")]),
    )
    options = KiroLaunchOptions(engine="v3", model="claude-sonnet-4.6")
    async with KiroAgent(options, cwd="~/code/project", handlers=handlers) as agent:
        session = await agent.new_session()
        async for event in session.prompt([{"type": "text", "text": "Where is logging configured?"}]):
            match event:
                case TextDelta(text=text):
                    print(text, end="", flush=True)
                case ToolCallEvent(call=call, phase="completed"):
                    print(f"\n[{call.kind}] {call.title}")
                case TurnComplete(stop_reason=reason):
                    print(f"\n-- {reason}")
        result = await session.prompt_text("Summarize in one line.")
        print(result.text, result.metadata.get("meteringUsage"))

asyncio.run(main())
```

`KiroLaunchOptions(raw_command=[...])` launches any other ACP agent with the same client.

## Development

```bash
uv sync                                  # install everything
uv run pytest                            # unit + protocol tests (scripted fake ACP agent)
KIRO_INTEGRATION=1 uv run pytest -m integration   # also run against the real kiro-cli
uv run ruff check src tests              # lint
uv run ruff format src tests             # format
uv add <package>                         # add a dependency (updates pyproject + uv.lock)
uv add --dev <package>                   # add a development dependency
```

Project layout:

```text
src/kiro_acp/
  acp/         ACP client library (client, session, handlers, kiro launcher, types, events)
  cli/         kiro-acp command
  gateway/     kiro-gateway server (config, backend, conversation model, tool emulation)
    protocols/ openai_chat, openai_completions, openai_responses, anthropic, models
tests/
  fake_agent/  scripted ACP agent used by the test suite
docs/          design notes
legacy/        the original proof-of-concept scripts (kept for reference)
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design,
[docs/KIRO_ACP_NOTES.md](docs/KIRO_ACP_NOTES.md) for what Kiro's ACP implementation
actually does on the wire, and [docs/ROADMAP.md](docs/ROADMAP.md) for planned work.

## License

Apache 2.0
