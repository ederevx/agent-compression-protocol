"""ACP's composition-root HTTP dispatch: interface v1 over `acp.ingress.Ingress`.

Owns every HTTP-specific concern (JSON parsing/serialization, path
routing, status-code mapping) so `acp.coordinator.Coordinator` itself
stays free of transport concerns. This is a deliberate choice: unlike
AALP -- where `Gateway.as_ingress_handler()` lives on the composition-root
class itself, because `Gateway.handle()` already needed an HTTP-shaped
`(method, path, headers, body)` signature for its own passthrough
forwarding job -- `Coordinator`'s public API
(`evaluate`/`store_source`/`status`) is a plain
Python API with no HTTP shape of its own, and is
worth keeping directly callable by an in-process host adapter with zero
HTTP machinery involved. Putting the operation dispatch in this
separate module keeps that split clean: `Coordinator` never imports
`json` or knows an HTTP status code exists.

Five operations, all under `/v1/...`, mirroring `interface/v1/contract.json`:

    POST /v1/context/evaluate
    POST /v1/source/store           (raw binary body, not JSON)
    GET  /v1/service/capabilities
    GET  /v1/service/status
    GET  /v1/service/telemetry

`context.prepare`/`context.resolve` (background prefetch) were removed
in interface_version 2 -- see acp/coordinator.py's module docstring for
why. `context.evaluate` (synchronous ingress compression, ACP's actual
token-reduction mechanism) is unaffected.
"""
from __future__ import annotations

import json
from typing import Any

from acp.compressor import CompressionResult
from acp.coordinator import Coordinator
from acp.errors import Outcome, TrafficClass
from acp.ingress import Handler
from acp.provenance import Provenance

# The sole definition of interface v1's capability list -- must match
# interface/v1/contract.json's top-level "capabilities" array verbatim.
# service.capabilities (below) is the only reader of this constant.
INTERFACE_V1_CAPABILITIES: tuple[str, ...] = (
    "context.evaluate",
    "source.store",
    "service.status",
    "service.telemetry",
)

# Bumped from 1: context.prepare/context.resolve were removed, a
# breaking change per interface/v1/contract.json's own compatibility
# rules ("an operation or field is removed or renamed" requires a new
# major interface version). See acp/coordinator.py's module docstring.
INTERFACE_VERSION = 2

# Mirrors agent-api-lane-protocol's aalp/gateway.py::_STATUS_BY_OUTCOME
# convention exactly, applied to acp.errors.Outcome (ACP's own outcome
# set -- ACP never imports AALP's).
_STATUS_BY_OUTCOME: dict[Outcome, int] = {
    Outcome.UNAVAILABLE: 503,
    Outcome.QUEUE_TIMEOUT: 504,
    Outcome.TOTAL_TIMEOUT: 504,
    Outcome.COMPRESSION_TIMEOUT: 504,
    Outcome.INVALID_RESPONSE: 502,
    Outcome.UPSTREAM_ERROR: 502,
    Outcome.RATE_LIMITED: 429,
}

_JSON_HEADERS = {"Content-Type": "application/json"}


class _BadRequest(ValueError):
    """Internal-only: a malformed request, caught once per operation and turned into a 400."""


