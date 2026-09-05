#!/usr/bin/env python3
"""Installs ACP's Claude Code host adapter (`acp/adapters/
claude_code_bash_mcp.py` plus the Phase 4 hook scripts under
`acp/adapters/hooks/`) into this user's live Claude Code config, and
reconciles it against this script's current desired state on every run.

Two files are touched:

- `~/.claude.json`: sets `mcpServers.acp-bash` to this repository's
  adapter entry. Only that one key under `mcpServers` is touched.
- `~/.claude/settings.json`: ensures `"Bash"` is present in
  `permissions.deny`, so the MCP-provided `bash` tool (which routes
  output through ACP, failing open to raw output if ACP is unreachable)
  is the only way to run a shell command; and reconciles this repo's own
  hook commands against `hook_specs` below, event by event -- adding
  whichever desired entries are missing and removing whichever
  ACP-owned entries are no longer desired, including ACP-owned entries
  under an event `hook_specs` doesn't mention at all (e.g. a hook whose
  module was since deleted from this repo -- see "Removal / ownership"
  below). Every non-ACP-owned entry, every other event, and everything
  outside `hooks`/`permissions.deny` is read back unchanged and
  rewritten byte-for-byte equivalent (only reformatted).

Removal / ownership: an existing hook entry is treated as ACP's own --
and therefore a removal candidate when no longer desired -- purely by
`_is_acp_hook_command()`, a command-string pattern
(`-m acp.adapters.hooks.<name>`), never by which group or event it sits
in. This is deliberately retroactive: it recognizes an orphaned entry
from a module this script no longer even mentions, such as the
`PreCompact` registration for `acp.adapters.hooks.precompact_pressure`,
deleted in d207cad ("Drop pressure subsystem...") without ever being
unregistered, which is exactly the bug this reconciliation exists to
fix. Two alternatives were considered and rejected:
  - An explicit ownership marker field (e.g. `"_acpOwned": true`) on
    each entry. Rejected because (a) it cannot retroactively identify
    entries installed by an older version of this script that predates
    the marker -- exactly the orphaned-entry case above -- and (b)
    Claude Code's settings schema tolerance for unrecognized hook-entry
    keys is unconfirmed; betting removal safety on an unverified
    assumption is worse than a pattern that needs no such assumption.
  - Matching by group/matcher position. Rejected because ACP's own
    entries share a matcher-less group with unrelated tools' entries in
    practice (e.g. agent-mem-struct's own `PreCompact` hook lives in the
    very same matcher-less group as the orphaned ACP entry above), so a
    whole group can never be swapped or cleared wholesale -- only the
    individual ACP-owned entries within it.
A stale command match is therefore sufficient on its own: any other
protocol's or user's entries never match `-m acp.adapters.hooks.` and
so are never touched, and `permissions` (and everything else in the
file) is never inspected by the reconciliation at all.

A prior `hooks.PreToolUse` hook (matched to the `Task` tool, "parent ->
child oversized support context") was removed 2026-09-04: it worked on
Claude, but the Codex equivalent (`PreToolUse.updatedInput` on
`spawn_agent`) was empirically confirmed rejected outright by Codex
itself -- a genuine parity gap, not just an unverified one -- so the
capability was dropped on both hosts rather than kept Claude-only.

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

# Every entry this installer owns is env-prefixed inline (settings.json
# hook commands are plain shell strings, not structured env maps like
# `mcpServers` entries above) so it is fully self-contained regardless of
# what environment Claude Code itself launches the hook subprocess with.
_ENV_PREFIX = f'PYTHONPATH="{_REPO_ROOT}" ACP_HOME="{_REPO_ROOT}"'


def _hook_command(module: str, extra_args: str = "") -> str:
    return f'{_ENV_PREFIX} "{_PYTHON}" -m {module} --agent claude{extra_args}'


_SUBAGENT_REPORT_MODULE = "acp.adapters.hooks.subagent_report"

# The substring every hook command this installer has ever emitted shares,
# past module names included -- see the module docstring's "Removal /
# ownership" section for why this, and not a marker field or group
# position, is what decides whether an *existing* settings.json entry is
# this installer's to add/remove.
_ACP_HOOK_SIGNATURE = "-m acp.adapters.hooks."


def _is_acp_hook_command(command: str) -> bool:
    return _ACP_HOOK_SIGNATURE in command


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _backup(path: Path) -> None:
    """Copy `path` to a sibling `.bak` file before it's mutated in place.

    Only called for `settings.json`, the one file this installer can now
    *remove* entries from -- `.claude.json`'s `mcpServers.acp-bash` write
    is still purely additive/idempotent (a single key set to a fixed
    value), so an interrupted or wrong write there is trivially re-run to
    fix, unlike a settings.json hook removal a user might want to
    inspect or revert. One rolling backup (overwritten each run, not
    timestamped) is enough to recover from "this installer's last write
    did something unexpected" without accumulating clutter in `~/.claude`."""
    if not path.exists():
        return
    path.with_name(path.name + ".bak").write_text(
        path.read_text(encoding="utf-8"), encoding="utf-8")


