"""Tests for `acp.adapters.hooks.parent_child_context`."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acp.adapters.hooks.parent_child_context import compress_prompt, main
from acp.adapters.acp_client import AcpClient
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider


def _compressor_body(mode: str, text: str = "") -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    return json.dumps({"content": [{"type": "text", "text": content_text}]}).encode("utf-8")


class MainNoBlockTest(unittest.TestCase):
    def test_no_context_block_is_a_silent_noop(self) -> None:
        stdin = io.StringIO(json.dumps({
            "tool_input": {"prompt": "plain instruction, no delimited block"},
            "session_id": "s1",
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["parent_child_context.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {"ACP_HOME": "/irrelevant"}):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")

    def test_missing_acp_home_is_a_silent_noop(self) -> None:
        stdin = io.StringIO(json.dumps({
            "tool_input": {"prompt": "<acp-context>big</acp-context>"},
            "session_id": "s1",
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["parent_child_context.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")

    def test_non_dict_tool_input_is_a_silent_noop(self) -> None:
        stdin = io.StringIO(json.dumps({"tool_input": "not-a-dict", "session_id": "s1"}))
        buffer = io.StringIO()
        with patch("sys.argv", ["parent_child_context.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {"ACP_HOME": "/irrelevant"}):
            self.assertEqual(main(), 0)
        self.assertEqual(buffer.getvalue(), "")


class RealAcpTest(unittest.TestCase):
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
        self.client = AcpClient(self.acp_root)

    def test_compress_prompt_leaves_undelimited_prompt_untouched(self) -> None:
        result = compress_prompt(self.client, "claude", "s1", "do the task, no block here")
        self.assertIsNone(result)

    def test_compress_prompt_only_replaces_the_delimited_block(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-block"),
        )
        big_block = "supporting evidence line " * 2200
        prompt = f"Do the task.\n<acp-context>{big_block}</acp-context>\nEnd of context."
        new_prompt = compress_prompt(self.client, "claude", "s1", prompt)
        self.assertIsNotNone(new_prompt)
        self.assertIn("Do the task.", new_prompt)
        self.assertIn("End of context.", new_prompt)
        self.assertIn("shrunk-block", new_prompt)
        self.assertNotIn(big_block, new_prompt)

    def test_small_block_pass_through_returns_none(self) -> None:
        prompt = "Do the task.\n<acp-context>tiny</acp-context>\nEnd."
        result = compress_prompt(self.client, "claude", "s1", prompt)
        self.assertIsNone(result)

    def test_main_emits_updated_input_for_a_large_block(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-block"),
        )
        big_block = "supporting evidence line " * 2200
        prompt = f"Do the task.\n<acp-context>{big_block}</acp-context>\nEnd of context."
        stdin = io.StringIO(json.dumps({
            "tool_input": {"prompt": prompt}, "session_id": "s1",
        }))
        buffer = io.StringIO()
        with patch("sys.argv", ["parent_child_context.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), patch("sys.stdout", buffer), \
             patch.dict("os.environ", {"ACP_HOME": str(self.acp_root)}):
            self.assertEqual(main(), 0)
        payload = json.loads(buffer.getvalue())
        updated_input = payload["hookSpecificOutput"]["updatedInput"]
        self.assertIn("shrunk-block", updated_input["prompt"])
        self.assertIn("Do the task.", updated_input["prompt"])


if __name__ == "__main__":
    unittest.main()
