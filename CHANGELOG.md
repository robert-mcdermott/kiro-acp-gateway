# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.1] - 2026-09-18

### Fixed

- Dashboard: clicking a session in "Recent sessions" opened the audit endpoint as a plain
  navigation, which cannot carry the API key and answered `401`. Records now open in a
  side panel inside the dashboard, fetched with the stored key, with a summary and the
  redacted details of every turn, tool call, permission decision, and stall.

### Added

- `kiro-gateway --env-file PATH`, and automatic loading of `~/.config/kiro-gateway/.env`
  when the current directory has no `.env`, so a globally installed gateway can be started
  from any directory with one configuration file.
- README: a numbered global-install procedure (`uv tool install .`, PATH check, config
  file, verification, update, uninstall), a "Permissions in agent mode" section with
  ready-to-copy `.env` configurations and the rule syntax, and an explanation of how the
  gateway's policy layers on Kiro's own `permissions.yaml` in agent and harness mode.

### Changed

- Agent-mode callers (scripts, curl, the SDKs) are now shown naming their own project
  with `X-Kiro-Workspace`, and the gateway examples start with
  `KIRO_GATEWAY_ALLOWED_WORKSPACES` plus a scratch fallback workspace instead of the
  gateway's checkout. All client examples send the header; the CLI's `--cwd` is
  documented. Harness clients are unaffected.
- The README start example and `.env.example` use `KIRO_GATEWAY_PERMISSIONS=allow-always`
  and explain what `deny` actually does.
- The `legacy/` proof-of-concept scripts were removed; they remain in the git history.

## [0.1.0] - 2026-09-17

First release. Verified against Kiro CLI 2.22 on macOS with Claude Code, Codex CLI,
OpenCode, Collomia, the OpenAI and Anthropic Python SDKs, curl, and plain `requests`.

### Library (`kiro_acp.acp`)

- ACP client for `kiro-cli acp`: JSON-RPC over stdio, both Kiro engines (v2 Rust CLI and
  v3 Kiro Agent Server), sessions as event streams, permission policies and rules, client
  file-system and terminal capabilities, session listing and deletion.
- Engine-aware model, mode and effort control (v3 config options; v2 `session/set_model`
  and effort through `_kiro.dev/commands/execute`), cold-start catalogue retry, per-session
  process groups, and an ACP frame recorder with a replay agent for tests.

### CLI (`kiro-acp`)

- `prompt`, `chat`, `models`, `agents`, `sessions` (list, delete, prune), `info`,
  `doctor` (engine checks, orphan detection) and `codex-catalog`.

### Gateway (`kiro-gateway`)

- OpenAI Chat Completions, OpenAI Responses (including freeform `custom` tools), legacy
  Completions and Anthropic Messages APIs, streaming and non-streaming, with images,
  effort, stop sequences, `max_tokens`, structured output with schema validation, model
  aliases and a model catalogue with context windows.
- Two modes chosen per request: harness mode bridges the client's tools to Kiro as native
  MCP tool calls (or an emulated text protocol) and runs Kiro in an empty scratch
  directory; agent mode lets Kiro use its own tools inside the configured workspace.
- Harness support verified for Claude Code (OAuth-token pitfall documented), Codex CLI
  (direct tools and code mode; the Codex model catalogue is served from the gateway),
  OpenCode and any OpenAI-compatible client.
- Session affinity with a bounded pool, per-request workspaces (allow-listed), per-request
  MCP servers from a catalogue or discovered client config files, inline agent
  definitions on the v3 engine, and a `kiro` body extension for all per-request options.
- Reliability: SSE keepalives, queue and rate limits, graceful shutdown, classified Kiro
  errors with `Retry-After`, refusal surfacing, image size guard, stall detection with a
  continue-nudge, and never regressing the model catalogue on an empty answer.
- Operations: Prometheus `/metrics`, a self-contained live dashboard with light and dark
  themes, an audit ledger with secret redaction, health endpoint, Docker/Podman image,
  launchd and systemd installers, GitHub Actions CI, and ready-to-use client examples.

[0.1.1]: https://github.com/robert-mcdermott/kiro-acp-gateway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/robert-mcdermott/kiro-acp-gateway/releases/tag/v0.1.0
