"""EXPERIMENTAL (branch `experimental/model-side-output-split`, not wired
into `compressor.py`/`aalp_client.py`): alternatives to `acp/queue_codec.py`'s
regex-based `ACP-QUEUE/1` response grammar.

Idea being tested: instead of ACP defining a textual per-item delimiter
format (`ACP-QUEUE-ITEM: <id>` / `ACP-MODE: ...`) and parsing it back apart
itself with a regex over the whole response text, ask the provider model to
perform the output split itself by emitting the whole multi-member response
as structured JSON, and have ACP do a straight `json.loads` + lookup
instead of regex block-boundary detection.

Request-side member framing is unchanged (`ACP-QUEUE-ITEM: <id>\\n
<content>`, still mechanically joined by AALP, which never interprets it)
-- only the *response* grammar and its parser(s) differ here.

Five parser variants, deliberately kept side by side for comparison rather
than collapsed into one "final" implementation:

`parse_queue_member_result_json_naive` -- id-keyed JSON object, plain
`json.loads`. Structural JSON escaping means a member's own `output`
string can never be reinterpreted as a delimiter or another member's
entry (queue_codec.py's textual grammar's documented weak point, see
tests/test_queue_contamination.py, is eliminated for free). But Python's
`json.loads` (like most JSON parsers, per the JSON spec, which does not
forbid duplicate object keys) silently keeps only the *last* of two
duplicate top-level keys -- a real regression versus
`queue_codec.parse_queue_member_result`'s explicit duplicate-id rejection.

`parse_queue_member_result_json` -- hardened id-keyed variant: rejects
duplicate top-level keys (`object_pairs_hook`), rejects unexpected extra
fields per entry (schema drift / hidden-field defense-in-depth -- ignored
extra fields are not actually returned to the caller today, but silently
accepting them invites a future refactor that reads one without deciding
it's meant to be trusted), and tolerates exactly one markdown code-fence
wrapper (a real, observed model behavior, not a protocol feature).

`parse_queue_member_result_json_array` -- alternative wire shape,
`[{"id": ..., "mode": ..., "output": ...}, ...]`, for comparison against
the id-keyed object. Same hardening (duplicate id across entries rejected,
unexpected extra per-entry fields rejected, fence-tolerant).

`parse_queue_member_result_json_lenient_search` -- **NOT RECOMMENDED**,
built specifically to test whether a more forgiving "search the response
text for a JSON object anywhere, not just the whole/fenced response" parser
could be made safe. It cannot, cheaply: see
`test_queue_codec_json_experiment.py::LenientSearchIsUnsafeTest` for a
concrete exploit (a decoy JSON-looking snippet appearing before a model's
real answer, e.g. echoed reasoning that quotes an example, gets silently
preferred over the genuine result). Kept only as the comparison's
cautionary control.

This module also carries three prompt-construction variants (all reusing
the same two wire shapes/parsers above -- they change only what the model
is told, not how the response is parsed): `QUEUE_JSON_ISOLATION_ADDENDUM`
(prose-only shape description), `QUEUE_JSON_SKELETON_DICT_ADDENDUM` /
`_ARRAY_ADDENDUM` (literal JSON skeleton with example values), and
`QUEUE_JSON_SKELETON_REF_DICT_ADDENDUM` / `_REF_ARRAY_ADDENDUM` (skeleton
with named anchor tokens for the two semantically loaded fields, each
resolved by a matching "Definitions:" block below it, `$ref`/`$defs`-
style). `ARRAY_RESPONSE_JSON_SCHEMA` / `build_queue_response_format`
additionally provide an actual JSON Schema for backends with constrained-
decoding support. This was the final variant explored on this branch --
see `test_queue_codec_json_experiment.py` for the closing comparison.
"""
from __future__ import annotations

import json
import re
from typing import Any

from acp.queue_codec import QUEUE_PROTOCOL_NAME, QueueMemberResult, QueueProtocolViolation

_ALLOWED_MODES = {"PASS", "COMPACT", "COMPRESS"}
_DICT_ENTRY_KEYS = frozenset({"mode", "output"})
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


