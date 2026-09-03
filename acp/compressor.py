"""ACP's compressor orchestration: gate -> provenance -> AALP -> policy -> telemetry.

This module defines and implements ACP's compression request/response
protocol end-to-end (it did not exist anywhere before this wave). The
external compressor is called through `acp.aalp_client.AalpClient`
only -- this module never talks to a provider directly and never falls
back to native inference on failure; there is no such code path here.

The wire shape is the Anthropic Messages API (`POST /v1/messages` on
whichever AALP provider id is configured, default `"ci"`):

    {
      "model": "<configurable>",
      "max_tokens": <computed>,
      "system": "<COMPRESSOR_SYSTEM_PROMPT>",
      "messages": [{"role": "user", "content": "<traffic-class preamble + payload>"}]
    }

The compressor's first response line must be exactly one of
`ACP-MODE: PASS`, `ACP-MODE: COMPACT`, `ACP-MODE: COMPRESS`. For PASS,
ACP substitutes the original payload verbatim itself -- it never trusts
whatever (if anything) the model echoes back after that line. For
COMPACT/COMPRESS, everything after the mode line and its one blank
line is the transformed output ACP returns.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from acp import gate
from acp import provenance as provenance_mod
from acp.aalp_client import AalpClient
from acp.errors import AcpResult, Outcome, TrafficClass
from acp.gate import GateAction, TrafficClassThresholds
from acp.provenance import Provenance
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

# Ceiling on the compressed output's `max_tokens`, tunable later. The
# actual cap used per call is min(this ceiling, half the payload's own
# estimated token count) -- a reduction stage should not be allowed to
# ask for more than half its input's tokens back out, floored so tiny
# payloads still get a workable budget. See `_compute_max_tokens`.
DEFAULT_MAX_TOKENS_CEILING = 4096
_MIN_MAX_TOKENS = 256

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

COMPRESSOR_SYSTEM_PROMPT = """You are ACP's compression stage: a loss-minimizing context processor, not a task-solving agent.

Given a payload, choose exactly one mode:
- PASS: content is already dense, exact preservation is required, or reduction would be unsafe.
- COMPACT: preferred reduction. Remove redundancy/noise while preserving substantive evidence (collapse repeated warnings, deduplicate repeated success/log lines, group identical failures with occurrence counts, retain exact representative evidence).
- COMPRESS: structured semantic reduction for material that cannot remain mostly verbatim at a reasonable size. Produce a stable evidence capsule, not loose prose.

When relevant, preserve exactly or losslessly represent: latest user instructions; requirements and prohibitions; file paths; code identifiers and symbols; API signatures; commands; compiler/test/runtime errors; numeric values and thresholds; versions and hashes; decisions already made and their status; validation results; unresolved ambiguity/conflict; pending work and known failure state.

You must NOT: redesign the system; solve architecture or debugging work that belongs downstream; silently resolve ambiguity; change task intent; invent missing facts; pick a side between conflicting evidence without preserving the conflict; declare unfinished work complete.

