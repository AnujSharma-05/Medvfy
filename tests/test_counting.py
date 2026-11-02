"""Pill counting by area: robustness on real textures.

Built from the real test setup: the background is the real paper of the test sheet, and the
"pill" is one real green strip cut out of the webcam photo (tests/fixtures/sheet_loaded.jpg).
Each scenario places copies of that strip on the paper, as a person would handle them.
"""

import unittest

import cv2
import numpy as np

from vision.auto_regions import foreground_mask
from vision.content import CountTracker, estimate_count, measure

PHOTO = cv2.imread(__import__("pathlib").Path(__file__).resolve().parent.joinpath(
    "fixtures", "sheet_loaded.jpg").as_posix())


def _strip():
    """One real green strip (63x43 px, ~1400 px of medicine) and its mask."""
    x, y, w, h = 11, 310, 165, 157
    region = PHOTO[y:y + h, x:x + w]
    mask = foreground_mask(region)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    i = 1 + int(np.argmin([abs(a - 1392) for a in stats[1:, 4]]))
    bx, by, bw, bh = stats[i][:4]
    return region[by:by + bh, bx:bx + bw].copy(), (labels[by:by + bh, bx:bx + bw] == i)


STRIP, STRIP_MASK = _strip()
# Real paper: an untouched area of the sheet (between the groups), tiled to compartment size.
PAPER = cv2.resize(PHOTO[150:250, 220:400], (280, 220), interpolation=cv2.INTER_LINEAR)


def place(positions, rotate=None, light=1.0, noise=0.0, seed=0):
    """The compartment with a strip at each (x, y)."""
    img = PAPER.copy()
    strip, mask = STRIP, STRIP_MASK
    if rotate is not None:
        strip, mask = cv2.rotate(STRIP, rotate), cv2.rotate(STRIP_MASK.astype(np.uint8), rotate).astype(bool)
    for x, y in positions:
        h, w = mask.shape
        patch = img[y:y + h, x:x + w]
        patch[mask] = strip[mask]
    if light != 1.0:
        img = np.clip(img.astype(np.float32) * light, 0, 255).astype(np.uint8)
    if noise:
        rng = np.random.default_rng(seed)
        img = np.clip(img.astype(np.float32) + rng.normal(0, noise, img.shape), 0, 255).astype(np.uint8)
    return img


SPREAD5 = [(10, 10), (90, 10), (170, 10), (10, 110), (90, 110)]


class TestCountingOnRealTexture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.empty = PAPER.copy()
        cls.unit = measure(place(SPREAD5), cls.empty).area / 5  # calibration: "5 pills"

    def count(self, img):
        return estimate_count(measure(img, self.empty).area, self.unit)

    def assertCount(self, img, expected, tolerance=0.35):
        est = self.count(img)
        self.assertLess(abs(est - expected), tolerance, f"estimate {est:.2f}, expected {expected}")

    def test_unit_area_matches_one_real_strip(self):
        self.assertAlmostEqual(self.unit, STRIP_MASK.sum(), delta=0.15 * STRIP_MASK.sum())

    def test_untouched(self):
        self.assertCount(place(SPREAD5), 5)

    def test_one_and_two_taken(self):
        self.assertCount(place(SPREAD5[:4]), 4)
        self.assertCount(place(SPREAD5[:3]), 3)
        self.assertCount(place([]), 0)

    def test_rearranged_or_put_back_elsewhere_is_still_five(self):
        self.assertCount(place([(150, 120), (60, 60), (200, 20), (20, 150), (120, 5)]), 5)

    def test_pills_touching_each_other(self):
        touching = [(20, 20), (82, 20), (144, 20), (20, 62), (82, 62)]  # edge to edge, one blob
        self.assertCount(place(touching), 5)

    def test_dense_compartment_of_ten(self):
        dense = [(x, y) for y in (5, 55, 105, 155) for x in (5, 75, 145, 210) if x + 63 <= 280][:10]
        self.assertCount(place(dense), 10, tolerance=0.6)
        self.assertCount(place(dense[:9]), 9, tolerance=0.6)  # one dose out of ten still visible

    def test_rotated_pills(self):
        self.assertCount(place([(10, 10), (90, 10), (170, 10)], rotate=cv2.ROTATE_90_CLOCKWISE), 3)

    def test_room_light_changes(self):
        self.assertCount(place(SPREAD5, light=0.8), 5)
        self.assertCount(place(SPREAD5, light=1.15), 5)

    def test_camera_noise(self):
        for seed in range(3):
            with self.subTest(seed=seed):
                self.assertCount(place(SPREAD5, noise=4.0, seed=seed), 5)

    def test_stacked_pills_are_undercounted(self):
        # Known limit: a pill lying ON another hides it. Single-layer placement is required.
        stacked = SPREAD5[:3] + [(20, 20), (100, 15)]  # two strips mostly on top of others
        self.assertLess(self.count(place(stacked)), 4.0)


