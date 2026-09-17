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
uv run kiro-acp doctor                          # verify kiro-cli and both engines
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
git diff | uv run kiro-acp prompt - --model claude-haiku-4.5 \
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
`title=` (the human title, shell globs).

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
  "model": "claude-haiku-4.5",
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
`x-api-key: <key>` when one is configured.

### Endpoints

| Endpoint | Protocol |
|---|---|
| `POST /v1/chat/completions` | OpenAI Chat Completions (streaming and non-streaming, tools, images, `reasoning_effort`, `response_format`) |
| `POST /v1/responses` | OpenAI Responses (`input` items, `instructions`, function tools, `previous_response_id`, streaming events) |
| `POST /v1/completions` | Legacy OpenAI Completions (`prompt`, `echo`, streaming) |
| `POST /v1/messages` | Anthropic Messages (streaming and non-streaming, tools, images, thinking blocks, `output_config.effort`) |
| `POST /v1/messages/count_tokens` | Anthropic token counting (estimated) |
| `GET /v1/models`, `GET /v1/models/{id}` | OpenAI format by default; Anthropic format when `anthropic-version` or `x-api-key` is present |
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

stream = client.responses.create(model="claude-haiku-4.5", input="List the top-level files.", stream=True)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
```

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
claude
```

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
model = "claude-sonnet-4.6"
model_provider = "kiro"

