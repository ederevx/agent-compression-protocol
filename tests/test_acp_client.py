"""End-to-end tests for `acp.adapters.acp_client.AcpClient` against a real
`acp.serve.build_ingress` instance -- the same real-socket discipline
`tests/test_serve.py` uses, one layer further out: this is what a genuine
host-adapter subprocess (see `acp.adapters.claude_code_bash_mcp`) does to
reach ACP."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from acp.adapters.acp_client import AcpBootstrapError, AcpClient, AcpProtocolError
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

_SMALL_PAYLOAD = "a short payload well under the bypass threshold"
_BIG_PAYLOAD = "log line filler content " * 1600


def _compressor_body(mode: str, text: str = "") -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    return json.dumps({"content": [{"type": "text", "text": content_text}]}).encode("utf-8")


class AcpClientEndToEndTest(unittest.TestCase):
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

    def _receiver(self) -> dict:
        return {"host": "h", "session_id": "s", "agent_id": None}

    def test_bypass_small_payload_returns_unchanged(self) -> None:
        result = self.client.evaluate(_SMALL_PAYLOAD, "general", self._receiver())
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "PASS")
        self.assertEqual(result.output, _SMALL_PAYLOAD)

    def test_inspect_big_payload_returns_compressed_output(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-output"),
        )
        result = self.client.evaluate(_BIG_PAYLOAD, "general", self._receiver())
        self.assertTrue(result.ok)
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, "shrunk-output")
        self.assertIsNotNone(result.provenance)

    def test_non_success_outcome_surfaces_without_raising(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="total_timeout", message="boom"
        )
        result = self.client.evaluate(_BIG_PAYLOAD, "general", self._receiver())
        self.assertFalse(result.ok)
        self.assertEqual(result.outcome, "total_timeout")

    def test_bootstrap_error_when_ingress_descriptor_missing(self) -> None:
        with tempfile.TemporaryDirectory() as empty_root:
            client = AcpClient(empty_root)
            with self.assertRaises(AcpBootstrapError):
                client.evaluate(_SMALL_PAYLOAD, "general", self._receiver())

    def test_wrong_secret_raises_protocol_error(self) -> None:
        client = AcpClient(self.acp_root)
        client._ensure_bootstrapped()
        client._secret = "wrong-secret"
        with self.assertRaises(AcpProtocolError):
            client.evaluate(_SMALL_PAYLOAD, "general", self._receiver())

    def test_store_source_returns_content_addressed_hash(self) -> None:
        first = self.client.store_source(b"identical content")
        second = self.client.store_source(b"identical content")
        self.assertTrue(first)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
