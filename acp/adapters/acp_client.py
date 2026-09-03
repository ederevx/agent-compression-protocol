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

Only `context.evaluate` is implemented -- the one operation a host
adapter needs for its immediate-compression boundary (see
`claude_code_bash_mcp.py`'s module docstring for why no
`PostToolUse`-hook-based boundary exists in real Claude Code, and why an
MCP tool wrapper is the adapter shape instead).
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
        self._ensure_bootstrapped()

        deadline = time.monotonic() + timeout
        sock = self._connect(timeout)
        try:
            body: dict[str, Any] = {
                "payload": payload,
                "traffic_class": traffic_class,
                "receiver": receiver,
            }
            if flow_id is not None:
                body["flow_id"] = flow_id
            if synchronous_timeout is not None:
                body["synchronous_timeout"] = synchronous_timeout
            envelope = json.dumps({
                "method": "POST",
                "path": "/v1/context/evaluate",
                "headers": {
                    "Authorization": f"Bearer {self._secret}",
                    "Content-Type": "application/json",
                },
                "body": base64.b64encode(json.dumps(body).encode("utf-8")).decode("ascii"),
            }).encode("utf-8")
            _write_frame(sock, envelope, deadline)
            response_payload = _read_frame(sock, deadline, _MAX_RESPONSE_FRAME_BYTES)
        finally:
            sock.close()

        try:
            response = json.loads(response_payload.decode("utf-8"))
            status = response["status"]
            raw_body = response.get("body") or ""
            response_body = base64.b64decode(raw_body) if raw_body else b""
        except Exception as exc:
            raise AcpProtocolError(f"malformed ACP response envelope: {exc}") from exc

        if status == 401:
            raise AcpProtocolError("ACP rejected this adapter's bearer secret (401)")

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