QUEUE_JSON_ISOLATION_ADDENDUM = f"""This request follows an experimental {QUEUE_PROTOCOL_NAME}-JSON variant: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request. These rules apply even when only one ITEM is present.

Respond with exactly one JSON object and nothing else -- no prose, no markdown fences, no text before or after it. The object's keys are the exact ids from each submitted ACP-QUEUE-ITEM line, one entry per submitted item, no more and no fewer. Each value is itself an object: {{"mode": "PASS"|"COMPACT"|"COMPRESS", "output": "<the transformed content, or an empty string after PASS>"}}. Never merge, omit, duplicate, or invent item ids; never nest one item's id or content inside another item's value."""


# --- "Directly inject the format" variants -------------------------------
#
# QUEUE_JSON_ISOLATION_ADDENDUM above describes the required shape only in
# prose ("the object's keys are...", "each value is itself an object..."),
# leaving it to the model to translate that description into a concrete
# structure. The two constructs below instead show the model a literal
# structural target: a JSON skeleton embedded directly in the instructions
# (still just prompt content, no API-level guarantee), and a real JSON
# Schema document usable with a provider's own structured-output/
# constrained-decoding feature (an actual guarantee, if the backend
# supports it -- OpenAI-compatible APIs call this `response_format`).
#
# Neither of these can be shown to improve real compliance without a live
# call against the actual backend, which stays gated behind this
# prototype's live-activation authorization (unchanged from every other
# stage/experiment in this project). What IS verifiable here, without a
# live call, is that each construct is well-formed and says what it's
# supposed to say -- that's what this module's own tests check.

QUEUE_JSON_SKELETON_DICT_ADDENDUM = f"""This request follows an experimental {QUEUE_PROTOCOL_NAME}-JSON variant: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request.

Respond with exactly one JSON object matching this exact shape, and nothing else -- no prose, no markdown fences, no text before or after it:

{{
  "<the id from that item's own ACP-QUEUE-ITEM line>": {{"mode": "PASS", "output": ""}},
  "<repeat one entry per submitted item, using each item's own real id>": {{"mode": "COMPACT", "output": "<transformed content>"}}
}}

Substitute each "<...>" placeholder with the real value for that item -- never emit the literal placeholder text. "mode" must be exactly one of "PASS", "COMPACT", or "COMPRESS" (case-sensitive, no other values). "output" must be a string: empty for PASS, the transformed content otherwise. Exactly one entry per submitted item id, no more, no fewer, no duplicates, no invented ids."""

QUEUE_JSON_SKELETON_ARRAY_ADDENDUM = f"""This request follows an experimental {QUEUE_PROTOCOL_NAME}-JSON variant: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request.

Respond with exactly one JSON array matching this exact shape, and nothing else -- no prose, no markdown fences, no text before or after it:

[
  {{"id": "<the id from that item's own ACP-QUEUE-ITEM line>", "mode": "PASS", "output": ""}},
  {{"id": "<repeat one entry per submitted item, using each item's own real id>", "mode": "COMPACT", "output": "<transformed content>"}}
]

Substitute each "<...>" placeholder with the real value for that item -- never emit the literal placeholder text. "mode" must be exactly one of "PASS", "COMPACT", or "COMPRESS" (case-sensitive, no other values). "output" must be a string: empty for PASS, the transformed content otherwise. Exactly one array entry per submitted item id, no more, no fewer, no duplicates, no invented ids."""


