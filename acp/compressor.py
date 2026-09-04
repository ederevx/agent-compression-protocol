"""ACP's compressor orchestration: gate -> provenance -> AALP -> policy -> telemetry.

This module defines and implements ACP's compression request/response
protocol end-to-end (it did not exist anywhere before this wave). The
external compressor is called through `acp.aalp_client.AalpClient`
only -- this module never talks to a provider directly and never falls
back to native inference on failure; there is no such code path here.

The wire shape is the Anthropic Messages API (`POST /v1/messages` on
whichever AALP provider id is configured, default `"ci"`), submitted via
AALP's `request.queue` (`acp.aalp_client.submit_queue_member`) rather
than `request.forward`: every call submits exactly one logical member of
a possibly-shared physical generation (see `acp.queue_codec`'s
`ACP-QUEUE/1` grammar) -- AALP may genuinely coalesce it with other
concurrent calls sharing the same `queue_key` (§6-§13). Each call sends
a self-describing envelope (`acp.queue_codec.build_queue_envelope`)
rather than a fully-built physical body; AALP mechanically assembles the
final Messages-shaped request from however many members land in the
same generation:

    {
      "model": "<configurable>",
      "max_tokens": <computed>,
      "system": "<COMPRESSOR_SYSTEM_PROMPT + queue_codec's isolation addendum>",
      "messages": [{"role": "user", "content": "<one or more ACP-QUEUE-ITEM: <id>\\n... blocks, joined by AALP, plus a trailing ACP-QUEUE-MEMBER-COUNT: n>"}]
    }

The compressor's response may therefore contain other, unknown-to-this-
call members' blocks alongside this call's own. ACP extracts only its
own member id's block (`acp.queue_codec.parse_queue_member_result`),
whose first line must be exactly one of `ACP-MODE: PASS`,
`ACP-MODE: COMPACT`, `ACP-MODE: COMPRESS`. For PASS, ACP substitutes the
original payload verbatim itself -- it never trusts whatever (if
anything) the model echoes back after that line. For COMPACT/COMPRESS,
everything after the mode line and its one blank line is the transformed
output ACP returns.
"""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acp import bypass
from acp import gate
from acp import provenance as provenance_mod
from acp import queue_codec
from acp.aalp_client import AalpClient
from acp.errors import AcpResult, Outcome, TrafficClass
from acp.gate import GateAction, TrafficClassThresholds
from acp.provenance import Provenance
from acp.queue_codec import QueueMemberRequest, QueueProtocolViolation
from acp.telemetry import Telemetry
from acp.warnings import CompressionWarningTracker

# The `ci` provider's real, currently-only-available model as confirmed
# by a live agent_protocols_v1 Phase 3 activation test against the real
# CheapestInference-backed endpoint (a `claude-*` model id 404s there --
# see cheapestinference_claude_agent_metadata_v2.md §4: this endpoint
# proxies Claude-shaped requests to DeepSeek, not a real Claude model).
# Still just a default: which model is best for compression quality is a
# Phase 6 benchmarking decision, and any caller can override it per-
# instance.
DEFAULT_MODEL = "deepseek-v4-flash"

# No absolute ceiling on the compressed output's `max_tokens` -- purely a
# fraction of the payload's own estimated input tokens (half, plus a
# small tolerance; see `_MAX_TOKENS_TOLERANCE_FRACTION`), floored so tiny
# payloads still get a workable budget. An earlier version of this
# module clamped the fraction against a fixed absolute ceiling (whether
# a hardcoded 4096 or one derived from `gate.py`'s thresholds); that was
# removed at explicit user direction, because clamping large payloads
# down to a small absolute number forces disproportionately more lossy
# compression exactly where the input is largest -- the opposite of what
# the 50% ratio guarantee is supposed to mean. The ratio is now
# unconditional: a bigger payload gets a proportionally bigger output
# budget, all the way up. See `_compute_max_tokens` and
# `_compute_target_tokens`.
_MIN_MAX_TOKENS = 256

