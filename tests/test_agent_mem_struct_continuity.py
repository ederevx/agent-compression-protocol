"""§33/§16: agent-mem-struct pre-compaction checkpoint/restore continuity
still works once ACP has transformed the transcript content it reads.

This is a black-box integration test, not a unit test of ACP's own code:
it runs a real payload through ACP's actual `Compressor` (against a fake
AALP, per ACP's own test convention -- see `tests/fixtures/fake_aalp_v1.
py`), embeds the genuinely-transformed output in a synthetic transcript
exactly as Claude/Codex would after ACP's `PostToolUse.updatedToolOutput`
(or the Codex producer/report-path equivalent, per
agent_protocols_v1_background_compression_adjustment_metadata_v1.md §26-
29) replaces a tool result, and then invokes agent-mem-struct's real,
installed `hooks/root-memory-context.py` as a subprocess -- exactly as
Claude/Codex would fire it, since ACP's own ingress-transform path and
agent-mem-struct's PreCompact hook operate on the same transcript
content (§29 of agent_protocols_v1_metadata_v1.md). It never imports or
monkeypatches agent-mem-struct internals, and never writes into that
repository.

Skipped (not failed) when the agent-mem-struct checkout isn't present
next to this repo -- it is a sibling protocol repository, not a
dependency of ACP.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from acp.aalp_client import AalpClient
from acp.compressor import Compressor
from acp.errors import TrafficClass
from acp.telemetry import Telemetry
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

_MEM_STRUCT_HOOK = Path("/home/ederevx/agent-mem-struct/hooks/root-memory-context.py")

# > 32000 chars -> > 8000 estimated tokens (len // 4) -> GENERAL traffic
# class INSPECT, not BYPASS. Mirrors tests/test_compressor.py.
_RAW_TOOL_OUTPUT = "build failure: bar() null deref at foo.c:42\n" * 900
_COMPRESSED_CAPSULE = "3 build failures in foo.c; root cause: bar() null deref"


def _compressor_body(mode: str, text: str) -> bytes:
    obj = {"content": [{"type": "text", "text": f"ACP-QUEUE-ITEM: solo\nACP-MODE: {mode}\n\n{text}"}]}
    return json.dumps(obj).encode("utf-8")


@unittest.skipUnless(
    _MEM_STRUCT_HOOK.exists(), "agent-mem-struct checkout not present next to this repo"
)
class AgentMemStructContinuityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._aalp_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._aalp_tempdir.cleanup)
        aalp_root = Path(self._aalp_tempdir.name)
        self.fake = FakeAalpV1(root=aalp_root).start()
        self.addCleanup(self.fake.stop)
        self.fake.add_provider(
            FakeProvider(id="ci", display_name="ci", accepted_paths=["/v1/messages"])
        )
        self.compressor = Compressor(AalpClient(aalp_root=aalp_root), Telemetry())

        self._home_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._home_tempdir.cleanup)
        self.home = Path(self._home_tempdir.name)
        (self.home / "memory").mkdir(parents=True)
        (self.home / "memory" / "MEMORY.md").write_text(
            "Structure-Version: v1\nStructure: ../STRUCTURE.md\n", encoding="utf-8"
        )
        (self.home / "RULES.md").write_text("rules", encoding="utf-8")
        (self.home / "STRUCTURE.md").write_text("Structure-Version: v1\n", encoding="utf-8")

    def _run_hook(self, event: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(_MEM_STRUCT_HOOK), "--agent", "claude", "--home", str(self.home)],
            input=json.dumps(event), text=True, capture_output=True, timeout=30,
        )

    def test_checkpoint_and_compact_restore_survive_a_real_acp_transform(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", _COMPRESSED_CAPSULE),
        )
        result = self.compressor.compress(_RAW_TOOL_OUTPUT, TrafficClass.GENERAL)
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, _COMPRESSED_CAPSULE)
        self.assertNotEqual(result.output, _RAW_TOOL_OUTPUT)

        transcript_path = self.home / "transcript.jsonl"
        with transcript_path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(
                {"type": "last-prompt", "lastPrompt": "fix the failing build"}
            ) + "\n")
            handle.write(json.dumps({
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "content": result.output}],
                }
            }) + "\n")

        session_id = "sess-continuity-1"
        precompact = self._run_hook({
            "hook_event_name": "PreCompact",
            "session_id": session_id,
            "transcript_path": str(transcript_path),
            "triggered_by": "auto",
        })
        self.assertEqual(precompact.returncode, 0, precompact.stderr)

        checkpoint_file = (
            self.home / ".agent-mem-struct" / "compaction-checkpoints" / f"{session_id}.json"
        )
        self.assertTrue(checkpoint_file.exists())
        saved = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        self.assertIn(_COMPRESSED_CAPSULE, saved["checkpoint"])
        self.assertIn("fix the failing build", saved["checkpoint"])
        # Continuity survived the transform, not merely a truncated
        # fragment of it -- the raw, uncompressed tool output never
        # appears anywhere (it was never re-inserted into the transcript;
        # ACP's capsule is the only version agent-mem-struct ever saw).
        self.assertNotIn(_RAW_TOOL_OUTPUT, saved["checkpoint"])

        restore = self._run_hook({
            "hook_event_name": "SessionStart",
            "session_id": session_id,
            "source": "compact",
        })
        self.assertEqual(restore.returncode, 0, restore.stderr)
        additional_context = json.loads(restore.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("PRE-COMPACTION CONTINUITY CHECKPOINT", additional_context)
        self.assertIn(_COMPRESSED_CAPSULE, additional_context)
        self.assertNotIn("CONTINUITY WARNING", additional_context)
        # A restored checkpoint is single-use.
        self.assertFalse(checkpoint_file.exists())


if __name__ == "__main__":
    unittest.main()
