"""EXPERIMENTAL (branch `experimental/model-side-output-split`, not wired
into `compressor.py`/`aalp_client.py`): alternative to `acp/queue_codec.py`'s
regex-based `ACP-QUEUE/1` response grammar.

Idea tested: instead of ACP defining a textual per-item delimiter format
(`ACP-QUEUE-ITEM: <id>` / `ACP-MODE: ...`) and parsing it back apart itself
with a regex over the whole response text, ask the provider model to
perform the output split itself by emitting the whole multi-member
response as structured JSON, and have ACP do a straight `json.loads` +
lookup instead of regex block-boundary detection.

Request-side member framing is unchanged (`ACP-QUEUE-ITEM: <id>\\n
<content>`, still mechanically joined by AALP, which never interprets it)
-- only the *response* grammar and its parser differ here.

This module originally compared five parser variants and three prompt-
construction styles side by side (id-keyed dict vs. array wire shape;
naive vs. hardened vs. lenient-search parsing; prose-only vs. literal-
skeleton vs. skeleton-with-anchor-definitions prompts) against the same
contamination/robustness properties `test_queue_contamination.py`
established for the shipped textual grammar. That comparison is closed;
only the winning combination survives here:

- Wire shape + parser: `parse_queue_member_result_json_array`, array-of-
  entries (`[{"id": ..., "mode": ..., "output": ...}, ...]`). Rejects
  duplicate `id` values across entries (checked explicitly -- the array
  shape gets no free duplicate-key detection the way a JSON object would
  from `object_pairs_hook`), rejects unexpected extra per-entry fields,
  tolerates exactly one markdown code-fence wrapper (a real, observed
  model behavior, not a protocol feature). Preferred over the id-keyed-
  object shape because it is the only shape a full, literal JSON Schema
  can describe with no workaround -- the object shape's keys are runtime
  member ids unknown at schema-authoring time.
- Prompt construction: `QUEUE_JSON_SKELETON_REF_ARRAY_ADDENDUM` -- a
  literal JSON skeleton embedded in the instructions, using named anchor
  tokens ("<MODE>", "<OUTPUT>") for the two semantically loaded fields
  instead of one under-specified example value, each resolved by a
  matching "Definitions:" block immediately below the skeleton (`$ref`/
  `$defs`-style indirection, expressed as plain prompt text since the
  addendum is only ever prompt content).
- Enforcement: `build_queue_response_format()` wraps
  `ARRAY_RESPONSE_JSON_SCHEMA` in the OpenAI-compatible `response_format`
  convention, for a backend with constrained-decoding support. Defense in
  depth only -- not a substitute for the parser's own duplicate-id check,
  since a schema validator cannot see a duplicate id that already
  survived (or a duplicate key already destroyed by) `json.loads`.

Every property above was verified structurally/adversarially against
fixtures, not against real model behavior -- promotion past this
prototype requires the same separately-authorized live-activation pass
every other stage of this project is gated behind.
"""
from __future__ import annotations

import json
import re

from acp.queue_codec import QUEUE_PROTOCOL_NAME, QueueMemberResult, QueueProtocolViolation

_ALLOWED_MODES = {"PASS", "COMPACT", "COMPRESS"}
_ARRAY_ENTRY_KEYS = frozenset({"id", "mode", "output"})

# Real models routinely wrap JSON output in a markdown code fence even when
# explicitly told not to -- a practical risk this experiment found, not a
# hypothetical one. Strips at most one leading/trailing fence pair; does
# nothing if the text isn't fenced, so it's a no-op safety net rather than
# a behavior change for already-compliant responses. Anchored to the whole
# string (not a search) -- anything other than pure whitespace outside the
# fence is left alone, so a subsequent strict `json.loads` on the result
# still rejects prose-wrapped JSON instead of silently accepting it.
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_markdown_fence(text: str) -> str:
    match = _FENCE_RE.match(text)
    return match.group(1) if match else text


QUEUE_JSON_SKELETON_REF_ARRAY_ADDENDUM = f"""This request follows an experimental {QUEUE_PROTOCOL_NAME}-JSON variant: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request.

Respond with exactly one JSON array matching this exact shape, and nothing else -- no prose, no markdown fences, no text before or after it:

[
  {{"id": "<the id from that item's own ACP-QUEUE-ITEM line>", "mode": "<MODE>", "output": "<OUTPUT>"}},
  {{"id": "<repeat one entry per submitted item, using each item's own real id>", "mode": "<MODE>", "output": "<OUTPUT>"}}
]

"<MODE>" and "<OUTPUT>" above are pointers, not literal values -- each must be replaced with a real value as defined below, matched by the same tag name. The id placeholders follow the same rule: substitute each with that item's own real id, never the placeholder text itself.

Definitions:
  <MODE> -- a JSON string, exactly one of "PASS", "COMPACT", or "COMPRESS" (case-sensitive, no other values):
    - "PASS": the item's content, completely unchanged.
    - "COMPACT": a shortened paraphrase that preserves every fact and decision.
    - "COMPRESS": a maximally-terse, lossy summary; some detail loss is expected.
  <OUTPUT> -- a JSON string: empty for "PASS", the transformed content otherwise.

None of the tag strings "<MODE>", "<OUTPUT>", or the id placeholder text may appear literally in your response -- every one of them must be replaced. Exactly one array entry per submitted item id, no more, no fewer, no duplicates, no invented ids."""


