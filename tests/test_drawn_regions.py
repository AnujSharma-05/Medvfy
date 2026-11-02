"""Hand-drawn compartments: pen squares on white paper, found with or without medicine inside."""

import unittest
from pathlib import Path

import cv2
import numpy as np

from vision.auto_regions import RegionsNotFound
from vision.content import crosses_line, inside_mask, intrusion_mask
from vision.drawn_regions import find_drawn_cells, find_drawn_layout, find_drawn_regions

SLOTS = ["A", "B", "C", "D"]
FIXTURES = Path(__file__).parent / "fixtures"


def drawn_sheet(slant: int = 4, gap: bool = True, empty_cell: str | None = "D") -> np.ndarray:
    """640x480 white paper with a hand-drawn-looking 2x2 grid (slanted, wobbly, one pen skip)
    and a coloured paper piece in each square except `empty_cell`."""
    rng = np.random.default_rng(5)
    sheet = np.full((480, 640, 3), 205, np.uint8)
    sheet = cv2.add(sheet, rng.integers(0, 6, sheet.shape, dtype=np.uint8))
    pen = (110, 110, 115)
    x0, x1, x2 = 140, 320, 500
    y0, y1, y2 = 80, 240, 400
    for y in (y0, y1, y2):           # horizontal strokes, slightly slanted
        cv2.line(sheet, (x0 - 6, y), (x2 + 6, y + slant), pen, 2)
    for x in (x0, x1, x2):           # vertical strokes
        cv2.line(sheet, (x, y0 - 6), (x + slant, y2 + 6), pen, 2)
    if gap:                          # the pen skipped for a few pixels
        cv2.line(sheet, (220, y0 - 3), (225, y0 + 5), (205, 205, 205), 3)
    colours = {"A": (200, 90, 40), "B": (150, 60, 150), "C": (40, 160, 60), "D": (40, 60, 200)}
    centres = {"A": (230, 160), "B": (410, 160), "C": (230, 320), "D": (410, 320)}
    for slot, (cx, cy) in centres.items():
        if empty_cell not in (slot, "all"):
            cv2.rectangle(sheet, (cx - 25, cy - 20), (cx + 25, cy + 20), colours[slot], -1)
    return sheet


