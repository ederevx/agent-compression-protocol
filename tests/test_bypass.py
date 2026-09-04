import tempfile
import unittest
from pathlib import Path

from acp import bypass


class BypassFlagTest(unittest.TestCase):
    def setUp(self) -> None:
        self._root_tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._root_tmp.name)

    def tearDown(self) -> None:
        self._root_tmp.cleanup()

    def test_absent_by_default(self) -> None:
        self.assertFalse(bypass.is_bypass_mode(self.root))

    def test_enter_creates_flag_and_parent_dirs(self) -> None:
        bypass.enter_bypass(self.root)

        self.assertTrue(bypass.is_bypass_mode(self.root))
        self.assertTrue((self.root / ".acp" / "state" / "bypass").is_file())

    def test_enter_is_idempotent(self) -> None:
        bypass.enter_bypass(self.root)
        bypass.enter_bypass(self.root)

        self.assertTrue(bypass.is_bypass_mode(self.root))

    def test_exit_removes_flag(self) -> None:
        bypass.enter_bypass(self.root)
        bypass.exit_bypass(self.root)

        self.assertFalse(bypass.is_bypass_mode(self.root))

    def test_exit_without_enter_is_a_no_op(self) -> None:
        bypass.exit_bypass(self.root)

        self.assertFalse(bypass.is_bypass_mode(self.root))

    def test_root_env_var_used_when_no_explicit_root_given(self) -> None:
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"ACP_HOME": str(self.root)}):
            bypass.enter_bypass()
            self.assertTrue(bypass.is_bypass_mode())
            self.assertTrue(bypass.is_bypass_mode(self.root))


if __name__ == "__main__":
    unittest.main()
