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
from acp.pressure import PressureController, PressureMode
from acp.telemetry import Telemetry
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

# > 32000 chars -> > 8000 estimated tokens (len // 4) -> GENERAL traffic
# class INSPECT, not BYPASS (bypass_max=8000). Mirrors tests/test_compressor.py.
_BIG_PAYLOAD = "log line filler content " * 1600
_BIG_PAYLOAD_B = "a different big payload body " * 1500
_RECEIVER = ("test-host", "session-1", None)
_RECEIVER_B = ("test-host", "session-2", None)


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

    def _wait_until_resolved(self, job_id: str, timeout: float = 5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.coordinator.resolve(job_id)
            if result is not None:
                return result
            time.sleep(0.01)
        self.fail(f"job {job_id} never reached a resolvable terminal state")


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


class PrepareResolveTest(CoordinatorTestCase):
    def test_prepare_then_resolve_returns_ready_result_once_ready(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "prepared"), delay=0.2,
        )

        job_id = self.coordinator.prepare(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        # Almost certainly still in flight immediately after prepare().
        self.assertIsNone(self.coordinator.resolve(job_id))

        result = self._wait_until_resolved(job_id)
        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.output, "prepared")

    def test_prepare_reuses_ready_cached_result(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "cached"),
        )

        first_job_id = self.coordinator.prepare(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self._wait_until_resolved(first_job_id)

        second_job_id = self.coordinator.prepare(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self.assertEqual(first_job_id, second_job_id)
        self.assertEqual(self.telemetry.get("background_jobs_reused"), 1)


class EvaluateCacheHitTest(CoordinatorTestCase):
    def test_evaluate_consumes_ready_cache_from_prior_prepare(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "from-prepare"),
        )

        job_id = self.coordinator.prepare(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self._wait_until_resolved(job_id)

        # Only one response was programmed; a second real AALP call here
        # would raise LookupError inside the fixture.
        result = self.coordinator.evaluate(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.output, "from-prepare")
        self.assertEqual(self.telemetry.get("synchronous_gate_cache_hits"), 1)


class PressureModeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.telemetry = Telemetry()
        self.controller = PressureController(telemetry=self.telemetry)

    def test_upward_crossings_transition_immediately(self) -> None:
        self.assertEqual(self.controller.report(_RECEIVER, 0.50), PressureMode.BELOW_SOFT)
        self.assertEqual(self.controller.report(_RECEIVER, 0.65), PressureMode.SOFT_TO_HARD)
        self.assertEqual(self.controller.report(_RECEIVER, 0.80), PressureMode.HARD_TO_EMERGENCY)
        self.assertEqual(self.controller.report(_RECEIVER, 0.90), PressureMode.ABOVE_EMERGENCY)
        self.assertEqual(self.telemetry.get("pressure_mode_entries"), 3)
        self.assertEqual(self.telemetry.get("pressure_mode_exits"), 3)

    def test_small_downward_fluctuation_does_not_demote(self) -> None:
        self.controller.report(_RECEIVER, 0.65)  # SOFT_TO_HARD, entry watermark 0.60
        mode = self.controller.report(_RECEIVER, 0.58)  # 0.60 - 0.05 = 0.55 exit threshold
        self.assertEqual(mode, PressureMode.SOFT_TO_HARD)
        self.assertEqual(self.telemetry.get("pressure_mode_entries"), 1)

    def test_large_downward_move_demotes(self) -> None:
        self.controller.report(_RECEIVER, 0.65)  # SOFT_TO_HARD
        mode = self.controller.report(_RECEIVER, 0.50)  # well below 0.55 exit threshold
        self.assertEqual(mode, PressureMode.BELOW_SOFT)
        self.assertEqual(self.telemetry.get("pressure_mode_entries"), 2)
        self.assertEqual(self.telemetry.get("pressure_mode_exits"), 2)

    def test_entries_and_exits_only_increment_on_actual_transition(self) -> None:
        self.controller.report(_RECEIVER, 0.65)
        before = self.telemetry.get("pressure_mode_entries")
        self.controller.report(_RECEIVER, 0.66)  # still SOFT_TO_HARD: no transition
        self.controller.report(_RECEIVER, 0.64)  # still SOFT_TO_HARD: no transition
        self.assertEqual(self.telemetry.get("pressure_mode_entries"), before)

    def test_pressure_isolated_per_receiver_key(self) -> None:
        self.controller.report(_RECEIVER, 0.90)  # ABOVE_EMERGENCY
        self.assertEqual(self.controller.mode_of(_RECEIVER), PressureMode.ABOVE_EMERGENCY)
        self.assertEqual(self.controller.mode_of(_RECEIVER_B), PressureMode.BELOW_SOFT)

    def test_should_run_maintenance_true_at_soft_to_hard_and_above(self) -> None:
        self.assertFalse(self.controller.should_run_maintenance(_RECEIVER))
        self.controller.report(_RECEIVER, 0.65)
        self.assertTrue(self.controller.should_run_maintenance(_RECEIVER))


class CoordinatorPressureIsolationTest(CoordinatorTestCase):
    def test_report_pressure_isolated_across_receivers(self) -> None:
        self.coordinator.report_pressure(_RECEIVER, 0.90)
        status = self.coordinator.status()
        self.assertIn("ABOVE_EMERGENCY", status["pressure"].values())
        self.assertEqual(len(status["pressure"]), 1)


class SweepStaleJobsTest(CoordinatorTestCase):
    def test_sweep_removes_only_old_terminal_jobs(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=_compressor_body("COMPACT", "old")
        )
        old_job_id = self.coordinator.prepare(_BIG_PAYLOAD, TrafficClass.GENERAL, _RECEIVER)
        self._wait_until_resolved(old_job_id)
        self.coordinator._jobs[old_job_id].completed_at = time.time() - 1_000

        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "recent"), delay=1.0,
        )
        recent_job_id = self.coordinator.prepare(
            _BIG_PAYLOAD_B, TrafficClass.GENERAL, _RECEIVER
        )

        removed = self.coordinator.sweep_stale_jobs(max_age_seconds=10)

        self.assertIn(old_job_id, removed)
        self.assertNotIn(recent_job_id, removed)
        self.assertIsNone(self.coordinator.resolve(old_job_id))


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