class TestDrawnRegions(unittest.TestCase):
    def test_finds_four_squares_in_reading_order(self):
        cells = find_drawn_cells(drawn_sheet())
        self.assertEqual(len(cells), 4)
        xs = [c.box[0] for c in cells]
        ys = [c.box[1] for c in cells]
        self.assertLess(xs[0], xs[1])
        self.assertLess(xs[2], xs[3])
        self.assertLess(max(ys[:2]), min(ys[2:]))
        self.assertEqual(cells[3].contents, "empty")
        for c in cells[:3]:
            self.assertNotEqual(c.contents, "empty")

    def test_compartment_is_the_whole_drawn_square(self):
        rois, outlines = find_drawn_layout(drawn_sheet(), SLOTS)
        xs, ys = [p[0] for p in outlines["A"]], [p[1] for p in outlines["A"]]
        # Up to the pen lines (drawn at x 140..320, y 80..~242), not a shrunken box inside them.
        self.assertLessEqual(min(xs), 146)
        self.assertGreaterEqual(max(xs), 314)
        self.assertLessEqual(min(ys), 86)
        self.assertGreaterEqual(max(ys), 234)
        area = cv2.contourArea(np.array(outlines["A"], np.float32))
        self.assertGreater(area, 0.9 * 180 * 160)
        # The crop keeps a margin of paper beyond the line, to see what crosses it.
        x, y, w, h = rois["A"]
        self.assertLess(x, min(xs))
        self.assertGreater(x + w, max(xs))

    def test_empty_sheet_squares_are_found_too(self):
        cells = find_drawn_cells(drawn_sheet(empty_cell="all"))
        self.assertEqual([c.contents for c in cells], ["empty"] * 4)

    def test_assign_picks_squares_by_number(self):
        cells = find_drawn_cells(drawn_sheet())
        rois = find_drawn_regions(drawn_sheet(), SLOTS, assign={"A": 4, "B": 3, "C": 2, "D": 1})
        self.assertEqual(rois["A"], cells[3].box)
        self.assertEqual(rois["D"], cells[0].box)

    def test_grid_cut_off_by_the_picture_is_refused(self):
        cut = drawn_sheet()[:300]  # bottom row runs off the picture
        self.assertEqual(len(find_drawn_cells(cut)), 2)
        with self.assertRaisesRegex(RegionsNotFound, "Found 2 complete drawn square"):
            find_drawn_regions(cut, SLOTS)

    def test_plain_medicine_groups_are_not_squares(self):
        sheet = np.full((480, 640, 3), 205, np.uint8)
        for cx, cy in ((200, 150), (440, 150), (200, 330), (440, 330)):
            cv2.rectangle(sheet, (cx - 40, cy - 30), (cx + 40, cy + 30), (40, 60, 200), -1)
        self.assertEqual(find_drawn_cells(sheet), [])

    def test_real_photo_with_bottom_row_cut_off(self):
        frame = cv2.imread(str(FIXTURES / "grid_partial.jpg"))
        self.assertIsNotNone(frame)
        cells = find_drawn_cells(frame)
        self.assertEqual([c.contents for c in cells], ["blue", "purple"])

    def test_real_photo_of_a_crooked_grid(self):
        """Regression: the sheet lay ~7 degrees crooked and pieces nearly touched the lines;
        only 1 of 4 squares was found when lines had to be exactly horizontal/vertical."""
        frame = cv2.imread(str(FIXTURES / "grid_rotated.jpg"))
        self.assertIsNotNone(frame)
        rois = find_drawn_regions(frame, SLOTS)
        self.assertEqual([c.contents for c in find_drawn_cells(frame)][:3], ["blue", "purple", "green"])
        for slot, (x, y, w, h) in rois.items():
            self.assertGreater(w * h, 9000, slot)  # most of each ~125 px square


class TestScatteredPieces(unittest.TestCase):
    """Real taped sheet: pieces lie anywhere in their squares, some against the pen line.
    Regression: an upright box shrunk inside each tilted square refused this photo (A, C, D
    "touch the edge"), though every piece was inside its drawn square."""

    def setUp(self):
        self.empty = cv2.imread(str(FIXTURES / "grid_taped_empty.png"))
        self.loaded = cv2.imread(str(FIXTURES / "grid_taped_loaded.jpg"))
        self.rois, self.outlines = find_drawn_layout(self.empty, SLOTS)

    def blocked(self, frame):
        out = set()
        for s, (x, y, w, h) in self.rois.items():
            mask = intrusion_mask(frame[y:y + h, x:x + w], self.empty[y:y + h, x:x + w])
            if crosses_line(mask, inside_mask(self.rois[s], self.outlines[s])):
                out.add(s)
        return out

    def test_pieces_anywhere_inside_their_square_are_accepted(self):
        self.assertEqual(self.blocked(self.loaded), set())

    def test_arm_reaching_across_the_line_blocks_that_compartment(self):
        arm = self.loaded.copy()
        cv2.ellipse(arm, (120, 260), (75, 22), -10, 0, 360, (120, 160, 215), -1)
        self.assertEqual(self.blocked(arm), {"A"})

    def test_piece_across_the_line_between_two_squares_is_reported(self):
        across = self.loaded.copy()
        cx = int(np.mean([p[0] for p in self.outlines["B"]]))
        top_b = min(p[1] for p in self.outlines["D"])  # the line between B and D
        cv2.rectangle(across, (cx - 15, top_b - 25), (cx + 15, top_b + 25), (60, 60, 200), -1)
        self.assertTrue({"B", "D"} & self.blocked(across))


if __name__ == "__main__":
    unittest.main()
