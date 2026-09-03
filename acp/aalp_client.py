"""ACP's client adapter for AALP interface v1.

This module is the *only* channel through which ACP is permitted to talk to
`agent-api-lane-protocol` (AALP). It depends solely on AALP's published,
versioned interface (`agent-api-lane-protocol/interface/v1/contract.json`
and its README) -- it never imports an `aalp.*` Python module, never
instantiates `Gateway`/`Lane`, never calls an undocumented function, and
never reads AALP's private `.aalp/` on-disk state beyond the two files
interface v1 explicitly publishes for client bootstrap.

No native-inference fallback lives here, and none should ever be added
elsewhere in ACP to route around it: `forward()` always resolves to one of
the seven `Outcome` values (never raises for a classifiable transport or
AALP-reported result), and a non-`SUCCESS` outcome is meant to be a
terminal result for that request. A later wave's compressor orchestration
must call *only* this client for external inference -- if AALP is
unavailable, that is an `UNAVAILABLE`/`UPSTREAM_ERROR`/timeout outcome
surfaced to the caller, not a trigger to fall back to some other inference
path.

Transport: interface v1 speaks a length-prefixed JSON protocol over a Unix
domain socket (see AALP's `aalp/ingress.py` module docstring for the exact
wire format). This replaces an earlier HTTP-over-TCP-loopback transport,
retired after live activation testing found a real, reproducible defect:
`http.client`'s `settimeout()` bounds a single socket syscall, not a whole
multi-syscall read, so a slow multi-packet response delivery could blow
past the configured `compression_timeout` by many seconds even after AALP
itself had the response ready. Every read/write loop below tracks its own
cumulative deadline explicitly (`_recv_exact`/`_send_all`), rather than
trusting one `settimeout()` call to bound work that may take multiple
`recv()`/`send()` calls.
"""
from __future__ import annotations

import base64
import json
import socket
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Outcome

_DISCOVERY_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_FRAME_BYTES = 64 * 1024 * 1024

_LENGTH_PREFIX = struct.Struct(">I")

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


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
    """Read exactly `n` bytes, honoring a cumulative deadline across
    however many individual `recv()` calls it takes -- never a single
    `settimeout()` that only bounds one syscall (see module docstring)."""
    chunks = []
    remaining = n
    while remaining > 0:
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise socket.timeout("cumulative read deadline exceeded")
        sock.settimeout(timeout)
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(
                "AALP closed the connection before sending all expected bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_all(sock: socket.socket, data: bytes, deadline: float) -> None:
    """Write all of `data`, honoring a cumulative deadline across however
    many individual `send()` calls it takes."""
    view = memoryview(data)
    sent = 0
    while sent < len(view):
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise socket.timeout("cumulative write deadline exceeded")
        sock.settimeout(timeout)
        sent += sock.send(view[sent:])


def _read_frame(sock: socket.socket, deadline: float, max_frame_bytes: int) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size, deadline)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > max_frame_bytes:
        raise AalpProtocolError(
            f"AALP response frame length {length} exceeds {max_frame_bytes}")
    return _recv_exact(sock, length, deadline)


def _write_frame(sock: socket.socket, payload: bytes, deadline: float) -> None:
    _send_all(sock, _LENGTH_PREFIX.pack(len(payload)) + payload, deadline)