# `_compute_max_tokens`'s hard, API-enforced cap extends
# `_compute_target_tokens`'s strict 50% figure by this fraction of the
# payload's estimated input tokens. `gate.py`'s `token_estimator` is a
# coarse char/4 approximation of the real tokenizer, so a genuinely
# well-compressed response can occasionally land a little over the
# *estimated* 50% mark while still being under 50% of the *real* input
# token count -- this tolerance exists purely to keep that estimation
# slack from spuriously truncating good output via `stop_reason:
# max_tokens`. It is deliberately not surfaced to the model as something
# to use: the model is told to target the strict, untolerated figure
# from `_compute_target_tokens` and to compress as much as it reasonably
# can, never to fill whatever budget it is given.
_MAX_TOKENS_TOLERANCE_FRACTION = 0.05

# `None` means "no `thinking` field at all" -- the historical, unchanged
# default. AALP's `ci` provider forwards the request body opaquely
# (`providers/ci.json`'s `request_shape.passthrough: true`; see
# `aalp/forwarder.py`), so an Anthropic-shaped `thinking` field reaches
# CheapestInference's endpoint untouched if a caller opts in. Whether the
# `deepseek-v4-flash` backend behind that endpoint actually honors it was
# an open question until the Phase 6 benchmark in
# `benchmarks/phase6_effort_thinking_2026-09-03.md`; see that file for
# the measured result this default reflects.
DEFAULT_THINKING_BUDGET_TOKENS: int | None = None

# Anthropic's Messages API requires max_tokens > thinking.budget_tokens.
# This is the minimum headroom reserved for actual output above the
# thinking budget when thinking is enabled.
_MIN_OUTPUT_HEADROOM_ABOVE_THINKING = 256

# Mirrors `AalpClient.forward`'s own defaults; these are ACP-side
# round-trip budgets layered on top of AALP's internal ones (see
# `acp/aalp_client.py`'s `forward()` docstring).
DEFAULT_QUEUE_TIMEOUT_SECONDS = 5.0
DEFAULT_COMPRESSION_TIMEOUT_SECONDS = 30.0
DEFAULT_TOTAL_TIMEOUT_SECONDS = 60.0

DEFAULT_PROVIDER_ID = "ci"
_FORWARD_PATH = "/v1/messages"

# Where the assembled member train belongs inside the `shared` envelope
# (agent_protocols_v1_queue_coalescing_adjustment_metadata_v1.md §11):
# AALP deep-sets the joined train here once admission happens, replacing
# `_CONTENT_SENTINEL` below. Generic path traversal on AALP's side --
# this module is the only place that needs to know it's `messages[0]
# .content` in Anthropic's Messages API shape.
_CONTENT_PATH = ["messages", 0, "content"]
_CONTENT_SENTINEL = "__ACP_QUEUE_PENDING_MEMBER_TRAIN__"

COMPRESSOR_SYSTEM_PROMPT = """You are ACP's compression stage: a loss-minimizing context processor, not a task-solving agent.

Given a payload, choose exactly one mode:
- PASS: content is already dense, exact preservation is required, or reduction would be unsafe.
- COMPACT: preferred reduction. Remove redundancy/noise while preserving substantive evidence (collapse repeated warnings, deduplicate repeated success/log lines, group identical failures with occurrence counts, retain exact representative evidence).
- COMPRESS: structured semantic reduction for material that cannot remain mostly verbatim at a reasonable size. Produce a stable evidence capsule, not loose prose.

When relevant, preserve exactly or losslessly represent: latest user instructions; requirements and prohibitions; file paths; code identifiers and symbols; API signatures; commands; compiler/test/runtime errors; numeric values and thresholds; versions and hashes; decisions already made and their status; validation results; unresolved ambiguity/conflict; pending work and known failure state.

You must NOT: redesign the system; solve architecture or debugging work that belongs downstream; silently resolve ambiguity; change task intent; invent missing facts; pick a side between conflicting evidence without preserving the conflict; declare unfinished work complete.

For COMPACT/COMPRESS, your goal is the smallest output that safely preserves the required substance -- not approaching, filling, or matching any budget or ceiling you are given. The message below states a reduction ceiling (the maximum ever permitted, 50% of the payload) and, separately, a hard output budget (a safety margin, never something to spend). Neither number is a goal: compress as far below the ceiling as the required-substance rules allow, and treat "as small as possible while staying correct" as the only real objective. Never pad, restate, or elaborate to use up more of either figure, and never stop compressing early just because you are already under one.

Respond with exactly one line as your first line: "ACP-MODE: PASS", "ACP-MODE: COMPACT", or "ACP-MODE: COMPRESS".
If PASS, output nothing else after that line.
If COMPACT or COMPRESS, follow the mode line with exactly one blank line, then output STRICTLY AND ONLY the transformed content being compressed -- nothing else. Do not include commentary, a preamble, meta-discussion of your own process, an explanation or summary of what you changed, or any restatement of these instructions or the traffic-class/budget header above the payload. Every token you spend must be part of the compressed content itself; your entire output budget is sized for that content alone, so anything else you add directly displaces content and risks truncation.
"""

