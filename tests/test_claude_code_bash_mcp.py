"""Tests for `acp.adapters.claude_code_bash_mcp`.

`BashMcpServer` is exercised as a plain in-process object (its
`handle_message` never touches stdio) -- `main()`'s stdio loop is a thin
enough wrapper that it does not need its own test. The fail-open
guarantee (ACP unreachable never blocks or corrupts a command's output)
is exercised against a real `acp.serve.build_ingress` instance, both
running and deliberately stopped.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acp.adapters.claude_code_bash_mcp import BashMcpServer, run_bash
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider


class RunBashTest(unittest.TestCase):
    def test_combines_stdout_and_stderr(self) -> None:
        output = run_bash("echo out; echo err 1>&2", timeout_seconds=5)
        self.assertIn("out", output)
        self.assertIn("err", output)

    def test_nonzero_exit_appends_exit_code_note(self) -> None:
        output = run_bash("exit 3", timeout_seconds=5)
        self.assertIn("[exit code: 3]", output)

    def test_zero_exit_has_no_exit_code_note(self) -> None:
        output = run_bash("echo ok", timeout_seconds=5)
        self.assertNotIn("[exit code:", output)

    def test_timeout_reports_without_raising(self) -> None:
        output = run_bash("sleep 5", timeout_seconds=0.2)
        self.assertIn("timed out", output)


class BashMcpServerProtocolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.server = BashMcpServer(self._tempdir.name)

    def test_initialize_handshake(self) -> None:
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["protocolVersion"], "2024-11-05")
        self.assertIn("tools", response["result"]["capabilities"])

    def test_initialized_notification_gets_no_response(self) -> None:
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(response)

    def test_tools_list_advertises_bash(self) -> None:
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in response["result"]["tools"]]
        self.assertEqual(names, ["bash", "report"])
        self.assertIn("command", response["result"]["tools"][0]["inputSchema"]["properties"])
        self.assertIn("text", response["result"]["tools"][1]["inputSchema"]["properties"])

    def test_unknown_tool_call_returns_is_error(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        })
        self.assertTrue(response["result"]["isError"])

    def test_missing_command_argument_returns_is_error(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "bash", "arguments": {}},
        })
        self.assertTrue(response["result"]["isError"])

    def test_missing_text_argument_returns_is_error(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 41, "method": "tools/call",
            "params": {"name": "report", "arguments": {}},
        })
        self.assertTrue(response["result"]["isError"])

    def test_report_call_with_acp_unreachable_falls_back_to_raw_text(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 42, "method": "tools/call",
            "params": {"name": "report", "arguments": {"text": "a raw report body"}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "a raw report body")

    def test_report_calls_store_source_and_still_succeeds_if_it_fails(self) -> None:
        with patch.object(
            self.server._client, "store_source", side_effect=RuntimeError("boom")
        ) as mock_store:
            response = self.server.handle_message({
                "jsonrpc": "2.0", "id": 43, "method": "tools/call",
                "params": {"name": "report", "arguments": {"text": "a raw report body"}},
            })
        mock_store.assert_called_once_with("a raw report body".encode("utf-8"))
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "a raw report body")

    def test_unknown_method_with_id_returns_json_rpc_error(self) -> None:
        response = self.server.handle_message(
            {"jsonrpc": "2.0", "id": 5, "method": "not/a/real/method"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_notification_without_id_gets_no_response(self) -> None:
        response = self.server.handle_message({"jsonrpc": "2.0", "method": "notifications/x"})
        self.assertIsNone(response)

    def test_bash_call_with_acp_unreachable_falls_back_to_raw_output(self) -> None:
        # No ingress descriptor exists under this tempdir at all -- the
        # server must still return the command's real output, not error out.
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 6, "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "echo hello-fallback"}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertIn("hello-fallback", response["result"]["content"][0]["text"])


def _compressor_body(mode: str, text: str = "") -> bytes:
    content_text = f"ACP-QUEUE-ITEM: solo\nACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    return json.dumps({"content": [{"type": "text", "text": content_text}]}).encode("utf-8")


class BashMcpServerRealAcpTest(unittest.TestCase):
    """End-to-end: a real ACP ingress, a real (fake-upstream) AALP, and the
    server's `tools/call` path all wired together over real sockets."""

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

        self.server = BashMcpServer(str(self.acp_root))

    def test_small_command_output_bypasses_unchanged(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "echo small-output"}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertIn("small-output", response["result"]["content"][0]["text"])

    def test_large_command_output_is_compressed_via_real_acp_roundtrip(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-command-output"),
        )
        # yes '...' * N under a single echo keeps this a valid, fast command
        # while producing a payload well above GENERAL's 8000-token bypass.
        command = "python3 -c \"print('log line filler content ' * 1600)\""
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": command}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(
            response["result"]["content"][0]["text"], "shrunk-command-output")

    def test_acp_stopped_mid_session_falls_back_to_raw_output(self) -> None:
        self.ingress.stop()
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "bash", "arguments": {"command": "echo still-works"}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertIn("still-works", response["result"]["content"][0]["text"])

    def test_small_report_text_bypasses_unchanged(self) -> None:
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "report", "arguments": {"text": "small report body"}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "small report body")

    def test_large_report_text_is_compressed_via_real_acp_roundtrip(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-report"),
        )
        big_report = "subagent finding line " * 1600
        response = self.server.handle_message({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "report", "arguments": {"text": big_report}},
        })
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(response["result"]["content"][0]["text"], "shrunk-report")



if __name__ == "__main__":
    unittest.main()
