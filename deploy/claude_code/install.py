#!/usr/bin/env python3
"""Additively installs ACP's Claude Code host adapter
(`acp/adapters/claude_code_bash_mcp.py`) into this user's live Claude Code
config.

Two files are touched, both additively -- every other key in each is read
back unchanged and rewritten byte-for-byte equivalent (only reformatted):

- `~/.claude.json`: sets `mcpServers.acp-bash` to this repository's
  adapter entry. Only that one key under `mcpServers` is touched.
- `~/.claude/settings.json`: ensures `"Bash"` is present in
  `permissions.deny`, so the MCP-provided `bash` tool (which routes
  output through ACP, failing open to raw output if ACP is unreachable)
  is the only way to run a shell command. Existing hooks, env, and every
  other key are left untouched.

This does not start ACP itself -- run `deploy/install.sh` (or
`deploy/acp.service` via your own supervisor) first, and AALP's own
`deploy/install.sh` in `agent-api-lane-protocol` before that, since ACP
requires a reachable AALP instance to actually compress (it fails open,
not silently, if AALP is unreachable -- see `acp/aalp_client.py`).

Usage: python3 deploy/claude_code/install.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MCP_ENTRY = {
    "type": "stdio",
    "command": "/usr/bin/python3",
    "args": ["-m", "acp.adapters.claude_code_bash_mcp"],
    "env": {
        "ACP_HOME": str(_REPO_ROOT),
        "PYTHONPATH": str(_REPO_ROOT),
    },
}


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def install(dry_run: bool) -> int:
    claude_json_path = Path.home() / ".claude.json"
    settings_path = Path.home() / ".claude" / "settings.json"

    claude_json = _load_json(claude_json_path)
    mcp_servers = claude_json.setdefault("mcpServers", {})
    mcp_changed = mcp_servers.get("acp-bash") != _MCP_ENTRY
    mcp_servers["acp-bash"] = _MCP_ENTRY
    print(f"{claude_json_path}: mcpServers.acp-bash "
          f"{'already set' if not mcp_changed else 'will be set'} "
          f"(ACP_HOME={_MCP_ENTRY['env']['ACP_HOME']})")

    settings = _load_json(settings_path)
    permissions = settings.setdefault("permissions", {})
    deny = permissions.setdefault("deny", [])
    deny_changed = "Bash" not in deny
    if deny_changed:
        deny.append("Bash")
    print(f"{settings_path}: permissions.deny "
          f"{'already contains' if not deny_changed else 'will gain'} 'Bash'")

    if not mcp_changed and not deny_changed:
        print("Nothing to do -- already installed.")
        return 0

    if dry_run:
        print("--dry-run: no files written")
        return 0

    if mcp_changed:
        _atomic_write(claude_json_path, claude_json)
    if deny_changed:
        _atomic_write(settings_path, settings)

    print("Installed. Restart any running Claude Code sessions to pick up "
          "the new MCP server and permission change.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would change without writing.")
    args = parser.parse_args(argv)
    return install(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
