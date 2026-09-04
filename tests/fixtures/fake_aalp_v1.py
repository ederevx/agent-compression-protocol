"""A local, loopback fake of AALP interface v1 for ACP's client tests.

Built directly from `agent-api-lane-protocol/interface/v1/contract.json`
(the interface's own authoritative schema) -- not by importing `aalp.*` or
any of AALP's own fixtures. `AalpClient`'s test suite must never depend on
AALP's real source or a real running AALP process; this is what it talks
to instead.

Usage:

    with FakeAalpV1(root=some_tempdir) as fake:
        fake.add_provider(FakeProvider(id="ci", accepted_paths=["/v1/x"]))
        fake.program_response("ci", "/v1/x", outcome="success", body=b"ok")
        client = AalpClient(aalp_root=some_tempdir)
        ...

Starting the fake writes real `.aalp/state/ingress.json` and
`.aalp/state/ingress.secret` files under `root`, so a client's bootstrap
step is exercised against real files, not a mock.

Speaks the same Unix-domain-socket, length-prefixed-JSON wire protocol as
the real `aalp/ingress.py` (see its module docstring for the exact
format) -- this fake was updated in lockstep with AALP's real ingress
when interface v1's transport moved off HTTP, for the same reason: a
socket read/write bounded by a single `settimeout()` call, rather than a
cumulative deadline across every `recv()`/`send()` it takes, can silently
let a slow multi-packet delivery run far past a caller's configured
budget.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import stat
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_LENGTH_PREFIX = struct.Struct(">I")

# A caller's own real `submit_queue_member()` member id is only assigned
# per-call (freshly random, agent_protocols_v1_queue_coalescing_adjustment
# _metadata_v1.md §14 requires uniqueness within a generation), so a test
# cannot know it ahead of time when calling `program_response()`. Callers
# needing the compressor's canned response to carry the real id can use
# this token as a placeholder in the programmed body; `handle_queue()`
# substitutes the id it actually parsed out of the request's own
# `member_block` before returning -- the same thing a real compressor
# model does when it echoes back "the same id from the request"
# (`acp.queue_codec.QUEUE_ISOLATION_ADDENDUM`).
MEMBER_ID_TOKEN = "__ACP_TEST_MEMBER_ID__"

_QUEUE_ITEM_RE = re.compile(r"^ACP-QUEUE-ITEM: (?P<id>.+)$", re.MULTILINE)

# Mirrors contract.json's outcomes.values.<outcome>.response_status_code
# for every outcome other than "success" (which passes the programmed
# upstream status code through as-is).
_OUTCOME_STATUS = {
    "unavailable": 503,
    "queue_timeout": 504,
    "compression_timeout": 504,
    "total_timeout": 504,
    "invalid_response": 502,
    "upstream_error": 502,
}

_DEFAULT_CAPABILITIES = [
    "request.forward",
    "provider.status",
    "provider.concurrency",
    "request.timeout_outcomes",
    "request.queue",
]


@dataclass
class FakeProvider:
    id: str
    display_name: str = ""
    active: bool = True
    concurrency_limit: int = 1
    in_flight: int = 0
    queued: int = 0
    idle: bool = True
    idle_seconds: float = 0.0
    accepted_paths: list[str] = field(default_factory=list)

    def as_status_object(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "active": self.active,
            "concurrency_limit": self.concurrency_limit,
            "in_flight": self.in_flight,
            "queued": self.queued,
            "idle": self.idle,
            "idle_seconds": self.idle_seconds,
            "accepted_paths": list(self.accepted_paths),
        }


@dataclass
class _ProgrammedResponse:
    outcome: str = "success"
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    message: str = ""
    delay: float = 0.0


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError(
                "peer closed the connection before sending all expected bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(sock: socket.socket, max_frame_bytes: int) -> bytes:
    header = _recv_exact(sock, _LENGTH_PREFIX.size)
    (length,) = _LENGTH_PREFIX.unpack(header)
    if length > max_frame_bytes:
        raise ValueError(f"frame length {length} exceeds max_frame_bytes {max_frame_bytes}")
    return _recv_exact(sock, length)


def _write_frame(sock: socket.socket, payload: bytes) -> None:
    sock.sendall(_LENGTH_PREFIX.pack(len(payload)) + payload)


class FakeAalpV1:
    """A real loopback Unix-socket server implementing interface v1's
    three operations."""

    _MAX_FRAME_BYTES = 64 * 1024 * 1024
    _ACCEPT_POLL_SECONDS = 0.2

    def __init__(
        self,
        root: str | Path,
        capabilities: list[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.capabilities_list = (
            list(capabilities) if capabilities is not None else list(_DEFAULT_CAPABILITIES)
        )
        self.secret = secrets.token_urlsafe(32)

        self._lock = threading.Lock()
        self._providers: dict[str, FakeProvider] = {}
        self._programmed: dict[tuple[str, str], deque[_ProgrammedResponse]] = {}
        self.last_headers: Any = None  # last request.forward call's headers, for assertions
        self.last_body: bytes = b""  # last request.forward call's raw body, for assertions

        self.socket_path = self.root / ".aalp" / "state" / "ingress.sock"
        self._server_socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "FakeAalpV1":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

        server_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_socket.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        server_socket.listen(128)
        server_socket.settimeout(self._ACCEPT_POLL_SECONDS)
        self._server_socket = server_socket

        self._thread = threading.Thread(target=self._serve_forever, daemon=True)
        self._thread.start()
        self._write_bootstrap_files()
        return self

    def stop(self) -> None:
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._server_socket is not None:
            self._server_socket.close()
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "FakeAalpV1":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _write_bootstrap_files(self) -> None:
        state_dir = self.root / ".aalp" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        secret_path = state_dir / "ingress.secret"
        # A trailing newline, like a real AALP's atomic writer produces --
        # exercises the client's strip()-on-read bootstrap behavior for real.
        secret_path.write_text(self.secret + "\n", encoding="utf-8")
        os.chmod(secret_path, stat.S_IRUSR | stat.S_IWUSR)

        descriptor = {"socket_path": str(self.socket_path), "secret_file": str(secret_path)}
        (state_dir / "ingress.json").write_text(json.dumps(descriptor), encoding="utf-8")

    # -- configuration --------------------------------------------------------

    def add_provider(self, provider: FakeProvider | dict) -> None:
        p = provider if isinstance(provider, FakeProvider) else FakeProvider(**provider)
        with self._lock:
            self._providers[p.id] = p

    def program_response(
        self,
        provider_id: str,
        path: str,
        *,
        outcome: str = "success",
        status: int = 200,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
        message: str = "",
        delay: float = 0.0,
    ) -> None:
        """Queue one canned `request.forward` result, consumed FIFO per (provider_id, path)."""
        if outcome != "success" and outcome not in _OUTCOME_STATUS:
            raise ValueError(f"unknown outcome {outcome!r}")
        entry = _ProgrammedResponse(
            outcome=outcome, status=status, headers=dict(headers or {}),
            body=body, message=message, delay=delay,
        )
        with self._lock:
            self._programmed.setdefault((provider_id, path), deque()).append(entry)

    # -- request handling, called from the socket-serving loop --------------

    def check_auth(self, authorization_header: str | None) -> bool:
        return authorization_header == f"Bearer {self.secret}"

    def capabilities_body(self) -> dict[str, Any]:
        return {"service": "aalp", "interface_version": 1, "capabilities": list(self.capabilities_list)}

    def list_providers_body(self) -> dict[str, Any]:
        with self._lock:
            providers = [p.as_status_object() for p in self._providers.values()]
        return {"providers": providers}

    def provider_status_body(self, provider_id: str) -> dict[str, Any] | None:
        with self._lock:
            provider = self._providers.get(provider_id)
        return provider.as_status_object() if provider is not None else None

    def handle_forward(self, provider_id: str, path: str) -> tuple[int, dict[str, str], bytes]:
        with self._lock:
            provider = self._providers.get(provider_id)

        if provider_id.startswith("_") or provider is None or not provider.active:
            return self._synthesize(
                "unavailable", f"provider {provider_id!r} unknown or inactive"
            )

        key = (provider_id, path)
        with self._lock:
            queue = self._programmed.get(key)
            if not queue:
                raise LookupError(
                    f"fake_aalp_v1: no response programmed for provider={provider_id!r} "
                    f"path={path!r}; call program_response(...) first"
                )
            programmed = queue.popleft()

        if programmed.delay:
            time.sleep(programmed.delay)

        if programmed.outcome == "success":
            headers = dict(programmed.headers)
            headers["X-Aalp-Outcome"] = "success"
            return programmed.status, headers, programmed.body

        return self._synthesize(programmed.outcome, programmed.message, status=_OUTCOME_STATUS[programmed.outcome])

    @staticmethod
    def _synthesize(outcome: str, message: str, status: int | None = None) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps({"outcome": outcome, "message": message}).encode()
        headers = {"X-Aalp-Outcome": outcome, "Content-Type": "application/json"}
        return status if status is not None else _OUTCOME_STATUS[outcome], headers, body

    def handle_queue(
        self, provider_id: str, path: str, request_body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """`request.queue`: identical to `handle_forward()` plus the two
        generation-metadata headers (this fixture always answers as its
        own singleton generation -- it does not implement real
        coalescing; see `tests/fixtures/fake_aalp_v1_service.py` on the
        AALP side for that). If the programmed response body contains
        `MEMBER_ID_TOKEN`, it is replaced with the real member id parsed
        out of the request's own envelope, since a test cannot know that
        id (freshly random per call) ahead of time.
        """
        status, headers, body = self.handle_forward(provider_id, path)
        headers = dict(headers)
        headers["X-Aalp-Queue-Generation-Id"] = secrets.token_hex(8)
        headers["X-Aalp-Queue-Member-Count"] = "1"
        member_id = self._extract_member_id(request_body)
        if member_id is not None:
            token = MEMBER_ID_TOKEN.encode("utf-8")
            if token in body:
                body = body.replace(token, member_id.encode("utf-8"))
        return status, headers, body

    @staticmethod
    def _extract_member_id(request_body: bytes) -> str | None:
        try:
            envelope = json.loads(request_body)
            member_block = envelope["member_block"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return None
        match = _QUEUE_ITEM_RE.search(member_block)
        return match.group("id").strip() if match else None

    # -- socket server --------------------------------------------------------

    def _serve_forever(self) -> None:
        while not self._stop_requested.is_set():
            try:
                connection, _ = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            thread = threading.Thread(
                target=self._handle_connection, args=(connection,), daemon=True)
            thread.start()

    @staticmethod
    def _encode_response(status: int, headers: dict[str, str] | None, body: bytes) -> bytes:
        envelope = {
            "status": status,
            "headers": dict(headers or {}),
            "body": base64.b64encode(body or b"").decode("ascii"),
        }
        return json.dumps(envelope).encode("utf-8")

    def _handle_connection(self, connection: socket.socket) -> None:
        with connection:
            try:
                try:
                    payload = _read_frame(connection, self._MAX_FRAME_BYTES)
                except ValueError:
                    _write_frame(
                        connection,
                        self._encode_response(413, {}, b"request body too large"))
                    return

                try:
                    envelope = json.loads(payload.decode("utf-8"))
                    method = envelope["method"]
                    path = envelope["path"]
                    headers = envelope["headers"]
                    raw_body = envelope.get("body") or ""
                    body = base64.b64decode(raw_body) if raw_body else b""
                except Exception:
                    _write_frame(
                        connection,
                        self._encode_response(400, {}, b"malformed request envelope"))
                    return

                if not self.check_auth(headers.get("Authorization")):
                    _write_frame(
                        connection, self._encode_response(401, {}, json.dumps(
                            {"error": "unauthorized"}).encode()))
                    return

                self.last_body = body
                try:
                    status, response_headers, response_body = self._dispatch(
                        method, path, headers, body)
                except LookupError as exc:
                    _write_frame(
                        connection,
                        self._encode_response(500, {"Content-Type": "text/plain"}, str(exc).encode()))
                    return
                _write_frame(
                    connection,
                    self._encode_response(status, response_headers, response_body))
            except (ConnectionError, OSError):
                pass  # peer went away mid-request/response; nothing more to do

    def _dispatch(
        self, method: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        if method == "GET" and path == "/_aalp/v1/capabilities":
            body = json.dumps(self.capabilities_body()).encode()
            return 200, {"Content-Type": "application/json"}, body
        if method == "GET" and path == "/_aalp/v1/providers":
            body = json.dumps(self.list_providers_body()).encode()
            return 200, {"Content-Type": "application/json"}, body
        if method == "GET" and path.startswith("/_aalp/v1/providers/"):
            provider_id = path[len("/_aalp/v1/providers/"):]
            status_obj = self.provider_status_body(provider_id)
            if status_obj is None:
                body = json.dumps(
                    {"error": "provider_not_found", "provider_id": provider_id}).encode()
                return 404, {"Content-Type": "application/json"}, body
            return 200, {"Content-Type": "application/json"}, json.dumps(status_obj).encode()
        if path.startswith("/_aalp/"):
            body = json.dumps({"error": "not_found"}).encode()
            return 404, {"Content-Type": "application/json"}, body

        segment, _, rest = path.lstrip("/").partition("/")
        upstream_path = "/" + rest if rest else ""
        self.last_headers = headers
        if headers.get("X-Aalp-Queue-Key") is not None:
            return self.handle_queue(segment, upstream_path, body)
        return self.handle_forward(segment, upstream_path)
