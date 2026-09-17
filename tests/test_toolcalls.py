from __future__ import annotations

from kiro_acp.gateway.backend import normalize_model_name
from kiro_acp.gateway.conversation import (
    Conversation,
    Message,
    TextPart,
    ToolCallPart,
    ToolResultPart,
)
from kiro_acp.gateway.toolcalls import ToolCallParser


def feed_all(parser: ToolCallParser, chunks: list[str]):
    text = ""
    calls = []
    for chunk in chunks:
        t, c = parser.feed(chunk)
        text += t
        calls.extend(c)
    t, c = parser.flush()
    return text + t, calls + c


def test_parser_plain_text_passthrough() -> None:
    text, calls = feed_all(ToolCallParser(), ["hello ", "world <b>", " done"])
    assert text == "hello world <b> done" and calls == []


def test_parser_extracts_call_split_across_chunks() -> None:
    chunks = [
        "Let me look. <tool_c",
        'all>{"name": "Bash", "argu',
        'ments": {"command": "ls"}}</tool_call>',
    ]
    text, calls = feed_all(ToolCallParser(), chunks)
    assert text.strip() == "Let me look."
    assert len(calls) == 1 and calls[0].name == "Bash" and calls[0].arguments == {"command": "ls"}


def test_parser_multiple_calls_and_fenced_json() -> None:
    body = '<tool_call>```json\n{"name": "a", "arguments": {}}\n```</tool_call>\n<tool_call>{"name":"b","arguments":"{\\"x\\":1}"}</tool_call>'
    text, calls = feed_all(ToolCallParser(), [body])
    assert [c.name for c in calls] == ["a", "b"]
    assert calls[1].arguments == {"x": 1}
    assert text.strip() == ""


def test_parser_invalid_json_is_emitted_as_text() -> None:
    text, calls = feed_all(ToolCallParser(), ["<tool_call>not json</tool_call>"])
    assert calls == [] and text == "<tool_call>not json</tool_call>"


def test_parser_unterminated_call_flush() -> None:
    text, calls = feed_all(ToolCallParser(), ['<tool_call>{"name": "x", "arguments": {}}'])
    assert len(calls) == 1 and calls[0].name == "x" and text == ""


def test_parser_disabled() -> None:
    parser = ToolCallParser(enabled=False)
    assert parser.feed('<tool_call>{"name":"x"}</tool_call>') == (
        '<tool_call>{"name":"x"}</tool_call>',
        [],
    )


def test_normalize_model_name() -> None:
    assert normalize_model_name("claude-sonnet-4-5-20250929") == "claude-sonnet-4.5"
    assert normalize_model_name("claude-haiku-4-5") == "claude-haiku-4.5"
    assert normalize_model_name("claude-opus-4.6") == "claude-opus-4.6"
    assert normalize_model_name("Claude-Sonnet-4-6-latest") == "claude-sonnet-4.6"
    assert normalize_model_name("gpt-5.6-terra") == "gpt-5.6-terra"
    assert normalize_model_name("kiro-gpt-5.6-luna") == "gpt-5.6-luna"
    assert normalize_model_name("kiro/claude-sonnet-4-6") == "claude-sonnet-4.6"


def test_fingerprint_matches_after_echo() -> None:
    conv = Conversation(system="sys", messages=[Message("user", [TextPart("hi")])])
    reply = Message(
        "assistant", [TextPart("hello "), ToolCallPart("call_1", "Bash", {"command": "ls"})]
    )
    after = conv.fingerprint_after(reply)
    echoed = Conversation(
        system="sys",
        messages=[
            Message("user", [TextPart("hi")]),
            Message(
                "assistant",
                [TextPart("hello"), ToolCallPart("different_id", "Bash", {"command": "ls"})],
            ),
            Message("tool", [ToolResultPart("different_id", "a.txt")]),
        ],
    )
    assert echoed.prefix_length_for_affinity() == 2
    assert echoed.fingerprint(2) == after
    assert echoed.fingerprint() != after
