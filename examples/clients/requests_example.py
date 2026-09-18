"""Use the gateway from plain Python with the requests library (no SDK)."""

import json
import os

import requests

GATEWAY = os.environ.get("KIRO_GATEWAY_URL", "http://127.0.0.1:8000")
KEY = os.environ.get("KIRO_GATEWAY_KEY", "your-gateway-key")
# Agent mode: Kiro's own tools run in WORKSPACE on the gateway host, so send the project
# directory (default: where this script runs). It must match KIRO_GATEWAY_ALLOWED_WORKSPACES.
WORKSPACE = os.environ.get("KIRO_WORKSPACE", os.getcwd())
HEADERS = {
    "Authorization": f"Bearer {KEY}",
    "Content-Type": "application/json",
    "X-Kiro-Workspace": WORKSPACE,
}

# 1. Non-streaming chat completion (agent mode: Kiro may use its own tools in the workspace)
r = requests.post(
    f"{GATEWAY}/v1/chat/completions",
    headers=HEADERS,
    json={
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "Describe this directory in two sentences."}],
        "reasoning_effort": "low",
    },
    timeout=900,
)
r.raise_for_status()
body = r.json()
print(body["choices"][0]["message"]["content"])
print("credits:", body["kiro"]["credits"], "session:", body["kiro"]["session_id"])

# 2. Streaming (Server-Sent Events)
with requests.post(
    f"{GATEWAY}/v1/chat/completions",
    headers=HEADERS,
    json={
        "model": "gpt-5.6-luna",
        "messages": [{"role": "user", "content": "Count to five."}],
        "stream": True,
    },
    stream=True,
    timeout=900,
) as stream:
    for line in stream.iter_lines():
        if not line.startswith(b"data: "):
            continue
        data = line[6:]
        if data == b"[DONE]":
            break
        print(json.loads(data)["choices"][0]["delta"].get("content", ""), end="", flush=True)
print()

# 3. Structured output (OpenAI response_format with a JSON schema; validated by the gateway)
r = requests.post(
    f"{GATEWAY}/v1/chat/completions",
    headers=HEADERS,
    json={
        "model": "claude-sonnet-4.6",
        "messages": [{"role": "user", "content": "Classify: 'fix: null check in parser'"}],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "commit",
                "schema": {
                    "type": "object",
                    "properties": {"kind": {"type": "string"}, "scope": {"type": "string"}},
                    "required": ["kind"],
                },
            },
        },
    },
    timeout=900,
)
print(
    json.loads(r.json()["choices"][0]["message"]["content"]),
    "valid:",
    r.json()["kiro"]["schema_valid"],
)
