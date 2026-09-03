"""End-to-end tests for ACP interface v1 over a real loopback Unix socket.

Exercises `acp.serve.build_ingress` the same way `agent-api-lane-protocol`'s
own `tests/test_serve.py` exercises `aalp.serve.build_ingress`: a real
`Coordinator` wired to a real loopback `Ingress`, reachable only through
interface v1's own length-prefixed-JSON-over-`AF_UNIX` surface -- the same
path a genuine out-of-process client (a future host adapter) would take.
Real sockets throughout, no mocking of `acp.ingress`.
"""
from __future__ import annotations

import base64
import json
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from acp import containment
from acp.provenance import compute_hash
from acp.serve import build_ingress
from tests.fixtures.fake_aalp_v1 import FakeAalpV1, FakeProvider

_TIMEOUT = 5.0
_LENGTH_PREFIX = struct.Struct(">I")


class _FakeHTTPResponse:
    """A minimal stand-in for `http.client.HTTPResponse` covering the two
    members this test module's assertions use, so test bodies below did
    not need to change when the transport moved off HTTP."""

    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._headers = headers
        self.body = body

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)

# > 32000 chars -> > 8000 estimated tokens (len // 4) -> GENERAL traffic
# class INSPECT, not BYPASS (bypass_max=8000). Mirrors tests/test_coordinator.py.
_BIG_PAYLOAD = "log line filler content " * 1600
_SMALL_PAYLOAD = "a short payload well under the bypass threshold"


def _compressor_body(mode: str, text: str = "", usage: dict | None = None) -> bytes:
    content_text = f"ACP-MODE: {mode}"
    if text or mode != "PASS":
        content_text += "\n\n" + text
    obj: dict = {"content": [{"type": "text", "text": content_text}]}
    if usage is not None:
        obj["usage"] = usage
    return json.dumps(obj).encode("utf-8")