# UNAVAILABLE / INVALID_RESPONSE / UPSTREAM_ERROR have no dedicated
# per-outcome counter in `acp.telemetry.COUNTER_NAMES` (only the three
# timeout outcomes do). They are bucketed under the pre-registered
# `compression_availability` counter as "AALP-side failure, not a
# timeout" -- see the wave report for the reasoning.
_TIMEOUT_COUNTER_BY_OUTCOME = {
    Outcome.QUEUE_TIMEOUT: "compression_queue_timeouts",
    Outcome.COMPRESSION_TIMEOUT: "compression_execution_timeouts",
    Outcome.TOTAL_TIMEOUT: "compression_total_timeouts",
}
_UNCOUNTERED_FAILURE_BUCKET = "compression_availability"


class CompressorProtocolViolation(ValueError):
    """The compressor's response did not follow ACP's response protocol."""


@dataclass
class CompressionResult(AcpResult):
    """`AcpResult` plus the `Provenance` a caller must carry forward.

    Reuses `AcpResult`'s `outcome`/`mode`/`output`/`warnings`/`message`
    fields rather than inventing a parallel shape; `provenance` is the
    only field this wave adds.
    """

    provenance: Provenance | None = None


def _compute_target_tokens(estimated_input_tokens: int) -> int:
    """The strict, untolerated 50%-of-input reduction CEILING communicated
    to the compressor model -- half of `estimated_input_tokens`, no
    tolerance and no `max_tokens` ceiling applied.

    Despite the name (kept for continuity with `_compute_max_tokens`'s
    internal `target` variable), this is presented to the model as a
    maximum never to be reached, not a value to aim for -- the model is
    separately instructed (`COMPRESSOR_SYSTEM_PROMPT`, `_build_user_
    message`) that its real goal is the smallest output that safely
    preserves required substance, as far below this figure as the
    content allows. Telling the model "compress to N tokens" invites it
    to fill N; telling it "N is the most you may ever use" does not.

    `estimated_input_tokens` must be the effective input -- the payload
    being compressed alone, i.e. `decision.estimated_tokens` from
    `gate.evaluate(payload, ...)` -- never a count that includes
    `COMPRESSOR_SYSTEM_PROMPT` or `_build_user_message`'s traffic-class/
    budget wrapper text. Folding the compressor's own instructions into
    the ratio would inflate the effective ceiling for every call by a
    fixed amount unrelated to what's actually being compressed.

    `_compute_max_tokens` computes the separate, tolerance-extended
    figure actually enforced as the hard `max_tokens` cutoff sent to the
    API -- the two are intentionally different numbers.
    """
    return estimated_input_tokens // 2


