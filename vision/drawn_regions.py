"""Compartments drawn by hand: find the pen-drawn squares on the sheet.

The squares are drawn with a pen on white paper (a 2x2 grid with shared lines in the test
setup). Finding them, rather than the medicine groups, means a compartment exists even when
it is empty, a refill cannot move it, and a piece outside its square is clearly outside.

Method:
1. Pen lines are thin and darker than the paper around them. A "black-hat" filter keeps dark
   features thinner than ~9 px, which suppresses medicine (blobs, not thin lines).
2. Only long, roughly straight runs are kept (horizontal or vertical, tolerant of a hand-drawn
   slant), then small gaps in the drawing are bridged.
3. Each square is a region of paper fully enclosed by lines. A square cut off by the picture
   edge is not enclosed and is ignored instead of half-used.
4. The compartment is the square's INSIDE, kept clear of the pen line.

Measured on the real test sheet: the pen line is 1-2 px wide, grey ~115 against paper ~208.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from vision.auto_regions import RegionsNotFound, color_name, foreground_mask, reading_order


MARGIN = 12  # px of paper kept around the square in its crop, to see what crosses the line


@dataclass
class Cell:
    outline: list[list[int]]  # inside of the square up to the pen line: polygon [[x, y], ...]
    box: list[int]            # upright crop [x, y, w, h]: the outline plus MARGIN all round
    contents: str             # colour name of what lies inside ("empty" if nothing)
    fill: float               # fraction of the square covered by objects


LINE_ANGLES = (-15, -10, -5, 0, 5, 10, 15)  # degrees of tilt accepted for a drawn line


def _line_kernel(length: int, degrees: float) -> np.ndarray:
    """Structuring element: a 1 px straight line of `length` at `degrees`."""
    rad = np.deg2rad(degrees)
    dx, dy = np.cos(rad) * (length - 1) / 2, np.sin(rad) * (length - 1) / 2
    size = int(np.ceil(max(abs(dx), abs(dy)))) * 2 + 1
    kernel = np.zeros((size, size), np.uint8)
    c = size // 2
    cv2.line(kernel, (int(round(c - dx)), int(round(c - dy))), (int(round(c + dx)), int(round(c + dy))), 1, 1)
    return kernel


def line_mask(frame: np.ndarray) -> np.ndarray:
    """uint8 mask of long, thin, dark pen lines."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    blackhat = cv2.morphologyEx(gray, cv2.MORPH_BLACKHAT, cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9)))
    thin_dark = (blackhat > 30).astype(np.uint8) * 255
    # Thicken a little so a slightly slanted hand-drawn line still forms long runs.
    thick = cv2.dilate(thin_dark, np.ones((3, 3), np.uint8))
    run = max(25, min(h, w) // 12)
    # A sheet lying a little crooked under the camera turns the whole grid (measured: 7 degrees on
    # the test setup), so look for runs at several angles around horizontal and vertical.
    lines = np.zeros_like(thick)
    for angle in LINE_ANGLES:
        for base in (0, 90):
            kernel = _line_kernel(run, base + angle)
            lines |= cv2.morphologyEx(thick, cv2.MORPH_OPEN, kernel)
    # Bridge small gaps where the pen skipped or two strokes do not quite meet.
    return cv2.morphologyEx(lines, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))


