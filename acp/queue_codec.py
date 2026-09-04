"""ACP's `ACP-QUEUE/1` model-facing wire grammar
(agent_protocols_v1_queue_coalescing_adjustment_metadata_v1.md §10-§19).

This module owns exactly the *textual* member-train framing embedded
inside a provider request's `content`/response text -- it knows nothing
about the surrounding Messages-API envelope (that stays in
`acp/compressor.py`, which already builds/parses that shape) and nothing
about AALP's own `X-Aalp-Queue-Key`/generation transport (that's
`acp/aalp_client.py`'s `submit_queue_member()`). Three independent
layers, three independent modules -- matching how the adjustment itself
separates AALP's opaque queue_key (§9) from ACP's queue grammar (§15)
from the physical Messages request (§17).

Stage 2 scope: `acp/compressor.py` migrates onto this grammar for
`member_count == 1` first (the adjustment's own §31 migration gate).
`build_member_train`/`parse_queue_response` are already list-of-members
APIs, not a singleton-only shortcut -- Stage 3's real accumulation grows
the caller (AALP's mechanical train assembly, §11), not this module's
shape.

Concrete wire format (this pass's own choice; the adjustment doc leaves
the exact per-item framing unspecified, only its conceptual ordering --
§12/§13/§14). Each logical member is framed as:

    ACP-QUEUE-ITEM: <opaque member id>
    <member content, built by the caller>

in submission order, followed by a trailing:

    ACP-QUEUE-MEMBER-COUNT: <n>

The compressor's response must mirror the same per-item framing:

    ACP-QUEUE-ITEM: <opaque member id>
    ACP-MODE: PASS|COMPACT|COMPRESS

    <transformed content, or nothing for PASS>

`parse_queue_response` enforces §14's declared-count/id-set invariant
(no missing, duplicated, or invented item ids) and raises
`QueueProtocolViolation` on any violation -- per §19, a caller must treat
that as a whole-generation failure, never a partial one.

Known limitation, deferred to Stage 5's adversarial fixtures (§30): this
is a textual delimiter format, so payload content that itself contains a
line beginning `ACP-QUEUE-ITEM: ` could in principle be echoed back
verbatim by PASS/COMPACT and confuse response parsing. Not addressed
here; flagged for the adjustment's own adversarial cross-member-
contamination testing pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

QUEUE_PROTOCOL_NAME = "ACP-QUEUE/1"

_ITEM_HEADER_RE = re.compile(r"^ACP-QUEUE-ITEM: (?P<id>.+)$", re.MULTILINE)

_MODE_LINES = {
    "ACP-MODE: PASS": "PASS",
    "ACP-MODE: COMPACT": "COMPACT",
    "ACP-MODE: COMPRESS": "COMPRESS",
}

# §15: permanent isolation/output-format invariants, applied identically
# regardless of member count (§13) -- appended after a caller's own base
# compressor instructions, never replacing them.
QUEUE_ISOLATION_ADDENDUM = f"""This request follows {QUEUE_PROTOCOL_NAME}: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request. These rules apply even when only one ITEM is present.

Respond with exactly one block per submitted ITEM, in any order, each shaped as:

ACP-QUEUE-ITEM: <the same id from the request>
ACP-MODE: PASS, COMPACT, or COMPRESS

<the transformed content for that item, or nothing after PASS>

Return exactly one block for every supplied item id. Never merge, omit, duplicate, or invent item ids."""


class QueueProtocolViolation(ValueError):
    """A queue response failed §14/§19's whole-generation validation.

    Per §19, a caller must treat this as a failure of every member in
    the generation, never a partial one -- there is deliberately no
    partial-success/cache path here.
    """


@dataclass
class QueueMemberRequest:
    member_id: str
    content: str


@dataclass
class QueueMemberResult:
    member_id: str
    mode: str
    output: str


def build_system_prompt(base_prompt: str) -> str:
    """Compose a caller's base compressor instructions with the queue
    grammar's isolation/output-format addendum. Static-prefix-first per
    §12: this belongs in the request's `system` field, never in the
    per-call member train."""
    return base_prompt.rstrip() + "\n\n" + QUEUE_ISOLATION_ADDENDUM + "\n"


def build_member_train(members: list[QueueMemberRequest]) -> str:
    """§11/§14: the volatile member-train-plus-count suffix. Identical
    shape for one member (§13's "queue-of-one uses the identical
    grammar") or several -- only `len(members)` changes."""
    if not members:
        raise ValueError("build_member_train requires at least one member")
    blocks = [
        f"ACP-QUEUE-ITEM: {member.member_id}\n{member.content}"
        for member in members
    ]
    blocks.append(f"ACP-QUEUE-MEMBER-COUNT: {len(members)}")
    return "\n\n".join(blocks)


def parse_queue_response(
    text: str, expected_member_ids: list[str]
) -> list[QueueMemberResult]:
    """Parse a queue-grammar response text and enforce §14's
    declared-count/id-set invariant against `expected_member_ids`
    (the ids ACP itself submitted, in submission order -- duplicates in
    this list would be a caller bug, not a response defect).

    Raises `QueueProtocolViolation` if: no item blocks are found; an
    item block is missing a valid `ACP-MODE` line; the response's item
    ids (as a set) don't exactly match `expected_member_ids` (as a set);
    or the response declares any duplicate item id.
    """
    matches = list(_ITEM_HEADER_RE.finditer(text))
    if not matches:
        raise QueueProtocolViolation("no ACP-QUEUE-ITEM blocks found in response")

    results: list[QueueMemberResult] = []
    for index, match in enumerate(matches):
        member_id = match.group("id").strip()
        block_start = match.end()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[block_start:block_end].lstrip("\n")

        first_line, _, remainder = block.partition("\n")
        mode = _MODE_LINES.get(first_line.strip())
        if mode is None:
            raise QueueProtocolViolation(
                f"item {member_id!r}: missing or invalid ACP-MODE line, "
                f"got {first_line.strip()!r}"
            )
        if remainder.startswith("\n"):
            remainder = remainder[1:]
        results.append(
            QueueMemberResult(member_id=member_id, mode=mode, output=remainder.rstrip("\n"))
        )

    returned_ids = [result.member_id for result in results]
    if len(returned_ids) != len(set(returned_ids)):
        raise QueueProtocolViolation(f"duplicate item ids in response: {returned_ids}")
    if set(returned_ids) != set(expected_member_ids):
        raise QueueProtocolViolation(
            f"response item ids {sorted(set(returned_ids))} do not match "
            f"submitted item ids {sorted(set(expected_member_ids))}"
        )

    return results
