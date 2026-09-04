#!/usr/bin/env python3
"""SubagentStart hook: the cooperative worker-report pattern
(agent_protocols_v1 background-compression adjustment §29).

Neither host lets a hook rewrite/replace what a parent sees after a
subagent finishes -- there is no "child -> parent report filtering" or
"subagent -> subagent handoff filtering" mutation point on either host
(see the C6/D4 addenda below for the empirical detail on each). On
`SubagentStart`, this hook instead asks the subagent, cooperatively, to
compress its own large final report (or large inter-teammate message)
itself, via the `report` MCP tool (`acp/adapters/claude_code_bash_mcp.
py`) before returning/sending it. On Claude only, `SubagentStop` below
adds an enforced backstop on top of that cooperative ask.

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

`SubagentStop` enforced retry (C6, Claude and Codex)
-----------------------------------------------------
Live-probe testing (2026-09-03, see `agent_protocols_v1_1_hook_enforced_
io_compression_evaluation.md` §C6) empirically confirmed that on Claude
Code, a `SubagentStop` hook returning a top-level (not under
`hookSpecificOutput`) `{"decision": "block", "reason": ...}` forces
exactly one retry; the parent only ever sees the subagent's final,
post-retry report (the pre-block draft never leaks); and
`stop_hook_active` is `false` on the first invocation and `true` on the
retry's, giving a reliable recursion guard. This upgrades the
`SubagentStart`-injected ask above (still emitted, still cooperative)
with an enforced backstop: if the subagent ignores the ask and returns
an oversized final report anyway, block once and tell it to call
`report` before finishing.

**Correction (2026-09-03, follow-up pass, see the evaluation doc's D4
addendum):** an earlier pass claimed Codex's `SubagentStop`-equivalent
event had no `SubagentStopHookSpecificOutputWire` at all, based on
searching the compiled `codex` binary for the wrong string. Re-verified
directly against the binary (0.153.2, linux-musl standalone): it embeds
`subagent-stop.command.input`/`subagent-stop.command.output` JSON
schemas (siblings to `subagent-start.*`), not wrapped in a
`HookSpecificOutputWire` the way `PreToolUse`/`PostToolUse` are -- which
is why the original search missed them. The output schema's fields are
`decision` (a `BlockDecisionWire` enum, sole value `"block"`), `reason`
(required by Codex's own parsing when `decision` is `block`), and
`continue` (boolean, default `true`); the input schema requires
`stop_hook_active` (boolean). A live probe (isolated `CODEX_HOME`,
`codex exec --dangerously-bypass-hook-trust
--dangerously-bypass-approvals-and-sandbox` against the real backend)
confirmed the same top-level JSON shape as Claude's -- `{"decision":
"block", "reason": ...}`, `stop_hook_active`, and `last_assistant_message`
all present with matching names and semantics -- so this handler reuses
the identical Claude branch's payload shape for Codex rather than a
separate one.

**Critical difference from Claude: Codex showed no evidence of a
host-side retry cap** analogous to Claude's 8-block hard limit -- a
probe run produced 207+ consecutive `SubagentStop` re-invocations with
`stop_hook_active: true` and no natural termination before the run was
manually killed. This module must never rely on a host-side cap for
Codex. The `stop_hook_active` recursion guard below is therefore load
bearing on Codex in a way it is merely a safety net for on Claude: it is
the *only* thing preventing unbounded retries, so it is checked exactly
the same way, unconditionally, before anything else, for both agents.

Fails open (silent no-op) on any ACP error, missing/malformed field, or
unexpected exception -- a stop must never be blocked on ambiguous input.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from acp.gate import NATIVE_AGENT_REPORT_THRESHOLDS, estimate_tokens

_REPORT_INSTRUCTION = (
    "ACP is available in this session as MCP tool `report` (server "
    "`acp-bash`). If your final report to the parent, or a message to a "
    "teammate via SendMessage, would otherwise be large, call `report` "
    "with that text first and return/send its compressed output instead "
    "of the raw text."
)

# Reuse the same size policy the `report` MCP tool itself is gated on
# (`acp/adapters/claude_code_bash_mcp.py`'s `_TRAFFIC_CLASS =
# "native_agent_report"`) rather than inventing a second threshold for
# the same kind of payload -- a subagent's final report. `bypass_max` is
# in estimated tokens (`acp.gate.estimate_tokens`, ~4 chars/token).
_STOP_BLOCK_REASON = (
    "Your final report looks large. Call the `report` MCP tool (server "
    "`acp-bash`) on it first, then return that compressed output as your "
    "final report instead of the raw text."
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


def handle_stop(agent: str, event: dict[str, Any]) -> int:
    """Enforced bounded retry (C6), Claude and Codex -- see module docstring.

    Both hosts confirmed to use the identical top-level JSON shape
    (`decision`/`reason`/`stop_hook_active`/`last_assistant_message`), so
    one code path serves both -- no `agent`-specific branching needed
    beyond accepting the value. Fails open unconditionally: any
    exception, missing field, or wrong type just falls through to
    `return 0` (no decision emitted, stop proceeds normally).

    The `stop_hook_active` check is the ONLY guard against unbounded
    retries on Codex (no confirmed host-side cap there, unlike Claude's
    8-block limit) -- it must stay first and unconditional for both
    agents.
    """
    if agent not in ("claude", "codex"):
        return 0

    try:
        if bool(event.get("stop_hook_active")):
            return 0  # recursion guard: never block twice

        message = event.get("last_assistant_message")
        if not isinstance(message, str):
            return 0

        if estimate_tokens(message) <= NATIVE_AGENT_REPORT_THRESHOLDS.bypass_max:
            return 0

        json.dump(
            {"decision": "block", "reason": _STOP_BLOCK_REASON},
            sys.stdout, separators=(",", ":"),
        )
    except Exception:
        return 0
    return 0


def main() -> int:
    args = parse_args()
    event = read_event()
    event_name = str(event.get("hook_event_name") or event.get("hookEventName") or "")

    if event_name == "SubagentStart":
        return handle_start()
    if event_name == "SubagentStop":
        return handle_stop(args.agent, event)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