def _reconcile_hook_entries(
    settings: dict,
    event_name: str,
    desired: list[tuple[str | None, dict]],
) -> tuple[bool, bool]:
    """Make `settings["hooks"][event_name]` match `desired` -- adding
    whichever of `desired`'s `(matcher, entry)` pairs aren't already
    present (by exact `entry["command"]` match, so re-running this
    installer never duplicates one) and removing whichever *ACP-owned*
    existing entries (`_is_acp_hook_command`) aren't in `desired` at all,
    including when `desired` is empty because this event isn't in
    `hook_specs` any more (the orphaned-entry cleanup case -- see the
    module docstring). Every non-ACP-owned entry is left in place
    untouched, in its original group, even when that group also holds an
    ACP-owned entry being removed; a group left with zero entries after
    removal is itself dropped, and an event left with zero groups is
    dropped from `hooks` entirely, rather than kept around empty.

    Returns `(added, removed)`, either of which may be true.
    """
    hooks = settings.setdefault("hooks", {})
    groups = hooks.get(event_name, [])

    desired_commands = {entry["command"] for _, entry in desired}

    removed = False
    for group in groups:
        entries = group.get("hooks", [])
        kept = [
            entry for entry in entries
            if not (_is_acp_hook_command(entry.get("command", ""))
                    and entry.get("command") not in desired_commands)
        ]
        if len(kept) != len(entries):
            removed = True
            group["hooks"] = kept

    groups = [group for group in groups if group.get("hooks")]
    if groups:
        hooks[event_name] = groups
    else:
        hooks.pop(event_name, None)

    added = False
    for matcher, entry in desired:
        groups = hooks.setdefault(event_name, [])
        existing_commands = {
            e.get("command") for group in groups for e in group.get("hooks", [])
        }
        if entry["command"] in existing_commands:
            continue
        added = True
        if matcher is not None:
            for group in groups:
                if group.get("matcher") == matcher:
                    group.setdefault("hooks", []).append(entry)
                    break
            else:
                groups.append({"matcher": matcher, "hooks": [entry]})
        else:
            for group in groups:
                if "matcher" not in group:
                    group.setdefault("hooks", []).append(entry)
                    break
            else:
                groups.append({"hooks": [entry]})

    if not hooks.get(event_name):
        hooks.pop(event_name, None)

    return added, removed


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

    hook_specs = [
        ("SubagentStart", _SUBAGENT_REPORT_MODULE, "SubagentStart", dict(
            status_message="ACP: offer the report tool for a large final report", timeout=5)),
        ("SubagentStop", _SUBAGENT_REPORT_MODULE, "SubagentStop", dict(
            status_message="ACP: enforce report-tool use for oversized subagent output",
            timeout=5)),
    ]

    desired_by_event: dict[str, list[tuple[str | None, dict]]] = {}
    for _label, module, event_name, kwargs in hook_specs:
        kwargs = dict(kwargs)
        matcher = kwargs.pop("matcher", None)
        entry = {
            "type": "command",
            "command": _hook_command(module),
            "timeout": kwargs["timeout"],
            "statusMessage": kwargs["status_message"],
        }
        desired_by_event.setdefault(event_name, []).append((matcher, entry))

    # Reconcile every event `hook_specs` currently wants *and* every event
    # already present in settings.json, so an event this version of the
    # script no longer registers at all (like the deleted PreCompact
    # pressure hook) still gets its orphaned ACP entry pruned instead of
    # being silently skipped because it's absent from `desired_by_event`.
    all_events = sorted(set(desired_by_event) | set(settings.get("hooks", {})))
    hooks_changed = False
    for event_name in all_events:
        added, removed = _reconcile_hook_entries(
            settings, event_name, desired_by_event.get(event_name, []))
        hooks_changed = hooks_changed or added or removed
        if added and removed:
            status = "updated (added + removed a stale entry)"
        elif added:
            status = "will gain an entry"
        elif removed:
            status = "lost a stale ACP entry"
        else:
            status = "already up to date"
        print(f"{settings_path}: hooks.{event_name} {status}")

    if not mcp_changed and not deny_changed and not hooks_changed:
        print("Nothing to do -- already installed.")
        return 0

    if dry_run:
        print("--dry-run: no files written")
        return 0

    if mcp_changed:
        _atomic_write(claude_json_path, claude_json)
    if deny_changed or hooks_changed:
        _backup(settings_path)
        _atomic_write(settings_path, settings)

    print("Installed. Restart any running Claude Code sessions to pick up "
          "the new MCP server, permission change, and/or hooks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would change without writing.")
    args = parser.parse_args(argv)
    return install(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
