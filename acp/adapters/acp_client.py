"""A host adapter's client for ACP interface v1.

Mirrors `acp.aalp_client.AalpClient` exactly, one layer up: where that
module is the *only* channel ACP itself is permitted to use to reach
AALP, this module is the channel a genuine out-of-process host adapter
(e.g. `acp.adapters.claude_code_bash_mcp`, an MCP server subprocess a
host spawns) uses to reach ACP -- over the same real, authenticated
Unix-domain-socket ingress a live `python -m acp` process publishes
(`acp.serve`/`acp.ingress`), never by importing `acp.coordinator` or any
other in-process module. This keeps a host adapter honest about running
as a separate process talking to ACP's real interface boundary, the same
way ACP itself never reaches into AALP's internals.

All five interface v1 operations `Coordinator` implements are reachable
here except `service.capabilities`/`service.status` (a host adapter has
no need for either): `evaluate` (the synchronous immediate-compression
boundary; see `claude_code_bash_mcp.py`'s module docstring for why no
`PostToolUse`-hook-based boundary exists in real Claude Code, and why an
MCP tool wrapper is the adapter shape instead), plus `prepare`/`resolve`
(background job submission/polling, for proactive/prewarm use from a
`SubagentStop`-style hook), and `store_source` (raw provenance
registration ahead of an `evaluate`/
`prepare` call on the same content, for the cooperative worker-report
pattern -- agent_protocols_v1 background-compression adjustment §29).
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

_MAX_RESPONSE_FRAME_BYTES = 64 * 1024 * 1024
_LENGTH_PREFIX = struct.Struct(">I")


class AcpBootstrapError(RuntimeError):
    """ACP's ingress descriptor or secret could not be read."""


class AcpProtocolError(RuntimeError):
    """ACP responded in a way interface v1 does not allow for."""


@dataclass
class AcpEvaluateResult:
    """Parsed `POST /v1/context/evaluate` response body -- mirrors
    `acp.http_api._serialize_result` field-for-field."""

    outcome: str
    mode: str | None = None
    output: str | None = None
    warnings: list[str] = field(default_factory=list)
    message: str = ""
    provenance: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == "success"


def _recv_exact(sock: socket.socket, n: int, deadline: float) -> bytes:
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
                "ACP closed the connection before sending all expected bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_all(sock: socket.socket, data: bytes, deadline: float) -> None:
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
        raise AcpProtocolError(f"ACP response frame length {length} exceeds {max_frame_bytes}")
    return _recv_exact(sock, length, deadline)


def _write_frame(sock: socket.socket, payload: bytes, deadline: float) -> None:
    _send_all(sock, _LENGTH_PREFIX.pack(len(payload)) + payload, deadline)


