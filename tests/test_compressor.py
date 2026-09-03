import json
import tempfile
import unittest
from pathlib import Path

import json as _json

from acp.aalp_client import AalpClient
from acp.compressor import Compressor, FailurePolicy, _build_request_body
from acp.errors import Outcome, TrafficClass
from acp.provenance import compute_hash
from acp.telemetry import Telemetry
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

# > 32000 chars -> > 8000 estimated tokens (len // 4) -> GENERAL traffic
# class INSPECT, not BYPASS (bypass_max=8000).
_BIG_PAYLOAD = "log line filler content " * 1600
_BIG_PAYLOAD_B = "a different big payload body " * 1500
_SMALL_PAYLOAD = "tiny payload"


def _compressor_body(mode: str, text: str = "", usage: dict | None = None) -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    obj: dict = {"content": [{"type": "text", "text": content_text}]}
    if usage is not None:
        obj["usage"] = usage
    return json.dumps(obj).encode("utf-8")


class CompressorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.fake = FakeAalpV1(root=self.root).start()
        self.addCleanup(self.fake.stop)
        self.addCleanup(self._tempdir.cleanup)
        self.client = AalpClient(aalp_root=self.root)
        self.telemetry = Telemetry()
        self.compressor = Compressor(self.client, self.telemetry)

    def _add_ci_provider(self, **overrides) -> None:
        defaults = dict(id="ci", display_name="ci", accepted_paths=["/v1/messages"])
        defaults.update(overrides)
        self.fake.add_provider(FakeProvider(**defaults))


class BypassTest(CompressorTestCase):
    def test_bypass_short_circuits_before_any_aalp_call(self) -> None:
        # provider exists but nothing is programmed -- if compress() ever
        # called forward(), the fake's handler would raise LookupError
        # and AalpClient would surface that as AalpProtocolError.
        self._add_ci_provider()
        result = self.compressor.compress(_SMALL_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.mode, "PASS")
        self.assertEqual(result.output, _SMALL_PAYLOAD)
        self.assertEqual(self.telemetry.get("compression_bypass_payloads"), 1)
        self.assertEqual(
            self.telemetry.get("compression_bypass_tokens"), len(_SMALL_PAYLOAD) // 4
        )
        self.assertEqual(self.telemetry.get("compression_attempts"), 0)
        self.assertTrue(result.provenance.processed)


class AntiRecursionTest(CompressorTestCase):
    def test_same_payload_skips_second_aalp_call(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk"),
        )

        first = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(first.outcome, Outcome.SUCCESS)

        # only one response was programmed; a second real call would
        # raise. This must not raise.
        second = self.compressor.compress(
            _BIG_PAYLOAD, TrafficClass.GENERAL, prior_provenance=first.provenance
        )
        self.assertIs(second.outcome, Outcome.SUCCESS)
        self.assertEqual(second.output, _BIG_PAYLOAD)
        self.assertEqual(second.provenance, first.provenance)
        # anti-recursion short-circuit increments no telemetry itself
        self.assertEqual(self.telemetry.get("compression_attempts"), 1)

    def test_new_payload_after_prior_is_reevaluated(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-a"),
        )
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-b"),
        )

        first = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        second = self.compressor.compress(
            _BIG_PAYLOAD_B, TrafficClass.GENERAL, prior_provenance=first.provenance
        )

        self.assertIs(second.outcome, Outcome.SUCCESS)
        self.assertEqual(second.output, "shrunk-b")
        self.assertEqual(second.provenance.generation, first.provenance.generation + 1)
        self.assertEqual(self.telemetry.get("compression_attempts"), 2)


