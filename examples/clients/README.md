# Client examples

Ready-to-use configurations for the clients verified against the gateway. Each assumes a
gateway at `http://127.0.0.1:8000` with `KIRO_GATEWAY_API_KEY=your-gateway-key`; change
both to match yours. Model ids come from `curl -s $URL/v1/models -H "Authorization: Bearer $KEY"`.

| Client | File | Mode exercised | Verify with |
|---|---|---|---|
| curl | `curl.sh` | agent (no tools) | `sh curl.sh` |
| Python `requests` | `requests_example.py` | agent, streaming, structured output | `python requests_example.py` |
| OpenAI SDK | `openai_sdk.py` | agent, plus function calling from a script (harness) | `python openai_sdk.py` |
| Anthropic SDK | `anthropic_sdk.py` | agent | `python anthropic_sdk.py` |
| Claude Code | `claude-code.sh` | harness (Claude Code runs the tools) | `sh claude-code.sh` then ask "what does this project do?" |
| Codex CLI | `codex-config.toml` | harness (direct tools or code mode) | `codex exec "list the files here"` |
| OpenCode | `opencode.json` | harness | `opencode` and pick the Kiro provider |
| Any OpenAI-compatible harness (Collomia, Kilo, Cline, Continue, LiteLLM, Vercel AI SDK) | see `generic-openai-compatible.md` | harness | its own model list |

Recommended models: `claude-sonnet-4.6` or `claude-opus-4.8` for agent work, `gpt-5.6-luna`
as the small fast model (Claude Code background calls, Codex code mode).
