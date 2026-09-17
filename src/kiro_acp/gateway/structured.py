"""Structured-output helpers: fence stripping and JSON Schema validation."""

from __future__ import annotations

import json
import re
from typing import Any

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def strip_json_fences(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text.strip()


def validate_json_reply(text: str, schema: dict[str, Any] | None) -> tuple[bool, list[str]]:
    """Return (valid, error messages). Without a schema only JSON syntax is checked."""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        return False, [f"invalid JSON: {error.msg} at position {error.pos}"]
    if not schema:
        return True, []
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        return True, []
    validator = jsonschema.Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda e: list(e.path))
    if not errors:
        return True, []
    messages = []
    for error in errors[:10]:
        location = "/".join(str(p) for p in error.absolute_path) or "$"
        messages.append(f"{location}: {error.message}")
    return False, messages
