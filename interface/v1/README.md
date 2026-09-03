# ACP interface v1

This directory is ACP's published, versioned, cross-protocol interface
description. It is the *only* surface a client — in practice a future
Phase-4 host adapter (Claude/Codex), or a local test harness — is entitled
to depend on when talking to ACP's coordinator. `contract.json` is the
machine-validatable schema; this file is the human-readable explanation of
the same contract.

## Why this exists

`acp.coordinator.Coordinator` is the single owner of ACP's
background/synchronous compression state (agent_protocols_v1_background_
compression_adjustment_metadata_v1.md §17): job dedup, the prepared-result
cache, synchronous/background lifecycle, AALP client calls, provenance,
and telemetry. A host
adapter that imported `acp.coordinator` directly, instantiated
`Coordinator` itself, or read `.acp/` private state on disk would be
coupled to implementation details that were never meant to be a contract
— and, per §17, would risk running its own compression operation outside
the coordinator's dedup/lifecycle ownership entirely. Interface v1 exists
so a host adapter (and this repository's own socket-level tests) can be
built and tested against *this document alone*.

## Bootstrap: discovering ACP and authenticating

Every operation below requires a live loopback Unix-domain-socket
connection to ACP and a bearer secret. A client discovers both the same
way AALP's own clients discover AALP (agent-api-lane-protocol/interface/v1/README.md's own
Bootstrap section) — this is the one deliberate, narrow exception to
"never read another protocol's — or ACP's own — private state outside its
published interface."

- **`<ACP root>/.acp/state/ingress.json`** — written atomically by ACP's
  ingress on startup, before it accepts any connection. Contains
  `{"socket_path": "<absolute path>", "secret_file": "<absolute path>"}`.
  `<ACP root>` resolves to the `ACP_HOME` environment variable if set,
  else the working directory ACP was started from
  (`acp.containment.resolve_root`); a client must be told this root
  out-of-band (shared deployment configuration) — interface v1 defines no
  operation for locating an unknown root.
- **`<secret_file>`** (default `<ACP root>/.acp/state/ingress.secret`) —
  an opaque bearer token, `0600`, owner-only, generated once. A client
  reads its raw contents and sends them back as the request envelope's
  `Authorization: Bearer <contents>` header field on every request below.

A client reads exactly these two paths and nothing else under `.acp/`.
Raw stored source, job/cache internals, and logs remain off-limits in
every version of this interface.

### Wire protocol

Interface v1 speaks the same minimal length-prefixed JSON protocol over
`AF_UNIX` as AALP's own interface v1 (see
agent-api-lane-protocol/interface/v1/README.md's Wire protocol section):
one request and one response per connection, each a 4-byte big-endian
length prefix followed by that many bytes of UTF-8 JSON. `method`/`path`
below describe each operation's binding exactly the way an HTTP verb and
path would — they are envelope fields now, not a literal HTTP request
line. ACP's ingress imposes no read/write deadline of its own; a caller
enforces its own budgets as one cumulative deadline across however many
individual socket reads/writes a call takes.

## The six operations

### `context.evaluate` — SYNCHRONOUS GATE

`POST /v1/context/evaluate` — evaluate/gate one payload synchronously,
coalescing with any in-flight job for the same content (§21), blocking
until a result is available or `synchronous_timeout` elapses.

Request body: `payload`, `traffic_class`
(`general`/`native_agent_report`/`downward_context`), `receiver`
(`{host, session_id, agent_id}`), and optionally `flow_id`,
`synchronous_timeout`.

Response: the same `compression_result_envelope` shape on every outcome —
`{"outcome", "mode", "output", "warnings", "message", "provenance"}` —
because `acp.compressor.CompressionResult` always populates all five
fields regardless of success/failure (a failed compression always
passes the original payload through unchanged).
Status code follows `outcomes.values.<outcome>.response_status_code` in
`contract.json`; every response also carries an `X-Acp-Outcome` header
naming the outcome explicitly, mirroring AALP's own `X-Aalp-Outcome`
requirement — see `x_acp_outcome_header` below for why.

### `context.prepare` — BACKGROUND PREFETCH

`POST /v1/context/prepare` — enqueue/deduplicate background preparation
without blocking the caller. Same payload/traffic_class/receiver/flow_id
fields as `context.evaluate`; no `synchronous_timeout` — that's
`context.evaluate`-only.
Response: `{"job_id": "..."}`, `202 Accepted`. `prepare()` never installs a
result anywhere by itself (§12) — a caller must explicitly `context.resolve`
at its own safe boundary.

### `context.resolve`