# A real JSON Schema (draft 2020-12 vocabulary, but deliberately using only
# widely-supported keywords) for the array shape -- usable as the `schema`
# field of an OpenAI-compatible `response_format: {"type": "json_schema",
# "json_schema": {"name": ..., "schema": ARRAY_RESPONSE_JSON_SCHEMA,
# "strict": True}}` request field, for a backend that actually enforces it
# via constrained decoding rather than merely reading it as a hint.
#
# The dict (id-keyed-object) shape's *value* shape can still be
# schema-constrained even though its keys are runtime member ids unknown
# at schema-authoring time (`additionalProperties: {<entry schema>}`
# applies that entry schema to every key, whatever the key names turn out
# to be) -- so per-entry type strictness is not actually the dict shape's
# weak point versus a schema. Its real, irreducible weak point is
# duplicate-key detection specifically: by the time any JSON Schema
# validator (including a provider's own constrained-decoding pass, and
# `jsonschema` package validation in this repo's own tests) ever sees the
# data, standard `json.loads` has *already* silently resolved a duplicate
# key to its last value -- the information that a duplicate ever existed
# is gone before schema validation can run at all. Only a custom
# `object_pairs_hook` (this module's `_reject_duplicate_keys`, used by
# `parse_queue_member_result_json`) sees the raw key-value pairs in time
# to catch it; no schema, provider-side or otherwise, can substitute for
# that check. The array shape's duplicate-*id* problem is different: two
# entries with the same "id" both survive parsing intact as distinct list
# items, so it -- unlike the dict shape's problem -- is at least visible
# post-parse, though the schema below still doesn't check for it (no
# built-in JSON Schema keyword expresses "unique by one field of an
# object" across array items); it is the caller's own
# `parse_queue_member_result_json_array`, not schema validation, that
# performs the check.  See
# test_queue_codec_json_experiment.py::JsonSchemaInjectionTest.
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


# --- Hybrid: skeleton anchors + matching definitions ----------------------
#
# The skeleton addenda above show one literal example value per field
# ("mode": "PASS", "output": "<transformed content>") -- adequate for
# structural facts (object nesting, key names, array-vs-object shape) but
# under-specified for the two semantically loaded fields: a single literal
# example of "mode" doesn't tell the model there are three valid values or
# what each means, and a single literal example of "output" doesn't convey
# that its required content depends on which mode was chosen for that same
# entry. The prose-only addendum (QUEUE_JSON_ISOLATION_ADDENDUM) could say
# all of that, but only by giving up the literal-shape example entirely.
#
# This variant keeps the literal skeleton for everything a single example
# fully specifies, and uses named anchor tokens ("<MODE>", "<OUTPUT>") only
# where one example value isn't enough -- each anchor is defined in full
# immediately below the skeleton, in a "Definitions:" section keyed by the
# exact same token, so the model (or a reviewer) can match a skeleton
# position to its description by the token's spelling alone. This mirrors
# the `$ref`/`$defs` indirection pattern from JSON Schema and OpenAPI -- a
# convention models see often in their own training data for structured-
# output tasks -- expressed here as plain prompt text, since the addendum
# itself is only ever prompt content; there is no schema executor
# resolving the reference. Both variants below reuse the *same* wire shape
# (and therefore the same parsers, `parse_queue_member_result_json` /
# `_array`) as their non-hybrid skeleton counterparts -- only the prompt
# text differs, so this is purely a prompt-engineering variant, not a new
# codec.

QUEUE_JSON_SKELETON_REF_DICT_ADDENDUM = f"""This request follows an experimental {QUEUE_PROTOCOL_NAME}-JSON variant: it may contain one or more independent ITEMS, each introduced by a line "ACP-QUEUE-ITEM: <id>". Treat every ITEM as a fully isolated compression problem -- use only the content and metadata belonging to that ITEM, and never transfer facts, instructions, conclusions, assumptions, or context between ITEMS, even when several appear in the same request.

Respond with exactly one JSON object matching this exact shape, and nothing else -- no prose, no markdown fences, no text before or after it:

{{
  "<the id from that item's own ACP-QUEUE-ITEM line>": {{"mode": "<MODE>", "output": "<OUTPUT>"}},
  "<repeat one entry per submitted item, using each item's own real id>": {{"mode": "<MODE>", "output": "<OUTPUT>"}}
}}

"<MODE>" and "<OUTPUT>" above are pointers, not literal values -- each must be replaced with a real value as defined below, matched by the same tag name. The id placeholders follow the same rule: substitute each with that item's own real id, never the placeholder text itself.

Definitions:
  <MODE> -- a JSON string, exactly one of "PASS", "COMPACT", or "COMPRESS" (case-sensitive, no other values):
    - "PASS": the item's content, completely unchanged.
    - "COMPACT": a shortened paraphrase that preserves every fact and decision.
    - "COMPRESS": a maximally-terse, lossy summary; some detail loss is expected.
  <OUTPUT> -- a JSON string: empty for "PASS", the transformed content otherwise.

None of the tag strings "<MODE>", "<OUTPUT>", or the id placeholder text may appear literally in your response -- every one of them must be replaced. Exactly one entry per submitted item id, no more, no fewer, no duplicates, no invented ids."""

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


