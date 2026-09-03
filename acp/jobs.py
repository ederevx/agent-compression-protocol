"""Background compression job data model.

Data model only: `JobState`, `Job`, and state-transition validation.
No queueing, scheduling, or execution logic lives here — that belongs
to the coordinator built in a later wave.

HARD INVARIANT: `Job` never carries a credential, token, or other
secret field. A job references its source and result by hash/ref only;
anything requiring a credential is looked up through AALP at the point
of use, never stored on the job.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from acp.errors import TrafficClass


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
    STALE = "stale"


# "Terminal" here means the compressor's own work on the job is done
# (nothing left for the coordinator's synchronous/background pipeline to
# do). READY is included even though VALID_TRANSITIONS still allows
# READY -> STALE: that edge is expiry/garbage-collection housekeeping on
# an already-finished job, not further compression work.
_TERMINAL_STATES = frozenset({
    JobState.READY,
    JobState.FAILED,
    JobState.QUEUE_TIMEOUT,
    JobState.COMPRESSION_TIMEOUT,
    JobState.TOTAL_TIMEOUT,
    JobState.BYPASSED,
    JobState.BLOCKED,
    JobState.STALE,
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
    JobState.READY: frozenset({JobState.STALE}),
    JobState.FAILED: frozenset(),
    JobState.QUEUE_TIMEOUT: frozenset(),
    JobState.COMPRESSION_TIMEOUT: frozenset(),
    JobState.TOTAL_TIMEOUT: frozenset(),
    JobState.BYPASSED: frozenset(),
    JobState.BLOCKED: frozenset(),
    JobState.STALE: frozenset(),
}


class JobTransitionError(ValueError):
    """Raised for an invalid job state transition."""


@dataclass
class Job:
    job_id: str
    source_ref: str
    source_hash: str
    receiver_host: str
    receiver_session_id: str
    receiver_agent_id: str | None
    flow_id: str | None
    turn_id: str | None
    traffic_class: TrafficClass
    urgency_class: str
    estimated_input_tokens: int
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
