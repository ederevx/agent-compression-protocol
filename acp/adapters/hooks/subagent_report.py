#!/usr/bin/env python3
"""SubagentStart hook: the cooperative worker-report pattern
(agent_protocols_v1 background-compression adjustment §29).

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

This hook previously also fired a best-effort background `context.
prepare` cache warm on `SubagentStop`. Removed: nothing could ever
install that prepared result back into a receiver's live context short
of a proxy intercepting outbound API traffic, which was evaluated and
declined (see STATUS.md, "bounded-context reclamation," declined
2026-09-03) -- so the cache warm could pay off only on the rare
coincidence of the exact same content later reaching `evaluate()` and
hitting ACP's content-hash coalescing before expiring, never as a real,
documented compression point. See `acp/coordinator.py`'s module
docstring for the full removal rationale.

Fails open (silent no-op) on any ACP error.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

_REPORT_INSTRUCTION = (
    "ACP is available in this session as MCP tool `report` (server "
    "`acp-bash`). If your final report to the parent, or a message to a "
    "teammate via SendMessage, would otherwise be large, call `report` "
    "with that text first and return/send its compressed output instead "
    "of the raw text."
)


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


def main() -> int:
    parse_args()  # --agent is still passed by every host's hook command line
    event = read_event()
    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")

    if event_name == "SubagentStart":
        return handle_start()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
