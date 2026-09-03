import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from acp import containment
from acp.coordinator import Coordinator
from acp.errors import Outcome, TrafficClass
from acp.provenance import compute_hash
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

# > 32000 chars -> > 8000 estimated tokens (len // 4) -> GENERAL traffic
# class INSPECT, not BYPASS (bypass_max=8000). Mirrors tests/test_compressor.py.
_BIG_PAYLOAD = "log line filler content " * 1600
_BIG_PAYLOAD_B = "a different big payload body " * 1500
_RECEIVER = ("test-host", "session-1", None)


def _compressor_body(mode: str, text: str = "", usage: dict | None = None) -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    obj: dict = {"content": [{"type": "text", "text": content_text}]}
    if usage is not None:
        obj["usage"] = usage
    return json.dumps(obj).encode("utf-8")


class CoordinatorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._aalp_tempdir = tempfile.TemporaryDirectory()
        self._acp_tempdir = tempfile.TemporaryDirectory()
        self.aalp_root = Path(self._aalp_tempdir.name)
        self.acp_root = Path(self._acp_tempdir.name)

        self.fake = FakeAalpV1(root=self.aalp_root).start()
        self.addCleanup(self.fake.stop)
        self.addCleanup(self._aalp_tempdir.cleanup)
        self.addCleanup(self._acp_tempdir.cleanup)

        self.coordinator = Coordinator(self.aalp_root, self.acp_root)
        self.telemetry = self.coordinator.telemetry

    def _add_ci_provider(self, **overrides) -> None:
        defaults = dict(id="ci", display_name="ci", accepted_paths=["/v1/messages"])
        defaults.update(overrides)
        self.fake.add_provider(FakeProvider(**defaults))


class ConcurrentCoalesceTest(CoordinatorTestCase):
    def test_two_concurrent_evaluate_calls_coalesce_into_one_aalp_call(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk"), delay=0.3,
        )

        results: list = [None, None]
        errors: list = [None, None]

        def worker(index: int) -> None:
            try:
                results[index] = self.coordinator.evaluate(
                    _BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER
                )
            except Exception as exc:  # pragma: no cover - failure path only
                errors[index] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        start = time.monotonic()
        for t in threads:
            t.start()
        # Give both threads a chance to reach the coordinator before either
        # completes, so the second really does observe the first's job.
        time.sleep(0.05)
        for t in threads:
            t.join(timeout=10)
        elapsed = time.monotonic() - start

        self.assertIsNone(errors[0])
        self.assertIsNone(errors[1])
        self.assertIsNotNone(results[0])
        self.assertIsNotNone(results[1])
        self.assertIs(results[0].outcome, Outcome.SUCCESS)
        self.assertIs(results[1].outcome, Outcome.SUCCESS)
        self.assertEqual(results[0].output, "shrunk")
        self.assertEqual(results[1].output, "shrunk")
        # Only one programmed response existed; if a second real AALP call
        # had been made, the fake would have raised LookupError inside the
        # worker thread and surfaced as an error above.
        self.assertLess(elapsed, 2.0)


class EvaluateCacheHitTest(CoordinatorTestCase):
    def test_evaluate_reuses_ready_cached_result_from_prior_evaluate(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "from-first-evaluate"),
        )

        first = self.coordinator.evaluate(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self.assertIs(first.outcome, Outcome.SUCCESS)
        self.assertEqual(first.output, "from-first-evaluate")

        # Only one response was programmed; a second real AALP call here
        # would raise LookupError inside the fixture.
        second = self.coordinator.evaluate(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self.assertIs(second.outcome, Outcome.SUCCESS)
        self.assertEqual(second.output, "from-first-evaluate")
        self.assertEqual(self.telemetry.get("synchronous_gate_cache_hits"), 1)


class SweepStaleJobsTest(CoordinatorTestCase):
    def test_sweep_removes_only_old_terminal_jobs(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=_compressor_body("COMPACT", "old")
        )
        old_result = self.coordinator.evaluate(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self.assertIs(old_result.outcome, Outcome.SUCCESS)
        old_job_id = self.coordinator._store.cache[
            self.coordinator._cache_key(
                compute_hash(_BIG_PAYLOAD), TrafficClass.GENERAL
            )
        ]
        self.coordinator._store.jobs[old_job_id].completed_at = time.time() - 1_000

        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=_compressor_body("COMPACT", "recent")
        )
        recent_result = self.coordinator.evaluate(
            _BIG_PAYLOAD_B, TrafficClass.GENERAL, _RECEIVER
        )
        self.assertIs(recent_result.outcome, Outcome.SUCCESS)
        recent_job_id = self.coordinator._store.cache[
            self.coordinator._cache_key(
                compute_hash(_BIG_PAYLOAD_B), TrafficClass.GENERAL
            )
        ]

        removed = self.coordinator.sweep_stale_jobs(max_age_seconds=10)

        self.assertIn(old_job_id, removed)
        self.assertNotIn(recent_job_id, removed)
        self.assertNotIn(old_job_id, self.coordinator._store.jobs)


class StoreSourceTest(CoordinatorTestCase):
    def test_store_source_round_trips_via_containment(self) -> None:
        content = b"raw source bytes for round-trip"
        source_hash = self.coordinator.store_source(content)
        self.assertEqual(source_hash, compute_hash(content))
        self.assertEqual(containment.read_raw(self.acp_root, source_hash), content)


class StatusPrivacyTest(CoordinatorTestCase):
    def test_status_never_exposes_raw_payload_or_secret(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=_compressor_body("COMPACT", "secret-free")
        )
        self.coordinator.evaluate(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)

        status = self.coordinator.status()
        dumped = json.dumps(status)

        self.assertNotIn(_BIG_PAYLOAD[:80], dumped)
        self.assertNotIn(self.fake.secret, dumped)
        self.assertTrue(status["aalp_reachable"])
        self.assertIn("jobs_by_state", status)


if __name__ == "__main__":
    unittest.main()
