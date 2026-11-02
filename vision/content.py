"""How much medicine is in a compartment, independent of where it lies.

Pixel-by-pixel comparison with the "loaded" photo fails exactly when people handle medicine
normally: pills put back in a slightly different arrangement look "changed", and taking one
pill out of a week's supply barely changes the picture. On a uniform background, a pill
covers the same AREA wherever it lies (touching others or not, rotated or not). So:

    medicine pixels = what differs from the EMPTY photo of the same compartment
                      (after evening out an overall brightness change)
    count           = medicine area / area of one pill   (learned at calibration from a count)

Measured on the real test sheet: pieces of one group touch and merge into one blob, so counting
blobs is useless there, while area stays stable when pieces are moved or put back.

Limits: pills must lie in a single layer (stacked pills hide each other); one compartment
should hold one kind of pill (equal sizes); white pills on white paper are nearly invisible -
use a contrasting tray colour for those.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

CHROMA_DIFF = 14.0   # colour difference from the empty photo (Lab a/b units) that counts as medicine
LIGHT_DIFF = 28.0    # brightness difference (Lab L, 0-255 scale) that counts as medicine


@dataclass
class ContentMeasure:
    area: int              # medicine pixels in the compartment
    fraction: float        # of the compartment's pixels


def _lab(img: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(img, cv2.COLOR_BGR2LAB).astype(np.float32)


INTRUSION_LIGHT_DIFF = 55.0  # darkening that a soft shadow does not reach (measured: shadows stay below)


def _differences(crop: np.ndarray, empty: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per pixel: colour difference and brightness difference from the empty photo."""
    cur, ref = _lab(crop), _lab(empty)
    # Even out an overall brightness change (lamp, daylight): the median is mostly paper.
    ref_l, cur_l = float(np.median(ref[..., 0])), float(np.median(cur[..., 0]))
    if cur_l > 1:
        cur[..., 0] *= ref_l / cur_l
    # Even out a colour cast the same way (webcam white balance resets when it is replugged:
    # measured live, paper turned slightly blue and counts read 4.65 instead of 4).
    for ch in (1, 2):
        cur[..., ch] += float(np.median(ref[..., ch])) - float(np.median(cur[..., ch]))
    chroma = np.sqrt((cur[..., 1] - ref[..., 1]) ** 2 + (cur[..., 2] - ref[..., 2]) ** 2)
    light = np.abs(cur[..., 0] - ref[..., 0])
    return chroma, light


def _clean(mask: np.ndarray) -> np.ndarray:
    mask = mask.astype(np.uint8) * 255
    # Remove sensor-noise specks, then fill small holes (printing on strips, glare).
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))


def medicine_mask(crop: np.ndarray, empty: np.ndarray | None) -> np.ndarray:
    """uint8 mask (255 = medicine) of a compartment crop."""
    if crop is None or crop.size == 0:
        return np.zeros((1, 1), np.uint8)
    if empty is None or empty.shape != crop.shape:
        # No usable empty photo: fall back to "coloured or dark compared with the paper".
        from vision.auto_regions import foreground_mask
        return foreground_mask(crop)
    chroma, light = _differences(crop, empty)
    return _clean((chroma > CHROMA_DIFF) | (light > LIGHT_DIFF))


def intrusion_mask(crop: np.ndarray, empty: np.ndarray | None) -> np.ndarray:
    """Like medicine_mask, but ignoring soft shadows: real colour or STRONG darkening only.

    Used for the border test. A piece's own shadow often reaches the edge of its box (measured
    on the test sheet: 256 edge pixels of pure soft darkening, 0 with colour, 0 strongly dark),
    while a hand or sleeve reaching in has colour or is strongly dark.
    """
    if crop is None or crop.size == 0 or empty is None or empty.shape != crop.shape:
        return medicine_mask(crop, empty)
    chroma, light = _differences(crop, empty)
    return _clean((chroma > CHROMA_DIFF) | (light > INTRUSION_LIGHT_DIFF))


def edge_pixels(mask: np.ndarray) -> tuple[int, int]:
    """Medicine-like pixels on the compartment's border band, and the band's size.

    Regions are drawn with a margin of paper around the medicine, so nothing that belongs in
    the compartment touches its border. Something that does is reaching in from outside: a
    hand, a finger, a sleeve, or a piece left half outside the box.
    """
    h, w = mask.shape[:2]
    band = max(3, int(round(0.03 * min(h, w))))
    edge = np.zeros(mask.shape[:2], dtype=bool)
    edge[:band, :] = edge[-band:, :] = True
    edge[:, :band] = edge[:, -band:] = True
    return int(np.count_nonzero(mask[edge])), int(np.count_nonzero(edge))


def crosses_edge(mask: np.ndarray) -> bool:
    """True when something medicine-like sits on the border (see edge_pixels)."""
    on_edge, band_size = edge_pixels(mask)
    return on_edge >= max(25, 0.01 * band_size)


LINE_CLEARANCE = 7  # px past the drawn line before something counts as being on the other side