Respond with exactly one line as your first line: "ACP-MODE: PASS", "ACP-MODE: COMPACT", or "ACP-MODE: COMPRESS".
If PASS, output nothing else after that line.
If COMPACT or COMPRESS, follow the mode line with exactly one blank line, then your transformed output only -- no commentary, no preamble, no meta-discussion of your own process.
"""

_MODE_LINES = {
    "ACP-MODE: PASS": "PASS",
    "ACP-MODE: COMPACT": "COMPACT",
    "ACP-MODE: COMPRESS": "COMPRESS",
}

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


class FailurePolicy(Enum):
    PASSTHROUGH = "passthrough"
    BLOCK = "block"


@dataclass
class CompressionResult(AcpResult):
    """`AcpResult` plus the `Provenance` a caller must carry forward.

    Reuses `AcpResult`'s `outcome`/`mode`/`output`/`warnings`/`message`
    fields rather than inventing a parallel shape; `provenance` is the
    only field this wave adds.
    """

    provenance: Provenance | None = None


def _compute_max_tokens(estimated_input_tokens: int, ceiling: int) -> int:
    """Cap the compressor's requested `max_tokens` so any actual
    compression (the INSPECT path -- BYPASS never reaches this) cannot
    ask for more than half of its own estimated input: an architectural
    >=50% reduction guarantee, not just an advisory ceiling.

    `estimated_input_tokens // 2` alone would undershoot the floor only
    for a payload smaller than `2 * _MIN_MAX_TOKENS` estimated tokens;
    every built-in traffic class's `bypass_max` (gate.py) is well above
    that, so `_MIN_MAX_TOKENS` never actually binds against the 50%
    guarantee for a default-threshold payload. A caller-supplied custom
    threshold letting a much smaller payload reach INSPECT is the one
    case where the floor could push `max_tokens` back above half its
    input -- an accepted trade favoring a workable output budget over
    the ratio for payloads that small.
    """
    return max(_MIN_MAX_TOKENS, min(ceiling, estimated_input_tokens // 2))


def _build_user_message(
    payload: str, traffic_class: TrafficClass, reduction_hint: str | None, max_tokens: int
) -> str:
    hint = reduction_hint or "none"
    return (
        f"Traffic class: {traffic_class.value}. Size-band hint: {hint}. "
        f"Output budget: {max_tokens} tokens -- your response (including the "
        f"ACP-MODE line) is cut off if it exceeds this, so scale COMPACT/COMPRESS "
        f"output to fit; PASS is exempt, it never needs the budget.\n\n"
        f"---\n\n{payload}"
    )


def _build_request_body(
    payload: str,
    traffic_class: TrafficClass,
    reduction_hint: str | None,
    model: str,
    max_tokens: int,
    thinking_budget_tokens: int | None = None,
) -> bytes:
    if thinking_budget_tokens is not None:
        max_tokens = max(
            max_tokens, thinking_budget_tokens + _MIN_OUTPUT_HEADROOM_ABOVE_THINKING
        )
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": COMPRESSOR_SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": _build_user_message(
                    payload, traffic_class, reduction_hint, max_tokens
                ),
            }
        ],
    }
    if thinking_budget_tokens is not None:
        request["thinking"] = {
            "type": "enabled",
            "budget_tokens": thinking_budget_tokens,
        }
    return json.dumps(request).encode("utf-8")


def _parse_compressor_response(body: bytes) -> tuple[str, str, dict[str, Any] | None]:
    """Parse one Anthropic-Messages-shaped compressor response.

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

    first_line, _, remainder = text.partition("\n")
    mode = _MODE_LINES.get(first_line.strip())
    if mode is None:
        raise CompressorProtocolViolation(
            f"missing or invalid ACP-MODE line, got {first_line.strip()!r}"
        )

    if remainder.startswith("\n"):
        remainder = remainder[1:]

    usage = response_json.get("usage")
    if not isinstance(usage, dict):
        usage = None

    # The tightened `max_tokens` cap in `_compute_max_tokens` (>=50%
    # reduction guarantee) makes truncation a real, newly-introduced
    # risk for COMPACT/COMPRESS: a real Anthropic-shaped response cut
    # off mid-output reports `stop_reason: "max_tokens"`. PASS is exempt
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