class AcpServeEndToEndTest(unittest.TestCase):
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

        self.ingress = build_ingress(
            aalp_root=self.aalp_root, root=self.acp_root
        )
        self.ingress.start()
        self.addCleanup(self.ingress.stop)

    # -- socket helpers -----------------------------------------------------

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes:
        chunks = []
        remaining = n
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ConnectionError("peer closed before sending all expected bytes")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _call(
        self, method: str, path: str, headers: dict | None = None, body: bytes = b""
    ) -> _FakeHTTPResponse:
        envelope = json.dumps({
            "method": method,
            "path": path,
            "headers": headers or {},
            "body": base64.b64encode(body).decode("ascii") if body else "",
        }).encode("utf-8")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_TIMEOUT)
        try:
            sock.connect(str(self.ingress.socket_path))
            sock.sendall(_LENGTH_PREFIX.pack(len(envelope)) + envelope)
            header = self._recv_exact(sock, _LENGTH_PREFIX.size)
            (length,) = _LENGTH_PREFIX.unpack(header)
            payload = self._recv_exact(sock, length)
        finally:
            sock.close()
        response = json.loads(payload.decode("utf-8"))
        raw_body = response.get("body") or ""
        response_body = base64.b64decode(raw_body) if raw_body else b""
        return _FakeHTTPResponse(response["status"], dict(response.get("headers") or {}), response_body)

    def _auth_headers(self, extra: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.ingress.secret}"}
        if extra:
            headers.update(extra)
        return headers

    def _get(self, path: str, *, authorized: bool = True) -> tuple[_FakeHTTPResponse, bytes]:
        headers = self._auth_headers() if authorized else {}
        response = self._call("GET", path, headers)
        return response, response.body

    def _post(
        self, path: str, body: bytes = b"", *, authorized: bool = True,
        content_type: str = "application/json",
    ) -> tuple[_FakeHTTPResponse, bytes]:
        extra = {"Content-Type": content_type}
        headers = self._auth_headers(extra) if authorized else extra
        response = self._call("POST", path, headers, body)
        return response, response.body

    def _post_json(self, path: str, obj: dict, **kwargs) -> tuple[_FakeHTTPResponse, dict]:
        response, body = self._post(path, json.dumps(obj).encode("utf-8"), **kwargs)
        return response, json.loads(body)

    def _wait_until_resolved(self, job_id: str, timeout: float = 5.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response, body = self._get(f"/v1/context/resolve/{job_id}")
            parsed = json.loads(body)
            if parsed.get("status") != "pending":
                return parsed
            time.sleep(0.02)
        self.fail(f"job {job_id} never reached a resolvable terminal state")


class CapabilitiesAndStatusTest(AcpServeEndToEndTest):
    def test_capabilities_reachable(self) -> None:
        response, body = self._get("/v1/service/capabilities")
        self.assertEqual(response.status, 200)
        parsed = json.loads(body)
        self.assertEqual(parsed["service"], "acp")
        self.assertEqual(parsed["interface_version"], 1)
        self.assertIn("context.evaluate", parsed["capabilities"])
        self.assertIn("service.status", parsed["capabilities"])

    def test_status_reflects_job_after_evaluate_and_never_leaks_secrets(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk"),
        )
        response, evaluate_body = self._post_json(
            "/v1/context/evaluate",
            {
                "payload": _BIG_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(evaluate_body["outcome"], "success")

        status_response, status_body = self._get("/v1/service/status")
        self.assertEqual(status_response.status, 200)
        parsed = json.loads(status_body)
        self.assertIn("jobs_by_state", parsed)
        self.assertGreaterEqual(sum(parsed["jobs_by_state"].values()), 1)
        self.assertTrue(parsed["aalp_reachable"])

        dumped = json.dumps(parsed)
        self.assertNotIn(self.fake.secret, dumped)
        self.assertNotIn(self.ingress.secret, dumped)
        self.assertNotIn(_BIG_PAYLOAD[:80], dumped)


class TelemetryTest(AcpServeEndToEndTest):
    def test_capabilities_lists_telemetry(self) -> None:
        response, body = self._get("/v1/service/capabilities")
        self.assertEqual(response.status, 200)
        parsed = json.loads(body)
        self.assertIn("service.telemetry", parsed["capabilities"])

    def test_unauthorized_401s(self) -> None:
        response, _ = self._get("/v1/service/telemetry", authorized=False)
        self.assertEqual(response.status, 401)

    def test_counters_start_at_zero_for_a_fresh_coordinator(self) -> None:
        response, body = self._get("/v1/service/telemetry")
        self.assertEqual(response.status, 200)
        parsed = json.loads(body)
        counters = parsed["counters"]
        self.assertEqual(counters["compression_attempts"], 0)

    def test_real_compression_event_through_live_path_is_observable(self) -> None:
        # A real INSPECT-mode round-trip through the live evaluate path
        # (same fake-AALP convention as every other test in this module) --
        # this is what §21's live-deployed-system observability gap
        # requires: a client reading /v1/service/telemetry after the fact,
        # not just the increment call being unit-tested in isolation.
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk"),
        )
        evaluate_response, evaluate_body = self._post_json(
            "/v1/context/evaluate",
            {
                "payload": _BIG_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(evaluate_response.status, 200)
        self.assertEqual(evaluate_body["outcome"], "success")

        response, body = self._get("/v1/service/telemetry")
        self.assertEqual(response.status, 200)
        parsed = json.loads(body)
        counters = parsed["counters"]
        self.assertEqual(counters["compression_attempts"], 1)
        self.assertEqual(counters["compression_successes"], 1)
        self.assertGreater(counters["compression_input_tokens"], 0)

        dumped = json.dumps(parsed)
        self.assertNotIn(self.fake.secret, dumped)
        self.assertNotIn(self.ingress.secret, dumped)


class EvaluateTest(AcpServeEndToEndTest):
    def test_evaluate_bypass_small_payload_needs_no_aalp_call(self) -> None:
        # No response programmed on the fake -- if ACP called AALP anyway,
        # the fake would raise LookupError inside its handler thread.
        response, body = self._post_json(
            "/v1/context/evaluate",
            {
                "payload": _SMALL_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Acp-Outcome"), "success")
        self.assertEqual(body["outcome"], "success")
        self.assertEqual(body["mode"], "PASS")
        self.assertEqual(body["output"], _SMALL_PAYLOAD)

    def test_evaluate_inspect_big_payload_real_roundtrip(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "shrunk-output"),
        )
        response, body = self._post_json(
            "/v1/context/evaluate",
            {
                "payload": _BIG_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Acp-Outcome"), "success")
        self.assertEqual(body["outcome"], "success")
        self.assertEqual(body["mode"], "COMPACT")
        self.assertEqual(body["output"], "shrunk-output")
        self.assertIsNotNone(body["provenance"])
        self.assertEqual(body["provenance"]["processed"], True)

    def test_evaluate_non_success_outcome_maps_status_and_header(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="total_timeout", message="boom"
        )
        response, body = self._post_json(
            "/v1/context/evaluate",
            {
                "payload": _BIG_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(response.status, 504)
        self.assertEqual(response.getheader("X-Acp-Outcome"), "total_timeout")
        self.assertEqual(body["outcome"], "total_timeout")


class PrepareResolveTest(AcpServeEndToEndTest):
    def test_prepare_then_resolve_pending_then_ready(self) -> None:
        self.fake.program_response(
            "ci", "/v1/messages", outcome="success",
            body=_compressor_body("COMPACT", "prepared-output"), delay=0.2,
        )
        response, body = self._post_json(
            "/v1/context/prepare",
            {
                "payload": _BIG_PAYLOAD,
                "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            },
        )
        self.assertEqual(response.status, 202)
        job_id = body["job_id"]

        pending_response, pending_body = self._get(f"/v1/context/resolve/{job_id}")
        self.assertEqual(pending_response.status, 200)
        self.assertEqual(json.loads(pending_body), {"status": "pending"})

        final = self._wait_until_resolved(job_id)
        self.assertEqual(final["outcome"], "success")
        self.assertEqual(final["output"], "prepared-output")


class SourceStoreTest(AcpServeEndToEndTest):
    def test_raw_body_round_trips_to_matching_source_hash(self) -> None:
        content = b"raw source bytes for http round-trip"
        response, body = self._post(
            "/v1/source/store", content, content_type="application/octet-stream"
        )
        self.assertEqual(response.status, 201)
        parsed = json.loads(body)
        expected_hash = compute_hash(content)
        self.assertEqual(parsed["source_hash"], expected_hash)
        self.assertEqual(
            containment.read_raw(self.acp_root, expected_hash), content
        )


class AuthenticationTest(AcpServeEndToEndTest):
    def test_every_endpoint_401s_without_correct_secret(self) -> None:
        get_response, _ = self._get("/v1/service/capabilities", authorized=False)
        self.assertEqual(get_response.status, 401)

        status_response, _ = self._get("/v1/service/status", authorized=False)
        self.assertEqual(status_response.status, 401)

        evaluate_response, _ = self._post(
            "/v1/context/evaluate",
            json.dumps({
                "payload": _SMALL_PAYLOAD, "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            }).encode("utf-8"),
            authorized=False,
        )
        self.assertEqual(evaluate_response.status, 401)

        prepare_response, _ = self._post(
            "/v1/context/prepare",
            json.dumps({
                "payload": _SMALL_PAYLOAD, "traffic_class": "general",
                "receiver": {"host": "h", "session_id": "s", "agent_id": None},
            }).encode("utf-8"),
            authorized=False,
        )
        self.assertEqual(prepare_response.status, 401)

        resolve_response, _ = self._get("/v1/context/resolve/nonexistent", authorized=False)
        self.assertEqual(resolve_response.status, 401)

        store_response, _ = self._post(
            "/v1/source/store", b"data", authorized=False,
            content_type="application/octet-stream",
        )
        self.assertEqual(store_response.status, 401)


class IngressDescriptorTest(AcpServeEndToEndTest):
    def test_descriptor_and_secret_written_under_configured_root(self) -> None:
        descriptor_path = self.acp_root / ".acp" / "state" / "ingress.json"
        self.assertTrue(descriptor_path.exists())
        descriptor = json.loads(descriptor_path.read_text())
        self.assertEqual(descriptor["socket_path"], str(self.ingress.socket_path))

        secret_path = self.acp_root / ".acp" / "state" / "ingress.secret"
        self.assertTrue(secret_path.exists())
        self.assertEqual(secret_path.read_text().strip(), self.ingress.secret)


if __name__ == "__main__":
    unittest.main()