class AcpClient:
    """A client for one ACP instance, bootstrapped from its state root.

    `acp_root` is this adapter's own out-of-band knowledge of where ACP
    is rooted (interface v1 defines no discovery operation for an
    unknown root) -- a constructor parameter, not read from `ACP_HOME`
    automatically, mirroring `AalpClient(aalp_root=...)` exactly.
    """

    def __init__(self, acp_root: str | Path) -> None:
        self._acp_root = Path(acp_root)
        self._socket_path: str | None = None
        self._secret: str | None = None

    def _ensure_bootstrapped(self) -> None:
        if self._secret is not None:
            return

        descriptor_path = self._acp_root / ".acp" / "state" / "ingress.json"
        try:
            raw = descriptor_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AcpBootstrapError(
                f"cannot read ACP ingress descriptor at {descriptor_path}: {exc}"
            ) from exc
        try:
            descriptor = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AcpBootstrapError(
                f"ACP ingress descriptor at {descriptor_path} is not valid JSON: {exc}"
            ) from exc
        try:
            socket_path = descriptor["socket_path"]
            secret_file = descriptor["secret_file"]
        except (KeyError, TypeError) as exc:
            raise AcpBootstrapError(
                f"ACP ingress descriptor at {descriptor_path} is malformed: {exc}"
            ) from exc

        secret_path = Path(secret_file)
        try:
            secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AcpBootstrapError(
                f"cannot read ACP ingress secret at {secret_path}: {exc}"
            ) from exc

        self._socket_path = socket_path
        self._secret = secret

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
        self,
        method: str,
        path: str,
        raw_body: bytes,
        *,
        content_type: str,
        timeout: float,
    ) -> tuple[int, bytes]:
        """Send one request envelope over a fresh connection, return
        `(status, response_body_bytes)`. Every public method's own
        exception discipline (see `evaluate`'s docstring) starts here:
        raises `AcpBootstrapError`/`AcpProtocolError`/`socket.timeout`/
        `OSError` on anything short of a well-formed transport-level
        response; a non-2xx/202 `status` is returned, not raised, since
        each caller maps its own operation's status codes."""
        self._ensure_bootstrapped()

        deadline = time.monotonic() + timeout
        sock = self._connect(timeout)
        try:
            envelope = json.dumps({
                "method": method,
                "path": path,
                "headers": {
                    "Authorization": f"Bearer {self._secret}",
                    "Content-Type": content_type,
                },
                "body": base64.b64encode(raw_body).decode("ascii"),
            }).encode("utf-8")
            _write_frame(sock, envelope, deadline)
            response_payload = _read_frame(sock, deadline, _MAX_RESPONSE_FRAME_BYTES)
        finally:
            sock.close()

        try:
            response = json.loads(response_payload.decode("utf-8"))
            status = response["status"]
            raw_response_body = response.get("body") or ""
            response_body = base64.b64decode(raw_response_body) if raw_response_body else b""
        except Exception as exc:
            raise AcpProtocolError(f"malformed ACP response envelope: {exc}") from exc

        if status == 401:
            raise AcpProtocolError("ACP rejected this adapter's bearer secret (401)")

        return status, response_body

    @staticmethod
    def _parse_result_body(response_body: bytes) -> AcpEvaluateResult:
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise AcpProtocolError(f"ACP response body is not valid JSON: {exc}") from exc

        try:
            outcome = parsed["outcome"]
        except (KeyError, TypeError) as exc:
            raise AcpProtocolError(f"ACP response missing 'outcome': {exc}") from exc

        return AcpEvaluateResult(
            outcome=outcome,
            mode=parsed.get("mode"),
            output=parsed.get("output"),
            warnings=list(parsed.get("warnings") or []),
            message=parsed.get("message", ""),
            provenance=parsed.get("provenance"),
        )

    def evaluate(
        self,
        payload: str,
        traffic_class: str,
        receiver: dict[str, str | None],
        *,
        flow_id: str | None = None,
        synchronous_timeout: float | None = None,
        timeout: float = 30.0,
    ) -> AcpEvaluateResult:
        """`POST /v1/context/evaluate`. Raises `AcpBootstrapError`/
        `AcpProtocolError`/`socket.timeout`/`OSError` on anything short of
        a well-formed response -- callers embedding this in a host adapter
        must treat any of those as "ACP unreachable" and fall back to the
        original, uncompressed payload rather than block or corrupt a
        tool result (see `claude_code_bash_mcp.py`)."""
        body: dict[str, Any] = {
            "payload": payload,
            "traffic_class": traffic_class,
            "receiver": receiver,
        }
        if flow_id is not None:
            body["flow_id"] = flow_id
        if synchronous_timeout is not None:
            body["synchronous_timeout"] = synchronous_timeout
        _status, response_body = self._call(
            "POST", "/v1/context/evaluate",
            json.dumps(body).encode("utf-8"),
            content_type="application/json", timeout=timeout,
        )
        return self._parse_result_body(response_body)

    def prepare(
        self,
        payload: str,
        traffic_class: str,
        receiver: dict[str, str | None],
        *,
        flow_id: str | None = None,
        timeout: float = 30.0,
    ) -> str:
        """`POST /v1/context/prepare`. Enqueues a background compression
        job and returns its `job_id` immediately (202) -- never blocks on
        the job itself (a best-effort opportunistic prefetch, e.g. a
        `SubagentStop` cache warm). Same exception discipline as
        `evaluate`: any raised exception means "ACP unreachable," and
        callers here are expected to treat a failed prewarm as a no-op,
        not a fatal error."""
        body: dict[str, Any] = {
            "payload": payload,
            "traffic_class": traffic_class,
            "receiver": receiver,
        }
        if flow_id is not None:
            body["flow_id"] = flow_id
        status, response_body = self._call(
            "POST", "/v1/context/prepare",
            json.dumps(body).encode("utf-8"),
            content_type="application/json", timeout=timeout,
        )
        if status != 202:
            raise AcpProtocolError(f"ACP context.prepare returned unexpected status {status}")
        try:
            parsed = json.loads(response_body)
            return parsed["job_id"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AcpProtocolError(f"ACP context.prepare response missing 'job_id': {exc}") from exc

    def resolve(self, job_id: str, *, timeout: float = 30.0) -> AcpEvaluateResult | None:
        """`GET /v1/context/resolve/{job_id}`. `None` while the job is
        still in flight (or unknown to ACP); a terminal result otherwise.
        Polling cadence and what counts as a "safe boundary" to call this
        at are entirely the caller's decision -- this method only speaks
        the wire protocol."""
        status, response_body = self._call(
            "GET", f"/v1/context/resolve/{job_id}",
            b"", content_type="application/json", timeout=timeout,
        )
        try:
            parsed = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise AcpProtocolError(f"ACP context.resolve response is not valid JSON: {exc}") from exc
        if parsed.get("status") == "pending":
            return None
        return self._parse_result_body(response_body)

    def store_source(self, content: bytes, *, timeout: float = 30.0) -> str:
        """`POST /v1/source/store`. Raw binary body, not JSON -- mirrors
        `_handle_store` in `acp/http_api.py`, which reads the request body
        directly rather than parsing it as a JSON envelope. Returns the
        resulting `source_hash` for later reference (e.g. as
        `prior_provenance.source_hash` on a subsequent `evaluate`/
        `prepare` call for the same content)."""
        status, response_body = self._call(
            "POST", "/v1/source/store", content,
            content_type="application/octet-stream", timeout=timeout,
        )
        if status != 201:
            raise AcpProtocolError(f"ACP source.store returned unexpected status {status}")
        try:
            parsed = json.loads(response_body)
            return parsed["source_hash"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AcpProtocolError(f"ACP source.store response missing 'source_hash': {exc}") from exc
