"""Outcome classification and traffic classes shared across ACP's pipeline.

A single closed set of outcomes (mirroring AALP's `aalp/errors.py` shape,
but this is ACP's own outcome set — ACP never imports AALP) lets every
stage of the compression pipeline report failure the same way.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(Enum):
    SUCCESS = "success"
    UNAVAILABLE = "unavailable"
    QUEUE_TIMEOUT = "queue_timeout"
    COMPRESSION_TIMEOUT = "compression_timeout"
    TOTAL_TIMEOUT = "total_timeout"
    INVALID_RESPONSE = "invalid_response"
    UPSTREAM_ERROR = "upstream_error"
    RATE_LIMITED = "rate_limited"
    MAINTENANCE = "maintenance"


class TrafficClass(Enum):
    """Which size/threshold policy (see acp/gate.py) applies to a payload."""

    GENERAL = "general"
    NATIVE_AGENT_REPORT = "native_agent_report"
    DOWNWARD_CONTEXT = "downward_context"


@dataclass
class AcpResult:
    """What a compression pipeline stage hands back to its caller.

    Kept minimal deliberately: later waves that build the actual
    compressor add richer fields (mode selection details, token
    accounting, provenance) on top of this.
    """

    outcome: Outcome
    mode: str | None = None
    output: str | bytes | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS
