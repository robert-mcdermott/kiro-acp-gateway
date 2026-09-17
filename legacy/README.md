# Kiro OpenAI-Compatible Gateway

`kiro_openai_gateway.py` is a FastAPI server that exposes an OpenAI-compatible Chat Completions API backed by Kiro CLI through the Agent Client Protocol (ACP).

The gateway launches `kiro-cli acp`, creates an ACP session, selects the requested Kiro model, converts OpenAI chat messages into an ACP prompt, and translates Kiro response events into either a normal OpenAI Chat Completion response or a Server-Sent Events (SSE) stream.

## Features

- OpenAI-compatible `POST /v1/chat/completions`
- OpenAI-compatible `GET /v1/models`
- Nonstreaming JSON responses
- Streaming `chat.completion.chunk` responses over SSE
- OpenAI Python SDK compatibility
- Fixed, server-controlled Kiro workspace
- Optional bearer-token authentication
- Configurable ACP permission handling
- Model discovery and validation through Kiro ACP
- Concurrent-request limiting
- Request cancellation when streaming clients disconnect
- Kiro subprocess stderr capture and logging
- Kiro metadata passthrough for nonstreaming requests

## Architecture

```text
OpenAI-compatible client
        |
        | HTTP / JSON or SSE
        v
FastAPI gateway
        |
        | OpenAI messages -> ACP prompt
        | ACP chunks -> OpenAI chunks
        v
kiro-cli acp subprocess
        |
        v
Kiro model and built-in tools
```

For the current implementation, each Chat Completions request gets a separate Kiro ACP subprocess and session. This is simple and isolates requests, though it has more startup overhead than a persistent worker pool.

## Files

```text
kiro_openai_gateway.py   FastAPI server and Kiro ACP adapter
requirements.txt         Python dependencies
README.md                This documentation
```

## Requirements

- macOS or Linux
- Python 3.10 or newer
- Kiro CLI installed and available on `PATH`
- A working Kiro authentication session

Verify Kiro CLI:

```bash
kiro-cli --version
```

Verify interactive Kiro access:

```bash
kiro-cli chat
```

Exit the TUI after confirming that Kiro can answer a prompt.

You can also verify the v3 noninteractive execution path independently, although the gateway itself uses the raw `kiro-cli acp` server:

```bash
kiro-cli chat \
  --agent-engine v3 \
  --no-interactive \
  --output-format stream-json \
  "Respond with exactly: hello"
```

## Installation

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

The dependencies are:

```text
fastapi>=0.115,<1
uvicorn[standard]>=0.30,<1
pydantic>=2.8,<3
```

## Configuration

The gateway is configured with environment variables.

| Variable | Default | Description |
|---|---:|---|
| `KIRO_CLI` | `kiro-cli` | Path or executable name for Kiro CLI. |
| `KIRO_WORKSPACE` | Current directory | Fixed working directory exposed to Kiro. |
| `KIRO_API_KEY` | Empty | Optional bearer token required by `/v1/models` and `/v1/chat/completions`. |
| `KIRO_PERMISSIONS` | `deny` | ACP permission policy: `deny` or `allow-once`. |
| `KIRO_MAX_CONCURRENCY` | `2` | Maximum number of concurrent Kiro ACP subprocesses. |
| `KIRO_TIMEOUT` | `900` | Maximum ACP prompt time in seconds. |
| `KIRO_DEBUG_ACP` | `false` | Log raw ACP request and response messages when true. |
| `KIRO_HOST` | `127.0.0.1` | HTTP bind address. |
| `KIRO_PORT` | `8000` | HTTP listening port. |
| `LOG_LEVEL` | `info` | Uvicorn log level. |

### Workspace

Set the directory Kiro may inspect and modify:

```bash
export KIRO_WORKSPACE="$PWD"
```

Use an absolute path for a different repository:

```bash
export KIRO_WORKSPACE="/Users/robertm/mycode/example-project"
```

Clients cannot submit an arbitrary working directory. This is intentional: the gateway owns the workspace boundary rather than trusting an HTTP request to select a host path.

### API authentication

Configure an API key:

```bash
export KIRO_API_KEY='replace-with-a-long-random-value'
```

Clients must then send:

```http
Authorization: Bearer replace-with-a-long-random-value
```

If `KIRO_API_KEY` is empty, the gateway does not require authentication. Only use that configuration when the server is bound to a trusted local interface.

Generate a random development key with Python:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

### Permission policy

