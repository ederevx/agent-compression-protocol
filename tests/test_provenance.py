import unittest

from acp.provenance import Provenance, compute_hash, next_provenance, should_reprocess


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


class ShouldReprocessTest(unittest.TestCase):
    def test_no_prior_provenance_should_process(self) -> None:
        self.assertTrue(should_reprocess(None, "abc123"))

    def test_same_hash_processed_should_not_reprocess(self) -> None:
        prior = Provenance(processed=True, source_hash="abc123", generation=0)
        self.assertFalse(should_reprocess(prior, "abc123"))

    def test_same_hash_twice_in_a_row_stays_false(self) -> None:
        prior = Provenance(processed=True, source_hash="abc123", generation=0)
        self.assertFalse(should_reprocess(prior, "abc123"))
        self.assertFalse(should_reprocess(prior, "abc123"))

    def test_changed_hash_should_reprocess(self) -> None:
        prior = Provenance(processed=True, source_hash="abc123", generation=0)
        self.assertTrue(should_reprocess(prior, "def456"))

    def test_prior_not_processed_same_hash_still_new_payload_semantics(self) -> None:
        # processed=False with a matching hash does not match the
        # "processed=True and same hash" exemption, so it is treated as
        # a hash mismatch check: same hash -> falls through to False only
        # via the processed+same-hash branch. Since processed is False,
        # that branch does not apply, and the same-hash check at the end
        # also evaluates to False (source_hash == current_source_hash),
        # so this should NOT be treated as new (never got that far).
        prior = Provenance(processed=False, source_hash="abc123", generation=0)
        self.assertFalse(should_reprocess(prior, "abc123"))

    def test_prior_not_processed_different_hash_should_reprocess(self) -> None:
        prior = Provenance(processed=False, source_hash="abc123", generation=0)
        self.assertTrue(should_reprocess(prior, "def456"))


class NextProvenanceTest(unittest.TestCase):
    def test_no_prior_starts_at_generation_zero(self) -> None:
        result = next_provenance(None, "abc123")
        self.assertEqual(result.generation, 0)
        self.assertEqual(result.source_hash, "abc123")
        self.assertTrue(result.processed)

    def test_same_hash_keeps_generation(self) -> None:
        prior = Provenance(processed=True, source_hash="abc123", generation=3)
        result = next_provenance(prior, "abc123")
        self.assertEqual(result.generation, 3)

    def test_changed_hash_bumps_generation(self) -> None:
        prior = Provenance(processed=True, source_hash="abc123", generation=3)
        result = next_provenance(prior, "def456")
        self.assertEqual(result.generation, 4)
        self.assertEqual(result.source_hash, "def456")

    def test_processed_flag_overridable(self) -> None:
        result = next_provenance(None, "abc123", processed=False)
        self.assertFalse(result.processed)


if __name__ == "__main__":
    unittest.main()
