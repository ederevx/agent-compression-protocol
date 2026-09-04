#!/usr/bin/env python3
"""PreToolUse hook matched to the `SendMessage` tool (inter-agent
messaging -- peer/teammate messages, cross-session sends, and replies to
`main`), implementing the `SendMessage` analogue of "oversized support
context" compression (Phase 4 follow-on, item C7).

Same discipline as `parent_child_context.py` (read that module's
docstring first -- this one only restates what differs): only ever
touches an explicitly delimited `<acp-context>...</acp-context>` block
inside the outgoing `message` text, never the message as a whole. That
delimiter convention exists precisely so a hook never has to guess where
a caller's actual instruction/communication ends and merely-supporting
material begins -- it only acts on what the caller deliberately marked
as compressible. A `message` with no such block is a silent no-op.

Traffic class: `general`, not `downward_context`. `downward_context`'s
policy (see `acp/gate.py`) is documented as parent -> child spawn
context specifically, and its conservative thresholds are justified by
that direction ("preserving the task instruction matters more here").
`SendMessage` has no fixed direction -- it carries peer-to-peer,
child-to-parent, and parent-to-child traffic all through the same tool,
and nothing in the tool input reliably distinguishes those cases. It is
also not a report flowing back up from a finished subagent, so
`native_agent_report` (already used for the cooperative `report` MCP
tool and the `SubagentStop` backstop in `subagent_report.py`) does not
fit either. `general` is ACP's own catch-all for exactly this case --
content whose relationship/direction doesn't match either specialized
policy -- and its thresholds sit between the other two. This is a
judgment call, not an empirically-forced conclusion; flagged as such in
the C7 report.

`SendMessage`'s `message` parameter is a union: plain text, or a
structured `shutdown_request`/`shutdown_response`/`plan_approval_response`
object (see the tool's own schema). Only the plain-text-string shape is
ever touched; a structured `message` is left completely alone (no
delimited-block convention applies to those, and mutating protocol
control fields is out of scope here).

Whether `PreToolUse`'s `updatedInput` actually applies to `SendMessage`
must be empirically confirmed before this hook is wired into a live
install -- same caveat as `parent_child_context.py`. Codex's `SendMessage`
tool (if/when one exists there) has not been tested either way -- unlike
`spawn_agent`, which was empirically *rejected* for Codex (see the D1
finding in the v1.1 evaluation doc) -- so this stays Claude-only by
default until Codex support is actually checked, not because Codex is
known to reject it.

Fails open (no `updatedInput` emitted, i.e. the original input goes
through unchanged) on any ACP error, missing block, or a block ACP
itself decides not to shrink (PASS).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

from acp.adapters.acp_client import AcpClient

_HOST_BY_AGENT = {"claude": "claude-code", "codex": "codex-cli"}
_MESSAGE_FIELD = "message"
_CONTEXT_BLOCK_RE = re.compile(r"<acp-context>(.*?)</acp-context>", re.DOTALL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", choices=("claude", "codex"), required=True)
    return parser.parse_args()


def read_event() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def compress_message(
    client: AcpClient, agent: str, session_id: str, message: str,
) -> str | None:
    """The new message text if a delimited block was found and ACP
    shrank it; `None` if there is nothing to do (no block, ACP
    unreachable, or ACP chose PASS/returned the block unchanged)."""
    match = _CONTEXT_BLOCK_RE.search(message)
    if match is None:
        return None
    block = match.group(1)

    receiver = {"host": _HOST_BY_AGENT[agent], "session_id": session_id, "agent_id": None}
    try:
        result = client.evaluate(block, "general", receiver)
    except Exception:
        return None
    if not result.ok or result.output is None or result.output == block:
        return None

    return message[: match.start()] + result.output + message[match.end():]


def main() -> int:
    args = parse_args()
    acp_root = os.environ.get("ACP_HOME")
    event = read_event()

    tool_input = event.get("tool_input")
    if not acp_root or not isinstance(tool_input, dict):
        return 0

    message = tool_input.get(_MESSAGE_FIELD)
    if not isinstance(message, str):
        return 0  # structured (shutdown/plan-approval) message: leave untouched

    session_id = str(event.get("session_id") or "unknown")
    new_message = compress_message(AcpClient(acp_root), args.agent, session_id, message)
    if new_message is None:
        return 0

    new_input = dict(tool_input)
    new_input[_MESSAGE_FIELD] = new_message
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": new_input,
            }
        },
        sys.stdout, separators=(",", ":"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
