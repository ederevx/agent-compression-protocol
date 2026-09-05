# Queue coalescing live comparison: textual vs. JSON-array response codec (2026-09-04)

Live-activation pass for the `agent_protocols_v1` queue-coalescing adjustment
(`project_md/agent_protocols_v1/agent_protocols_v1_queue_coalescing_adjustment_metadata_v1.md`).
Compares the shipped `ACP-QUEUE/1` textual response grammar
(`acp/queue_codec.py`) against the JSON-array design recommended by the
`experimental/model-side-output-split` branch (`acp/queue_codec_json.py`:
array wire shape, hybrid skeleton-anchor prompt addendum, JSON Schema
`response_format` enforcement) — both run for real, through the real
`ci` (CheapestInference, `deepseek-v4-flash`) backend, through the real
AALP/ACP ingress stack.

Mirrors the Phase 6 precedent
(`benchmarks/phase6_effort_thinking_2026-09-03.md`): an ad hoc, throwaway
driver script, not committed anywhere; this report is the only persisted
artifact.

## What was tested

Both AALP and ACP were run from a merged `integrate/queue-coalescing`
branch (queue coalescing Stages 0-5 plus, for ACP, the new
`Compressor(response_codec=...)` selector added specifically to make this
comparison possible — see `acp/compressor.py`). The real `aalp.service`/
`acp.service` systemd units were restarted onto these branches for the
duration of the test, with AALP maintenance mode and ACP bypass mode kept
active for every step *except* the sweep itself and two short follow-up
diagnostic calls — both flags were re-enabled immediately afterward.

Swept dimensions:

- **Codec:** `textual` (shipped) vs. `json_array` (recommended experimental).
- **Corpus size:** small (~9k tokens), medium (~20k tokens), large (~35k
  tokens per logical member) — all comfortably above `GENERAL`'s bypass
  threshold, so every call reaches the real compressor.