def _select_failure_policy(
    estimated_tokens: int,
    block_above_estimated_tokens: int | None,
    override: FailurePolicy | None,
) -> FailurePolicy:
    if override is not None:
        return override
    if (
        block_above_estimated_tokens is not None
        and estimated_tokens > block_above_estimated_tokens
    ):
        return FailurePolicy.BLOCK
    return FailurePolicy.PASSTHROUGH


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
        max_tokens_ceiling: int = DEFAULT_MAX_TOKENS_CEILING,
        thinking_budget_tokens: int | None = DEFAULT_THINKING_BUDGET_TOKENS,
        queue_timeout: float = DEFAULT_QUEUE_TIMEOUT_SECONDS,
        compression_timeout: float = DEFAULT_COMPRESSION_TIMEOUT_SECONDS,
        total_timeout: float = DEFAULT_TOTAL_TIMEOUT_SECONDS,
        thresholds: dict[TrafficClass, TrafficClassThresholds] | None = None,
        # Phase 6 benchmarking decision, not fixed here: `None` means
        # "always PASSTHROUGH by default until benchmarked". Pass a
        # token-count threshold to make large payloads BLOCK on
        # failure instead, or override per-call via `compress(...,
        # force_policy=...)`.
        block_above_estimated_tokens: int | None = None,
    ) -> None:
        self._aalp_client = aalp_client
        self._telemetry = telemetry
        self._provider_id = provider_id
        self._model = model
        self._max_tokens_ceiling = max_tokens_ceiling
        self._thinking_budget_tokens = thinking_budget_tokens
        self._queue_timeout = queue_timeout
        self._compression_timeout = compression_timeout
        self._total_timeout = total_timeout
        self._thresholds = thresholds
        self._block_above_estimated_tokens = block_above_estimated_tokens
        self._warnings = CompressionWarningTracker()

    @property
    def warning_tracker(self) -> CompressionWarningTracker:
        return self._warnings

    def compress(
        self,
        payload: str,
        traffic_class: TrafficClass,
        *,
        prior_provenance: Provenance | None = None,
        flow_id: str | None = None,
        force_policy: FailurePolicy | None = None,
    ) -> CompressionResult:
        source_hash = provenance_mod.compute_hash(payload)

        # Anti-recursion guard: must short-circuit before any
        # telemetry/gate/AALP work. No counters are touched here by
        # design -- a repeated-crossing no-op should carry effectively
        # zero overhead, not accrue phantom attempt/bypass counts.
        if not provenance_mod.should_reprocess(prior_provenance, source_hash):
            return CompressionResult(
                outcome=Outcome.SUCCESS,
                mode="PASS",
                output=payload,
                warnings=[],
                message="anti-recursion short-circuit: already processed",
                provenance=prior_provenance,
            )

        decision = gate.evaluate(payload, traffic_class, thresholds=self._thresholds)

        if decision.action is GateAction.BYPASS:
            self._telemetry.increment("compression_bypass_payloads")
            self._telemetry.increment(
                "compression_bypass_tokens", decision.estimated_tokens
            )
            new_provenance = provenance_mod.next_provenance(
                prior_provenance, source_hash, processed=True
            )
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
        max_tokens = _compute_max_tokens(decision.estimated_tokens, self._max_tokens_ceiling)
        body = _build_request_body(
            payload, traffic_class, decision.reduction_hint, self._model, max_tokens,
            self._thinking_budget_tokens,
        )

        started = time.monotonic()
        forward_result = self._aalp_client.forward(
            self._provider_id,
            "POST",
            _FORWARD_PATH,
            headers={"Content-Type": "application/json"},
            body=body,
            flow_id=flow_id,
            queue_timeout=self._queue_timeout,
            compression_timeout=self._compression_timeout,
            total_timeout=self._total_timeout,
        )
        # `AalpClient.forward` does not hand back a queue/execution
        # timing split, so only the whole round-trip is measured here,
        # against `compression_execution_ms`; `compression_queue_wait_ms`
        # is left untouched by this module.
        elapsed_seconds = time.monotonic() - started

        outcome = forward_result.outcome
        failure_message = forward_result.message
        parsed_mode: str | None = None
        parsed_output: str | None = None
        usage: dict[str, Any] | None = None

        if outcome is Outcome.SUCCESS:
            try:
                parsed_mode, parsed_output, usage = _parse_compressor_response(
                    forward_result.body
                )
            except CompressorProtocolViolation as violation:
                outcome = Outcome.INVALID_RESPONSE
                failure_message = str(violation)

        if outcome is Outcome.SUCCESS:
            return self._handle_success(
                payload, parsed_mode, parsed_output, usage, decision.estimated_tokens,
                elapsed_seconds, prior_provenance, source_hash,
            )

        return self._handle_failure(
            payload, outcome, failure_message, decision.estimated_tokens,
            elapsed_seconds, prior_provenance, source_hash, force_policy,
        )

    def _handle_success(
        self,
        payload: str,
        mode: str,
        transformed_output: str,
        usage: dict[str, Any] | None,
        estimated_input_tokens: int,
        elapsed_seconds: float,
        prior_provenance: Provenance | None,
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

        new_provenance = provenance_mod.next_provenance(
            prior_provenance, source_hash, processed=True
        )
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
        prior_provenance: Provenance | None,
        source_hash: str,
        force_policy: FailurePolicy | None,
    ) -> CompressionResult:
        counter_name = _TIMEOUT_COUNTER_BY_OUTCOME.get(outcome)
        self._telemetry.increment(counter_name or _UNCOUNTERED_FAILURE_BUCKET)

        policy = _select_failure_policy(
            estimated_tokens, self._block_above_estimated_tokens, force_policy
        )
        blocked = policy is FailurePolicy.BLOCK

        warnings = self._warnings.record_failure(
            outcome,
            elapsed_seconds=elapsed_seconds,
            estimated_tokens=estimated_tokens,
            blocked=blocked,
        )

        # A failed/timed-out attempt still received a verdict from this
        # pipeline (passthrough or block); mark it processed so a
        # caller that re-crosses the *same* boundary with the same
        # provenance record doesn't loop. This does not disable retrying
        # a genuinely re-submitted payload -- that's a fresh `compress()`
        # call with no prior_provenance, which is always evaluated.
        new_provenance = provenance_mod.next_provenance(
            prior_provenance, source_hash, processed=True
        )

        if blocked:
            self._telemetry.increment("compression_timeout_blocked_payloads")
            placeholder = (
                f"[ACP: content blocked -- compression failed ({outcome.value}) "
                f"and this payload's failure policy is BLOCK; "
                f"~{estimated_tokens} tokens withheld]"
            )
            return CompressionResult(
                outcome=outcome,
                mode=None,
                output=placeholder,
                warnings=warnings,
                message=failure_message,
                provenance=new_provenance,
            )

        self._telemetry.increment("compression_timeout_bypass_tokens", estimated_tokens)
        return CompressionResult(
            outcome=outcome,
            mode="PASS",
            output=payload,
            warnings=warnings,
            message=failure_message,
            provenance=new_provenance,
        )
