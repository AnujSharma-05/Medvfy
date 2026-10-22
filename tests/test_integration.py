"""End-to-End Integration Test: Camera Inspector Pipeline to Backend API."""

import tempfile
import unittest
from pathlib import Path
import numpy as np

import backend_test_env  # noqa: F401  (isolates data/config; must precede backend.app)
from backend.app import app
from vision.comparator import VisualComparator
from vision.event_logger import EventLogger
from vision.reference_store import ReferenceStore
from vision.roi_manager import ROIManager
from vision.state_machine import CompartmentState, CompartmentStateMachine


class TestEndToEndPipeline(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.baseline_file = self.data_dir / "vision_baseline.npz"
        self.log_file = self.data_dir / "vision_observations.csv"
        self.roi_file = self.data_dir / "rois.json"

        app.config["TESTING"] = True
        self.client = app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_full_reminder_removal_return_lifecycle(self):
        # 1. Trigger reminder on Backend for Compartment A
        remind_resp = self.client.post("/api/reminders", json={"compartment_id": "A"})
        self.assertEqual(remind_resp.status_code, 200)

        # 2. Setup Vision Pipeline components
        slots = ["A", "B", "C", "D"]
        roi_manager = ROIManager(self.roi_file)
        rois = {
            "A": [0, 0, 50, 50],
            "B": [50, 0, 50, 50],
            "C": [0, 50, 50, 50],
            "D": [50, 50, 50, 50],
        }
        frame_shape = (100, 100, 3)
        roi_manager.save(rois, frame_shape, camera_index=0)

        ref_store = ReferenceStore(self.baseline_file)
        comparator = VisualComparator(pixel_threshold=25)
        state_machine = CompartmentStateMachine(debounce_frames=1)

        # 3. Create initial baseline (pills in all 4 compartments)
        frame_baseline = np.ones(frame_shape, dtype=np.uint8) * 35
        # Add white pills at centers of all compartments
        import cv2
        for s, (x, y, w, h) in rois.items():
            cv2.circle(frame_baseline, (x + w // 2, y + h // 2), 10, (240, 240, 240), -1)

        crops_baseline = roi_manager.crop(frame_baseline, rois)
        ref_store.save_expected(crops_baseline, {"camera_width": 100, "camera_height": 100})

        for s in slots:
            state_machine.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)

        # Baseline array snapshot for immutability verification
        original_crop_a = ref_store.get_expected("A").copy()

        # 4. Medication removed from Compartment A
        frame_removed = frame_baseline.copy()
        # Clear compartment A floor
        frame_removed[0:50, 0:50] = 35

        crops_removed = roi_manager.crop(frame_removed, rois)
        m_a = comparator.compare(ref_store.get_expected("A"), crops_removed["A"])
        t_a = state_machine.evaluate("A", diff_from_expected=m_a.composite_diff)

        self.assertEqual(t_a.new_state, CompartmentState.REMOVAL_DETECTED)
        self.assertTrue(t_a.is_new_event)

        # Post observation to backend via test client
        obs_payload = {
            "compartment_id": "A",
            "state": t_a.new_state.value,
            "changed_pixel_fraction": m_a.composite_diff,
            "note": t_a.reason,
        }
        post_resp = self.client.post("/api/observations", json=obs_payload)
        self.assertEqual(post_resp.status_code, 201)

        # 5. CRITICAL CHECK: Invariant that baseline was NOT overwritten
        np.testing.assert_array_equal(ref_store.get_expected("A"), original_crop_a)

        # 6. Medication returned to Compartment A
        frame_returned = frame_baseline.copy()
        crops_returned = roi_manager.crop(frame_returned, rois)

        m_ret = comparator.compare(ref_store.get_expected("A"), crops_returned["A"])
        t_ret = state_machine.evaluate("A", diff_from_expected=m_ret.composite_diff)

        self.assertEqual(t_ret.new_state, CompartmentState.OBJECT_RETURNED)
        self.assertTrue(t_ret.is_new_event)

        # 7. Stationary check: no change occurs -> duplicate suppressed
        t_dup = state_machine.evaluate("A", diff_from_expected=m_ret.composite_diff)
        self.assertEqual(t_dup.new_state, CompartmentState.OBJECT_RETURNED)
        self.assertFalse(t_dup.is_new_event)

        # 8. Stop device alert
        stop_resp = self.client.post("/api/device/stop", json={})
        self.assertEqual(stop_resp.status_code, 200)

        # 9. Verify Backend Status reflects recent events and observations
        status_resp = self.client.get("/api/status")
        self.assertEqual(status_resp.status_code, 200)
        status_data = status_resp.get_json()
        self.assertTrue(status_data["ok"])
        self.assertGreaterEqual(len(status_data["observations"]), 1)


if __name__ == "__main__":
    unittest.main()