- **Queue width:** 1, 2, 4 concurrent logical members sharing one physical
  call (matching the widths Stage 5's fixture benchmark tested).
- **Repeats:** 3 per (codec, size, width) cell.

54 trials, 63 real logical-member calls per codec (some 2- and 4-wide
trials additionally needed one small "occupier" call to force coalescing
by holding the `ci` provider's Lane open — mirrors
`tests/test_queue_benchmark.py`'s own occupier-thread technique, just
against the real backend instead of a fake connection).

## Results

### Reliability (member calls reaching `Outcome.SUCCESS`)

| codec        | success | fail | rate |
|--------------|---------|------|------|
| `textual`    | 58/63   | 5    | 92%  |
| `json_array` | 43/63   | 20   | 68%  |

**Follow-up investigation (post-report):** `textual`'s 5 failures were
initially written up as generic scattered flakiness. Re-examined against
the raw per-trial data
(`/tmp/queue_coalescing_live_sweep_results.json`), they split into two
distinct, identifiable root causes, not one uniform cause:

- **Model protocol non-adherence (2 incidents, 3 of 5 failed members):**
  `small/width=1/repeat=0` (2.10s, solo member) and
  `large/width=2/repeat=2` (7.81s, both coalesced members) — both took
  normal real-completion time (matching the 2-24s range every genuine
  success in this sweep falls in), but the model's actual text response
  never contained an `ACP-QUEUE-ITEM:` block at all
  (`"no ACP-QUEUE-ITEM blocks found in response"`). `queue_codec.py`'s own
  addendum is explicit that its framing applies *"even when only one ITEM
  is present"*, so this is a real instruction-following miss, not an
  ambiguity in the prompt. Plausibly aggravated by this sweep's own
  synthetic corpus (`make_payload()`'s degenerate, thousands-of-times-
  repeated `"log line filler content..."` filler is not representative of
  real payload text) — flagged as a caveat on the failure rate, not a
  confirmed cause. Independent of that, this is a live confirmation of
  §19's whole-generation-failure design: at `width=2` one bad completion
  took out *both* legitimate coalesced members, not just one — the
  blast radius of a non-adherent completion scales with width.
- **Upstream error body passed through as `Outcome.SUCCESS` (1 incident,
  2 of 5 failed members):** `medium/width=2/repeat=1` — both members
  failed in 0.58s, roughly an order of magnitude faster than every real
  completion elsewhere in the sweep. `aalp/forwarder.py` documents its own
  design outright: *"4xx/5xx from the upstream API is still
  `Outcome.SUCCESS`... the gateway — not this module — interprets the
  passed-through status/body."* AALP's success determination is
  transport-level only; it does not validate that the body is actually
  Messages-shaped. The timing here is consistent with the `ci` proxy
  returning a fast error/overload response (under concurrent load from
  the occupier plus 2 coalesced members hitting a `concurrency_limit=1`
  lane at once) that AALP relayed as "success" and only ACP's own
  body-shape check caught downstream. No raw body was captured for this
  call, so this is the best-supported reading of the timing/error-shape
  evidence, not a confirmed root cause.

This second failure mode is the same `CompressorProtocolViolation:
...not a parseable Messages-shaped body: 'content'` signature that
accounts for **all 20** of `json_array`'s failures below — see the
revised reading of that hypothesis after the reliability table.

`json_array`'s 20 failures were **not** scattered: all 20 occurred at the
**large** corpus size, and nowhere else — 20 of 21 large-size member
calls failed. Broken down by width: `large`+width=1 had 1 success and 2
failures (2 of 3 repeats); `large`+width=2 and `large`+width=4 failed
every single repeat (0/6 and 0/12). Small and medium sizes were 100%
reliable for `json_array` (21/21 and 21/21).

Failure shape in every case: `CompressorProtocolViolation: compressor
response is not a parseable Messages-shaped body: 'content'` — AALP
reports `Outcome.SUCCESS` (HTTP 200, transport succeeded) but the
response body itself isn't Messages-shaped, i.e. the real backend
returned something other than a normal completion.

**This is not a pure request-size/context-length ceiling** — `textual`
succeeded 12/12 at `large`+width=4 (an estimated ~140k combined input
tokens: four ~35k-token members merged into one physical request per the
size definitions above), the single largest physical request size this
sweep produced, while `json_array` failed 2 of 3 repeats at `large`+width=1
(a single ~35k-token member, no coalescing at all, roughly a quarter the
combined size of the `textual` case that succeeded cleanly). The one
variable that actually distinguishes the two codecs at request-build time
is `json_array`'s added `response_format` field (JSON-schema-constrained
decoding).