The gateway supports two noninteractive ACP permission policies.

#### Deny

```bash
export KIRO_PERMISSIONS=deny
```

Any `session/request_permission` request is answered with a cancelled outcome. This is the safest default, but Kiro may be unable to perform file writes or shell commands that require approval.

#### Allow once

```bash
export KIRO_PERMISSIONS=allow-once
```

When Kiro requests permission, the gateway examines the options supplied by Kiro. If Kiro offers an option whose kind is `allow_once`, the gateway returns that option's exact opaque `optionId`. If no such option is offered, the request is denied.

Use `allow-once` only for a trusted gateway and a controlled workspace. Kiro can use built-in tools to read files, write files, and execute commands according to its own configuration and the permission decision.

The HTTP server does not support the interactive `ask` policy from the standalone command-line example because a server request has no safe interactive terminal in which to ask a user.

### Concurrency

Set the maximum number of Kiro requests that may run concurrently:

```bash
export KIRO_MAX_CONCURRENCY=2
```

Requests wait on an asynchronous semaphore when the limit is reached. Start conservatively because every active request launches a separate `kiro-cli acp` subprocess.

### ACP debugging

Enable raw protocol logs:

```bash
export KIRO_DEBUG_ACP=true
```

Raw ACP traffic and Kiro stderr are written to application logs, not API response bodies.

Do not enable detailed protocol logging in a sensitive production environment without reviewing log retention. Prompts, paths, tool inputs, and model output may appear in logs.

## Running the server

Configure a local development instance:

```bash
export KIRO_WORKSPACE="$PWD"
export KIRO_PERMISSIONS=deny
export KIRO_API_KEY='local-development-key'
export KIRO_MAX_CONCURRENCY=2
```

Start the gateway:

```bash
python3 kiro_openai_gateway.py
```

The default address is:

```text
http://127.0.0.1:8000
```

Specify a different host and port with command-line options:

```bash
python3 kiro_openai_gateway.py \
  --host 127.0.0.1 \
  --port 8080 \
  --log-level info
```

Alternatively, launch the ASGI application directly with Uvicorn:

```bash
uvicorn kiro_openai_gateway:app \
  --host 127.0.0.1 \
  --port 8000
```

Avoid multiple Uvicorn process workers in the initial version. Each process has its own semaphore and model cache, so `--workers 4` would allow up to four times the configured Kiro concurrency.

## API endpoints

### `GET /health`

Returns basic server state. This endpoint does not require the configured API key.

Example:

```bash
curl --silent http://127.0.0.1:8000/health | jq
```

Example response:

```json
{
  "status": "ok",
  "backend": "kiro-cli-acp",
  "workspace": "/path/to/workspace",
  "permissions": "deny"
}
```

The health endpoint reports process configuration; it does not launch Kiro or perform a model inference check.

### `GET /v1/models`

Starts a temporary ACP client when the model cache is empty, creates a Kiro session, reads the models advertised by `session/new`, and returns them in OpenAI model-list format. Results are cached for five minutes per gateway process.

Example:

```bash
curl --silent http://127.0.0.1:8000/v1/models \
  -H 'Authorization: Bearer local-development-key' |
jq
```

Example response shape:

```json
{
  "object": "list",
  "data": [
    {
      "id": "auto",
      "object": "model",
      "created": 1789360000,
      "owned_by": "kiro"
    },
    {
      "id": "gpt-5.6-terra",
      "object": "model",
      "created": 1789360000,
      "owned_by": "kiro"
    }
  ]
}
```

### `POST /v1/chat/completions`

Accepts a subset of the OpenAI Chat Completions request format.

Supported request fields:

| Field | Support |
|---|---|
| `model` | Supported and validated against Kiro's advertised models. |
| `messages` | Supported for developer, system, user, assistant, and tool roles with text content. |
| `stream` | Supported for `false` and `true`. |
| `n` | Only `1` is supported. |
| `user` | Accepted but currently not passed to Kiro. |

Explicitly rejected fields:

| Field | Reason |
|---|---|
| `reasoning_effort` | The current raw Kiro ACP server does not expose a reliable effort-setting RPC. |
| `tools` | OpenAI client-defined tool schemas are not mapped into Kiro tools. |
| `tool_choice` | OpenAI tool selection is not implemented. |
| `response_format` | Structured output enforcement is not implemented. |
| `logprobs` | Kiro ACP does not provide compatible log-probability output. |
| `n != 1` | The gateway creates one Kiro turn per request. |