def find_drawn_cells(frame: np.ndarray, min_fraction: float = 0.01, max_fraction: float = 0.35) -> list[Cell]:
    """Enclosed squares on the sheet, in reading order."""
    if frame is None or frame.size == 0:
        raise RegionsNotFound("No image")
    h, w = frame.shape[:2]
    lines = line_mask(frame)
    paper = cv2.bitwise_not(lines)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(paper, connectivity=4)
    objects = foreground_mask(frame)
    cells: list[Cell] = []
    for i in range(1, count):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if x <= 1 or y <= 1 or x + bw >= w - 1 or y + bh >= h - 1:
            continue  # touches the picture edge: not a closed square (or cut off)
        if not (min_fraction * h * w <= bw * bh <= max_fraction * h * w):
            continue
        # Box-like: judged on the convex hull of the square's outline against its tightest
        # (possibly tilted) rectangle, so medicine inside or touching the line, and a crooked
        # sheet, do not count against it.
        component = (labels[y:y + bh, x:x + bw] == i).astype(np.uint8)
        outlines, _ = cv2.findContours(component, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not outlines:
            continue
        hull = cv2.convexHull(max(outlines, key=cv2.contourArea))
        (_, _), (rw, rh), _ = cv2.minAreaRect(hull)
        if cv2.contourArea(hull) < 0.85 * rw * rh or not 0.4 <= rw / max(1.0, rh) <= 2.5:
            continue  # not a box-like shape
        # The compartment is the whole inside of the drawn square, whatever its tilt or wobble:
        # anything lying within the pen lines belongs to it.
        poly = cv2.approxPolyDP(hull, 0.01 * cv2.arcLength(hull, True), True).reshape(-1, 2) + [x, y]
        outline = [[int(px), int(py)] for px, py in poly]
        x0, y0 = max(0, x - MARGIN), max(0, y - MARGIN)
        x1, y1 = min(w, x + bw + MARGIN), min(h, y + bh + MARGIN)
        box = [x0, y0, x1 - x0, y1 - y0]
        inside_square = np.zeros((h, w), np.uint8)
        cv2.fillPoly(inside_square, [poly.astype(np.int32)], 1)
        inside = objects[inside_square > 0]
        fill = float(np.count_nonzero(inside)) / max(1, inside.size)
        contents = "empty"
        if fill > 0.01:
            in_crop = (inside_square[y0:y1, x0:x1] > 0) & (objects[y0:y1, x0:x1] > 0)
            contents = color_name(frame[y0:y1, x0:x1], in_crop.astype(np.uint8) * 255)
        cells.append(Cell(outline, box, contents, round(fill, 3)))
    by_box = {tuple(c.box): c for c in cells}
    return [by_box[tuple(b)] for b in reading_order([c.box for c in cells], h)]


def find_drawn_layout(frame: np.ndarray, slots: list[str], assign: dict[str, int] | None = None
                      ) -> tuple[dict[str, list[int]], dict[str, list[list[int]]]]:
    """Per compartment letter: its crop box and the drawn square's outline (letters in reading
    order, or by `assign` = {"A": 2, ...} using the preview's numbers). Raises RegionsNotFound."""
    cells = find_drawn_cells(frame)
    if assign:
        letters = {str(k).upper(): int(v) for k, v in assign.items()}
        if sorted(letters) != sorted(slots) or len(set(letters.values())) != len(slots):
            raise RegionsNotFound(f"Give each of {', '.join(slots)} a different square number.")
        bad = [n for n in letters.values() if not 1 <= n <= len(cells)]
        if bad:
            raise RegionsNotFound(f"There is no square {bad[0]}: {len(cells)} drawn square(s) found.")
        chosen = {s: cells[letters[s] - 1] for s in slots}
    elif len(cells) < len(slots):
        raise RegionsNotFound(f"Found {len(cells)} complete drawn square(s), need {len(slots)}. Every square "
                              "must be fully in the picture, with its lines closed.")
    elif len(cells) > len(slots):
        raise RegionsNotFound(f"Found {len(cells)} drawn squares, need {len(slots)}: say which is which "
                              "(e.g. A=1 B=2 C=3 D=4 from the preview).")
    else:
        chosen = dict(zip(slots, cells))
    return {s: c.box for s, c in chosen.items()}, {s: c.outline for s, c in chosen.items()}


def find_drawn_regions(frame: np.ndarray, slots: list[str], assign: dict[str, int] | None = None) -> dict[str, list[int]]:
    """Crop box per compartment letter (see find_drawn_layout)."""
    return find_drawn_layout(frame, slots, assign)[0]
