"""Unit tests for ROIManager: bounds checking, resolution matching, and legacy compatibility."""

import json
import tempfile
import unittest
from pathlib import Path
import numpy as np

from vision.roi_manager import ROIManager


class TestROIManager(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.roi_path = Path(self.temp_dir.name) / "rois.json"
        self.manager = ROIManager(self.roi_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_save_and_load_valid_rois(self):
        slots = ["A", "B", "C", "D"]
        rois = {
            "A": [10, 10, 50, 50],
            "B": [70, 10, 50, 50],
            "C": [10, 70, 50, 50],
            "D": [70, 70, 50, 50],
        }
        frame_shape = (200, 200, 3)

        self.manager.save(rois, frame_shape, camera_index=0)
        self.assertTrue(self.roi_path.exists())

        loaded = self.manager.load(slots, frame_shape, camera_index=0)
        self.assertEqual(loaded, rois)

    def test_out_of_bounds_rejected(self):
        slots = ["A"]
        # Box extends to 10 + 200 = 210, which exceeds width of 200
        rois = {"A": [10, 10, 200, 50]}
        frame_shape = (200, 200, 3)

        self.manager.save(rois, (300, 300, 3))
        loaded = self.manager.load(slots, frame_shape)
        self.assertIsNone(loaded)

    def test_resolution_mismatch_rejected(self):
        slots = ["A"]
        rois = {"A": [10, 10, 40, 40]}
        self.manager.save(rois, (720, 1280, 3))

        # Try loading on 480x640 frame
        loaded = self.manager.load(slots, (480, 640, 3))
        self.assertIsNone(loaded)

    def test_camera_index_mismatch_rejected(self):
        slots = ["A"]
        rois = {"A": [10, 10, 40, 40]}
        self.manager.save(rois, (200, 200, 3), camera_index=0)

        # Loaded with camera_index 1
        loaded = self.manager.load(slots, (200, 200, 3), camera_index=1)
        self.assertIsNone(loaded)

    def test_legacy_flat_format_compatibility(self):
        legacy_data = {
            "A": [10, 10, 40, 40],
            "B": [60, 10, 40, 40],
        }
        self.roi_path.write_text(json.dumps(legacy_data), encoding="utf-8")

        loaded = self.manager.load(["A", "B"], (200, 200, 3))
        self.assertEqual(loaded, legacy_data)

    def test_crop_compartments(self):
        frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frame[10:30, 10:30] = 255  # Fill compartment A
        rois = {"A": [10, 10, 20, 20], "B": [40, 40, 20, 20]}

        crops = self.manager.crop(frame, rois)
        self.assertEqual(crops["A"].shape, (20, 20, 3))
        self.assertEqual(crops["B"].shape, (20, 20, 3))
        self.assertTrue(np.all(crops["A"] == 255))
        self.assertTrue(np.all(crops["B"] == 0))


if __name__ == "__main__":
    unittest.main()