Unknown request fields are accepted by Pydantic but are not passed to Kiro. Applications that require strict OpenAI behavior should add explicit validation for every relevant parameter.

## Message conversion

The gateway converts the OpenAI message list into one role-labelled text prompt. For example:

```json
{
  "messages": [
    {
      "role": "system",
      "content": "Answer concisely."
    },
    {
      "role": "user",
      "content": "What files are in this directory?"
    }
  ]
}
```

becomes approximately:

```text
System instructions:
Answer concisely.

User:
What files are in this directory?
```

This allows stateless OpenAI clients to send complete conversation history. It is not a lossless mapping of every OpenAI content or tool-call feature.

Text content arrays are supported when every part has `type: "text"`:

```json
{
  "role": "user",
  "content": [
    {
      "type": "text",
      "text": "Explain this repository."
    }
  ]
}
```

Image, audio, file, and other multimodal content parts are rejected.

## curl examples

The following examples assume:

```bash
export KIRO_API_KEY='local-development-key'
```

### List models

```bash
curl --silent http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $KIRO_API_KEY" |
jq
```

### Nonstreaming completion

```bash
curl --silent http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KIRO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-terra",
    "messages": [
      {
        "role": "user",
        "content": "What files are in this directory?"
      }
    ],
    "stream": false
  }' |
jq
```

Extract only the answer:

```bash
curl --silent http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KIRO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-terra",
    "messages": [
      {
        "role": "user",
        "content": "What files are in this directory?"
      }
    ]
  }' |
jq -r '.choices[0].message.content'
```

### System instructions

```bash
curl --silent http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KIRO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-terra",
    "messages": [
      {
        "role": "system",
        "content": "Act as a software architect. Be concise and cite relevant file paths."
      },
      {
        "role": "user",
        "content": "Describe the architecture of this repository."
      }
    ]
  }' |
jq -r '.choices[0].message.content'
```

### Streaming completion

Use `curl -N` to disable client-side buffering:

```bash
curl -N http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KIRO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-terra",
    "messages": [
      {
        "role": "user",
        "content": "Explain the architecture of this repository."
      }
    ],
    "stream": true
  }'
```

The stream uses OpenAI-style SSE records:

```text
data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}

data: {"id":"chatcmpl-...","object":"chat.completion.chunk",...}

data: [DONE]
```

### File modification

To allow one-time ACP approvals, start the gateway with:

```bash
export KIRO_PERMISSIONS=allow-once
python3 kiro_openai_gateway.py
```

Then send:

```bash
curl --silent http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $KIRO_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-5.6-terra",
    "messages": [
      {
        "role": "user",
        "content": "Create a file named gateway-test.txt containing hello."
      }
    ]
  }' |
jq -r '.choices[0].message.content'
```

Only do this in a disposable or source-controlled workspace you trust.

## OpenAI Python client

Install the OpenAI SDK:

```bash
pip install openai
```

### Nonstreaming request

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="local-development-key",
)

response = client.chat.completions.create(
    model="gpt-5.6-terra",
    messages=[
        {
            "role": "system",
            "content": "Answer concisely and mention relevant file paths.",
        },
        {
            "role": "user",
            "content": "Describe the architecture of this repository.",
        },
    ],
)

print(response.choices[0].message.content)
```

### Streaming request

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="local-development-key",
)

stream = client.chat.completions.create(
    model="gpt-5.6-terra",
    messages=[
        {
            "role": "user",
            "content": "Explain the architecture of this repository.",
        }
    ],
    stream=True,
)

for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)

print()
```

### List models

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8000/v1",
    api_key="local-development-key",
)

for model in client.models.list():
    print(model.id)
```

### Async client

```python
import asyncio
from openai import AsyncOpenAI


async def main() -> None:
    client = AsyncOpenAI(
        base_url="http://127.0.0.1:8000/v1",
        api_key="local-development-key",
    )

    response = await client.chat.completions.create(
        model="gpt-5.6-terra",
        messages=[
            {
                "role": "user",
                "content": "What files are in this directory?",
            }
        ],
    )

    print(response.choices[0].message.content)


