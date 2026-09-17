# Any OpenAI-compatible client

Give the client these three values and nothing else:

| Setting | Value |
|---|---|
| Base URL | `http://127.0.0.1:8000/v1` (Anthropic-style clients: `http://127.0.0.1:8000`) |
| API key | the gateway's `KIRO_GATEWAY_API_KEY` |
| Model | a Kiro model id from `GET /v1/models`, e.g. `claude-sonnet-4.6` |

Supported over Chat Completions: streaming, tool calling (parallel calls too), images
(base64), `reasoning_effort`, `response_format` JSON schemas, `stop`, and reasoning
content in `reasoning_content`. The Responses API is available for clients that prefer it.

Examples of clients that only need those values: Collomia, Kilo Code, Cline, Continue,
Aider (`--openai-api-base`), LiteLLM (`openai/<model>` with `api_base`), the Vercel AI SDK
(`createOpenAI({ baseURL })`), LangChain's `ChatOpenAI(base_url=...)`.

If a client always attaches tools you do not want executed client-side, run the gateway
with `KIRO_GATEWAY_TOOL_MODE=ignore` so those requests use Kiro's own tools instead.
