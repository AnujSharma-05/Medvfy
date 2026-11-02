"""Headless camera service and automatic regions, driven with the real photo of the test sheet.

tests/fixtures/sheet_loaded.jpg is a 640x480 webcam image of the actual test setup: white
paper with four groups of medicine strips (blue, dark, green, purple). "Removing" a group is
simulated by painting its region with the surrounding paper colour.
"""

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from vision.auto_regions import RegionsNotFound, find_groups, find_regions
from vision.camera_service import CameraService, ServicePaths

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sheet_loaded.jpg"
LOADED = cv2.imread(str(FIXTURE))
SLOTS = ["A", "B", "C", "D"]


def without(frame, rois, slots):
    """The sheet with the medicine of `slots` taken away (region painted with paper colour)."""
    out = frame.copy()
    for s in slots:
        x, y, w, h = rois[s]
        patch = out[y:y + h, x:x + w]
        paper = np.median(patch.reshape(-1, 3), axis=0)
        mask = cv2.dilate((np.abs(patch.astype(int) - paper).sum(axis=2) > 60).astype(np.uint8),
                          np.ones((7, 7), np.uint8)).astype(bool)
        patch[mask] = paper
    return out


class FakeLogger:
    def __init__(self):
        self.commands, self.heartbeats, self.logged = [], [], []
        self.sent = 0

    def send(self, kind, params=None):
        self.sent += 1  # Unique ids: the service ignores an id it has already handled
        cmd = {"id": f"cmd-{self.sent}", "type": kind, "params": params or {}}
        self.commands.append(cmd)
        return cmd["id"]

    def take_commands(self):
        out, self.commands = self.commands, []
        return out

    def send_heartbeat(self, payload):
        self.heartbeats.append(payload)

    def log_transitions(self, transitions):
        self.logged.extend(t for t in transitions if t.is_new_event)

    def log_count_change(self, slot, old, new, estimate, timestamp):
        self.count_changes = getattr(self, "count_changes", []) + [(slot, old, new)]

    def result(self, cmd_id):
        for hb in reversed(self.heartbeats):
            for r in hb["command_results"]:
                if r["id"] == cmd_id:
                    return r
        return None


class TestAutoRegions(unittest.TestCase):
    def test_real_sheet_gives_four_regions_in_reading_order(self):
        rois = find_regions(LOADED, SLOTS)
        centres = {s: (x + w / 2, y + h / 2) for s, (x, y, w, h) in rois.items()}
        self.assertLess(centres["A"][0], centres["B"][0])   # A left of B (top row)
        self.assertLess(centres["C"][0], centres["D"][0])   # C left of D (bottom row)
        self.assertLess(centres["A"][1], centres["C"][1])   # top row above bottom row
        self.assertLess(centres["B"][1], centres["D"][1])
        # Each region contains its group's colour: blue A, green C, purple D
        hue = lambda s: np.median(cv2.cvtColor(LOADED[rois[s][1]:rois[s][1] + rois[s][3],
                                                      rois[s][0]:rois[s][0] + rois[s][2]], cv2.COLOR_BGR2HSV)
                                  [..., 0][cv2.cvtColor(LOADED[rois[s][1]:rois[s][1] + rois[s][3],
                                                               rois[s][0]:rois[s][0] + rois[s][2]],
                                                        cv2.COLOR_BGR2HSV)[..., 1] > 60])
        self.assertTrue(95 <= hue("A") <= 125, hue("A"))   # blue
        self.assertTrue(60 <= hue("C") <= 95, hue("C"))    # green/teal
        self.assertTrue(120 <= hue("D") <= 150, hue("D"))  # purple

    @staticmethod
    def _sheet(gap):
        """Four coloured groups on white paper; the two bottom groups `gap` px apart."""
        img = np.full((480, 640, 3), 225, np.uint8)
        for (x, y), color in (((80, 80), (200, 60, 60)), ((420, 80), (60, 60, 200)),
                              ((80, 300), (60, 160, 60)), ((80 + 120 + gap, 300), (160, 60, 160))):
            cv2.rectangle(img, (x, y), (x + 120, y + 100), color, -1)
        return img

    def test_overlapping_margins_are_split_between_close_groups(self):
        rois = find_regions(self._sheet(40), SLOTS)  # 40 px apart: 30 px margins would collide
        c, d = rois["C"], rois["D"]
        self.assertLessEqual(c[0] + c[2], d[0])  # no overlap
        self.assertGreaterEqual(c[0] + c[2], 200 + 10)  # C still keeps paper beyond its objects
        self.assertLessEqual(d[0], 240 - 10)

    def test_groups_too_close_are_still_refused(self):
        # 10 px apart: the two groups merge into one and the layout is refused, not guessed.
        with self.assertRaises(RegionsNotFound) as ctx:
            find_regions(self._sheet(10), SLOTS)
        self.assertIn("Found 3 group(s)", str(ctx.exception))

    def test_missing_group_is_reported_not_guessed(self):
        rois = find_regions(LOADED, SLOTS)
        with self.assertRaises(RegionsNotFound) as ctx:
            find_regions(without(LOADED, rois, ["B"]), SLOTS)
        self.assertIn("Found 3 group(s)", str(ctx.exception))
        self.assertEqual(len(find_groups(LOADED)), 4)
        with self.assertRaises(RegionsNotFound):
            find_regions(np.full_like(LOADED, 235), SLOTS)


