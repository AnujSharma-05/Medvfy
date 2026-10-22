"""Unit test for camera_inspector.py interactive main() flow using mocked OpenCV."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import numpy as np

import vision.camera_inspector as ci


class TestCameraInspectorFlow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

        ci.CONFIG_DIR = self.temp_path / "config"
        ci.DATA_DIR = self.temp_path / "data"
        ci.SETTINGS_FILE = ci.CONFIG_DIR / "settings.json"
        ci.ROI_FILE = ci.CONFIG_DIR / "rois.json"
        ci.BASELINE_FILE = ci.DATA_DIR / "vision_baseline.npz"
        ci.STATE_FILE = ci.DATA_DIR / "vision_state.json"
        ci.LOG_FILE = ci.DATA_DIR / "vision_observations.csv"

        ci.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        ci.DATA_DIR.mkdir(parents=True, exist_ok=True)

        ci.SETTINGS_FILE.write_text(
            '{"compartments": ["A", "B", "C", "D"], "change_threshold": 0.08, "camera_index": 0,'
            ' "server_url": "http://backend.test"}',
            encoding="utf-8",
        )
        # Never let a test reach a real backend (one may be running on 127.0.0.1:5000).
        post_patcher = patch("vision.event_logger.requests.post", return_value=MagicMock(status_code=201))
        self.default_post = post_patcher.start()
        self.addCleanup(post_patcher.stop)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def observation_posts(mock_post) -> list:
        return [c for c in mock_post.call_args_list if c.args and c.args[0].endswith("/api/observations")]

    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_main_key_sequence_execution(self, mock_cv2, mock_open_camera):
        # Create a mock video capture
        mock_cap = MagicMock()
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        mock_cap.read.return_value = (True, frame)
        mock_open_camera.return_value = mock_cap

        # Pre-seed rois
        manager = ci.ROIManager(ci.ROI_FILE)
        manager.save(
            {
                "A": [0, 0, 50, 50],
                "B": [50, 0, 50, 50],
                "C": [0, 50, 50, 50],
                "D": [50, 50, 50, 50],
            },
            frame.shape,
            camera_index=0,
        )

        # Sequence of keys:
        # 1. 's' -> save baseline
        # 2. 'v' -> compare (exercises assessor, comparator, state_machine, logger)
        # 3. 'q' -> quit
        key_sequence = [ord("s"), ord("v"), ord("q")]
        mock_cv2.waitKey.side_effect = key_sequence
        mock_cv2.selectROI.return_value = (0, 0, 50, 50)

        # Run main without throwing any NameError or exceptions
        ci.main()

        # Verify baseline was created
        self.assertTrue(ci.BASELINE_FILE.exists())
        # Verify log was written
        self.assertTrue(ci.LOG_FILE.exists())

    def _seed_rois(self, frame):
        ci.ROIManager(ci.ROI_FILE).save(
            {
                "A": [0, 0, 50, 50],
                "B": [50, 0, 50, 50],
                "C": [0, 50, 50, 50],
                "D": [50, 50, 50, 50],
            },
            frame.shape,
            camera_index=0,
        )

    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_redrawing_regions_discards_in_memory_references(self, mock_cv2, mock_open_camera):
        """Regression: after R, V compared against the pre-R references still held in memory.

        select_rois cleared a separate ReferenceStore (deleting only the files), so main()'s
        store kept the old crops and V ran a comparison against the wrong layout.
        """
        mock_cap = MagicMock()
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        mock_cap.read.return_value = (True, frame)
        mock_open_camera.return_value = mock_cap
        self._seed_rois(frame)

        # S = calibrate expected, R = redraw regions (invalidates references), V = compare, Q = quit
        mock_cv2.waitKey.side_effect = [ord("s"), ord("r"), ord("v"), ord("q")]
        mock_cv2.selectROI.return_value = (10, 10, 40, 40)

        ci.main()

        # References from the old layout are gone on disk...
        self.assertFalse(ci.BASELINE_FILE.exists())
        # ...and in memory: V must refuse to compare, so no observation is logged.
        self.assertFalse(ci.LOG_FILE.exists())

    @patch("vision.event_logger.requests.post")
    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_repeated_compare_on_stable_scene_logs_once(self, mock_cv2, mock_open_camera, mock_post):
        """Regression: each V press re-logged and re-POSTed every compartment, even unchanged."""
        mock_post.return_value = MagicMock(status_code=201)
        mock_cap = MagicMock()
        # Uniform frame fails the blur gate, so every V yields UNCERTAIN for all four slots.
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        mock_cap.read.return_value = (True, frame)
        mock_open_camera.return_value = mock_cap
        self._seed_rois(frame)

        mock_cv2.waitKey.side_effect = [ord("s"), ord("v"), ord("v"), ord("v"), ord("q")]

        ci.main()

        with ci.LOG_FILE.open("r", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        # First V: EXPECTED_PRESENT -> UNCERTAIN is a new event per slot; later presses are not.
        self.assertEqual(len(rows), 4)
        self.assertEqual({r[2] for r in rows}, {"UNCERTAIN"})
        self.assertEqual(len(self.observation_posts(mock_post)), 4)

    @patch("vision.event_logger.requests.post")
    @patch("vision.camera_inspector.time")
    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_monitoring_mode_evaluates_without_key_presses(
        self, mock_cv2, mock_open_camera, mock_time, mock_post
    ):
        """M starts continuous monitoring: evaluations happen on their own, and a stable
        scene is logged once rather than once per evaluation."""
        mock_post.return_value = MagicMock(status_code=201)
        clock = iter(i * 0.1 for i in range(10_000))
        mock_time.monotonic.side_effect = lambda: next(clock)

        mock_cap = MagicMock()
        # Uniform frame: still (passes the motion gate) but fails the blur gate -> UNCERTAIN.
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        mock_cap.read.return_value = (True, frame)
        mock_open_camera.return_value = mock_cap
        self._seed_rois(frame)

        # S, M, then 70 frames (~7 s simulated, past one 5 s heartbeat) with no key, then Q
        mock_cv2.waitKey.side_effect = [ord("s"), ord("m")] + [255] * 70 + [ord("q")]

        ci.main()

        with ci.LOG_FILE.open("r", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
        self.assertEqual(len(rows), 4)
        self.assertEqual({r[2] for r in rows}, {"UNCERTAIN"})
        # Several evaluations ran (settle 1 s, then every 0.5 s), but only the first was new
        self.assertGreaterEqual(mock_time.monotonic.call_count, 30)
        self.assertEqual(len(self.observation_posts(mock_post)), 4)

        # Heartbeats went out roughly every 5 simulated seconds, carrying monitoring state
        heartbeats = [c for c in mock_post.call_args_list if c.args[0].endswith("/api/vision/heartbeat")]
        self.assertGreaterEqual(len(heartbeats), 1)
        last = heartbeats[-1].kwargs["json"]
        self.assertTrue(last["monitoring"])
        self.assertEqual(set(last["states"]), {"A", "B", "C", "D"})
        observation = self.observation_posts(mock_post)[0].kwargs["json"]
        self.assertEqual(observation["previous_state"], "EXPECTED_PRESENT")

    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_monitoring_refuses_to_start_without_baseline(self, mock_cv2, mock_open_camera):
        mock_cap = MagicMock()
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        mock_cap.read.return_value = (True, frame)
        mock_open_camera.return_value = mock_cap
        self._seed_rois(frame)

        mock_cv2.waitKey.side_effect = [ord("m")] + [255] * 20 + [ord("q")]

        ci.main()

        self.assertFalse(ci.LOG_FILE.exists())

    def test_warm_up_discards_dark_frames(self):
        dark = np.zeros((10, 10, 3), dtype=np.uint8)
        bright = np.full((10, 10, 3), 120, dtype=np.uint8)
        cap = MagicMock()
        cap.read.side_effect = [(True, dark)] * 8 + [(True, bright)] * 5
        self.assertTrue(ci.warm_up(cap, max_seconds=5.0))
        self.assertEqual(cap.read.call_count, 13)

        always_dark = MagicMock()
        always_dark.read.return_value = (True, dark)
        self.assertFalse(ci.warm_up(always_dark, max_seconds=0.05))

    def test_select_rois_clears_callers_store_and_records_camera(self):
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 50
        store = ci.ReferenceStore(ci.BASELINE_FILE)
        store.save_expected({"A": frame[0:50, 0:50]}, {"camera_index": 1})
        self.assertTrue(store.has_expected(["A"]))

        with patch("vision.camera_inspector.cv2") as mock_cv2:
            mock_cv2.selectROI.return_value = (10, 10, 40, 40)
            rois = ci.select_rois(frame, ["A"], ref_store=store, camera_index=1)

        self.assertEqual(rois, {"A": [10, 10, 40, 40]})
        self.assertFalse(store.has_expected(["A"]))
        self.assertIsNone(store.get_expected("A"))
        saved = ci.json.loads(ci.ROI_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["camera_index"], 1)


if __name__ == "__main__":
    unittest.main()
