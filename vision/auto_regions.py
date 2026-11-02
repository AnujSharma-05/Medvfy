"""Find the compartment regions automatically on a plain (white) background.

Used instead of drawing boxes by hand with the R key: it finds the groups of objects
that stand out from the paper (by colour saturation or darkness), merges nearby
pieces of one group, and returns one box per compartment with a safety margin.

Compartments are lettered in reading order: top row left to right, then the next row
(A top-left, B top-right, C bottom-left, D bottom-right for a 2x2 layout).

Limits: needs a light, fairly uniform background and visibly separated groups. White
or very pale pills on white paper are hard to see; the caller reports how many groups
were found so a wrong count is visible instead of silently mislabelled.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


class RegionsNotFound(ValueError):
    """The image does not show the expected number of separate groups."""


def foreground_mask(frame: np.ndarray) -> np.ndarray:
    """Pixels that differ from plain paper: coloured (saturated) or clearly darker than the paper."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(np.int16)
    val = hsv[:, :, 2].astype(np.int16)
    # Paper brightness varies across the frame (vignetting, shadows): compare with a local estimate.
    local_paper = cv2.medianBlur(hsv[:, :, 2], 51).astype(np.int16)
    paper_sat = int(np.median(sat))
    mask = ((sat > paper_sat + 45) | (val < local_paper - 55)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    return mask


@dataclass
class Group:
    """One group of objects on the sheet: its region (with margin) and a colour name for people."""
    box: list[int]
    area: int
    color: str


def color_name(frame: np.ndarray, mask: np.ndarray) -> str:
    """Rough colour of the masked pixels, to help a person tell the groups apart."""
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[mask > 0]
    if hsv.size == 0:
        return "unknown"
    sat = hsv[:, 1] > 60
    strong = float(np.mean(hsv[:, 1] > 90))
    value = float(np.median(hsv[:, 2]))
    # Webcams tint dark foil slightly, so "dark" is judged by brightness and weak colour together
    # (measured on the test sheet: dark strips V 53-87 with 24-26% strong colour).
    if value < 110 and strong < 0.4:
        return "dark"
    if sat.mean() < 0.3:
        return "light" if value >= 140 else "grey"
    hue = float(np.median(hsv[sat, 0]))  # OpenCV hue runs 0-179; on the test sheet: mint 80-84,
    for limit, name in ((8, "red"), (22, "orange"), (34, "yellow"), (90, "green"),  # blue 103-104,
                        (115, "blue"), (150, "purple"), (172, "pink"), (180, "red")):  # purple 121-125
        if hue < limit:
            return name
    return "red"


def reading_order(boxes: list[list[int]], frame_height: int) -> list[list[int]]:
    """Top row first (a new row when centres differ by over 20% of the height), left to right."""
    rows: list[list[list[int]]] = []
    for b in sorted(boxes, key=lambda b: b[1] + b[3] / 2):
        cy = b[1] + b[3] / 2
        if rows and abs(cy - (rows[-1][0][1] + rows[-1][0][3] / 2)) < frame_height * 0.2:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [b for row in rows for b in sorted(row, key=lambda b: b[0])]


def detect_groups(frame: np.ndarray, margin: int = 30, min_area_fraction: float = 0.0015,
                  edge_px: int = 3) -> tuple[list[Group], int]:
    """Groups of objects lying on the sheet, in reading order, plus how many were ignored.

    Anything whose pixels touch the edge of the picture is ignored: medicine lies on the
    sheet with paper all around it, while the desk, a stand, or a hand comes in from outside.
    """
    if frame is None or frame.size == 0:
        raise RegionsNotFound("No image")
    h, w = frame.shape[:2]
    mask = foreground_mask(frame)
    edge = np.zeros_like(mask, dtype=bool)
    edge[:edge_px, :] = edge[-edge_px:, :] = True
    edge[:, :edge_px] = edge[:, -edge_px:] = True
    # Merge the separate pills of one group into one blob.
    merge = max(15, int(min(h, w) * 0.08)) | 1
    grouped = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (merge, merge)))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(grouped)
    min_area = min_area_fraction * h * w
    pad = merge // 2
    groups, ignored = [], 0
    for i in range(1, count):
        real_mask = (labels == i) & (mask > 0)  # the real (undilated) object pixels of this group
        real = int(np.count_nonzero(real_mask))
        if real < min_area:
            continue
        # A group may graze the edge (5 px on the test sheet); background runs off it
        # (a stand: 1183 px, the desk: 409 px).
        if np.count_nonzero(real_mask & edge) >= max(25, 0.02 * real):
            ignored += 1
            continue
        x, y, bw, bh = (int(v) for v in stats[i][:4])
        # Shrink the dilation back off, then add the margin, clamped to the frame.
        x0, y0 = max(0, x + pad - margin), max(0, y + pad - margin)
        x1, y1 = min(w, x + bw - pad + margin), min(h, y + bh - pad + margin)
        groups.append(Group([x0, y0, x1 - x0, y1 - y0], real, color_name(frame, real_mask.astype(np.uint8))))
    by_box = {tuple(g.box): g for g in groups}
    ordered = [by_box[tuple(b)] for b in reading_order([g.box for g in groups], h)]
    return ordered, ignored


