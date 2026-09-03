"""Tests for `acp.adapters.hooks.precompact_pressure`."""
from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from acp.adapters.hooks.precompact_pressure import main, receiver_of
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider


class ReceiverOfTest(unittest.TestCase):
    def test_uses_agent_specific_host_and_session_id(self) -> None:
        receiver = receiver_of("codex", {"session_id": "abc", "agent_id": "sub-1"})
        self.assertEqual(receiver, {"host": "codex-cli", "session_id": "abc", "agent_id": "sub-1"})

    def test_missing_session_id_and_agent_id_default_sanely(self) -> None:
        receiver = receiver_of("claude", {})
        self.assertEqual(receiver, {"host": "claude-code", "session_id": "unknown", "agent_id": None})


class MainNoAcpHomeTest(unittest.TestCase):
    def test_exits_zero_without_acp_home(self) -> None:
        stdin = io.StringIO("{}")
        with patch("sys.argv", ["precompact_pressure.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(), 0)


class MainRealAcpTest(unittest.TestCase):
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

    def test_reports_maximum_pressure_and_exits_zero(self) -> None:
        stdin = io.StringIO('{"session_id": "s1"}')
        with patch("sys.argv", ["precompact_pressure.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), \
             patch.dict("os.environ", {"ACP_HOME": str(self.acp_root)}):
            self.assertEqual(main(), 0)

    def test_acp_unreachable_still_exits_zero(self) -> None:
        self.ingress.stop()
        stdin = io.StringIO('{"session_id": "s1"}')
        with patch("sys.argv", ["precompact_pressure.py", "--agent", "claude"]), \
             patch("sys.stdin", stdin), \
             patch.dict("os.environ", {"ACP_HOME": str(self.acp_root)}):
            self.assertEqual(main(), 0)


if __name__ == "__main__":
    unittest.main()
