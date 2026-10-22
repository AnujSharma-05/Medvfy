"""Unit tests for ReferenceStore: verifying baseline isolation, persistence, and immutability."""

import tempfile
import unittest
from pathlib import Path
import numpy as np

from vision.reference_store import ReferenceStore


class TestReferenceStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.baseline_path = Path(self.temp_dir.name) / "test_baseline.npz"
        self.metadata_path = Path(self.temp_dir.name) / "test_baseline.json"
        self.store = ReferenceStore(self.baseline_path, self.metadata_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_empty_store_on_init(self):
        self.assertEqual(self.store.expected, {})
        self.assertEqual(self.store.empty, {})
        self.assertFalse(self.store.has_expected(["A", "B"]))

    def test_save_and_load_expected(self):
        crop_a = np.ones((50, 50, 3), dtype=np.uint8) * 100
        crop_b = np.ones((60, 60, 3), dtype=np.uint8) * 200

        self.store.save_expected({"A": crop_a, "B": crop_b}, {"camera_index": 0})
        self.assertTrue(self.baseline_path.exists())
        self.assertTrue(self.metadata_path.exists())

        # Load fresh instance
        fresh_store = ReferenceStore(self.baseline_path, self.metadata_path)
        self.assertTrue(fresh_store.has_expected(["A", "B"]))
        np.testing.assert_array_equal(fresh_store.get_expected("A"), crop_a)
        np.testing.assert_array_equal(fresh_store.get_expected("B"), crop_b)
        self.assertEqual(fresh_store.metadata.get("camera_index"), 0)

    def test_expected_and_empty_isolation(self):
        """Saving empty references must not overwrite expected references, and vice versa."""
        expected_a = np.ones((50, 50, 3), dtype=np.uint8) * 150
        empty_a = np.zeros((50, 50, 3), dtype=np.uint8)

        self.store.save_expected({"A": expected_a})
        self.store.save_empty({"A": empty_a})

        fresh_store = ReferenceStore(self.baseline_path, self.metadata_path)
        self.assertTrue(fresh_store.has_expected(["A"]))
        self.assertTrue(fresh_store.has_empty(["A"]))

        np.testing.assert_array_equal(fresh_store.get_expected("A"), expected_a)
        np.testing.assert_array_equal(fresh_store.get_empty("A"), empty_a)

    def test_immutability_during_observation(self):
        """CRITICAL: Observing a new image must not alter stored reference."""
        original_expected = np.ones((40, 40, 3), dtype=np.uint8) * 75
        self.store.save_expected({"A": original_expected})

        # Simulate incoming observation that is completely different (e.g. empty)
        observation = np.zeros((40, 40, 3), dtype=np.uint8)
        self.assertFalse(np.array_equal(observation, self.store.get_expected("A")))

        # Verify stored reference remains unchanged
        np.testing.assert_array_equal(self.store.get_expected("A"), original_expected)

        # Re-read from disk
        fresh = ReferenceStore(self.baseline_path, self.metadata_path)
        np.testing.assert_array_equal(fresh.get_expected("A"), original_expected)

    def test_corrupt_file_handling(self):
        """A corrupt file should be handled gracefully without crashing."""
        self.baseline_path.write_text("corrupted content", encoding="utf-8")
        fresh = ReferenceStore(self.baseline_path, self.metadata_path)
        self.assertEqual(fresh.expected, {})
        self.assertEqual(fresh.empty, {})
        self.assertFalse(fresh.has_expected(["A"]))


if __name__ == "__main__":
    unittest.main()
