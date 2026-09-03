#!/usr/bin/env python3
"""PreToolUse hook (matched to the subagent-spawning tool -- `Task` on
Claude Code, `spawn_agent` on Codex) implementing "parent -> child
oversized support context" (Phase 4).

Only ever touches an explicitly delimited block, never the whole prompt:
ACP's own `downward_context` traffic class is documented (`acp/gate.py`)
as sizing only supporting material, never the instruction itself, and
there is no reliable way for a hook to tell instruction text apart from
pasted context in an arbitrary prompt string. So this hook only acts
when the prompt contains a `<acp-context>...</acp-context>` block (a
convention the caller -- e.g. the main agent authoring a Task-tool call
-- opts into deliberately); everything outside that block is left
untouched, and a prompt with no such block is a silent no-op.

Whether `PreToolUse`'s `updatedInput` actually applies to the
subagent-spawning tool must be empirically confirmed per host before
this hook is wired into a live install -- see project_md's STATUS.md
Phase 4 checkpoint for each host's result. Undocumented for Claude;
documented as applying for Codex.

Fails open (no `updatedInput` emitted, i.e. the original prompt goes
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
_PROMPT_FIELD_BY_AGENT = {"claude": "prompt", "codex": "prompt"}
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


def compress_prompt(
    client: AcpClient, agent: str, session_id: str, prompt: str,
) -> str | None:
    """The new prompt if a delimited block was found and ACP shrank it;
    `None` if there is nothing to do (no block, ACP unreachable, or ACP
    chose PASS/returned the block unchanged)."""
    match = _CONTEXT_BLOCK_RE.search(prompt)
    if match is None:
        return None
    block = match.group(1)

    receiver = {"host": _HOST_BY_AGENT[agent], "session_id": session_id, "agent_id": None}
    try:
        result = client.evaluate(block, "downward_context", receiver)
    except Exception:
        return None
    if not result.ok or result.output is None or result.output == block:
        return None

    return prompt[: match.start()] + result.output + prompt[match.end():]


def main() -> int:
    args = parse_args()
    acp_root = os.environ.get("ACP_HOME")
    event = read_event()

    tool_input = event.get("tool_input")
    if not acp_root or not isinstance(tool_input, dict):
        return 0

    prompt_field = _PROMPT_FIELD_BY_AGENT[args.agent]
    prompt = tool_input.get(prompt_field)
    if not isinstance(prompt, str):
        return 0

    session_id = str(event.get("session_id") or "unknown")
    new_prompt = compress_prompt(AcpClient(acp_root), args.agent, session_id, prompt)
    if new_prompt is None:
        return 0

    new_input = dict(tool_input)
    new_input[prompt_field] = new_prompt
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
