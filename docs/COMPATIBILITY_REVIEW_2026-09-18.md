# API gateway compatibility review — 2026-09-18

## Scope and baseline

Reviewed commit: `45563a24adc3df7b911720c992576635ab1664b2`.

The intended goal is an API gateway that lets OpenAI and Anthropic clients,
scripts, and coding harnesses use the user's Kiro subscription and models through
`kiro-cli` ACP.

The protocol-neutral conversation model, separate ACP layer, and native MCP tool
bridge provide a useful foundation. The main remaining risks are protocol
correctness and session lifetime across multiple HTTP requests.

Validation performed during the review:

- `uv run pytest -q`: **220 passed, 2 skipped**.
- `uv run ruff check src tests`: **passed**.
- Focused in-process reproductions for pool overwrite, Responses streaming item
  indexes, MCP output controls, and SSE cleanup.
- Source inspection of tool selection and multimodal tool-result handling.
- OpenAI API contract checks against official documentation.

No implementation files were changed. Live Kiro integration tests were not run.
An additional standalone HTTP/MCP reproduction could not bind its Unix socket
inside the execution sandbox; the focused MCP reproduction used stubbed ACP
events instead. These findings do not claim live-model reproduction of every
downstream effect.

All findings below were open at the time of review. P1 means high priority for
reliable client compatibility; P2 means a correctness issue to address next.

## Findings

### 1. P1 — Session-pool collisions can orphan sessions and misroute continuations

**Sources:** [`backend.py`, `_acquire` / `_release`](../src/kiro_acp/gateway/backend.py),
[`conversation.py`, `canonical_message`](../src/kiro_acp/gateway/conversation.py).

The pool stores one session per conversation fingerprint. `_release()` assigns
`self._pool[fingerprint] = pooled` without closing or otherwise retaining an
existing entry under that key. Fingerprints intentionally omit tool-call IDs.

**Confirmed:** releasing two stub sessions under the same fingerprint left one
pool entry; the displaced session was neither closed nor tracked in the pool.

**Implication from source inspection:** two identical conversations can produce
different pending tool-call IDs yet share a pool key. A follow-up may acquire the
other conversation's pending turn, whose result-ID mapping does not match. The
current fallback can then supply a synthetic missing-result error. An overwritten
session also escapes pool-based idle cleanup.

**Suggested change:**

- Track every live session independently of the affinity index.
- Route pending tool continuations using tool-call identity, with explicit
  handling of ambiguous, expired, or unknown continuations.
- Use fingerprints to find eligible reusable sessions, rather than as their
  unique identity. Ensure replacement and eviction close all displaced resources.

**Acceptance tests:** two identical tool requests retain distinct pending turns;
each continuation reaches its own call IDs; replacement, expiration, cancellation,
and shutdown leave no untracked sessions or bridge registrations.

### 2. P1 — Responses streaming reuses output indexes across reasoning and tool calls

**Source:** [`openai_responses.py`, `stream_response`](../src/kiro_acp/gateway/protocols/openai_responses.py).

The tool-call branch closes an open text item but does not close an open reasoning
item. The shared `output_index` consequently refers to different items during the
same stream.

**Confirmed:** a stub backend yielding thought → tool call → done produced:

```text
response.output_item.added  index=0  reasoning
response.output_item.added  index=0  function_call
response.output_item.done   index=0  function_call
response.output_item.done   index=1  reasoning
```

Clients assembling responses by index can reject or incorrectly assemble this
sequence.

**Suggested change:** assign each item a stable index and explicit lifecycle.
Finalize open items consistently when transitioning between reasoning, text, and
tool calls. Maintain per-item buffers separately from whole-turn accumulated text.

**Acceptance tests:** feed reasoning → tool, text → reasoning → text, and multiple
tool/text transitions through the OpenAI SDK stream assembler. Verify stable
item IDs/indexes, paired lifecycle events, no duplicated text, and agreement
between assembled streaming output and the final response.

### 3. P1 — The default MCP path bypasses output limits and structured-output validation

**Source:** [`backend.py`, `run` / `_run_mcp`](../src/kiro_acp/gateway/backend.py).

