"""Optional sanitizer for client system prompts (``KIRO_GATEWAY_SANITIZE_SYSTEM``).

The harness framing keeps Kiro's identity, which resolved model refusals in
testing. As a second, opt-in layer this module strips lines from the client's
system prompt that assert a different identity or demand concealment, which are
the lines most likely to trigger "prompt injection" refusals. Everything else
(project instructions, tool guidance, memory) is kept verbatim.
"""

from __future__ import annotations

import re

_PRODUCTS = r"(Claude(?: Code)?|Codex|Copilot|ChatGPT|GPT-\d[\w.-]*|Gemini|Cursor|OpenCode|Kiro)"
_PATTERNS = [
    re.compile(rf"^\s*(You are|I am|This is|You must identify as)\b.*\b{_PRODUCTS}\b", re.I),
    re.compile(
        r"^\s*You are (an? )?(interactive |AI |autonomous )?(CLI |coding )?(tool|agent|assistant) (made|created|developed|built) by\b",
        re.I,
    ),
    re.compile(
        r"^\s*You are (NOT|never)\b.*\b(other AI|another AI|different (AI|model|identity)|Claude|Kiro)\b",
        re.I,
    ),
    re.compile(
        r"^\s*(Never|Do not|Don't) (say|claim|admit|reveal|disclose|mention)\b.*\b(you are|your (identity|model|provider)|gateway|proxy|bridge)\b",
        re.I,
    ),
    re.compile(
        r"^\s*(Ignore|Override|Disregard)\b.*\b(instructions?|prompts?)\b.*\b(contradict|conflict|previous|above)\b",
        re.I,
    ),
    re.compile(r"^\s*#+\s*(Identity|Persona)\s*$", re.I),
]


def sanitize_system(text: str) -> tuple[str, int]:
    """Return the sanitized text and the number of lines removed."""
    kept: list[str] = []
    removed = 0
    for line in text.splitlines():
        if any(pattern.search(line) for pattern in _PATTERNS):
            removed += 1
            continue
        kept.append(line)
    return "\n".join(kept), removed
