"""Use the gateway from the official OpenAI Python SDK, including function calling."""

import json
import os
import subprocess

from openai import OpenAI

client = OpenAI(
    base_url=os.environ.get("KIRO_GATEWAY_URL", "http://127.0.0.1:8000") + "/v1",
    api_key=os.environ.get("KIRO_GATEWAY_KEY", "your-gateway-key"),
    # Agent mode: the project Kiro's own tools work in (must match KIRO_GATEWAY_ALLOWED_WORKSPACES).
    # Ignored for requests that carry tools, which the caller executes itself.
    default_headers={"X-Kiro-Workspace": os.environ.get("KIRO_WORKSPACE", os.getcwd())},
)

# Plain completion (agent mode).
reply = client.chat.completions.create(
    model="claude-sonnet-4.6",
    messages=[{"role": "user", "content": "What is in this directory? One sentence."}],
)
print(reply.choices[0].message.content)

# Function calling: sending `tools` switches the request to harness mode, so this script
# (not Kiro) executes the tool and returns the result. Works exactly like OpenAI.
tools = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command and return its output",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]
messages = [
    {"role": "user", "content": "How many files are in the current directory? Use the tool."}
]
first = client.chat.completions.create(model="gpt-5.6-luna", messages=messages, tools=tools)
message = first.choices[0].message
messages.append(message)
for call in message.tool_calls or []:
    args = json.loads(call.function.arguments)
    output = subprocess.run(args["command"], shell=True, capture_output=True, text=True).stdout
    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
final = client.chat.completions.create(model="gpt-5.6-luna", messages=messages, tools=tools)
print(final.choices[0].message.content)
