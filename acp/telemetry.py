"""In-memory counter registry for ACP telemetry.

Every counter name is pre-registered at construction time (from
`COUNTER_NAMES` below) and defaults to 0; incrementing or reading an
unregistered name raises `TelemetryError` rather than silently
creating a new counter, so a typo'd name fails loudly instead of
quietly going nowhere.

This wave is in-memory only. A later wave may add persistence: the
constructor accepts an optional `sink` callback invoked as
`sink(name, new_value)` on every increment, which is enough hook point
for a subclass or caller to fan counter updates out to a file, metrics
endpoint, etc., without this class needing to know about any of that.
"""
from __future__ import annotations

from typing import Callable

COUNTER_NAMES: tuple[str, ...] = (
    "compression_attempts",
    "compression_successes",
    "compression_input_tokens",
    "compression_output_tokens",
    "compression_saved_tokens",
    "compression_bypass_payloads",
    "compression_bypass_tokens",
    "compression_availability",
    "compression_queue_timeouts",
    "compression_execution_timeouts",
    "compression_total_timeouts",
    "compression_timeout_bypass_tokens",
    "compression_execution_ms",
    "synchronous_gate_wait_ms",
    "synchronous_gate_cache_hits",
    "synchronous_gate_cache_misses",
)


class TelemetryError(ValueError):
    """Raised for an unregistered counter name."""


class Telemetry:
    def __init__(self, sink: Callable[[str, int], None] | None = None) -> None:
        self._counters: dict[str, int] = {name: 0 for name in COUNTER_NAMES}
        self._sink = sink

    def increment(self, name: str, amount: int = 1) -> int:
        if name not in self._counters:
            raise TelemetryError(f"unregistered counter: {name!r}")
        new_value = self._counters[name] + amount
        self._counters[name] = new_value
        if self._sink is not None:
            self._sink(name, new_value)
        return new_value

    def get(self, name: str) -> int:
        if name not in self._counters:
            raise TelemetryError(f"unregistered counter: {name!r}")
        return self._counters[name]

    def snapshot(self) -> dict[str, int]:
        return dict(self._counters)

    def reset(self) -> None:
        for name in self._counters:
            self._counters[name] = 0
