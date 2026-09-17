"""Prometheus text-format metrics without external dependencies."""

from __future__ import annotations

import threading
from collections import defaultdict

_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300, 600, 900)
Labels = tuple[tuple[str, str], ...]


def _fmt(labels: Labels) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{str(v).replace(chr(34), chr(39))}"' for k, v in labels)
    return "{" + inner + "}"


class Metrics:
    """Counters, gauges, and one latency histogram for gateway turns."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.turns: dict[Labels, int] = defaultdict(int)
        self.credits: dict[Labels, float] = defaultdict(float)
        self.reuse: dict[Labels, int] = defaultdict(int)
        self.errors: dict[Labels, int] = defaultdict(int)
        self.latency_buckets: dict[Labels, list[int]] = defaultdict(lambda: [0] * len(_BUCKETS))
        self.latency_sum: dict[Labels, float] = defaultdict(float)
        self.latency_count: dict[Labels, int] = defaultdict(int)

    def record_turn(
        self,
        *,
        mode: str,
        engine: str,
        model: str,
        finish: str,
        seconds: float,
        credits: float,
        reused: bool,
    ) -> None:
        base: Labels = (("mode", mode), ("engine", engine), ("model", model))
        with self._lock:
            self.turns[base + (("finish", finish),)] += 1
            if credits:
                self.credits[(("model", model),)] += credits
            self.reuse[(("reused", "true" if reused else "false"),)] += 1
            buckets = self.latency_buckets[base]
            for index, bound in enumerate(_BUCKETS):
                if seconds <= bound:
                    buckets[index] += 1
            self.latency_sum[base] += seconds
            self.latency_count[base] += 1

    def record_error(self, code: str, status: int) -> None:
        with self._lock:
            self.errors[(("code", code), ("status", str(status))),] += 1

    def render(self, *, active_turns: int, live_sessions: int, models_cached: int) -> str:
        lines: list[str] = []

        def header(name: str, kind: str, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

        with self._lock:
            header(
                "kiro_gateway_turns_total",
                "counter",
                "Completed turns by mode, engine, model, finish reason.",
            )
            for labels, value in sorted(self.turns.items()):
                lines.append(f"kiro_gateway_turns_total{_fmt(labels)} {value}")
            header("kiro_gateway_credits_total", "counter", "Kiro credits consumed, by model.")
            for labels, value in sorted(self.credits.items()):
                lines.append(f"kiro_gateway_credits_total{_fmt(labels)} {value:.6f}")
            header(
                "kiro_gateway_session_reuse_total",
                "counter",
                "Turns that reused a live Kiro session vs started one.",
            )
            for labels, value in sorted(self.reuse.items()):
                lines.append(f"kiro_gateway_session_reuse_total{_fmt(labels)} {value}")
            header(
                "kiro_gateway_errors_total",
                "counter",
                "API errors by gateway error code and HTTP status.",
            )
            for labels, value in sorted(self.errors.items()):
                lines.append(f"kiro_gateway_errors_total{_fmt(labels)} {value}")
            header("kiro_gateway_turn_seconds", "histogram", "Turn latency in seconds.")
            for labels, buckets in sorted(self.latency_buckets.items()):
                for index, bound in enumerate(_BUCKETS):
                    lines.append(
                        f"kiro_gateway_turn_seconds_bucket{_fmt(labels + (('le', str(bound)),))} {buckets[index]}"
                    )
                lines.append(
                    f"kiro_gateway_turn_seconds_bucket{_fmt(labels + (('le', '+Inf'),))} {self.latency_count[labels]}"
                )
                lines.append(
                    f"kiro_gateway_turn_seconds_sum{_fmt(labels)} {self.latency_sum[labels]:.3f}"
                )
                lines.append(
                    f"kiro_gateway_turn_seconds_count{_fmt(labels)} {self.latency_count[labels]}"
                )
        header("kiro_gateway_active_turns", "gauge", "Turns currently running.")
        lines.append(f"kiro_gateway_active_turns {active_turns}")
        header("kiro_gateway_live_sessions", "gauge", "Idle Kiro sessions kept for reuse.")
        lines.append(f"kiro_gateway_live_sessions {live_sessions}")
        header("kiro_gateway_models_cached", "gauge", "Models in the cached catalogue.")
        lines.append(f"kiro_gateway_models_cached {models_cached}")
        return "\n".join(lines) + "\n"
