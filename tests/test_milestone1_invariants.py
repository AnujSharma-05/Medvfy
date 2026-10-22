"""Independent QA Verification Tests for Milestone 1 Invariants.

Verifies:
1. Repeated comparisons never modify the expected reference.
2. Empty and expected references remain independent across restarts.
3. Removal requires configured consecutive observations (temporal debouncing).
4. Return requires evidence consistent with the expected reference.
5. An unchanged scene does not generate duplicate confirmed transition events.
6. An invalid observation cannot incorrectly confirm a return.
7. A process restart does not silently lose or corrupt required state.
8. Existing CSV and backend observation contracts remain compatible.
"""

import csv
import tempfile
import unittest
from pathlib import Path
import numpy as np

import backend_test_env  # noqa: F401  (isolates data/config; must precede backend.app)
from backend.app import app
from vision.comparator import VisualComparator
from vision.event_logger import EventLogger
from vision.reference_store import ReferenceStore
from vision.state_machine import CompartmentState, CompartmentStateMachine


class TestMilestone1Invariants(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.baseline_file = self.data_dir / "vision_baseline.npz"
        self.metadata_file = self.data_dir / "vision_baseline.json"
        self.state_file = self.data_dir / "vision_state.json"
        self.log_file = self.data_dir / "vision_observations.csv"

        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _create_sample_crop(self, val: int = 150) -> np.ndarray:
        return np.ones((60, 60, 3), dtype=np.uint8) * np.uint8(val)

    def test_invariant_1_repeated_comparisons_never_modify_expected_reference(self):
        """Invariant 1: Repeated comparisons must never mutate or overwrite expected baseline."""
        store = ReferenceStore(self.baseline_file, self.metadata_file)
        expected_a = self._create_sample_crop(200)
        store.save_expected({"A": expected_a})

        comparator = VisualComparator()
        diff_crops = [
            self._create_sample_crop(0),    # empty
            self._create_sample_crop(255),  # bright
            self._create_sample_crop(50),   # dark
            self._create_sample_crop(200),  # identical
        ]

        # Perform 50 consecutive comparisons
        for i in range(50):
            current = diff_crops[i % len(diff_crops)]
            stored_ref = store.get_expected("A")
            metrics = comparator.compare(stored_ref, current)
            self.assertIsNotNone(metrics)

        # Verify internal array is byte-identical to original
        np.testing.assert_array_equal(store.get_expected("A"), expected_a)

        # Verify on-disk file remains byte-identical
        reloaded = ReferenceStore(self.baseline_file, self.metadata_file)
        np.testing.assert_array_equal(reloaded.get_expected("A"), expected_a)

    def test_invariant_2_empty_and_expected_references_remain_independent_across_restarts(self):
        """Invariant 2: Empty and expected references must remain independent after saving and reloading."""
        store1 = ReferenceStore(self.baseline_file, self.metadata_file)
        exp_initial = self._create_sample_crop(180)
        emp_initial = self._create_sample_crop(30)

        store1.save_expected({"A": exp_initial})
        store1.save_empty({"A": emp_initial})

        # Process restart 1: reload from disk
        store2 = ReferenceStore(self.baseline_file, self.metadata_file)
        np.testing.assert_array_equal(store2.get_expected("A"), exp_initial)
        np.testing.assert_array_equal(store2.get_empty("A"), emp_initial)

        # Mutate expected only
        exp_updated = self._create_sample_crop(220)
        store2.save_expected({"A": exp_updated})

        # Process restart 2: reload from disk
        store3 = ReferenceStore(self.baseline_file, self.metadata_file)
        np.testing.assert_array_equal(store3.get_expected("A"), exp_updated)
        # Empty must remain unchanged
        np.testing.assert_array_equal(store3.get_empty("A"), emp_initial)

        # Mutate empty only
        emp_updated = self._create_sample_crop(10)
        store3.save_empty({"A": emp_updated})

        # Process restart 3
        store4 = ReferenceStore(self.baseline_file, self.metadata_file)
        np.testing.assert_array_equal(store4.get_expected("A"), exp_updated)
        np.testing.assert_array_equal(store4.get_empty("A"), emp_updated)

    def test_invariant_3_removal_requires_configured_consecutive_observations(self):
        """Invariant 3: Removal requires N consecutive observations when debounce_frames = N."""
        sm = CompartmentStateMachine(debounce_frames=3)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        # Obs 1: Removal observed -> candidate 1
        t1 = sm.evaluate("A", diff_from_expected=0.40, diff_from_empty=0.01)
        self.assertEqual(t1.new_state, CompartmentState.REMOVAL_CANDIDATE)

        # Obs 2: Removal observed -> candidate 2
        t2 = sm.evaluate("A", diff_from_expected=0.41, diff_from_empty=0.01)
        self.assertEqual(t2.new_state, CompartmentState.REMOVAL_CANDIDATE)

        # Obs 3: Removal observed -> confirmed REMOVAL_DETECTED!
        t3 = sm.evaluate("A", diff_from_expected=0.40, diff_from_empty=0.01)
        self.assertEqual(t3.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(t3.is_new_event)

        # Sub-case: Interrupted sequence (transient disturbance reverts before confirmation)
        sm2 = CompartmentStateMachine(debounce_frames=3)
        sm2.set_initial_state("B", CompartmentState.EXPECTED_PRESENT)

        # Obs 1 & 2: disturbance
        sm2.evaluate("B", diff_from_expected=0.35, diff_from_empty=0.01)
        sm2.evaluate("B", diff_from_expected=0.35, diff_from_empty=0.01)
        # Obs 3: returns to expected before confirmation
        t_revert = sm2.evaluate("B", diff_from_expected=0.01)
        self.assertEqual(t_revert.new_state, CompartmentState.EXPECTED_PRESENT)

    def test_invariant_4_return_requires_evidence_consistent_with_expected(self):
        """Invariant 4: Return requires evidence consistent with the expected reference."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("A", CompartmentState.REMOVAL_DETECTED)

        # 1. Unrelated object introduced (high diff from expected, high diff from empty)
        t_foreign = sm.evaluate("A", diff_from_expected=0.35, diff_from_empty=0.30)
        self.assertEqual(t_foreign.new_state, CompartmentState.PARTIAL_OR_WRONG)

        # 2. Reset to removal
        sm.set_initial_state("A", CompartmentState.REMOVAL_DETECTED)
        # Still empty
        t_empty = sm.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
        self.assertEqual(t_empty.new_state, CompartmentState.REMOVAL_DETECTED)

        # 3. Valid return matching expected reference
        t_return = sm.evaluate("A", diff_from_expected=0.01, diff_from_empty=0.45)
        self.assertEqual(t_return.new_state, CompartmentState.OBJECT_RETURNED)

    def test_invariant_5_unchanged_scene_suppresses_duplicate_events(self):
        """Invariant 5: An unchanged scene does not generate duplicate transition events."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        # 3 consecutive observations of unchanged expected scene
        for _ in range(3):
            t = sm.evaluate("A", diff_from_expected=0.01)
            self.assertEqual(t.new_state, CompartmentState.EXPECTED_PRESENT)
            self.assertFalse(t.is_new_event)

        # Remove object
        t_rem = sm.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
        self.assertEqual(t_rem.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(t_rem.is_new_event)

        # 3 consecutive observations of unchanged empty scene
        for _ in range(3):
            t = sm.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
            self.assertEqual(t.new_state, CompartmentState.REMOVAL_DETECTED)
            self.assertFalse(t.is_new_event)  # Zero duplicate events emitted!

    def test_invariant_6_invalid_observation_cannot_confirm_return(self):
        """Invariant 6: Degraded, occluded, or mathematically invalid frames cannot confirm a return."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("A", CompartmentState.REMOVAL_DETECTED)

        # Blurry frame
        t_blur = sm.evaluate("A", diff_from_expected=0.01, is_blurry=True)
        self.assertEqual(t_blur.new_state, CompartmentState.UNCERTAIN)

        # Hand occluded
        t_occl = sm.evaluate("A", diff_from_expected=0.01, is_occluded=True)
        self.assertEqual(t_occl.new_state, CompartmentState.UNCERTAIN)

        # NaN diff metric
        t_nan = sm.evaluate("A", diff_from_expected=float("nan"))
        self.assertEqual(t_nan.new_state, CompartmentState.UNCERTAIN)

        # Negative diff metric
        t_neg = sm.evaluate("A", diff_from_expected=-0.10)
        self.assertEqual(t_neg.new_state, CompartmentState.UNCERTAIN)

        # None diff metric
        t_none = sm.evaluate("A", diff_from_expected=None)
        self.assertEqual(t_none.new_state, CompartmentState.UNCERTAIN)

    def test_invariant_7_process_restart_preserves_confirmed_state(self):
        """Invariant 7: Process restart does not lose confirmed state or cause false re-triggers."""
        sm1 = CompartmentStateMachine(debounce_frames=1, state_file=self.state_file)
        sm1.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        sm1.set_initial_state("B", CompartmentState.EXPECTED_PRESENT)

        # Compartment A medicine removed
        t_rem = sm1.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
        self.assertEqual(t_rem.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(self.state_file.exists())

        # Simulate process termination & restart
        sm2 = CompartmentStateMachine(debounce_frames=1, state_file=self.state_file)
        self.assertEqual(sm2.get_state("A"), CompartmentState.REMOVAL_DETECTED)
        self.assertEqual(sm2.get_state("B"), CompartmentState.EXPECTED_PRESENT)

        # After restart: unchanged empty A does NOT trigger new removal event
        t_after_restart = sm2.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
        self.assertEqual(t_after_restart.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertFalse(t_after_restart.is_new_event)

        # After restart: returning dose to A triggers OBJECT_RETURNED
        t_ret = sm2.evaluate("A", diff_from_expected=0.01, diff_from_empty=0.45)
        self.assertEqual(t_ret.new_state, CompartmentState.OBJECT_RETURNED)
        self.assertTrue(t_ret.is_new_event)

    def test_invariant_8_existing_csv_and_backend_contracts_compatible(self):
        """Invariant 8: Existing 5-column CSV files and backend API schemas remain compatible."""
        # 1. Existing 5-column CSV
        legacy_csv = self.data_dir / "legacy.csv"
        with legacy_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["timestamp_utc", "compartment_id", "state", "changed_pixel_fraction", "interpretation"])
            w.writerow(["2026-10-21T00:00:00Z", "A", "CHANGED", 0.12, "Visual change only"])

        logger = EventLogger(legacy_csv)
        sm = CompartmentStateMachine()
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        t = sm.evaluate("A", diff_from_expected=0.45)

        logger.log_transitions([t])

        # Verify all rows in legacy_csv maintain exactly 5 columns
        with legacy_csv.open("r", encoding="utf-8") as f:
            reader = list(csv.reader(f))
            self.assertEqual(len(reader), 3)  # header + legacy row + new row
            for row in reader:
                self.assertEqual(len(row), 5)

        # 2. Backend API contract verification
        payload = {
            "compartment_id": "A",
            "state": "REMOVAL_DETECTED",
            "changed_pixel_fraction": 0.45,
            "note": "Visual observation only",
        }
        resp = self.client.post("/api/observations", json=payload)
        self.assertEqual(resp.status_code, 201)
        self.assertTrue(resp.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
