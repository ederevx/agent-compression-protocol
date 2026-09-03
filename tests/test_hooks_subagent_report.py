"""Tests for `acp.adapters.hooks.subagent_report`."""
from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from acp.adapters.hooks.subagent_report import handle_start, main


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

    def test_subagent_stop_is_now_a_noop(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": "x" * 100_000,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "codex"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
