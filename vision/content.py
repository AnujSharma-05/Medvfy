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


def medicine_mask(crop: np.ndarray, empty: np.ndarray | None) -> np.ndarray:
    """uint8 mask (255 = medicine) of a compartment crop."""
    if crop is None or crop.size == 0:
        return np.zeros((1, 1), np.uint8)
    if empty is None or empty.shape != crop.shape:
        # No usable empty photo: fall back to "coloured or dark compared with the paper".
        from vision.auto_regions import foreground_mask
        return foreground_mask(crop)
    cur, ref = _lab(crop), _lab(empty)
    # Even out an overall brightness change (lamp, daylight): the median is mostly paper.
    ref_l, cur_l = float(np.median(ref[..., 0])), float(np.median(cur[..., 0]))
    if cur_l > 1:
        cur[..., 0] *= ref_l / cur_l
    chroma = np.sqrt((cur[..., 1] - ref[..., 1]) ** 2 + (cur[..., 2] - ref[..., 2]) ** 2)
    light = np.abs(cur[..., 0] - ref[..., 0])
    mask = ((chroma > CHROMA_DIFF) | (light > LIGHT_DIFF)).astype(np.uint8) * 255
    # Remove sensor-noise specks, then fill small holes (printing on strips, glare).
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    return mask


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
    """

    def __init__(self, confirm_observations: int = 2, confirm_seconds: float = 2.0,
                 max_rounding_error: float = 0.35) -> None:
        self.confirm_observations = max(1, confirm_observations)
        self.confirm_seconds = confirm_seconds
        self.max_rounding_error = max_rounding_error
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

    def update(self, estimate: float | None, now: float) -> tuple[int, int] | None:
        """Feed one estimate; returns (old, new) when a changed count is confirmed."""
        self.last_estimate = estimate
        if estimate is None:
            self.interrupt()
            return None
        rounded = int(round(max(0.0, estimate)))
        if abs(estimate - rounded) > self.max_rounding_error and rounded > 0:
            self.interrupt()  # between two counts: not evidence for either
            return None
        if rounded == self.confirmed:
            self.interrupt()
            return None
        if rounded != self._candidate:
            self._candidate, self._since, self._seen = rounded, now, 1
        else:
            self._seen += 1
        since = self._since if self._since is not None else now  # (0.0 is a valid start time)
        if self._seen >= self.confirm_observations and now - since >= self.confirm_seconds:
            old, self.confirmed = self.confirmed, rounded
            self.interrupt()
            return (old if old is not None else rounded, rounded) if old is not None else None
        return None
