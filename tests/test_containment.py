import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from acp.containment import (
    AcpContainmentError,
    ensure_dirs,
    read_raw,
    resolve_root,
    store_raw,
    expire_stale,
)


class ContainmentTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ResolveRootTest(ContainmentTestBase):
    def test_explicit_root_wins(self) -> None:
        self.assertEqual(resolve_root(self.root), self.root)

    def test_env_var_used_when_no_explicit_root(self) -> None:
        old = os.environ.get("ACP_HOME")
        os.environ["ACP_HOME"] = str(self.root)
        try:
            self.assertEqual(resolve_root(None), self.root)
        finally:
            if old is None:
                os.environ.pop("ACP_HOME", None)
            else:
                os.environ["ACP_HOME"] = old

    def test_falls_back_to_cwd(self) -> None:
        old = os.environ.pop("ACP_HOME", None)
        try:
            self.assertEqual(resolve_root(None), Path.cwd())
        finally:
            if old is not None:
                os.environ["ACP_HOME"] = old


class EnsureDirsTest(ContainmentTestBase):
    def test_creates_four_subdirs(self) -> None:
        paths = ensure_dirs(self.root)
        self.assertEqual(set(paths.keys()), {"state", "metrics", "raw", "logs"})
        for path in paths.values():
            self.assertTrue(path.is_dir())

    def test_dirs_are_0700(self) -> None:
        paths = ensure_dirs(self.root)
        for path in paths.values():
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o700)

    def test_idempotent(self) -> None:
        ensure_dirs(self.root)
        paths = ensure_dirs(self.root)
        for path in paths.values():
            self.assertTrue(path.is_dir())

    def test_paths_under_acp_prefix(self) -> None:
        paths = ensure_dirs(self.root)
        for path in paths.values():
            self.assertEqual(path.parent.name, ".acp")


class StoreReadRawTest(ContainmentTestBase):
    def test_round_trip(self) -> None:
        source_hash = "a" * 64
        store_raw(self.root, b"raw content", source_hash)
        self.assertEqual(read_raw(self.root, source_hash), b"raw content")

    def test_missing_raises(self) -> None:
        with self.assertRaises(AcpContainmentError):
            read_raw(self.root, "b" * 64)

    def test_dedupes_identical_hash_without_rewrite(self) -> None:
        source_hash = "c" * 64
        path = store_raw(self.root, b"first", source_hash)
        original_mtime_ns = path.stat().st_mtime_ns
        time.sleep(0.01)
        second_path = store_raw(self.root, b"first", source_hash)
        self.assertEqual(path, second_path)
        self.assertEqual(path.stat().st_mtime_ns, original_mtime_ns)
        self.assertEqual(read_raw(self.root, source_hash), b"first")

    def test_stored_file_is_0600(self) -> None:
        source_hash = "d" * 64
        path = store_raw(self.root, b"content", source_hash)
        mode = stat.S_IMODE(path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_no_leftover_temp_files(self) -> None:
        source_hash = "e" * 64
        path = store_raw(self.root, b"content", source_hash)
        siblings = [entry for entry in os.listdir(path.parent) if entry != path.name]
        self.assertEqual(siblings, [])

    def test_rejects_non_hex_source_hash(self) -> None:
        with self.assertRaises(AcpContainmentError):
            store_raw(self.root, b"content", "../../etc/passwd")

    def test_rejects_empty_source_hash(self) -> None:
        with self.assertRaises(AcpContainmentError):
            store_raw(self.root, b"content", "")


class ExpireStaleTest(ContainmentTestBase):
    def test_removes_files_older_than_max_age(self) -> None:
        source_hash = "f" * 64
        path = store_raw(self.root, b"stale", source_hash)

        removed = expire_stale(self.root, max_age_seconds=100, clock=lambda: (
            path.stat().st_mtime + 200))
        self.assertEqual(removed, [path])
        self.assertFalse(path.exists())

    def test_keeps_fresh_files(self) -> None:
        source_hash = "0" * 64
        path = store_raw(self.root, b"fresh", source_hash)

        removed = expire_stale(self.root, max_age_seconds=100, clock=lambda: (
            path.stat().st_mtime + 10))
        self.assertEqual(removed, [])
        self.assertTrue(path.exists())

    def test_no_raw_dir_yet_returns_empty(self) -> None:
        removed = expire_stale(self.root, max_age_seconds=100)
        self.assertEqual(removed, [])


if __name__ == "__main__":
    unittest.main()
