"""ContinuousMonitor: stable-scene gate and time-based confirmation on synthetic frame streams.

These use synthetic frames and a simulated clock. They validate the timing and gating
logic, not real-camera accuracy.
"""

import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from vision.monitor import (
    EVALUATED,
    MOVING,
    NOT_READY,
    ContinuousMonitor,
    MonitorConfig,
)
from vision.reference_store import ReferenceStore
from vision.state_machine import CompartmentState, CompartmentStateMachine

SLOTS = ["A", "B", "C", "D"]
ROIS = {"A": [0, 0, 100, 100], "B": [100, 0, 100, 100], "C": [0, 100, 100, 100], "D": [100, 100, 100, 100]}
STEP = 0.1  # Simulated camera frame period (s)


def _background() -> np.ndarray:
    # Fixed texture so frames are sharp enough to pass the blur gate and identical between frames.
    rng = np.random.default_rng(7)
    return rng.integers(50, 110, (200, 200, 3), dtype=np.uint8)


BACKGROUND = _background()


def scene(pills: set[str]) -> np.ndarray:
    frame = BACKGROUND.copy()
    for slot in pills:
        x, y, w, h = ROIS[slot]
        cv2.circle(frame, (x + w // 2, y + h // 2), 14, (240, 240, 240), -1)
    return frame


def with_hand(frame: np.ndarray, step: int) -> np.ndarray:
    """A skin-toned block that moves every frame, as a reaching hand would."""
    out = frame.copy()
    x = 10 + (step * 17) % 120
    cv2.rectangle(out, (x, 20), (x + 70, 120), (120, 160, 210), -1)
    return out


EXPECTED = scene(set(SLOTS))
A_REMOVED = scene({"B", "C", "D"})
EMPTY = scene(set())


class TestContinuousMonitor(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = ReferenceStore(Path(self.temp_dir.name) / "baseline.npz")
        crop = lambda f: {s: f[y : y + h, x : x + w].copy() for s, (x, y, w, h) in ROIS.items()}
        self.store.save_expected(crop(EXPECTED))
        self.store.save_empty(crop(EMPTY))

        self.sm = CompartmentStateMachine(debounce_frames=2, confirm_seconds=1.0)
        for s in SLOTS:
            self.sm.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
        self.monitor = ContinuousMonitor(
            SLOTS, self.store, self.sm, MonitorConfig(eval_interval_s=0.5, settle_s=1.0)
        )
        self.monitor.rois = ROIS

    def tearDown(self):
        self.temp_dir.cleanup()

    def run_stream(self, frame_at, duration: float):
        """Feed frames every STEP seconds; return [(time, tick)]."""
        ticks = []
        for i in range(int(round(duration / STEP)) + 1):
            now = round(i * STEP, 3)
            ticks.append((now, self.monitor.process(frame_at(now, i), now)))
        return ticks

    @staticmethod
    def first_event(ticks, slot, state):
        for now, tick in ticks:
            for t in tick.new_events:
                if t.slot == slot and t.new_state == state:
                    return now
        return None

    def test_stable_unchanged_scene_is_evaluated_without_events(self):
        ticks = self.run_stream(lambda now, i: EXPECTED, 4.0)

        evaluated = [now for now, t in ticks if t.status == EVALUATED]
        self.assertGreaterEqual(len(evaluated), 4)
        self.assertGreaterEqual(evaluated[0], 1.0)  # not before the settle period
        self.assertEqual(sum(len(t.new_events) for _, t in ticks), 0)
        for s in SLOTS:
            self.assertEqual(self.sm.get_state(s), CompartmentState.EXPECTED_PRESENT)

    def test_moving_hand_is_never_evaluated(self):
        ticks = self.run_stream(lambda now, i: with_hand(EXPECTED, i), 5.0)

        self.assertTrue(all(t.status == MOVING for _, t in ticks))
        self.assertEqual(self.sm.history, {})
        for s in SLOTS:
            self.assertEqual(self.sm.get_state(s), CompartmentState.EXPECTED_PRESENT)

    def test_removal_confirmed_only_after_settle_and_confirm_time(self):
        # Pill in A disappears at t=1.0; the scene is otherwise still.
        ticks = self.run_stream(lambda now, i: EXPECTED if now < 1.0 else A_REMOVED, 5.0)

        detected_at = self.first_event(ticks, "A", CompartmentState.REMOVAL_DETECTED)
        self.assertIsNotNone(detected_at)
        # settle (1.0 s) + confirm (1.0 s) after the change; evaluation cadence adds < 0.6 s
        self.assertGreaterEqual(detected_at, 1.0 + 1.0 + 1.0)
        self.assertLessEqual(detected_at, 1.0 + 1.0 + 1.0 + 0.6)
        self.assertIsNotNone(self.first_event(ticks, "A", CompartmentState.REMOVAL_CANDIDATE))

        removal_events = [
            t for _, tick in ticks for t in tick.new_events if t.new_state == CompartmentState.REMOVAL_DETECTED
        ]
        self.assertEqual(len(removal_events), 1)  # no repeat while A stays empty
        for s in ("B", "C", "D"):
            self.assertEqual(self.sm.get_state(s), CompartmentState.EXPECTED_PRESENT)

    def test_motion_during_candidate_restarts_confirmation(self):
        # A removed at t=1.0 -> candidate around t=2.1; a hand moves over the box from
        # t=2.4 to t=3.0, i.e. before confirmation (~3.1) would have happened.
        def frame_at(now, i):
            if now < 1.0:
                return EXPECTED
            if 2.4 <= now < 3.0:
                return with_hand(A_REMOVED, i)
            return A_REMOVED

        ticks = self.run_stream(frame_at, 7.0)

        candidate_at = self.first_event(ticks, "A", CompartmentState.REMOVAL_CANDIDATE)
        self.assertIsNotNone(candidate_at)
        self.assertLess(candidate_at, 2.4)

        detected_at = self.first_event(ticks, "A", CompartmentState.REMOVAL_DETECTED)
        self.assertIsNotNone(detected_at)
        # Must re-settle (1.0 s) and re-confirm (1.0 s) after motion ends at 3.0
        self.assertGreaterEqual(detected_at, 3.0 + 1.0 + 1.0)
        self.assertLessEqual(detected_at, 3.0 + 1.0 + 1.0 + 0.7)

    def test_not_ready_without_regions_or_references(self):
        self.monitor.rois = None
        self.assertEqual(self.monitor.process(EXPECTED, 0.0).status, NOT_READY)

        self.monitor.rois = ROIS
        self.store.clear()
        self.assertEqual(self.monitor.process(EXPECTED, 0.0).status, NOT_READY)

    def test_blur_is_judged_relative_to_calibration(self):
        """Regression: a real webcam on a plain page scored sharpness 13-22, under the fixed
        threshold of 60, so every evaluation was UNCERTAIN. Judge blur against calibration."""
        soft_bg = np.random.default_rng(3).integers(88, 96, (200, 200, 3), dtype=np.uint8)
        soft = cv2.GaussianBlur(soft_bg, (3, 3), 0)  # low-texture but in-focus scene
        self.assertLess(self.monitor.assessor.compute_blur_score(soft), 60)
        crop = lambda f: {s: f[y : y + h, x : x + w].copy() for s, (x, y, w, h) in ROIS.items()}
        self.store.save_expected(crop(soft))

        # Old behaviour (no calibration sharpness recorded): absolute 60 -> unusable
        self.assertTrue(self.monitor.frame_unusable(self.monitor.assessor.assess(soft)))

        self.store.save_expected(crop(soft), {"calibration_sharpness":
                                              self.monitor.assessor.compute_blur_score(soft)})
        self.assertFalse(self.monitor.frame_unusable(self.monitor.assessor.assess(soft)))
        states = {t.slot: t.new_state for t in self.monitor.evaluate(soft, observed_at=0.0)}
        self.assertEqual(set(states.values()), {CompartmentState.EXPECTED_PRESENT})

        # Genuinely defocused relative to calibration -> still rejected
        blurred = cv2.GaussianBlur(soft, (15, 15), 6)
        self.assertTrue(self.monitor.frame_unusable(self.monitor.assessor.assess(blurred)))
        # A black frame is never usable
        self.assertTrue(self.monitor.frame_unusable(self.monitor.assessor.assess(np.zeros_like(soft))))

    def test_missing_reference_for_a_slot_is_uncertain_not_removed(self):
        """The comparator scores a missing reference as 1.0; that must not read as removal."""
        del self.store.expected["D"]
        transitions = {t.slot: t for t in self.monitor.evaluate(EXPECTED, observed_at=0.0)}

        self.assertEqual(transitions["D"].new_state, CompartmentState.UNCERTAIN)
        self.assertEqual(transitions["A"].new_state, CompartmentState.EXPECTED_PRESENT)


if __name__ == "__main__":
    unittest.main()
