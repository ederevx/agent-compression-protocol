"""Deterministic size/type gate: bypass vs. inspect, per traffic class.

This module answers exactly one question — is a payload small enough to
skip the compressor entirely (`BYPASS`), or does it need to be handed to
the compressor for a PASS/COMPACT/COMPRESS decision (`INSPECT`)? Size
alone is not sufficient to choose *how* to reduce a payload (e.g. an
exact patch or source file may warrant PASS at a size where a log dump
should COMPACT) — that nuance is the compressor's job. This gate only
produces a coarse `reduction_hint` telling the compressor which band the
payload landed in, as a hint it may use or override.

Thresholds are v1 starting defaults, expected to be benchmarked and
tuned later. They are exposed as module-level constants (not baked into
function bodies) so a caller can override them per-call without editing
this file — either by passing an explicit `thresholds=` argument or by
monkeypatching the module-level defaults.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from acp.errors import TrafficClass


def estimate_tokens(text: str) -> int:
    """Rough chars-per-token estimate: len(text) // 4.

    This is a crude, swappable approximation (English prose and code
    both average roughly 4 characters per token for common tokenizers).
    A caller with access to a real tokenizer should compute token counts
    itself and pass them directly wherever this module accepts a token
    count, rather than relying on this estimator.
    """
    return len(text) // 4


class GateAction(Enum):
    BYPASS = "bypass"
    INSPECT = "inspect"


@dataclass(frozen=True)
class ThresholdBand:
    """One (lower-bound-exclusive, upper-bound-inclusive) band of a policy.

    `upper` of None means "no upper bound" (the final, most-aggressive
    band for a traffic class).
    """

    upper: int | None
    hint: str | None


@dataclass(frozen=True)
class TrafficClassThresholds:
    """A traffic class's bypass cutoff plus ordered inspect bands.

    `bypass_max` is the largest estimated-token count still eligible for
    BYPASS (i.e. `estimated_tokens <= bypass_max` bypasses). `bands` is
    evaluated in order; the first band whose `upper` is None or exceeds
    the payload's token count applies.
    """

    bypass_max: int
    bands: tuple[ThresholdBand, ...]


# v1 starting defaults — tunable. Override by passing `thresholds=` to
# `evaluate`, or by replacing these module attributes directly.
GENERAL_THRESHOLDS = TrafficClassThresholds(
    bypass_max=8_000,
    bands=(
        ThresholdBand(upper=24_000, hint=None),
        ThresholdBand(upper=50_000, hint="compact_preferred"),
        ThresholdBand(upper=None, hint="reduction_required"),
    ),
)

NATIVE_AGENT_REPORT_THRESHOLDS = TrafficClassThresholds(
    bypass_max=4_000,
    bands=(
        ThresholdBand(upper=8_000, hint=None),
        ThresholdBand(upper=20_000, hint="reduction_required"),
        ThresholdBand(upper=None, hint="aggressive_reduction_required"),
    ),
)

# Downward task/supporting context: deliberately more conservative than
# the native-agent-report policy above (bypass/inspect bands kick in at
# higher token counts), because preserving the task instruction matters
# more here than aggressively shrinking supporting material. This gate
# only sizes the supporting material; it never rewrites the instruction.
DOWNWARD_CONTEXT_THRESHOLDS = TrafficClassThresholds(
    bypass_max=12_000,
    bands=(
        ThresholdBand(upper=32_000, hint=None),
        ThresholdBand(upper=64_000, hint="compact_preferred"),
        ThresholdBand(upper=None, hint="reduction_required"),
    ),
)

DEFAULT_THRESHOLDS: dict[TrafficClass, TrafficClassThresholds] = {
    TrafficClass.GENERAL: GENERAL_THRESHOLDS,
    TrafficClass.NATIVE_AGENT_REPORT: NATIVE_AGENT_REPORT_THRESHOLDS,
    TrafficClass.DOWNWARD_CONTEXT: DOWNWARD_CONTEXT_THRESHOLDS,
}


@dataclass(frozen=True)
class GateDecision:
    action: GateAction
    traffic_class: TrafficClass
    estimated_tokens: int
    reduction_hint: str | None


def evaluate(
    text: str,
    traffic_class: TrafficClass,
    *,
    thresholds: dict[TrafficClass, TrafficClassThresholds] | None = None,
    token_estimator=estimate_tokens,
) -> GateDecision:
    """Decide BYPASS vs. INSPECT (with a reduction-band hint) for `text`.

    `thresholds` defaults to the module-level `DEFAULT_THRESHOLDS`
    dict; pass an override to tune policy per-call without mutating
    module state. `token_estimator` defaults to `estimate_tokens` and
    may be swapped for a real tokenizer.
    """
    policy_table = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
    policy = policy_table[traffic_class]
    token_count = token_estimator(text)

    if token_count <= policy.bypass_max:
        return GateDecision(
            action=GateAction.BYPASS,
            traffic_class=traffic_class,
            estimated_tokens=token_count,
            reduction_hint=None,
        )

    for band in policy.bands:
        if band.upper is None or token_count <= band.upper:
            return GateDecision(
                action=GateAction.INSPECT,
                traffic_class=traffic_class,
                estimated_tokens=token_count,
                reduction_hint=band.hint,
            )

    # Unreachable: the last band always has upper=None in the defaults,
    # but guard anyway in case a caller-supplied policy omits it.
    return GateDecision(
        action=GateAction.INSPECT,
        traffic_class=traffic_class,
        estimated_tokens=token_count,
        reduction_hint="reduction_required",
    )
