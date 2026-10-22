"""Unit tests for VisualComparator: SSIM, illumination normalization, contour analysis."""

import unittest
import numpy as np

from vision.comparator import VisualComparator


class TestVisualComparator(unittest.TestCase):
    def setUp(self):
        self.comparator = VisualComparator(pixel_threshold=25, min_contour_area=20)

    def _create_synthetic_compartment(self, pill_present: bool = True, offset: int = 0) -> np.ndarray:
        """Create a synthetic 100x100 compartment with dark background and optional pill."""
        img = np.ones((100, 100, 3), dtype=np.uint8) * 40  # dark compartment floor
        if pill_present:
            # Draw a white circular pill at center + offset
            import cv2
            center = (50 + offset, 50)
            cv2.circle(img, center, 14, (240, 240, 240), -1)
            # Add subtle cross score on pill
            cv2.line(img, (center[0] - 8, center[1]), (center[0] + 8, center[1]), (180, 180, 180), 2)
        return img

    def test_identical_images(self):
        img = self._create_synthetic_compartment(pill_present=True)
        metrics = self.comparator.compare(img, img.copy())

        self.assertAlmostEqual(metrics.composite_diff, 0.0, places=2)
        self.assertAlmostEqual(metrics.structural_dissimilarity, 0.0, places=2)
        self.assertAlmostEqual(metrics.color_correlation, 1.0, places=2)
        self.assertEqual(metrics.contour_count, 0)

    def test_pill_removal(self):
        expected = self._create_synthetic_compartment(pill_present=True)
        empty = self._create_synthetic_compartment(pill_present=False)

        metrics = self.comparator.compare(expected, empty)
        self.assertGreater(metrics.composite_diff, 0.08)
        self.assertGreaterEqual(metrics.structural_dissimilarity, 0.04)
        self.assertGreaterEqual(metrics.contour_count, 1)

    def test_lighting_shift_robustness(self):
        """Uniform illumination shift should not be classified as object removal."""
        img1 = self._create_synthetic_compartment(pill_present=True)
        # Shift brightness up by 25 levels
        img2 = np.clip(img1.astype(np.int16) + 25, 0, 255).astype(np.uint8)

        metrics = self.comparator.compare(img1, img2)
        # Structural dissimilarity should remain very low because structural edges are identical
        self.assertLess(metrics.structural_dissimilarity, 0.10)
        self.assertTrue(metrics.is_lighting_shift or metrics.composite_diff < 0.08)

    def test_pill_movement_shift(self):
        """Slight pill movement inside compartment."""
        img1 = self._create_synthetic_compartment(pill_present=True, offset=0)
        img2 = self._create_synthetic_compartment(pill_present=True, offset=5)

        metrics = self.comparator.compare(img1, img2)
        self.assertGreater(metrics.color_correlation, 0.80)
        self.assertTrue(metrics.is_shift_only or metrics.structural_dissimilarity < 0.25)

    def _three_pill_compartment(self) -> np.ndarray:
        import cv2
        img = np.full((120, 120, 3), 40, dtype=np.uint8)
        for cx in (30, 60, 90):
            cv2.circle(img, (cx, 60), 12, (240, 240, 240), -1)
        return img

    def test_fully_covered_frame_stays_on_unit_scale(self):
        """Regression: a count change plus a full-frame diff mask used to score ~1.97.

        Simulates a hand or lid covering the whole compartment. The score must stay a
        [0, 1] dissimilarity fraction while still reading as a large change.
        """
        expected = self._three_pill_compartment()
        covered = np.full_like(expected, 200)

        metrics = self.comparator.compare(expected, covered)
        self.assertLessEqual(metrics.composite_diff, 1.0)
        self.assertGreater(metrics.composite_diff, 0.08)

    def test_composite_bounded_for_varied_inputs(self):
        expected = self._three_pill_compartment()
        rng = np.random.default_rng(0)
        checker = (np.indices((120, 120)).sum(0) // 6 % 2 * 255).astype(np.uint8)
        observations = {
            "noise": rng.integers(0, 256, (120, 120, 3), dtype=np.uint8),
            "checker": np.repeat(checker[..., None], 3, axis=2),
            "black": np.zeros_like(expected),
            "white": np.full_like(expected, 255),
            "different_size": np.full((60, 200, 3), 120, dtype=np.uint8),
        }
        for name, observation in observations.items():
            with self.subTest(observation=name):
                metrics = self.comparator.compare(expected, observation)
                self.assertGreaterEqual(metrics.composite_diff, 0.0)
                self.assertLessEqual(metrics.composite_diff, 1.0)


if __name__ == "__main__":
    unittest.main()
