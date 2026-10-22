"""Trial recorder, trial evaluator, and trial mode in the camera inspector (synthetic frames)."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cv2
import numpy as np

from vision.monitor import EVALUATED, ContinuousMonitor, MonitorConfig
from vision.reference_store import ReferenceStore
from vision.state_machine import CompartmentState, CompartmentStateMachine
from vision.trial_eval import Params, grade, load_trials, replay, rescore, separability, sweep
from vision.trials import TrialRecorder, next_label, state_category

SLOTS = ["A", "B", "C", "D"]
ROIS = {"A": [0, 0, 100, 100], "B": [100, 0, 100, 100], "C": [0, 100, 100, 100], "D": [100, 100, 100, 100]}
BACKGROUND = np.random.default_rng(7).integers(50, 110, (200, 200, 3), dtype=np.uint8)


def scene(pills):
    frame = BACKGROUND.copy()
    for slot in pills:
        x, y, w, h = ROIS[slot]
        cv2.circle(frame, (x + w // 2, y + h // 2), 14, (240, 240, 240), -1)
    return frame


LOADED = scene(SLOTS)
EMPTY = scene([])


def crops(frame):
    return {s: frame[y : y + h, x : x + w].copy() for s, (x, y, w, h) in ROIS.items()}


class TrialTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = ReferenceStore(self.root / "baseline.npz")
        self.store.save_expected(crops(LOADED))
        self.store.save_empty(crops(EMPTY))
        self.recorder = TrialRecorder(self.root / "trials", SLOTS, settings={"change_threshold": 0.08})

    def tearDown(self):
        self.tmp.cleanup()

    def run_trial(self, labels, frame_at, duration=5.0):
        """Record one trial through the real monitor; returns the TrialSummary."""
        # Labels persist between trials (handy for repeats), so set every compartment explicitly.
        for slot in SLOTS:
            while self.recorder.labels[slot] != labels.get(slot, "unchanged"):
                self.recorder.cycle_label(slot)
        sm = CompartmentStateMachine(debounce_frames=2, confirm_seconds=1.0)
        for s in SLOTS:
            sm.set_initial_state(s, CompartmentState.EXPECTED_PRESENT)
        monitor = ContinuousMonitor(SLOTS, self.store, sm, MonitorConfig(eval_interval_s=0.5, settle_s=1.0))
        monitor.rois = ROIS
        self.recorder.start(0.0, ROIS, self.store, camera_index=0)
        for i in range(int(duration * 10) + 1):
            now = round(i * 0.1, 3)
            frame = frame_at(now)
            tick = monitor.process(frame, now)
            if tick.status == EVALUATED:
                self.recorder.record(frame, tick.transitions, now)
        self.last_sm = sm
        return self.recorder.finish(duration, {s: sm.get_state(s).value for s in SLOTS})


class TestTrialRecorder(TrialTestBase):
    def test_labels_cycle_and_lock_during_trial(self):
        self.assertEqual(next_label("unchanged"), "removed")
        self.assertEqual(next_label("changed"), "unchanged")
        self.assertEqual(self.recorder.cycle_label("A"), "removed")
        self.recorder.start(0.0, ROIS, self.store)
        self.assertEqual(self.recorder.cycle_label("A"), "removed")  # locked
        self.recorder.finish(1.0, {s: "EXPECTED_PRESENT" for s in SLOTS})

    def test_removal_trial_is_graded_and_saved(self):
        summary = self.run_trial({"A": "removed"}, lambda t: LOADED if t < 2.0 else scene(["B", "C", "D"]))

        self.assertEqual(summary.passed, 4)
        a = next(r for r in summary.results if r.slot == "A")
        self.assertEqual(a.final_state, "REMOVAL_DETECTED")
        self.assertIsNotNone(a.confirmed_after_s)
        self.assertGreaterEqual(a.confirmed_after_s, 4.0)  # acted at 2 s, then 1 s settle + 1 s confirm
        self.assertEqual(summary.flags, [])

        meta = json.loads((summary.path / "trial.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["status"], "finished")
        self.assertEqual(meta["labels"]["A"], "removed")
        self.assertFalse(meta["start_mismatch"])
        lines = (summary.path / "evaluations.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), summary.evaluations)
        self.assertEqual(len(list((summary.path / "frames").glob("*.jpg"))), summary.evaluations)
        with np.load(summary.path / "references.npz") as refs:
            self.assertEqual(sorted(refs.files), sorted([f"expected_{s}" for s in SLOTS] + [f"empty_{s}" for s in SLOTS]))

    def test_wrong_action_fails_the_right_compartments(self):
        # Said B would be removed, actually removed A
        summary = self.run_trial({"B": "removed"}, lambda t: LOADED if t < 2.0 else scene(["B", "C", "D"]))
        failed = {r.slot for r in summary.results if not r.passed}
        self.assertEqual(failed, {"A", "B"})

    def test_box_not_as_calibrated_at_start_is_flagged(self):
        summary = self.run_trial({}, lambda t: scene(["B", "C", "D"]), duration=3.0)
        self.assertTrue(any("already differed" in f for f in summary.flags))
        meta = json.loads((summary.path / "trial.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["start_mismatch"])

    def test_trial_that_never_settles_cannot_pass(self):
        rng = np.random.default_rng(1)
        summary = self.run_trial({}, lambda t: rng.integers(0, 256, (200, 200, 3), dtype=np.uint8), duration=2.0)
        self.assertEqual(summary.evaluations, 0)
        self.assertEqual(summary.passed, 0)
        self.assertTrue(any("No evaluations" in f for f in summary.flags))


class TestTrialEvaluator(TrialTestBase):
    def record_set(self):
        self.run_trial({"A": "removed"}, lambda t: LOADED if t < 2.0 else scene(["B", "C", "D"]))
        self.run_trial({"C": "removed", "D": "removed"}, lambda t: LOADED if t < 2.0 else scene(["A", "B"]))
        self.run_trial({}, lambda t: LOADED)

    def test_replay_matches_live_decisions(self):
        summary = self.run_trial({"A": "removed"}, lambda t: LOADED if t < 2.0 else scene(["B", "C", "D"]))
        trials, _ = load_trials(self.root / "trials")
        finals = replay(trials[0], Params(0.08, 0.05, 2, 1.0))
        self.assertEqual(finals, {r.slot: r.final_state for r in summary.results})

    def test_grade_sweep_and_separability(self):
        self.record_set()
        trials, skipped = load_trials(self.root / "trials")
        self.assertEqual((len(trials), skipped), (3, []))

        current = grade(trials, Params(0.08, 0.05, 2, 1.0))
        self.assertEqual((current.correct, current.total), (12, 12))
        self.assertEqual(current.confusion["removed"]["removed"], 3)

        # A "matches loaded" tolerance looser than the removal scores (0.15 here) hides every removal.
        # (A high change_threshold alone does not: a scene that matches the EMPTY photo still
        # resolves to removed through the uncertain path, which is real evidence.)
        loose = grade(trials, Params(0.30, 0.20, 2, 1.0))
        self.assertEqual(loose.removal_missed, 3)
        self.assertGreater(loose.cost, current.cost)

        ranked = sweep(trials, Params(0.08, 0.05, 2, 1.0))
        self.assertLessEqual(ranked[0].cost, current.cost)
        self.assertTrue(all(g.params.match_threshold < g.params.change_threshold for g in ranked))

        sep = separability(trials)
        self.assertGreater(sep["removed"]["diff_from_expected"]["min"], sep["unchanged"]["diff_from_expected"]["max"])

    def test_flagged_and_unfinished_trials_are_skipped(self):
        self.run_trial({}, lambda t: scene(["B", "C", "D"]), duration=3.0)  # start mismatch
        self.recorder.start(0.0, ROIS, self.store)  # left running
        trials, skipped = load_trials(self.root / "trials")
        self.assertEqual(trials, [])
        self.assertEqual(len(skipped), 2)
        trials, _ = load_trials(self.root / "trials", include_flagged=True)
        self.assertEqual(len(trials), 1)

    def test_rescore_from_saved_frames(self):
        self.run_trial({"A": "removed"}, lambda t: LOADED if t < 2.0 else scene(["B", "C", "D"]))
        trial = load_trials(self.root / "trials")[0][0]
        redone = rescore(trial)
        last = redone.evaluations[-1]["slots"]
        self.assertGreater(last["A"]["diff_from_expected"], 0.08)
        self.assertLess(last["A"]["diff_from_empty"], last["A"]["diff_from_expected"])
        self.assertEqual(grade([redone], Params(0.08, 0.05, 2, 1.0)).removal_missed, 0)

    def test_report_builds(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "evaluate_trials", Path(__file__).resolve().parents[1] / "tools" / "evaluate_trials.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.record_set()
        trials, skipped = load_trials(self.root / "trials")
        lines, best = mod.build_report(trials, skipped, Params(0.08, 0.05, 2, 1.0), rescored=False)
        text = "\n".join(lines)
        self.assertIn("Correct: **12/12**", text)
        self.assertIn("only 3 trials", text)
        empty_lines, _ = mod.build_report([], [], Params(0.08, 0.05), rescored=False)
        self.assertIn("No finished trials yet", "\n".join(empty_lines))


class TestStateCategory(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(state_category("OBJECT_RETURNED"), "loaded")
        self.assertEqual(state_category("EMPTY"), "removed")
        self.assertEqual(state_category("PARTIAL_OR_WRONG"), "changed")
        self.assertEqual(state_category("REMOVAL_CANDIDATE"), "unclear")
        self.assertEqual(state_category(None), "unclear")


class TestInspectorTrialMode(unittest.TestCase):
    """T, label A, N, remove A, F, T, Q through the real inspector loop (camera and window mocked)."""

    def setUp(self):
        import vision.camera_inspector as ci
        self.ci = ci
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = {k: getattr(ci, k) for k in ("CONFIG_DIR", "DATA_DIR", "SETTINGS_FILE", "ROI_FILE",
                                                  "BASELINE_FILE", "STATE_FILE", "LOG_FILE")}
        ci.CONFIG_DIR, ci.DATA_DIR = root / "config", root / "data"
        ci.SETTINGS_FILE, ci.ROI_FILE = ci.CONFIG_DIR / "settings.json", ci.CONFIG_DIR / "rois.json"
        ci.BASELINE_FILE, ci.STATE_FILE = ci.DATA_DIR / "vision_baseline.npz", ci.DATA_DIR / "vision_state.json"
        ci.LOG_FILE = ci.DATA_DIR / "vision_observations.csv"
        ci.CONFIG_DIR.mkdir(parents=True)
        ci.DATA_DIR.mkdir(parents=True)
        ci.SETTINGS_FILE.write_text(json.dumps({
            "compartments": SLOTS, "camera_index": 0, "server_url": "http://backend.test",
            "confirm_seconds": 1.0, "monitor_on_start": False,
        }), encoding="utf-8")
        ci.ROIManager(ci.ROI_FILE).save(ROIS, LOADED.shape, camera_index=0)
        store = ReferenceStore(ci.BASELINE_FILE)
        store.save_expected(crops(LOADED))
        store.save_empty(crops(EMPTY))

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(self.ci, k, v)
        self.tmp.cleanup()

    @patch("vision.event_logger.requests.post", return_value=MagicMock(status_code=201))
    @patch("vision.camera_inspector.time")
    @patch("vision.camera_inspector.open_camera")
    @patch("vision.camera_inspector.cv2")
    def test_trial_mode_end_to_end(self, mock_cv2, mock_open_camera, mock_time, mock_post):
        clock = iter(i * 0.1 for i in range(100_000))
        mock_time.monotonic.side_effect = lambda: next(clock)
        reads = {"n": 0}

        def read():
            reads["n"] += 1
            return True, (LOADED if reads["n"] < 25 else scene(["B", "C", "D"]))

        cap = MagicMock()
        cap.read.side_effect = read
        mock_open_camera.return_value = cap
        mock_cv2.waitKey.side_effect = [ord("t"), ord("1"), ord("n")] + [255] * 80 + [ord("f"), ord("t"), ord("q")]

        self.ci.main()

        trial_dirs = list((self.ci.DATA_DIR / "trials").iterdir())
        self.assertEqual(len(trial_dirs), 1)
        meta = json.loads((trial_dirs[0] / "trial.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["labels"], {"A": "removed", "B": "unchanged", "C": "unchanged", "D": "unchanged"})
        self.assertEqual(meta["passed"], 4, meta["results"])
        self.assertFalse(meta["start_mismatch"])

        # Nothing from the trial reached the real log, the real state, or the backend's dose logic
        self.assertFalse(self.ci.LOG_FILE.exists())
        self.assertFalse(self.ci.STATE_FILE.exists())
        urls = [c.args[0] for c in mock_post.call_args_list]
        self.assertFalse(any(u.endswith("/api/observations") for u in urls))
        trial_beats = [c.kwargs["json"] for c in mock_post.call_args_list
                       if c.args[0].endswith("/api/vision/heartbeat") and c.kwargs["json"]["trial"]]
        self.assertTrue(trial_beats)
        self.assertTrue(all(b["states"] == {} and not b["monitoring"] for b in trial_beats))


if __name__ == "__main__":
    unittest.main()
