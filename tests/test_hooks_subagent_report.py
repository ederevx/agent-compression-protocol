"""Tests for `acp.adapters.hooks.subagent_report`."""
from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from acp.adapters.hooks.subagent_report import (
    handle_start,
    main,
    transcript_tail,
)
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider


def _compressor_body(mode: str, text: str = "") -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    return json.dumps({"content": [{"type": "text", "text": content_text}]}).encode("utf-8")


class TranscriptTailTest(unittest.TestCase):
    def test_codex_reads_last_assistant_message_directly(self) -> None:
        text = transcript_tail("codex", {"last_assistant_message": "final report text"})
        self.assertEqual(text, "final report text")

    def test_codex_missing_field_returns_none(self) -> None:
        self.assertIsNone(transcript_tail("codex", {}))

    def test_claude_reads_last_assistant_message_from_transcript_file(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as handle:
            handle.write(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
            handle.write(json.dumps({
                "message": {"role": "assistant", "content": [{"type": "text", "text": "the report"}]}
            }) + "\n")
            path = handle.name
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))

        text = transcript_tail("claude", {"transcript_path": path})
        self.assertEqual(text, "the report")

    def test_claude_missing_transcript_path_returns_none(self) -> None:
        self.assertIsNone(transcript_tail("claude", {}))

    def test_claude_unreadable_transcript_path_returns_none(self) -> None:
        self.assertIsNone(transcript_tail("claude", {"transcript_path": "/no/such/file.jsonl"}))


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

    def test_subagent_stop_without_acp_home_is_a_noop(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop", "last_assistant_message": "x" * 100_000,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "codex"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")


class SubagentStopRealAcpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._aalp_tempdir = tempfile.TemporaryDirectory()
        self._acp_tempdir = tempfile.TemporaryDirectory()
        self.aalp_root = Path(self._aalp_tempdir.name)
        self.acp_root = Path(self._acp_tempdir.name)
        self.addCleanup(self._aalp_tempdir.cleanup)
        self.addCleanup(self._acp_tempdir.cleanup)

        self.fake = FakeAalpV1(root=self.aalp_root).start()
        self.addCleanup(self.fake.stop)
        self.fake.add_provider(
            FakeProvider(id="ci", display_name="ci", accepted_paths=["/v1/messages"])
        )
        self.ingress = build_ingress(aalp_root=self.aalp_root, root=self.acp_root)
        self.ingress.start()
        self.addCleanup(self.ingress.stop)

    def test_large_transcript_tail_fires_a_prepare_job_and_exits_zero(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-prewarm"),
        )
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop",
            "session_id": "s1",
            "last_assistant_message": "y" * 50_000,
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "codex"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {"ACP_HOME": str(self.acp_root)}):
            self.assertEqual(main(), 0)
        # Let the fire-and-forget background job finish before teardown
        # tears down the fake AALP/ACP sockets out from under it.
        time.sleep(0.2)

    def test_small_transcript_tail_is_a_noop(self) -> None:
        stdin = io.StringIO(json.dumps({
            "hook_event_name": "SubagentStop",
            "session_id": "s1",
            "last_assistant_message": "small",
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["subagent_report.py", "--agent", "codex"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {"ACP_HOME": str(self.acp_root)}):
            self.assertEqual(main(), 0)
        # Below the prewarm threshold: `prepare()` (and therefore any AALP
        # forward call) must never have been reached.
        self.assertIsNone(self.fake.last_headers)


if __name__ == "__main__":
    unittest.main()