class SuccessParsingTest(CompressorTestCase):
    def test_compact_response_parses_and_updates_telemetry(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body(
                "COMPACT", "compacted text",
                usage={"input_tokens": 10000, "output_tokens": 40},
            ),
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, "compacted text")
        self.assertEqual(self.telemetry.get("compression_successes"), 1)
        self.assertEqual(self.telemetry.get("compression_input_tokens"), 10000)
        self.assertEqual(self.telemetry.get("compression_output_tokens"), 40)
        self.assertEqual(self.telemetry.get("compression_saved_tokens"), 9960)

    def test_compress_response_parses_and_updates_telemetry(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body(
                "COMPRESS", "evidence capsule",
                usage={"input_tokens": 12000, "output_tokens": 200},
            ),
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.mode, "COMPRESS")
        self.assertEqual(result.output, "evidence capsule")
        self.assertEqual(self.telemetry.get("compression_saved_tokens"), 11800)

    def test_thinking_response_skips_leading_empty_and_thinking_blocks(self) -> None:
        # Exact shape observed live against the real `ci` backend with
        # `thinking` enabled during the Phase 6 benchmark: content[0] is
        # an EMPTY text placeholder, content[1] is the thinking block,
        # and the real answer is content[2]. See
        # benchmarks/phase6_effort_thinking_2026-09-03.md.
        self._add_ci_provider()
        obj = {
            "content": [
                {"type": "text", "text": ""},
                {"type": "thinking", "thinking": "reasoning...", "signature": ""},
                {"type": "text", "text": "ACP-MODE: COMPACT\n\ncompacted via thinking"},
            ],
            "usage": {"input_tokens": 9000, "output_tokens": 50},
        }
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_json.dumps(obj).encode("utf-8"),
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.mode, "COMPACT")
        self.assertEqual(result.output, "compacted via thinking")

    def test_usage_absent_falls_back_to_token_estimate(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "abcd" * 10),  # no usage block
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(self.telemetry.get("compression_input_tokens"), len(_BIG_PAYLOAD) // 4)
        self.assertEqual(self.telemetry.get("compression_output_tokens"), 40 // 4)


class PassSubstitutionTest(CompressorTestCase):
    def test_pass_ignores_trailing_garbage_and_returns_original(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("PASS", "some garbage that is NOT the payload"),
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)

        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertEqual(result.mode, "PASS")
        self.assertEqual(result.output, _BIG_PAYLOAD)
        self.assertNotIn("garbage", result.output)


class MalformedResponseTest(CompressorTestCase):
    def test_missing_mode_line_is_invalid_response(self) -> None:
        self._add_ci_provider()
        body = json.dumps({"content": [{"type": "text", "text": "no mode line here"}]}).encode()
        self.fake.program_response("ci", "/v1/messages", outcome="success", body=body)

        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.INVALID_RESPONSE)
        self.assertEqual(result.output, _BIG_PAYLOAD)  # default policy: PASSTHROUGH

    def test_unparseable_json_is_invalid_response(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=b"not json at all"
        )
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.INVALID_RESPONSE)


class FailureOutcomeTest(CompressorTestCase):
    def _program_failure(self, outcome_name: str) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome=outcome_name, message=f"{outcome_name} happened"
        )

    def test_unavailable_passthrough(self) -> None:
        self._program_failure("unavailable")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.UNAVAILABLE)
        self.assertEqual(result.output, _BIG_PAYLOAD)
        self.assertEqual(len(result.warnings), 1)
        self.assertEqual(self.telemetry.get("compression_availability"), 1)
        self.assertEqual(
            self.telemetry.get("compression_timeout_bypass_tokens"), len(_BIG_PAYLOAD) // 4
        )

    def test_queue_timeout_passthrough(self) -> None:
        self._program_failure("queue_timeout")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.QUEUE_TIMEOUT)
        self.assertEqual(self.telemetry.get("compression_queue_timeouts"), 1)

    def test_compression_timeout_passthrough(self) -> None:
        self._program_failure("compression_timeout")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.COMPRESSION_TIMEOUT)
        self.assertEqual(self.telemetry.get("compression_execution_timeouts"), 1)

    def test_total_timeout_passthrough(self) -> None:
        self._program_failure("total_timeout")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.TOTAL_TIMEOUT)
        self.assertEqual(self.telemetry.get("compression_total_timeouts"), 1)

    def test_upstream_error_passthrough(self) -> None:
        self._program_failure("upstream_error")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.UPSTREAM_ERROR)
        self.assertEqual(self.telemetry.get("compression_availability"), 1)

    def test_invalid_response_outcome_from_aalp_passthrough(self) -> None:
        self._program_failure("invalid_response")
        result = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        self.assertIs(result.outcome, Outcome.INVALID_RESPONSE)
        self.assertEqual(self.telemetry.get("compression_availability"), 1)

    def test_block_policy_withholds_original_payload(self) -> None:
        self._program_failure("total_timeout")
        result = self.compressor.compress(
            _BIG_PAYLOAD, TrafficClass.GENERAL, force_policy=FailurePolicy.BLOCK
        )
        self.assertIs(result.outcome, Outcome.TOTAL_TIMEOUT)
        self.assertNotIn(_BIG_PAYLOAD, result.output)
        self.assertIn("blocked", result.output)
        self.assertEqual(self.telemetry.get("compression_timeout_blocked_payloads"), 1)
        self.assertEqual(self.telemetry.get("compression_timeout_bypass_tokens"), 0)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("blocked instead of exposed", result.warnings[0])

    def test_block_via_size_threshold(self) -> None:
        self._program_failure("upstream_error")
        result = self.compressor.compress(
            _BIG_PAYLOAD, TrafficClass.GENERAL, prior_provenance=None,
        )
        # default threshold is None -> PASSTHROUGH even for a big payload
        self.assertEqual(result.output, _BIG_PAYLOAD)


