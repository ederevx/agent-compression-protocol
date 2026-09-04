"""Tests for `acp.adapters.hooks.subagent_report`."""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from acp.adapters.hooks.subagent_report import handle_start, handle_stop, main
from acp.gate import NATIVE_AGENT_REPORT_THRESHOLDS

_SMALL_MESSAGE = "ok"
_LARGE_MESSAGE = "x" * ((NATIVE_AGENT_REPORT_THRESHOLDS.bypass_max + 1) * 4)


class HandleStartTest(unittest.TestCase):
    def test_emits_additional_context_instructing_the_report_tool(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            self.assertEqual(handle_start(), 0)
        payload = json.loads(buffer.getvalue())
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("report", context)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SubagentStart")


class MainDispatchTest(unittest.TestCase):
    def test_subagent_start_event_emits_context(self) -> None:
        stdin = io.StringIO(json.dumps({"hook_event_name": "SubagentStart"}))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertIn("additionalContext", buffer.getvalue())

    def test_unrecognized_event_is_a_silent_noop(self) -> None:
        stdin = io.StringIO(json.dumps({"hook_event_name": "SomethingElse"}))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")

    def test_subagent_stop_is_a_noop_on_codex_regardless_of_size(self) -> None:
        # Codex has no SubagentStopHookSpecificOutputWire (confirmed by
        # inspecting the compiled binary's schema) -- structurally cannot
        # carry a block/retry decision, so this stays a no-op there.
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": _LARGE_MESSAGE,
            "stop_hook_active": False,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "codex"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")

    def test_subagent_stop_blocks_on_claude_for_oversized_report(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": _LARGE_MESSAGE,
            "stop_hook_active": False,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["decision"], "block")
        self.assertIn("report", payload["reason"])

    def test_subagent_stop_respects_recursion_guard(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": _LARGE_MESSAGE,
            "stop_hook_active": True,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")

    def test_subagent_stop_does_not_block_small_report_on_claude(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": _SMALL_MESSAGE,
            "stop_hook_active": False,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")


class HandleStopTest(unittest.TestCase):
    def test_fails_open_on_non_string_message(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            self.assertEqual(
                handle_stop("claude", {"stop_hook_active": False, "last_assistant_message": None}), 0,
            )
        self.assertEqual(buffer.getvalue(), "")

    def test_fails_open_on_missing_fields(self) -> None:
        buffer = io.StringIO()
        with patch("sys.stdout", buffer):
            self.assertEqual(handle_stop("claude", {}), 0)
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
