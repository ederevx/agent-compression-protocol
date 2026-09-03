import unittest

from acp.telemetry import COUNTER_NAMES, Telemetry, TelemetryError


class TelemetryTest(unittest.TestCase):
    def test_all_counters_start_at_zero(self) -> None:
        telemetry = Telemetry()
        snapshot = telemetry.snapshot()
        self.assertEqual(set(snapshot.keys()), set(COUNTER_NAMES))
        self.assertTrue(all(value == 0 for value in snapshot.values()))

    def test_increment_default_amount(self) -> None:
        telemetry = Telemetry()
        telemetry.increment("compression_attempts")
        self.assertEqual(telemetry.get("compression_attempts"), 1)

    def test_increment_custom_amount(self) -> None:
        telemetry = Telemetry()
        telemetry.increment("compression_input_tokens", amount=500)
        self.assertEqual(telemetry.get("compression_input_tokens"), 500)

    def test_increment_accumulates(self) -> None:
        telemetry = Telemetry()
        telemetry.increment("background_jobs_enqueued")
        telemetry.increment("background_jobs_enqueued")
        telemetry.increment("background_jobs_enqueued", amount=3)
        self.assertEqual(telemetry.get("background_jobs_enqueued"), 5)

    def test_unknown_counter_raises_on_increment(self) -> None:
        telemetry = Telemetry()
        with self.assertRaises(TelemetryError):
            telemetry.increment("typo_counter_name")

    def test_unknown_counter_raises_on_get(self) -> None:
        telemetry = Telemetry()
        with self.assertRaises(TelemetryError):
            telemetry.get("typo_counter_name")

    def test_snapshot_is_a_copy(self) -> None:
        telemetry = Telemetry()
        snapshot = telemetry.snapshot()
        snapshot["compression_attempts"] = 999
        self.assertEqual(telemetry.get("compression_attempts"), 0)

    def test_reset_zeroes_all_counters(self) -> None:
        telemetry = Telemetry()
        telemetry.increment("compression_attempts", amount=10)
        telemetry.reset()
        self.assertTrue(all(value == 0 for value in telemetry.snapshot().values()))

    def test_sink_called_with_name_and_new_value(self) -> None:
        calls = []
        telemetry = Telemetry(sink=lambda name, value: calls.append((name, value)))
        telemetry.increment("compression_attempts")
        telemetry.increment("compression_attempts", amount=2)
        self.assertEqual(
            calls, [("compression_attempts", 1), ("compression_attempts", 3)])

    def test_all_spec_counter_names_present(self) -> None:
        expected = {
            "compression_attempts", "compression_successes",
            "compression_input_tokens", "compression_output_tokens",
            "compression_saved_tokens", "compression_bypass_payloads",
            "compression_bypass_tokens", "compression_availability",
            "compression_queue_timeouts", "compression_execution_timeouts",
            "compression_total_timeouts", "compression_timeout_bypass_tokens",
            "compression_execution_ms", "background_jobs_enqueued",
            "background_jobs_started", "background_jobs_ready",
            "background_jobs_reused", "background_jobs_stale",
            "background_jobs_failed", "synchronous_gate_wait_ms",
            "synchronous_gate_cache_hits", "synchronous_gate_cache_misses",
        }
        self.assertEqual(set(COUNTER_NAMES), expected)


if __name__ == "__main__":
    unittest.main()