def _json_response(
    status: int, obj: dict, *, extra_headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    headers = dict(_JSON_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    return status, headers, json.dumps(obj).encode("utf-8")


def _error_response(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _json_response(status, {"error": "bad_request", "message": message})


def _parse_json_body(body: bytes) -> dict:
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise _BadRequest(f"invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _BadRequest("request body must be a JSON object")
    return parsed


def _parse_traffic_class(value: Any) -> TrafficClass:
    try:
        return TrafficClass(value)
    except ValueError:
        raise _BadRequest(f"invalid traffic_class: {value!r}") from None


def _parse_receiver(value: Any) -> tuple[str, str, str | None]:
    if not isinstance(value, dict):
        raise _BadRequest("receiver must be an object")
    try:
        host = value["host"]
        session_id = value["session_id"]
    except KeyError as exc:
        raise _BadRequest(f"receiver missing required field: {exc}") from None
    agent_id = value.get("agent_id")
    if not isinstance(host, str) or not isinstance(session_id, str):
        raise _BadRequest("receiver.host and receiver.session_id must be strings")
    if agent_id is not None and not isinstance(agent_id, str):
        raise _BadRequest("receiver.agent_id must be a string or null")
    return (host, session_id, agent_id)


def _parse_optional_str(value: Any, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise _BadRequest(f"{field_name} must be a string or null")
    return value


def _parse_optional_number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise _BadRequest(f"{field_name} must be a number or null")
    return float(value)


def _serialize_provenance(provenance: Provenance | None) -> dict | None:
    if provenance is None:
        return None
    return {
        "processed": provenance.processed,
        "source_hash": provenance.source_hash,
    }


def _serialize_result(result: CompressionResult) -> dict:
    return {
        "outcome": result.outcome.value,
        "mode": result.mode,
        "output": result.output,
        "warnings": list(result.warnings),
        "message": result.message,
        "provenance": _serialize_provenance(result.provenance),
    }


def _result_response(result: CompressionResult) -> tuple[int, dict[str, str], bytes]:
    status = (
        200 if result.outcome is Outcome.SUCCESS
        else _STATUS_BY_OUTCOME.get(result.outcome, 502)
    )
    return _json_response(
        status, _serialize_result(result),
        extra_headers={"X-Acp-Outcome": result.outcome.value},
    )


def build_handler(coordinator: Coordinator) -> Handler:
    """Build the closure `acp.ingress.Ingress` calls per request.

    See the module docstring: the path routing and JSON envelope shapes
    below are this module's own concrete choice of wire protocol,
    matching `interface/v1/contract.json` exactly -- nothing about them
    is pinned down by `Coordinator` itself.
    """

    def _handle_evaluate(body: bytes) -> tuple[int, dict[str, str], bytes]:
        payload = _parse_json_body(body)
        text = payload.get("payload")
        if not isinstance(text, str):
            raise _BadRequest("payload must be a string")
        traffic_class = _parse_traffic_class(payload.get("traffic_class"))
        receiver_key = _parse_receiver(payload.get("receiver"))
        flow_id = _parse_optional_str(payload.get("flow_id"), "flow_id")
        synchronous_timeout = _parse_optional_number(
            payload.get("synchronous_timeout"), "synchronous_timeout"
        )
        result = coordinator.evaluate(
            text, traffic_class, receiver_key,
            flow_id=flow_id, synchronous_timeout=synchronous_timeout,
        )
        return _result_response(result)

    def _handle_store(body: bytes) -> tuple[int, dict[str, str], bytes]:
        source_hash = coordinator.store_source(body)
        return _json_response(201, {"source_hash": source_hash})

    def _handle_capabilities() -> tuple[int, dict[str, str], bytes]:
        return _json_response(200, {
            "service": "acp",
            "interface_version": INTERFACE_VERSION,
            "capabilities": list(INTERFACE_V1_CAPABILITIES),
        })

    def _handle_status() -> tuple[int, dict[str, str], bytes]:
        return _json_response(200, coordinator.status())

    def _handle_telemetry() -> tuple[int, dict[str, str], bytes]:
        return _json_response(200, {"counters": coordinator.telemetry.snapshot()})

    def _handler(
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> tuple[int, dict[str, str], bytes]:
        segments = [segment for segment in path.split("/") if segment]
        try:
            if method == "POST" and segments == ["v1", "context", "evaluate"]:
                return _handle_evaluate(body)
            if method == "POST" and segments == ["v1", "source", "store"]:
                return _handle_store(body)
            if method == "GET" and segments == ["v1", "service", "capabilities"]:
                return _handle_capabilities()
            if method == "GET" and segments == ["v1", "service", "status"]:
                return _handle_status()
            if method == "GET" and segments == ["v1", "service", "telemetry"]:
                return _handle_telemetry()
        except _BadRequest as exc:
            return _error_response(400, str(exc))
        return _json_response(404, {"error": "not_found"})

    return _handler