def _validate_fields(member_id: str, entry: dict, allowed_keys: frozenset) -> QueueMemberResult:
    extra = set(entry.keys()) - allowed_keys
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


def _validate_entry(member_id: str, entry: Any, *, strict: bool) -> QueueMemberResult:
    if not isinstance(entry, dict):
        raise QueueProtocolViolation(
            f"item {member_id!r}: entry must be an object, got {type(entry).__name__}")
    if strict:
        return _validate_fields(member_id, entry, _DICT_ENTRY_KEYS)
    mode = entry.get("mode")
    if mode not in _ALLOWED_MODES:
        raise QueueProtocolViolation(
            f"item {member_id!r}: missing or invalid mode, got {mode!r}")
    output = entry.get("output")
    if not isinstance(output, str):
        raise QueueProtocolViolation(
            f"item {member_id!r}: 'output' must be a string, got {type(output).__name__}")
    return QueueMemberResult(member_id=member_id, mode=mode, output=output)


def parse_queue_member_result_json_naive(text: str, member_id: str) -> QueueMemberResult:
    """Plain `json.loads`, id-keyed object -- see module docstring for the
    duplicate-key caveat this variant does NOT defend against."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise QueueProtocolViolation(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QueueProtocolViolation(
            f"response JSON must be an object keyed by member id, got {type(data).__name__}")
    entry = data.get(member_id)
    if entry is None:
        raise QueueProtocolViolation(f"no entry found for member id {member_id!r}")
    return _validate_entry(member_id, entry, strict=False)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise QueueProtocolViolation(
                f"duplicate top-level key {key!r} in queue response JSON")
        seen[key] = value
    return seen


def parse_queue_member_result_json(text: str, member_id: str) -> QueueMemberResult:
    """Hardened id-keyed variant: rejects duplicate top-level keys,
    rejects unexpected extra per-entry fields, tolerates exactly one
    markdown code-fence wrapper. See module docstring."""
    text = _strip_markdown_fence(text)
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except QueueProtocolViolation:
        raise
    except json.JSONDecodeError as exc:
        raise QueueProtocolViolation(f"response is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QueueProtocolViolation(
            f"response JSON must be an object keyed by member id, got {type(data).__name__}")
    entry = data.get(member_id)
    if entry is None:
        raise QueueProtocolViolation(f"no entry found for member id {member_id!r}")
    return _validate_entry(member_id, entry, strict=True)


def parse_queue_member_result_json_array(text: str, member_id: str) -> QueueMemberResult:
    """Alternative wire shape: `[{"id": ..., "mode": ..., "output": ...}]`.
    Hardened the same way as the id-keyed variant: duplicate `id` values
    across array entries are rejected (the array shape has no free
    duplicate-key detection from `object_pairs_hook` the way a JSON object
    does -- this has to be checked explicitly instead), unexpected extra
    per-entry fields are rejected, and exactly one markdown code-fence
    wrapper is tolerated."""
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
    return _validate_fields(member_id, own_entry, _ARRAY_ENTRY_KEYS)


def _extract_first_balanced_json_object(text: str) -> str | None:
    """Finds the first top-level `{...}` substring anywhere in `text` by
    brace-depth counting (a common naive real-world heuristic for
    tolerating non-compliant LLM output that mixes prose and JSON).
    Deliberately used only by the lenient variant below, which this
    experiment does not recommend."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return None