def _compute_max_tokens(estimated_input_tokens: int) -> int:
    """Cap the compressor's requested `max_tokens` so any actual
    compression (the INSPECT path -- BYPASS never reaches this) cannot
    ask for much more than half of its own estimated input, with no
    absolute upper bound: the ratio holds no matter how large the
    payload is, so compression never becomes more lossy just because
    the input is bigger.

    The hard cutoff extends `_compute_target_tokens`'s strict 50% figure
    by `_MAX_TOKENS_TOLERANCE_FRACTION` (5%) of the estimated input. This
    absorbs `gate.py`'s `token_estimator` char/4 approximation's own
    slack against the real tokenizer, so a response that is genuinely
    within the true 50% guarantee doesn't get spuriously truncated over
    an imprecise estimate. It is not a relaxation of the guarantee the
    model is asked to meet: the model is told the strict, untolerated
    target from `_compute_target_tokens`, never this extended figure,
    and is instructed to compress as much as it reasonably can rather
    than use whatever budget it is given (see `COMPRESSOR_SYSTEM_PROMPT`
    and `_build_user_message`). The structural floor this function
    enforces moves from "never more than 50% of estimated input" to
    "never more than 55%" as a deliberate, documented trade -- not a
    silent or hidden change, and the model's own instructions and stated
    target are unaffected by it.

    `estimated_input_tokens // 2` (plus tolerance) alone would undershoot
    the floor only for a payload smaller than roughly `2 * _MIN_MAX_TOKENS`
    estimated tokens; every built-in traffic class's `bypass_max`
    (gate.py) is well above that, so `_MIN_MAX_TOKENS` never actually
    binds for a default-threshold payload. A caller-supplied custom
    threshold letting a much smaller payload reach INSPECT is the one
    case where the floor could push `max_tokens` back above the intended
    ratio -- an accepted trade favoring a workable output budget over
    the ratio for payloads that small.
    """
    target = _compute_target_tokens(estimated_input_tokens)
    tolerance = round(estimated_input_tokens * _MAX_TOKENS_TOLERANCE_FRACTION)
    return max(_MIN_MAX_TOKENS, target + tolerance)


def _build_user_message(
    payload: str,
    traffic_class: TrafficClass,
    reduction_hint: str | None,
    target_tokens: int,
    max_tokens: int,
) -> str:
    hint = reduction_hint or "none"
    return (
        f"Traffic class: {traffic_class.value}. Size-band hint: {hint}. "
        f"Reduction ceiling: {target_tokens} tokens -- the maximum ever "
        f"permitted (50% of the payload's own size), NOT a target to "
        f"reach. Your real goal is the smallest output that safely "
        f"preserves the required substance, as far below this ceiling as "
        f"the content allows -- do not pad, restate, or stop compressing "
        f"early just because you are already under it. Hard output "
        f"budget: {max_tokens} tokens -- sized from the payload below "
        f"only (everything above this line, including this budget note "
        f"itself, is excluded from that count); this is a small safety "
        f"margin for estimation error, not something to use on purpose "
        f"-- your response (including the ACP-MODE line) is cut off if "
        f"it exceeds this. Output nothing but the compressed content "
        f"itself -- no commentary, no restatement of these instructions. "
        f"PASS is exempt, it never needs either figure.\n\n"
        f"---\n\n{payload}"
    )


def _build_request_body(
    payload: str,
    traffic_class: TrafficClass,
    reduction_hint: str | None,
    model: str,
    target_tokens: int,
    max_tokens: int,
    member_id: str,
    thinking_budget_tokens: int | None = None,
) -> bytes:
    """Build this call's self-describing queue envelope (§10-§12) -- not
    a fully-built physical request body. `shared` is the physical
    request skeleton with `_CONTENT_SENTINEL` standing in for wherever
    AALP will later deep-set the assembled member train (possibly
    including other members this call has no visibility into, §9)."""
    if thinking_budget_tokens is not None:
        max_tokens = max(
            max_tokens, thinking_budget_tokens + _MIN_OUTPUT_HEADROOM_ABOVE_THINKING
        )
    user_message = _build_user_message(
        payload, traffic_class, reduction_hint, target_tokens, max_tokens
    )
    shared: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": queue_codec.build_system_prompt(COMPRESSOR_SYSTEM_PROMPT),
        "messages": [{"role": "user", "content": _CONTENT_SENTINEL}],
    }
    if thinking_budget_tokens is not None:
        shared["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget_tokens,
        }
    member = QueueMemberRequest(member_id=member_id, content=user_message)
    return queue_codec.build_queue_envelope(shared, _CONTENT_PATH, member)


