from __future__ import annotations

from kiro_acp.gateway.limits import StreamLimiter


def feed_all(limiter: StreamLimiter, chunks: list[str]) -> str:
    out = ""
    for chunk in chunks:
        out += limiter.feed(chunk)
    return out + limiter.flush()


def test_stop_sequence_within_chunk() -> None:
    limiter = StreamLimiter(stop_sequences=["END"])
    assert feed_all(limiter, ["hello END world"]) == "hello "
    assert limiter.hit == "stop" and limiter.stop_sequence == "END"


def test_stop_sequence_across_chunks_and_holdback() -> None:
    limiter = StreamLimiter(stop_sequences=["<END>"])
    first = limiter.feed("abc<EN")
    assert first == "ab"  # the partial tail (len(stop) - 1 chars) is held back
    second = limiter.feed("D>tail")
    assert second == "c" and limiter.hit == "stop"  # "abc" emitted in total, nothing after the stop
    assert limiter.flush() == ""


def test_holdback_released_when_no_stop() -> None:
    limiter = StreamLimiter(stop_sequences=["STOP"])
    assert feed_all(limiter, ["abc", "def"]) == "abcdef"
    assert limiter.hit is None


def test_max_tokens_truncates() -> None:
    limiter = StreamLimiter(max_tokens=2)  # ~8 characters
    out = feed_all(limiter, ["0123456789abcdef"])
    assert out == "01234567" and limiter.hit == "length"
    assert limiter.feed("more") == ""


def test_inactive_limiter_passthrough() -> None:
    limiter = StreamLimiter()
    assert not limiter.active
    assert feed_all(limiter, ["a", "b"]) == "ab"
