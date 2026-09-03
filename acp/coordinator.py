"""ACP's coordinator: the single owner of background/synchronous state.

Per agent_protocols_v1_background_compression_adjustment_metadata_v1.md
§17, no host adapter is permitted to run its own compression operation --
every SYNCHRONOUS GATE / BACKGROUND PREFETCH / PRESSURE MAINTENANCE call
(§11) goes through one `Coordinator` instance, which owns: job dedup, the
prepared-result cache, synchronous/background/maintenance lifecycle,
receiver-specific pressure state, AALP client calls (via `acp.compressor.
Compressor`, itself via `acp.aalp_client.AalpClient`), provenance, and
telemetry.

This is a pure Python API in this wave -- no HTTP yet (a later wave
publishes it over loopback HTTP as ACP interface v1: `context.evaluate` /
`context.prepare` / `context.resolve` / `context.pressure` / `source.
store` / `service.status` map directly onto `evaluate` / `prepare` /
`resolve` / `report_pressure` / `store_source` / `status` below).

All three execution modes ultimately call the exact same `Compressor.
compress()` -- the mode only controls *when* ACP submits and *whether the
caller waits*, never a different code path to AALP. Background work is
preparation only: `prepare()` never installs a result anywhere by itself;
a caller must explicitly `resolve()` a prepared result at its own safe
boundary (§12) -- this module has no opinion on what a safe boundary is.

Prepared-result cache key and coalescing (§21): a cache entry is keyed by
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
from acp import gate
from acp import provenance as provenance_mod
from acp.aalp_client import AalpClient
from acp.compressor import CompressionResult, Compressor
from acp.errors import Outcome, TrafficClass
from acp.jobs import Job, JobState, JobTransitionError, is_terminal, transition
from acp.pressure import PressureController, PressureMode, PressureWatermarks, ReceiverKey
from acp.telemetry import Telemetry

# ACP-side budget for how long a synchronous `evaluate()` caller will wait
# on a coalesced RUNNING/QUEUED job before falling through to its own
# gate/compress attempt. Mirrors `Compressor`'s own DEFAULT_TOTAL_TIMEOUT_
# SECONDS -- a caller waiting longer than a whole compression round-trip
# would take by itself gains nothing by continuing to wait.
DEFAULT_SYNCHRONOUS_TIMEOUT_SECONDS = 60.0

# A job's `urgency_class` for a job created purely to perform (or
# coalesce) a SYNCHRONOUS GATE, as distinct from "prefetch"/"maintenance"
# background jobs. Background-only telemetry counters
# (background_jobs_enqueued/started/ready/failed) are never incremented
# for a job carrying this urgency_class.
_SYNCHRONOUS_URGENCY = "synchronous"

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


class Coordinator:
    """Single owner of ACP's background/synchronous compression state."""

    def __init__(
        self,
        aalp_root: str | Path,
        root: str | Path | None = None,
        *,
        telemetry: Telemetry | None = None,
        compressor_kwargs: dict | None = None,
        policy_version: str = "v1",
        pressure_watermarks: PressureWatermarks | None = None,
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

        self._pressure = PressureController(
            telemetry=self._telemetry, watermarks=pressure_watermarks
        )

        # Single coarse lock guarding every dict below. Workload for this
        # wave (in-memory job store, no persistence) does not warrant
        # finer-grained per-key locking; correctness of the coalescing
        # property (see module docstring) depends on cache-slot claiming
        # and job registration happening atomically under one lock.
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._job_events: dict[str, threading.Event] = {}
        self._results: dict[str, CompressionResult] = {}
        self._cache: dict[tuple, str] = {}
        self._job_cache_keys: dict[str, tuple] = {}
        self._pending: dict[str, tuple] = {}

    @property
    def telemetry(self) -> Telemetry:
        return self._telemetry

    @property
    def pressure(self) -> PressureController:
        return self._pressure

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
        receiver_key: ReceiverKey,
        flow_id: str | None,
        payload: str,
        *,
        urgency_class: str,
        state: JobState,
    ) -> tuple[str, Job]:
        """Create, store, and (optionally) start a new `Job`. Caller holds `self._lock`."""
        host, session_id, agent_id = receiver_key
        job_id = uuid.uuid4().hex
        now = time.time()
        job = Job(
            job_id=job_id,
            source_ref=source_hash,
            source_hash=source_hash,
            receiver_host=host,
            receiver_session_id=session_id,
            receiver_agent_id=agent_id,
            flow_id=flow_id,
            turn_id=None,
            traffic_class=traffic_class,
            urgency_class=urgency_class,
            estimated_input_tokens=gate.estimate_tokens(payload),
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
        self._jobs[job_id] = job
        self._job_events[job_id] = threading.Event()
        return job_id, job

    def _finalize_job(self, job_id: str, result: CompressionResult) -> None:
        new_state = _JOB_STATE_BY_OUTCOME.get(result.outcome, _DEFAULT_FAILED_STATE)
        with self._lock:
            job = self._jobs[job_id]
            transition(job, new_state)
            job.completed_at = time.time()
            if result.output is not None:
                job.result_hash = provenance_mod.compute_hash(
                    result.output if isinstance(result.output, bytes) else str(result.output)
                )
                job.result_ref = job.result_hash
            self._results[job_id] = result
            event = self._job_events[job_id]
            is_background = job.urgency_class != _SYNCHRONOUS_URGENCY
        event.set()
        if is_background:
            if new_state is JobState.READY:
                self._telemetry.increment("background_jobs_ready")
            else:
                self._telemetry.increment("background_jobs_failed")

    def _credit_reuse(self, job: Job, *, synchronous: bool) -> None:
        """Telemetry for consuming an existing job's result instead of compressing.

        `background_jobs_reused` fires only when the reused job is itself
        a background (prefetch/maintenance) job -- not when two
        synchronous `evaluate()` callers coalesce onto each other's ad hoc
        job, since that is synchronous coalescing, not reuse of prior
        background *preparation*. `synchronous_gate_cache_hits` fires
        whenever a synchronous caller avoids running its own `compress()`
        by consuming someone else's result, regardless of that job's
        origin.
        """
        if job.urgency_class != _SYNCHRONOUS_URGENCY:
            self._telemetry.increment("background_jobs_reused")
        if synchronous:
            self._telemetry.increment("synchronous_gate_cache_hits")

    # -- SYNCHRONOUS GATE -------------------------------------------------

    def evaluate(
        self,
        payload: str,
        traffic_class: TrafficClass,
        receiver_key: ReceiverKey,
        *,
        flow_id: str | None = None,
        synchronous_timeout: float | None = None,
        prior_provenance=None,
        force_policy=None,
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
            job_id = self._cache.get(key)
            job = self._jobs.get(job_id) if job_id is not None else None

            if job is not None and job.state is JobState.READY:
                result = self._results[job_id]
                self._credit_reuse(job, synchronous=True)
                return result

            if job is not None and job.state in (JobState.QUEUED, JobState.RUNNING):
                wait_job_id = job_id
                wait_event = self._job_events[job_id]
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
                    self._cache.pop(key, None)
                own_job_id, _job = self._register_job(
                    source_hash, traffic_class, receiver_key, flow_id, payload,
                    urgency_class=_SYNCHRONOUS_URGENCY, state=JobState.RUNNING,
                )
                self._cache[key] = own_job_id
                self._job_cache_keys[own_job_id] = key

        if wait_event is not None:
            started_wait = time.monotonic()
            finished = wait_event.wait(timeout=timeout)
            elapsed_ms = int((time.monotonic() - started_wait) * 1000)
            self._telemetry.increment("synchronous_gate_wait_ms", elapsed_ms)

            if finished:
                with self._lock:
                    waited_job = self._jobs.get(wait_job_id)
                    result = self._results.get(wait_job_id)
                if result is not None and waited_job is not None:
                    self._credit_reuse(waited_job, synchronous=True)
                    return result

            # Timed out (or the coalesced job resolved with no usable
            # result, e.g. it was marked STALE): fall through to our own
            # synchronous attempt. Do NOT cancel or otherwise interfere
            # with the still-running original job -- it keeps running and
            # its eventual result still populates the cache for the next
            # caller.
            self._telemetry.increment("synchronous_gate_cache_misses")
            with self._lock:
                own_job_id, _job = self._register_job(
                    source_hash, traffic_class, receiver_key, flow_id, payload,
                    urgency_class=_SYNCHRONOUS_URGENCY, state=JobState.RUNNING,
                )
                # Only claim the cache slot if nobody currently owns it
                # (e.g. the original job was marked STALE and removed
                # itself from the cache). If the original job is still
                # actively running, leave its cache ownership alone.
                if self._cache.get(key) is None:
                    self._cache[key] = own_job_id
                    self._job_cache_keys[own_job_id] = key
        else:
            self._telemetry.increment("synchronous_gate_cache_misses")

        result = self._compressor.compress(
            payload, traffic_class, prior_provenance=prior_provenance,
            flow_id=flow_id, force_policy=force_policy,
        )
        self._finalize_job(own_job_id, result)
        return result

    # -- BACKGROUND PREFETCH / PRESSURE MAINTENANCE ------------------------

    def prepare(
        self,
        payload: str,
        traffic_class: TrafficClass,
        receiver_key: ReceiverKey,
        *,
        urgency: str = "prefetch",
        flow_id: str | None = None,
        prior_provenance=None,
    ) -> str:
        source_hash = provenance_mod.compute_hash(payload)
        key = self._cache_key(source_hash, traffic_class)

        with self._lock:
            job_id = self._cache.get(key)
            job = self._jobs.get(job_id) if job_id is not None else None

            if job is not None and job.state in (
                JobState.QUEUED, JobState.RUNNING, JobState.READY
            ):
                self._telemetry.increment("background_jobs_reused")
                return job_id

            if job is not None:
                self._cache.pop(key, None)

            job_id, _job = self._register_job(
                source_hash, traffic_class, receiver_key, flow_id, payload,
                urgency_class=urgency, state=JobState.QUEUED,
            )
            self._cache[key] = job_id
            self._job_cache_keys[job_id] = key
            self._pending[job_id] = (payload, prior_provenance, flow_id)
            self._telemetry.increment("background_jobs_enqueued")

        thread = threading.Thread(target=self._run_background_job, args=(job_id,), daemon=True)
        thread.start()
        return job_id

    def _run_background_job(self, job_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(job_id, None)
            job = self._jobs.get(job_id)
            if pending is None or job is None or job.state is not JobState.QUEUED:
                # Marked STALE (or otherwise removed) before this thread
                # got to run -- nothing to do, and per §31 a stale job
                # that nothing depended on must not emit a warning, so we
                # simply never call the compressor for it.
                return
            payload, prior_provenance, flow_id = pending
            transition(job, JobState.RUNNING)
            job.started_at = time.time()
            traffic_class = job.traffic_class

        self._telemetry.increment("background_jobs_started")
        result = self._compressor.compress(
            payload, traffic_class, prior_provenance=prior_provenance, flow_id=flow_id,
        )
        self._finalize_job(job_id, result)

    def resolve(self, job_id: str) -> CompressionResult | None:
        """READY/terminal-with-a-result -> the result; still in flight -> None.

        A STALE job never has a stored result (this coordinator's
        `mark_stale` only accepts QUEUED jobs -- see below -- so a STALE
        job never reached `_finalize_job`), so it naturally resolves to
        `None` here rather than needing a special case.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or not is_terminal(job.state):
                return None
            return self._results.get(job_id)

    def mark_stale(self, job_id: str) -> Job:
        """Mark a not-yet-submitted background job STALE (§31).

        Restricted to `QUEUED` jobs even though `acp.jobs.VALID_
        TRANSITIONS` (unchanged from Wave A) also technically allows
        READY -> STALE: §31 frames staleness as "before AALP submission",
        and allowing it on a READY job here would silently orphan an
        already-stored result out from under `resolve()`. A READY job
        that is no longer wanted is instead reclaimed by `sweep_stale_
        jobs`'s ordinary TTL expiry.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(f"unknown job_id {job_id!r}")
            if job.state is not JobState.QUEUED:
                raise JobTransitionError(
                    f"mark_stale only applies to a QUEUED (pre-submission) job; "
                    f"job {job_id!r} is {job.state.value}"
                )
            transition(job, JobState.STALE)
            cache_key = self._job_cache_keys.get(job_id)
            if cache_key is not None and self._cache.get(cache_key) == job_id:
                del self._cache[cache_key]
            event = self._job_events.get(job_id)
        if event is not None:
            # Wake any synchronous waiter coalesced onto this job so it
            # can fall through to its own attempt immediately rather than
            # blocking until its wait timeout.
            event.set()
        self._telemetry.increment("background_jobs_stale")
        return job

    # -- pressure ---------------------------------------------------------

    def report_pressure(self, receiver_key: ReceiverKey, observed_ratio: float) -> PressureMode:
        return self._pressure.report(receiver_key, observed_ratio)

    def maybe_trigger_maintenance(
        self,
        payload: str,
        traffic_class: TrafficClass,
        receiver_key: ReceiverKey,
        *,
        flow_id: str | None = None,
        prior_provenance=None,
    ) -> str | None:
        """Demonstration hook: `prepare()` under pressure, if warranted.

        A full proactive scanner that discovers deferred/raw material to
        prepare is out of scope for this wave (§23 only asks for the
        threshold machinery and a hook point); this method is that hook,
        callable once a caller already has a specific payload in mind.
        """
        if not self._pressure.should_run_maintenance(receiver_key):
            return None
        job_id = self.prepare(
            payload, traffic_class, receiver_key, urgency="maintenance",
            flow_id=flow_id, prior_provenance=prior_provenance,
        )
        self._telemetry.increment("pressure_maintenance_jobs")
        return job_id

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
            for job_id, job in list(self._jobs.items()):
                if not is_terminal(job.state):
                    continue
                reference_time = job.completed_at if job.completed_at is not None else job.created_at
                if now - reference_time <= max_age_seconds:
                    continue
                removed.append(job_id)
                del self._jobs[job_id]
                self._job_events.pop(job_id, None)
                self._results.pop(job_id, None)
                self._pending.pop(job_id, None)
                cache_key = self._job_cache_keys.pop(job_id, None)
                if cache_key is not None and self._cache.get(cache_key) == job_id:
                    del self._cache[cache_key]
        return removed

    # -- status -----------------------------------------------------------

    def status(self) -> dict:
        """Bounded health/status. Never includes a raw payload or credential."""
        with self._lock:
            jobs_by_state: dict[str, int] = {}
            for job in self._jobs.values():
                jobs_by_state[job.state.value] = jobs_by_state.get(job.state.value, 0) + 1
            pressure_snapshot = self._pressure.snapshot()

        try:
            self._aalp_client.capabilities()
            aalp_reachable = True
        except Exception:
            aalp_reachable = False

        return {
            "policy_version": self._policy_version,
            "jobs_by_state": jobs_by_state,
            "aalp_reachable": aalp_reachable,
            "pressure": pressure_snapshot,
        }
