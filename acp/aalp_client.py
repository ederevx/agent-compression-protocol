"""ACP's client adapter for AALP interface v1.

This module is the *only* channel through which ACP is permitted to talk to
`agent-api-lane-protocol` (AALP). It depends solely on AALP's published,
versioned interface (`agent-api-lane-protocol/interface/v1/contract.json`
and its README) — it never imports an `aalp.*` Python module, never
instantiates `Gateway`/`Lane`, never calls an undocumented function, and
never reads AALP's private `.aalp/` on-disk state beyond the two files
interface v1 explicitly publishes for client bootstrap.

No native-inference fallback lives here, and none should ever be added
elsewhere in ACP to route around it: `forward()` always resolves to one of
the seven `Outcome` values (never raises for a classifiable transport or
AALP-reported result), and a non-`SUCCESS` outcome is meant to be a
terminal result for that request. A later wave's compressor orchestration
must call *only* this client for external inference — if AALP is
unavailable, that is an `UNAVAILABLE`/`UPSTREAM_ERROR`/timeout outcome
surfaced to the caller, not a trigger to fall back to some other inference
path.
"""
from __future__ import annotations

import http.client
import json
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Outcome

_DISCOVERY_TIMEOUT_SECONDS = 10.0

_NON_SUCCESS_OUTCOMES: dict[str, Outcome] = {
    "unavailable": Outcome.UNAVAILABLE,
    "queue_timeout": Outcome.QUEUE_TIMEOUT,
    "compression_timeout": Outcome.COMPRESSION_TIMEOUT,
    "total_timeout": Outcome.TOTAL_TIMEOUT,
    "invalid_response": Outcome.INVALID_RESPONSE,
    "upstream_error": Outcome.UPSTREAM_ERROR,
}


class AalpBootstrapError(RuntimeError):
    """AALP's bootstrap descriptor or secret could not be read."""


class AalpProtocolError(RuntimeError):
    """AALP responded in a way interface v1 does not allow for."""


@dataclass
class AalpForwardResult:
    """The result of one `request.forward` call.

    `status`/`headers`/`body` are the passthrough upstream response on
    `SUCCESS`, or AALP's small synthesized JSON error body otherwise
    (already parsed into `message` for convenience).
    """

    outcome: Outcome
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.SUCCESS


