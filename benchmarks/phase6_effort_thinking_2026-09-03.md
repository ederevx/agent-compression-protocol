# Phase 6 benchmark: effort/thinking modes (2026-09-03)

Bounded live benchmark against the real `ci` provider (CheapestInference,
backing model `deepseek-v4-flash`), run through the actual AALP+ACP stack
(real Unix-socket ingress, real `Compressor`/`AalpClient`, no mocks), per
`agent_protocols_v1/STATUS.md` Phase 6: "benchmark supported effort/
thinking modes... select fastest configuration meeting the quality
floor." Run ahead of Phase 4 at explicit user direction.

## What was tested

Anthropic's `thinking` field (`{"type": "enabled", "budget_tokens": N}`),
newly wired through `acp/compressor.py`'s `_build_request_body` this same
session (`Compressor(thinking_budget_tokens=...)`, default `None` =
field omitted entirely, unchanged historical behavior). AALP's `ci`
provider forwards the request body opaquely (`providers/ci.json`'s
`request_shape.passthrough: true`; `aalp/forwarder.py` never parses the
body), so this field reaches CheapestInference's endpoint untouched.

`concurrency_limit: 1` for `ci` means every call below is fully serial;
total wall time for this benchmark was on the order of minutes.

## Corpus

Reused the `build #N: compiled ok, N warnings, N tests flaky` synthetic
pattern already used elsewhere in this session's live testing, varying
line count. **Correction made mid-run:** the first attempt used a
50-line and 300-line tier, both of which came in under ACP's `GENERAL`
traffic class `bypass_max` (8,000 estimated tokens) and were silently
gate-BYPASSed (0.00s, no AALP call at all, `mode: PASS` with the
original payload echoed back) -- not a benchmark result. Corrected to
two tiers that both clear the bypass threshold:

| corpus | lines | chars | ~estimated tokens |
| --- | --- | --- | --- |
| `near_threshold_380` | 380 | 35,101 | 8,775 |
| `large_700` | 700 | 64,756 | 16,189 |

## A real bug found and fixed by this benchmark

The first thinking-enabled call (`on_1024` / `large_700`, old parser)
came back `INVALID_RESPONSE` in 15.99s. A follow-up diagnostic call
(`AalpClient.forward()` directly, bypassing `Compressor`'s parser
entirely) proved the backend **does** honor `thinking` -- it returned a
real chain-of-thought block -- but the response `content` array shape
with thinking enabled is:

```json
"content": [
  {"type": "text", "text": ""},
  {"type": "thinking", "thinking": "...", "signature": ""},
  {"type": "text", "text": "<the actual answer>"}
]
```

`acp/compressor.py`'s `_parse_compressor_response` read only
`content[0]["text"]` -- the empty placeholder -- so every thinking-
enabled call was guaranteed to fail parsing, 100% of the time, not by
chance. Fixed in the same commit as this benchmark: the parser now scans
all content blocks and takes the **last** non-empty `type: "text"`
block, which is where the real answer lives with or without thinking.
Covered by a new regression test,
`test_thinking_response_skips_leading_empty_and_thinking_blocks`, using
this exact real-world shape. All results below for `on_1024` conditions
were captured (or, for `large_700`, re-captured) after this fix.

## Results

Bypassed rows (0.00s, gate short-circuit, no real call) omitted below --
see raw JSONL for the full record including those.

| condition | corpus | input chars | elapsed | outcome | mode | output chars |
| --- | --- | --- | --- | --- | --- | --- |
| thinking off | `large_700` | 64,756 | 3.48s | SUCCESS | COMPRESS | 142 |
| thinking off | `near_threshold_380` | 35,101 | 5.00s | SUCCESS | COMPRESS | 633 |
| thinking on (1024) | `near_threshold_380` | 35,101 | 40.18s | SUCCESS | COMPRESS | 577 |
| thinking on (1024) | `large_700` | 64,756 | 55.24s | SUCCESS | COMPRESS | 655 |
| thinking on (1024), *pre-fix* | `large_700` | 64,756 | 15.99s | INVALID_RESPONSE (parser bug, see above) | PASS (fallback) | 64,756 |

Latency stats over the 4 valid same-parser rows:

- thinking off (n=2): min 3.48s, max 5.00s, mean 4.24s
- thinking on (n=2): min 40.18s, max 55.24s, mean 47.71s
- **thinking on was ~8-16x slower than thinking off at both tested
  sizes**, and the direction was consistent (not just backend noise --
  in the same short run, off stayed fast both times and on stayed slow
  both times).

