"""Use the gateway from the official Anthropic Python SDK."""

import os

from anthropic import Anthropic

client = Anthropic(
    base_url=os.environ.get("KIRO_GATEWAY_URL", "http://127.0.0.1:8000"),
    api_key=os.environ.get("KIRO_GATEWAY_KEY", "your-gateway-key"),
    # Agent mode: the project Kiro's own tools work in (must match KIRO_GATEWAY_ALLOWED_WORKSPACES).
    default_headers={"X-Kiro-Workspace": os.environ.get("KIRO_WORKSPACE", os.getcwd())},
)

with client.messages.stream(
    model="claude-sonnet-4-6",  # Anthropic-style ids are mapped to Kiro's claude-sonnet-4.6
    max_tokens=1024,
    messages=[{"role": "user", "content": "Explain what this repository is for."}],
) as stream:
    for text in stream.text_stream:
        print(text, end="", flush=True)
print()