def _parse_compressor_response(
    body: bytes, member_id: str
) -> tuple[str, str, dict[str, Any] | None]:
    """Parse one Anthropic-Messages-shaped compressor response and
    extract just `member_id`'s own result, ignoring any other members
    that may have been coalesced into the same physical response (§9).

    Returns `(mode, transformed_text, usage)`. `transformed_text` is
    only meaningful for COMPACT/COMPRESS (the caller must ignore it and
    substitute the original payload for PASS). Raises
    `CompressorProtocolViolation` for anything that doesn't parse.
    """
    try:
        response_json = json.loads(body)
        content_blocks = response_json["content"]
        # With `thinking` enabled, a real Anthropic-shaped response's
        # `content` array holds one or more non-text blocks (a
        # `thinking` block, and observed in practice also a leading
        # empty `text` placeholder block) around the actual answer --
        # confirmed live against the `ci` backend during the Phase 6
        # benchmark (see benchmarks/phase6_effort_thinking_2026-09-03.md).
        # `content[0]` is not reliably the answer even when it happens
        # to have `type == "text"`, so pick the LAST non-empty text
        # block instead: that is where the model's actual final answer
        # lives, both with and without thinking enabled.
        text = None
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                text = block["text"]
        if text is None:
            raise KeyError("no non-empty text content block")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise CompressorProtocolViolation(
            f"compressor response is not a parseable Messages-shaped body: {exc}"
        ) from exc

    try:
        result = queue_codec.parse_queue_member_result(text, member_id)
    except QueueProtocolViolation as violation:
        raise CompressorProtocolViolation(str(violation)) from violation

    mode = result.mode
    remainder = result.output

    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        usage = None

    # The tightened `max_tokens` cap in `_compute_max_tokens` (~50%
    # reduction guarantee, plus a small tolerance for estimation slack --
    # see `_MAX_TOKENS_TOLERANCE_FRACTION`) makes truncation a real,
    # newly-introduced risk for COMPACT/COMPRESS: a real Anthropic-shaped
    # response cut off mid-output reports `stop_reason: "max_tokens"`.
    # PASS is exempt
    # -- its trailing text is always discarded and the original payload
    # substituted verbatim, so a truncated PASS response cannot corrupt
    # anything downstream. Treating a truncated COMPACT/COMPRESS body as
    # a protocol violation (rather than returning the partial text)
    # keeps compressor failure visible and reason-specific instead of
    # silently handing back incoherent, cut-off output.
    if mode != "PASS" and response_json.get("stop_reason") == "max_tokens":
        raise CompressorProtocolViolation(
            f"compressor response truncated (stop_reason=max_tokens) before "
            f"completing its {mode} output"
        )

    return mode, remainder, usage


