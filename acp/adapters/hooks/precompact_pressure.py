#!/usr/bin/env python3
"""PreCompact hygiene hook: signals ACP that a receiving context is about
to be natively compacted, per Phase 4's pre-native-compaction-hygiene
checklist item.

PreCompact firing at all is itself the signal -- neither host's
PreCompact input reliably exposes a token/size ratio, and computing one
independently would duplicate the host's own compaction-trigger logic
for no real gain. So this hook reports `observed_ratio=1.0`
unconditionally, promoting the receiver's `PressureMode`
(`acp/pressure.py`) toward `ABOVE_EMERGENCY`, which only affects how
eager ACP's own proactive-preparation heuristics are for that receiver
(`Coordinator.maybe_trigger_maintenance`) -- it never blocks, delays, or
otherwise alters the host's actual compaction.

Fails open (silent no-op, exit 0) on any ACP error, matching every other
ACP host-adapter piece.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from acp.adapters.acp_client import AcpClient

_HOST_BY_AGENT = {"claude": "claude-code", "codex": "codex-cli"}


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


def main() -> int:
    args = parse_args()
    acp_root = os.environ.get("ACP_HOME")
    if not acp_root:
        return 0

    event = read_event()
    try:
        AcpClient(acp_root).report_pressure(receiver_of(args.agent, event), 1.0)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