[model_providers.kiro]
name = "Kiro via kiro-gateway"
base_url = "http://127.0.0.1:8000/v1"
env_key = "KIRO_GATEWAY_KEY"
wire_api = "responses"
```

Then `export KIRO_GATEWAY_KEY=your-gateway-key` and run `codex`.

> **Model choice for harnesses.** Claude Code and Codex send very large system prompts and
> dozens of tool definitions. Sonnet- and Opus-class Kiro models (and the GPT 5.6 previews)
> follow the emulated tool protocol reliably; `claude-haiku-4.5` tends to lose track of the
> tool list at that prompt size. Both harnesses were verified end to end with
> `claude-sonnet-4.6` (Claude Code: multi-turn read/write task; Codex: shell command via the
> Responses API).

### Use it from OpenCode

```json
{
  "provider": {
    "kiro": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Kiro",
      "options": {"baseURL": "http://127.0.0.1:8000/v1", "apiKey": "your-gateway-key"},
      "models": {"claude-sonnet-4.6": {"name": "Claude Sonnet 4.6 (Kiro)"}}
    }
  }
}
```

### How requests are translated

**Models.** Requests may name any Kiro model id (`uv run kiro-acp models`). Common aliases
are normalized (`claude-sonnet-4-6-20260101` becomes `claude-sonnet-4.6`; `claude-haiku-4-5`
becomes `claude-haiku-4.5`). Unknown names fall back to `KIRO_GATEWAY_DEFAULT_MODEL` or
Kiro's current default unless `KIRO_GATEWAY_MODEL_FALLBACK=false`, in which case they are
rejected with a 404. Extra mappings: `KIRO_GATEWAY_MODEL_ALIASES='gpt-4*=gpt-5.6-terra,o3*=claude-opus-4.6'`.

**Conversations and sessions.** Kiro keeps its own context, but every OpenAI or Anthropic
request carries the whole conversation. In the default `affinity` mode the gateway
fingerprints the conversation prefix and, when it matches a live Kiro session, sends only
the new messages to that session. Otherwise it starts a fresh session and replays the
history as a transcript. `KIRO_GATEWAY_SESSION_MODE=stateless` always starts fresh.
Responses expose `kiro.session_id`, `kiro.reused_session`, `kiro.agent`, and `kiro.model`.

**Client tools (harness mode).** Kiro cannot execute a client's tool definitions, so the
gateway describes them in the prompt and asks the model to answer with
`<tool_call>{"name": ..., "arguments": {...}}</tool_call>` blocks. Those blocks are
parsed out of the stream and returned as OpenAI `tool_calls`, Responses `function_call`
items, or Anthropic `tool_use` blocks, with the matching finish reason. Tool results sent
back by the client are rendered into the transcript. Anthropic-defined tools (`bash`,
`text_editor`) get synthesized schemas; server-side tools such as `web_search` are ignored.
`KIRO_GATEWAY_TOOL_MODE=reject` refuses requests with tools; `ignore` drops them.

Kiro's stock agents have their own file and shell tools and its models are tuned to use
them, which competes with the harness. For requests that carry tools the gateway therefore
runs Kiro with a **tool-less agent**: at startup it writes
`~/.kiro/agents/kiro-gateway-harness.json` (no tools, no MCP servers) unless the file
already exists, and selects it for every harness-mode turn. Point
`KIRO_GATEWAY_HARNESS_AGENT` at your own agent to customize this, or set it empty to keep
Kiro's default agent; `KIRO_GATEWAY_PROVISION_HARNESS_AGENT=false` stops the gateway
from writing the file. Kiro's remaining permission requests in harness mode follow
`KIRO_GATEWAY_HARNESS_PERMISSIONS` (default `deny`).

**Kiro's own tools (agent mode).** When no client tools are supplied, Kiro acts as a full
agent inside `KIRO_GATEWAY_WORKSPACE`, subject to `KIRO_GATEWAY_PERMISSIONS` and
`KIRO_GATEWAY_PERMISSION_RULES` (same rule syntax as the CLI). Its tool activity is
surfaced as reasoning (`reasoning_content`, Responses `reasoning` items, Anthropic
`thinking` blocks) by default; `KIRO_GATEWAY_TOOL_ACTIVITY=text` puts it in the answer and
`none` hides it. Full details are always in the `kiro.tool_calls` extension field.

**Effort.** `reasoning_effort`, `reasoning.effort`, and `output_config.effort` map to
Kiro's `low|medium|high|max` (`xhigh` becomes `max`). The v3 engine only exposes effort for
some models; when it cannot be applied the response carries `kiro.effort_warning`.

**Usage.** Kiro meters credits, not tokens. Responses include estimated token counts
(about four characters per token) so client dashboards keep working, plus the real
`kiro.credits` and `kiro.contextUsagePercentage`. Set `KIRO_GATEWAY_USAGE_ESTIMATES=false`
to report zeros.

**Images** are accepted as base64 (`data:` URLs or Anthropic `base64` sources). Remote
image URLs are not fetched.

**Per-request headers.** `X-Kiro-Agent` selects a Kiro agent, `X-Kiro-Effort` sets effort,
and `X-Kiro-Permissions` overrides the permission policy when
`KIRO_GATEWAY_ALLOW_PERMISSION_OVERRIDE=true`.

### Configuration reference

All settings are environment variables with the `KIRO_GATEWAY_` prefix (a `.env` file in
the working directory is also read). The most important ones:

| Variable | Default | Description |
|---|---|---|
| `KIRO_GATEWAY_WORKSPACE` | current directory | Directory Kiro's own tools operate in (agent mode). Requests cannot change it. Harness clients such as Claude Code run their own tools in their own directory and are unaffected. |
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
| `KIRO_GATEWAY_PERMISSION_RULES` | empty | `allow:kind=read;deny:tool=shell` style rules, `;`-separated. |
| `KIRO_GATEWAY_HARNESS_PERMISSIONS` | `deny` | Policy while emulating client tools. |
| `KIRO_GATEWAY_HARNESS_AGENT` | `kiro-gateway-harness` | Tool-less Kiro agent used in harness mode (empty disables). |
| `KIRO_GATEWAY_PROVISION_HARNESS_AGENT` | `true` | Write the harness agent file into `~/.kiro/agents` when missing. |
| `KIRO_GATEWAY_SESSION_MODE` | `affinity` | `affinity` or `stateless`. |
| `KIRO_GATEWAY_SESSION_IDLE_TTL` | `600` | Seconds an idle session is kept. |
| `KIRO_GATEWAY_MAX_SESSIONS` | `8` | Live Kiro processes kept for reuse. |
| `KIRO_GATEWAY_DELETE_SESSIONS` | `true` | Delete Kiro's stored copy of gateway sessions when they are closed (v3). |
| `KIRO_GATEWAY_MAX_CONCURRENCY` | `4` | Simultaneous turns. |
| `KIRO_GATEWAY_TIMEOUT` | `900` | Seconds per turn before cancellation. |
| `KIRO_GATEWAY_TOOL_MODE` | `emulate` | `emulate`, `reject`, or `ignore` client tools. |
| `KIRO_GATEWAY_TOOL_ACTIVITY` | `thought` | `thought`, `text`, or `none`. |
| `KIRO_GATEWAY_EXPOSE_THOUGHTS` | `true` | Forward Kiro's thinking. |
| `KIRO_GATEWAY_USAGE_ESTIMATES` | `true` | Estimated token counts. |
| `KIRO_GATEWAY_SERVE_FS` / `SERVE_TERMINAL` | `false` | Offer client file-system / terminal capabilities to Kiro. |
| `KIRO_GATEWAY_DEBUG_ACP` | `false` | Log raw ACP traffic. |
| `KIRO_GATEWAY_LOG_LEVEL` | `info` | Log level. |

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| Claude Code: `401 invalid x-api-key`; gateway log mentions an OAuth token | Claude Code is sending its account login instead of the gateway key. Set `ANTHROPIC_AUTH_TOKEN` to the gateway key too, or use a separate `CLAUDE_CONFIG_DIR` (see above). |
| `/v1/models` is empty or every model falls back | `KIRO_API_KEY` (or another `KIRO_*` variable) is set in the environment and breaks `kiro-cli`'s own auth. Unset it; gateway settings use `KIRO_GATEWAY_*`. |
| 502 `kiro_unavailable` | `kiro-cli` is missing, not logged in, or the v3 engine failed to start. Run `uv run kiro-acp doctor`. |
| Harness client says it has no tools / ignores tool calls | The model is too small for the harness prompt. Use a Sonnet- or Opus-class model. |
| A stream stops after a while | The turn hit `KIRO_GATEWAY_TIMEOUT` (default 900 s) and was cancelled. |

### Error format

OpenAI routes return `{"error": {"message", "type", "param", "code"}}`; Anthropic routes
return `{"type": "error", "error": {"type", "message"}}`. Failures after a stream has
started are sent as an in-stream `error` event. Kiro failures are `502`.

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

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design and
[docs/KIRO_ACP_NOTES.md](docs/KIRO_ACP_NOTES.md) for what Kiro's ACP implementation
actually does on the wire.

## License

MIT