class AalpClient:
    """A client for one AALP instance, bootstrapped from its root directory.

    `aalp_root` is ACP's own out-of-band knowledge of where AALP is
    rooted (interface v1 defines no discovery operation for an unknown
    root) -- this is deliberately a constructor parameter, not read from
    `AALP_HOME`, which is AALP's own environment variable, not ACP's.
    """

    def __init__(self, aalp_root: str | Path) -> None:
        self._aalp_root = Path(aalp_root)
        self._socket_path: str | None = None
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
            socket_path = descriptor["socket_path"]
            secret_file = descriptor["secret_file"]
        except (KeyError, TypeError) as exc:
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

        self._socket_path = socket_path
        self._secret = secret

    def _auth_header(self) -> str:
        return f"Bearer {self._secret}"

    def _connect(self, timeout: float) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self._socket_path)
        except BaseException:
            sock.close()
            raise
        return sock

    def _call(
        self, method: str, path: str, headers: dict[str, str], body: bytes,
        deadline: float,
    ) -> tuple[int, dict[str, str], bytes]:
        """One request/response round trip: connect, send one framed
        request, read one framed response, close. Raises `socket.timeout`/
        `OSError`/`AalpProtocolError` on failure; never returns partial
        results."""
        remaining = deadline - time.monotonic()
        sock = self._connect(remaining if remaining > 0 else 0)
        try:
            envelope = json.dumps({
                "method": method,
                "path": path,
                "headers": headers,
                "body": base64.b64encode(body).decode("ascii") if body else "",
            }).encode("utf-8")
            _write_frame(sock, envelope, deadline)
            response_payload = _read_frame(sock, deadline, _MAX_RESPONSE_FRAME_BYTES)
        finally:
            sock.close()

        try:
            response = json.loads(response_payload.decode("utf-8"))
            status = response["status"]
            response_headers = dict(response.get("headers") or {})
            raw_body = response.get("body") or ""
            response_body = base64.b64decode(raw_body) if raw_body else b""
        except Exception as exc:
            raise AalpProtocolError(f"malformed AALP response envelope: {exc}") from exc
        return status, response_headers, response_body

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

    def _discovery_request(self, method: str, path: str) -> tuple[int, bytes]:
        deadline = time.monotonic() + _DISCOVERY_TIMEOUT_SECONDS
        try:
            status, _headers, body = self._call(
                method, path, {"Authorization": self._auth_header()}, b"", deadline)
        except (OSError, socket.timeout) as exc:
            raise AalpProtocolError(
                f"cannot reach AALP at {self._socket_path}: {exc}"
            ) from exc
        return status, body

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

        overall_deadline = time.monotonic() + total_timeout

        connect_deadline = min(overall_deadline, time.monotonic() + queue_timeout)
        remaining = connect_deadline - time.monotonic()
        if remaining <= 0:
            return AalpForwardResult(
                Outcome.TOTAL_TIMEOUT, message="total_timeout budget exhausted before connect"
            )
        try:
            sock = self._connect(remaining)
        except socket.timeout:
            if time.monotonic() >= overall_deadline:
                return AalpForwardResult(
                    Outcome.TOTAL_TIMEOUT, message="connect exceeded total_timeout budget"
                )
            return AalpForwardResult(
                Outcome.QUEUE_TIMEOUT, message="connect exceeded queue_timeout budget"
            )
        except OSError as exc:
            return AalpForwardResult(Outcome.UPSTREAM_ERROR, message=str(exc))

        try:
            request_deadline = min(overall_deadline, time.monotonic() + compression_timeout)
            remaining = request_deadline - time.monotonic()
            if remaining <= 0:
                return AalpForwardResult(
                    Outcome.TOTAL_TIMEOUT, message="total_timeout budget exhausted before send"
                )

            envelope = json.dumps({
                "method": method,
                "path": upstream_path,
                "headers": outgoing_headers,
                "body": base64.b64encode(body).decode("ascii") if body else "",
            }).encode("utf-8")

            try:
                _write_frame(sock, envelope, request_deadline)
                response_payload = _read_frame(
                    sock, request_deadline, _MAX_RESPONSE_FRAME_BYTES)
            except (socket.timeout, TimeoutError):
                if time.monotonic() >= overall_deadline:
                    return AalpForwardResult(
                        Outcome.TOTAL_TIMEOUT,
                        message="request/response exceeded total_timeout budget",
                    )
                return AalpForwardResult(
                    Outcome.COMPRESSION_TIMEOUT,
                    message="request/response exceeded compression_timeout budget",
                )
            except (OSError, ConnectionError) as exc:
                return AalpForwardResult(Outcome.UPSTREAM_ERROR, message=str(exc))
        finally:
            sock.close()

        try:
            response = json.loads(response_payload.decode("utf-8"))
            status = response["status"]
            response_headers = dict(response.get("headers") or {})
            raw_body = response.get("body") or ""
            response_body = base64.b64decode(raw_body) if raw_body else b""
        except Exception as exc:  # malformed response framing
            return AalpForwardResult(Outcome.INVALID_RESPONSE, message=str(exc))

        aalp_outcome = response_headers.get("X-Aalp-Outcome")
        if aalp_outcome is None:
            raise AalpProtocolError(
                "AALP response is missing the required X-Aalp-Outcome header "
                "(interface v1, request.forward.response.x_aalp_outcome_header)"
            )

        if aalp_outcome == "success":
            return AalpForwardResult(
                Outcome.SUCCESS,
                status=status,
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
            status=status,
            headers=response_headers,
            body=response_body,
            message=message,
        )
