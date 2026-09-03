"""ACP's coordinator: the single owner of synchronous compression state.

Per agent_protocols_v1_background_compression_adjustment_metadata_v1.md
§17, no host adapter is permitted to run its own compression operation --
every SYNCHRONOUS GATE call (§11) goes through one `Coordinator` instance,
which owns: job dedup, the result cache, AALP client calls (via
`acp.compressor.Compressor`, itself via `acp.aalp_client.AalpClient`),
provenance, and telemetry.

This module previously also owned a BACKGROUND PREFETCH mode
(`prepare()`/`resolve()`, a `context.prepare`/`context.resolve` interface
v1 pair): a caller could enqueue non-blocking preparation of a payload,
polled for later via a job id. That was removed -- it existed only to
proactively cache-warm a receiver's context ahead of a later synchronous
call, but nothing in either host can install a prepared result back into
a receiver's live context short of a proxy intercepting outbound API
traffic, which was evaluated and explicitly declined (see STATUS.md,
"bounded-context reclamation," declined 2026-09-03: it would disable
Claude Code's Remote Control and downgrade MCP tool search/streaming).
Every real token-reduction win ACP delivers comes from the synchronous
ingress path below, which this removal leaves untouched.

Exposed over loopback HTTP as ACP interface v1: `context.evaluate` /
`source.store` / `service.status` map directly onto `evaluate` /
`store_source` / `status` below.

Coalescing (§21): a cache entry is keyed by
`(source_hash, policy_version, traffic_class, provider_id, model)`. The
gate's `reduction_hint` is deliberately *not* part of the key: given a
fixed `thresholds` policy (owned by the `Compressor` instance this
coordinator constructs), `reduction_hint` is a pure function of
`(source_hash, traffic_class)` already covered by the key, so adding it
would be redundant rather than a materially different transformation
input.

Coalescing correctness (the trickiest property here): a synchronous
`evaluate()` call that finds no matching cache entry registers its *own*
new `Job` in `RUNNING` state and claims the cache slot for its key while
still holding `self._lock` -- so a second concurrent `evaluate()` call for
the same key is guaranteed to observe that job (not create a second one)
the next time it acquires the lock, and instead waits on the first job's
completion `threading.Event`. This is what keeps two near-simultaneous
callers for the same payload down to exactly one `Compressor.compress()`
call.
"""
from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from acp import compressor as compressor_mod
from acp import containment
from acp import provenance as provenance_mod
from acp.aalp_client import AalpClient
from acp.compressor import CompressionResult, Compressor
from acp.errors import Outcome, TrafficClass
from acp.jobs import Job, JobState, ReceiverKey, is_terminal, transition
from acp.telemetry import Telemetry

# ACP-side budget for how long a synchronous `evaluate()` caller will wait
# on a coalesced RUNNING/QUEUED job before falling through to its own
# gate/compress attempt. Mirrors `Compressor`'s own DEFAULT_TOTAL_TIMEOUT_
# SECONDS -- a caller waiting longer than a whole compression round-trip
# would take by itself gains nothing by continuing to wait.
DEFAULT_SYNCHRONOUS_TIMEOUT_SECONDS = 60.0

# Outcome -> JobState mapping for a job that finished RUNNING.
# `acp.jobs.VALID_TRANSITIONS[RUNNING]` only permits {READY, FAILED,
# COMPRESSION_TIMEOUT, TOTAL_TIMEOUT} -- there is no RUNNING -> QUEUE_
# TIMEOUT edge, because JobState.QUEUE_TIMEOUT is reachable only from
# QUEUED (it models *this coordinator's own* pre-submission admission
# queue timing out, which cannot happen in this wave's one-thread-per-job
# model: a job is transitioned to RUNNING synchronously, in the same call
# that is about to invoke `Compressor.compress()`, so there is no
# internal queue to wait in). An AALP-reported `Outcome.QUEUE_TIMEOUT`
# (AALP's ingress connect budget) therefore arrives after our Job is
# already RUNNING, and -- like UNAVAILABLE/INVALID_RESPONSE/UPSTREAM_
# ERROR -- is bucketed into JobState.FAILED, mirroring compressor.py's
# own `_UNCOUNTERED_FAILURE_BUCKET` grouping of those same non-timeout
# outcomes.
_JOB_STATE_BY_OUTCOME = {
    Outcome.SUCCESS: JobState.READY,
    Outcome.COMPRESSION_TIMEOUT: JobState.COMPRESSION_TIMEOUT,
    Outcome.TOTAL_TIMEOUT: JobState.TOTAL_TIMEOUT,
}
_DEFAULT_FAILED_STATE = JobState.FAILED

