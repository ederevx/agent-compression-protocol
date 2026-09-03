"""ACP's synchronous compression job data model.

Data model only: `JobState`, `Job`, and state-transition validation.
No queueing, scheduling, or execution logic lives here — that belongs
to the coordinator.

Every job here is created by `Coordinator.evaluate()` (the SYNCHRONOUS
GATE ingress-compression path); the background-prefetch job type this
model once also carried (`prepare()`/`resolve()`) was removed as a
dead end -- proactive/prewarm compression of a subagent's transcript
tail was never installable back into a receiver's context short of a
proxy intercepting outbound API traffic, which was evaluated and
declined (agent_protocols_v1_metadata_v1.md, "bounded-context
reclamation," declined 2026-09-03; see STATUS.md). ACP's actual token
reduction happens at ingress, synchronously, via `evaluate()`.

HARD INVARIANT: `Job` never carries a credential, token, or other
secret field. A job references its source and result by hash/ref only;
anything requiring a credential is looked up through AALP at the point
of use, never stored on the job.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from acp.errors import TrafficClass

# (host, session_id, agent_id-or-None) identifying who requested a job.
# Accepted by Coordinator.evaluate() as part of interface v1's required
# `receiver` field, but not currently stored on `Job` or read back by
# any coordinator logic.
ReceiverKey = tuple[str, str, Optional[str]]


class JobState(Enum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    QUEUE_TIMEOUT = "queue_timeout"
    COMPRESSION_TIMEOUT = "compression_timeout"
    TOTAL_TIMEOUT = "total_timeout"
    BYPASSED = "bypassed"
    BLOCKED = "blocked"


# "Terminal" here means the compressor's own work on the job is done
# (nothing left for the coordinator to do).
_TERMINAL_STATES = frozenset({
    JobState.READY,
    JobState.FAILED,
    JobState.QUEUE_TIMEOUT,
    JobState.COMPRESSION_TIMEOUT,
    JobState.TOTAL_TIMEOUT,
    JobState.BYPASSED,
    JobState.BLOCKED,
})

VALID_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({
        JobState.RUNNING,
        JobState.QUEUE_TIMEOUT,
        JobState.BYPASSED,
        JobState.BLOCKED,
    }),
    JobState.RUNNING: frozenset({
        JobState.READY,
        JobState.FAILED,
        JobState.COMPRESSION_TIMEOUT,
        JobState.TOTAL_TIMEOUT,
    }),
    JobState.READY: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.QUEUE_TIMEOUT: frozenset(),
    JobState.COMPRESSION_TIMEOUT: frozenset(),
    JobState.TOTAL_TIMEOUT: frozenset(),
    JobState.BYPASSED: frozenset(),
    JobState.BLOCKED: frozenset(),
}


class JobTransitionError(ValueError):
    """Raised for an invalid job state transition."""


@dataclass
class Job:
    job_id: str
    source_hash: str
    flow_id: str | None
    traffic_class: TrafficClass
    created_at: float
    started_at: float | None
    completed_at: float | None
    result_ref: str | None
    result_hash: str | None
    state: JobState
    policy_version: str


def transition(job: Job, new_state: JobState) -> Job:
    """Move `job` to `new_state` in place, raising on an invalid edge."""
    allowed = VALID_TRANSITIONS.get(job.state, frozenset())
    if new_state not in allowed:
        raise JobTransitionError(
            f"invalid transition for job {job.job_id!r}: "
            f"{job.state.value} -> {new_state.value}")
    job.state = new_state
    return job


def is_terminal(state: JobState) -> bool:
    return state in _TERMINAL_STATES
