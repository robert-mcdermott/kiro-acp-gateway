"""Gateway-side enforcement of ``stop`` sequences and ``max_tokens``.

Kiro ignores both parameters, so the gateway watches the text it forwards and
cancels the Kiro turn when a limit is hit. Stop sequences may span chunk
boundaries, so the last ``len(longest_stop) - 1`` characters are held back
until more text arrives or the stream ends.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kiro_acp.gateway.turn import estimate_tokens


@dataclass
class StreamLimiter:
    stop_sequences: list[str] = field(default_factory=list)
    max_tokens: int | None = None
    hit: str | None = None  # "stop" or "length"
    stop_sequence: str | None = None
    _buffer: str = ""
    _emitted_tokens: int = 0

    @property
    def active(self) -> bool:
        return bool(self.stop_sequences) or self.max_tokens is not None

    @property
    def _holdback(self) -> int:
        return (
            max((len(s) for s in self.stop_sequences), default=0) - 1 if self.stop_sequences else 0
        )

    def feed(self, text: str) -> str:
        """Return the text safe to forward; ``hit`` is set when a limit triggered."""
        if self.hit or not text:
            return ""
        self._buffer += text
        if self.stop_sequences:
            earliest: tuple[int, str] | None = None
            for sequence in self.stop_sequences:
                index = self._buffer.find(sequence)
                if index >= 0 and (earliest is None or index < earliest[0]):
                    earliest = (index, sequence)
            if earliest is not None:
                out = self._buffer[: earliest[0]]
                self._buffer = ""
                self.hit = "stop"
                self.stop_sequence = earliest[1]
                return self._cap(out)
        keep = max(self._holdback, 0)
        out = self._buffer[: len(self._buffer) - keep] if keep else self._buffer
        self._buffer = self._buffer[len(out) :]
        return self._cap(out)

    def flush(self) -> str:
        out, self._buffer = self._buffer, ""
        return self._cap(out) if not self.hit else ""

    def _cap(self, out: str) -> str:
        if self.max_tokens is None or not out:
            return out
        remaining = self.max_tokens - self._emitted_tokens
        if remaining <= 0:
            self.hit = self.hit or "length"
            return ""
        tokens = estimate_tokens(out)
        if tokens > remaining:
            out = out[: remaining * 4]
            self.hit = self.hit or "length"
            self._emitted_tokens = self.max_tokens
            return out
        self._emitted_tokens += tokens
        return out