def find_groups(frame: np.ndarray, margin: int = 30, min_area_fraction: float = 0.0015) -> list[list[int]]:
    """Boxes of the groups on the sheet, in reading order (see detect_groups)."""
    return [g.box for g in detect_groups(frame, margin, min_area_fraction)[0]]


def find_regions(frame: np.ndarray, slots: list[str], assign: dict[str, int] | None = None,
                 margin: int = 30, min_area_fraction: float = 0.0015) -> dict[str, list[int]]:
    """One [x, y, w, h] box per slot. Raises RegionsNotFound.

    By default the largest groups are lettered in reading order. `assign` maps letters to
    group numbers as shown by the preview (1 = first in reading order), e.g. {"A": 2, ...},
    for when the camera's view is rotated relative to the LED layout.
    """
    groups, ignored = detect_groups(frame, margin, min_area_fraction)
    note = f" ({ignored} object(s) touching the picture edge were ignored)" if ignored else ""
    if assign:
        letters = {str(k).upper(): int(v) for k, v in assign.items()}
        if sorted(letters) != sorted(slots) or len(set(letters.values())) != len(slots):
            raise RegionsNotFound(f"Give each of {', '.join(slots)} a different group number.")
        bad = [n for n in letters.values() if not 1 <= n <= len(groups)]
        if bad:
            raise RegionsNotFound(f"There is no group {bad[0]}: the preview shows {len(groups)} group(s){note}.")
        chosen = {s: groups[letters[s] - 1].box for s in slots}
    else:
        if len(groups) < len(slots):
            raise RegionsNotFound(f"Found {len(groups)} group(s) of objects on the sheet, need {len(slots)}{note}. "
                                  "Every group must be fully inside the picture with paper all around it.")
        largest = sorted(groups, key=lambda g: g.area, reverse=True)[: len(slots)]
        keep = {id(g) for g in largest}
        chosen = {s: g.box for s, g in zip(slots, [g for g in groups if id(g) in keep])}

    # Boxes must not overlap, or one compartment's change would show in another's region.
    boxes = list(chosen.values())
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if a[0] < b[0] + b[2] and b[0] < a[0] + a[2] and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]:
                raise RegionsNotFound("Two groups are too close together to separate. Move them apart.")
    return chosen


def draw_regions(frame: np.ndarray, rois: dict[str, list[int]], states: dict[str, str] | None = None) -> np.ndarray:
    """Annotated copy of the frame: one labelled box per compartment."""
    view = frame.copy()
    for slot, (x, y, w, h) in rois.items():
        state = (states or {}).get(slot, "")
        color = {"EXPECTED_PRESENT": (0, 170, 0), "REMOVAL_DETECTED": (0, 0, 230),
                 "PARTIAL_OR_WRONG": (0, 140, 255), "UNCERTAIN": (0, 100, 255)}.get(state, (200, 120, 0))
        cv2.rectangle(view, (x, y), (x + w, y + h), color, 2)
        label = f"{slot} {state}".strip()
        cv2.putText(view, label, (x + 4, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3)
        cv2.putText(view, label, (x + 4, y + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 1)
    return view