`GET /v1/context/resolve/{job_id}` — resolve a job previously created by
`context.prepare`. Still in flight (or an unknown `job_id`, e.g. already
reclaimed by the coordinator's stale-job sweep) returns
`{"status": "pending"}`, `200` — **this is not an error case**. A terminal
job returns the same `compression_result_envelope`/`X-Acp-Outcome` shape
`context.evaluate` uses, status-coded the same way.

### `source.store`

`POST /v1/source/store` — register raw authoritative content,
content-addressed by its SHA-256 hash, for provenance/audit purposes. This
is the one operation whose request body is the raw payload itself
(`Content-Type: application/octet-stream`), not a JSON envelope. Response:
`{"source_hash": "..."}`, `201 Created`. There is no operation in this
interface to read the stored bytes back out — `source.store` exists for
provenance/audit registration, not as a retrieval store.

### `service.capabilities`

`GET /v1/service/capabilities` → `{"service": "acp", "interface_version": 1, "capabilities": [...]}`

The discovery entry point. v1 declares exactly the six capability strings
above, one per operation (`context.evaluate`, `context.prepare`,
`context.resolve`, `source.store`, `service.status`,
`service.telemetry`).

### `service.status`

`GET /v1/service/status` → `acp.coordinator.Coordinator.status()`'s own
return value directly: `policy_version`, `jobs_by_state` (a per-`JobState`
count, never per-job detail), and `aalp_reachable`. Guaranteed, by
`Coordinator.status()`'s own docstring, to never carry a raw payload or
credential.

### `service.telemetry`

`GET /v1/service/telemetry` → `{"counters": {...}}`, every name in
`acp.telemetry.COUNTER_NAMES` mapped to its current integer value —
directly `acp.telemetry.Telemetry.snapshot()`'s own return value for the
live `Coordinator`. Added additively in interface v1 (agent_protocols_v1_
metadata_v1.md §21): before this operation existed, nothing outside the
running `acp.service` process could read telemetry at all, since
`service.status` deliberately excludes it. `service.telemetry` closes that
gap without changing `service.status`'s existing response schema — a
client that never adopts this capability keeps working exactly as before.
Aggregate integer counts only; never a raw payload, compressed output, or
per-job/per-cache-entry detail, so it does not fall under `coordinator.
internal_state`'s exclusion below.

## Outcomes

`context.evaluate` and `context.resolve` report exactly one of the seven
values already implemented in `acp/errors.py`'s `Outcome` enum — ACP's own
outcome set; ACP never imports AALP's `Outcome`. No client-facing code
should ever need to handle an eighth value for these two operations, and a
future v1.x addition may never repurpose one of these to mean something
new — that would require a new major interface version instead.

| Outcome | Meaning | Response status |
|---|---|---|
| `success` | Gate/compressor round-trip completed (including a gate BYPASS, reported as `success` with `mode: "PASS"`). | 200 |
| `unavailable` | AALP itself unreachable, or its target provider unavailable. | 503 |
| `queue_timeout` | AALP's own admission queue-timeout budget elapsed. | 504 |
| `compression_timeout` | A connection-level timeout occurred once an upstream attempt started. | 504 |
| `total_timeout` | The overall queue+upstream timeout budget elapsed. | 504 |
| `invalid_response` | AALP's response didn't parse as ACP's own compressor response protocol. | 502 |
| `upstream_error` | AALP reported an upstream transport-level failure. | 502 |

On every non-`success` outcome, ACP always passes the original payload
through unchanged (`mode: "PASS"`, `output` = the original payload),
still reported with the real, non-`success` `outcome` value and the
matching non-200 status code — a client must never infer success from
the presence of a usable `output` field alone.

`context.prepare`, `source.store`, `service.capabilities`, and
`service.status` are not modeled with this enum; they use ordinary
status-code semantics (see `contract.json` for each one's exact status
codes).

## `X-Acp-Outcome`

Required on every `context.evaluate` and `context.resolve` (terminal)
response, mirroring AALP's own `X-Aalp-Outcome` requirement exactly: two
different outcomes can share the same status code (504 covers three
distinct outcomes, 502 covers two), so status code alone cannot always
disambiguate which outcome actually happened. A client should treat this
header's absence as a contract violation rather than guessing from the
status code or the response body's own `outcome` field alone (though in
practice this header and the body's `outcome` field are always kept
identical).

## What this interface explicitly does not cover

Coordinator internals (job dict entries, cache keys, lock state) and raw
stored source content are never exposed in any form, in any version — see
`excluded_from_this_interface` in `contract.json`. A conforming client
only ever calls the six operations documented above and in
`contract.json`. It never imports an `acp.*` Python module, never
instantiates `Coordinator` directly, never calls a function not named on
this page, and never reads `.acp/` state from disk beyond the two
bootstrap files named above. If a future need can't be met through this
interface, the fix is to extend the interface (additively, if possible),
not to reach around it.

## Compatibility rules

Interface major versions are protocol-local to ACP — this repository's
`v1` need not track AALP's or ADP's own version numbers.

A change **stays within `interface_version: 1`** when all of the
following hold:

- existing valid requests remain valid;
- existing outcomes retain their documented meaning;
- existing clients keep working without adopting any new capability;
- new fields are optional and/or gated behind a new capability string;
- `service.capabilities` exposes the addition, so clients can detect it.

A change **requires a new major interface version** when any of the
following is true:

- an operation or field is removed or renamed;
- an existing operation's or outcome's semantics change incompatibly;
- a previously valid request becomes invalid;
- an outcome's meaning changes;
- authentication/bootstrap semantics change incompatibly.
