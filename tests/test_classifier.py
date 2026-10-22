"""Unit tests for medication object counting and classification interface."""

import unittest
import cv2
import numpy as np

from vision.classifier_interface import ClassicalHeuristicClassifier


class TestClassicalHeuristicClassifier(unittest.TestCase):
    def setUp(self):
        self.classifier = ClassicalHeuristicClassifier(min_area=30, max_area=5000)

    def test_count_single_tablet(self):
        crop = np.ones((80, 80, 3), dtype=np.uint8) * 30  # dark floor
        cv2.circle(crop, (40, 40), 12, (240, 240, 240), -1)  # white pill

        res = self.classifier.classify(crop, slot="A", expected_count=1)
        self.assertEqual(res.detected_count, 1)
        self.assertTrue(res.count_matches)
        self.assertEqual(res.objects[0].color_family, "white")
        # Explicit conservative confidence requirement
        self.assertLessEqual(res.identification_confidence, 0.5)

    def test_count_two_tablets(self):
        crop = np.ones((100, 100, 3), dtype=np.uint8) * 30
        cv2.circle(crop, (30, 50), 10, (230, 230, 230), -1)
        cv2.circle(crop, (70, 50), 10, (0, 165, 255), -1)  # orange pill

        res = self.classifier.classify(crop, slot="B", expected_count=2)
        self.assertEqual(res.detected_count, 2)
        self.assertTrue(res.count_matches)

    def test_count_mismatch(self):
        crop = np.ones((80, 80, 3), dtype=np.uint8) * 30
        cv2.circle(crop, (40, 40), 12, (240, 240, 240), -1)

        # Expected 2 pills, but only 1 present
        res = self.classifier.classify(crop, slot="C", expected_count=2)
        self.assertEqual(res.detected_count, 1)
        self.assertFalse(res.count_matches)

    def test_empty_compartment(self):
        crop = np.ones((80, 80, 3), dtype=np.uint8) * 30
        res = self.classifier.classify(crop, slot="D", expected_count=0)
        self.assertEqual(res.detected_count, 0)
        self.assertTrue(res.count_matches)


if __name__ == "__main__":
    unittest.main()