class TestAngledRealView(unittest.TestCase):
    """tests/fixtures/sheet_angled.jpg: the same sheet seen by the fixed camera, rotated ~90 degrees,
    with a dark stand (bottom-left) and the desk (bottom-right corner) running off the picture."""

    ANGLED = cv2.imread(str(FIXTURE.parent / "sheet_angled.jpg"))

    def test_background_at_the_edge_is_ignored_and_colours_named(self):
        from vision.auto_regions import detect_groups
        groups, ignored = detect_groups(self.ANGLED)
        self.assertEqual(ignored, 2)
        self.assertEqual([g.color for g in groups], ["blue", "green", "dark", "purple"])

    def test_letters_follow_the_physical_layout_not_the_picture(self):
        # Physical layout (first photo): A blue, B dark, C green, D purple
        rois = find_regions(self.ANGLED, SLOTS, assign={"A": 1, "B": 3, "C": 2, "D": 4})
        self.assertEqual(rois["A"], [444, 21, 133, 144])   # blue
        self.assertEqual(rois["C"], [187, 117, 170, 145])  # green
        for bad in ({"A": 1, "B": 1, "C": 2, "D": 4}, {"A": 1, "B": 3, "C": 2}, {"A": 1, "B": 3, "C": 2, "D": 9}):
            with self.subTest(bad=bad), self.assertRaises(RegionsNotFound):
                find_regions(self.ANGLED, SLOTS, assign=bad)

    def test_removal_detected_in_the_angled_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            logger = FakeLogger()
            svc = CameraService({"compartments": SLOTS, "confirm_seconds": 1.0},
                                ServicePaths(Path(tmp) / "c", Path(tmp) / "d"), logger)
            svc.attach(self.ANGLED)
            t = [0.0]

            def feed(frame, seconds):
                for _ in range(int(seconds * 10)):
                    t[0] = round(t[0] + 0.1, 3)
                    svc.step(frame, t[0])

            def send(kind, params=None, frame=self.ANGLED, seconds=2.0):
                cid = logger.send(kind, params)
                feed(frame, seconds)
                return logger.result(cid)

            self.assertTrue(send("set_regions", {"auto": True, "assign": {"A": 1, "B": 3, "C": 2, "D": 4}})["ok"])
            empty = without(self.ANGLED, svc.rois, SLOTS)
            self.assertTrue(send("save_empty", frame=empty)["ok"])
            self.assertTrue(send("recalibrate")["ok"])
            feed(self.ANGLED, 3.0)
            self.assertEqual(logger.logged, [])
            feed(without(self.ANGLED, svc.rois, ["B"]), 5.0)  # take the dark strips away
            events = [(e.slot, e.new_state.value) for e in logger.logged]
            self.assertEqual(events, [("B", "REMOVAL_CANDIDATE"), ("B", "REMOVAL_DETECTED")])

            # Regression: an emptied sheet has far fewer edges than a loaded one. Judged against the
            # loaded sharpness alone it read as "blurry", so emptying everything was never confirmed.
            feed(empty, 6.0)
            states = logger.heartbeats[-1]["states"]
            self.assertEqual(set(states.values()), {"REMOVAL_DETECTED"}, states)


