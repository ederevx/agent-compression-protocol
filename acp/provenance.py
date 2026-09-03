"""Provenance tracking and the anti-recursion guard.

Every ACP-processed object carries a `Provenance` record: whether it has
already been through the compression pipeline (`processed`), a content
hash of the object it was derived from (`source_hash`), and a
generation counter that increments each time a genuinely new payload
(a different `source_hash`) is evaluated.

CALLER-DISCIPLINE INVARIANT: a compressor's own result object must
never be handed back into `should_reprocess` (or the compressor itself)
as if it were fresh, un-evaluated input. `should_reprocess` tells a
caller whether a payload crossing a boundary needs (re-)evaluation; it
is not a substitute for keeping track of which objects are ACP's own
output. Feeding ACP's own output back in as "new" input without routing
it through this guard first can create an uncontrolled compress -> feed
back -> compress-again loop.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


def compute_hash(payload: bytes | str) -> str:
    """SHA-256 hex digest of `payload` (UTF-8 encoded if given as str)."""
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Provenance:
    processed: bool
    source_hash: str
    generation: int


def should_reprocess(provenance: Provenance | None, current_source_hash: str) -> bool:
    """Decide whether a payload needs (re-)evaluation by the compressor.

    Rules, in order:
      - No prior provenance (never seen before) -> True.
      - Prior provenance, `processed=True`, same `source_hash` -> False
        (an unchanged ACP output crossing another boundary; do not
        blindly recompress it).
      - Prior provenance with a different `source_hash` -> True (this is
        a new payload, e.g. a newly aggregated object, so it may be
        re-evaluated).
    """
    if provenance is None:
        return True
    if provenance.processed and provenance.source_hash == current_source_hash:
        return False
    return provenance.source_hash != current_source_hash


def next_provenance(
    prior: Provenance | None,
    current_source_hash: str,
    *,
    processed: bool = True,
) -> Provenance:
    """Build the `Provenance` to attach after evaluating a payload.

    Bumps `generation` by 1 relative to `prior` when `current_source_hash`
    differs from the prior one (a genuinely new payload); starts at
    generation 0 when there is no prior provenance; otherwise (same
    hash) keeps the prior generation unchanged.
    """
    if prior is None:
        return Provenance(
            processed=processed, source_hash=current_source_hash, generation=0)
    if prior.source_hash != current_source_hash:
        return Provenance(
            processed=processed,
            source_hash=current_source_hash,
            generation=prior.generation + 1,
        )
    return Provenance(
        processed=processed,
        source_hash=current_source_hash,
        generation=prior.generation,
    )
