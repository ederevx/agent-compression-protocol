"""Host lifecycle-event hook scripts for ACP's Phase 4 integration.

Each module here is a standalone CLI entry point a host (Claude Code's
`settings.json` `hooks`, Codex's `hooks.json`) invokes as a subprocess per
event, mirroring `agent-mem-struct/hooks/root-memory-context.py`'s
`--agent claude|codex` / stdin-JSON-event / stdout-JSON-response
convention exactly. None of these import `acp.coordinator` directly --
like `acp.adapters.claude_code_bash_mcp`, they only ever reach ACP
through `acp.adapters.acp_client.AcpClient`, ACP's real interface-v1
socket boundary.
"""
from __future__ import annotations