class TestCountedPipeline(unittest.TestCase):
    """Whole service with pill counts: real paper, real strips, 5 per compartment."""

    def setUp(self):
        from test_counting import STRIP, STRIP_MASK
        self.strip, self.mask = STRIP, STRIP_MASK
        paper = LOADED[150:250, 220:400]
        self.paper = cv2.resize(paper, (640, 480), interpolation=cv2.INTER_LINEAR)
        self.rois = {"A": [20, 20, 280, 200], "B": [340, 20, 280, 200],
                     "C": [20, 260, 280, 200], "D": [340, 260, 280, 200]}
        self.spots = [(10, 10), (90, 10), (170, 10), (10, 110), (90, 110)]
        self.tmp = tempfile.TemporaryDirectory()
        self.logger = FakeLogger()
        self.svc = CameraService({"compartments": SLOTS, "confirm_seconds": 1.0},
                                 ServicePaths(Path(self.tmp.name) / "c", Path(self.tmp.name) / "d"), self.logger)
        self.svc.attach(self.paper)
        self.t = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def frame(self, per_slot):
        img = self.paper.copy()
        h, w = self.mask.shape
        for slot, n in per_slot.items():
            rx, ry = self.rois[slot][:2]
            for x, y in self.spots[:n]:
                patch = img[ry + y:ry + y + h, rx + x:rx + x + w]
                patch[self.mask] = self.strip[self.mask]
        return img

    def feed(self, frame, seconds):
        for _ in range(int(seconds * 10)):
            self.t = round(self.t + 0.1, 3)
            self.svc.step(frame, self.t)

    def send(self, kind, params=None, frame=None, seconds=2.0):
        cid = self.logger.send(kind, params)
        self.feed(frame if frame is not None else self.frame({s: 5 for s in SLOTS}), seconds)
        return self.logger.result(cid)

    def test_one_pill_out_of_five_is_counted(self):
        full = self.frame({s: 5 for s in SLOTS})
        self.assertTrue(self.send("set_regions", {"rois": self.rois}, full)["ok"])
        self.assertTrue(self.send("save_empty", frame=self.paper)["ok"])
        r = self.send("recalibrate", {"counts": {s: 5 for s in SLOTS}}, full)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["loaded_counts"], {s: 5 for s in SLOTS})
        self.feed(full, 2.0)
        self.assertEqual(self.logger.heartbeats[-1]["counts"], {s: 5 for s in SLOTS})

        self.feed(self.frame({"A": 4, "B": 5, "C": 5, "D": 5}), 5.0)  # one pill out of A
        self.assertEqual(getattr(self.logger, "count_changes", []), [("A", 5, 4)])
        self.assertEqual(self.logger.heartbeats[-1]["counts"], {"A": 4, "B": 5, "C": 5, "D": 5})

        # Rearranging B's pills (same count) changes nothing
        self.spots = list(reversed(self.spots))
        self.feed(self.frame({"A": 4, "B": 5, "C": 5, "D": 5}), 5.0)
        self.assertEqual(getattr(self.logger, "count_changes", []), [("A", 5, 4)])

    def calibrate_counts(self):
        full = self.frame({s: 5 for s in SLOTS})
        self.assertTrue(self.send("set_regions", {"rois": self.rois}, full)["ok"])
        self.assertTrue(self.send("save_empty", frame=self.paper)["ok"])
        self.assertTrue(self.send("recalibrate", {"counts": {s: 5 for s in SLOTS}}, full)["ok"])
        return full

    def test_hand_held_still_over_a_compartment_is_not_counted(self):
        full = self.calibrate_counts()
        hand = full.copy()
        rx, ry = self.rois["A"][:2]
        # A skin-coloured hand reaching in over A's left edge, covering two strips, held still
        cv2.rectangle(hand, (rx - 15, ry + 5), (rx + 120, ry + 90), (120, 160, 210), -1)
        self.feed(hand, 5.0)
        self.assertEqual(getattr(self.logger, "count_changes", []), [])
        self.assertIn("A", self.logger.heartbeats[-1]["details"]["view_blocked"])
        self.assertEqual(self.logger.heartbeats[-1]["counts"]["A"], 5)
        self.feed(full, 4.0)  # hand gone
        self.assertEqual(getattr(self.logger, "count_changes", []), [])
        self.assertEqual(self.logger.heartbeats[-1]["details"]["view_blocked"], [])

    def test_calibration_refused_when_a_piece_crosses_its_box(self):
        full = self.frame({s: 5 for s in SLOTS})
        self.assertTrue(self.send("set_regions", {"rois": self.rois}, full)["ok"])
        self.assertTrue(self.send("save_empty", frame=self.paper)["ok"])
        sticking_out = full.copy()
        h, w = self.mask.shape
        bx, by = self.rois["B"][0] - 30, self.rois["B"][1] + 120  # half outside B's left edge
        patch = sticking_out[by:by + h, bx:bx + w]
        patch[self.mask] = self.strip[self.mask]
        r = self.send("recalibrate", {"counts": {s: 5 for s in SLOTS}}, sticking_out)
        self.assertFalse(r["ok"])
        self.assertIn("B", r["message"])
        self.assertIn("edge", r["message"])

    def test_regions_redrawn_without_clearing_the_sheet_again(self):
        full = self.frame({s: 5 for s in SLOTS})
        self.assertTrue(self.send("set_regions", {"rois": self.rois}, full)["ok"])
        self.assertTrue(self.send("save_empty", frame=self.paper)["ok"])
        # Pieces end up elsewhere: new boxes, same camera. The empty photo is re-cut, not lost.
        moved_rois = {s: [x + 10, y + 5, w - 20, h - 10] for s, (x, y, w, h) in self.rois.items()}
        r = self.send("set_regions", {"rois": moved_rois}, full)
        self.assertTrue(r["data"]["empty_reused"], r)
        self.assertTrue(self.logger.heartbeats[-1]["details"]["has_empty_photo"])
        self.rois = moved_rois
        self.spots = [(10, 10), (90, 10), (170, 10), (10, 105), (90, 105)]  # inside the new boxes
        r = self.send("recalibrate", {"counts": {s: 5 for s in SLOTS}}, self.frame({s: 5 for s in SLOTS}))
        self.assertTrue(r["ok"], r)

    def test_drawn_sheet_that_slid_keeps_its_empty_photo(self):
        """Regression: the paper slid ~10 px while pieces were put in; calibration then saw the pen
        lines on the box edges and refused. The empty photo now follows the drawn grid."""
        from test_drawn_regions import drawn_sheet
        empty = drawn_sheet(empty_cell="all")
        self.assertTrue(self.send("set_regions", {"auto": True}, empty)["ok"])
        self.assertTrue(self.send("save_empty", frame=empty)["ok"])
        slid = np.roll(drawn_sheet(empty_cell=None), (10, -5), axis=(0, 1))
        r = self.send("set_regions", {"auto": True}, slid)
        self.assertTrue(r["data"]["empty_reused"], r)
        r = self.send("recalibrate", {"counts": {s: 1 for s in SLOTS}}, slid)
        self.assertTrue(r["ok"], r)

    def test_real_scattered_pieces_calibrate_and_count(self):
        """The real taped sheet: 4 pieces per square, lying anywhere, some against the line."""
        empty = cv2.imread(str(Path(__file__).parent / "fixtures" / "grid_taped_empty.png"))
        loaded = cv2.imread(str(Path(__file__).parent / "fixtures" / "grid_taped_loaded.jpg"))
        self.assertTrue(self.send("set_regions", {"auto": True}, empty)["ok"])
        self.assertTrue(self.send("save_empty", frame=empty)["ok"])
        r = self.send("recalibrate", {"counts": {s: 4 for s in SLOTS}}, loaded)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["data"]["loaded_counts"], {s: 4 for s in SLOTS})
        self.feed(loaded, 4.0)
        self.assertEqual(self.svc.monitor.counts(), {s: 4 for s in SLOTS})
        self.assertEqual(self.logger.heartbeats[-1]["details"]["view_blocked"], [])

    def test_empty_photo_not_reused_after_the_camera_moved(self):
        full = self.frame({s: 5 for s in SLOTS})
        self.send("set_regions", {"rois": self.rois}, full)
        self.send("save_empty", frame=self.paper)
        other_view = cv2.resize(LOADED, (640, 480))  # the camera now sees a different scene
        r = self.send("set_regions", {"rois": self.rois}, other_view)
        self.assertFalse(r["data"]["empty_reused"])
        self.assertFalse(self.logger.heartbeats[-1]["details"]["has_empty_photo"])

    def test_counted_box_with_nothing_in_it_is_refused_not_a_crash(self):
        """Regression (live): a box given a count but showing no medicine crashed the service."""
        self.send("set_regions", {"rois": self.rois})
        self.send("save_empty", frame=self.paper)
        r = self.send("recalibrate", {"counts": {s: 5 for s in SLOTS}}, self.frame({"A": 5, "B": 5, "C": 5}))
        self.assertFalse(r["ok"])
        self.assertIn("No medicine seen in D", r["message"])

    def test_bad_counts_rejected(self):
        self.send("set_regions", {"rois": self.rois})
        self.assertFalse(self.send("recalibrate", {"counts": {"A": 0}})["ok"])
        self.assertFalse(self.send("recalibrate", {"counts": {"Z": 3}})["ok"])