For context, AALP's own audit log across 8 unrelated real calls earlier
this session (all thinking-field-absent, i.e. equivalent to "off") shows
much wider variance under that condition alone: successes from 4.8s to
43.8s, plus two 60s timeouts and one 190s+ timeout. Backend latency is
genuinely noisy regardless of thinking; this benchmark's "off" sample
happened to land on the fast end of that range both times, so the
~8-16x on/off ratio above should be read as directional, not a precise
multiplier -- but the direction itself (on is much slower) held at both
tested sizes against a backend that is otherwise erratic in duration.

## Quality (qualitative, not rigorously scored)

Both conditions produced coherent, on-topic `COMPRESS`-mode evidence
capsules preserving the cyclic warning/branch/flaky-test/duration
patterns actually present in the synthetic corpus. Thinking's output
read as slightly more analytically explicit (e.g. calling out the
period-7/period-5 cycles by name), but was not obviously more correct,
and was not more compact -- at `large_700`, thinking-off actually
produced a *more* aggressive reduction (142 chars) than thinking-on (655
chars); at `near_threshold_380` the two were comparable (633 vs 577
chars). No hallucination-detection pipeline was run; this is a
single-pass qualitative read, not a scored benchmark, consistent with
this being a bounded pass rather than the full Phase 6 corpus-based
evidence-preservation benchmark.

## Decision

**`DEFAULT_THINKING_BUDGET_TOKENS` stays `None` (thinking off by
default).** The measured latency cost (8-16x) is large, consistent
across both tested sizes, and not offset by any observed compression-
ratio or quality improvement. The `thinking_budget_tokens` parameter
remains available on `Compressor` for a caller to opt into explicitly
(e.g. a future flow willing to trade latency for deeper reasoning on a
specific payload class), now that the parser bug that made it unusable
is fixed.

## What this bounded pass did NOT cover (left for a fuller Phase 6 pass)

- No systematic hallucination/evidence-loss detection.
- No compression-ratio measurement across a larger corpus.
- No sweep over `thinking_budget_tokens` values (only 1024 was tried).
- No sweep over `model` alternatives -- `DEFAULT_MODEL` is untouched.
- No conclusion about AALP/ACP's own timeout defaults -- those remain
  the existing Phase-6-placeholder values; this pass only benchmarked
  the effort/thinking knob and measured latency, per the user's explicit
  request to benchmark before continuing to the next phase.

## Raw data

Per-call JSONL (including the bypassed 0.00s rows and the pre-fix
INVALID_RESPONSE row) is reproduced below for completeness.

