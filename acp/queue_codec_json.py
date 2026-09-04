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
