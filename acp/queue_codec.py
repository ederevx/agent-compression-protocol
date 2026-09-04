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

Stage 3 scope: real multi-member accumulation happens entirely on AALP's
side (`aalp/queue.py`'s `build_physical_body`, mechanically joining each
member's own framed block) -- ACP itself only ever builds *its own*
single member's block and envelope (`build_member_block`/
`build_queue_envelope`), and only ever parses *its own* member's result
back out of a response that may also contain other, unknown-to-it
members' blocks (`parse_queue_member_result`). No caller here ever has
visibility into who else it was coalesced with (§9) -- that asymmetry
(build one block, parse a response that might hold several) is the
central Stage 3 change from Stage 2's build/parse-the-whole-train shape.

Concrete wire format (this pass's own choice; the adjustment doc leaves
the exact per-item framing unspecified, only its conceptual ordering --
§12/§13/§14). Each logical member is framed as:

    ACP-QUEUE-ITEM: <opaque member id>
    <member content, built by the caller>

AALP mechanically joins members in FIFO order and appends a trailing:

    ACP-QUEUE-MEMBER-COUNT: <n>

to the physical request (§11/§12) -- ACP never builds that trailer
itself, since it never knows the final count until AALP assembles it.

The compressor's response must mirror the same per-item framing:

    ACP-QUEUE-ITEM: <opaque member id>
    ACP-MODE: PASS|COMPACT|COMPRESS

    <transformed content, or nothing for PASS>

`parse_queue_member_result` enforces §14's per-member invariant (exactly
one block for the caller's own member id, never zero or duplicated) and
raises `QueueProtocolViolation` on any violation -- per §19, a caller
must treat a structurally malformed response (no item blocks at all) as
a whole-generation failure, never a partial one.

Known limitation, deferred to Stage 5's adversarial fixtures (§30): this
is a textual delimiter format, so payload content that itself contains a
line beginning `ACP-QUEUE-ITEM: ` could in principle be echoed back
verbatim by PASS/COMPACT and confuse response parsing. Not addressed
here; flagged for the adjustment's own adversarial cross-member-
contamination testing pass.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

QUEUE_PROTOCOL_NAME = "ACP-QUEUE/1"

# §11/§12: what AALP's `aalp.queue.QueueGeneration.build_physical_body()`
# joins members with, and the trailer template it appends after them --
# fixed constants shared by every caller of `build_queue_envelope`, since
# AALP compares nothing about them beyond using them mechanically.
_MEMBER_JOIN = "\n\n"
_COUNT_TEMPLATE = "ACP-QUEUE-MEMBER-COUNT: {member_count}"

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


def build_member_block(member: QueueMemberRequest) -> str:
    """One member's own framed text (§11) -- the piece AALP mechanically
    joins with every other coalesced member's block to build the shared
    physical request body. A caller only ever builds its own block; it
    never has visibility into who else it might be coalesced with (§9),
    so unlike Stage 2's `build_member_train` this cannot build the full
    train or the trailing count -- AALP owns both, since only AALP knows
    the final membership."""
    return f"ACP-QUEUE-ITEM: {member.member_id}\n{member.content}"


def build_queue_envelope(
    shared: dict[str, Any], content_path: list, member: QueueMemberRequest
) -> bytes:
    """The self-describing JSON envelope `AalpClient.submit_queue_member`'s
    `body` now carries (§10-§12): `shared` is the full physical request
    skeleton (already containing a placeholder value at `content_path`,
    which AALP overwrites with the assembled member train once admission
    happens) -- AALP never interprets `shared` beyond that one path, so
    any Messages-API-shaped dict works here unchanged. `content_path` is
    a list of dict keys / list indices identifying where inside `shared`
    the member train belongs (e.g. `["messages", 0, "content"]`)."""
    envelope = {
        "shared": shared,
        "content_path": list(content_path),
        "member_block": build_member_block(member),
        "member_join": _MEMBER_JOIN,
        "count_template": _COUNT_TEMPLATE,
    }
    return json.dumps(envelope).encode("utf-8")


def parse_queue_member_result(text: str, member_id: str) -> QueueMemberResult:
    """Parse a queue-grammar response and extract just `member_id`'s own
    block, ignoring any other members that may have been coalesced into
    the same physical response alongside it -- a caller only ever knows
    its own member id, never the full membership of the generation it
    landed in (§9).

    Raises `QueueProtocolViolation` if: no item blocks are found at all
    (§19 -- a structurally malformed physical response is a shared,
    whole-generation failure, not specific to any one member); `member_id`
    does not appear in the response, or appears more than once; or its
    block is missing a valid `ACP-MODE` line.
    """
    matches = list(_ITEM_HEADER_RE.finditer(text))
    if not matches:
        raise QueueProtocolViolation("no ACP-QUEUE-ITEM blocks found in response")

    own_blocks: list[str] = []
    for index, match in enumerate(matches):
        if match.group("id").strip() != member_id:
            continue
        block_start = match.end()
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        own_blocks.append(text[block_start:block_end].lstrip("\n"))

    if not own_blocks:
        raise QueueProtocolViolation(
            f"no ACP-QUEUE-ITEM block found for member id {member_id!r}")
    if len(own_blocks) > 1:
        raise QueueProtocolViolation(
            f"duplicate ACP-QUEUE-ITEM blocks for member id {member_id!r}")

    first_line, _, remainder = own_blocks[0].partition("\n")
    mode = _MODE_LINES.get(first_line.strip())
    if mode is None:
        raise QueueProtocolViolation(
            f"item {member_id!r}: missing or invalid ACP-MODE line, "
            f"got {first_line.strip()!r}"
        )
    if remainder.startswith("\n"):
        remainder = remainder[1:]
    return QueueMemberResult(member_id=member_id, mode=mode, output=remainder.rstrip("\n"))
