"""Region of Interest (ROI) Manager for MedVerify.

Provides structured validation, bounds checking, resolution matching,
and atomic persistence for compartment observation regions.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


class ROIManager:
    """Manages compartment bounding boxes and camera layout validation."""

    def __init__(self, roi_file: Path | str) -> None:
        self.roi_path = Path(roi_file)

    def load(
        self,
        slots: list[str],
        frame_shape: tuple[int, int] | tuple[int, int, int],
        camera_index: int | None = None,
    ) -> dict[str, list[int]] | None:
        """Load and strictly validate ROIs against the current frame dimensions.

        Args:
            slots: Expected compartment IDs (e.g. ['A', 'B', 'C', 'D'])
            frame_shape: (height, width, ...) of current camera frame
            camera_index: Optional current camera index to verify match

        Returns:
            dict mapping slot -> [x, y, w, h], or None if invalid or missing
        """
        if not self.roi_path.exists():
            return None

        try:
            data = json.loads(self.roi_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

        # Support both new schema {"version": 2, "rois": {...}, ...} and legacy flat {"A": [...]}
        if "rois" in data and isinstance(data["rois"], dict):
            rois_dict = data["rois"]
            saved_w = data.get("frame_width")
            saved_h = data.get("frame_height")
            saved_cam = data.get("camera_index")

            # Check camera index match if specified
            if camera_index is not None and saved_cam is not None:
                if int(saved_cam) != int(camera_index):
                    return None
        else:
            rois_dict = data
            saved_w, saved_h = None, None

        height, width = frame_shape[:2]

        if saved_w is not None and saved_w != width:
            return None
        if saved_h is not None and saved_h != height:
            return None

        validated: dict[str, list[int]] = {}
        for slot in slots:
            if slot not in rois_dict:
                return None

            try:
                x, y, w, h = map(int, rois_dict[slot])
            except (ValueError, TypeError):
                return None

            # Boundary checks
            if x < 0 or y < 0 or w < 15 or h < 15:
                return None
            if x + w > width or y + h > height:
                return None

            validated[slot] = [x, y, w, h]

        return validated

    def save(
        self,
        rois: dict[str, list[int]],
        frame_shape: tuple[int, int] | tuple[int, int, int],
        camera_index: int | None = None,
    ) -> None:
        """Atomically save ROIs along with frame dimension metadata."""
        height, width = frame_shape[:2]
        payload = {
            "version": 2,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "camera_index": camera_index,
            "frame_width": width,
            "frame_height": height,
            "rois": {slot: [int(v) for v in box] for slot, box in rois.items()},
        }

        self.roi_path.parent.mkdir(parents=True, exist_ok=True)
        dir_name = self.roi_path.parent

        with tempfile.NamedTemporaryFile(
            dir=dir_name, suffix=".json", delete=False, mode="w", encoding="utf-8"
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(payload, tmp, indent=2)

        tmp_path.replace(self.roi_path)

    def crop(
        self, frame: np.ndarray, rois: dict[str, list[int]]
    ) -> dict[str, np.ndarray]:
        """Crop compartment regions from a frame."""
        crops: dict[str, np.ndarray] = {}
        for slot, (x, y, w, h) in rois.items():
            crops[slot] = frame[y : y + h, x : x + w].copy()
        return crops

    def delete(self) -> None:
        if self.roi_path.exists():
            try:
                self.roi_path.unlink()
            except OSError:
                pass