asyncio.run(main())
```

## Response format

### Nonstreaming

A successful nonstreaming response has this shape:

```json
{
  "id": "chatcmpl-0123456789abcdef",
  "object": "chat.completion",
  "created": 1789360000,
  "model": "gpt-5.6-terra",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "The files are ...",
        "refusal": null
      },
      "logprobs": null,
      "finish_reason": "stop"
    }
  ],
  "usage": null,
  "kiro": {
    "contextUsagePercentage": 2.79,
    "meteringUsage": [
      {
        "value": 0.03,
        "unit": "credit",
        "unitPlural": "credits"
      }
    ],
    "turnDurationMs": 3555
  }
}
```

Kiro reports credit metering and context percentages rather than OpenAI-compatible prompt, completion, and total token counts. The gateway therefore returns `usage: null` instead of fabricating token usage.

The optional `kiro` extension contains the latest metadata reported during the turn. Clients should treat it as gateway-specific rather than part of the standard OpenAI schema.

### Streaming

The first event assigns the assistant role:

```text
data: {"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}],...}
```

Each Kiro `agent_message_chunk` becomes a content delta:

```text
data: {"choices":[{"index":0,"delta":{"content":"The repository"},"finish_reason":null}],...}
```

The final JSON event sets `finish_reason`, followed by the terminator:

```text
data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],...}

data: [DONE]
```

## Error format

The gateway returns OpenAI-shaped errors:

```json
{
  "error": {
    "message": "Unknown model 'example'",
    "type": "invalid_request_error",
    "param": null,
    "code": "kiro_backend_error"
  }
}
```

Common status codes:

| Status | Meaning |
|---:|---|
| `400` | Unsupported request option, invalid content, or unknown model. |
| `401` | Missing or invalid bearer token. |
| `422` | FastAPI/Pydantic request validation failure. |
| `502` | Kiro ACP process or backend failure. |

A failure occurring after an SSE response has started cannot change the HTTP status. The gateway instead sends an SSE record containing an `error` object and then sends `[DONE]`.

## ACP behavior

The server uses the following ACP sequence for each request:

```text
initialize
session/new
session/set_model
session/prompt
session/update notifications
session/prompt result
subprocess shutdown
```

The `session/prompt` parameters must use the ACP field `prompt`:

```json
{
  "sessionId": "...",
  "prompt": [
    {
      "type": "text",
      "text": "What files are in this directory?"
    }
  ]
}
```

Kiro streams assistant text through `session/update` events whose update type is `agent_message_chunk`. The gateway also recognizes Kiro's `_kiro.dev/session/update` extension.

Model selection uses:

```json
{
  "method": "session/set_model",
  "params": {
    "sessionId": "...",
    "modelId": "gpt-5.6-terra"
  }
}
```

The installed `kiro-cli acp` server may identify itself as a Kiro 2.x ACP agent even when `kiro-cli chat --agent-engine v3` uses the newer KAS/unified harness. These are separate execution paths.

## Permission handling

ACP is bidirectional. While the gateway waits for `session/prompt`, Kiro may send a JSON-RPC request such as:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "method": "session/request_permission",
  "params": {
    "sessionId": "...",
    "toolCall": {
      "title": "Writing gateway-test.txt",
      "kind": "edit",
      "rawInput": {}
    },
    "options": [
      {
        "optionId": "opaque-value",
        "name": "Allow once",
        "kind": "allow_once"
      }
    ]
  }
}
```

With `KIRO_PERMISSIONS=allow-once`, the gateway selects by the semantic `kind` and returns the exact option ID supplied by Kiro:

```json
{
  "jsonrpc": "2.0",
  "id": 5,
  "result": {
    "outcome": {
      "outcome": "selected",
      "optionId": "opaque-value"
    }
  }
}
```

With `KIRO_PERMISSIONS=deny`, it returns:

```json
{
  "outcome": {
    "outcome": "cancelled"
  }
}
```

Permission approval does not provide OS-level isolation. Use containers, VMs, separate users, or another sandbox when exposing this gateway beyond a trusted local environment.

## Security guidance

The gateway enables an AI coding agent to inspect files and potentially execute tools. Treat it as privileged developer infrastructure.

- Bind to `127.0.0.1` unless remote access is required.
- Configure `KIRO_API_KEY` before allowing any remote access.
- Place TLS and mature authentication in a reverse proxy for nonlocal deployments.
- Use a dedicated, canonical workspace rather than a broad home directory.
- Do not point `KIRO_WORKSPACE` at directories containing SSH keys, cloud credentials, browser profiles, password stores, or unrelated source repositories.
- Prefer `KIRO_PERMISSIONS=deny` until write and command behavior has been tested.
- Use `allow-once` only with a trusted caller and an isolated or disposable workspace.
- Run the gateway as a nonprivileged operating-system user.
- Consider a container, gVisor, Firecracker, or another execution sandbox.
- Add request-size, rate, and duration limits before multi-user deployment.
- Review Kiro and gateway logs for prompt text, file paths, command arguments, and sensitive output.
- Do not expose arbitrary `cwd` selection through the OpenAI request body.

