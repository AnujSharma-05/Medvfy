"""Comprehensive 12-Scenario Verification Suite for MedVerify.

Implements Section 8.2 Scenario Tests:
1. Correct contents remain unchanged.
2. An expected object is removed.
3. The same object is returned.
4. An object is shifted but not removed.
5. An unrelated object is introduced.
6. Some contents are removed (partial removal).
7. Lighting changes while objects remain present.
8. A hand temporarily occludes the compartment.
9. Camera moves or loses focus.
10. Wrong compartment is modified.
11. Image or reference is unavailable.
12. Two visually similar medication items tested (conservatism).
"""

import unittest
import cv2
import numpy as np

from vision.comparator import VisualComparator
from vision.frame_quality import FrameQualityAssessor
from vision.state_machine import CompartmentState, CompartmentStateMachine
from vision.classifier_interface import ClassicalHeuristicClassifier


class TestVerificationScenarios(unittest.TestCase):
    def setUp(self):
        self.comparator = VisualComparator(pixel_threshold=25, min_contour_area=25)
        self.assessor = FrameQualityAssessor(blur_threshold=50.0)
        self.sm = CompartmentStateMachine(debounce_frames=1)

    def _make_crop(
        self,
        pill_count: int = 1,
        color: tuple[int, int, int] = (240, 240, 240),
        offset_x: int = 0,
        offset_y: int = 0,
        radius: int = 14,
        brightness_shift: int = 0,
    ) -> np.ndarray:
        """Create a synthetic 100x100 compartment crop."""
        bg = np.clip(35 + brightness_shift, 0, 255)
        crop = np.ones((100, 100, 3), dtype=np.uint8) * np.uint8(bg)

        if pill_count == 1:
            center = (50 + offset_x, 50 + offset_y)
            cv2.circle(crop, center, radius, color, -1)
            cv2.line(crop, (center[0] - 6, center[1]), (center[0] + 6, center[1]), (120, 120, 120), 2)
        elif pill_count == 2:
            c1 = (35 + offset_x, 50 + offset_y)
            c2 = (65 + offset_x, 50 + offset_y)
            cv2.circle(crop, c1, radius, color, -1)
            cv2.circle(crop, c2, radius, color, -1)

        return crop

    def test_scenario_01_unchanged_contents(self):
        """Scenario 1: Correct contents remain unchanged."""
        expected = self._make_crop(pill_count=1)
        observation = self._make_crop(pill_count=1)
        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        metrics = self.comparator.compare(expected, observation)
        transition = self.sm.evaluate("A", diff_from_expected=metrics.composite_diff)

        self.assertEqual(transition.new_state, CompartmentState.EXPECTED_PRESENT)
        self.assertFalse(transition.is_new_event)  # duplicate suppressed

    def test_scenario_02_object_removal(self):
        """Scenario 2: An expected object is removed."""
        expected = self._make_crop(pill_count=1)
        empty = self._make_crop(pill_count=0)
        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        metrics_exp = self.comparator.compare(expected, empty)
        metrics_emp = self.comparator.compare(empty, empty)

        transition = self.sm.evaluate(
            "A",
            diff_from_expected=metrics_exp.composite_diff,
            diff_from_empty=metrics_emp.composite_diff,
        )

        self.assertEqual(transition.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(transition.is_new_event)

    def test_scenario_03_object_returned(self):
        """Scenario 3: The same object is returned after removal."""
        expected = self._make_crop(pill_count=1)
        empty = self._make_crop(pill_count=0)
        returned = self._make_crop(pill_count=1)

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        # Step 1: Remove
        m_exp = self.comparator.compare(expected, empty)
        m_emp = self.comparator.compare(empty, empty)
        t_remove = self.sm.evaluate("A", m_exp.composite_diff, m_emp.composite_diff)
        self.assertEqual(t_remove.new_state, CompartmentState.REMOVAL_DETECTED)

        # Step 2: Return
        m_ret_exp = self.comparator.compare(expected, returned)
        m_ret_emp = self.comparator.compare(empty, returned)
        t_return = self.sm.evaluate("A", m_ret_exp.composite_diff, m_ret_emp.composite_diff)

        self.assertEqual(t_return.new_state, CompartmentState.OBJECT_RETURNED)
        self.assertTrue(t_return.is_new_event)

    def test_scenario_04_object_shifted_not_removed(self):
        """Scenario 4: An object is shifted slightly but not removed."""
        expected = self._make_crop(pill_count=1, offset_x=0)
        shifted = self._make_crop(pill_count=1, offset_x=6)

        metrics = self.comparator.compare(expected, shifted)
        self.assertTrue(metrics.is_shift_only or metrics.color_correlation > 0.85)
        # Verify it still detects pill presence (not empty)
        empty = self._make_crop(pill_count=0)
        diff_from_empty = self.comparator.compare(empty, shifted).composite_diff
        self.assertGreater(diff_from_empty, 0.10)

    def test_scenario_05_unrelated_object_introduced(self):
        """Scenario 5: An unrelated object (e.g. red token) is placed into compartment."""
        expected = self._make_crop(pill_count=1, color=(240, 240, 240))
        empty = self._make_crop(pill_count=0)
        unrelated = self._make_crop(pill_count=1, color=(0, 0, 220))  # Bright red object

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        m_exp = self.comparator.compare(expected, unrelated)
        m_emp = self.comparator.compare(empty, unrelated)

        transition = self.sm.evaluate("A", m_exp.composite_diff, m_emp.composite_diff)
        self.assertEqual(transition.new_state, CompartmentState.PARTIAL_OR_WRONG)

    def test_scenario_06_partial_removal(self):
        """Scenario 6: Some contents removed (from 2 pills to 1 pill)."""
        expected = self._make_crop(pill_count=2)
        empty = self._make_crop(pill_count=0)
        partial = self._make_crop(pill_count=1)

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        m_exp = self.comparator.compare(expected, partial)
        m_emp = self.comparator.compare(empty, partial)

        transition = self.sm.evaluate("A", m_exp.composite_diff, m_emp.composite_diff)
        self.assertEqual(transition.new_state, CompartmentState.PARTIAL_OR_WRONG)

    def test_scenario_07_lighting_change(self):
        """Scenario 7: Lighting changes while objects remain present."""
        expected = self._make_crop(pill_count=1)
        brightened = self._make_crop(pill_count=1, brightness_shift=30)

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        metrics = self.comparator.compare(expected, brightened)
        self.assertTrue(metrics.is_lighting_shift or metrics.composite_diff < 0.08)

        transition = self.sm.evaluate("A", metrics.composite_diff)
        # Lighting change does not trigger REMOVAL_DETECTED
        self.assertNotEqual(transition.new_state, CompartmentState.REMOVAL_DETECTED)

    def test_scenario_08_temporary_hand_occlusion(self):
        """Scenario 8: A hand occludes the compartment."""
        expected = self._make_crop(pill_count=1)
        hand_occluded = np.ones((100, 100, 3), dtype=np.uint8) * np.array([80, 120, 180], dtype=np.uint8)

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)

        # Occlusion detected -> UNCERTAIN
        transition = self.sm.evaluate("A", diff_from_expected=0.60, is_occluded=True)
        self.assertEqual(transition.new_state, CompartmentState.UNCERTAIN)

    def test_scenario_09_camera_blur_or_focus_loss(self):
        """Scenario 9: Camera loses focus or image is heavily blurred."""
        sharp = self._make_crop(pill_count=1)
        blurry = cv2.GaussianBlur(sharp, (35, 35), 15.0)

        quality = self.assessor.assess(blurry)
        self.assertTrue(quality.is_blurry)

        transition = self.sm.evaluate("A", diff_from_expected=0.25, is_blurry=True)
        self.assertEqual(transition.new_state, CompartmentState.UNCERTAIN)

    def test_scenario_10_wrong_compartment_modified(self):
        """Scenario 10: Compartment A was scheduled, but user modifies Compartment C."""
        exp_a = self._make_crop(pill_count=1)
        exp_c = self._make_crop(pill_count=1)
        empty = self._make_crop(pill_count=0)

        self.sm.set_initial_state("A", CompartmentState.EXPECTED_PRESENT)
        self.sm.set_initial_state("C", CompartmentState.EXPECTED_PRESENT)

        # A is untouched; C has pill removed
        t_a = self.sm.evaluate("A", self.comparator.compare(exp_a, exp_a).composite_diff)
        t_c = self.sm.evaluate(
            "C",
            self.comparator.compare(exp_c, empty).composite_diff,
            self.comparator.compare(empty, empty).composite_diff,
        )

        self.assertEqual(t_a.new_state, CompartmentState.EXPECTED_PRESENT)
        self.assertEqual(t_c.new_state, CompartmentState.REMOVAL_DETECTED)

    def test_scenario_11_reference_unavailable(self):
        """Scenario 11: Compartment has no saved reference baseline."""
        self.assertEqual(self.sm.get_state("B"), CompartmentState.UNKNOWN)

    def test_scenario_12_two_visually_similar_medications(self):
        """Scenario 12: Two visually similar round white tablets."""
        classifier = ClassicalHeuristicClassifier()

        tab1 = self._make_crop(pill_count=1, radius=13, color=(245, 245, 245))
        tab2 = self._make_crop(pill_count=1, radius=14, color=(235, 235, 235))

        res1 = classifier.classify(tab1, slot="A", expected_count=1)
        res2 = classifier.classify(tab2, slot="B", expected_count=1)

        self.assertEqual(res1.detected_count, 1)
        self.assertEqual(res2.detected_count, 1)
        # SAFETY INVARIANT: Classical vision must NOT claim distinct chemical identity
        self.assertLessEqual(res1.identification_confidence, 0.5)
        self.assertLessEqual(res2.identification_confidence, 0.5)
        self.assertIn("Classical morphology only", res1.notes)


if __name__ == "__main__":
    unittest.main()
