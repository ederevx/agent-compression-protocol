import tempfile
import unittest
from pathlib import Path

from acp.aalp_client import AalpClient, AalpProtocolError
from acp.errors import Outcome
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider


class AalpClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tempdir.name)
        self.fake = FakeAalpV1(root=self.root).start()
        self.addCleanup(self.fake.stop)
        self.addCleanup(self._tempdir.cleanup)
        self.client = AalpClient(aalp_root=self.root)

    def _add_ci_provider(self, **overrides) -> None:
        defaults = dict(id="ci", display_name="CI", accepted_paths=["/v1/chat"])
        defaults.update(overrides)
        self.fake.add_provider(FakeProvider(**defaults))


class CapabilitiesTest(AalpClientTestCase):
    def test_successful_bootstrap_and_capabilities(self) -> None:
        caps = self.client.capabilities()
        self.assertEqual(caps["service"], "aalp")
        self.assertEqual(caps["interface_version"], 1)
        self.assertEqual(
            set(caps["capabilities"]),
            {"request.forward", "provider.status", "provider.concurrency", "request.timeout_outcomes"},
        )

    def test_capability_list_is_configurable(self) -> None:
        fake = FakeAalpV1(root=self.root, capabilities=["provider.status"])
        fake.start()
        self.addCleanup(fake.stop)
        client = AalpClient(aalp_root=self.root)
        caps = client.capabilities()
        self.assertEqual(caps["capabilities"], ["provider.status"])

    def test_result_is_cached(self) -> None:
        first = self.client.capabilities()
        self.fake.stop()  # if a second call hit the network, this would now fail
        second = self.client.capabilities()
        self.assertEqual(first, second)

    def test_force_refresh_bypasses_cache(self) -> None:
        self.client.capabilities()
        self.fake.stop()
        with self.assertRaises(AalpProtocolError):
            self.client.capabilities(force_refresh=True)


class ProviderStatusTest(AalpClientTestCase):
    def test_list_providers(self) -> None:
        self._add_ci_provider()
        self.fake.add_provider(FakeProvider(id="other", accepted_paths=["/v1/y"]))
        providers = self.client.provider_status()
        self.assertEqual({p["id"] for p in providers}, {"ci", "other"})

    def test_single_provider(self) -> None:
        self._add_ci_provider(concurrency_limit=3)
        status = self.client.provider_status("ci")
        self.assertEqual(status["id"], "ci")
        self.assertEqual(status["concurrency_limit"], 3)
        self.assertEqual(status["accepted_paths"], ["/v1/chat"])

    def test_unknown_provider_returns_none(self) -> None:
        self.assertIsNone(self.client.provider_status("nope"))


class ForwardSuccessTest(AalpClientTestCase):
    def test_success_passes_through_body_and_headers(self) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/chat", outcome="success", status=201,
            headers={"X-Upstream": "yes"}, body=b'{"ok": true}',
        )
        result = self.client.forward("ci", "POST", "/v1/chat", body=b"{}")
        self.assertIs(result.outcome, Outcome.SUCCESS)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, 201)
        self.assertEqual(result.body, b'{"ok": true}')
        self.assertEqual(result.headers.get("X-Upstream"), "yes")

    def test_flow_id_sent_when_provided(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/chat", outcome="success")
        self.client.forward("ci", "POST", "/v1/chat", flow_id="flow-123")
        self.assertEqual(self.fake.last_headers.get("X-Aalp-Flow-Id"), "flow-123")

    def test_flow_id_omitted_when_not_provided(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/chat", outcome="success")
        self.client.forward("ci", "POST", "/v1/chat")
        self.assertIsNone(self.fake.last_headers.get("X-Aalp-Flow-Id"))


class ForwardNonSuccessOutcomeTest(AalpClientTestCase):
    def _assert_outcome(self, outcome_name: str, expected_status: int) -> None:
        self._add_ci_provider()
        self.fake.program_response(
            "ci", "/v1/chat", outcome=outcome_name, message=f"{outcome_name} happened"
        )
        result = self.client.forward("ci", "POST", "/v1/chat")
        self.assertIs(result.outcome, Outcome[outcome_name.upper()])
        self.assertFalse(result.ok)
        self.assertEqual(result.status, expected_status)
        self.assertEqual(result.message, f"{outcome_name} happened")

    def test_unavailable(self) -> None:
        self._assert_outcome("unavailable", 503)

    def test_queue_timeout(self) -> None:
        self._assert_outcome("queue_timeout", 504)

    def test_compression_timeout_outcome_from_aalp(self) -> None:
        self._assert_outcome("compression_timeout", 504)

    def test_total_timeout_outcome_from_aalp(self) -> None:
        self._assert_outcome("total_timeout", 504)

    def test_invalid_response(self) -> None:
        self._assert_outcome("invalid_response", 502)

    def test_upstream_error(self) -> None:
        self._assert_outcome("upstream_error", 502)

    def test_unavailable_for_unknown_provider_without_programming(self) -> None:
        # no add_provider() call at all -> AALP-side "unavailable", not a
        # LookupError from the fixture's own program_response bookkeeping
        result = self.client.forward("ghost", "GET", "/v1/chat")
        self.assertIs(result.outcome, Outcome.UNAVAILABLE)
        self.assertEqual(result.status, 503)


class AuthTest(AalpClientTestCase):
    def test_correct_secret_succeeds(self) -> None:
        # the positive control: the client's default bootstrap reads and
        # sends the real secret correctly.
        caps = self.client.capabilities()
        self.assertEqual(caps["service"], "aalp")

    def test_wrong_secret_is_rejected(self) -> None:
        self.client._ensure_bootstrapped()
        self.client._secret = "definitely-the-wrong-secret"
        with self.assertRaises(AalpProtocolError):
            self.client.capabilities(force_refresh=True)

    def test_wrong_secret_is_rejected_on_forward(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/chat", outcome="success")
        self.client._ensure_bootstrapped()
        self.client._secret = "definitely-the-wrong-secret"
        with self.assertRaises(AalpProtocolError):
            self.client.forward("ci", "POST", "/v1/chat")


class TimeoutTest(AalpClientTestCase):
    def test_compression_timeout_when_aalp_is_slow(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/chat", outcome="success", delay=1.0)
        result = self.client.forward(
            "ci", "POST", "/v1/chat",
            queue_timeout=2.0, compression_timeout=0.2, total_timeout=5.0,
        )
        self.assertIs(result.outcome, Outcome.COMPRESSION_TIMEOUT)

    def test_total_timeout_when_overall_deadline_is_tighter(self) -> None:
        self._add_ci_provider()
        self.fake.program_response("ci", "/v1/chat", outcome="success", delay=1.0)
        result = self.client.forward(
            "ci", "POST", "/v1/chat",
            queue_timeout=2.0, compression_timeout=5.0, total_timeout=0.2,
        )
        self.assertIs(result.outcome, Outcome.TOTAL_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