```json
{"label": "off", "thinking_budget_tokens": null, "corpus": "medium_300", "input_chars": 27689, "elapsed_seconds": 0.0, "outcome": "Outcome.SUCCESS", "mode": "PASS", "output_chars": 27689, "output_preview": "build #0: compiled ok, 0 warnings, 0 tests flaky, branch feature/phase6-0, duration 2m0s\nbuild #1: compiled ok, 1 warnings, 13 tests flaky, branch feature/phase6-1, duration 3m7s\nbuild #2: compiled ok, 2 warnings, 3 tests flaky, branch feature/phase6-2, duration 4m14s\nbuild #3: compiled ok, 3 warnings, 16 tests flaky, branch feature/phase6-3, duration 5m21s\nbuild #4: compiled ok, 4 warnings, 6 tes"}
{"label": "on_1024", "thinking_budget_tokens": 1024, "corpus": "medium_300", "input_chars": 27689, "elapsed_seconds": 0.0, "outcome": "Outcome.SUCCESS", "mode": "PASS", "output_chars": 27689, "output_preview": "build #0: compiled ok, 0 warnings, 0 tests flaky, branch feature/phase6-0, duration 2m0s\nbuild #1: compiled ok, 1 warnings, 13 tests flaky, branch feature/phase6-1, duration 3m7s\nbuild #2: compiled ok, 2 warnings, 3 tests flaky, branch feature/phase6-2, duration 4m14s\nbuild #3: compiled ok, 3 warnings, 16 tests flaky, branch feature/phase6-3, duration 5m21s\nbuild #4: compiled ok, 4 warnings, 6 tes"}
{"label": "off", "thinking_budget_tokens": null, "corpus": "small_50", "input_chars": 4570, "elapsed_seconds": 0.0, "outcome": "Outcome.SUCCESS", "mode": "PASS", "output_chars": 4570, "output_preview": "build #0: compiled ok, 0 warnings, 0 tests flaky, branch feature/phase6-0, duration 2m0s\nbuild #1: compiled ok, 1 warnings, 13 tests flaky, branch feature/phase6-1, duration 3m7s\nbuild #2: compiled ok, 2 warnings, 3 tests flaky, branch feature/phase6-2, duration 4m14s\nbuild #3: compiled ok, 3 warnings, 16 tests flaky, branch feature/phase6-3, duration 5m21s\nbuild #4: compiled ok, 4 warnings, 6 tes"}
{"label": "on_1024", "thinking_budget_tokens": 1024, "corpus": "small_50", "input_chars": 4570, "elapsed_seconds": 0.0, "outcome": "Outcome.SUCCESS", "mode": "PASS", "output_chars": 4570, "output_preview": "build #0: compiled ok, 0 warnings, 0 tests flaky, branch feature/phase6-0, duration 2m0s\nbuild #1: compiled ok, 1 warnings, 13 tests flaky, branch feature/phase6-1, duration 3m7s\nbuild #2: compiled ok, 2 warnings, 3 tests flaky, branch feature/phase6-2, duration 4m14s\nbuild #3: compiled ok, 3 warnings, 16 tests flaky, branch feature/phase6-3, duration 5m21s\nbuild #4: compiled ok, 4 warnings, 6 tes"}
{"label": "off", "thinking_budget_tokens": null, "corpus": "large_700", "input_chars": 64756, "elapsed_seconds": 3.482, "outcome": "Outcome.SUCCESS", "mode": "COMPRESS", "output_chars": 142, "output_preview": "build #0\u2013699 (feature/phase6 branches): all compiled ok; 0\u20136 warnings cycle per 7 builds; flaky test count 0\u201322 cycles; durations 2m0s\u201312m59s."}
{"label": "on_1024", "thinking_budget_tokens": 1024, "corpus": "large_700", "input_chars": 64756, "elapsed_seconds": 15.99, "outcome": "Outcome.INVALID_RESPONSE", "mode": "PASS", "output_chars": 64756, "output_preview": "build #0: compiled ok, 0 warnings, 0 tests flaky, branch feature/phase6-0, duration 2m0s\nbuild #1: compiled ok, 1 warnings, 13 tests flaky, branch feature/phase6-1, duration 3m7s\nbuild #2: compiled ok, 2 warnings, 3 tests flaky, branch feature/phase6-2, duration 4m14s\nbuild #3: compiled ok, 3 warnings, 16 tests flaky, branch feature/phase6-3, duration 5m21s\nbuild #4: compiled ok, 4 warnings, 6 tes"}
{"label": "off", "thinking_budget_tokens": null, "corpus": "near_threshold_380", "input_chars": 35101, "elapsed_seconds": 4.996, "outcome": "Outcome.SUCCESS", "mode": "COMPRESS", "output_chars": 633, "output_preview": "Build logs for `feature/phase6` (builds #0\u2013#379). All compiled OK.\n- **Branches**: `feature/phase6-0`, `feature/phase6-1`, `feature/phase6-2`, `feature/phase6-3`, `feature/phase6-4` (cyclic assignment).\n- **Warnings**: 0\u20136 per build (cyclic per build number mod 7).\n- **Flaky tests**: 0\u201322 per build (cyclic sequence: 0,13,3,16,6,19,9,22,12,2,15,5,18,8,21,11,1,14,4,17,7,20,10,0\u2026 repeating).\n- **Dura"}
{"label": "on_1024", "thinking_budget_tokens": 1024, "corpus": "near_threshold_380", "input_chars": 35101, "elapsed_seconds": 40.183, "outcome": "Outcome.SUCCESS", "mode": "COMPRESS", "output_chars": 577, "output_preview": "379 consecutive builds (0-378) all compiled successfully. Key patterns: \n- Warnings cycle 0-6 with each build (warnings = build mod 7).\n- Branches cycle feature/phase6-{0-4} per build mod 5.\n- Flaky tests vary from 0 to 22, correlated with warnings and branches; exact counts per build omitted due to redundancy.\n- Durations range from 2m0s to 12m59s, increasing with periodic resets approximately ev"}
{"label": "on_1024", "thinking_budget_tokens": 1024, "corpus": "large_700", "input_chars": 64756, "elapsed_seconds": 55.235, "outcome": "Outcome.SUCCESS", "mode": "COMPRESS", "output_chars": 655, "output_preview": "**Builds 0\u2013699:** All 700 builds compiled OK with 0 compilation failures.\n\n**Cyclical patterns:**\n- **Warnings:** 0\u21921\u21922\u21923\u21924\u21925\u21926\u21920 (period 7)\n- **Branches:** feature/phase6-{0,1,2,3,4} cycling (period 5)\n- **Duration:** increases ~1m07s/build, resets after ~12m50s\u2192~2m00s (period ~20 builds)\n- **Flaky tests:** range 0\u201322, deterministic pattern (increments of ~5 mod 20 with periodic -1 adjustment)\n\n*"}
```