## Current limitations

- Chat Completions only; `/v1/responses` is not implemented.
- Text input only.
- One Kiro subprocess and one ACP session per request.
- No persistent conversation/session affinity.
- No OpenAI client-defined tool calling.
- No structured output or JSON Schema enforcement.
- No token usage accounting.
- No reliable raw-ACP reasoning-effort setting.
- No interactive permission approval over HTTP.
- No tenant-specific workspaces or policies.
- No container or VM sandbox built into the supplied server.
- No retry policy for Kiro process failures.
- Model cache is local to each server process.

## Reasoning effort

The current raw `kiro-cli acp` server advertises model selection through `session/set_model`, but it does not expose a reliable programmatic effort-setting method. The gateway therefore rejects `reasoning_effort` rather than silently ignoring it.

The separate v3 headless path exposes effort metadata in its stream:

```bash
kiro-cli chat \
  --agent-engine v3 \
  --no-interactive \
  --output-format stream-json \
  "Respond with exactly: hello"
```

If explicit effort control is a requirement, add a second backend adapter for that v3 headless JSONL interface or update the ACP adapter when Kiro exposes effort through a stable ACP method.

## Production evolution

A more scalable implementation can replace one-process-per-request execution with a bounded worker pool:

```text
FastAPI
  |
  v
Request scheduler
  |
  +-- Kiro ACP worker 1
  |     +-- session A
  |     +-- session B
  |
  +-- Kiro ACP worker 2
        +-- session C
```

Potential improvements include:

- Persistent `kiro-cli acp` workers
- ACP session affinity and expiration
- `/v1/responses` support
- Tenant-to-workspace mapping
- Policy-based permissions by tool, command, path, and network destination
- Asynchronous human approval records and an approval API
- Container or microVM isolation
- Prometheus metrics and OpenTelemetry tracing
- Request rate limits and queue timeouts
- Kiro credit accounting and cost controls
- Stronger OpenAI request validation
- Graceful server shutdown with active Kiro turn cancellation

## Troubleshooting

### Kiro command not found

Set an explicit path:

```bash
export KIRO_CLI="/absolute/path/to/kiro-cli"
```

### Authentication failure

Open Kiro interactively and verify authentication:

```bash
kiro-cli chat
```

### Unknown model

List the models advertised by the active ACP server:

```bash
curl --silent http://127.0.0.1:8000/v1/models \
  -H "Authorization: Bearer $KIRO_API_KEY" |
jq -r '.data[].id'
```

### Requests cannot modify files

Check the permission policy:

```bash
curl --silent http://127.0.0.1:8000/health | jq '.permissions'
```

For a trusted disposable workspace, restart with:

```bash
export KIRO_PERMISSIONS=allow-once
python3 kiro_openai_gateway.py
```

### Streaming appears buffered

Use `curl -N` and ensure an HTTP reverse proxy does not buffer SSE responses. The gateway sends `X-Accel-Buffering: no` and `Cache-Control: no-cache`, but proxy configuration may still need adjustment.

### Detailed ACP diagnostics

```bash
export KIRO_DEBUG_ACP=true
python3 kiro_openai_gateway.py --log-level debug
```

### Too many Kiro processes

Reduce concurrency:

```bash
export KIRO_MAX_CONCURRENCY=1
```

Avoid combining high `KIRO_MAX_CONCURRENCY` values with multiple Uvicorn worker processes.

## Development test checklist

1. Verify `kiro-cli chat` works.
2. Start the gateway with a fixed test workspace.
3. Call `/health`.
4. Call `/v1/models`.
5. Submit a nonstreaming completion.
6. Submit an SSE streaming completion.
7. Test an invalid model and verify an OpenAI-shaped error.
8. Test a write request with `KIRO_PERMISSIONS=deny`.
9. Test a write request in a disposable workspace with `KIRO_PERMISSIONS=allow-once`.
10. Disconnect a streaming client and confirm the Kiro turn is cancelled.
11. Test the OpenAI Python SDK in both normal and streaming modes.
12. Review logs to ensure secrets and unwanted prompt content are not retained.
