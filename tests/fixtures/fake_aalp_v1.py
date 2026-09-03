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
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


class FakeAalpV1:
    """A real loopback HTTP server implementing interface v1's three operations."""

    def __init__(
        self,
        root: str | Path,
        host: str = "127.0.0.1",
        capabilities: list[str] | None = None,
    ) -> None:
        self.root = Path(root)
        self.host = host
        self.capabilities_list = (
            list(capabilities) if capabilities is not None else list(_DEFAULT_CAPABILITIES)
        )
        self.secret = secrets.token_urlsafe(32)

        self._lock = threading.Lock()
        self._providers: dict[str, FakeProvider] = {}
        self._programmed: dict[tuple[str, str], deque[_ProgrammedResponse]] = {}
        self.last_headers: Any = None  # last request.forward call's headers, for assertions

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> "FakeAalpV1":
        self._httpd = ThreadingHTTPServer((self.host, 0), _build_handler(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        self._write_bootstrap_files()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeAalpV1":
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    def _write_bootstrap_files(self) -> None:
        state_dir = self.root / ".aalp" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        secret_path = state_dir / "ingress.secret"
        # A trailing newline, like a real AALP's atomic writer produces --
        # exercises the client's strip()-on-read bootstrap behavior for real.
        secret_path.write_text(self.secret + "\n", encoding="utf-8")
        os.chmod(secret_path, stat.S_IRUSR | stat.S_IWUSR)

        descriptor = {"host": self.host, "port": self.port, "secret_file": str(secret_path)}
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

    # -- request handling, called from the HTTP handler --------------------

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


def _build_handler(fake: FakeAalpV1) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:  # silence stdlib access log
            pass

        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_PATCH(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def _write_json(self, status: int, obj: dict) -> None:
            self._write_raw(status, {"Content-Type": "application/json"}, json.dumps(obj).encode())

        def _write_raw(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _dispatch(self) -> None:
            # Mirrors aalp/ingress.py's real requirement: every request,
            # GET included, must carry Content-Length or the real ingress
            # 400s it. Enforcing that here too (rather than defaulting a
            # missing header to 0) is what would have caught ACP's own
            # discovery-request bug -- omitting Content-Length on a
            # body-less GET -- in this fixture's own test suite instead
            # of only via a live activation run.
            length_header = self.headers.get("Content-Length")
            if length_header is None:
                self._write_raw(400, {}, b"missing Content-Length")
                return
            length = int(length_header)
            if length:
                self.rfile.read(length)  # request bodies are unused by this fake
            path = self.path

            if not fake.check_auth(self.headers.get("Authorization")):
                self._write_json(401, {"error": "unauthorized"})
                return

            if self.command == "GET" and path == "/_aalp/v1/capabilities":
                self._write_json(200, fake.capabilities_body())
                return
            if self.command == "GET" and path == "/_aalp/v1/providers":
                self._write_json(200, fake.list_providers_body())
                return
            if self.command == "GET" and path.startswith("/_aalp/v1/providers/"):
                provider_id = path[len("/_aalp/v1/providers/"):]
                status_obj = fake.provider_status_body(provider_id)
                if status_obj is None:
                    self._write_json(404, {"error": "provider_not_found", "provider_id": provider_id})
                else:
                    self._write_json(200, status_obj)
                return
            if path.startswith("/_aalp/"):
                self._write_json(404, {"error": "not_found"})
                return

            segment, _, rest = path.lstrip("/").partition("/")
            upstream_path = "/" + rest if rest else ""
            fake.last_headers = self.headers
            try:
                status, headers, body = fake.handle_forward(segment, upstream_path)
            except LookupError as exc:
                self._write_raw(500, {"Content-Type": "text/plain"}, str(exc).encode())
                return
            self._write_raw(status, headers, body)

    return Handler