# JobState.BYPASSED / JobState.BLOCKED are intentionally never produced by
# this coordinator: both are reachable only from QUEUED (per acp/jobs.py),
# modeling a coordinator-level pre-submission short-circuit decided
# *before* a job is ever run. This wave always delegates gate-bypass-
# eligible payloads through `Compressor.compress()` like everything else
# (there is exactly one channel to AALP -- see acp/compressor.py's own
# docstring), which already resolves BYPASS internally as `Outcome.
# SUCCESS` (mode="PASS", message="gate bypass") before returning; that
# surfaces here as an ordinary JobState.READY. A future wave that adds an
# early coordinator-side size check ahead of job creation could produce
# BYPASSED/BLOCKED for real.


class _JobStore:
    """Groups the coordinator's job-tracking dicts as one private unit.

    Purely a namespace for what were five loose `Coordinator` attributes
    (`_jobs`, `_job_events`, `_results`, `_cache`, `_job_cache_keys`); it
    owns no locking or invariants of its own; every compound read/modify
    sequence across these dicts still happens under `Coordinator._lock`,
    exactly as before. Nothing outside this module constructs or touches
    a `_JobStore` -- ACP interface v1 (`context.evaluate` / `source.
    store` / `service.status`, per this module's docstring) and every
    Claude/Codex host adapter only ever call `Coordinator`'s public
    methods, so this is an internal-only regrouping with no client-visible
    effect (§39 of agent_protocols_v1_background_compression_adjustment_
    metadata_v1.md).
    """

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.events: dict[str, threading.Event] = {}
        self.results: dict[str, CompressionResult] = {}
        self.cache: dict[tuple, str] = {}
        self.cache_keys: dict[str, tuple] = {}


