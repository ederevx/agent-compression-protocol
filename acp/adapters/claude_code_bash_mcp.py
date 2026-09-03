"""A stdio MCP server exposing one tool, `bash`, that runs a shell command
and passes its output through a live ACP instance before returning it to
Claude Code -- ACP's Phase 4 host integration for Claude Code.

Why an MCP tool instead of a `PostToolUse` hook
------------------------------------------------
The adjustment metadata (`agent_protocols_v1_background_compression_
adjustment_metadata_v1.md` §26) originally specified `PostToolUse.
updatedToolOutput` as the integration boundary: intercept a tool's
result and replace it before the next model turn. Verified against the
real, current Claude Code hooks reference (fetched 2026-09-03): no such
field exists. `PostToolUse` fires strictly after the tool has already
run, and can only attach a `systemMessage`/`terminalSequence` -- it
cannot rewrite or replace the tool's output. §26 was built on a false
premise; this module is Phase 4's corrected design, not an
implementation of that section as originally written.

The only real mechanism that lets ACP see and replace command output
before it reaches Claude's context is to *be* the tool that ran the
command, rather than react to one that already did. Claude Code also
has no way to force Claude to prefer one tool over an equivalent
built-in one -- but a bare `"Bash"` entry in `permissions.deny` removes
the built-in Bash tool from Claude's context entirely (confirmed against
the real permissions reference), leaving this MCP-provided `bash` tool
as the only way to run a shell command. Claude Code's own Cowork
integration uses exactly this pattern (`mcp__workspace__bash` in place
of the built-in `Bash` tool, with a `Bash` deny rule applying to it too).
Installing that deny rule in a live, host-wide settings file is a
separate, explicitly-confirmed step -- see the Phase 4 checkpoint in
project_md's STATUS.md; this module only implements the tool itself.

Fail-open, always
------------------
ACP being unreachable, erroring, or timing out must never block or
corrupt a tool result: `_evaluate_or_passthrough` catches every failure
mode from `acp.adapters.acp_client.AcpClient.evaluate` and falls back to
the command's own raw output, unmodified. A live agent host must never
find itself unable to run a command, or looking at corrupted output,
because a supporting compression service is down.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from typing import Any, TextIO

from acp.adapters.acp_client import AcpClient

_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "acp-bash"
_SERVER_VERSION = "1.0.0"

_DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0
_MAX_COMMAND_TIMEOUT_SECONDS = 600.0
_ACP_EVALUATE_TIMEOUT_SECONDS = 30.0

# Native-agent-report: this tool's own output is a native tool (Bash)
# reporting back into the calling agent's own context, not a general
# payload and not host-to-subagent supporting context -- see
# `acp/gate.py`'s three `TrafficClass` docstrings.
_TRAFFIC_CLASS = "native_agent_report"

_TOOLS: list[dict[str, Any]] = [
    {
        "name": "bash",
        "description": (
            "Run a shell command. Output is compressed through ACP when it "
            "would otherwise consume disproportionate context; ACP being "
            "unavailable never blocks the command or alters its output "
            "beyond that compression step."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "description": {
                    "type": "string",
                    "description": "Optional human-readable summary of the command.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Timeout in milliseconds (default 120000, max 600000).",
                },
            },
            "required": ["command"],
        },
    }
]


def _resolve_timeout_seconds(timeout_ms: Any) -> float:
    if timeout_ms is None:
        return _DEFAULT_COMMAND_TIMEOUT_SECONDS
    try:
        seconds = float(timeout_ms) / 1000.0
    except (TypeError, ValueError):
        return _DEFAULT_COMMAND_TIMEOUT_SECONDS
    if seconds <= 0:
        return _DEFAULT_COMMAND_TIMEOUT_SECONDS
    return min(seconds, _MAX_COMMAND_TIMEOUT_SECONDS)


def run_bash(command: str, timeout_seconds: float) -> str:
    """Run `command` in a shell, returning combined stdout+stderr (matching
    the built-in Bash tool's single-text-stream shape) plus a trailing exit
    status note when the command failed or timed out. Never raises for a
    failing command -- only for something Python itself couldn't do."""
    try:
        completed = subprocess.run(
            command,
            shell=True,
            executable="/bin/bash",
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return partial + f"\n[command timed out after {timeout_seconds:.0f}s]"

    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        output += f"\n[exit code: {completed.returncode}]"
    return output


def _evaluate_or_passthrough(
    client: AcpClient, output: str, session_id: str
) -> tuple[str, list[str]]:
    """Route `output` through ACP's real `context.evaluate`; on any
    failure (unreachable, protocol error, timeout), fall back to `output`
    unchanged -- see module docstring's "Fail-open, always"."""
    try:
        result = client.evaluate(
            output,
            _TRAFFIC_CLASS,
            {"host": "claude-code-bash-mcp", "session_id": session_id, "agent_id": None},
            timeout=_ACP_EVALUATE_TIMEOUT_SECONDS,
        )
    except Exception:
        return output, []

    if not result.ok or result.output is None:
        return output, []

    return result.output, list(result.warnings)


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class BashMcpServer:
    """Holds the process-lifetime state (one ACP client, one session id
    identifying this server process to ACP) and dispatches JSON-RPC
    messages. `main()` below is the only piece that touches stdio."""

    def __init__(self, acp_root: str) -> None:
        self._client = AcpClient(acp_root)
        self._session_id = uuid.uuid4().hex

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        request_id = message.get("id")

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": _SERVER_NAME, "version": _SERVER_VERSION},
                },
            }

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": _TOOLS}}

        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name != "bash":
                return {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": _text_result(f"unknown tool: {name!r}", is_error=True),
                }
            command = arguments.get("command")
            if not isinstance(command, str) or not command:
                return {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": _text_result("'command' must be a non-empty string", is_error=True),
                }
            timeout_seconds = _resolve_timeout_seconds(arguments.get("timeout"))
            raw_output = run_bash(command, timeout_seconds)
            final_output, _warnings = _evaluate_or_passthrough(
                self._client, raw_output, self._session_id)
            return {"jsonrpc": "2.0", "id": request_id, "result": _text_result(final_output)}

        # Unknown method: only reply with an error if it expected a response
        # (has an id) -- never talk back on an unrecognized notification.
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }


def _read_message(stream: TextIO) -> dict[str, Any] | None:
    line = stream.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    return json.loads(line)


def _write_message(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message) + "\n")
    stream.flush()


def main(argv: list[str] | None = None) -> int:
    acp_root = os.environ.get("ACP_HOME")
    if not acp_root:
        print(
            "acp-bash: ACP_HOME must be set to the ACP instance's state "
            "root (where .acp/ lives)", file=sys.stderr,
        )
        return 2

    server = BashMcpServer(acp_root)
    while True:
        try:
            message = _read_message(sys.stdin)
        except json.JSONDecodeError:
            continue
        if message is None:
            return 0
        if not message:
            continue
        response = server.handle_message(message)
        if response is not None:
            _write_message(sys.stdout, response)


if __name__ == "__main__":
    raise SystemExit(main())