The regular execution path constructs a `StreamLimiter`, but `_run_mcp()` forwards
`TextDelta` directly. The MCP branch also returns before the regular path's
structured-output validation/retry logic.

**Confirmed:** with stubbed ACP events, MCP execution returned the complete text
`one two three`, `finish="stop"`, and no stop-sequence metadata despite
`stop_sequences=["two"]`, `max_tokens=1`, and `enforce_max_tokens=True`.

**Suggested change:** share output-limit enforcement and structured-output policy
across execution modes. Coordinate truncation with pending-turn cancellation and
pool retention. Preserve the distinction between approximate token accounting and
exact stop-sequence matching.

**Acceptance tests:** exercise stop sequences spanning chunks, enabled token limits,
and invalid structured output under both MCP and emulation, streaming and
non-streaming. Verify protocol finish reasons and cleanup after truncation. Define
streaming structured-output behavior explicitly where retry is not possible.

### 4. P1 — MCP tool-selection constraints are not enforced

**Sources:** [`backend.py`, `_spawn` / `_run_mcp`](../src/kiro_acp/gateway/backend.py),
[`prompting.py`, `build_system_text`](../src/kiro_acp/gateway/prompting.py),
[`openai_responses.py`, `parse_tool_choice`](../src/kiro_acp/gateway/protocols/openai_responses.py).

MCP session creation exposes all supplied tools. MCP prompting uses
`emulate_tools=False`, so the instructions containing the requested tool choice
are not included. Required and named tool choices are not enforced on the result.
Responses `allowed_tools` is reduced to `auto`, losing its restriction.

**Confirmed:** focused MCP execution accepted a text-only completion with
`tool_choice="required"`. Source inspection establishes that named/allowed tool
restrictions are not applied to MCP exposure.