def parse_queue_member_result_json_lenient_search(text: str, member_id: str) -> QueueMemberResult:
    """NOT RECOMMENDED. Extracts the first balanced `{...}` substring found
    anywhere in the response and parses that, instead of requiring the
    whole (optionally fenced) response to be exactly one JSON object. Built
    and kept specifically to demonstrate why this is unsafe for this use
    case -- see `test_queue_codec_json_experiment.py::
    LenientSearchIsUnsafeTest` for the concrete exploit."""
    candidate = _extract_first_balanced_json_object(text)
    if candidate is None:
        raise QueueProtocolViolation("no JSON object found anywhere in response")
    try:
        data = json.loads(candidate, object_pairs_hook=_reject_duplicate_keys)
    except QueueProtocolViolation:
        raise
    except json.JSONDecodeError as exc:
        raise QueueProtocolViolation(f"extracted candidate is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise QueueProtocolViolation("extracted JSON is not an object")
    entry = data.get(member_id)
    if entry is None:
        raise QueueProtocolViolation(f"no entry found for member id {member_id!r}")
    return _validate_entry(member_id, entry, strict=True)


# --- Closing recommendation (2026-09-04) -----------------------------------
#
# Comparing every variant built on this branch against the same
# contamination/robustness properties `test_queue_contamination.py`
# established for the shipped `queue_codec.py` textual grammar:
#
#   naive dict     -- duplicate top-level key silently overwritten (last
#                     write wins); escapes the textual grammar's
#                     truncation-only limitation for free (JSON string
#                     escaping means embedded fake headers can never be
#                     reinterpreted); NOT SAFE, ruled out.
#   hardened dict  -- duplicate key rejected via `object_pairs_hook`;
#                     escapes truncation for free; per-entry values are
#                     schema-constrainable via `additionalProperties`, but
#                     the duplicate-key defense is invisible to any schema
#                     validator regardless (destroyed by `json.loads`
#                     before validation ever runs) -- schema support adds
#                     nothing this shape's own parser doesn't already do.
#   hardened array -- duplicate id rejected via an explicit post-parse
#                     check (no free protection the way `object_pairs_hook`
#                     gives the dict shape); escapes truncation for free;
#                     the ONLY shape a full, literal JSON Schema can be
#                     written against with no workaround, since array
#                     items have a fixed shape unlike the dict shape's
#                     runtime-id keys.
#   lenient-search -- exploitable by a decoy JSON-looking snippet earlier
#                     in the response; NOT SAFE, ruled out, kept only as a
#                     cautionary control.
#
# Recommended combination for this experiment -- the one to carry into any
# future live-activation test (still separately gated; not performed on
# this branch):
#   - Wire shape + parser: `parse_queue_member_result_json_array` -- the
#     array shape's lack of "free" duplicate protection is not actually a
#     cost difference (the dict shape's protection is itself just a custom
#     hook, not zero-effort), and its full schema-describability is a real
#     advantage the dict shape cannot match without a workaround.
#   - Prompt construction: `QUEUE_JSON_SKELETON_REF_ARRAY_ADDENDUM` --
#     keeps the literal skeleton's structural clarity while adding full
#     "mode"/"output" semantics via matched anchors, avoiding both the
#     plain skeleton's under-specification and prose-only's verbosity.
#   - Enforcement: `build_queue_response_format()` (wrapping
#     `ARRAY_RESPONSE_JSON_SCHEMA`) attached wherever a live backend
#     advertises constrained-decoding support, as defense in depth -- NOT
#     a substitute for `parse_queue_member_result_json_array`'s own
#     duplicate-id check, which stays regardless of schema support.
#
# This is a design recommendation, not a decision to replace the shipped
# `queue_codec.py` grammar in `compressor.py`/`aalp_client.py`. Every
# property compared above is structural/adversarial against fixtures, not
# evidence of real model behavior -- promotion past this prototype needs
# the same separately-authorized live-activation pass every other stage of
# this project is gated behind, run specifically against this combination.

RECOMMENDED_PARSER = parse_queue_member_result_json_array
RECOMMENDED_ADDENDUM = QUEUE_JSON_SKELETON_REF_ARRAY_ADDENDUM
RECOMMENDED_RESPONSE_FORMAT = build_queue_response_format
