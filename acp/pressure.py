"""Proactive per-receiver context-pressure tracking with hysteresis.

Tracks how close each receiving context is to its native auto-compaction
boundary (agent_protocols_v1 background-compression adjustment, §22-23)
and maps an observed ratio in `[0.0, 1.0]` to one of four `PressureMode`
values. State is isolated per `ReceiverKey` (`(host, session_id,
agent_id-or-None)`) -- one receiver's pressure never influences another's
mode, and this module never decides *what* to compress, only *how urgent*
proactive preparation currently is for a given receiver (see
`should_run_maintenance`).

Watermarks are v1 starting defaults (§23 calls them "Phase 6 benchmark
hypotheses", not permanent constants) exposed as an overridable
`PressureWatermarks` dataclass, mirroring `acp/gate.py`'s
"tunable, not baked into function bodies" discipline.

Hysteresis: promotion into a more severe mode happens immediately at the
raw watermark. Demotion out of a mode requires the ratio to fall a fixed
margin (`hysteresis_margin`, default 5 percentage points) below the
watermark that triggered entry into the *current* mode -- not below
whatever lower watermark the new, less-severe mode would itself use. This
is a standard single-band hysteresis: once confirmed, the ratio is
reclassified normally, so a large enough single drop can skip directly
past an intermediate mode in one `report()` call.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

from acp.telemetry import Telemetry

ReceiverKey = tuple[str, str, Optional[str]]


class PressureMode(IntEnum):
    BELOW_SOFT = 0
    SOFT_TO_HARD = 1
    HARD_TO_EMERGENCY = 2
    ABOVE_EMERGENCY = 3


@dataclass(frozen=True)
class PressureWatermarks:
    soft: float = 0.60
    hard: float = 0.75
    emergency: float = 0.85
    hysteresis_margin: float = 0.05


DEFAULT_WATERMARKS = PressureWatermarks()


def _classify(ratio: float, watermarks: PressureWatermarks) -> PressureMode:
    if ratio >= watermarks.emergency:
        return PressureMode.ABOVE_EMERGENCY
    if ratio >= watermarks.hard:
        return PressureMode.HARD_TO_EMERGENCY
    if ratio >= watermarks.soft:
        return PressureMode.SOFT_TO_HARD
    return PressureMode.BELOW_SOFT


def _entry_watermark(mode: PressureMode, watermarks: PressureWatermarks) -> float | None:
    """The raw watermark that promotes a receiver *into* `mode`.

    `None` for `BELOW_SOFT`, which has no watermark of its own -- it is
    the floor, so it is never demoted out of.
    """
    if mode is PressureMode.SOFT_TO_HARD:
        return watermarks.soft
    if mode is PressureMode.HARD_TO_EMERGENCY:
        return watermarks.hard
    if mode is PressureMode.ABOVE_EMERGENCY:
        return watermarks.emergency
    return None


def _key_str(key: ReceiverKey) -> str:
    host, session_id, agent_id = key
    return f"{host}|{session_id}|{agent_id or ''}"


class PressureController:
    """Owns per-`ReceiverKey` pressure mode with hysteresis.

    A single instance is meant to be shared across all receivers known to
    one `Coordinator`; per-key isolation is enforced by keying every
    internal dict on `ReceiverKey`, never on a shared/global value.
    """

    def __init__(
        self,
        *,
        telemetry: Telemetry | None = None,
        watermarks: PressureWatermarks | None = None,
    ) -> None:
        self._telemetry = telemetry
        self._watermarks = watermarks or DEFAULT_WATERMARKS
        self._lock = threading.Lock()
        self._modes: dict[ReceiverKey, PressureMode] = {}
        self._last_ratio: dict[ReceiverKey, float] = {}

    def report(self, receiver_key: ReceiverKey, observed_ratio: float) -> PressureMode:
        if not 0.0 <= observed_ratio <= 1.0:
            raise ValueError(
                f"observed_ratio must be in [0.0, 1.0], got {observed_ratio!r}"
            )

        with self._lock:
            current = self._modes.get(receiver_key, PressureMode.BELOW_SOFT)
            previous_ratio = self._last_ratio.get(receiver_key, observed_ratio)
            raw = _classify(observed_ratio, self._watermarks)

            if raw >= current:
                # Promotion (or no change): always immediate, no hysteresis.
                new_mode = raw
            else:
                entry_watermark = _entry_watermark(current, self._watermarks)
                exit_threshold = (
                    entry_watermark - self._watermarks.hysteresis_margin
                    if entry_watermark is not None
                    else None
                )
                if exit_threshold is not None and observed_ratio < exit_threshold:
                    new_mode = raw
                else:
                    new_mode = current  # small fluctuation: stay put

            transitioned = new_mode != current
            if transitioned:
                self._modes[receiver_key] = new_mode
            self._last_ratio[receiver_key] = observed_ratio

        if transitioned and self._telemetry is not None:
            self._telemetry.increment("pressure_mode_exits")
            self._telemetry.increment("pressure_mode_entries")
            self._telemetry.increment("context_pressure_before", round(previous_ratio * 100))
            self._telemetry.increment("context_pressure_after", round(observed_ratio * 100))

        return new_mode

    def mode_of(self, receiver_key: ReceiverKey) -> PressureMode:
        with self._lock:
            return self._modes.get(receiver_key, PressureMode.BELOW_SOFT)

    def should_run_maintenance(self, receiver_key: ReceiverKey) -> bool:
        """True once a receiver is at `SOFT_TO_HARD` or above (§23)."""
        return self.mode_of(receiver_key) >= PressureMode.SOFT_TO_HARD

    def snapshot(self) -> dict[str, str]:
        """Bounded status view: `{receiver_key string: mode name}`."""
        with self._lock:
            return {_key_str(key): mode.name for key, mode in self._modes.items()}
