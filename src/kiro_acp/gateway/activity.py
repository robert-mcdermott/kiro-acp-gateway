"""Render Kiro's own tool activity for the reasoning/text channels (agent mode)."""

from __future__ import annotations

import json

from kiro_acp.acp.types import PlanEntry, ToolCall, ToolCallStatus, ToolKind

MAX_CHARS = 2000
HIDDEN_ARGS = {"__tool_use_purpose", "summary", "purpose"}


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = text.rstrip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"\n… ({len(text) - limit} more chars)"


def _scalar(value: object) -> str:
    if isinstance(value, str):
        return (
            value
            if "\n" not in value and len(value) <= 200
            else json.dumps(value[:200] + ("…" if len(value) > 200 else ""))
        )
    return json.dumps(value, ensure_ascii=False)[:200]


def render_started(call: ToolCall, *, detail: str) -> str:
    lines = [f"⚙ {call.title or call.tool_name or call.kind.value}"]
    if detail == "full" and isinstance(call.raw_input, dict):
        # Kiro passes the model's one-line reason for the call as __tool_use_purpose.
        purpose = call.raw_input.get("__tool_use_purpose") or call.raw_input.get("purpose")
        if isinstance(purpose, str) and purpose.strip():
            lines.append(f"  purpose: {_scalar(purpose.strip())}")
        for key, value in call.raw_input.items():
            if key in HIDDEN_ARGS or value in (None, "", [], {}):
                continue
            if isinstance(value, (dict, list)) and len(json.dumps(value)) > 200:
                lines.append(f"  {key}: {_scalar(json.dumps(value)[:200])}")
            else:
                lines.append(f"  {key}: {_scalar(value)}")
    return _clip("\n".join(lines))


def render_completed(call: ToolCall, *, detail: str) -> str:
    state = "done" if call.status == ToolCallStatus.COMPLETED else "failed"
    header = f"⚙ {call.title or call.tool_name or call.kind.value} — {state}"
    if detail != "full":
        return header
    body = ""
    if call.kind == ToolKind.EDIT:
        diffs = [c for c in call.content if isinstance(c, dict) and c.get("type") == "diff"]
        if diffs:
            chunks = []
            for diff in diffs:
                old = diff.get("oldText") or ""
                new = diff.get("newText") or ""
                path = diff.get("path", "")
                removed = "".join(f"-{line}\n" for line in old.splitlines()) if old else ""
                added = "".join(f"+{line}\n" for line in new.splitlines())
                chunks.append(f"```diff\n--- {path}\n+++ {path}\n{removed}{added}```")
            body = "\n".join(chunks)
    if not body:
        output = call.output_text().strip()
        if output:
            if call.kind == ToolKind.SEARCH:
                lines = output.splitlines()
                body = f"↳ {len(lines)} result line(s)" + (f": {lines[0][:160]}" if lines else "")
            elif call.kind in (ToolKind.EXECUTE, ToolKind.READ, ToolKind.FETCH, ToolKind.OTHER):
                body = f"```\n{_clip(output, 1200)}\n```"
            else:
                body = _clip(output, 600)
    return _clip(header + ("\n" + body if body else ""))


def render_plan(entries: list[PlanEntry]) -> str:
    marks = {"completed": "x", "in_progress": "~", "pending": " "}
    lines = ["Plan:"]
    for entry in entries:
        lines.append(f"- [{marks.get(entry.status, ' ')}] {entry.content}")
    return _clip("\n".join(lines))