class TestCameraService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.paths = ServicePaths(root / "config", root / "data")
        self.logger = FakeLogger()
        self.svc = CameraService({"compartments": SLOTS, "confirm_seconds": 1.0, "monitor_on_start": True},
                                 self.paths, self.logger)
        self.svc.attach(LOADED)
        self.t = 0.0

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, frame, seconds):
        for _ in range(int(seconds * 10)):
            self.t = round(self.t + 0.1, 3)
            self.svc.step(frame, self.t)

    def command(self, kind, params=None, frame=LOADED, seconds=0.2):
        cmd_id = self.logger.send(kind, params)
        self.feed(frame, seconds)
        return self.logger.result(cmd_id)

    def calibrate(self):
        self.assertTrue(self.command("set_regions", {"auto": True})["ok"])
        self.rois = self.svc.rois
        self.empty = without(LOADED, self.rois, SLOTS)
        self.assertTrue(self.command("save_empty", frame=self.empty, seconds=2.0)["ok"])
        self.assertTrue(self.command("recalibrate", frame=LOADED, seconds=2.0)["ok"])

    def test_fresh_service_has_no_regions_and_does_not_monitor(self):
        self.assertIsNone(self.svc.rois)
        self.assertFalse(self.svc.monitoring)
        self.feed(LOADED, 1.0)
        details = self.logger.heartbeats[-1]["details"]
        self.assertEqual(details["frame_size"], [640, 480])
        self.assertFalse(details["has_loaded_photo"])
        r = self.command("recalibrate")
        self.assertFalse(r["ok"])
        self.assertIn("regions", r["message"])

    def test_preview_changes_nothing(self):
        r = self.command("set_regions", {"auto": True, "preview": True})
        self.assertTrue(r["ok"])
        self.assertTrue(r["data"]["ok"])
        self.assertEqual([g["color"] for g in r["data"]["groups"]], ["blue", "dark", "green", "purple"])
        self.assertIn("1: blue", r["message"])
        self.assertTrue(Path(r["data"]["image"]).exists())
        self.assertIsNone(self.svc.rois)
        self.assertFalse(self.paths.roi_file.exists())

    def test_calibration_then_real_removal_is_detected(self):
        self.calibrate()
        self.assertTrue(self.svc.monitoring)
        self.assertTrue(self.logger.heartbeats[-1]["details"]["has_empty_photo"])
        sharp = self.logger.heartbeats[-1]["details"]
        self.assertGreater(sharp["sharpness"], sharp["sharpness_limit"])

        self.feed(LOADED, 3.0)  # still, loaded: nothing happens
        self.assertEqual(self.logger.logged, [])

        self.feed(without(LOADED, self.rois, ["A"]), 5.0)  # take the blue strips away
        events = [(t.slot, t.new_state.value) for t in self.logger.logged]
        self.assertIn(("A", "REMOVAL_DETECTED"), events)
        self.assertEqual({s for s, _ in events}, {"A"})  # B, C, D untouched
        self.assertEqual(self.logger.heartbeats[-1]["states"]["A"], "REMOVAL_DETECTED")

    def test_calibration_waits_for_a_still_scene(self):
        self.command("set_regions", {"auto": True})
        cmd_id = self.logger.send("recalibrate")
        rng = np.random.default_rng(0)
        for _ in range(20):  # 2 s of a hand moving around
            self.t += 0.1
            moving = LOADED.copy()
            x = int(rng.integers(0, 500))
            cv2.rectangle(moving, (x, 100), (x + 120, 300), (60, 90, 140), -1)
            self.svc.step(moving, self.t)
        self.assertIsNone(self.logger.result(cmd_id))
        self.assertEqual(self.logger.heartbeats[-1]["details"]["waiting_for"], "recalibrate")
        self.feed(LOADED, 2.0)
        self.assertTrue(self.logger.result(cmd_id)["ok"])

    def test_trial_through_commands(self):
        self.calibrate()
        r = self.command("trial_start", {"labels": {"C": "removed"}})
        self.assertTrue(r["ok"], r)
        self.feed(LOADED, 2.0)
        trial = self.logger.heartbeats[-1]["details"]["trial"]
        self.assertTrue(trial["ready"])
        self.assertEqual(trial["labels"]["C"], "removed")
        self.assertEqual(self.logger.heartbeats[-1]["states"], {})  # real states not reported during trials

        self.feed(without(LOADED, self.rois, ["C"]), 5.0)
        r = self.command("trial_finish", frame=without(LOADED, self.rois, ["C"]))
        self.assertTrue(r["ok"])
        self.assertEqual((r["data"]["passed"], r["data"]["total"]), (4, 4), r["message"])
        self.assertEqual(self.logger.logged, [])  # trial never touched the real log
        meta = json.loads((Path(r["data"]["path"]) / "trial.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["labels"]["C"], "removed")
        self.assertFalse(self.svc.trial_mode)

    def test_bad_trial_labels_and_unknown_commands(self):
        self.calibrate()
        self.assertFalse(self.command("trial_start", {"labels": {"Z": "removed"}})["ok"])
        self.assertFalse(self.command("trial_start", {"labels": {"A": "eaten"}})["ok"])
        self.assertFalse(self.command("trial_finish")["ok"])
        self.assertFalse(self.command("launch_rocket")["ok"])

    def test_snapshot_and_manual_regions(self):
        r = self.command("snapshot")
        self.assertTrue(Path(r["data"]["raw"]).exists())
        self.assertTrue(Path(r["data"]["annotated"]).exists())
        bad = self.command("set_regions", {"rois": {"A": [0, 0, 5, 5], "B": [0, 0, 50, 50],
                                                    "C": [0, 0, 50, 50], "D": [0, 0, 50, 50]}})
        self.assertFalse(bad["ok"])
        good = self.command("set_regions", {"rois": {"A": [0, 0, 100, 100], "B": [200, 0, 100, 100],
                                                     "C": [0, 200, 100, 100], "D": [200, 200, 100, 100]}})
        self.assertTrue(good["ok"])
        self.assertEqual(self.svc.rois["D"], [200, 200, 100, 100])


if __name__ == "__main__":
    unittest.main()
