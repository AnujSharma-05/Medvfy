"""Unit tests for FrameQualityAssessor: blur, brightness, and motion stability."""

import unittest
import cv2
import numpy as np

from vision.frame_quality import FrameQualityAssessor


class TestFrameQualityAssessor(unittest.TestCase):
    def setUp(self):
        self.assessor = FrameQualityAssessor(
            blur_threshold=50.0,
            min_brightness=20.0,
            max_brightness=235.0,
            motion_threshold=0.03,
        )

    def test_sharp_vs_blurred_image(self):
        # Create a sharp image with high-contrast edges/checkerboard
        sharp = np.zeros((100, 100, 3), dtype=np.uint8)
        sharp[::10, :] = 255
        sharp[:, ::10] = 255

        # Create a severely blurred version
        blurred = cv2.GaussianBlur(sharp, (31, 31), 10.0)

        res_sharp = self.assessor.assess(sharp)
        res_blurred = self.assessor.assess(blurred)

        self.assertTrue(res_sharp.is_acceptable)
        self.assertFalse(res_sharp.is_blurry)
        self.assertFalse(res_blurred.is_acceptable)
        self.assertTrue(res_blurred.is_blurry)

    def test_exposure_detection(self):
        # Very dark image
        dark = np.ones((50, 50, 3), dtype=np.uint8) * 5
        res_dark = self.assessor.assess(dark)
        self.assertFalse(res_dark.is_acceptable)
        self.assertTrue(res_dark.is_underexposed)

        # Very bright/overexposed image
        bright = np.ones((50, 50, 3), dtype=np.uint8) * 250
        res_bright = self.assessor.assess(bright)
        self.assertFalse(res_bright.is_acceptable)
        self.assertTrue(res_bright.is_overexposed)

    def test_motion_stability(self):
        frame1 = np.ones((100, 100, 3), dtype=np.uint8) * 120
        frame2 = frame1.copy()

        # Perfectly stationary
        stable, motion = self.assessor.check_motion_stability([frame1, frame2])
        self.assertTrue(stable)
        self.assertEqual(motion, 0.0)

        # Significant motion/change
        frame3 = frame1.copy()
        frame3[20:80, 20:80] = 0
        stable, motion = self.assessor.check_motion_stability([frame1, frame3])
        self.assertFalse(stable)
        self.assertGreater(motion, 0.03)

    def test_invalid_and_empty_frame(self):
        res = self.assessor.assess(None)
        self.assertFalse(res.is_acceptable)

        empty = np.array([])
        res_empty = self.assessor.assess(empty)
        self.assertFalse(res_empty.is_acceptable)


if __name__ == "__main__":
    unittest.main()
