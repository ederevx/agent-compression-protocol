import unittest

from acp.errors import AcpResult, Outcome, TrafficClass


class OutcomeTest(unittest.TestCase):
    def test_exactly_eight_outcomes(self) -> None:
        names = {member.name for member in Outcome}
        self.assertEqual(
            names,
            {
                "SUCCESS", "UNAVAILABLE", "QUEUE_TIMEOUT", "COMPRESSION_TIMEOUT",
                "TOTAL_TIMEOUT", "INVALID_RESPONSE", "UPSTREAM_ERROR",
                "MAINTENANCE",
            },
        )


class TrafficClassTest(unittest.TestCase):
    def test_exactly_three_traffic_classes(self) -> None:
        names = {member.name for member in TrafficClass}
        self.assertEqual(
            names, {"GENERAL", "NATIVE_AGENT_REPORT", "DOWNWARD_CONTEXT"})


class AcpResultTest(unittest.TestCase):
    def test_ok_true_on_success(self) -> None:
        result = AcpResult(outcome=Outcome.SUCCESS)
        self.assertTrue(result.ok)

    def test_ok_false_on_non_success(self) -> None:
        result = AcpResult(outcome=Outcome.UPSTREAM_ERROR)
        self.assertFalse(result.ok)

    def test_defaults(self) -> None:
        result = AcpResult(outcome=Outcome.SUCCESS)
        self.assertIsNone(result.mode)
        self.assertIsNone(result.output)
        self.assertEqual(result.warnings, [])
        self.assertEqual(result.message, "")

    def test_warnings_default_not_shared_between_instances(self) -> None:
        first = AcpResult(outcome=Outcome.SUCCESS)
        second = AcpResult(outcome=Outcome.SUCCESS)
        first.warnings.append("x")
        self.assertEqual(second.warnings, [])


if __name__ == "__main__":
    unittest.main()
