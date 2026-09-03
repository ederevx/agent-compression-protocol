"""Content hashing and the `Provenance` audit record.

Every compression result carries a `Provenance` record: a content hash
of the payload it was derived from (`source_hash`), and whether it was
actually processed by the compressor (`processed`). This is audit
metadata only -- no code path re-supplies a prior `Provenance` back
into a later call, so it never influences a compression decision.
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
