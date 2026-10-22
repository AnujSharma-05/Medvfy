"""Unit tests for CompartmentStateMachine: verifying transitions, debouncing, and duplicate suppression."""

import unittest
from vision.state_machine import CompartmentStateMachine, CompartmentState


class TestCompartmentStateMachine(unittest.TestCase):
    def test_initial_state(self):
        sm = CompartmentStateMachine(debounce_frames=1)
        self.assertEqual(sm.get_state("A"), CompartmentState.UNKNOWN)

        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        self.assertEqual(sm.get_state("A"), CompartmentState.EXPECTED_PRESENT)

    def test_removal_and_return_cycle_single_frame(self):
        """Verify: EXPECTED_PRESENT -> REMOVAL_DETECTED -> OBJECT_RETURNED -> EXPECTED_PRESENT."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        # 1. Observation matches expected -> remains EXPECTED_PRESENT
        t1 = sm.evaluate("A", diff_from_expected=0.01)
        self.assertEqual(t1.new_state, CompartmentState.EXPECTED_PRESENT)
        self.assertFalse(t1.is_new_event)  # duplicate suppressed

        # 2. Medication removed (high diff from expected, low diff from empty)
        t2 = sm.evaluate("A", diff_from_expected=0.45, diff_from_empty=0.01)
        self.assertEqual(t2.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(t2.is_new_event)

        # 3. Subsequent check while still removed -> remains REMOVAL_DETECTED, duplicate suppressed
        t3 = sm.evaluate("A", diff_from_expected=0.44, diff_from_empty=0.01)
        self.assertEqual(t3.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertFalse(t3.is_new_event)

        # 4. Medication returned (matches expected baseline again)
        t4 = sm.evaluate("A", diff_from_expected=0.02, diff_from_empty=0.42)
        self.assertEqual(t4.new_state, CompartmentState.OBJECT_RETURNED)
        self.assertTrue(t4.is_new_event)

    def test_temporal_debouncing(self):
        """Verify that with debounce_frames=2, transient candidate states do not immediately confirm."""
        sm = CompartmentStateMachine(debounce_frames=2)
        sm.set_initial_state("B", CompartmentState.EXPECTED_PRESENT)

        # Frame 1: potential removal -> candidate
        t1 = sm.evaluate("B", diff_from_expected=0.35, diff_from_empty=0.02)
        self.assertEqual(t1.new_state, CompartmentState.REMOVAL_CANDIDATE)

        # Frame 2: reverts back before confirmation (e.g. transient hand occlusion passes)
        t2 = sm.evaluate("B", diff_from_expected=0.02)
        self.assertEqual(t2.new_state, CompartmentState.EXPECTED_PRESENT)

        # Frame 3: genuine removal begins
        t3 = sm.evaluate("B", diff_from_expected=0.40, diff_from_empty=0.01)
        self.assertEqual(t3.new_state, CompartmentState.REMOVAL_CANDIDATE)

        # Frame 4: second consecutive removal frame -> confirmed!
        t4 = sm.evaluate("B", diff_from_expected=0.41, diff_from_empty=0.01)
        self.assertEqual(t4.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(t4.is_new_event)

    def test_partial_or_wrong_object(self):
        """An object that differs from expected but is NOT empty should be PARTIAL_OR_WRONG."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("C", CompartmentState.EXPECTED_PRESENT)

        # Object is present, but neither matches expected nor matches empty
        t1 = sm.evaluate("C", diff_from_expected=0.30, diff_from_empty=0.28)
        self.assertEqual(t1.new_state, CompartmentState.PARTIAL_OR_WRONG)

    def test_blur_uncertainty(self):
        """Blurry or occluded frames must yield UNCERTAIN rather than guessing."""
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("D", CompartmentState.EXPECTED_PRESENT)

        t1 = sm.evaluate("D", diff_from_expected=0.60, is_blurry=True)
        self.assertEqual(t1.new_state, CompartmentState.UNCERTAIN)
        self.assertEqual(t1.confidence, 0.0)

    def test_legacy_state_conversion(self):
        sm = CompartmentStateMachine()
        self.assertEqual(sm.to_legacy_state(CompartmentState.EXPECTED_PRESENT), "NO CHANGE")
        self.assertEqual(sm.to_legacy_state(CompartmentState.REMOVAL_DETECTED), "CHANGED")
        self.assertEqual(sm.to_legacy_state(CompartmentState.OBJECT_RETURNED), "CHANGED")
        self.assertEqual(sm.to_legacy_state(CompartmentState.PARTIAL_OR_WRONG), "CHANGED")
        self.assertEqual(sm.to_legacy_state(CompartmentState.UNCERTAIN), "UNCERTAIN")


if __name__ == "__main__":
    unittest.main()