class WarningAggregationTest(CompressorTestCase):
    def test_two_consecutive_failures_warn_once_then_one_recovery(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/messages", outcome="unavailable")
        self.fake.program_response("ci", "/v1/messages", outcome="unavailable")
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success", body=_compressor_body("COMPACT", "ok")
        )

        first = self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        second = self.compressor.compress(
            _BIG_PAYLOAD_B, TrafficClass.GENERAL, prior_provenance=first.provenance
        )
        third = self.compressor.compress(
            "yet another distinct big payload " * 1500, TrafficClass.GENERAL,
            prior_provenance=second.provenance,
        )

        self.assertEqual(len(first.warnings), 1)
        self.assertEqual(len(second.warnings), 0)
        self.assertEqual(len(third.warnings), 1)
        self.assertIn("restored", third.warnings[0])


class ThinkingBudgetRequestBodyTest(unittest.TestCase):
    """`_build_request_body` shape correctness for the new optional
    `thinking` field -- no network call, no AalpClient/Compressor
    involved, matching Phase 6's "benchmark effort/thinking modes" item.
    """

    def test_default_omits_thinking_field(self) -> None:
        body = _json.loads(
            _build_request_body("payload", TrafficClass.GENERAL, None, "m", 512)
        )
        self.assertNotIn("thinking", body)

    def test_explicit_budget_adds_thinking_field(self) -> None:
        body = _json.loads(
            _build_request_body(
                "payload", TrafficClass.GENERAL, None, "m", 512,
                thinking_budget_tokens=1024,
            )
        )
        self.assertEqual(
            body["thinking"], {"type": "enabled", "budget_tokens": 1024}
        )

    def test_max_tokens_raised_above_thinking_budget_when_needed(self) -> None:
        # max_tokens (512) is below thinking_budget_tokens (1024) --
        # Anthropic's Messages API requires max_tokens > budget_tokens,
        # so _build_request_body must raise it, not send an invalid body.
        body = _json.loads(
            _build_request_body(
                "payload", TrafficClass.GENERAL, None, "m", 512,
                thinking_budget_tokens=1024,
            )
        )
        self.assertGreater(body["max_tokens"], 1024)

    def test_max_tokens_left_alone_when_already_sufficient(self) -> None:
        body = _json.loads(
            _build_request_body(
                "payload", TrafficClass.GENERAL, None, "m", 4096,
                thinking_budget_tokens=1024,
            )
        )
        self.assertEqual(body["max_tokens"], 4096)


class CompressorThinkingBudgetWiringTest(CompressorTestCase):
    """`Compressor(thinking_budget_tokens=...)` must actually reach the
    outbound body -- covered end-to-end via the fake AALP fixture rather
    than mocking `_build_request_body`, so a future refactor that breaks
    the wiring (not just the helper) still fails this test."""

    def setUp(self) -> None:
        super().setUp()
        self.compressor = Compressor(
            self.client, self.telemetry, thinking_budget_tokens=1024
        )

    def test_thinking_field_reaches_outbound_request(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "ok"),
        )
        self.compressor.compress(_BIG_PAYLOAD, TrafficClass.GENERAL)
        sent_body = _json.loads(self.fake.last_body)
        self.assertEqual(
            sent_body["thinking"], {"type": "enabled", "budget_tokens": 1024}
        )


if __name__ == "__main__":
    unittest.main()
