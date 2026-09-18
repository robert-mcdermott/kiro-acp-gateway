"""Protocol-neutral conversation model shared by the OpenAI and Anthropic adapters."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

JSON = dict[str, Any]
Role = Literal["user", "assistant", "tool"]


@dataclass(slots=True)
class TextPart:
    text: str


@dataclass(slots=True)
class ImagePart:
    mime_type: str
    data_base64: str


@dataclass(slots=True)
class ToolCallPart:
    """An assistant-issued call to a client-defined tool."""

    id: str
    name: str
    arguments: JSON


@dataclass(slots=True)
class ToolResultPart:
    call_id: str
    content: str
    is_error: bool = False
    name: str | None = None
    images: list[ImagePart] = field(default_factory=list)
    """Images returned by the tool (Anthropic ``tool_result`` blocks; e.g. a screenshot)."""


Part = TextPart | ImagePart | ToolCallPart | ToolResultPart


@dataclass(slots=True)
class Message:
    role: Role
    parts: list[Part] = field(default_factory=list)
    name: str | None = None

    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPart))

    @property
    def tool_calls(self) -> list[ToolCallPart]:
        return [p for p in self.parts if isinstance(p, ToolCallPart)]

    @property
    def tool_results(self) -> list[ToolResultPart]:
        return [p for p in self.parts if isinstance(p, ToolResultPart)]

    @property
    def images(self) -> list[ImagePart]:
        return [p for p in self.parts if isinstance(p, ImagePart)]


@dataclass(slots=True)
class ToolDef:
    name: str
    description: str = ""
    parameters: JSON = field(default_factory=lambda: {"type": "object", "properties": {}})
    kind: str = "function"
    """``function`` (JSON arguments) or ``custom`` (OpenAI freeform tool: one raw text input)."""


@dataclass(slots=True)
class ToolChoice:
    """``mode``: auto | none | required | named (then ``name`` is set).

    ``names`` restricts the tools the model may see (OpenAI ``allowed_tools``); with
    ``mode="required"`` one of them must be called.
    """

    mode: str = "auto"
    name: str | None = None
    names: list[str] = field(default_factory=list)

    def exposed(self, tools: list) -> list:
        """The subset of ``tools`` the model should be offered."""
        if self.mode == "none":
            return []
        if self.mode == "named":
            return [t for t in tools if t.name == self.name] or tools
        if self.names:
            allowed = set(self.names)
            return [t for t in tools if t.name in allowed]
        return tools

    @property
    def must_call(self) -> bool:
        return self.mode in ("required", "named")


@dataclass(slots=True)
class JsonOutput:
    """Structured output request (OpenAI ``response_format`` / Anthropic ``output_config.format``)."""

    schema: JSON | None = None
    name: str | None = None


@dataclass
class Conversation:
    system: str = ""
    messages: list[Message] = field(default_factory=list)
    tools: list[ToolDef] = field(default_factory=list)
    tool_choice: ToolChoice = field(default_factory=ToolChoice)
    json_output: JsonOutput | None = None
    effort: str | None = None
    metadata: JSON = field(default_factory=dict)

    # ------------------------------------------------------------------ fingerprints

    def prefix_length_for_affinity(self) -> int:
        """Index of the first message after the last assistant turn.

        Messages before that index should already be known to a reused session
        (they were produced in earlier requests); everything from it on is new.
        """
        for index in range(len(self.messages) - 1, -1, -1):
            if self.messages[index].role == "assistant":
                return index + 1
        return 0

    def fingerprint(self, upto: int | None = None) -> str:
        """Stable hash of system + tools + the first ``upto`` messages (canonical form)."""
        payload = {
            "system": self.system.strip(),
            "tools": sorted(canonical_tool(t) for t in self.tools),
            "messages": [
                canonical_message(m)
                for m in self.messages[: len(self.messages) if upto is None else upto]
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def fingerprint_after(self, assistant: Message) -> str:
        """Fingerprint a client would present after echoing ``assistant`` back."""
        extended = Conversation(
            system=self.system, messages=[*self.messages, assistant], tools=self.tools
        )
        return extended.fingerprint()

    def total_chars(self) -> int:
        total = len(self.system)
        for message in self.messages:
            for part in message.parts:
                if isinstance(part, TextPart):
                    total += len(part.text)
                elif isinstance(part, ImagePart):
                    total += len(part.data_base64)
                elif isinstance(part, ToolCallPart):
                    total += len(json.dumps(part.arguments))
                elif isinstance(part, ToolResultPart):
                    total += len(part.content)
        return total


def canonical_tool(tool: ToolDef) -> str:
    return json.dumps(
        {"n": tool.name, "d": tool.description, "p": tool.parameters, "k": tool.kind},
        sort_keys=True,
    )


def canonical_message(message: Message) -> JSON:
    """Canonical, id-free form so a client's echo of our reply hashes identically."""
    text = "".join(p.text for p in message.parts if isinstance(p, TextPart)).strip()
    calls = [
        {"name": p.name, "arguments": json.dumps(p.arguments, sort_keys=True, ensure_ascii=False)}
        for p in message.parts
        if isinstance(p, ToolCallPart)
    ]
    results = [
        {"content": p.content.strip(), "error": bool(p.is_error)}
        for p in message.parts
        if isinstance(p, ToolResultPart)
    ]
    images = [
        hashlib.sha1(p.data_base64.encode()).hexdigest()
        for p in message.parts
        if isinstance(p, ImagePart)
    ]
    payload: JSON = {"role": message.role, "text": text}
    if calls:
        payload["calls"] = calls
    if results:
        payload["results"] = results
    if images:
        payload["images"] = images
    return payload