class Compressor:
    """Ties gate -> provenance -> AALP -> failure policy -> telemetry together.

    One instance owns one `CompressionWarningTracker` (per §20's
    per-instance scoping requirement) plus the AALP provider id, model,
    and timeout/policy defaults for its flow. A later coordinator wave
    decides how many instances exist (e.g. per-provider, per-receiver).
    """

    def __init__(
        self,
        aalp_client: AalpClient,
        telemetry: Telemetry,
        *,
        provider_id: str = DEFAULT_PROVIDER_ID,
        model: str = DEFAULT_MODEL,
        thinking_budget_tokens: int | None = DEFAULT_THINKING_BUDGET_TOKENS,
        queue_timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        compression_timeout: float = DEFAULT_COMPRESSION_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        thresholds: dict[TrafficClass, TrafficClassThresholds] | None = None,
        root: str | Path | None = None,
    ) -> None:
        self._aalp_client = aalp_client
        self._telemetry = telemetry
        self._provider_id = provider_id
        self._model = model
        self._thinking_budget_tokens = thinking_budget_tokens
        self._queue_timeout = queue_timeout
        self._compression_timeout = compression_timeout
        self._total_timeout = total_timeout
        self._thresholds = thresholds
        self._root = root
        self._warnings = CompressionWarningTracker()

    @property
    def warning_tracker(self) -> CompressionWarningTracker:
        return self._warnings

    def compress(
        self,
        payload: str,
        traffic_class: TrafficClass,
        *,
        flow_id: str | None = None,
    ) -> CompressionResult:
        source_hash = provenance_mod.compute_hash(payload)

        if bypass.is_bypass_mode(self._root):
            # Cheapest possible check, ahead of gate.evaluate() and any
            # AALP call: an operator's flag file must pre-empt everything.
            # No telemetry call is reached on this path -- planned-
            # downtime bypass activity must never skew historical
            # compression statistics (explicit requirement).
            new_provenance = Provenance(processed=True, source_hash=source_hash)
            return CompressionResult(
                outcome=Outcome.MAINTENANCE,
                mode="PASS",
                output=payload,
                warnings=[],
                message="ACP is in bypass mode",
                provenance=new_provenance,
            )

        decision = gate.evaluate(payload, traffic_class, thresholds=self._thresholds)

        if decision.action is GateAction.BYPASS:
            self._telemetry.increment("compression_bypass_payloads")
            self._telemetry.increment(
                "compression_bypass_tokens", decision.estimated_tokens
            )
            new_provenance = Provenance(processed=True, source_hash=source_hash)
            return CompressionResult(
                outcome=Outcome.SUCCESS,
                mode="PASS",
                output=payload,
                warnings=[],
                message="gate bypass",
                provenance=new_provenance,
            )

        # INSPECT: hand off to the external compressor.
        self._telemetry.increment("compression_attempts")
        target_tokens = _compute_target_tokens(decision.estimated_tokens)
        max_tokens = _compute_max_tokens(decision.estimated_tokens)
        # A fresh id per call, unlike Stage 2's fixed "solo": real
        # coalescing (§6-§14) may land several concurrent compress()
        # calls in the same physical generation, and §14 requires every
        # member id within a generation to be distinct.
        member_id = secrets.token_hex(8)
        body = _build_request_body(
            payload, traffic_class, decision.reduction_hint, self._model,
            target_tokens, max_tokens, member_id, self._thinking_budget_tokens,
        )

        # request.queue rather than request.forward (§13's "queue-of-one
        # uses the identical grammar" -- the same envelope shape is used
        # whether or not this call ends up actually coalesced with
        # others). `queue_key` identifies which physical requests are
        # eligible to coalesce; it is scoped to everything that must
        # match for a shared physical response to be valid for every
        # member (provider, upstream path, model, thinking budget) --
        # payload content is deliberately excluded, since that's exactly
        # what coalescing is meant to combine across calls.
        queue_key = (
            f"acp-queue/1:{self._provider_id}:{_FORWARD_PATH}:"
            f"{self._model}:{self._thinking_budget_tokens}"
        )

        started = time.monotonic()
        queue_result = self._aalp_client.submit_queue_member(
            self._provider_id,
            queue_key,
            "POST",
            _FORWARD_PATH,
            headers={"Content-Type": "application/json"},
            body=body,
            flow_id=flow_id,
            member_id=member_id,
            queue_timeout=self._queue_timeout,
            compression_timeout=self._compression_timeout,
            total_timeout=self._total_timeout,
        )
        # `AalpClient.submit_queue_member` does not hand back a queue/
        # execution timing split, so only the whole round-trip is
        # measured here, against `compression_execution_ms`.
        # `queue_result.generation_id`/`member_count` are available for
        # cross-member audit correlation but not yet consumed here.
        elapsed_seconds = time.monotonic() - started

        outcome = queue_result.outcome
        failure_message = queue_result.message
        parsed_mode: str | None = None
        parsed_output: str | None = None
        usage: dict[str, Any] | None = None

        if outcome is Outcome.SUCCESS:
            try:
                parsed_mode, parsed_output, usage = _parse_compressor_response(
                    queue_result.body, member_id
                )
            except CompressorProtocolViolation as violation:
                outcome = Outcome.INVALID_RESPONSE
                failure_message = str(violation)

        if outcome is Outcome.SUCCESS:
            return self._handle_success(
                payload, parsed_mode, parsed_output, usage, decision.estimated_tokens,
                elapsed_seconds, source_hash,
            )

        return self._handle_failure(
            payload, outcome, failure_message, decision.estimated_tokens,
            elapsed_seconds, source_hash,
        )

    def _handle_success(
        self,
        payload: str,
        mode: str,
        transformed_output: str,
        usage: dict[str, Any] | None,
        estimated_input_tokens: int,
        elapsed_seconds: float,
        source_hash: str,
    ) -> CompressionResult:
        self._telemetry.increment("compression_successes")
        self._telemetry.increment(
            "compression_execution_ms", int(elapsed_seconds * 1000)
        )

        if usage and "input_tokens" in usage and "output_tokens" in usage:
            input_tokens = int(usage["input_tokens"])
            reported_output_tokens = int(usage["output_tokens"])
        else:
            # Graceful fallback for a backend (e.g. a test fixture) that
            # doesn't produce a `usage` block: estimate from raw text.
            input_tokens = estimated_input_tokens
            reported_output_tokens = gate.estimate_tokens(transformed_output)

        if mode == "PASS":
            # ACP substitutes the original payload verbatim for PASS,
            # so downstream token accounting must reflect that (no
            # reduction actually happened), not whatever the model's
            # own -- discarded -- trailing text would have cost.
            final_output = payload
            output_tokens = input_tokens
        else:
            final_output = transformed_output
            output_tokens = reported_output_tokens

        self._telemetry.increment("compression_input_tokens", input_tokens)
        self._telemetry.increment("compression_output_tokens", output_tokens)
        self._telemetry.increment(
            "compression_saved_tokens", max(0, input_tokens - output_tokens)
        )

        new_provenance = Provenance(processed=True, source_hash=source_hash)
        warnings = self._warnings.record_success()
        return CompressionResult(
            outcome=Outcome.SUCCESS,
            mode=mode,
            output=final_output,
            warnings=warnings,
            message="",
            provenance=new_provenance,
        )

    def _handle_failure(
        self,
        payload: str,
        outcome: Outcome,
        failure_message: str,
        estimated_tokens: int,
        elapsed_seconds: float,
        source_hash: str,
    ) -> CompressionResult:
        # AALP itself in maintenance mode (as opposed to ACP's own bypass
        # short-circuit at the top of compress(), which never reaches
        # this method at all): treated the same way for telemetry -- no
        # counters, no warning-tracker update. Planned-downtime activity
        # on either side of the AALP boundary must never skew historical
        # compression statistics (explicit requirement).
        if outcome is Outcome.MAINTENANCE:
            new_provenance = Provenance(processed=True, source_hash=source_hash)
            return CompressionResult(
                outcome=outcome,
                mode="PASS",
                output=payload,
                warnings=[],
                message=failure_message,
                provenance=new_provenance,
            )

        counter_name = _TIMEOUT_COUNTER_BY_OUTCOME.get(outcome)
        self._telemetry.increment(counter_name or _UNCOUNTERED_FAILURE_BUCKET)

        warnings = self._warnings.record_failure(
            outcome,
            elapsed_seconds=elapsed_seconds,
            estimated_tokens=estimated_tokens,
        )

        # A failed/timed-out attempt still receives a verdict (always
        # passthrough); mark it processed for audit purposes.
        new_provenance = Provenance(processed=True, source_hash=source_hash)

        self._telemetry.increment("compression_timeout_bypass_tokens", estimated_tokens)
        return CompressionResult(
            outcome=outcome,
            mode="PASS",
            output=payload,
            warnings=warnings,
            message=failure_message,
            provenance=new_provenance,
        )