def inside_mask(box: list[int], outline: list[list[int]]) -> np.ndarray:
    """uint8 mask (255 = inside the drawn square) in the coordinates of the crop `box`."""
    x, y, w, h = box
    mask = np.zeros((h, w), np.uint8)
    cv2.fillPoly(mask, [np.array(outline, np.int32) - [x, y]], 255)
    return mask


def crosses_line(mask: np.ndarray, inside: np.ndarray) -> bool:
    """True when one connected object lies clearly on BOTH sides of the drawn square's line.

    For drawn compartments the pen line is the border. A piece anywhere inside, even touching
    the line, is on one side only; so is a neighbour's piece seen in the margin. A hand, finger
    or sleeve reaching in must come from outside, across the line: that is what blocks the view.
    (A piece lying across the line between two squares is also reported: it belongs to neither.)
    """
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * LINE_CLEARANCE + 1,) * 2)
    deep_inside = cv2.erode(inside, kernel) > 0
    beyond = cv2.dilate(inside, kernel) == 0
    count, labels = cv2.connectedComponents((mask > 0).astype(np.uint8))
    for i in range(1, count):
        part = labels == i
        if np.count_nonzero(part & deep_inside) >= 25 and np.count_nonzero(part & beyond) >= 25:
            return True
    return False


def measure(crop: np.ndarray, empty: np.ndarray | None) -> ContentMeasure:
    mask = medicine_mask(crop, empty)
    area = int(np.count_nonzero(mask))
    return ContentMeasure(area=area, fraction=area / max(1, mask.size))


def estimate_count(area: float, unit_area: float | None) -> float | None:
    if not unit_area or unit_area <= 0:
        return None
    return area / unit_area


class CountTracker:
    """Confirms a compartment's pill count only after a new value has held steadily.

    Same rule as removals: at least `confirm_observations` consecutive evaluations AND
    `confirm_seconds` agree on the new rounded count, and each estimate must be clearly
    near a whole number. An unusable frame (blur, darkness) restarts the confirmation.

    A count going UP (pills put back) needs a closer match, `max_rounding_error_up`: a false
    increase would hide a dose really taken. Measured live: 3 crumpled pieces re-read as 3.7
    after a hand had shuffled them, and were wrongly confirmed as 4.
    """

    def __init__(self, confirm_observations: int = 2, confirm_seconds: float = 2.0,
                 max_rounding_error: float = 0.35, max_rounding_error_up: float = 0.2,
                 between_seconds: float = 0.0) -> None:
        self.confirm_observations = max(1, confirm_observations)
        self.confirm_seconds = confirm_seconds
        self.max_rounding_error = max_rounding_error
        self.max_rounding_error_up = max_rounding_error_up
        # Clearly FEWER pills but between two counts (a big crumpled piece left reads 1.4):
        # after this long steady, take the nearer count. 0 = never decide (wait for a clear reading).
        self.between_seconds = between_seconds
        self._between = False
        self.confirmed: int | None = None
        self.last_estimate: float | None = None
        self._candidate: int | None = None
        self._since: float | None = None
        self._seen = 0

    def reset(self, count: int | None = None) -> None:
        self.confirmed = count
        self._candidate, self._since, self._seen = None, None, 0

    def interrupt(self) -> None:
        self._candidate, self._since, self._seen = None, None, 0
        self._between = False

    def update(self, estimate: float | None, now: float) -> tuple[int, int] | None:
        """Feed one estimate; returns (old, new) when a changed count is confirmed."""
        self.last_estimate = estimate
        if estimate is None:
            self.interrupt()
            return None
        rounded = int(round(max(0.0, estimate)))
        between = False
        if abs(estimate - rounded) > self.max_rounding_error and rounded > 0:
            clearly_fewer = self.confirmed is not None and estimate < self.confirmed - 0.5
            if not (clearly_fewer and self.between_seconds > 0):
                self.interrupt()  # between two counts: not evidence for either
                return None
            between = True
            if self._candidate is not None and abs(estimate - self._candidate) < 0.6:
                rounded = self._candidate  # 1.38 .. 1.5 flickering: still the same reading
        if (self.confirmed is not None and rounded > self.confirmed
                and abs(estimate - rounded) > self.max_rounding_error_up):
            self.interrupt()  # not clearly more pills: keep the lower count
            return None
        if rounded == self.confirmed:
            self.interrupt()
            return None
        if rounded != self._candidate:
            self._candidate, self._since, self._seen = rounded, now, 1
            self._between = between
        else:
            self._seen += 1
            self._between = self._between or between
        since = self._since if self._since is not None else now  # (0.0 is a valid start time)
        needed = max(self.confirm_seconds, self.between_seconds) if self._between else self.confirm_seconds
        if self._seen >= self.confirm_observations and now - since >= needed:
            old, self.confirmed = self.confirmed, rounded
            self.interrupt()
            return (old if old is not None else rounded, rounded) if old is not None else None
        return None