# A real JSON Schema (draft 2020-12 vocabulary, but deliberately using only
# widely-supported keywords) for the array shape -- usable as the `schema`
# field of an OpenAI-compatible `response_format: {"type": "json_schema",
# "json_schema": {"name": ..., "schema": ARRAY_RESPONSE_JSON_SCHEMA,
# "strict": True}}` request field, for a backend that actually enforces it
# via constrained decoding rather than merely reading it as a hint.
#
# Duplicate-id detection is NOT expressible here -- no built-in JSON
# Schema keyword expresses "unique by one field of an object" across array
# items -- so `parse_queue_member_result_json_array`'s own explicit check
# stays the real defense regardless of whether a backend enforces this
# schema. See test_queue_codec_json_experiment.py::JsonSchemaInjectionTest.
ARRAY_RESPONSE_JSON_SCHEMA: dict = {
    "type": "array",
    "minItems": 1,
    "items": {
        "type": "object",
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "mode": {"type": "string", "enum": ["PASS", "COMPACT", "COMPRESS"]},
            "output": {"type": "string"},
        },
        "required": ["id", "mode", "output"],
        "additionalProperties": False,
    },
}


def build_queue_response_format(*, strict: bool = True) -> dict:
    """OpenAI-compatible `response_format` payload wrapping
    `ARRAY_RESPONSE_JSON_SCHEMA` -- attach to the physical request body's
    top level (a sibling of `model`/`messages`/`max_tokens`) for a backend
    that supports constrained JSON-schema decoding. AALP forwards this
    opaquely either way (`request_shape.passthrough: true`); a backend
    that doesn't recognize the field is expected to ignore it, per that
    same API convention's own documented behavior -- not verified here
    against the real `ci` backend, which stays gated behind live-
    activation."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "acp_queue_response_v1",
            "schema": ARRAY_RESPONSE_JSON_SCHEMA,
            "strict": strict,
        },
    }


def _validate_fields(member_id: str, entry: dict) -> QueueMemberResult:
    extra = set(entry.keys()) - _ARRAY_ENTRY_KEYS
    if extra:
        raise QueueProtocolViolation(
            f"item {member_id!r}: unexpected field(s) {sorted(extra)!r}")
    mode = entry.get("mode")
    if mode not in _ALLOWED_MODES:
        raise QueueProtocolViolation(
            f"item {member_id!r}: missing or invalid mode, got {mode!r}")
    output = entry.get("output")
    if not isinstance(output, str):
        raise QueueProtocolViolation(
            f"item {member_id!r}: 'output' must be a string, got {type(output).__name__}")
    return QueueMemberResult(member_id=member_id, mode=mode, output=output)


def parse_queue_member_result_json_array(text: str, member_id: str) -> QueueMemberResult:
    """Array wire shape: `[{"id": ..., "mode": ..., "output": ...}]`.
    Rejects duplicate `id` values across array entries (checked
    explicitly -- the array shape has no free duplicate-key detection the
    way a JSON object gets from `object_pairs_hook`), rejects unexpected
    extra per-entry fields, and tolerates exactly one markdown code-fence
    wrapper. See module docstring."""
    text = _strip_markdown_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueueProtocolViolation(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise QueueProtocolViolation(
            f"response JSON must be an array of entries, got {type(data).__name__}")

    seen_ids: set[str] = set()
    own_entry: dict | None = None
    for item in data:
        if not isinstance(item, dict):
            raise QueueProtocolViolation(
                f"array entry must be an object, got {type(item).__name__}")
        item_id = item.get("id")
        if not isinstance(item_id, str):
            raise QueueProtocolViolation(
                f"array entry 'id' must be a string, got {type(item_id).__name__}")
        if item_id in seen_ids:
            raise QueueProtocolViolation(f"duplicate id {item_id!r} in queue response array")
        seen_ids.add(item_id)
        if item_id == member_id:
            own_entry = item

    if own_entry is None:
        raise QueueProtocolViolation(f"no entry found for member id {member_id!r}")
    return _validate_fields(member_id, own_entry)
