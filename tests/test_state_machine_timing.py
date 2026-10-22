"""Time-based candidate confirmation in CompartmentStateMachine."""

import unittest

from vision.state_machine import CompartmentState, CompartmentStateMachine

REMOVED = {"diff_from_expected": 0.40, "diff_from_empty": 0.01}


class TestTimeBasedConfirmation(unittest.TestCase):
    def _machine(self, debounce_frames=2, confirm_seconds=2.0):
        sm = CompartmentStateMachine(debounce_frames=debounce_frames, confirm_seconds=confirm_seconds)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        return sm

    def test_enough_observations_but_too_little_time_does_not_confirm(self):
        """Regression: two observations 0.5 s apart confirmed removal (count-only debounce)."""
        sm = self._machine()
        self.assertEqual(sm.evaluate("A", **REMOVED, observed_at=0.0).new_state, CompartmentState.REMOVAL_CANDIDATE)
        self.assertEqual(sm.evaluate("A", **REMOVED, observed_at=0.5).new_state, CompartmentState.REMOVAL_CANDIDATE)
        self.assertEqual(sm.evaluate("A", **REMOVED, observed_at=1.0).new_state, CompartmentState.REMOVAL_CANDIDATE)

        confirmed = sm.evaluate("A", **REMOVED, observed_at=2.0)
        self.assertEqual(confirmed.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(confirmed.is_new_event)
        self.assertIn("2.0s", confirmed.reason)

    def test_enough_time_but_too_few_observations_does_not_confirm(self):
        sm = self._machine(debounce_frames=3, confirm_seconds=1.0)
        sm.evaluate("A", **REMOVED, observed_at=0.0)
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=5.0).new_state,
            CompartmentState.REMOVAL_CANDIDATE,
        )
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=5.5).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )

    def test_invalid_frame_restarts_the_clock(self):
        sm = self._machine()
        sm.evaluate("A", **REMOVED, observed_at=0.0)
        sm.evaluate("A", **REMOVED, observed_at=1.0)
        blurred = sm.evaluate("A", diff_from_expected=0.40, is_blurry=True, observed_at=1.5)
        self.assertEqual(blurred.new_state, CompartmentState.UNCERTAIN)

        # 2.5 s after the first observation, but only 0.5 s after the sequence resumed
        sm.evaluate("A", **REMOVED, observed_at=2.0)
        self.assertNotEqual(
            sm.evaluate("A", **REMOVED, observed_at=2.5).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=4.0).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )

    def test_reset_candidate_restarts_the_clock(self):
        sm = self._machine()
        sm.evaluate("A", **REMOVED, observed_at=0.0)
        sm.evaluate("A", **REMOVED, observed_at=1.0)
        sm.reset_candidate("A")

        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=2.5).new_state,
            CompartmentState.REMOVAL_CANDIDATE,
        )
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=4.5).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )

    def test_confirm_seconds_applies_even_with_single_frame_debounce(self):
        sm = self._machine(debounce_frames=1, confirm_seconds=1.0)
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=0.0).new_state,
            CompartmentState.REMOVAL_CANDIDATE,
        )
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=1.0).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )

    def test_return_also_requires_time(self):
        sm = CompartmentStateMachine(debounce_frames=2, confirm_seconds=2.0)
        sm.set_initial_state("A", CompartmentState.REMOVAL_DETECTED)
        returned = {"diff_from_expected": 0.01, "diff_from_empty": 0.40}
        sm.evaluate("A", **returned, observed_at=0.0)
        self.assertEqual(
            sm.evaluate("A", **returned, observed_at=0.5).new_state,
            CompartmentState.RETURN_CANDIDATE,
        )
        self.assertEqual(
            sm.evaluate("A", **returned, observed_at=2.0).new_state,
            CompartmentState.OBJECT_RETURNED,
        )

    def test_default_keeps_count_only_behaviour(self):
        sm = CompartmentStateMachine(debounce_frames=2)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        sm.evaluate("A", **REMOVED, observed_at=0.0)
        self.assertEqual(
            sm.evaluate("A", **REMOVED, observed_at=0.01).new_state,
            CompartmentState.REMOVAL_DETECTED,
        )


class TestUncertainRecoveryRespectsConfirmation(unittest.TestCase):
    def test_recovery_from_uncertain_goes_through_candidate(self):
        """Regression: one blurry frame then one empty-looking frame confirmed removal outright."""
        sm = CompartmentStateMachine(debounce_frames=2)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        sm.evaluate("A", diff_from_expected=0.02, is_blurry=True)

        first = sm.evaluate("A", **REMOVED)
        self.assertEqual(first.new_state, CompartmentState.REMOVAL_CANDIDATE)
        second = sm.evaluate("A", **REMOVED)
        self.assertEqual(second.new_state, CompartmentState.REMOVAL_DETECTED)

    def test_recovery_without_empty_reference_goes_through_candidate(self):
        sm = CompartmentStateMachine(debounce_frames=2)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        sm.evaluate("A", diff_from_expected=0.02, is_occluded=True)

        self.assertEqual(
            sm.evaluate("A", diff_from_expected=0.40).new_state,
            CompartmentState.REMOVAL_CANDIDATE,
        )

    def test_single_frame_debounce_still_recovers_immediately(self):
        sm = CompartmentStateMachine(debounce_frames=1)
        sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        sm.evaluate("A", diff_from_expected=0.02, is_blurry=True)
        self.assertEqual(sm.evaluate("A", **REMOVED).new_state, CompartmentState.REMOVAL_DETECTED)


if __name__ == "__main__":
    unittest.main()
