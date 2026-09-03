#!/usr/bin/env python3
"""Additively installs ACP's Claude Code host adapter (`acp/adapters/
claude_code_bash_mcp.py` plus the Phase 4 hook scripts under
`acp/adapters/hooks/`) into this user's live Claude Code config.

Two files are touched, both additively -- every other key in each is read
back unchanged and rewritten byte-for-byte equivalent (only reformatted):

- `~/.claude.json`: sets `mcpServers.acp-bash` to this repository's
  adapter entry. Only that one key under `mcpServers` is touched.
- `~/.claude/settings.json`: ensures `"Bash"` is present in
  `permissions.deny`, so the MCP-provided `bash` tool (which routes
  output through ACP, failing open to raw output if ACP is unreachable)
  is the only way to run a shell command; and adds this repo's own hook
  commands into `hooks.SubagentStart`, `hooks.SubagentStop`, and
  `hooks.PreCompact` (each identified by a stable marker substring in
  its `command`, so re-running this script is idempotent and existing
  entries from other tools, e.g. agent-mem-struct, are never touched or
  duplicated).

`hooks.PreToolUse` (matched to the `Task` tool, for "parent -> child
oversized support context") is deliberately NOT installed by this
script -- whether Claude Code's `PreToolUse.updatedInput` actually
applies to the Task tool is unconfirmed; see project_md's STATUS.md
Phase 4 checkpoint. Pass `--with-parent-child-context` once that is
empirically confirmed to also install it.

This does not start ACP itself -- run `deploy/install.sh` (or
`deploy/acp.service` via your own supervisor) first, and AALP's own
`deploy/install.sh` in `agent-api-lane-protocol` before that, since ACP
requires a reachable AALP instance to actually compress (it fails open,
not silently, if AALP is unreachable -- see `acp/aalp_client.py`).

Usage: python3 deploy/claude_code/install.py [--dry-run] [--with-parent-child-context]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = "/usr/bin/python3"

_MCP_ENTRY = {
    "type": "stdio",
    "command": _PYTHON,
    "args": ["-m", "acp.adapters.claude_code_bash_mcp"],
    "env": {
        "ACP_HOME": str(_REPO_ROOT),
        "PYTHONPATH": str(_REPO_ROOT),
    },
}

# A substring unique to each hook's command line, used both to detect an
# already-installed entry (idempotency) and to build it fresh -- every
# entry this installer owns is env-prefixed inline (settings.json hook
# commands are plain shell strings, not structured env maps like
# `mcpServers` entries above) so it is fully self-contained regardless of
# what environment Claude Code itself launches the hook subprocess with.
_ENV_PREFIX = f'PYTHONPATH="{_REPO_ROOT}" ACP_HOME="{_REPO_ROOT}"'


def _hook_command(module: str, extra_args: str = "") -> str:
    return f'{_ENV_PREFIX} "{_PYTHON}" -m {module} --agent claude{extra_args}'


_SUBAGENT_REPORT_MODULE = "acp.adapters.hooks.subagent_report"
_PARENT_CHILD_CONTEXT_MODULE = "acp.adapters.hooks.parent_child_context"


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _ensure_hook_entry(
    settings: dict,
    event_name: str,
    command: str,
    *,
    status_message: str,
    timeout: int,
    matcher: str | None = None,
) -> bool:
    """Additively ensure one `{"type": "command", "command": command, ...}`
    entry exists somewhere under `settings["hooks"][event_name]`.

    Idempotent by exact `command` string match against every existing
    entry for this event (regardless of which group holds it), so
    re-running this installer never duplicates an entry. When `matcher`
    is given, a fresh entry is grouped under a matcher-group with that
    exact matcher (creating one if none exists yet with that matcher);
    when it is `None`, a fresh entry is appended into the first
    matcher-less group (creating one if none exists) -- either way,
    every *other* group and entry for this event (e.g. agent-mem-struct's)
    is left completely untouched."""
    hooks = settings.setdefault("hooks", {})
    groups = hooks.setdefault(event_name, [])

    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == command:
                return False  # already installed, somewhere

    entry = {
        "type": "command",
        "command": command,
        "timeout": timeout,
        "statusMessage": status_message,
    }

    if matcher is not None:
        for group in groups:
            if group.get("matcher") == matcher:
                group.setdefault("hooks", []).append(entry)
                return True
        groups.append({"matcher": matcher, "hooks": [entry]})
        return True

    for group in groups:
        if "matcher" not in group:
            group.setdefault("hooks", []).append(entry)
            return True
    groups.append({"hooks": [entry]})
    return True


def install(dry_run: bool, with_parent_child_context: bool) -> int:
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

    hook_specs = [
        ("SubagentStart", _SUBAGENT_REPORT_MODULE, "SubagentStart", dict(
            status_message="ACP: offer the report tool for a large final report", timeout=5)),
        ("SubagentStop", _SUBAGENT_REPORT_MODULE, "SubagentStop", dict(
            status_message="ACP: best-effort background prewarm of a large transcript tail",
            timeout=5)),
    ]
    if with_parent_child_context:
        hook_specs.append(("PreToolUse/Task", _PARENT_CHILD_CONTEXT_MODULE, "PreToolUse", dict(
            status_message="ACP: compress delimited oversized support context",
            timeout=30, matcher="Task")))

    hook_changes = {}
    for label, module, event_name, kwargs in hook_specs:
        hook_changes[label] = _ensure_hook_entry(
            settings, event_name, _hook_command(module), **kwargs)
    hooks_changed = any(hook_changes.values())
    for label, changed in hook_changes.items():
        print(f"{settings_path}: hooks.{label} "
              f"{'will be added' if changed else 'already installed'}")

    if not mcp_changed and not deny_changed and not hooks_changed:
        print("Nothing to do -- already installed.")
        return 0

    if dry_run:
        print("--dry-run: no files written")
        return 0

    if mcp_changed:
        _atomic_write(claude_json_path, claude_json)
    if deny_changed or hooks_changed:
        _atomic_write(settings_path, settings)

    print("Installed. Restart any running Claude Code sessions to pick up "
          "the new MCP server, permission change, and/or hooks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would change without writing.")
    parser.add_argument(
        "--with-parent-child-context", action="store_true",
        help=(
            "Also install the PreToolUse/Task hook for oversized "
            "support-context compression -- only once Claude Code's "
            "PreToolUse.updatedInput is empirically confirmed to apply "
            "to the Task tool."
        ),
    )
    args = parser.parse_args(argv)
    return install(args.dry_run, args.with_parent_child_context)


if __name__ == "__main__":
    raise SystemExit(main())
