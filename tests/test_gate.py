import unittest

from acp.errors import TrafficClass
from acp.gate import (
    DEFAULT_THRESHOLDS,
    GateAction,
    ThresholdBand,
    TrafficClassThresholds,
    estimate_tokens,
    evaluate,
)


def _text_of_tokens(token_count: int) -> str:
    """Build text whose estimate_tokens() is exactly `token_count`."""
    return "a" * (token_count * 4)


class EstimateTokensTest(unittest.TestCase):
    def test_four_chars_per_token(self) -> None:
        self.assertEqual(estimate_tokens("a" * 4000), 1000)

    def test_empty_string(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_rounds_down(self) -> None:
        self.assertEqual(estimate_tokens("abc"), 0)


class GeneralTrafficTest(unittest.TestCase):
    def test_bypass_under_8k(self) -> None:
        decision = evaluate(_text_of_tokens(7_999), TrafficClass.GENERAL)
        self.assertIs(decision.action, GateAction.BYPASS)
        self.assertIsNone(decision.reduction_hint)

    def test_bypass_at_exact_8k_boundary(self) -> None:
        decision = evaluate(_text_of_tokens(8_000), TrafficClass.GENERAL)
        self.assertIs(decision.action, GateAction.BYPASS)

    def test_inspect_just_above_8k_no_hint(self) -> None:
        decision = evaluate(_text_of_tokens(8_001), TrafficClass.GENERAL)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertIsNone(decision.reduction_hint)

    def test_inspect_at_24k_boundary_still_no_hint(self) -> None:
        decision = evaluate(_text_of_tokens(24_000), TrafficClass.GENERAL)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertIsNone(decision.reduction_hint)

    def test_compact_preferred_just_above_24k(self) -> None:
        decision = evaluate(_text_of_tokens(24_001), TrafficClass.GENERAL)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertEqual(decision.reduction_hint, "compact_preferred")

    def test_compact_preferred_at_50k_boundary(self) -> None:
        decision = evaluate(_text_of_tokens(50_000), TrafficClass.GENERAL)
        self.assertEqual(decision.reduction_hint, "compact_preferred")

    def test_reduction_required_above_50k(self) -> None:
        decision = evaluate(_text_of_tokens(50_001), TrafficClass.GENERAL)
        self.assertEqual(decision.reduction_hint, "reduction_required")

    def test_estimated_tokens_reported(self) -> None:
        decision = evaluate(_text_of_tokens(100_000), TrafficClass.GENERAL)
        self.assertEqual(decision.estimated_tokens, 100_000)

    def test_traffic_class_reported(self) -> None:
        decision = evaluate(_text_of_tokens(1), TrafficClass.GENERAL)
        self.assertIs(decision.traffic_class, TrafficClass.GENERAL)


class NativeAgentReportTest(unittest.TestCase):
    def test_bypass_under_4k(self) -> None:
        decision = evaluate(
            _text_of_tokens(3_999), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertIs(decision.action, GateAction.BYPASS)

    def test_bypass_at_4k_boundary(self) -> None:
        decision = evaluate(
            _text_of_tokens(4_000), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertIs(decision.action, GateAction.BYPASS)

    def test_inspect_no_hint_between_4k_and_8k(self) -> None:
        decision = evaluate(
            _text_of_tokens(8_000), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertIsNone(decision.reduction_hint)

    def test_reduction_required_just_above_8k(self) -> None:
        decision = evaluate(
            _text_of_tokens(8_001), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertEqual(decision.reduction_hint, "reduction_required")

    def test_reduction_required_at_20k_boundary(self) -> None:
        decision = evaluate(
            _text_of_tokens(20_000), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertEqual(decision.reduction_hint, "reduction_required")

    def test_aggressive_reduction_above_20k(self) -> None:
        decision = evaluate(
            _text_of_tokens(20_001), TrafficClass.NATIVE_AGENT_REPORT)
        self.assertEqual(decision.reduction_hint, "aggressive_reduction_required")


class DownwardContextTest(unittest.TestCase):
    def test_more_conservative_bypass_than_general(self) -> None:
        general_default = DEFAULT_THRESHOLDS[TrafficClass.GENERAL]
        downward_default = DEFAULT_THRESHOLDS[TrafficClass.DOWNWARD_CONTEXT]
        self.assertGreater(
            downward_default.bypass_max, general_default.bypass_max)

    def test_more_conservative_bypass_than_native_agent_report(self) -> None:
        native_default = DEFAULT_THRESHOLDS[TrafficClass.NATIVE_AGENT_REPORT]
        downward_default = DEFAULT_THRESHOLDS[TrafficClass.DOWNWARD_CONTEXT]
        self.assertGreater(downward_default.bypass_max, native_default.bypass_max)

    def test_bypass_under_threshold(self) -> None:
        downward_default = DEFAULT_THRESHOLDS[TrafficClass.DOWNWARD_CONTEXT]
        decision = evaluate(
            _text_of_tokens(downward_default.bypass_max),
            TrafficClass.DOWNWARD_CONTEXT,
        )
        self.assertIs(decision.action, GateAction.BYPASS)

    def test_inspect_above_threshold(self) -> None:
        downward_default = DEFAULT_THRESHOLDS[TrafficClass.DOWNWARD_CONTEXT]
        decision = evaluate(
            _text_of_tokens(downward_default.bypass_max + 1),
            TrafficClass.DOWNWARD_CONTEXT,
        )
        self.assertIs(decision.action, GateAction.INSPECT)


class OverrideTest(unittest.TestCase):
    def test_custom_thresholds_argument_overrides_defaults(self) -> None:
        custom = {
            TrafficClass.GENERAL: TrafficClassThresholds(
                bypass_max=10,
                bands=(ThresholdBand(upper=None, hint="reduction_required"),),
            ),
        }
        decision = evaluate(
            _text_of_tokens(11), TrafficClass.GENERAL, thresholds=custom)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertEqual(decision.reduction_hint, "reduction_required")

        bypassed = evaluate(
            _text_of_tokens(10), TrafficClass.GENERAL, thresholds=custom)
        self.assertIs(bypassed.action, GateAction.BYPASS)

    def test_custom_token_estimator(self) -> None:
        decision = evaluate(
            "irrelevant", TrafficClass.GENERAL, token_estimator=lambda _t: 999_999)
        self.assertIs(decision.action, GateAction.INSPECT)
        self.assertEqual(decision.estimated_tokens, 999_999)


if __name__ == "__main__":
    unittest.main()