Two follow-up diagnostic calls (outside the sweep proper, with the
maintenance/bypass flags briefly disabled again for each) both happened
to **succeed** at `large`+`json_array` — one solo member, and one
concurrent 4-thread submission that only actually coalesced 3 of the 4
members into a shared generation (the 4th ran alone; the occupier-priming
technique the sweep script uses to reliably force full-width coalescing
wasn't used in this ad hoc follow-up). Neither diagnostic call reproduced
a failure, so the real upstream error body was never actually captured —
only the parse-level message the sweep itself recorded
(`compressor response is not a parseable Messages-shaped body: 'content'`)
is available.

**Revised reading (post-report investigation):** this exact failure
signature — `'content'` missing from the response body entirely — is
also what `textual`'s own `medium/width=2/repeat=1` incident produced
(above), where the anomalously fast 0.58s round-trip pointed to an
upstream error body relayed through as `Outcome.SUCCESS` by AALP's
transport-only success check (`aalp/forwarder.py`), not a `response_format`
problem. Since `json_array`'s 20 failures share that identical signature,
at least some — plausibly most or all — could be the same shared
AALP/backend-boundary gap (fast overload/error responses from `ci` under
load, indistinguishable at the ACP layer from a `response_format`-caused
malformed body) rather than something specific to JSON-schema-constrained
decoding. Large-size requests are also the most concurrency/load-stressing
requests in this sweep (biggest combined payload, most provider-side
work), which is an equally plausible reason for upstream overload errors
to concentrate there, independent of which response codec was in use.
The `response_format`-at-scale explanation is therefore weaker than
originally written: it remains *plausible* (JSON-array failures were
still far more concentrated at `large` than `textual`'s were, and
`response_format` is a genuine per-codec difference), but it is no
longer the best-supported explanation on its own — an upstream-overload
explanation shared with `textual`'s root cause B fits the same evidence
at least as well, and this pass cannot distinguish between them without
a captured raw failing body.

This sweep did not isolate `response_format` from the rest of
`json_array`'s prompt/parsing design as the sole cause, nor did it
distinguish a `response_format`-specific failure from a shared
upstream-overload failure carrying the identical error signature (both
would need a fourth configuration — JSON-array wire shape *without*
`response_format` — and capturing a raw failing response body rather
than only the parse failure, including its HTTP status code) — flagged
as follow-up work, not done here.

### Latency (successful calls only)

Wall-clock time per trial (one physical call shared by `width` members),
counting only trials where every member succeeded:

| codec        | width | n | mean wall (s) | median (s) | min-max (s) |
|--------------|-------|---|---------------|------------|-------------|
| `textual`    | 1     | 8 | 2.81          | 2.80       | 2.09-3.73   |
| `textual`    | 2     | 7 | 6.24          | 6.32       | 3.89-8.42   |
| `textual`    | 4     | 9 | 11.03         | 9.24       | 6.07-23.96  |
| `json_array` | 1     | 7 | 6.01          | 3.20       | 2.48-23.72  |
| `json_array` | 2     | 6 | 6.08          | 6.24       | 5.27-6.97   |
| `json_array` | 4     | 6 | 10.08         | 8.64       | 6.77-18.85  |

`json_array width=1`'s mean is pulled up by one slow 23.7s `large` call
(its median of 3.2s is more representative of typical single-member
latency); n is smaller than `textual`'s at every width because several
`json_array` trials were entirely excluded here for having at least one
failed member (see reliability table above).

Coalescing continues to behave as Stage 5 already established: combining
2-4 members into one physical call costs meaningfully less than that many
sequential calls would, though not for free (bigger merged prompts take
longer per call). Nothing in this pass changes Stage 5's `max_queue_members
= 4` conclusion; this sweep wasn't designed to re-derive it, only to
compare codecs at each width Stage 5 already validated.

## Decision

**Keep `RESPONSE_CODEC_TEXTUAL` (the shipped `ACP-QUEUE/1` grammar) as
`Compressor`'s default.** `json_array`'s ~25-point-lower success rate,
concentrated entirely at larger payload sizes, is disqualifying for
production use as currently built (with `response_format` enabled)
against this real backend. The `response_codec` selector added to
`Compressor` (`acp/compressor.py`) stays in the codebase — it's what made
this comparison possible and costs nothing when left at its default — but
`json_array` should not be adopted without a follow-up pass that isolates
whether dropping `response_format` (while keeping the array wire shape and
hybrid-anchor prompt) restores reliability at large sizes.

## What this pass did NOT cover

- Isolating `response_format` as the specific cause of `json_array`'s
  large-size failures (vs. the addendum prompt itself, or the array shape).
- Sizes larger than ~35k tokens per member, or widths above 4.
- Output quality/compression-ratio comparison between codecs (out of
  scope for this pass; Phase 6's own quality caveats apply equally here).
- Any change to `max_queue_members` or other Stage 5 parameters.

## Raw data

Full per-trial results (54 trials, per-member outcome/latency/output
length) were written to `/tmp/queue_coalescing_live_sweep_results.json`
during the sweep; that file and the throwaway driver script
(`/tmp/queue_coalescing_live_sweep.py`) are local scratch artifacts, not
committed. This report is the durable record.

Assisted-by: Claude-Code:claude-sonnet-5
Signed-off-by: Edrick Sinsuan <evcsinsuan@gmail.com>
