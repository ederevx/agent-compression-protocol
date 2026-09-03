#!/usr/bin/env python3
"""SubagentStart/SubagentStop hook: the cooperative worker-report pattern
(agent_protocols_v1 background-compression adjustment §29) plus a
best-effort proactive-compaction cache warm.

Neither host lets a hook mutate what a parent sees after a subagent
finishes (Claude's `SubagentStop` input has no report-content field at
all; Codex's has `last_assistant_message`, but `SubagentStop` is still
observational-only for continuation, not context injection) -- so
"child -> parent report filtering" and "subagent -> subagent handoff
filtering" cannot be host-intercepted. On `SubagentStart`, this hook
instead asks the subagent, cooperatively, to compress its own large
final report (or large inter-teammate message) itself, via the `report`
MCP tool (`acp/adapters/claude_code_bash_mcp.py`) before returning/
sending it.

On `SubagentStop`, this hook also fires a best-effort background
`context.prepare` over the subagent's transcript tail, purely as a cache
warm: no code path here (or elsewhere) calls `context.resolve` on this
job. It only pays off if identical content resurfaces through a later
`evaluate()`/`prepare()` call and hits ACP's own content-hash
coalescing -- this is not a guaranteed compression point, and must never
be documented as one (agent_protocols_v1 background-compression
adjustment §14: a prepared result becomes model-visible only when some
later caller reaches a safe boundary, never installed automatically).

Fails open (silent no-op) on any ACP error or missing/undersized
transcript data.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from acp.adapters.acp_client import AcpClient

_HOST_BY_AGENT = {"claude": "claude-code", "codex": "codex-cli"}

_REPORT_INSTRUCTION = (
    "ACP is available in this session as MCP tool `report` (server "
    "`acp-bash`). If your final report to the parent, or a message to a "
    "teammate via SendMessage, would otherwise be large, call `report` "
    "with that text first and return/send its compressed output instead "
    "of the raw text."
)

# A rough prewarm heuristic, not a policy boundary -- acp/gate.py's own
# thresholds are what actually decide PASS/COMPACT/COMPRESS on whatever
# later call (if any) evaluates this content for real.
_PREWARM_CHAR_THRESHOLD = 24_000


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


def receiver_of(agent: str, event: dict[str, Any]) -> dict[str, str | None]:
    session_id = str(event.get("session_id") or "unknown")
    agent_id = event.get("agent_id")
    return {
        "host": _HOST_BY_AGENT[agent],
        "session_id": session_id,
        "agent_id": agent_id if isinstance(agent_id, str) else None,
    }


def _assistant_text_from_transcript_line(line: str) -> str | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = record.get("message") if isinstance(record, dict) else None
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content or None
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return text or None
    return None


def transcript_tail(agent: str, event: dict[str, Any]) -> str | None:
    """The subagent's own final message text, host-appropriate.

    Codex's `SubagentStop` input carries `last_assistant_message`
    directly. Claude's carries no report field at all, only
    `transcript_path` -- so the last assistant-role message is read back
    out of that transcript file instead."""
    if agent == "codex":
        text = event.get("last_assistant_message")
        return text if isinstance(text, str) and text else None

    transcript_path = event.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        lines = Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        text = _assistant_text_from_transcript_line(line)
        if text:
            return text
    return None


def handle_start() -> int:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SubagentStart",
                "additionalContext": _REPORT_INSTRUCTION,
            }
        },
        sys.stdout, separators=(",", ":"),
    )
    return 0


def handle_stop(agent: str, event: dict[str, Any], acp_root: str) -> int:
    tail = transcript_tail(agent, event)
    if not tail or len(tail) < _PREWARM_CHAR_THRESHOLD:
        return 0
    try:
        AcpClient(acp_root).prepare(
            tail, "native_agent_report", receiver_of(agent, event),
        )
    except Exception:
        pass
    return 0


def main() -> int:
    args = parse_args()
    event = read_event()
    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")

    if event_name == "SubagentStart":
        return handle_start()

    if event_name == "SubagentStop":
        acp_root = os.environ.get("ACP_HOME")
        if not acp_root:
            return 0
        return handle_stop(args.agent, event, acp_root)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
