"""Tests for `deploy.claude_code.install`'s settings.json reconciliation.

Focus: the installer must be able to remove ACP-owned hook entries that
are no longer desired (the orphaned `precompact_pressure` PreCompact
registration left behind by d207cad, "Drop pressure subsystem...", is the
motivating real-world case -- see the module's docstring), while never
touching anything that isn't its own: other protocols' hook entries
(e.g. agent-delegation-protocol's), other tools' hook entries (e.g.
agent-mem-struct's), and `permissions` in general.

`install()` itself (which touches real `~/.claude.json`/`~/.claude/
settings.json` paths via `Path.home()`) is exercised end-to-end against
temp-file stand-ins by monkeypatching `Path.home`, rather than against
the real, live per-user config -- this suite must never write to a
developer's or CI runner's actual `~/.claude`.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.claude_code import install


# A stand-in for another protocol's own hook entry, so tests can assert
# it's never touched. Deliberately does NOT contain "acp" anywhere in its
# command, the way a real adp entry wouldn't either.
_ADP_ENTRY = {
    "type": "command",
    "command": 'PYTHONPATH="/home/x/agent-delegation-protocol" '
                '"/usr/bin/python3" -m adp.hooks.gate --agent claude',
    "timeout": 5,
    "statusMessage": "ADP: enforce delegation thresholds",
}

# A stand-in for agent-mem-struct's own PreCompact hook -- modeled on the
# real live entry, which sits in the very same matcher-less PreCompact
# group as the orphaned ACP entry below.
_MEM_STRUCT_ENTRY = {
    "type": "command",
    "command": '"/usr/bin/python3" "/home/x/agent-mem-struct/hooks/'
                'root-memory-context.py" --agent claude',
    "timeout": 60,
    "statusMessage": "agent-mem-struct root memory: checkpoint context "
                      "before compaction",
}

# The real orphaned entry: `acp.adapters.hooks.precompact_pressure` was
# deleted from the codebase in d207cad, but a prior install left this
# registered under PreCompact, and no version of install.py has ever
# unregistered it -- until now.
_ORPHANED_ACP_ENTRY = {
    "type": "command",
    "command": 'PYTHONPATH="/old/acp" ACP_HOME="/old/acp" '
                '"/usr/bin/python3" -m acp.adapters.hooks.precompact_pressure '
                '--agent claude',
    "timeout": 5,
    "statusMessage": "ACP: report context pressure ahead of native compaction",
}


class ReconcileHookEntriesTest(unittest.TestCase):
    """Direct tests of `_reconcile_hook_entries`, the core add/remove logic."""

    def test_adds_missing_entry_into_fresh_event(self) -> None:
        settings: dict = {}
        entry = {"type": "command", "command": "cmd-a", "timeout": 5,
                  "statusMessage": "msg"}
        added, removed = install._reconcile_hook_entries(
            settings, "SubagentStart", [(None, entry)])
        self.assertTrue(added)
        self.assertFalse(removed)
        self.assertEqual(
            settings["hooks"]["SubagentStart"], [{"hooks": [entry]}])

    def test_idempotent_when_entry_already_present(self) -> None:
        entry = {"type": "command", "command": "cmd-a", "timeout": 5,
                  "statusMessage": "msg"}
        settings = {"hooks": {"SubagentStart": [{"hooks": [entry]}]}}
        added, removed = install._reconcile_hook_entries(
            settings, "SubagentStart", [(None, entry)])
        self.assertFalse(added)
        self.assertFalse(removed)
        self.assertEqual(
            settings["hooks"]["SubagentStart"], [{"hooks": [entry]}])

    def test_removes_orphaned_acp_entry_leaving_other_tools_entry_intact(self) -> None:
        """The core bug fix: an ACP-owned command absent from the desired
        set is removed even though `desired` is empty for this event
        (mirrors PreCompact having no `hook_specs` entry at all), while
        the co-located agent-mem-struct entry in the very same
        matcher-less group survives untouched."""
        settings = {
            "hooks": {
                "PreCompact": [
                    {"hooks": [dict(_MEM_STRUCT_ENTRY), dict(_ORPHANED_ACP_ENTRY)]},
                ],
            },
        }
        added, removed = install._reconcile_hook_entries(settings, "PreCompact", [])
        self.assertFalse(added)
        self.assertTrue(removed)
        self.assertEqual(
            settings["hooks"]["PreCompact"], [{"hooks": [_MEM_STRUCT_ENTRY]}])

    def test_never_touches_non_acp_owned_commands(self) -> None:
        settings = {
            "hooks": {
                "SubagentStart": [{"hooks": [dict(_ADP_ENTRY)]}],
            },
        }
        added, removed = install._reconcile_hook_entries(
            settings, "SubagentStart", [])
        self.assertFalse(added)
        self.assertFalse(removed)
        self.assertEqual(
            settings["hooks"]["SubagentStart"], [{"hooks": [_ADP_ENTRY]}])

    def test_drops_event_key_entirely_once_its_last_acp_entry_is_removed(self) -> None:
        settings = {"hooks": {"PreCompact": [{"hooks": [dict(_ORPHANED_ACP_ENTRY)]}]}}
        added, removed = install._reconcile_hook_entries(settings, "PreCompact", [])
        self.assertTrue(removed)
        self.assertNotIn("PreCompact", settings["hooks"])

    def test_swaps_stale_acp_command_for_a_newly_desired_one_in_place(self) -> None:
        old_entry = {"type": "command", "command": "old-cmd", "timeout": 5,
                     "statusMessage": "old"}
        new_entry = {"type": "command", "command": "-m acp.adapters.hooks.new_module",
                     "timeout": 5, "statusMessage": "new"}
        settings = {"hooks": {"SubagentStart": [{"hooks": [dict(old_entry)]}]}}
        # old_entry itself doesn't match the ACP signature, so it should
        # NOT be removed even though it's absent from desired -- only
        # entries matching `_is_acp_hook_command` are removal candidates.
        added, removed = install._reconcile_hook_entries(
            settings, "SubagentStart", [(None, new_entry)])
        self.assertTrue(added)
        self.assertFalse(removed)
        self.assertEqual(
            settings["hooks"]["SubagentStart"],
            [{"hooks": [old_entry, new_entry]}])

    def test_matcher_grouping_creates_and_reuses_matcher_group(self) -> None:
        entry = {"type": "command", "command": "-m acp.adapters.hooks.x",
                  "timeout": 5, "statusMessage": "msg"}
        settings: dict = {}
        install._reconcile_hook_entries(settings, "PreToolUse", [("Task", entry)])
        self.assertEqual(
            settings["hooks"]["PreToolUse"], [{"matcher": "Task", "hooks": [entry]}])


class InstallEndToEndTest(unittest.TestCase):
    """Exercises `install()` against temp-file stand-ins for
    `~/.claude.json` and `~/.claude/settings.json`, never the real files."""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.home = Path(self._tempdir.name)
        (self.home / ".claude").mkdir()
        self.settings_path = self.home / ".claude" / "settings.json"
        patcher = patch("pathlib.Path.home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_settings(self, data: dict) -> None:
        self.settings_path.write_text(json.dumps(data, indent=2) + "\n")

    def _read_settings(self) -> dict:
        return json.loads(self.settings_path.read_text())

    def test_first_install_adds_hooks_and_deny_and_leaves_a_no_backup(self) -> None:
        rc = install.install(dry_run=False)
        self.assertEqual(rc, 0)
        settings = self._read_settings()
        self.assertIn("Bash", settings["permissions"]["deny"])
        self.assertIn("SubagentStart", settings["hooks"])
        self.assertIn("SubagentStop", settings["hooks"])
        # Nothing existed before this run, so there is nothing to back up.
        self.assertFalse((self.home / ".claude" / "settings.json.bak").exists())

    def test_second_run_is_a_pure_noop(self) -> None:
        install.install(dry_run=False)
        before = self.settings_path.read_text()
        rc = install.install(dry_run=False)
        self.assertEqual(rc, 0)
        after = self.settings_path.read_text()
        self.assertEqual(before, after)

    def test_removes_orphaned_precompact_entry_end_to_end_and_backs_up_first(self) -> None:
        self._write_settings({
            "permissions": {"deny": ["Bash"]},
            "hooks": {
                "PreCompact": [
                    {"hooks": [dict(_MEM_STRUCT_ENTRY), dict(_ORPHANED_ACP_ENTRY)]},
                ],
                "PreToolUse": [
                    {"matcher": "Task", "hooks": [dict(_ADP_ENTRY)]},
                ],
            },
        })
        before_text = self.settings_path.read_text()

        rc = install.install(dry_run=False)

        self.assertEqual(rc, 0)
        settings = self._read_settings()
        # Orphaned ACP entry is gone; the co-located mem-struct entry and
        # the unrelated ADP entry under a different event both survive
        # byte-for-byte.
        self.assertEqual(settings["hooks"]["PreCompact"], [{"hooks": [_MEM_STRUCT_ENTRY]}])
        self.assertEqual(
            settings["hooks"]["PreToolUse"],
            [{"matcher": "Task", "hooks": [_ADP_ENTRY]}])
        # New SubagentStart/SubagentStop entries were installed too.
        self.assertIn("SubagentStart", settings["hooks"])
        self.assertIn("SubagentStop", settings["hooks"])
        # A backup of the pre-mutation file was written since this run
        # both added and removed hook entries.
        backup_path = self.home / ".claude" / "settings.json.bak"
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_text(), before_text)

    def test_dry_run_writes_nothing(self) -> None:
        self._write_settings({
            "hooks": {"PreCompact": [{"hooks": [dict(_ORPHANED_ACP_ENTRY)]}]},
        })
        before_text = self.settings_path.read_text()
        rc = install.install(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.settings_path.read_text(), before_text)
        self.assertFalse((self.home / ".claude" / "settings.json.bak").exists())

    def test_atomic_write_leaves_no_tmp_file_behind(self) -> None:
        install.install(dry_run=False)
        leftovers = list((self.home / ".claude").glob("settings.json.tmp*"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