The [OpenAI Responses API reference](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
defines required, named, and allowed-tool selection behavior.

**Suggested change:** restrict exposed tools when necessary and validate emitted
calls. Include effective restrictions in session-reuse decisions. If a requested
guarantee cannot be implemented reliably, reject it explicitly instead of silently
treating it as automatic selection.

**Acceptance tests:** required choice cannot silently complete without a call;
named/allowed choices cannot emit other tools; restriction changes across reused
sessions take effect. Define and test parallel-tool controls as part of the same
compatibility contract.

### 5. P2 — SSE keepalive cleanup races the pending iterator task

**Source:** [`protocols/common.py`, `with_keepalive`](../src/kiro_acp/gateway/protocols/common.py).

The wrapper cancels its pending `__anext__()` task without awaiting it, then exits
the context that closes the source generator. Cancellation cleanup can still be
running when `aclose()` executes.

**Confirmed:** closing the wrapper after a keepalive, with a source that performs
asynchronous cleanup, raised:

```text
RuntimeError: aclose(): asynchronous generator is already running
```

**Suggested change:** cancel and await the pending task before closing the source,
while preserving cancellation propagation and bounded backend cleanup.

**Acceptance tests:** disconnect during a silent turn, immediately after a
keepalive, and while source cleanup awaits. Check that no iterator task is left
running and that turn/session resources are released.

### 6. P2 — MCP continuations lose tool-result images and accompanying user text

**Sources:** [`anthropic.py`, `tool_result_text` / `parse_user`](../src/kiro_acp/gateway/protocols/anthropic.py),
[`backend.py`, `_run_mcp`](../src/kiro_acp/gateway/backend.py),
[`mcp_turn.py`, `PendingTurn.deliver`](../src/kiro_acp/gateway/mcp_turn.py).

**Established by source inspection:** Anthropic parsing separates tool-result
images from the string result and retains them in the message's parts. The pending
MCP continuation forwards only string results, so those images are not delivered.
Text accompanying a tool result is retained in a tool-role message, but
`extra_text` checks only user-role messages, so that text is also missed.

This affects screenshot/browser tools and clients that combine tool results with
additional user instructions.

**Suggested change:** represent tool results as structured content blocks throughout
the conversation model and bridge. Preserve association between a result and its
images. Deliver accompanying user text deliberately; reject unsupported content
explicitly if it cannot reach Kiro.

**Acceptance tests:** round-trip a result containing text and an image, multiple
results with separate images, and a result plus additional user text. Assert what
reaches the bridge, not just what remains in the parsed conversation.

## Broader improvements to consider

### Publish a compatibility contract

Maintain a feature matrix for Chat Completions, Responses, and Anthropic Messages.
Classify each feature as supported, approximated, or rejected, and identify any
engine/tool-mode differences. Include tool choices, parallel calls, structured
output, token limits/accounting, image and document inputs, reasoning, response
storage, and unsupported tools or endpoints.

Prefer explicit errors to silently dropping a feature that changes the meaning
of a request. Describe the practical target as compatibility with supported
client workflows; an ACP agent adapter cannot automatically reproduce every
provider feature or instruction-hierarchy behavior.

### Add SDK and harness contract tests

The existing gateway tests primarily inspect HTTP JSON/SSE directly. Supplement
them with actual OpenAI and Anthropic SDK parsing and stream assembly, multi-turn
tool loops, parallel calls, retries, malformed requests, and disconnects. Parameterize
shared scenarios over MCP and emulation so the default mode receives equivalent
coverage.

Keep deterministic fake-agent tests, then add versioned, opt-in live Kiro and
harness smoke tests. Record client, SDK, Kiro CLI, and engine versions with each
verified compatibility result.

### Make server-side execution an explicit product choice

Requests without client tools currently enter Kiro agent mode. Consider a
tool-free default for ordinary API requests and explicit opt-in to server-side
agent execution. This would make script behavior more predictable and clarify
whether tools execute in the caller's harness or on the gateway host. Treat this
as a product decision with migration implications, rather than a confirmed defect.

## Suggested order of work

1. Session identity, pending-call routing, and resource ownership.
2. Responses streaming item lifecycle.
3. Shared output controls and tool-selection enforcement for MCP/emulation.
4. SSE cancellation cleanup and multimodal tool results.
5. SDK contract coverage and a published compatibility matrix.

Track implementation and verification separately: resolving a finding should
include a regression test for its triggering scenario, not only a code change.


## Resolution (2026-09-18, maintainers)

All six findings were confirmed against the code and fixed in the commit following this
review; each has a regression test in `tests/test_gateway.py` named for its scenario.

| Finding | Status | Where |
|---|---|---|
| 1. Session-pool collisions | Fixed. `_release` closes a displaced session; `_acquire` only reuses a pending session when the request carries one of its awaited call ids. | `test_identical_conversations_keep_distinct_pending_turns`, `test_displaced_session_is_closed_not_orphaned` |
| 2. Responses streaming indexes | Fixed. Each item takes its index when opened and closes with it; reasoning is closed before a tool call. | `test_responses_stream_assembles_with_openai_sdk` (real SDK assembler) |
| 3. MCP path limits and schema | Fixed. Shared `StreamLimiter`; schema validation reported without re-prompt. | `test_mcp_path_enforces_stop_sequences_and_schema` |
| 4. Tool-selection enforcement | Fixed. `ToolChoice.exposed()` filters what the bridge advertises (pool keyed on it); `required`/named end in `502 tool_choice_unsatisfied`; `allowed_tools` parsed on Chat and Responses. | `test_tool_choice_restricts_exposed_tools_in_mcp_mode`, `test_tool_choice_required_in_emulate_mode` |
| 5. Keepalive cleanup race | Fixed. The cancelled `__anext__` task is awaited before `aclose()`. | `test_keepalive_wrapper_closes_source_cleanly` |
| 6. Tool-result images and text | Fixed. `ToolResultPart.images` travel through the bridge as MCP image blocks; tool-role text is forwarded. | `test_tool_result_images_and_text_reach_the_bridge` |

Of the broader suggestions, SDK contract tests were added (OpenAI and Anthropic stream
assemblers against the fake agent) and the compatibility notes in the README were
extended. Making server-side agent execution opt-in was declined: the two-mode design is
deliberate, documented, and bounded by the workspace allow-list and permission policy.
Coding-harness behaviour was left unchanged by every fix (harnesses use `tool_choice`
`auto`, no stop sequences, and run their own tools).
