#!/usr/bin/env python3
"""Additively installs ACP's Codex host adapter into this user's live
Codex config -- the Codex counterpart to `deploy/claude_code/install.py`.

`acp/adapters/claude_code_bash_mcp.py` is already host-neutral (a plain
stdio MCP server speaking JSON-RPC 2.0; nothing in it is Claude-specific)
-- Codex gets no separate server module, only a separate registration
path, via `codex mcp add` (mirroring `codex mcp --help`'s documented
usage; confirmed idempotent -- re-adding the same name just overwrites).

`~/.codex/hooks.json` is touched additively, same discipline as the
Claude installer: this repo's hook commands (identified by a stable
marker substring in each `command`) are merged into `hooks.SubagentStart`
and `hooks.SubagentStop`, leaving every other event and every
agent-mem-struct entry untouched.

`hooks.PreToolUse` (matched to `spawn_agent`, for "parent -> child
oversized support context") and shell-tool-exclusivity (disabling
`shell_tool`/`unified_exec` via `codex features disable`, mirroring
Claude's `permissions.deny: ["Bash"]`) are deliberately NOT installed by
this script -- both are unconfirmed in this environment (the installed
Codex CLI's ChatGPT backend was unreachable when this was built, so no
live model turn could confirm either capability; Codex's own docs assert
`PreToolUse.updatedInput` "should theoretically apply" to `spawn_agent`,
but that is a documentation claim, not a verified one here). Pass
`--with-parent-child-context` once `PreToolUse.updatedInput` on
`spawn_agent` is empirically confirmed; pass `--disable-shell-tool` (or
`--disable-unified-exec`) once one is confirmed to actually remove the
native shell tool from the model's tool list.

Usage: python3 deploy/codex/install.py [--dry-run] [--with-parent-child-context]
                                        [--disable-shell-tool] [--disable-unified-exec]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = "/usr/bin/python3"
_CODEX = str(Path.home() / ".local" / "bin" / "codex")

_ENV_PREFIX = f'PYTHONPATH="{_REPO_ROOT}" ACP_HOME="{_REPO_ROOT}"'


def _hook_command(module: str) -> str:
    return f'{_ENV_PREFIX} "{_PYTHON}" -m {module} --agent codex'


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
    hooks_doc: dict,
    event_name: str,
    command: str,
    *,
    status_message: str,
    timeout: int,
    matcher: str | None = None,
) -> bool:
    """Same additive-merge discipline as `deploy/claude_code/install.py`'s
    identically-named helper -- see its docstring. `hooks_doc` here is the
    whole `~/.codex/hooks.json` document (`{"hooks": {...}}`), not the
    `hooks` value itself, since that is this file's top-level shape."""
    hooks = hooks_doc.setdefault("hooks", {})
    groups = hooks.setdefault(event_name, [])

    for group in groups:
        for entry in group.get("hooks", []):
            if entry.get("command") == command:
                return False

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


def _register_mcp_server(dry_run: bool) -> None:
    command = [
        _CODEX, "mcp", "add", "acp-bash",
        "--env", f"ACP_HOME={_REPO_ROOT}",
        "--env", f"PYTHONPATH={_REPO_ROOT}",
        "--", _PYTHON, "-m", "acp.adapters.claude_code_bash_mcp",
    ]
    print(f"codex mcp add acp-bash: {'would run' if dry_run else 'running'} "
          f"(idempotent -- re-adding just overwrites)")
    if dry_run:
        return
    subprocess.run(command, check=True)


def _disable_feature(feature: str, dry_run: bool) -> None:
    print(f"codex features disable {feature}: {'would run' if dry_run else 'running'}")
    if dry_run:
        return
    subprocess.run([_CODEX, "features", "disable", feature], check=True)


def install(
    dry_run: bool,
    with_parent_child_context: bool,
    disable_shell_tool: bool,
    disable_unified_exec: bool,
) -> int:
    hooks_path = Path.home() / ".codex" / "hooks.json"
    hooks_doc = _load_json(hooks_path)

    hook_specs = [
        ("SubagentStart", _SUBAGENT_REPORT_MODULE, "SubagentStart", dict(
            status_message="ACP: offer the report tool for a large final report", timeout=5)),
        ("SubagentStop", _SUBAGENT_REPORT_MODULE, "SubagentStop", dict(
            status_message="ACP: best-effort background prewarm of a large transcript tail",
            timeout=5)),
    ]
    if with_parent_child_context:
        hook_specs.append(("PreToolUse/spawn_agent", _PARENT_CHILD_CONTEXT_MODULE, "PreToolUse", dict(
            status_message="ACP: compress delimited oversized support context",
            timeout=30, matcher="spawn_agent")))

    hook_changes = {}
    for label, module, event_name, kwargs in hook_specs:
        hook_changes[label] = _ensure_hook_entry(hooks_doc, event_name, _hook_command(module), **kwargs)
    hooks_changed = any(hook_changes.values())
    for label, changed in hook_changes.items():
        print(f"{hooks_path}: hooks.{label} {'will be added' if changed else 'already installed'}")

    if hooks_changed and not dry_run:
        _atomic_write(hooks_path, hooks_doc)

    _register_mcp_server(dry_run)
    if disable_shell_tool:
        _disable_feature("shell_tool", dry_run)
    if disable_unified_exec:
        _disable_feature("unified_exec", dry_run)

    if dry_run:
        print("--dry-run: no files written, no commands run")
        return 0

    print("Installed. Restart any running Codex sessions to pick up the new "
          "MCP server and/or hooks.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                         help="Print what would change without writing or running commands.")
    parser.add_argument(
        "--with-parent-child-context", action="store_true",
        help=(
            "Also install the PreToolUse/spawn_agent hook for oversized "
            "support-context compression -- only once Codex's "
            "PreToolUse.updatedInput is empirically confirmed to apply "
            "to spawn_agent."
        ),
    )
    parser.add_argument(
        "--disable-shell-tool", action="store_true",
        help="codex features disable shell_tool -- only once confirmed to make acp-bash exclusive.",
    )
    parser.add_argument(
        "--disable-unified-exec", action="store_true",
        help="codex features disable unified_exec -- only once confirmed to make acp-bash exclusive.",
    )
    args = parser.parse_args(argv)
    return install(
        args.dry_run, args.with_parent_child_context,
        args.disable_shell_tool, args.disable_unified_exec,
    )


if __name__ == "__main__":
    raise SystemExit(main())
