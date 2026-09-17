# Recorded ACP frames

Real `kiro-cli acp` sessions captured with `KIRO_ACP_RECORD_FRAMES=<dir>` (see
`src/kiro_acp/acp/recorder.py`), with the home directory name scrubbed. Each file starts
with a header line naming the client, command, engine, model, and date. They are replayed
in tests by `tests/fake_agent/replay.py` so the client's parsing of a given Kiro version's
wire shapes is checked without Kiro installed.

| File | Kiro CLI | Engine | Content |
|---|---|---|---|
| `kiro-cli-2.22.0-v3-hello.jsonl` | 2.22.0 | v3 | `kiro-acp prompt "Reply with exactly the single word: hello"` with `gpt-5.6-luna`: initialize, session/new (config options, model catalogue), set_config_option, prompt turn. |

To add one: run the CLI or gateway with the environment variable set, pick the file that
contains the frames you care about, replace your user name, and add a row here.