class AalpClient:
    """A client for one AALP instance, bootstrapped from its root directory.

    `aalp_root` is ACP's own out-of-band knowledge of where AALP is
    rooted (interface v1 defines no discovery operation for an unknown
    root) — this is deliberately a constructor parameter, not read from
    `AALP_HOME`, which is AALP's own environment variable, not ACP's.
    """

    def __init__(self, aalp_root: str | Path) -> None:
        self._aalp_root = Path(aalp_root)
        self._host: str | None = None
        self._port: int | None = None
        self._secret: str | None = None
        self._capabilities: dict[str, Any] | None = None

    # -- bootstrap (interface v1's one narrow exception: exactly these
    # two files under `.aalp/`, nothing else) -----------------------------

    def _ensure_bootstrapped(self) -> None:
        if self._secret is not None:
            return

        descriptor_path = self._aalp_root / ".aalp" / "state" / "ingress.json"
        try:
            raw = descriptor_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AalpBootstrapError(
                f"cannot read AALP ingress descriptor at {descriptor_path}: {exc}"
            ) from exc
        try:
            descriptor = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AalpBootstrapError(
                f"AALP ingress descriptor at {descriptor_path} is not valid JSON: {exc}"
            ) from exc
        try:
            host = descriptor["host"]
            port = int(descriptor["port"])
            secret_file = descriptor["secret_file"]
        except (KeyError, TypeError, ValueError) as exc:
            raise AalpBootstrapError(
                f"AALP ingress descriptor at {descriptor_path} is malformed: {exc}"
            ) from exc

        secret_path = Path(secret_file)
        try:
            # AALP's own reader strips the secret file's contents (it may or
            # may not carry a trailing newline per interface v1); match that
            # so the token compared against the server's in-memory secret is
            # identical either way.
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AalpBootstrapError(
                f"cannot read AALP ingress secret at {secret_path}: {exc}"
            ) from exc

        self._host = host
        self._port = port
        self._secret = secret

    def _auth_header(self) -> str:
        return f"Bearer {self._secret}"

    # -- service.capabilities ---------------------------------------------

    def capabilities(self, *, force_refresh: bool = False) -> dict[str, Any]:
        """`GET /_aalp/v1/capabilities`. Cached in-memory after the first call."""
        if self._capabilities is not None and not force_refresh:
            return self._capabilities

        self._ensure_bootstrapped()
        status, body = self._discovery_request("GET", "/_aalp/v1/capabilities")
        if status != 200:
            raise AalpProtocolError(
                f"service.capabilities returned unexpected status {status}"
            )
        data = self._parse_json(body, context="service.capabilities")
        self._capabilities = data
        return data

    # -- provider.status ----------------------------------------------------

    def provider_status(self, provider_id: str | None = None) -> Any:
        """`GET /_aalp/v1/providers[/{provider_id}]`.

        Returns the list of provider-status objects when `provider_id` is
        omitted, a single provider-status object when it is given and
        known, or `None` when it is given and unknown (AALP's 404).
        """
        self._ensure_bootstrapped()
        if provider_id is None:
            status, body = self._discovery_request("GET", "/_aalp/v1/providers")
            if status != 200:
                raise AalpProtocolError(
                    f"provider.status (list) returned unexpected status {status}"
                )
            data = self._parse_json(body, context="provider.status")
            return data["providers"]

        status, body = self._discovery_request(
            "GET", f"/_aalp/v1/providers/{provider_id}"
        )
        if status == 404:
            return None
        if status != 200:
            raise AalpProtocolError(
                f"provider.status({provider_id!r}) returned unexpected status {status}"
            )
        return self._parse_json(body, context="provider.status")

    def _discovery_request(self, method: str, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection(
            self._host, self._port, timeout=_DISCOVERY_TIMEOUT_SECONDS
        )
        try:
            conn.request(method, path, headers={"Authorization": self._auth_header()})
            response = conn.getresponse()
            body = response.read()
        except OSError as exc:
            raise AalpProtocolError(
                f"cannot reach AALP at {self._host}:{self._port}: {exc}"
            ) from exc
        finally:
            conn.close()
        return response.status, body

    @staticmethod
    def _parse_json(body: bytes, *, context: str) -> Any:
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AalpProtocolError(f"{context} returned invalid JSON: {exc}") from exc

    # -- request.forward ------------------------------------------------

    def forward(
        self,
        provider_id: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        flow_id: str | None = None,
        queue_timeout: float = 5.0,
        compression_timeout: float = 30.0,
        total_timeout: float = 60.0,
    ) -> AalpForwardResult:
        """`{method} /{provider_id}/{path}`.

        `queue_timeout`, `compression_timeout`, and `total_timeout` are
        ACP-side budgets for this whole round-trip to AALP -- distinct
        from, and layered on top of, AALP's own internal admission/upstream
        timeout handling (which produces the AALP-reported `queue_timeout`
        / `compression_timeout` / `total_timeout` outcomes below).
        `queue_timeout` bounds connecting to AALP's ingress, `compression_
        timeout` bounds one already-connected send/receive attempt, and
        `total_timeout` bounds the call as a whole; whichever budget is
        exhausted first determines the local `Outcome`, and if the overall
        deadline is what actually ran out, that always wins as
        `Outcome.TOTAL_TIMEOUT` even if a narrower phase budget also
        elapsed at essentially the same moment.
        """
        self._ensure_bootstrapped()

        outgoing_headers = dict(headers or {})
        outgoing_headers["Authorization"] = self._auth_header()
        if flow_id is not None:
            outgoing_headers["X-Aalp-Flow-Id"] = flow_id

        upstream_path = "/" + provider_id + (path if path.startswith("/") else "/" + path)

        deadline = time.monotonic() + total_timeout
        conn = http.client.HTTPConnection(self._host, self._port)
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return AalpForwardResult(
                    Outcome.TOTAL_TIMEOUT, message="total_timeout budget exhausted before connect"
                )
            conn.timeout = min(queue_timeout, remaining)
            try:
                conn.connect()
            except (socket.timeout, TimeoutError):
                if time.monotonic() >= deadline:
                    return AalpForwardResult(
                        Outcome.TOTAL_TIMEOUT, message="connect exceeded total_timeout budget"
                    )
                return AalpForwardResult(
                    Outcome.QUEUE_TIMEOUT, message="connect exceeded queue_timeout budget"
                )
            except OSError as exc:
                return AalpForwardResult(Outcome.UPSTREAM_ERROR, message=str(exc))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return AalpForwardResult(
                    Outcome.TOTAL_TIMEOUT, message="total_timeout budget exhausted before send"
                )
            conn.sock.settimeout(min(compression_timeout, remaining))

            try:
                conn.request(method, upstream_path, body=body, headers=outgoing_headers)
                response = conn.getresponse()
            except (socket.timeout, TimeoutError):
                if time.monotonic() >= deadline:
                    return AalpForwardResult(
                        Outcome.TOTAL_TIMEOUT,
                        message="request/response exceeded total_timeout budget",
                    )
                return AalpForwardResult(
                    Outcome.COMPRESSION_TIMEOUT,
                    message="request/response exceeded compression_timeout budget",
                )
            except (OSError, http.client.HTTPException) as exc:
                return AalpForwardResult(Outcome.UPSTREAM_ERROR, message=str(exc))

            try:
                response_body = response.read()
            except (socket.timeout, TimeoutError):
                if time.monotonic() >= deadline:
                    return AalpForwardResult(
                        Outcome.TOTAL_TIMEOUT,
                        message="response read exceeded total_timeout budget",
                    )
                return AalpForwardResult(
                    Outcome.COMPRESSION_TIMEOUT,
                    message="response read exceeded compression_timeout budget",
                )
            except Exception as exc:  # malformed response body framing
                return AalpForwardResult(Outcome.INVALID_RESPONSE, message=str(exc))
        finally:
            conn.close()

        aalp_outcome = response.getheader("X-Aalp-Outcome")
        if aalp_outcome is None:
            raise AalpProtocolError(
                "AALP response is missing the required X-Aalp-Outcome header "
                "(interface v1, request.forward.response.x_aalp_outcome_header)"
            )

        response_headers = dict(response.getheaders())

        if aalp_outcome == "success":
            return AalpForwardResult(
                Outcome.SUCCESS,
                status=response.status,
                headers=response_headers,
                body=response_body,
            )

        outcome = _NON_SUCCESS_OUTCOMES.get(aalp_outcome)
        if outcome is None:
            raise AalpProtocolError(f"AALP reported an unknown outcome {aalp_outcome!r}")

        message = ""
        try:
            parsed = json.loads(response_body)
            if isinstance(parsed, dict):
                message = parsed.get("message", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        return AalpForwardResult(
            outcome,
            status=response.status,
            headers=response_headers,
            body=response_body,
            message=message,
        )
