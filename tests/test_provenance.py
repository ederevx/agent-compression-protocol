import unittest

from acp.provenance import Provenance, compute_hash


class ComputeHashTest(unittest.TestCase):
    def test_str_and_bytes_agree(self) -> None:
        self.assertEqual(compute_hash("hello"), compute_hash(b"hello"))

    def test_deterministic(self) -> None:
        self.assertEqual(compute_hash("payload"), compute_hash("payload"))

    def test_different_inputs_differ(self) -> None:
        self.assertNotEqual(compute_hash("a"), compute_hash("b"))

    def test_is_hex_sha256(self) -> None:
        digest = compute_hash("x")
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises if not hex


class ProvenanceTest(unittest.TestCase):
    def test_fields(self) -> None:
        provenance = Provenance(processed=True, source_hash="abc123")
        self.assertTrue(provenance.processed)
        self.assertEqual(provenance.source_hash, "abc123")

    def test_frozen(self) -> None:
        provenance = Provenance(processed=True, source_hash="abc123")
        with self.assertRaises(Exception):
            provenance.processed = False


if __name__ == "__main__":
    unittest.main()