class Coordinator:
    """Single owner of ACP's synchronous compression state."""

    def __init__(
        self,
        aalp_root: str | Path,
        root: str | Path | None = None,
        *,
        telemetry: Telemetry | None = None,
        compressor_kwargs: dict | None = None,
        policy_version: str = "v1",
        synchronous_timeout: float = DEFAULT_SYNCHRONOUS_TIMEOUT_SECONDS,
    ) -> None:
        self._root = root
        self._policy_version = policy_version
        self._default_synchronous_timeout = synchronous_timeout

        containment.ensure_dirs(self._root)

        self._telemetry = telemetry if telemetry is not None else Telemetry()
        self._aalp_client = AalpClient(aalp_root)

        compressor_kwargs = dict(compressor_kwargs or {})
        self._provider_id = compressor_kwargs.get(
            "provider_id", compressor_mod.DEFAULT_PROVIDER_ID
        )
        self._model = compressor_kwargs.get("model", compressor_mod.DEFAULT_MODEL)
        self._compressor = Compressor(self._aalp_client, self._telemetry, **compressor_kwargs)

        # Single coarse lock guarding every dict below. Workload for this
        # wave (in-memory job store, no persistence) does not warrant
        # finer-grained per-key locking; correctness of the coalescing
        # property (see module docstring) depends on cache-slot claiming
        # and job registration happening atomically under one lock.
        self._lock = threading.Lock()
        self._store = _JobStore()

    @property
    def telemetry(self) -> Telemetry:
        return self._telemetry

    # -- cache key -----------------------------------------------------

    def _cache_key(self, source_hash: str, traffic_class: TrafficClass) -> tuple:
        return (
            source_hash,
            self._policy_version,
            traffic_class.value,
            self._provider_id,
            self._model,
        )

    # -- job registration / finalization --------------------------------

    def _register_job(
        self,
        source_hash: str,
        traffic_class: TrafficClass,
        flow_id: str | None,
        *,
        state: JobState,
    ) -> tuple[str, Job]:
        """Create, store, and (optionally) start a new `Job`. Caller holds `self._lock`."""
        job_id = uuid.uuid4().hex
        now = time.time()
        job = Job(
            job_id=job_id,
            source_hash=source_hash,
            flow_id=flow_id,
            traffic_class=traffic_class,
            created_at=now,
            started_at=now if state is JobState.RUNNING else None,
            completed_at=None,
            result_ref=None,
            result_hash=None,
            state=JobState.QUEUED,
            policy_version=self._policy_version,
        )
        if state is JobState.RUNNING:
            transition(job, JobState.RUNNING)
        self._store.jobs[job_id] = job
        self._store.events[job_id] = threading.Event()
        return job_id, job

    def _finalize_job(self, job_id: str, result: CompressionResult) -> None:
        new_state = _JOB_STATE_BY_OUTCOME.get(result.outcome, _DEFAULT_FAILED_STATE)
        with self._lock:
            job = self._store.jobs[job_id]
            transition(job, new_state)
            job.completed_at = time.time()
            if result.output is not None:
                job.result_hash = provenance_mod.compute_hash(
                    result.output if isinstance(result.output, bytes) else str(result.output)
                )
                job.result_ref = job.result_hash
            self._store.results[job_id] = result
            event = self._store.events[job_id]
        event.set()

    def _credit_reuse(self, job: Job) -> None:
        """Telemetry for a synchronous caller consuming an existing job's
        result instead of running its own `compress()`."""
        self._telemetry.increment("synchronous_gate_cache_hits")

    # -- SYNCHRONOUS GATE -------------------------------------------------

    def evaluate(
        self,
        payload: str,
        traffic_class: TrafficClass,
        # Required by interface v1's wire contract; validated but not
        # otherwise consumed since the pressure-tracking subsystem this
        # once fed was removed. Left in the public signature rather than
        # dropped in this pass -- unlike `urgency`, every production
        # caller already supplies a real one, so dropping it is a
        # separate, larger-blast-radius decision than this pass's scope.
        receiver_key: ReceiverKey,
        *,
        flow_id: str | None = None,
        synchronous_timeout: float | None = None,
    ) -> CompressionResult:
        timeout = (
            self._default_synchronous_timeout
            if synchronous_timeout is None
            else synchronous_timeout
        )
        source_hash = provenance_mod.compute_hash(payload)
        key = self._cache_key(source_hash, traffic_class)

        own_job_id: str | None = None
        wait_job_id: str | None = None
        wait_event: threading.Event | None = None

        with self._lock:
            job_id = self._store.cache.get(key)
            job = self._store.jobs.get(job_id) if job_id is not None else None

            if job is not None and job.state is JobState.READY:
                result = self._store.results[job_id]
                self._credit_reuse(job)
                return result

            if job is not None and job.state in (JobState.QUEUED, JobState.RUNNING):
                wait_job_id = job_id
                wait_event = self._store.events[job_id]
            else:
                # No matching job, or a stale/terminal-non-ready one --
                # per §21 that is never substituted. Register our own job
                # (RUNNING, since we are about to compress synchronously
                # in this thread) and claim the cache slot for this key
                # under the same lock acquisition that discovered it was
                # free -- this is what forces a second concurrent caller
                # to observe and coalesce onto *this* job instead of
                # racing to create its own.
                if job is not None:
                    self._store.cache.pop(key, None)
                own_job_id, _job = self._register_job(
                    source_hash, traffic_class, flow_id, state=JobState.RUNNING,
                )
                self._store.cache[key] = own_job_id
                self._store.cache_keys[own_job_id] = key

        if wait_event is not None:
            started_wait = time.monotonic()
            finished = wait_event.wait(timeout=timeout)
            elapsed_ms = int((time.monotonic() - started_wait) * 1000)
            self._telemetry.increment("synchronous_gate_wait_ms", elapsed_ms)

            if finished:
                with self._lock:
                    waited_job = self._store.jobs.get(wait_job_id)
                    result = self._store.results.get(wait_job_id)
                if result is not None and waited_job is not None:
                    self._credit_reuse(waited_job)
                    return result

            # Timed out: fall through to our own synchronous attempt. Do
            # NOT cancel or otherwise interfere with the still-running
            # original job -- it keeps running and its eventual result
            # still populates the cache for the next caller.
            self._telemetry.increment("synchronous_gate_cache_misses")
            with self._lock:
                own_job_id, _job = self._register_job(
                    source_hash, traffic_class, flow_id, state=JobState.RUNNING,
                )
                # Only claim the cache slot if nobody currently owns it.
                # If the original job is still actively running, leave
                # its cache ownership alone.
                if self._store.cache.get(key) is None:
                    self._store.cache[key] = own_job_id
                    self._store.cache_keys[own_job_id] = key
        else:
            self._telemetry.increment("synchronous_gate_cache_misses")

        result = self._compressor.compress(
            payload, traffic_class, flow_id=flow_id,
        )
        self._finalize_job(own_job_id, result)
        return result

    # -- source store -------------------------------------------------------

    def store_source(self, content: bytes) -> str:
        source_hash = provenance_mod.compute_hash(content)
        containment.store_raw(self._root, content, source_hash)
        return source_hash

    # -- cleanup --------------------------------------------------------

    def sweep_stale_jobs(self, max_age_seconds: float) -> list[str]:
        now = time.time()
        removed: list[str] = []
        with self._lock:
            for job_id, job in list(self._store.jobs.items()):
                if not is_terminal(job.state):
                    continue
                reference_time = job.completed_at if job.completed_at is not None else job.created_at
                if now - reference_time <= max_age_seconds:
                    continue
                removed.append(job_id)
                del self._store.jobs[job_id]
                self._store.events.pop(job_id, None)
                self._store.results.pop(job_id, None)
                cache_key = self._store.cache_keys.pop(job_id, None)
                if cache_key is not None and self._store.cache.get(cache_key) == job_id:
                    del self._store.cache[cache_key]
        return removed

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        """Bounded health/status. Never includes a raw payload or credential."""
        with self._lock:
            jobs_by_state: dict[str, int] = {}
            for job in self._store.jobs.values():
                jobs_by_state[job.state.value] = jobs_by_state.get(job.state.value, 0) + 1

        try:
            self._aalp_client.capabilities()
            aalp_reachable = True
        except Exception:
            aalp_reachable = False

        return {
            "policy_version": self._policy_version,
            "jobs_by_state": jobs_by_state,
            "aalp_reachable": aalp_reachable,
        }