class TestCountTracker(unittest.TestCase):
    def test_needs_time_and_agreement(self):
        t = CountTracker(confirm_observations=2, confirm_seconds=2.0)
        t.reset(5)
        self.assertIsNone(t.update(4.1, 0.0))
        self.assertIsNone(t.update(3.9, 0.5))   # two observations, but only 0.5 s
        self.assertIsNone(t.update(4.0, 1.5))
        self.assertEqual(t.update(4.05, 2.0), (5, 4))
        self.assertEqual(t.confirmed, 4)

    def test_put_back_needs_a_clear_reading(self):
        """Regression (live): after one of 4 was taken, the 3 left read 3.6-3.7 once a hand had
        shuffled them, and the count went back to 4, hiding the dose."""
        t = CountTracker(2, 2.0)
        t.reset(4)
        t.update(3.33, 0.0)
        self.assertEqual(t.update(3.34, 2.0), (4, 3))   # taking one: normal tolerance
        for i, est in enumerate([3.61, 3.69, 3.72, 3.66, 3.7]):
            self.assertIsNone(t.update(est, 3.0 + i))
        self.assertEqual(t.confirmed, 3)
        t.update(3.95, 10.0)
        self.assertEqual(t.update(4.05, 12.0), (3, 4))  # really put back: accepted

    def test_brief_dip_does_not_count(self):
        t = CountTracker(2, 2.0)
        t.reset(5)
        t.update(4.0, 0.0)
        t.update(5.0, 1.0)      # back to 5: the candidate is dropped
        self.assertIsNone(t.update(4.0, 2.5))
        self.assertEqual(t.confirmed, 5)

    def test_ambiguous_or_unusable_estimates_restart_confirmation(self):
        t = CountTracker(2, 1.0)
        t.reset(5)
        t.update(4.0, 0.0)
        t.update(4.5, 0.6)      # half-way between 4 and 5: not evidence
        self.assertIsNone(t.update(4.0, 1.2))
        t.update(None, 1.5)     # blurry frame
        t.update(4.0, 2.0)
        self.assertIsNone(t.update(4.0, 2.5))
        self.assertEqual(t.update(4.0, 3.0), (5, 4))

    def test_crumpled_removal_with_demo_tolerance(self):
        """config/tuning.json: a crumpled piece taken out of 4 can leave 3.4 units visible."""
        t = CountTracker(2, 1.0, max_rounding_error=0.45)
        t.reset(4)
        t.update(3.44, 0.0)
        self.assertEqual(t.update(3.42, 1.0), (4, 3))
        strict = CountTracker(2, 1.0)
        strict.reset(4)
        strict.update(3.44, 0.0)
        self.assertIsNone(strict.update(3.42, 1.0))  # default 0.35: never decides

    def test_clearly_fewer_but_between_counts_decides_after_a_while(self):
        """Live: 3 of 4 taken from B, the big crumpled piece left read 1.38-1.50 for good."""
        t = CountTracker(2, 1.0, 0.45, 0.2, between_seconds=3.0)
        t.reset(4)
        readings = [1.41, 1.47, 1.38, 1.50, 1.44, 1.48, 1.43]
        results = [t.update(est, i * 0.5) for i, est in enumerate(readings)]
        self.assertEqual(results[:6], [None] * 6)           # not before 3 s
        self.assertEqual(results[6], (4, 1))
        up = CountTracker(2, 1.0, 0.45, 0.2, between_seconds=3.0)
        up.reset(1)
        self.assertTrue(all(up.update(1.6, i * 0.5) is None for i in range(20)))  # never upwards


if __name__ == "__main__":
    unittest.main()
